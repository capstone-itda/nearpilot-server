"""모니터링 이벤트와 조회 결과의 형식 (architecture.md §5 "대시보드 조회", "WebSocket"). 허용 목록 밖의 키는 버린다 (FR-11, NFR-08)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any

from nearpilot.api.ws.names import Person, PersonNames
from nearpilot.shared.enums import (
    Action,
    ApprovalMode,
    ApprovalStatus,
    Capability,
    DeviceState,
    ExecutionStatus,
    Occupancy,
    ReasonCode,
    Step,
    TargetKind,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import MonitorEvent, Subject


class SchemaError(ValueError):
    """형식 표와 맞지 않는다. 메시지에는 경로(예: "rssi.rssi")만 쓰고 값을 쓰지 않는다 — 값에 사용자 ID 가 있을 수 있다."""


# ── 값 종류 ──────────────────────────────────────────────────────────

# (값, 경로, 이름표) → 내보낼 값. 어긋나면 SchemaError(경로). 이름표는 사람이 있는 obj 만 쓴다.
_Check = Callable[[Any, str, PersonNames], Any]

# 판정 단계 ⓪~⑥. Step 의 "0"~"6" 이다. step 이벤트의 step 은 이 일곱 값만 받는다 (감사 행의 step 은 Step 전체다)
_JUDGMENT_STEPS = (
    Step.DEDUP, Step.BEACON, Step.CANDIDATE, Step.PROXIMITY, Step.POLICY, Step.DEVICE_OCCUPANCY, Step.USER_OCCUPANCY,
)
_STEPS = len(_JUDGMENT_STEPS)  # 요청 한 건의 steps 는 일곱 칸


def _str(value: Any, path: str, names: PersonNames) -> str:
    if not isinstance(value, str):
        raise SchemaError(path)
    text = str(value)  # StrEnum 멤버는 값 문자열로
    try:
        text.encode("utf-8")  # 짝 없는 서로게이트는 UTF-8 로 바꿀 수 없다. 본문을 만들 때 실패하므로 여기서 거른다
    except UnicodeEncodeError:
        raise SchemaError(path) from None  # 오류 메시지의 값을 잇지 않는다
    return text


_SAFE_INT = 2**53 - 1


def _bool(value: Any, path: str, names: PersonNames) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(path)
    return value


def _int(value: Any, path: str, names: PersonNames) -> int:
    if isinstance(value, bool) or not isinstance(value, int):  # bool 은 int 의 하위 형이다
        raise SchemaError(path)
    if abs(value) > _SAFE_INT:  # 대시보드(JS)가 정확히 읽는 범위. 아주 큰 정수는 json.dumps 도 실패한다
        raise SchemaError(path)
    return value


def _num(value: Any, path: str, names: PersonNames) -> int | float:
    if isinstance(value, float):
        if not isfinite(value):
            raise SchemaError(path)
        return value
    return _int(value, path, names)


def _time(value: Any, path: str, names: PersonNames) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:  # 시간대가 없는 시각은 받지 않는다
        raise SchemaError(path)
    return value.isoformat(timespec="milliseconds")


def _lit(*allowed: str) -> _Check:
    """lit{…}: 집합 안의 문자열. StrEnum 멤버로 와도 받고 값 문자열로 내보낸다."""
    allowed_set = frozenset(allowed)

    def check(value: Any, path: str, names: PersonNames) -> str:
        if not isinstance(value, str) or value not in allowed_set:
            raise SchemaError(path)
        return str(value)

    return check


def _enum(cls: type[StrEnum]) -> _Check:
    """E(이름): 열거형의 멤버나 값 문자열. 열거형이 모두 StrEnum 이라 값 문자열의 집합과 같다."""
    return _lit(*(member.value for member in cls))


def _items(value: Any, path: str) -> Sequence[Any]:
    """Sequence. 문자열과 Mapping 은 받지 않는다."""
    if not isinstance(value, Sequence) or isinstance(value, (str, Mapping)):
        raise SchemaError(path)
    return value


def _nummap(value: Any, path: str, names: PersonNames) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        raise SchemaError(path)
    out: dict[str, int | float] = {}
    for key, number in value.items():
        text = _str(key, path, names)  # 키가 사용자 ID 일 수 있어 경로에 쓰지 않는다
        out[text] = _num(number, path, names)
    return out


def _strs(value: Any, path: str, names: PersonNames) -> list[str]:
    return [_str(item, f"{path}[{i}]", names) for i, item in enumerate(_items(value, path))]


def _matrix(value: Any, path: str, names: PersonNames) -> list[list[int | float]]:
    return [
        [_num(number, f"{path}[{i}][{j}]", names) for j, number in enumerate(_items(row, f"{path}[{i}]"))]
        for i, row in enumerate(_items(value, path))
    ]


def _list(item: _Check, size: int | None = None) -> _Check:
    """[X]: 항목마다 X 로 바꾼 list. size 가 있으면 항목 수가 정확히 같아야 한다."""

    def check(value: Any, path: str, names: PersonNames) -> list[Any]:
        entries = _items(value, path)
        if size is not None and len(entries) != size:
            raise SchemaError(path)
        return [item(entry, f"{path}[{i}]", names) for i, entry in enumerate(entries)]

    return check


_VERDICT = _enum(Verdict)


def _verdict_counts(value: Any, path: str, names: PersonNames) -> dict[str, int]:
    """VerdictCounts: 키는 Verdict 값 다섯 개 모두, 값은 int. Verdict 정의 순서로 내보낸다."""
    if not isinstance(value, Mapping):
        raise SchemaError(path)
    counts: dict[str, int] = {}
    for key, count in value.items():
        verdict = _VERDICT(key, path, names)  # Verdict 값이 아닌 키는 경로만 쓴다
        counts[verdict] = _int(count, f"{path}.{verdict}", names)
    if len(counts) != len(Verdict):
        raise SchemaError(path)
    return {member.value: counts[member.value] for member in Verdict}


# ── 형식 표 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Field:
    name: str
    check: _Check
    required: bool  # 없거나 None 이면 SchemaError
    nullable: bool  # 필수이고 None 이면 null 로 내보낸다


@dataclass(frozen=True, slots=True)
class _Shape:
    fields: tuple[_Field, ...]
    person: Callable[[Person], dict[str, Any]] | None  # 사람이 있는 형식이면 사람 필드를 만든다


def _req(name: str, check: _Check) -> _Field:
    """필수 (✔)."""
    return _Field(name, check, required=True, nullable=False)


def _opt(name: str, check: _Check) -> _Field:
    """선택. 없거나 None 이면 내보내지 않는다."""
    return _Field(name, check, required=False, nullable=False)


def _nul(name: str, check: _Check) -> _Field:
    """null 허용 (∅). 키는 있어야 하고 None 이면 null 로 내보낸다."""
    return _Field(name, check, required=True, nullable=True)


def _shape(*fields: _Field, person: Callable[[Person], dict[str, Any]] | None = None) -> _Shape:
    return _Shape(fields, person)


def _person_full(person: Person) -> dict[str, Any]:
    """사람 full. letter 는 방문자만 있다."""
    out = {"personId": person.person_id, "pseudonym": person.pseudonym}
    if person.letter is not None:
        out["letter"] = person.letter
    out["role"] = person.role.value
    return out


def _person_id(person: Person) -> dict[str, Any]:
    """사람 id."""
    return {"personId": person.person_id}


def _person_label(person: Person) -> dict[str, Any]:
    """점검의 비콘. label 은 입력이 아니라 이름표의 이름이다."""
    return {"personId": person.person_id, "label": person.pseudonym}


_MISSING = object()


def _convert(shape: _Shape, data: Mapping[str, Any], path: str, names: PersonNames) -> dict[str, Any]:
    """표의 키만 검사해서 새 dict 로 옮긴다. 표에 없는 입력 키는 버린다."""
    out: dict[str, Any] = {}
    for field in shape.fields:
        value = data.get(field.name, _MISSING)
        if value is None and field.nullable:
            out[field.name] = None
        elif value is None or value is _MISSING:
            if field.required:
                raise SchemaError(f"{path}.{field.name}")
        else:
            out[field.name] = field.check(value, f"{path}.{field.name}", names)
    return out


def _person(
    build: Callable[[Person], dict[str, Any]], subject: Any, path: str, names: PersonNames
) -> dict[str, Any]:
    """subject 를 사람 필드로 바꾼다. Subject 가 아니면 SchemaError. 사용자 ID 는 이름표를 거쳐 사라진다."""
    if not isinstance(subject, Subject):
        raise SchemaError(f"{path}.subject")
    return build(names.person(subject))


def _obj(shape: _Shape) -> _Check:
    """obj(X): 사람이 있는 행은 사람 필드를 키 앞에 둔다. 사람은 행의 "subject" 키에서만 읽는다."""

    def check(value: Any, path: str, names: PersonNames) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise SchemaError(path)
        out = _convert(shape, value, path, names)
        if shape.person is None:
            return out
        return {**_person(shape.person, value.get("subject"), path, names), **out}

    return check


# 이벤트 (architecture.md §5 WebSocket 형식 표)

_TARGET = _shape(_req("kind", _enum(TargetKind)), _opt("capability", _enum(Capability)), _opt("nodeId", _str))
_FAULT = _shape(_req("cause", _enum(ReasonCode)), _opt("action", _enum(Action)))
_AUDIT_ROW = _shape(
    _req("id", _str),
    _req("at", _time),
    _req("step", _enum(Step)),
    _opt("nodeId", _str),
    _req("text", _str),
    _req("code", _enum(ReasonCode)),
)

_TOOLS = (  # MCP 도구 6개
    "find_available_devices",
    "get_device_state",
    "request_use",
    "request_access",
    "release_use",
    "get_use_status",
)

_EVENTS: dict[str, _Shape] = {
    "beacon": _shape(_req("seen", _bool), person=_person_full),
    "rssi": _shape(_req("rssi", _nummap), person=_person_id),
    "mcp_call": _shape(
        _req("requestId", _str),
        _req("tool", _lit(*_TOOLS)),
        _opt("target", _obj(_TARGET)),
        _opt("action", _enum(Action)),
        person=_person_id,
    ),
    "step": _shape(
        _req("requestId", _str),
        _req("step", _lit(*(step.value for step in _JUDGMENT_STEPS))),
        _req("result", _lit("pass", "stop")),
        _opt("detail", _str),
    ),
    "posterior": _shape(_req("requestId", _str), _req("posteriors", _nummap), _req("threshold", _num)),
    "verdict": _shape(
        _req("requestId", _str),
        _req("verdict", _enum(Verdict)),
        _req("reason", _enum(ReasonCode)),
        _opt("nodeId", _str),
    ),
    "command": _shape(_req("requestId", _str), _req("nodeId", _str), _req("action", _enum(Action))),
    "exec_result": _shape(
        _req("requestId", _str), _req("nodeId", _str),
        _req("result", _lit("succeeded", "failed", "rejected", "unknown")),
    ),
    "mcp_result": _shape(_req("requestId", _str)),
    "transition": _shape(
        _req("nodeId", _str),
        _req("to", _enum(DeviceState)),
        _opt("overdue", _bool),
        _opt("fault", _obj(_FAULT)),
    ),
    "approval": _shape(
        _req("requestId", _str),
        _req("nodeId", _str),
        _req("kind", _lit("requested", ApprovalStatus.APPROVED.value, ApprovalStatus.DENIED.value, "expired")),
        person=_person_id,
    ),
    "audit": _shape(_req("row", _obj(_AUDIT_ROW))),
}

# 조회 결과 (architecture.md §5 "대시보드 조회" 형식 표)

_DEVICE_INFO = _shape(
    _req("nodeId", _str),
    _req("name", _str),
    _req("capability", _enum(Capability)),
    _req("occupancy", _enum(Occupancy)),
)
_PLACE_INFO = _shape(
    _req("placeId", _str),
    _req("placeName", _str),
    _req("modelVersion", _str),
    _req("calibrationVersion", _str),
    _req("devices", _list(_obj(_DEVICE_INFO))),
)


def _place_list(value: Any, path: str, names: PersonNames) -> list[dict[str, Any]]:
    """장소 목록. placeId 와 nodeId 는 목록 전체에서 한 번씩만 나온다 — 대시보드가 nodeId 로 기기의 장소를 찾는다."""
    places = _list(_obj(_PLACE_INFO))(value, path, names)
    place_ids = [place["placeId"] for place in places]
    node_ids = [device["nodeId"] for place in places for device in place["devices"]]
    if len(set(place_ids)) != len(place_ids) or len(set(node_ids)) != len(node_ids):
        raise SchemaError(path)  # 겹친 값은 경로에 쓰지 않는다
    return places


# weakRssiDbm (대시보드 배치 화면의 약한 신호 기준)은 장소마다가 아니라 응답 전체에 하나다
_PLACES = _shape(_req("weakRssiDbm", _num), _req("places", _place_list))
_USE_ROW = _shape(
    _req("nodeId", _str),
    _req("occupancy", _enum(Occupancy)),
    _req("session", _lit("RESERVED", "ACTIVE", "CLOSING")),
    _opt("sessionNote", _str),
    _req("startedAt", _time),
    _opt("overdueMinutes", _num),
    person=_person_id,
)
_APPROVAL_ROW = _shape(
    _req("nodeId", _str),
    _opt("mode", _enum(ApprovalMode)),
    _opt("requestedAt", _time),
    _opt("remainingMinutes", _num),
    _opt("validUntil", _time),
    _opt("uses", _int),
    _opt(
        "endReason",
        _lit("APPROVAL_REJECTED", "APPROVAL_REQUEST_EXPIRED", "APPROVAL_EXHAUSTED", "APPROVAL_EXPIRED"),
    ),
    person=_person_id,
)
_APPROVALS = _shape(
    _req("pending", _list(_obj(_APPROVAL_ROW))),
    _req("active", _list(_obj(_APPROVAL_ROW))),
    _req("ended", _list(_obj(_APPROVAL_ROW))),
)
_HISTORY_ROW = _shape(
    _req("requestId", _str),
    _req("at", _time),
    _req("target", _obj(_TARGET)),
    _req("action", _enum(Action)),
    _req("verdict", _enum(Verdict)),
    _opt("nodeId", _str),
    _req("outcome", _str),
    _opt("code", _enum(ReasonCode)),
    person=_person_id,
)
_STEP_OUTCOME = _shape(_req("result", _lit("pending", "pass", "stop", "skip")), _opt("detail", _str))
_REQUEST_PROGRESS = _shape(
    _req("id", _str),
    _req("tool", _lit("request_use")),
    _req("target", _obj(_TARGET)),
    _req("action", _enum(Action)),
    _req("at", _time),
    _opt("verdict", _enum(Verdict)),
    _opt("reason", _enum(ReasonCode)),
    _req("steps", _list(_obj(_STEP_OUTCOME), size=_STEPS)),
    _opt("nodeId", _str),
    _opt("posteriors", _nummap),
    _opt("threshold", _num),
    _opt("execution", _enum(ExecutionStatus)),
    _opt("accessRequestedAt", _time),
    _opt("accessEnd", _lit("approved", "denied", "expired")),
    _opt("accessExpiredAt", _time),
    _opt("respondedAt", _time),
    person=_person_id,
)
_LIVE = _shape(
    _req("since", _time),
    _req("decisions", _int),
    _req("doubleOccupancy", _int),
    _nul("latencyMedianSec", _num),
    _nul("latencyP95Sec", _num),
    _req("counts", _verdict_counts),
)
_METRIC = _shape(
    _req("id", _int),
    _req("name", _str),
    _nul("pass", _bool),
    _req("label", _str),
    _req("sample", _str),
    _opt("sampleNote", _str),
)
_CONFUSION = _shape(_req("labels", _strs), _req("values", _matrix))
_EVALUATION = _shape(
    _req("date", _str), _req("metrics", _list(_obj(_METRIC))), _opt("confusion", _obj(_CONFUSION))
)
_TRUST_REPORT = _shape(_req("live", _obj(_LIVE)), _opt("evaluation", _obj(_EVALUATION)))
_TIMELINE_ITEM = _shape(_req("at", _time), _req("text", _str), _opt("now", _bool))
_RENTAL_INFO = _shape(
    _req("nodeId", _str),
    _req("startedAt", _time),
    _req("overdueMinutes", _num),
    _req("timeline", _list(_obj(_TIMELINE_ITEM))),
    person=_person_id,
)
_HEALTH_DEVICE = _shape(_req("nodeId", _str), _req("connected", _bool), _req("trust", _enum(TrustState)))
_HEALTH_BEACON = _shape(_nul("lastSeenSec", _num), _opt("note", _str), person=_person_label)
_HEALTH_SERVER = _shape(_req("database", _bool), _req("mqtt", _bool), _req("model", _str))
_HEALTH = _shape(
    _req("devices", _list(_obj(_HEALTH_DEVICE))),
    _req("beacons", _list(_obj(_HEALTH_BEACON))),
    _req("server", _obj(_HEALTH_SERVER)),
)

_RESPONSES: dict[str, _Check] = {
    "places": _obj(_PLACES),
    "uses": _list(_obj(_USE_ROW)),
    "approvals": _obj(_APPROVALS),
    "audit": _list(_obj(_AUDIT_ROW)),
    "history": _list(_obj(_HISTORY_ROW)),
    "request": _obj(_REQUEST_PROGRESS),
    "trust": _obj(_TRUST_REPORT),
    "rental": _obj(_RENTAL_INFO),
    "health": _obj(_HEALTH),
}


# ── 공개 함수 ────────────────────────────────────────────────────────

EVENT_TYPES: frozenset[str] = frozenset(_EVENTS)
RESPONSE_KINDS: frozenset[str] = frozenset(_RESPONSES)


def render_event(event: MonitorEvent, names: PersonNames) -> dict[str, Any]:
    """이벤트를 메시지 형식의 dict 로 바꾼다. 키 순서는 type, at, 표의 키, 사람 필드다."""
    type_ = event.type
    shape = _EVENTS.get(type_) if isinstance(type_, str) else None
    if shape is None:
        raise SchemaError("type")
    out: dict[str, Any] = {"type": type_, "at": _time(event.at, f"{type_}.at", names)}
    if not isinstance(event.fields, Mapping):
        raise SchemaError(f"{type_}.fields")
    out.update(_convert(shape, event.fields, type_, names))
    if shape.person is not None:
        out.update(_person(shape.person, event.subject, type_, names))
    return out


def render_response(kind: str, data: Any, names: PersonNames) -> Any:
    """조회 결과를 응답 형식으로 바꾼다. 최상위가 객체인 kind 는 dict, 목록인 kind 는 list 다."""
    check = _RESPONSES.get(kind) if isinstance(kind, str) else None
    if check is None:
        raise SchemaError("kind")
    return check(data, kind, names)
