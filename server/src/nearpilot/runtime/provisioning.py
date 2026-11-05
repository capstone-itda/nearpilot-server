"""기기 접속 설정 묶음과 mDNS 알림 (#234, specs/iot-ble-provisioning/spec.md 6절).

`FleetProvisioningSource`는 `[provisioning].fleet`의 기기 목록과 기기별 비밀번호 파일로 묶음을 만든다.
`MdnsAdvertiser`는 `<mdns_name>.local`을 `mdns_address`로 알린다. 시작에 실패해도 서버를 멈추지 않는다.
묶음과 비밀번호는 로그에 쓰지 않는다 (NFR-08).
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nearpilot.runtime.config import ProvisioningConfig
from nearpilot.shared.enums import Capability, Occupancy
from nearpilot.shared.models import ProvisioningBundle

log = logging.getLogger(__name__)

SERVICE_TYPE = "_mqtt._tcp.local."


def mdns_host(name: str) -> str:
    """기기와 묶음에 넣는 MQTT 주소."""
    return f"{name}.local"


class FleetProvisioningSource:
    """`ProvisioningSource` 구현. 조회할 때마다 파일을 읽는다 — 운영 중 목록과 비밀번호를 바꿔도 반영한다."""

    def __init__(self, config: ProvisioningConfig, mqtt_port: int) -> None:
        self._fleet = config.fleet
        self._host = mdns_host(config.mdns_name)
        self._port = mqtt_port

    def bundle(self, node_id: str) -> ProvisioningBundle | None:
        try:
            fleet = json.loads(self._fleet.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.error("기기 목록 파일을 읽지 못했다: %s", self._fleet)
            return None
        devices = fleet.get("devices") if isinstance(fleet, dict) else None
        device = next((d for d in devices or () if isinstance(d, dict) and d.get("node_id") == node_id), None)
        if device is None:
            return None
        try:
            capability, occupancy = Capability(device["capability"]), Occupancy(device["occupancy"])
        except (KeyError, ValueError):
            log.error("기기 목록의 기능·점유 방식이 계약 밖이다: node=%s", node_id)
            return None
        user = device.get("username") or node_id
        raw = device.get("password_file")
        path = Path(raw) if isinstance(raw, str) and raw else self._fleet.parent / f".env.{node_id}"
        if not path.is_absolute():
            path = self._fleet.parent / path
        try:
            password = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            log.warning("기기 비밀번호 파일이 없다: node=%s", node_id)
            return None
        if not password or not isinstance(user, str):
            return None
        return ProvisioningBundle(node_id, user, password, self._host, self._port, capability, occupancy)


def _zeroconf() -> Any:
    from zeroconf import Zeroconf  # 서버를 띄울 때만 가져온다

    return Zeroconf()


def _service(name: str, address: str, port: int) -> Any:
    from zeroconf import ServiceInfo

    return ServiceInfo(SERVICE_TYPE, f"{name}.{SERVICE_TYPE}", port=port,
                       addresses=[socket.inet_aton(address)], server=f"{mdns_host(name)}.")


class MdnsAdvertiser:
    """`<mdns_name>.local` 알림. `start`는 실패해도 예외를 내지 않는다 (spec 6절)."""

    def __init__(self, config: ProvisioningConfig, mqtt_port: int, *,
                 zeroconf_factory: Callable[[], Any] = _zeroconf,
                 service_factory: Callable[[str, str, int], Any] = _service) -> None:
        self._config = config
        self._port = mqtt_port
        self._zeroconf_factory = zeroconf_factory
        self._service_factory = service_factory
        self._zeroconf: Any = None
        self._info: Any = None

    @property
    def running(self) -> bool:
        return self._info is not None

    def start(self) -> None:
        name, address = self._config.mdns_name, self._config.mdns_address
        try:
            info = self._service_factory(name, address, self._port)
            self._zeroconf = self._zeroconf_factory()
            self._zeroconf.register_service(info)
        except Exception:  # noqa: BLE001 - 기기는 IP 설정으로도 접속한다. 묶음 조회는 계속한다
            log.exception("mDNS 알림을 시작하지 못했다: %s → %s. 기기의 .local 주소 찾기가 실패한다",
                          mdns_host(name), address)
            self._close()
            return
        self._info = info
        log.info("mDNS 알림: %s → %s (MQTT %d)", mdns_host(name), address, self._port)

    def stop(self) -> None:
        if self._info is not None and self._zeroconf is not None:
            try:
                self._zeroconf.unregister_service(self._info)
            except Exception:  # noqa: BLE001
                log.warning("mDNS 알림 해제 실패")
        self._info = None
        self._close()

    def _close(self) -> None:
        if self._zeroconf is not None:
            try:
                self._zeroconf.close()
            except Exception:  # noqa: BLE001
                log.warning("mDNS 종료 실패")
        self._zeroconf = None
