"""서버 지표 평가 설정(TOML)을 읽고 검증한다 (specs/db-evaluation-server-metrics/spec.md 2절 "입력").

예시는 `config/eval/server-metrics.example.toml`이다. 상대 경로(`model_root`)는 설정 파일이 있는 폴더 기준이다.
키는 아래만 허용하고, 모르는 키와 빠진 필수 키는 오류다. 잘못된 곳을 모두 모아 `EvalConfigError` 하나로 낸다.
  최상위             seed, model_root, model_version, actuation_sec, startup_timeout_sec
  [beacon]           uuid, major. 모든 사용자의 비콘 공통 값이다
  [server_settings]  서버 설정 `[settings]`를 덮어쓸 값(선택). 키는 `Settings`의 필드다
  [[users]]          user_id, role(owner|visitor), minor
  [[nodes]]          node_id, capability, occupancy, mode(선택, 기본 open), trusted(선택, 기본 true)
  [counts]           지표별 횟수. 값은 1 이상의 정수다
  [acceptance]       통과 기준. 값은 0 이상의 수다. 키가 `_rate`로 끝나면 0~1, `_sec`로 끝나면 0보다 크다
  [rssi]             시뮬레이터 RSSI(dBm). 값은 유한한 수다
`counts`, `acceptance`, `rssi`의 키 이름은 지표를 구현하는 쪽이 정한다. 이 모듈은 값의 형식과 범위만 본다.
"""

from __future__ import annotations

import math
import re
import tomllib
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import AccessMode, Capability, Occupancy, Role

# 기기 ID는 MQTT 토픽, Mosquitto 계정 이름, ACL, 파일 이름에 그대로 쓰인다.
_NODE_ID = re.compile(r"[A-Za-z0-9._-]+")
_BEACON_ID_MAX = 65535  # iBeacon major, minor의 범위

_TOP_KEYS = {
    "seed", "model_root", "model_version", "actuation_sec", "startup_timeout_sec",
    "beacon", "server_settings", "users", "nodes", "counts", "acceptance", "rssi",
}
_OPTIONAL_TOP_KEYS = {"server_settings"}
_USER_KEYS = ("user_id", "role", "minor")
_NODE_KEYS = ("node_id", "capability", "occupancy", "mode", "trusted")
_OPTIONAL_NODE_KEYS = ("mode", "trusted")


class EvalConfigError(ValueError):
    """평가 설정을 쓸 수 없다. 메시지에 잘못된 키를 모두 "; "로 잇는다."""


@dataclass(frozen=True)
class NodeSpec:
    """시뮬레이터 기기 하나."""

    node_id: str
    capability: str  # locker | light | speaker
    occupancy: str  # rental | shared
    mode: str = "open"  # open | restricted
    trusted: bool = True


@dataclass(frozen=True)
class UserSpec:
    user_id: str
    role: str  # owner | visitor
    minor: int  # 비콘 minor. uuid와 major는 설정의 [beacon] 공통값이다


@dataclass(frozen=True)
class EvalConfig:
    seed: int
    model_root: Path
    model_version: str  # 장소 place-a의 모델 버전
    actuation_sec: float  # 시뮬레이터 구동 시간
    startup_timeout_sec: float
    server_settings: Mapping[str, Any]  # 서버 설정 [settings] 위에 덮어쓸 값
    users: tuple[UserSpec, ...]
    nodes: tuple[NodeSpec, ...]
    counts: Mapping[str, int]  # 지표별 횟수. 키 예: "m3_requests", "m3_retransmits", "m3_clients", "m5_each"
    acceptance: Mapping[str, float]  # 통과 기준. 키 예: "m1_max_wrong_rate", "m7_max_median_sec"
    rssi: Mapping[str, float]  # 시뮬레이터 RSSI. 키 예: "near", "far", "absent"
    beacon_uuid: str  # 모든 사용자의 비콘 uuid
    beacon_major: int  # 모든 사용자의 비콘 major


def load_eval_config(path: Path) -> EvalConfig:
    """평가 설정 TOML을 읽고 검사한다. 읽을 수 없거나 틀리면 잘못된 키를 모두 담아 `EvalConfigError`다."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EvalConfigError(f"{path}: 읽을 수 없다 ({error.strerror or error})") from error
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise EvalConfigError(f"{path}: TOML로 읽을 수 없다 ({error})") from None

    problems = [f"{key}: 알 수 없는 키" for key in sorted(set(raw) - _TOP_KEYS)]
    problems += [f"{key}: 필수 키가 없다" for key in sorted(_TOP_KEYS - _OPTIONAL_TOP_KEYS - set(raw))]
    take = partial(_take, problems)

    seed = take("seed", _integer, raw.get("seed"))
    model_root = take("model_root", _text, raw.get("model_root"))
    model_version = take("model_version", _text, raw.get("model_version"))
    actuation_sec = take("actuation_sec", partial(_number, above=0), raw.get("actuation_sec"))
    startup_timeout_sec = take("startup_timeout_sec", partial(_number, above=0), raw.get("startup_timeout_sec"))
    beacon_uuid, beacon_major = _beacon(raw.get("beacon"), problems)
    server_settings = take("server_settings", _settings_overrides, raw.get("server_settings", {}))
    users = _users(raw.get("users"), problems)
    nodes = _nodes(raw.get("nodes"), problems)
    counts = _values(raw.get("counts"), "counts", partial(_integer, minimum=1), problems)
    acceptance = _values(raw.get("acceptance"), "acceptance", _acceptance, problems, by_key=True)
    rssi = _values(raw.get("rssi"), "rssi", _number, problems)

    if problems:
        raise EvalConfigError("; ".join(problems))
    return EvalConfig(
        seed=seed,
        model_root=_resolve(path, model_root),
        model_version=model_version,
        actuation_sec=actuation_sec,
        startup_timeout_sec=startup_timeout_sec,
        server_settings=MappingProxyType(server_settings),
        users=users,
        nodes=nodes,
        counts=MappingProxyType(counts),
        acceptance=MappingProxyType(acceptance),
        rssi=MappingProxyType(rssi),
        beacon_uuid=beacon_uuid,
        beacon_major=beacon_major,
    )


def _resolve(config_path: Path, value: str) -> Path:
    """상대 경로는 설정 파일이 있는 폴더 기준이다 (서버 설정과 같은 규칙)."""
    path = Path(value)
    return (path if path.is_absolute() else config_path.resolve().parent / path).resolve()


def _take(problems: list[str], where: str, convert: Callable[[Any], Any], value: Any) -> Any:
    """`convert(value)`를 돌려준다. `ValueError`면 사유를 `problems`에 모으고 None을 돌려준다."""
    if value is None:  # 없는 키는 위에서 이미 문제로 모았다
        return None
    try:
        return convert(value)
    except ValueError as error:
        problems.append(f"{where}: {error}")
        return None


def _text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError(f"비어 있지 않은 문자열이어야 한다 ({value!r:.40})")


def _integer(value: Any, minimum: int | None = None, maximum: int | None = None) -> int:
    """bool이 아닌 정수다. `minimum`·`maximum`이 있으면 그 범위 안이어야 한다."""
    if type(value) is not int:
        raise ValueError(f"정수여야 한다 ({value!r:.40})")
    if minimum is not None and value < minimum:
        raise ValueError(f"{minimum} 이상의 정수여야 한다 ({value})")
    if maximum is not None and value > maximum:
        raise ValueError(f"{maximum} 이하의 정수여야 한다 ({value})")
    return value


def _number(value: Any, minimum: float | None = None, above: float | None = None,
            maximum: float | None = None) -> float:
    """bool이 아닌 유한한 수다. `minimum` 이상, `above` 초과, `maximum` 이하여야 한다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"유한한 수여야 한다 ({value!r:.40})")
    if above is not None and not value > above:
        raise ValueError(f"{above:g}보다 커야 한다 ({value})")
    if minimum is not None and value < minimum:
        raise ValueError(f"{minimum:g} 이상이어야 한다 ({value})")
    if maximum is not None and value > maximum:
        raise ValueError(f"{maximum:g} 이하여야 한다 ({value})")
    return float(value)


def _acceptance(key: str, value: Any) -> float:
    """통과 기준 하나. 비율(`_rate`)은 0~1, 시간(`_sec`)은 0보다 크고, 그 밖은 0 이상이다."""
    if key.endswith("_rate"):
        return _number(value, minimum=0, maximum=1)
    if key.endswith("_sec"):
        return _number(value, above=0)
    return _number(value, minimum=0)


def _settings_overrides(value: Any) -> dict[str, Any]:
    """`Settings`의 필드 이름과 값이어야 한다. 서버 설정 예시와 합친 값의 검사는 서버가 시작할 때 한다."""
    if not isinstance(value, dict):
        raise ValueError("표여야 한다")
    try:
        Settings(**{"version": "eval", **value})
    except (TypeError, ValueError) as error:
        raise ValueError(str(error)) from None
    return dict(value)


def _beacon(value: Any, problems: list[str]) -> tuple[str | None, int | None]:
    """`[beacon]`의 (uuid, major)를 돌려준다."""
    if value is None:
        return None, None
    if not isinstance(value, dict):
        problems.append("beacon: 표여야 한다")
        return None, None
    problems += [f"beacon.{key}: 알 수 없는 키" for key in sorted(set(value) - {"uuid", "major"})]
    problems += [f"beacon.{key}: 필수 키가 없다" for key in ("uuid", "major") if key not in value]
    return (_take(problems, "beacon.uuid", _uuid, value.get("uuid")),
            _take(problems, "beacon.major", partial(_integer, minimum=0, maximum=_BEACON_ID_MAX), value.get("major")))


def _uuid(value: Any) -> str:
    try:
        uuid.UUID(_text(value))
    except ValueError:
        raise ValueError(f"UUID 형식이어야 한다 ({value!r:.40})") from None
    return value


def _rows(value: Any, name: str, keys: tuple[str, ...], optional: tuple[str, ...],
          problems: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """`[[name]]` 표 배열을 검사해 (경로, 행)을 돌려준다. 비어 있거나 키가 틀린 행은 문제로 모은다."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        problems.append(f"{name}: [[{name}]] 표 배열이어야 한다")
        return []
    if not value:
        problems.append(f"{name}: 하나 이상 있어야 한다")
    rows = []
    for index, row in enumerate(value):
        where = f"{name}[{index}]"
        problems += [f"{where}.{key}: 알 수 없는 키" for key in sorted(set(row) - set(keys))]
        problems += [f"{where}.{key}: 필수 키가 없다" for key in keys if key not in row and key not in optional]
        rows.append((where, row))
    return rows


def _users(value: Any, problems: list[str]) -> tuple[UserSpec, ...]:
    rows = _rows(value, "users", _USER_KEYS, (), problems)
    users, role_of = [], partial(_member, Role)
    for where, row in rows:
        user_id = _take(problems, f"{where}.user_id", _text, row.get("user_id"))
        role = _take(problems, f"{where}.role", role_of, row.get("role"))
        minor = _take(problems, f"{where}.minor", partial(_integer, minimum=0, maximum=_BEACON_ID_MAX),
                      row.get("minor"))
        users.append((where, UserSpec(user_id, role, minor)))
    # 사용자와 비콘은 하나씩이라서 ID와 minor가 겹치면 시드가 거부한다
    _report_duplicates(((f"{where}.user_id", user.user_id) for where, user in users), problems)
    _report_duplicates(((f"{where}.minor", user.minor) for where, user in users), problems)
    if users and not any(user.role == Role.OWNER for _, user in users):
        problems.append("users: owner가 한 명 이상 있어야 한다 (장소와 승인자를 owner가 맡는다)")
    return tuple(user for _, user in users)


def _nodes(value: Any, problems: list[str]) -> tuple[NodeSpec, ...]:
    rows = _rows(value, "nodes", _NODE_KEYS, _OPTIONAL_NODE_KEYS, problems)
    nodes = []
    for where, row in rows:
        node_id = _take(problems, f"{where}.node_id", _node_id, row.get("node_id"))
        capability = _take(problems, f"{where}.capability", partial(_member, Capability), row.get("capability"))
        occupancy = _take(problems, f"{where}.occupancy", partial(_member, Occupancy), row.get("occupancy"))
        mode = _take(problems, f"{where}.mode", partial(_member, AccessMode), row.get("mode", "open"))
        trusted = _take(problems, f"{where}.trusted", _boolean, row.get("trusted", True))
        nodes.append((where, NodeSpec(node_id, capability, occupancy, mode, trusted)))
    _report_duplicates(((f"{where}.node_id", node.node_id) for where, node in nodes), problems)
    return tuple(node for _, node in nodes)


def _node_id(value: Any) -> str:
    if isinstance(value, str) and _NODE_ID.fullmatch(value):
        return value
    raise ValueError(f"영문자, 숫자, `.`, `_`, `-`로만 이뤄진 이름이어야 한다 ({value!r:.40})")


def _member(enum: type, value: Any) -> str:
    """`enum`의 값 중 하나다."""
    allowed = [member.value for member in enum]
    if value in allowed:
        return value
    raise ValueError(f"{', '.join(allowed)} 중 하나여야 한다 ({value!r:.40})")


def _boolean(value: Any) -> bool:
    if type(value) is bool:
        return value
    raise ValueError(f"true 나 false 여야 한다 ({value!r:.40})")


def _values(value: Any, name: str, convert: Callable[..., Any], problems: list[str], *,
            by_key: bool = False) -> dict[str, Any]:
    """`[name]` 표의 값을 모두 `convert`로 바꾼다. `by_key`면 `convert(키, 값)`이다."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{name}: 표여야 한다")
        return {}
    result = {}
    for key, item in value.items():
        converted = _take(problems, f"{name}.{key}", partial(convert, key) if by_key else convert, item)
        if converted is not None:
            result[key] = converted
    return result


def _report_duplicates(items: Iterable[tuple[str, Any]], problems: list[str]) -> None:
    """(경로, 값) 중 앞에서 이미 나온 값을 문제로 모은다. 검사에 실패해 None인 값은 건너뛴다."""
    first: dict[Any, str] = {}
    for where, value in items:
        if value is None:
            continue
        if value in first:
            problems.append(f"{where}: 중복이다 ({first[value]}와 같다: {value})")
        else:
            first[value] = where
