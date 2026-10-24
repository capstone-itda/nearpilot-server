"""FR-12A·FR-12B 모니터링 이벤트와 조회 결과의 형식. 허용 목록 검사는 순수 함수라 이름표 외에 fake 가 없다."""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, NamedTuple

import pytest

from _dashboard import NOW, OWNER, VISITOR
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import EVENT_TYPES, RESPONSE_KINDS, SchemaError, render_event, render_response
from nearpilot.shared.enums import (
    Action,
    ApprovalMode,
    ApprovalStatus,
    Capability,
    DeviceState,
    ExecutionStatus,
    Occupancy,
    ReasonCode,
    Role,
    SessionState,
    Step,
    TargetKind,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import MonitorEvent, Subject

ISO = "2026-10-23T14:03:05.120+09:00"  # NOW
EARLY = NOW - timedelta(minutes=30)
EARLY_ISO = "2026-10-23T13:33:05.120+09:00"
LATER = NOW + timedelta(minutes=45)
LATER_ISO = "2026-10-23T14:48:05.120+09:00"
UTC_AT = datetime(2026, 10, 23, 5, 3, 4, 987654, tzinfo=timezone.utc)  # 밀리초 아래는 잘라 낸다
UTC_ISO = "2026-10-23T05:03:04.987+00:00"
NAIVE = datetime(2026, 10, 23, 14, 3, 5)  # 시간대가 없다
SECRET = f"secret-{VISITOR.user_id}"  # 틀린 값 자리에 넣는다. 오류 메시지에 나오면 안 된다
UNENCODABLE = "x\ud800"  # 짝 없는 서로게이트. UTF-8 로 바꿀 수 없다

DROP = object()  # patched 에서 그 자리를 지운다
KEEP = object()  # bad_event 에서 정상 사례의 subject 를 그대로 쓴다


def event(type_: str, fields: Any, subject: Any = None, at: Any = NOW) -> MonitorEvent:
    return MonitorEvent(type_, at, fields, subject)


class SpyNames(PersonNames):
    """이름표를 달라고 한 Subject 를 순서대로 쌓는다."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[Subject] = []

    def person(self, subject):
        self.asked.append(subject)
        return super().person(subject)


def assert_plain(value: Any, path: str = "result") -> None:
    """결과에 JSON 의 기본 형(dict, list, str, int, float, bool, None)만 있다. 열거형 멤버와 tuple 이 새지 않는다."""
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            assert type(key) is str, path
            assert_plain(item, f"{path}.{key}")
    elif kind is list:
        for i, item in enumerate(value):
            assert_plain(item, f"{path}[{i}]")
    else:
        assert kind in (str, int, float, bool, type(None)), f"{path} 가 {kind.__name__} 이다"


def dumps(value: Any) -> str:
    """키 순서까지 비교하려고 쓴다. dict 의 == 는 순서를 보지 않는다."""
    return json.dumps(value, ensure_ascii=False)


JUNK = {  # 허용 목록 밖의 키. 사용자 ID 와 가짜 사람 필드를 담는다
    "userId": VISITOR.user_id,
    "user_id": VISITOR.user_id,
    "account": {"user_id": VISITOR.user_id, "token": "tok-visitor"},
    "personId": "p99",
    "pseudonym": "가짜",
    "letter": "Z",
    "role": "owner",
    "label": "가짜",
    "subject": OWNER,
}
NUMMAPS = {"rssi", "posteriors", "counts"}  # 키가 기기 ID 나 판정 값인 맵에는 다른 키를 끼울 수 없다


def with_junk(value: Any) -> Any:
    """모든 객체에 허용 목록 밖의 키를 끼운다. 원래 있던 키가 이긴다."""
    if isinstance(value, dict):
        return {**JUNK, **{key: item if key in NUMMAPS else with_junk(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return [with_junk(item) for item in value]
    return value


def patched(base: Any, where: tuple = (), to: Any = None) -> Any:
    """base 를 복사해서 where 가 가리키는 자리를 to 로 바꾼다. to 가 DROP 이면 그 자리를 지운다."""
    data = copy.deepcopy(base)
    if where:
        holder = data
        for key in where[:-1]:
            holder = holder[key]
        if to is DROP:
            del holder[where[-1]]
        else:
            holder[where[-1]] = to
    return data


# ── 이벤트 ────────────────────────────────────────────────────────────


class EventCase(NamedTuple):
    id: str
    type: str
    fields: dict[str, Any]
    expected: dict[str, Any]  # type, at 뒤에 오는 키와 사람 필드
    subject: Subject | None = None  # 사람이 있는 형식만 둔다
    optional: tuple[str, ...] = ()  # 선택 최상위 키. 없거나 None 이면 결과에 없다


VISITOR_FULL = {"personId": "p1", "pseudonym": "방문자 A", "letter": "A", "role": "visitor"}
OWNER_FULL = {"personId": "p1", "pseudonym": "소유자", "role": "owner"}  # 소유자는 letter 가 없다

EVENT_CASES = [
    EventCase("beacon", "beacon", {"seen": True}, {"seen": True, **VISITOR_FULL}, VISITOR),
    EventCase("beacon_owner", "beacon", {"seen": False}, {"seen": False, **OWNER_FULL}, OWNER),
    EventCase(
        "rssi", "rssi",
        {"rssi": {"locker-1": -58, "light-1": -71.5}},
        {"rssi": {"locker-1": -58, "light-1": -71.5}, "personId": "p1"},
        VISITOR,
    ),
    EventCase(
        "mcp_call", "mcp_call",
        {"requestId": "req-1", "tool": "request_use", "action": Action.OPEN,
         "target": {"kind": TargetKind.NEAREST, "capability": "locker", "nodeId": "locker-1"}},
        {"requestId": "req-1", "tool": "request_use",
         "target": {"kind": "nearest", "capability": "locker", "nodeId": "locker-1"}, "action": "open",
         "personId": "p1"},
        VISITOR, ("target", "action"),
    ),
    EventCase(
        "step", "step",
        {"requestId": "req-1", "step": Step.PROXIMITY, "result": "stop", "detail": "p=0.62"},
        {"requestId": "req-1", "step": "3", "result": "stop", "detail": "p=0.62"},
        optional=("detail",),
    ),
    EventCase(
        "posterior", "posterior",
        {"requestId": "req-1", "posteriors": {"locker-1": 0.62, "light-1": 0.38}, "threshold": 0.9},
        {"requestId": "req-1", "posteriors": {"locker-1": 0.62, "light-1": 0.38}, "threshold": 0.9},
    ),
    EventCase(
        "verdict", "verdict",
        {"requestId": "req-1", "verdict": Verdict.ASK, "reason": "LOW_CONFIDENCE", "nodeId": "locker-1"},
        {"requestId": "req-1", "verdict": "ASK", "reason": "LOW_CONFIDENCE", "nodeId": "locker-1"},
        optional=("nodeId",),
    ),
    EventCase(
        "command", "command",
        {"requestId": "req-1", "nodeId": "locker-1", "action": "open"},
        {"requestId": "req-1", "nodeId": "locker-1", "action": "open"},
    ),
    EventCase(
        "exec_result", "exec_result",
        {"requestId": "req-1", "nodeId": "locker-1", "result": "unknown"},
        {"requestId": "req-1", "nodeId": "locker-1", "result": "unknown"},
    ),
    EventCase("mcp_result", "mcp_result", {"requestId": "req-1"}, {"requestId": "req-1"}),
    EventCase(
        "transition", "transition",
        {"nodeId": "locker-1", "to": DeviceState.FAULT, "overdue": True,
         "fault": {"cause": ReasonCode.EXEC_UNKNOWN, "action": Action.OPEN}},
        {"nodeId": "locker-1", "to": "FAULT", "overdue": True,
         "fault": {"cause": "EXEC_UNKNOWN", "action": "open"}},
        optional=("overdue", "fault"),
    ),
    EventCase(
        "approval", "approval",
        {"requestId": "req-1", "nodeId": "locker-1", "kind": "approved"},
        {"requestId": "req-1", "nodeId": "locker-1", "kind": "approved", "personId": "p1"},
        VISITOR,
    ),
    EventCase(
        "audit", "audit",
        {"row": {"id": "a-9", "at": UTC_AT, "step": Step.PROXIMITY, "nodeId": "locker-1",
                 "text": "후보 확률이 임계값 미만", "code": ReasonCode.LOW_CONFIDENCE}},
        {"row": {"id": "a-9", "at": UTC_ISO, "step": "3", "nodeId": "locker-1",
                 "text": "후보 확률이 임계값 미만", "code": "LOW_CONFIDENCE"}},
    ),
]
GOOD = {case.type: case for case in EVENT_CASES}  # type 마다 정상 사례 하나. beacon 은 소유자 사례다


def full_expected(case: EventCase) -> dict[str, Any]:
    return {"type": case.type, "at": ISO, **case.expected}


def test_FR12A_FR12B_형식_이름_집합이_architecture_표와_같다():
    assert EVENT_TYPES == {
        "beacon", "rssi", "mcp_call", "step", "posterior", "verdict", "command", "exec_result", "mcp_result",
        "transition", "approval", "audit",
    }
    assert RESPONSE_KINDS == {
        "places", "uses", "approvals", "audit", "history", "request", "trust", "rental", "health",
    }
    assert isinstance(EVENT_TYPES, frozenset) and isinstance(RESPONSE_KINDS, frozenset)
    assert {case.type for case in EVENT_CASES} == EVENT_TYPES  # 종류마다 사례가 있다
    assert {case.kind for case in RESPONSE_CASES} == RESPONSE_KINDS
    assert issubclass(SchemaError, ValueError)


@pytest.mark.parametrize("case", EVENT_CASES, ids=lambda c: c.id)
def test_FR12B_이벤트_종류마다_형식대로_보낸다(case):
    expected = full_expected(case)
    result = render_event(event(case.type, case.fields, case.subject), PersonNames())
    assert result == expected
    assert dumps(result) == dumps(expected)  # type, at, 표의 키, 사람 필드 순서
    assert_plain(result)
    # 선택 키는 None 이어도 없어도 결과에 나오지 않는다
    without = {key: value for key, value in expected.items() if key not in case.optional}
    nulled = {**case.fields, **dict.fromkeys(case.optional)}
    absent = {key: value for key, value in case.fields.items() if key not in case.optional}
    assert render_event(event(case.type, nulled, case.subject), PersonNames()) == without
    assert render_event(event(case.type, absent, case.subject), PersonNames()) == without


def test_FR12B_중첩_객체의_선택_키도_None이면_보내지_않는다():
    names = PersonNames()
    call = event("mcp_call", {"requestId": "req-1", "tool": "request_use",
                              "target": {"kind": "any", "capability": None, "nodeId": None}}, VISITOR)
    assert render_event(call, names)["target"] == {"kind": "any"}
    fault = event("transition", {"nodeId": "locker-1", "to": "FAULT",
                                 "fault": {"cause": "EXEC_FAILED", "action": None}})
    assert render_event(fault, names)["fault"] == {"cause": "EXEC_FAILED"}
    row = {"id": "a-1", "at": NOW, "step": "exec", "nodeId": None, "text": "명령", "code": "OK"}
    assert "nodeId" not in render_event(event("audit", {"row": row}), names)["row"]


@pytest.mark.parametrize(
    "tool",
    ["find_available_devices", "get_device_state", "request_use", "request_access", "release_use",
     "get_use_status"],
)
def test_FR12B_도구_호출은_MCP_도구_여섯_개를_모두_받는다(tool):
    result = render_event(event("mcp_call", {"requestId": "req-1", "tool": tool}, VISITOR), PersonNames())
    assert result == {"type": "mcp_call", "at": ISO, "requestId": "req-1", "tool": tool, "personId": "p1"}


@pytest.mark.parametrize(
    ("at", "text"),
    [
        (NOW, ISO),
        (datetime(2026, 10, 23, 0, 0, 0, tzinfo=timezone.utc), "2026-10-23T00:00:00.000+00:00"),  # 0 도 세 자리다
        (datetime(2026, 10, 23, 14, 3, 5, 999999, tzinfo=timezone(timedelta(hours=5, minutes=30))),
         "2026-10-23T14:03:05.999+05:30"),  # 올리지 않고 자른다
    ],
    ids=["tz_plus_9", "zero_millis_utc", "truncate_plus_5_30"],
)
def test_FR12B_시각은_밀리초까지_시간대와_함께_보낸다(at, text):
    assert render_event(event("mcp_result", {"requestId": "req-1"}, at=at), PersonNames())["at"] == text


@pytest.mark.parametrize("threshold", [1, 0, -3, 0.5, -0.0, 1e308, 2**53 - 1])
def test_FR12B_숫자는_정수와_실수를_그대로_보낸다(threshold):
    fields = {**GOOD["posterior"].fields, "threshold": threshold}
    sent = render_event(event("posterior", fields), PersonNames())["threshold"]
    assert sent == threshold and type(sent) is type(threshold)  # 범위 안의 가장 큰 정수도 그대로 간다 (범위 밖은 test_FR12B_범위_밖_정수는_형식_오류다)


@pytest.mark.parametrize("case", EVENT_CASES, ids=lambda c: c.id)
def test_FR11_이벤트에서_허용_목록_밖의_키와_사용자_ID를_버린다(case):
    names = SpyNames()
    result = render_event(event(case.type, with_junk(case.fields), case.subject), names)
    assert result == full_expected(case)  # personId 는 가짜 값이 아니라 이름표의 값이다
    assert names.asked == ([case.subject] if case.subject else [])  # 사람은 event.subject 에서만 읽는다
    assert VISITOR.user_id not in json.dumps(result)


@pytest.mark.parametrize(
    "case", [case for case in EVENT_CASES if case.subject is None], ids=lambda c: c.id
)
def test_FR12B_사람이_없는_형식은_subject를_보지_않는다(case):
    for subject in (VISITOR, SECRET, 7):  # Subject 가 아니어도 오류가 아니다
        names = SpyNames()
        assert render_event(event(case.type, case.fields, subject), names) == full_expected(case)
        assert names.asked == []


def bad_event(id_: str, type_: str, path: str, *where: Any, to: Any = None, subject: Any = KEEP):
    """GOOD[type_] 의 fields 에서 where 가 가리키는 자리를 to 로 바꾼 이벤트와 기대 경로."""
    good = GOOD[type_]
    return pytest.param(
        event(type_, patched(good.fields, where, to), good.subject if subject is KEEP else subject), path, id=id_
    )


BAD_EVENTS = [
    # 이벤트 자체
    pytest.param(event(SECRET, {}, VISITOR), "type", id="unknown_type"),
    pytest.param(event(["beacon"], {"seen": True}, VISITOR), "type", id="type_not_str"),
    pytest.param(event("beacon", {"seen": True}, VISITOR, at=NAIVE), "beacon.at", id="naive_at"),
    pytest.param(event("beacon", {"seen": True}, VISITOR, at=ISO), "beacon.at", id="str_at"),
    pytest.param(event("beacon", ["seen"], VISITOR), "beacon.fields", id="fields_not_mapping"),
    pytest.param(event("beacon", None, VISITOR), "beacon.fields", id="fields_none"),
    # 값 종류
    bad_event("missing_key", "rssi", "rssi.rssi", "rssi", to=DROP),
    bad_event("none_required", "mcp_result", "mcp_result.requestId", "requestId", to=None),
    bad_event("int_for_str", "mcp_result", "mcp_result.requestId", "requestId", to=7),
    bad_event("str_for_bool", "beacon", "beacon.seen", "seen", to=SECRET),
    bad_event("int_for_bool", "transition", "transition.overdue", "overdue", to=1),
    bad_event("bool_for_int", "step", "step.step", "step", to=True),
    bad_event("float_for_int", "step", "step.step", "step", to=3.0),
    bad_event("step_7", "step", "step.step", "step", to="7"),
    bad_event("step_minus_1", "step", "step.step", "step", to="-1"),
    bad_event("int_for_step", "step", "step.step", "step", to=3),  # Step 의 값 문자열이어야 한다
    bad_event("step_not_judgment", "step", "step.step", "step", to=Step.EXEC),  # Step 이지만 판정 단계 ⓪~⑥이 아니다
    bad_event("bool_for_num", "posterior", "posterior.threshold", "threshold", to=True),
    bad_event("str_for_num", "posterior", "posterior.threshold", "threshold", to=SECRET),
    bad_event("nan", "posterior", "posterior.threshold", "threshold", to=math.nan),
    bad_event("inf", "posterior", "posterior.threshold", "threshold", to=math.inf),
    bad_event("lit_outside", "step", "step.result", "result", to="skip"),
    bad_event("lit_not_str", "step", "step.result", "result", to=1),
    bad_event("unknown_tool", "mcp_call", "mcp_call.tool", "tool", to=SECRET),
    bad_event("approval_kind_outside", "approval", "approval.kind", "kind", to="granted"),
    bad_event("enum_outside", "verdict", "verdict.verdict", "verdict", to=SECRET),
    bad_event("enum_wrong_case", "command", "command.action", "action", to="OPEN"),
    bad_event("enum_not_str", "verdict", "verdict.reason", "reason", to=7),
    bad_event("nummap_str_value", "posterior", "posterior.posteriors", "posteriors", to={"locker-1": SECRET}),
    bad_event("nummap_bool_value", "rssi", "rssi.rssi", "rssi", to={"locker-1": True}),
    bad_event("nummap_not_mapping", "rssi", "rssi.rssi", "rssi", to=[-50]),
    bad_event("nummap_key_not_str", "rssi", "rssi.rssi", "rssi", to={7: -50}),
    bad_event("nummap_secret_key", "rssi", "rssi.rssi", "rssi", to={SECRET: SECRET}),
    # UTF-8 로 바꿀 수 없는 문자열
    bad_event("str_not_utf8", "mcp_result", "mcp_result.requestId", "requestId", to=UNENCODABLE),
    bad_event("optional_str_not_utf8", "step", "step.detail", "detail", to=UNENCODABLE),
    bad_event("nummap_key_not_utf8", "rssi", "rssi.rssi", "rssi", to={UNENCODABLE: -50}),
    bad_event("posteriors_key_not_utf8", "posterior", "posterior.posteriors", "posteriors", to={UNENCODABLE: 0.5}),
    # 중첩 객체
    bad_event("object_not_mapping", "mcp_call", "mcp_call.target", "target", to=SECRET),
    bad_event("nested_missing_key", "transition", "transition.fault.cause", "fault", "cause", to=DROP),
    bad_event("nested_enum_outside", "mcp_call", "mcp_call.target.kind", "target", "kind", to=SECRET),
    bad_event("nested_naive_time", "audit", "audit.row.at", "row", "at", to=NAIVE),
    bad_event("nested_enum_not_str", "audit", "audit.row.step", "row", "step", to=3),
    bad_event("nested_str_not_utf8", "audit", "audit.row.text", "row", "text", to=UNENCODABLE),
    # 사람
    bad_event("subject_missing", "beacon", "beacon.subject", subject=None),
    bad_event("subject_missing_rssi", "rssi", "rssi.subject", subject=None),
    bad_event("subject_is_user_id", "approval", "approval.subject", subject=VISITOR.user_id),
    bad_event("subject_only_in_fields", "approval", "approval.subject", "subject", to=VISITOR, subject=None),
]


@pytest.mark.parametrize(("bad", "path"), BAD_EVENTS)
def test_FR12B_형식이_틀린_이벤트는_경로만_담은_SchemaError(bad, path):
    with pytest.raises(SchemaError) as error:
        render_event(bad, PersonNames())
    assert str(error.value) == path  # 경로만 있다. 값과 사용자 ID 가 없다


# ── 조회 결과 ─────────────────────────────────────────────────────────


class ResponseCase(NamedTuple):
    id: str
    kind: str
    data: Any
    expected: Any


PLACES = {
    "weakRssiDbm": -80,
    "places": [
        {"placeId": "lounge", "placeName": "라운지", "modelVersion": "m1", "calibrationVersion": "c1",
         "devices": [
             {"nodeId": "locker-1", "name": "사물함 1", "capability": Capability.LOCKER,
              "occupancy": Occupancy.RENTAL},
             {"nodeId": "light-1", "name": "조명 1", "capability": "light", "occupancy": "shared"},
         ]},
        {"placeId": "side-room", "placeName": "옆방", "modelVersion": "m2", "calibrationVersion": "c2",
         "devices": [{"nodeId": "mood-1", "name": "무드등", "capability": "light", "occupancy": "shared"}]},
    ],
}
PLACES_OUT = {
    "weakRssiDbm": -80,
    "places": [
        {"placeId": "lounge", "placeName": "라운지", "modelVersion": "m1", "calibrationVersion": "c1",
         "devices": [
             {"nodeId": "locker-1", "name": "사물함 1", "capability": "locker", "occupancy": "rental"},
             {"nodeId": "light-1", "name": "조명 1", "capability": "light", "occupancy": "shared"},
         ]},
        {"placeId": "side-room", "placeName": "옆방", "modelVersion": "m2", "calibrationVersion": "c2",
         "devices": [{"nodeId": "mood-1", "name": "무드등", "capability": "light", "occupancy": "shared"}]},
    ],
}

USES = [
    {"nodeId": "locker-1", "occupancy": Occupancy.RENTAL, "session": SessionState.ACTIVE, "sessionNote": "반납 확인 중",
     "startedAt": EARLY, "overdueMinutes": 12.5, "subject": VISITOR},
    {"nodeId": "locker-2", "occupancy": "rental", "session": "RESERVED", "startedAt": NOW, "subject": OWNER},
]
USES_OUT = [
    {"personId": "p1", "nodeId": "locker-1", "occupancy": "rental", "session": "ACTIVE", "sessionNote": "반납 확인 중",
     "startedAt": EARLY_ISO, "overdueMinutes": 12.5},
    {"personId": "p2", "nodeId": "locker-2", "occupancy": "rental", "session": "RESERVED", "startedAt": ISO},
]

APPROVALS = {
    "pending": [{"nodeId": "locker-1", "requestedAt": NOW, "subject": VISITOR}],
    "active": [{"nodeId": "locker-1", "mode": ApprovalMode.TIME_WINDOW, "validUntil": LATER,
                "remainingMinutes": 45, "uses": 1, "subject": VISITOR}],
    "ended": [{"nodeId": "light-1", "mode": "one_time", "requestedAt": EARLY,
               "endReason": ReasonCode.APPROVAL_EXHAUSTED, "subject": OWNER}],
}
APPROVALS_OUT = {
    "pending": [{"personId": "p1", "nodeId": "locker-1", "requestedAt": ISO}],
    "active": [{"personId": "p1", "nodeId": "locker-1", "mode": "time_window", "remainingMinutes": 45,
                "validUntil": LATER_ISO, "uses": 1}],
    "ended": [{"personId": "p2", "nodeId": "light-1", "mode": "one_time", "requestedAt": EARLY_ISO,
               "endReason": "APPROVAL_EXHAUSTED"}],
}

AUDIT = [
    {"id": "a-2", "at": NOW, "step": Step.EXEC, "nodeId": "locker-1", "text": "명령을 보냈다", "code": ReasonCode.OK},
    {"id": "a-1", "at": EARLY, "step": "3", "text": "후보 확률이 임계값 미만", "code": "LOW_CONFIDENCE"},
]
AUDIT_OUT = [
    {"id": "a-2", "at": ISO, "step": "exec", "nodeId": "locker-1", "text": "명령을 보냈다", "code": "OK"},
    {"id": "a-1", "at": EARLY_ISO, "step": "3", "text": "후보 확률이 임계값 미만", "code": "LOW_CONFIDENCE"},
]

HISTORY = [
    {"requestId": "req-1", "at": NOW, "target": {"kind": TargetKind.NEAREST, "capability": Capability.LOCKER},
     "action": Action.OPEN, "verdict": Verdict.OK, "nodeId": "locker-1", "outcome": ExecutionStatus.SUCCEEDED,
     "code": ReasonCode.OK, "subject": VISITOR},  # str 자리에 온 StrEnum 멤버도 값 문자열로 나간다
    {"requestId": "req-2", "at": EARLY, "target": {"kind": "explicit", "nodeId": "light-1"}, "action": "on",
     "verdict": "ASK", "outcome": "더 가까이 와야 한다", "subject": OWNER},
]
HISTORY_OUT = [
    {"personId": "p1", "requestId": "req-1", "at": ISO, "target": {"kind": "nearest", "capability": "locker"},
     "action": "open", "verdict": "OK", "nodeId": "locker-1", "outcome": "succeeded", "code": "OK"},
    {"personId": "p2", "requestId": "req-2", "at": EARLY_ISO, "target": {"kind": "explicit", "nodeId": "light-1"},
     "action": "on", "verdict": "ASK", "outcome": "더 가까이 와야 한다"},
]

# 선택 키를 가능한 한 채운 요청 셋. 값의 앞뒤가 맞는지는 형식 검사의 일이 아니다
REQUEST = {  # 실행까지 끝난 요청
    "id": "req-1", "tool": "request_use", "target": {"kind": TargetKind.NEAREST, "capability": Capability.LOCKER},
    "action": Action.OPEN, "at": NOW, "verdict": Verdict.OK, "reason": ReasonCode.OK,
    "steps": [{"result": "pass"}, {"result": "pass"}, {"result": "pass", "detail": "후보 2개"},
              {"result": "pass", "detail": "p=0.97"}, {"result": "pass"}, {"result": "pass"}, {"result": "pass"}],
    "nodeId": "locker-1", "posteriors": {"locker-1": 0.97, "light-1": 0.03}, "threshold": 0.9,
    "execution": "succeeded", "accessRequestedAt": EARLY, "respondedAt": NOW, "subject": VISITOR,
}
REQUEST_OUT = {
    "personId": "p1", "id": "req-1", "tool": "request_use", "target": {"kind": "nearest", "capability": "locker"},
    "action": "open", "at": ISO, "verdict": "OK", "reason": "OK",
    "steps": [{"result": "pass"}, {"result": "pass"}, {"result": "pass", "detail": "후보 2개"},
              {"result": "pass", "detail": "p=0.97"}, {"result": "pass"}, {"result": "pass"}, {"result": "pass"}],
    "nodeId": "locker-1", "posteriors": {"locker-1": 0.97, "light-1": 0.03}, "threshold": 0.9,
    "execution": "succeeded", "accessRequestedAt": EARLY_ISO, "respondedAt": ISO,
}
REQUEST_STOPPED = {  # 승인이 필요해 ④에서 멈췄고 신청이 만료됐다
    "id": "req-2", "tool": "request_use", "target": {"kind": "explicit", "nodeId": "locker-1"}, "action": "open",
    "at": EARLY, "verdict": "NEED_APPROVAL", "reason": ReasonCode.APPROVAL_REQUIRED,
    "steps": [{"result": "pass"}, {"result": "pass"}, {"result": "pass"}, {"result": "pass"},
              {"result": "stop", "detail": "승인 필요"}, {"result": "skip"}, {"result": "skip"}],
    "nodeId": "locker-1", "accessRequestedAt": EARLY, "accessExpiredAt": NOW, "respondedAt": NOW,
    "subject": VISITOR,
}
REQUEST_STOPPED_OUT = {
    "personId": "p1", "id": "req-2", "tool": "request_use", "target": {"kind": "explicit", "nodeId": "locker-1"},
    "action": "open", "at": EARLY_ISO, "verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED",
    "steps": [{"result": "pass"}, {"result": "pass"}, {"result": "pass"}, {"result": "pass"},
              {"result": "stop", "detail": "승인 필요"}, {"result": "skip"}, {"result": "skip"}],
    "nodeId": "locker-1", "accessRequestedAt": EARLY_ISO, "accessExpiredAt": ISO, "respondedAt": ISO,
}
REQUEST_PENDING = {  # 판정 전. 선택 키가 모두 없다
    "id": "req-3", "tool": "request_use", "target": {"kind": "any", "capability": "light"}, "action": "on",
    "at": NOW, "steps": [{"result": "pending"}] * 7, "subject": OWNER,
}
REQUEST_PENDING_OUT = {
    "personId": "p1", "id": "req-3", "tool": "request_use", "target": {"kind": "any", "capability": "light"},
    "action": "on", "at": ISO, "steps": [{"result": "pending"}] * 7,
}

TRUST = {
    "live": {
        "since": EARLY, "decisions": 40, "doubleOccupancy": 0, "latencyMedianSec": 0.84, "latencyP95Sec": None,
        # 키는 Verdict 멤버와 값 문자열이 섞여 있고 순서도 다르다
        "counts": {"DENY": 2, Verdict.OK: 30, "NEED_APPROVAL": 1, "BUSY": 4, Verdict.ASK: 3},
    },
    "evaluation": {
        "date": "2026-10-20",
        "metrics": [
            {"id": 1, "name": "올바른 기기 선택", "pass": True, "label": "정확도", "sample": "n=50"},
            {"id": 2, "name": "이중 점유", "pass": None, "label": "건수", "sample": "n=0", "sampleNote": "아직 모른다"},
        ],
        "confusion": {"labels": ["locker-1", "light-1", "none"],
                      "values": [[98, 1, 1], [2.5, 97, 0.5], [0, 0, 100]]},
    },
}
TRUST_OUT = {
    "live": {
        "since": EARLY_ISO, "decisions": 40, "doubleOccupancy": 0, "latencyMedianSec": 0.84, "latencyP95Sec": None,
        "counts": {"OK": 30, "BUSY": 4, "ASK": 3, "DENY": 2, "NEED_APPROVAL": 1},
    },
    "evaluation": {
        "date": "2026-10-20",
        "metrics": [
            {"id": 1, "name": "올바른 기기 선택", "pass": True, "label": "정확도", "sample": "n=50"},
            {"id": 2, "name": "이중 점유", "pass": None, "label": "건수", "sample": "n=0", "sampleNote": "아직 모른다"},
        ],
        "confusion": {"labels": ["locker-1", "light-1", "none"],
                      "values": [[98, 1, 1], [2.5, 97, 0.5], [0, 0, 100]]},
    },
}

RENTAL = {
    "nodeId": "locker-1", "startedAt": EARLY, "overdueMinutes": 12.5, "subject": VISITOR,
    "timeline": [{"at": EARLY, "text": "대여를 시작했다"}, {"at": NOW, "text": "대여 시간이 지났다", "now": True}],
}
RENTAL_OUT = {
    "personId": "p1", "nodeId": "locker-1", "startedAt": EARLY_ISO, "overdueMinutes": 12.5,
    "timeline": [{"at": EARLY_ISO, "text": "대여를 시작했다"}, {"at": ISO, "text": "대여 시간이 지났다", "now": True}],
}

HEALTH = {
    "devices": [{"nodeId": "locker-1", "connected": True, "trust": TrustState.TRUSTED},
                {"nodeId": "light-1", "connected": False, "trust": "discovered"}],
    "beacons": [{"subject": VISITOR, "label": "무시된다", "lastSeenSec": 2.5, "note": "신호가 약하다"},
                {"subject": OWNER, "lastSeenSec": None}],
    "server": {"database": True, "mqtt": False, "model": "m1"},
}
HEALTH_OUT = {
    "devices": [{"nodeId": "locker-1", "connected": True, "trust": "trusted"},
                {"nodeId": "light-1", "connected": False, "trust": "discovered"}],
    "beacons": [{"personId": "p1", "label": "방문자 A", "lastSeenSec": 2.5, "note": "신호가 약하다"},
                {"personId": "p2", "label": "소유자", "lastSeenSec": None}],
    "server": {"database": True, "mqtt": False, "model": "m1"},
}

EMPTY_APPROVALS = {"pending": [], "active": [], "ended": []}
RESPONSE_CASES = [
    ResponseCase("places", "places", PLACES, PLACES_OUT),
    ResponseCase("places_no_devices", "places", patched(PLACES, ("places", 1, "devices"), []),
                 patched(PLACES_OUT, ("places", 1, "devices"), [])),
    ResponseCase("places_empty", "places", {**PLACES, "places": []}, {**PLACES_OUT, "places": []}),
    ResponseCase("uses", "uses", USES, USES_OUT),
    ResponseCase("uses_tuple", "uses", tuple(USES), USES_OUT),
    ResponseCase("uses_empty", "uses", [], []),
    ResponseCase("approvals", "approvals", APPROVALS, APPROVALS_OUT),
    ResponseCase("approvals_empty", "approvals", EMPTY_APPROVALS, EMPTY_APPROVALS),
    ResponseCase("audit", "audit", AUDIT, AUDIT_OUT),
    ResponseCase("audit_empty", "audit", [], []),
    ResponseCase("history", "history", HISTORY, HISTORY_OUT),
    ResponseCase("history_empty", "history", [], []),
    ResponseCase("request", "request", REQUEST, REQUEST_OUT),
    ResponseCase("request_stopped", "request", REQUEST_STOPPED, REQUEST_STOPPED_OUT),
    ResponseCase("request_pending", "request", REQUEST_PENDING, REQUEST_PENDING_OUT),
    ResponseCase("trust", "trust", TRUST, TRUST_OUT),
    ResponseCase("trust_no_evaluation", "trust", patched(TRUST, ("evaluation",), DROP),
                 patched(TRUST_OUT, ("evaluation",), DROP)),
    ResponseCase("trust_no_confusion", "trust", patched(TRUST, ("evaluation", "confusion"), DROP),
                 patched(TRUST_OUT, ("evaluation", "confusion"), DROP)),
    ResponseCase("rental", "rental", RENTAL, RENTAL_OUT),
    ResponseCase("health", "health", HEALTH, HEALTH_OUT),
]


@pytest.mark.parametrize("case", RESPONSE_CASES, ids=lambda c: c.id)
def test_FR12A_조회_종류마다_형식대로_돌려준다(case):
    before = copy.deepcopy(case.data)
    result = render_response(case.kind, case.data, PersonNames())
    assert result == case.expected
    assert dumps(result) == dumps(case.expected)  # 키 순서. 사람 필드는 행의 키 앞에 있다
    assert_plain(result)
    assert case.data == before  # 입력을 고치지 않는다


@pytest.mark.parametrize("case", RESPONSE_CASES, ids=lambda c: c.id)
def test_FR12A_조회_결과에서_허용_목록_밖의_키와_사용자_ID를_버린다(case):
    clean, noisy = SpyNames(), SpyNames()
    render_response(case.kind, case.data, clean)
    result = render_response(case.kind, with_junk(case.data), noisy)
    assert result == case.expected  # personId 와 label 은 가짜 값이 아니라 이름표의 값이다
    assert noisy.asked == clean.asked  # 사람이 없는 형식에 낀 subject 는 보지 않는다
    assert VISITOR.user_id not in json.dumps(result)


def test_FR12A_dict와_list가_아닌_Mapping과_Sequence도_받는다():
    places = MappingProxyType({
        **PLACES,
        "places": tuple(
            MappingProxyType({**place, "devices": tuple(MappingProxyType(device) for device in place["devices"])})
            for place in PLACES["places"]
        ),
    })
    assert render_response("places", places, PersonNames()) == PLACES_OUT
    request = MappingProxyType({
        **REQUEST,
        "steps": tuple(MappingProxyType(step) for step in REQUEST["steps"]),
        "posteriors": MappingProxyType(REQUEST["posteriors"]),
    })
    assert render_response("request", request, PersonNames()) == REQUEST_OUT


def bad_response(id_: str, kind: str, base: Any, path: str, where: tuple, to: Any):
    """base 에서 where 가 가리키는 자리를 to 로 바꾼 data 와 기대 경로."""
    return pytest.param(kind, patched(base, where, to), path, id=id_)


BAD_RESPONSES = [
    # 최상위
    pytest.param(SECRET, [], "kind", id="unknown_kind"),
    pytest.param(["uses"], [], "kind", id="kind_not_str"),
    pytest.param("uses", {"nodeId": "locker-1"}, "uses", id="mapping_for_list"),
    pytest.param("uses", SECRET, "uses", id="str_for_list"),
    pytest.param("uses", None, "uses", id="none_for_list"),
    pytest.param("places", [PLACES], "places", id="list_for_object"),
    # 목록과 행
    bad_response("row_not_mapping", "uses", USES, "uses[1]", (1,), SECRET),
    bad_response("naive_time_in_list", "uses", [*USES, {**USES[0]}], "uses[2].startedAt", (2, "startedAt"), NAIVE),
    bad_response("row_without_subject", "uses", USES, "uses[1].subject", (1, "subject"), DROP),
    bad_response("subject_is_user_id", "uses", USES, "uses[0].subject", (0, "subject"), VISITOR.user_id),
    bad_response("lit_outside", "uses", USES, "uses[0].session", (0, "session"), "CLOSED"),
    bad_response("places_missing", "places", PLACES, "places.places", ("places",), DROP),
    bad_response("places_not_list", "places", PLACES, "places.places", ("places",), PLACES["places"][0]),
    bad_response("place_row_not_mapping", "places", PLACES, "places.places[1]", ("places", 1), SECRET),
    bad_response("place_id_missing", "places", PLACES, "places.places[1].placeId", ("places", 1, "placeId"), DROP),
    bad_response("place_id_int", "places", PLACES, "places.places[1].placeId", ("places", 1, "placeId"), 7),
    bad_response("place_id_not_utf8", "places", PLACES, "places.places[1].placeId",
                 ("places", 1, "placeId"), UNENCODABLE),
    bad_response("place_devices_missing", "places", PLACES, "places.places[0].devices",
                 ("places", 0, "devices"), DROP),
    bad_response("place_nested_enum", "places", PLACES, "places.places[0].devices[1].capability",
                 ("places", 0, "devices", 1, "capability"), "gate"),
    bad_response("weak_rssi_missing", "places", PLACES, "places.weakRssiDbm", ("weakRssiDbm",), DROP),
    bad_response("weak_rssi_bool_for_num", "places", PLACES, "places.weakRssiDbm", ("weakRssiDbm",), True),
    pytest.param(  # 약한 신호 기준은 응답 전체에 하나다. 장소마다 있는 값은 그것을 대신하지 않는다
        "places", {"places": [{**place, "weakRssiDbm": -80} for place in PLACES["places"]]}, "places.weakRssiDbm",
        id="weak_rssi_only_in_places"),
    # 한 기기가 두 장소에 나온다. 경로만 있고 겹친 nodeId 는 없다
    bad_response("node_in_two_places", "places", PLACES, "places.places",
                 ("places", 1, "devices", 0, "nodeId"), "locker-1"),
    bad_response("node_twice_in_place", "places", PLACES, "places.places",
                 ("places", 0, "devices", 1, "nodeId"), "locker-1"),  # 한 장소 안에서 두 번
    bad_response("place_id_twice", "places", PLACES, "places.places", ("places", 1, "placeId"), "lounge"),
    bad_response("approvals_list_missing", "approvals", APPROVALS, "approvals.pending", ("pending",), DROP),
    bad_response("end_reason_outside", "approvals", APPROVALS, "approvals.ended[0].endReason",
                 ("ended", 0, "endReason"), "REJECTED"),
    bad_response("audit_enum_outside", "audit", AUDIT, "audit[0].code", (0, "code"), SECRET),
    bad_response("history_target_missing", "history", HISTORY, "history[0].target", (0, "target"), DROP),
    bad_response("history_outcome_missing", "history", HISTORY, "history[1].outcome", (1, "outcome"), DROP),
    bad_response("str_not_utf8", "audit", AUDIT, "audit[0].text", (0, "text"), UNENCODABLE),  # UTF-8 로 바꿀 수 없다
    # 요청 한 건
    bad_response("six_steps", "request", REQUEST, "request.steps", ("steps", 6), DROP),
    bad_response("eight_steps", "request", REQUEST, "request.steps", ("steps",),
                 [*REQUEST["steps"], {"result": "pass"}]),
    bad_response("steps_missing", "request", REQUEST, "request.steps", ("steps",), DROP),
    bad_response("step_result_outside", "request", REQUEST, "request.steps[2].result",
                 ("steps", 2, "result"), "done"),
    bad_response("tool_outside", "request", REQUEST, "request.tool", ("tool",), "release_use"),
    bad_response("target_missing", "request", REQUEST, "request.target", ("target",), DROP),
    bad_response("execution_outside", "request", REQUEST, "request.execution", ("execution",), "done"),
    bad_response("execution_sent", "request", REQUEST, "request.execution", ("execution",), "sent"),  # ExecutionStatus 에 없다
    bad_response("posteriors_key_not_utf8", "request", REQUEST, "request.posteriors", ("posteriors",), {UNENCODABLE: 0.5}),
    bad_response("optional_naive_time", "request", REQUEST, "request.respondedAt", ("respondedAt",), NAIVE),
    # 신뢰도
    bad_response("counts_missing_key", "trust", TRUST, "trust.live.counts",
                 ("live", "counts", "NEED_APPROVAL"), DROP),
    bad_response("counts_extra_key", "trust", TRUST, "trust.live.counts", ("live", "counts", SECRET), 1),
    bad_response("counts_other_key_instead", "trust", TRUST, "trust.live.counts", ("live", "counts"),
                 {"OK": 30, "BUSY": 4, "ASK": 3, "DENY": 2, SECRET: 1}),  # 개수는 다섯 개로 맞다
    bad_response("counts_key_not_utf8", "trust", TRUST, "trust.live.counts", ("live", "counts"),
                 {"OK": 30, "BUSY": 4, "ASK": 3, "DENY": 2, UNENCODABLE: 1}),
    bad_response("counts_bool_value", "trust", TRUST, "trust.live.counts.OK", ("live", "counts", "OK"), True),
    bad_response("counts_not_mapping", "trust", TRUST, "trust.live.counts", ("live", "counts"), [1, 2, 3, 4, 5]),
    bad_response("null_key_missing", "trust", TRUST, "trust.live.latencyP95Sec", ("live", "latencyP95Sec"), DROP),
    bad_response("null_allowed_str", "trust", TRUST, "trust.live.latencyMedianSec",
                 ("live", "latencyMedianSec"), "0.8"),
    bad_response("metric_pass_str", "trust", TRUST, "trust.evaluation.metrics[0].pass",
                 ("evaluation", "metrics", 0, "pass"), "yes"),
    bad_response("metric_pass_missing", "trust", TRUST, "trust.evaluation.metrics[1].pass",
                 ("evaluation", "metrics", 1, "pass"), DROP),
    bad_response("matrix_cell_str", "trust", TRUST, "trust.evaluation.confusion.values[1][0]",
                 ("evaluation", "confusion", "values", 1, 0), "98"),
    bad_response("matrix_row_str", "trust", TRUST, "trust.evaluation.confusion.values[0]",
                 ("evaluation", "confusion", "values", 0), "98"),
    bad_response("labels_str", "trust", TRUST, "trust.evaluation.confusion.labels",
                 ("evaluation", "confusion", "labels"), "locker-1"),
    bad_response("labels_item_int", "trust", TRUST, "trust.evaluation.confusion.labels[1]",
                 ("evaluation", "confusion", "labels", 1), 7),
    bad_response("labels_item_not_utf8", "trust", TRUST, "trust.evaluation.confusion.labels[1]",
                 ("evaluation", "confusion", "labels", 1), UNENCODABLE),
    # 대여와 점검
    bad_response("rental_subject_missing", "rental", RENTAL, "rental.subject", ("subject",), DROP),
    bad_response("rental_overdue_missing", "rental", RENTAL, "rental.overdueMinutes", ("overdueMinutes",), DROP),
    bad_response("rental_timeline_time", "rental", RENTAL, "rental.timeline[1].at", ("timeline", 1, "at"), NAIVE),
    bad_response("beacon_subject_missing", "health", HEALTH, "health.beacons[0].subject",
                 ("beacons", 0, "subject"), DROP),
    bad_response("beacon_last_seen_missing", "health", HEALTH, "health.beacons[1].lastSeenSec",
                 ("beacons", 1, "lastSeenSec"), DROP),
    bad_response("server_str_for_bool", "health", HEALTH, "health.server.mqtt", ("server", "mqtt"), "up"),
    bad_response("device_trust_outside", "health", HEALTH, "health.devices[0].trust",
                 ("devices", 0, "trust"), "banned"),
]


@pytest.mark.parametrize(("kind", "data", "path"), BAD_RESPONSES)
def test_FR12A_형식이_틀린_조회는_경로만_담은_SchemaError(kind, data, path):
    with pytest.raises(SchemaError) as error:
        render_response(kind, data, PersonNames())
    assert str(error.value) == path  # 경로만 있다. 값과 사용자 ID 가 없다


def test_FR12B_형식_값은_shared_열거형_값을_쓴다():
    names = PersonNames()

    def step_event(step: Any) -> dict[str, Any]:
        return render_event(event("step", {**GOOD["step"].fields, "step": step}), names)

    def approval_event(kind: Any) -> dict[str, Any]:
        return render_event(event("approval", {**GOOD["approval"].fields, "kind": kind}, VISITOR), names)

    def request_response(execution: Any) -> dict[str, Any]:
        return render_response("request", {**REQUEST, "execution": execution}, names)

    # step 이벤트의 step: 판정 단계 ⓪~⑥(Step 의 "0"~"6")만 받는다. Step 멤버나 값 문자열로 와서 값 문자열로 나간다
    judgment = {str(n) for n in range(7)}
    assert step_event(Step.PROXIMITY)["step"] == step_event("3")["step"] == "3"
    for member in Step:
        for value in (member, member.value):
            if member.value in judgment:
                assert step_event(value)["step"] == member.value
            else:  # auth, exec, close, fault, announce, admin
                with pytest.raises(SchemaError, match=r"^step\.step$"):
                    step_event(value)
    for value in (0, 3, 6, True, 3.0):  # 정수, bool, 실수는 받지 않는다
        with pytest.raises(SchemaError, match=r"^step\.step$"):
            step_event(value)

    # approval 이벤트의 kind: 소유자 거부는 ApprovalStatus.DENIED 의 값 "denied" 다
    assert approval_event(ApprovalStatus.DENIED)["kind"] == approval_event("denied")["kind"] == "denied"
    assert approval_event(ApprovalStatus.APPROVED)["kind"] == "approved"
    assert [approval_event(kind)["kind"] for kind in ("requested", "expired")] == ["requested", "expired"]
    for kind in ("rejected", ApprovalStatus.PENDING.value):  # 대기(pending)는 이벤트 kind 가 아니다 (requested 로 보낸다)
        with pytest.raises(SchemaError, match=r"^approval\.kind$"):
            approval_event(kind)

    # RequestProgress 의 execution: ExecutionStatus 의 값 모두
    for status in ExecutionStatus:
        assert request_response(status)["execution"] == request_response(status.value)["execution"] == status.value
    with pytest.raises(SchemaError, match=r"^request\.execution$"):
        request_response("sent")


def test_FR12B_같은_이름표로_이벤트와_조회가_같은_사람을_내보낸다():
    names = PersonNames()
    other = Subject("user-visitor-2", Role.VISITOR)
    beacon = render_event(event("beacon", {"seen": True}, VISITOR), names)
    other_beacon = render_event(event("beacon", {"seen": True}, other), names)
    uses = render_response("uses", [{**USES[0], "subject": subject} for subject in (other, VISITOR, OWNER)], names)
    health = render_response("health", {**HEALTH, "beacons": [{"subject": VISITOR, "lastSeenSec": 1.0}]}, names)
    assert (beacon["personId"], beacon["pseudonym"]) == ("p1", "방문자 A")
    assert (other_beacon["personId"], other_beacon["pseudonym"], other_beacon["letter"]) == ("p2", "방문자 B", "B")
    assert [row["personId"] for row in uses] == ["p2", "p1", "p3"]  # 조회에서도 같은 사람이 같은 personId 다
    assert health["beacons"][0] == {"personId": "p1", "label": "방문자 A", "lastSeenSec": 1.0}
