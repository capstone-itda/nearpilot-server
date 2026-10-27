"""서버 TOML 설정 (specs/core-server-assembly/spec.md 6절 "설정과 모델").

운영 `Settings`, runtime 실행 설정, HTTP·DB·근접 모델·MQTT·보고 설정과 선택 섹션 `[dashboard]`
(specs/core-dashboard-queries/spec.md 2절)를 한 파일에서 읽는다.
상대 경로는 설정 파일의 부모 디렉터리 기준이다. 알 수 없는 키는 거부한다.
비밀은 파일 경로로만 받는다. 오류 메시지에는 키 이름만 쓰고 값을 쓰지 않는다 (NFR-08).
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.iot.mqtt.connection import MqttConfig
from nearpilot.iot.mqtt.delivery import DeliveryConfig
from nearpilot.shared.config import Settings


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
    model_root: Path
    versions: Mapping[str, str]  # place_id → model_version. 파일 이름으로 최신을 추측하지 않는다


@dataclass(frozen=True)
class DashboardConfig:
    """선택 섹션 `[dashboard]`. 없으면 대시보드를 열지 않는다."""

    api: DashboardSettings  # queue_max_events, batch_max_events, auth_timeout_sec, audit_limit_max
    weak_rssi_dbm: float  # 장소 목록의 weakRssiDbm
    tail_interval_sec: float  # 따라 읽기 간격
    reports_dir: Path  # 평가 리포트 폴더. cli 의 `dirs_for` 와 같은 규칙이다: 설정 파일 폴더의 부모 / var / reports


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


_SECTIONS = {"settings", "runtime", "http", "database", "proximity", "mqtt", "delivery", "dashboard"}
_REQUIRED_SECTIONS = _SECTIONS - {"runtime", "dashboard"}


def load_config(path: str | Path, *, require_models: bool = True) -> ServerConfig:
    """설정 파일을 읽고 검증한다. 잘못되면 `ConfigError` 다.

    `require_models=False` 면 `[proximity.models]` 가 없거나 비어도 된다. 모델 없이 도는 명령이 쓴다.
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
        )
    except ConfigError:
        raise
    except (TypeError, ValueError) as error:
        # 각 계층의 검증 오류다. 메시지는 키 이름만 담는다.
        raise ConfigError(str(error)) from None


# ── 절별 변환 ──────────────────────────────────────────────────────


def _settings(table: Mapping[str, Any]) -> Settings:
    names = {f.name for f in fields(Settings)}
    _keys(table, "settings", required={"version"}, allowed=names)
    if not isinstance(table["version"], str) or not table["version"].strip():
        raise ConfigError("settings.version 은 빈 문자열이 아니어야 한다")
    return Settings(**table)


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
    # model_root 는 항상 필수다. models 는 있으면 형식을 검사하고, require_models 일 때만 비면 안 된다.
    _keys(table, "proximity", required={"model_root", "models"} if require_models else {"model_root"},
          allowed={"model_root", "models"})
    models = table.get("models", {})
    if not isinstance(models, Mapping) or (require_models and not models):
        raise ConfigError("proximity.models 는 장소별 모델 버전 표여야 한다")
    for place_id, version in models.items():
        if not isinstance(version, str) or not version.strip():
            raise ConfigError(f"proximity.models.{place_id} 는 모델 버전 문자열이어야 한다")
    return ProximityConfig(_path(table, "proximity", "model_root", base),
                           MappingProxyType(dict(models)))


def _mqtt(table: Mapping[str, Any], base: Path) -> MqttConfig:
    names = {f.name for f in fields(MqttConfig)}
    _keys(table, "mqtt", required=names)  # 임의 기본값을 넣지 않는다
    values = dict(table)
    values["password_file"] = _path(table, "mqtt", "password_file", base)
    return MqttConfig(**values)


def _delivery(table: Mapping[str, Any]) -> DeliveryConfig:
    names = {f.name for f in fields(DeliveryConfig)}
    _keys(table, "delivery", required=names)
    if not isinstance(table["clock_skew_sec"], Mapping):
        raise ConfigError("delivery.clock_skew_sec 는 기기별 표여야 한다")
    return DeliveryConfig(**table)


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
