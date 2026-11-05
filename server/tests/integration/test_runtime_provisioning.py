"""기기 접속 설정 묶음과 mDNS 알림 (NFR-12, #234, specs/iot-ble-provisioning/spec.md)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from _runtime import make_config
from nearpilot.runtime.config import ConfigError, ProvisioningConfig, parse_config
from nearpilot.runtime.provisioning import FleetProvisioningSource, MdnsAdvertiser
from nearpilot.shared.enums import Capability, Occupancy
from nearpilot.shared.interfaces import ProvisioningSource

SECRET = "node-secret-0123456789"


def write_fleet(folder: Path, devices: list[dict]) -> Path:
    path = folder / "fleet.json"
    path.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    return path


@pytest.fixture
def fleet(tmp_path):
    path = write_fleet(tmp_path, [
        {"node_id": "light-1", "capability": "light", "occupancy": "shared"},
        {"node_id": "locker-1", "capability": "locker", "occupancy": "rental", "username": "acct-locker",
         "password_file": "secrets/locker.txt"},
        {"node_id": "bad-1", "capability": "door", "occupancy": "shared"},
    ])
    (tmp_path / ".env.light-1").write_text(SECRET + "\n", encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "locker.txt").write_text("locker-secret\n", encoding="utf-8")
    return path


def source(fleet: Path) -> FleetProvisioningSource:
    return FleetProvisioningSource(ProvisioningConfig(fleet, "nearpilot", "10.0.0.5"), 18883)


def test_NFR12_fleet_source_builds_bundle(fleet):
    target = source(fleet)
    assert isinstance(target, ProvisioningSource)
    bundle = target.bundle("light-1")
    assert (bundle.node_id, bundle.mqtt_user, bundle.mqtt_password, bundle.mqtt_host, bundle.mqtt_port,
            bundle.capability, bundle.occupancy) == (
        "light-1", "light-1", SECRET, "nearpilot.local", 18883, Capability.LIGHT, Occupancy.SHARED)
    assert SECRET not in repr(bundle)  # repr 에 비밀번호를 쓰지 않는다 (NFR-08)
    locker = target.bundle("locker-1")
    assert (locker.mqtt_user, locker.mqtt_password) == ("acct-locker", "locker-secret")


def test_NFR12_fleet_source_missing_device(fleet, tmp_path, caplog):
    target = source(fleet)
    assert target.bundle("light-9") is None  # 목록에 없다
    assert target.bundle("bad-1") is None  # 기능이 계약 밖이다
    (tmp_path / ".env.light-1").unlink()
    assert target.bundle("light-1") is None  # 비밀번호 파일이 없다
    fleet.write_text("not json", encoding="utf-8")
    assert target.bundle("locker-1") is None
    assert "secret" not in caplog.text


def raw_config(tmp_path, provisioning):
    config = make_config(tmp_path)
    raw = {"settings": {"version": "t1"}, "http": {"host": "127.0.0.1", "port": 8000},
           "database": {"path": "np.db", "audit_jsonl": "audit.jsonl"},
           "proximity": {"model_root": str(config.proximity.model_root),
                         "models": dict(config.proximity.versions)},
           "mqtt": {"host": "127.0.0.1", "port": 18883, "client_id": "s", "username": "nearpilot-server",
                    "password_file": ".env.mqtt-password", "keepalive_sec": 30, "connect_timeout_sec": 1.0,
                    "start_timeout_sec": 2.0, "stop_timeout_sec": 2.0, "reconnect_min_sec": 1,
                    "reconnect_max_sec": 2},
           "delivery": {"clock_max_error_sec": 0.5, "clock_trust_sec": 120, "clock_resync_sec": 60,
                        "pending_limit": 10, "retry_interval_sec": 0.5, "stop_timeout_sec": 1.0}}
    if provisioning is not None:
        raw["provisioning"] = provisioning
    return parse_config(raw, base=tmp_path)


def test_NFR12_provisioning_config_validated(tmp_path, fleet):
    assert raw_config(tmp_path, None).provisioning is None
    good = {"fleet": "fleet.json", "mdns_name": "nearpilot", "mdns_address": "10.0.0.5"}
    assert raw_config(tmp_path, good).provisioning == ProvisioningConfig(fleet.resolve(), "nearpilot", "10.0.0.5")
    for changes in ({"fleet": "missing.json"}, {"mdns_name": "NearPilot"}, {"mdns_name": "near.pilot"},
                    {"mdns_name": "nearpilot.local"}, {"mdns_name": "-np"}, {"mdns_address": "nearpilot"},
                    {"mdns_address": "0.0.0.0"}, {"mdns_address": "224.0.0.251"}, {"mdns_address": 10},
                    {"extra": 1}):
        with pytest.raises(ConfigError):
            raw_config(tmp_path, good | changes)
    with pytest.raises(ConfigError):
        raw_config(tmp_path, {"fleet": "fleet.json", "mdns_name": "nearpilot"})


class FakeZeroconf:
    def __init__(self, fail=False):
        self.fail = fail
        self.registered, self.unregistered, self.closed = [], [], False

    def register_service(self, info):
        if self.fail:
            raise OSError("port 5353 in use")
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


def test_NFR12_mdns_registers_host_and_stops(fleet):
    zeroconf = FakeZeroconf()
    advertiser = MdnsAdvertiser(ProvisioningConfig(fleet, "nearpilot", "10.0.0.5"), 18883,
                                zeroconf_factory=lambda: zeroconf)
    advertiser.start()
    [info] = zeroconf.registered
    assert info.server == "nearpilot.local." and info.port == 18883
    assert info.parsed_addresses() == ["10.0.0.5"]
    assert advertiser.running
    advertiser.stop()
    assert zeroconf.unregistered == [info] and zeroconf.closed and not advertiser.running


def test_NFR12_mdns_failure_keeps_server(fleet, caplog):
    zeroconf = FakeZeroconf(fail=True)
    advertiser = MdnsAdvertiser(ProvisioningConfig(fleet, "nearpilot", "10.0.0.5"), 18883,
                                zeroconf_factory=lambda: zeroconf)
    with caplog.at_level(logging.ERROR):
        advertiser.start()  # 예외를 내지 않는다
    assert not advertiser.running and zeroconf.closed
    assert "nearpilot.local" in caplog.text
    advertiser.stop()


class FakeAdvertiser:
    def __init__(self, config, port):
        self.config, self.port, self.calls = config, port, []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


def test_FR12A_runtime_serves_bundle_and_mdns(tmp_path):
    """조립: [provisioning] 이 있으면 mDNS 를 시작·정지하고 소유자에게만 묶음을 준다."""
    from dataclasses import replace

    from _runtime import NODES, OWNER, VISITOR_A, SimBroker, running, seed
    from nearpilot.runtime import Runtime

    cfg = make_config(tmp_path)
    node_id = next(iter(NODES))
    capability, occupancy = NODES[node_id]
    fleet = write_fleet(tmp_path, [{"node_id": node_id, "capability": capability, "occupancy": occupancy}])
    (tmp_path / f".env.{node_id}").write_text(SECRET, encoding="utf-8")
    cfg = replace(cfg, provisioning=ProvisioningConfig(fleet, "nearpilot", "10.0.0.5"))
    seed(cfg.database.path)
    broker = SimBroker(tmp_path)
    runtime = Runtime(cfg, client_factory=broker.factory, mdns_factory=FakeAdvertiser)
    with running(runtime):
        assert runtime.mdns.calls == ["start"] and runtime.mdns.port == cfg.mqtt.port
        bundle = runtime.nodes.provisioning(OWNER, node_id)
        assert (bundle.mqtt_password, bundle.mqtt_host) == (SECRET, "nearpilot.local")
        with pytest.raises(PermissionError):
            runtime.nodes.provisioning(VISITOR_A, node_id)
        with pytest.raises(LookupError):
            runtime.nodes.provisioning(OWNER, list(NODES)[1])  # 소유한 기기지만 목록에 없다
    assert runtime.mdns.calls == ["start", "stop"]
