"""서버 TOML 설정 (specs/core-server-assembly/spec.md 6절 "설정과 모델").

운영 `Settings`, runtime 실행 설정, HTTP·DB·근접 모델·MQTT·보고 설정과 선택 섹션 `[dashboard]`
(specs/core-dashboard-queries/spec.md 2절)를 한 파일에서 읽는다.
상대 경로는 설정 파일의 부모 디렉터리 기준이다. 알 수 없는 키는 거부한다.
비밀은 파일 경로로만 받는다. 오류 메시지에는 키 이름만 쓰고 값을 쓰지 않는다 (NFR-08).
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.iot.mqtt.connection import MqttConfig
from nearpilot.iot.mqtt.delivery import DeliveryConfig
from nearpilot.shared.config import EXEC_TIMEOUT_PREFIX, LEGACY_PARAM_KEYS, Settings, migrate_settings_table

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """설정을 쓸 수 없다. 서버는 요청을 받지 않는다."""


@dataclass(frozen=True)
class RuntimeSettings:
    """runtime 실행 설정. 초기값은 spec 6절 "주기 작업"과 "종료와 부분 실패"의 값이다."""

    sqlite_busy_timeout_ms: int = 5000
    shutdown_timeout_sec: float = 30.0
    execution_tick_sec: float = 0.25  # 실행 저장 재시도와 예약 만료
    release_tick_sec: float = 0.25  # 저장 결과가 불명인 반납 종료의 재확인
    restore_tick_sec: float = 0.25  # 미완료 복원 재시도
    recovery_tick_sec: float = 0.25  # 복구 저장 재시도
    shared_light_tick_sec: float = 1.0  # 공용 조명 이탈·유지 권한 검사
    occupancy_tick_sec: float = 1.0  # 대여형 기기의 점유 표시 발행

    def __post_init__(self) -> None:
        v = self.sqlite_busy_timeout_ms
        if type(v) is not int or v < 0:
            raise ConfigError("runtime.sqlite_busy_timeout_ms 는 0 이상의 정수여야 한다")
        for f in fields(self):
            if f.name == "sqlite_busy_timeout_ms":
                continue
            v = getattr(self, f.name)
            if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
                raise ConfigError(f"runtime.{f.name} 는 양수여야 한다")


@dataclass(frozen=True)
class HttpConfig:
    host: str
    port: int
    public_hosts: tuple[str, ...] = ()  # 터널 같은 공개 주소의 Host. `/mcp` 의 Host 검사가 받는다


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path
    audit_jsonl: Path  # 모든 연결이 같은 절대 경로를 쓴다


@dataclass(frozen=True)
class ProximityConfig:
    model_root: Path  # `{model_version}.json` 파일이 있는 폴더
    model_version: str | None  # 공통 모델 하나. require_models=False 이고 설정에 없으면 None. 파일 이름으로 최신을 추측하지 않는다


@dataclass(frozen=True)
class ProvisioningConfig:
    """선택 섹션 `[provisioning]` (#234). 기기 접속 설정 묶음과 mDNS 알림. 없으면 둘 다 하지 않는다."""

    fleet: Path  # 기기 목록 파일 (fleet.json)
    mdns_name: str  # `<mdns_name>.local`
    mdns_address: str  # 알릴 IPv4 주소


@dataclass(frozen=True)
class DashboardConfig:
    """선택 섹션 `[dashboard]`. 없으면 대시보드를 열지 않는다."""

    api: DashboardSettings  # queue_max_events, batch_max_events, auth_timeout_sec, audit_limit_max
    weak_rssi_dbm: float  # 장소 목록의 weakRssiDbm
    tail_interval_sec: float  # 따라 읽기 간격
    reports_dir: Path  # 평가 리포트 폴더. cli 의 `dirs_for` 와 같은 규칙이다: 설정 파일 폴더의 부모 / var / reports


@dataclass(frozen=True)
class SmartThingsBinding:
    """동반 기기 `node_id`와 SmartThings `deviceId`의 쌍. `deviceId`는 호스트·로그·감사에 남기지 않는다 (NFR-08)."""

    node_id: str
    device_id: str = field(repr=False)


@dataclass(frozen=True)
class SmartThingsConfig:
    """선택 섹션 `[smartthings]` (specs/iot-smartthings-companion). 없으면 모든 명령을 MQTT로 보낸다."""

    token_file: Path  # JSON 객체의 access_token. 요청마다 다시 읽는다
    confirm_poll_sec: float  # 명령 뒤 상태 확인 조회 간격
    request_timeout_sec: float  # HTTP 요청 하나의 제한 시간
    bindings: tuple[SmartThingsBinding, ...]


@dataclass(frozen=True)
class ServerConfig:
    source: Path
    settings: Settings
    runtime: RuntimeSettings
    http: HttpConfig
    database: DatabaseConfig
    proximity: ProximityConfig
    mqtt: MqttConfig
    delivery: DeliveryConfig
    dashboard: DashboardConfig | None = None  # [dashboard] 가 없으면 None
    provisioning: ProvisioningConfig | None = None  # [provisioning] 이 없으면 None (#234)
    smartthings: SmartThingsConfig | None = None  # [smartthings] 가 없으면 None


_SECTIONS = {"settings", "runtime", "http", "database", "proximity", "mqtt", "delivery", "dashboard", "provisioning",
             "smartthings"}
_REQUIRED_SECTIONS = _SECTIONS - {"runtime", "dashboard", "provisioning", "smartthings"}
# mDNS 이름 하나(DNS 라벨). `.local` 은 붙이지 않는다.
_MDNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def load_config(path: str | Path, *, require_models: bool = True) -> ServerConfig:
    """설정 파일을 읽고 검증한다. 잘못되면 `ConfigError` 다.

    `require_models=False` 면 `[proximity] model_version` 이 없어도 된다. 모델 없이 도는 명령이 쓴다.
    """
    source = Path(path).resolve()
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"설정 파일을 읽을 수 없다: {source}") from error
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"설정 파일이 TOML 이 아니다: {error}") from None
    return parse_config(raw, base=source.parent, source=source, require_models=require_models)


def parse_config(raw: Mapping[str, Any], *, base: Path, source: Path | None = None,
                 require_models: bool = True) -> ServerConfig:
    _keys(raw, "", required=_REQUIRED_SECTIONS, allowed=_SECTIONS)
    try:
        return ServerConfig(
            source=source or base,
            settings=_settings(_table(raw, "settings")),
            runtime=_runtime(_table(raw, "runtime", optional=True)),
            http=_http(_table(raw, "http")),
            database=_database(_table(raw, "database"), base),
            proximity=_proximity(_table(raw, "proximity"), base, require_models=require_models),
            mqtt=_mqtt(_table(raw, "mqtt"), base),
            delivery=_delivery(_table(raw, "delivery")),
            dashboard=_dashboard(_table(raw, "dashboard"), base) if "dashboard" in raw else None,
            provisioning=_provisioning(_table(raw, "provisioning"), base) if "provisioning" in raw else None,
            smartthings=_smartthings(_table(raw, "smartthings"), base) if "smartthings" in raw else None,
        )
    except ConfigError:
        raise
    except (TypeError, ValueError) as error:
        # 각 계층의 검증 오류다. 메시지는 키 이름만 담는다.
        raise ConfigError(str(error)) from None


# ── 절별 변환 ──────────────────────────────────────────────────────


def _settings(table: Mapping[str, Any]) -> Settings:
    """`[settings]`. 종류별 임계값은 `threshold_{kind}`, 인자 설정은 `[settings.param_max.{kind}.{action}]` 표다.

    옛 조명·스피커 키는 새 형식으로 옮겨 읽고 경고를 남긴다 (requirements.md §1.2, #245).
    """
    names = {f.name for f in fields(Settings)} - {"thresholds", "exec_timeouts"}
    thresholds = {key for key in table if key.startswith("threshold_")}
    # 종류별 실행 응답 제한은 따옴표 키 `"exec_timeout_sec.{kind}"` 로 쓴다 (#271)
    exec_timeouts = {key for key in table if key.startswith(EXEC_TIMEOUT_PREFIX)}
    _keys(table, "settings", required={"version"},
          allowed=names | thresholds | exec_timeouts | set(LEGACY_PARAM_KEYS))
    if not isinstance(table["version"], str) or not table["version"].strip():
        raise ConfigError("settings.version 은 빈 문자열이 아니어야 한다")
    values, legacy = migrate_settings_table(table)
    for key in legacy:
        section, kind, action, arg = LEGACY_PARAM_KEYS[key]
        log.warning("옛 설정 키 settings.%s 를 settings.%s.%s.%s.%s 로 옮겨 읽는다", key, section, kind, action, arg)
    if "any_allowed_kinds" in values:
        if not isinstance(values["any_allowed_kinds"], list):
            raise ConfigError("settings.any_allowed_kinds 는 종류 이름의 목록이어야 한다")
        values["any_allowed_kinds"] = tuple(values["any_allowed_kinds"])
    return Settings(**values)


def _runtime(table: Mapping[str, Any]) -> RuntimeSettings:
    _keys(table, "runtime", required=set(), allowed={f.name for f in fields(RuntimeSettings)})
    return RuntimeSettings(**table)


def _http(table: Mapping[str, Any]) -> HttpConfig:
    _keys(table, "http", required={"host", "port"}, allowed={"host", "port", "public_hosts"})
    host, port = table["host"], table["port"]
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("http.host 는 빈 문자열이 아니어야 한다")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("http.port 는 1~65535 정수여야 한다")
    public_hosts = table.get("public_hosts", [])
    if not isinstance(public_hosts, list) or not all(
            isinstance(h, str) and h.strip() and h == h.strip() and "/" not in h for h in public_hosts):
        raise ConfigError("http.public_hosts 는 Host 문자열(스킴·경로 없음)의 배열이어야 한다")
    return HttpConfig(host, port, tuple(public_hosts))


def _database(table: Mapping[str, Any], base: Path) -> DatabaseConfig:
    _keys(table, "database", required={"path", "audit_jsonl"})
    return DatabaseConfig(_path(table, "database", "path", base),
                          _path(table, "database", "audit_jsonl", base))


def _proximity(table: Mapping[str, Any], base: Path, *, require_models: bool) -> ProximityConfig:
    # 장소별 `[proximity.models]` 표는 받지 않는다. 공통 모델 하나를 model_version 으로 고른다 (#252).
    if "models" in table:
        raise ConfigError("알 수 없는 설정 키: proximity.models "
                          "(`[proximity.models]`는 #252로 없어졌다. `model_version` 하나를 쓴다)")
    # model_root 는 항상 필수다. model_version 은 있으면 형식을 검사하고, require_models 일 때만 꼭 있어야 한다.
    _keys(table, "proximity", required={"model_root", "model_version"} if require_models else {"model_root"},
          allowed={"model_root", "model_version"})
    version = table.get("model_version")
    if "model_version" in table and (not isinstance(version, str) or not version.strip()):
        raise ConfigError("proximity.model_version 은 비어 있지 않은 문자열이어야 한다")
    return ProximityConfig(_path(table, "proximity", "model_root", base), version)


def _mqtt(table: Mapping[str, Any], base: Path) -> MqttConfig:
    names = {f.name for f in fields(MqttConfig)}
    _keys(table, "mqtt", required=names)  # 임의 기본값을 넣지 않는다
    values = dict(table)
    values["password_file"] = _path(table, "mqtt", "password_file", base)
    return MqttConfig(**values)


def _delivery(table: Mapping[str, Any]) -> DeliveryConfig:
    if "clock_skew_sec" in table:  # #236 이전 설정. 기기별 실측 오차 표는 없어졌다
        raise ConfigError("delivery.clock_skew_sec 는 없어졌다 (#236). "
                          "delivery.clock_max_error_sec, clock_trust_sec, clock_resync_sec 를 쓴다")
    names = {f.name for f in fields(DeliveryConfig)}
    _keys(table, "delivery", required=names)
    try:
        return DeliveryConfig(**table)
    except ValueError as error:  # 값은 쓰지 않고 키 이름만 알린다
        name = str(error).rsplit(": ", 1)[-1]
        raise ConfigError(f"delivery.{name} 가 허용 범위 밖이다") from None


def _provisioning(table: Mapping[str, Any], base: Path) -> ProvisioningConfig:
    _keys(table, "provisioning", required={"fleet", "mdns_name", "mdns_address"})
    fleet = _path(table, "provisioning", "fleet", base)
    if not fleet.is_file():
        raise ConfigError(f"provisioning.fleet 파일이 없다: {fleet}")
    name = table["mdns_name"]
    if not isinstance(name, str) or not _MDNS_NAME.fullmatch(name):
        raise ConfigError("provisioning.mdns_name 은 영문 소문자·숫자·하이픈의 이름 하나다 (.local 없이)")
    try:
        address = ipaddress.IPv4Address(table["mdns_address"])
    except (ValueError, TypeError):
        raise ConfigError("provisioning.mdns_address 는 IPv4 주소 문자열이어야 한다") from None
    if not isinstance(table["mdns_address"], str) or address.is_unspecified or address.is_multicast:
        raise ConfigError("provisioning.mdns_address 는 기기가 접속할 IPv4 주소여야 한다")
    return ProvisioningConfig(fleet, name, str(address))


def _smartthings(table: Mapping[str, Any], base: Path) -> SmartThingsConfig:
    """`[smartthings]`. 바인딩은 `[[smartthings.bindings]]` 의 `node_id`·`device_id` 쌍이다.

    오류 메시지에는 몇 번째 바인딩인지만 쓰고 `device_id` 값은 쓰지 않는다 (NFR-08).
    """
    _keys(table, "smartthings", required={"token_file", "confirm_poll_sec", "request_timeout_sec", "bindings"})
    for key in ("confirm_poll_sec", "request_timeout_sec"):
        if not _finite_number(table[key]) or table[key] <= 0:
            raise ConfigError(f"smartthings.{key} 는 0보다 큰 유한한 수여야 한다")
    raw = table["bindings"]
    if not isinstance(raw, list) or not raw:
        raise ConfigError("smartthings.bindings 는 바인딩 하나 이상의 배열이어야 한다")
    bindings: list[SmartThingsBinding] = []
    for index, item in enumerate(raw):
        where = f"smartthings.bindings[{index}]"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{where} 는 표여야 한다")
        _keys(item, where, required={"node_id", "device_id"})
        for key in ("node_id", "device_id"):
            value = item[key]
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ConfigError(f"{where}.{key} 는 앞뒤 공백 없는 빈 문자열이 아닌 값이어야 한다")
        bindings.append(SmartThingsBinding(item["node_id"], item["device_id"]))
    for key in ("node_id", "device_id"):
        values = [getattr(b, key) for b in bindings]
        if len(set(values)) != len(values):
            raise ConfigError(f"smartthings.bindings 에 같은 {key} 가 두 번 있다")
    return SmartThingsConfig(_path(table, "smartthings", "token_file", base),
                             table["confirm_poll_sec"], table["request_timeout_sec"], tuple(bindings))


def _dashboard(table: Mapping[str, Any], base: Path) -> DashboardConfig:
    names = {f.name for f in fields(DashboardSettings)}
    _keys(table, "dashboard", required=names | {"weak_rssi_dbm", "tail_interval_sec"})
    # DashboardSettings 도 같은 규칙을 검사하지만 오류 메시지에 값을 쓴다. 여기서 먼저 걸러 키 이름만 알린다.
    for key in ("queue_max_events", "batch_max_events", "audit_limit_max"):
        if type(table[key]) is not int or table[key] < 1:
            raise ConfigError(f"dashboard.{key} 는 1 이상의 정수여야 한다")
    for key in ("auth_timeout_sec", "tail_interval_sec"):
        if not _finite_number(table[key]) or table[key] <= 0:
            raise ConfigError(f"dashboard.{key} 는 0보다 큰 유한한 수여야 한다")
    if not _finite_number(table["weak_rssi_dbm"]):
        raise ConfigError("dashboard.weak_rssi_dbm 는 유한한 수여야 한다")
    return DashboardConfig(
        DashboardSettings(**{name: table[name] for name in names}),
        table["weak_rssi_dbm"], table["tail_interval_sec"],
        base.resolve().parent / "var" / "reports")  # cli 의 dirs_for(설정 파일 폴더의 부모 / var / reports)와 같은 규칙


# ── 공통 검사 ──────────────────────────────────────────────────────


def _table(raw: Mapping[str, Any], name: str, *, optional: bool = False) -> Mapping[str, Any]:
    value = raw.get(name, {} if optional else None)
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{name}] 는 표여야 한다")
    return value


def _keys(table: Mapping[str, Any], where: str, *, required: set[str],
          allowed: set[str] | None = None) -> None:
    allowed = required if allowed is None else allowed
    prefix = f"{where}." if where else ""
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"알 수 없는 설정 키: {', '.join(prefix + k for k in unknown)}")
    missing = sorted(required - set(table))
    if missing:
        raise ConfigError(f"필수 설정 키가 없다: {', '.join(prefix + k for k in missing)}")


def _finite_number(value: Any) -> bool:
    """bool 이 아닌 유한한 수. bool 은 type 이 int 가 아니라서 걸러진다."""
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _path(table: Mapping[str, Any], where: str, key: str, base: Path) -> Path:
    value = table[key]
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{where}.{key} 는 경로 문자열이어야 한다")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()
