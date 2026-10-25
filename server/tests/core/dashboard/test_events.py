"""FR-12B 행 → 모니터링 이벤트와 감사 행. 손으로 만든 행은 pipeline.py·execution_store.py 가 실제로 쓰는 모양이다."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import render_event
from nearpilot.core.dashboard import (
    approval_event,
    audit_node,
    audit_row,
    beacon_event,
    request_events,
    target_of,
    transition_event,
)
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
    Step,
    TargetKind,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import (
    DashboardApproval,
    DashboardAudit,
    DashboardCommand,
    DashboardNode,
    DashboardRequest,
    MonitorEvent,
    Subject,
)

T0 = datetime(2026, 10, 24, 14, 0, 0, tzinfo=timezone.utc)
NAMES = {"locker-1": "사물함 1", "locker-2": "사물함 2"}
SECRET = "user-secret-7f3a"  # 사용자 ID 자리. 어떤 이벤트·행에도 나오면 안 된다
VISITOR = Subject(SECRET, Role.VISITOR)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def trail(*steps: tuple[Step, dict], start: int = 1) -> list[DashboardAudit]:
    """감사 행을 rowid `start` 부터 차례로 붙인다. 행 시각은 rowid 초다."""
    return [
        DashboardAudit(seq=i, log_id=f"log-{i}", internal_request_id="i1", command_id=None, step=step,
                       payload=payload, created_at=at(i))
        for i, (step, payload) in enumerate(steps, start=start)
    ]


def request(
    verdict: Verdict | None, reason: ReasonCode | None, node_id: str | None = "locker-1", *,
    kind: str = "visitor", target_spec: str = "nearest:locker", action: Action = Action.OPEN,
) -> DashboardRequest:
    return DashboardRequest(
        seq=1, internal_request_id="i1", user_id=None if kind == "server_internal" else SECRET, kind=kind,
        target_spec=target_spec, action=action, verdict=verdict, reason_code=reason, node_id=node_id,
        decision_context=None, received_at=T0)


def command(
    seq: int = 1, status: ExecutionStatus = ExecutionStatus.PENDING, *, started: int | None = None,
    outcome: str | None = None, occurred: int | None = None, node_id: str = "locker-1",
    action: Action = Action.OPEN, evidence: dict | None = None,
) -> DashboardCommand:
    return DashboardCommand(
        seq=seq, command_id=f"c{seq}", internal_request_id="i1", node_id=node_id, action=action, status=status,
        use_id="use-1", send_started_at=None if started is None else at(started), outcome=outcome,
        occurred_at=None if occurred is None else at(occurred), evidence=evidence or {})


# ── 판정 흐름이 남기는 감사 행 (pipeline.py 의 _audit_step 과 같은 모양) ─────────────

BEACON = (Step.BEACON, {"beacons": 1, "frames": 3, "anchors": ["locker-1"], "beacon_recent_sec": 10})
NO_BEACON = (Step.BEACON, {"beacons": 1, "frames": 0, "anchors": [], "beacon_recent_sec": 10})
CANDIDATE = (Step.CANDIDATE, {"place_id": "p1", "candidates": ["locker-1", "locker-2"], "online_ttl_sec": 10})
NEAR = (Step.PROXIMITY, {"posteriors": {"locker-1": 0.91, "locker-2": 0.05}, "threshold": 0.5,
                         "model_version": "m1", "calib_version": "c1", "best": ["locker-1", 0.91]})
POLICY = (Step.POLICY, {"node_id": "locker-1", "verdict": "OK", "reason": "OK", "policy_version": 1})
RESERVED = (Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "reserved"})


def decided(step: Step, verdict: str, reason: str) -> tuple[Step, dict]:
    return step, {"verdict": verdict, "reason": reason, "settings_version": "v1"}


OK_RENTAL = trail(BEACON, CANDIDATE, NEAR, POLICY, RESERVED, decided(Step.USER_OCCUPANCY, "OK", "OK"))
OK_SHARED = trail(BEACON, CANDIDATE, NEAR, POLICY, RESERVED, decided(Step.POLICY, "OK", "OK"))
DENY_BEACON = trail(NO_BEACON, decided(Step.BEACON, "DENY", "BEACON_NOT_SEEN"))
ASK_PROXIMITY = trail(
    BEACON, CANDIDATE,
    (Step.PROXIMITY, {"posteriors": {"locker-1": 0.31}, "threshold": 0.5, "model_version": "m1",
                      "calib_version": "c1", "best": ["locker-1", 0.31]}),
    decided(Step.PROXIMITY, "ASK", "LOW_CONFIDENCE"))

OK = request(Verdict.OK, ReasonCode.OK)
SENT = command(1, ExecutionStatus.IN_PROGRESS, started=10)
DONE = command(1, ExecutionStatus.SUCCEEDED, started=10, outcome="succeeded", occurred=12)


def types(events: list[MonitorEvent]) -> list[str]:
    return [e.type for e in events]


# ── 요청 하나의 이벤트 ───────────────────────────────────────────────


def test_FR12B_요청_이벤트_순서():
    # 판정 중 → 판정 저장 → 명령 보냄 → 결과 저장으로 나아가며 부른다
    progress = [
        (request(None, None), [], []),  # 감사 행이 아직 없다 (도구를 모른다)
        (request(None, None), OK_RENTAL[:1], []),  # ① 행이 나왔다
        (request(None, None), OK_RENTAL[:5], []),  # 판정 중
        (OK, OK_RENTAL, []),  # 판정이 저장됐다
        (OK, OK_RENTAL, [command(1, ExecutionStatus.PENDING)]),  # 명령이 만들어졌다 (보내기 전)
        (OK, OK_RENTAL, [SENT]),  # 보내기 시작했다
        (OK, OK_RENTAL, [DONE]),  # 결과가 저장됐다
    ]
    lists = [request_events(req, rows, cmds, VISITOR, Occupancy.RENTAL, NAMES) for req, rows, cmds in progress]
    for earlier, later in zip(lists, lists[1:]):
        assert later[: len(earlier)] == earlier  # 앞 목록이 뒤 목록의 앞부분이다
    assert types(lists[-1]) == [
        "mcp_call", *["step"] * 7, "posterior", "verdict", "command", "exec_result", "mcp_result"]
    assert [len(events) for events in lists] == [0, 1, 1, 10, 10, 11, 13]


def test_FR12B_요청_이벤트의_필드와_시각():
    events = request_events(OK, OK_RENTAL, [DONE], VISITOR, Occupancy.RENTAL, NAMES)
    by_type = {e.type: e for e in events}
    call = by_type["mcp_call"]
    assert call.at == T0 and call.subject == VISITOR
    assert dict(call.fields) == {
        "requestId": "i1", "tool": "request_use", "target": {"kind": TargetKind.NEAREST,
                                                             "capability": Capability.LOCKER},
        "action": Action.OPEN}
    steps = [e for e in events if e.type == "step"]
    assert [dict(e.fields) for e in steps][:2] == [
        {"requestId": "i1", "step": Step.DEDUP, "result": "pass"},  # ⓪ 은 감사 행이 없어 detail 키가 없다
        {"requestId": "i1", "step": Step.BEACON, "result": "pass", "detail": "비콘 최근 수신"},
    ]
    assert [e.at for e in steps] == [T0, at(1), at(2), at(3), at(4), at(5), at(5)]  # ⓪ 은 받은 시각
    assert [e.at for e in steps] == sorted(e.at for e in steps)  # 단계 순서와 시각 순서가 같다
    posterior = by_type["posterior"]
    assert dict(posterior.fields) == {
        "requestId": "i1", "posteriors": {"locker-1": 0.91, "locker-2": 0.05}, "threshold": 0.5}
    assert posterior.at == at(3)
    verdict = by_type["verdict"]
    assert dict(verdict.fields) == {"requestId": "i1", "verdict": Verdict.OK, "reason": ReasonCode.OK,
                                    "nodeId": "locker-1"}
    assert verdict.at == at(6)
    assert dict(by_type["command"].fields) == {"requestId": "i1", "nodeId": "locker-1", "action": Action.OPEN}
    assert by_type["command"].at == at(10)
    assert dict(by_type["exec_result"].fields) == {"requestId": "i1", "nodeId": "locker-1", "result": "succeeded"}
    assert by_type["exec_result"].at == at(12)
    assert dict(by_type["mcp_result"].fields) == {"requestId": "i1"} and by_type["mcp_result"].at == at(12)
    # 사람은 mcp_call 에만 있다
    assert [e.subject for e in events if e.type != "mcp_call"] == [None] * (len(events) - 1)


def test_FR12B_거부_요청은_판정_뒤_바로_응답():
    req = request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None)
    events = request_events(req, DENY_BEACON, [], VISITOR, None, NAMES)
    assert types(events) == ["mcp_call", "step", "step", "verdict", "mcp_result"]
    assert [dict(e.fields) for e in events[1:3]] == [
        {"requestId": "i1", "step": Step.DEDUP, "result": "pass"},
        {"requestId": "i1", "step": Step.BEACON, "result": "stop", "detail": "비콘 수신 없음"},
    ]
    assert dict(events[3].fields) == {"requestId": "i1", "verdict": Verdict.DENY,
                                      "reason": ReasonCode.BEACON_NOT_SEEN}  # 기기가 없으면 nodeId 키가 없다
    assert events[4].at == events[3].at == at(2)  # mcp_result 의 시각은 그 앞 이벤트의 시각


def test_FR12B_거부_요청은_posterior_없이_근접_단계에서_멈춘다():
    ask = request(Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None)
    events = request_events(ask, ASK_PROXIMITY, [], VISITOR, Occupancy.RENTAL, NAMES)
    assert types(events) == ["mcp_call", "step", "step", "step", "step", "posterior", "verdict", "mcp_result"]
    assert events[4].fields["result"] == "stop" and events[4].fields["step"] is Step.PROXIMITY
    assert events[4].fields["detail"] == "근접 부족 0.31"
    assert dict(events[5].fields)["threshold"] == 0.5
    # ① 에서 멈추면 근접 행이 없어 posterior 도 없다
    assert "posterior" not in types(request_events(
        request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None), DENY_BEACON, [], VISITOR, None, NAMES))


def test_FR12B_공용_OK는_점유_단계를_발행하지_않는다():
    events = request_events(OK, OK_SHARED, [DONE], VISITOR, Occupancy.SHARED, NAMES)
    assert types(events) == ["mcp_call", *["step"] * 5, "posterior", "verdict", "command", "exec_result", "mcp_result"]
    assert [e.fields["step"] for e in events if e.type == "step"] == [
        Step.DEDUP, Step.BEACON, Step.CANDIDATE, Step.PROXIMITY, Step.POLICY]
    # 점유형을 모르면 일곱 칸 모두 발행한다
    assert types(request_events(OK, OK_SHARED, [], VISITOR, None, NAMES)).count("step") == 7


def test_FR12B_서버_내부_요청과_접근_신청은_명령만():
    internal = request(Verdict.OK, ReasonCode.OK, kind="server_internal", target_spec="locker-1", action=Action.OPEN)
    rows = trail((Step.CLOSE, {"event": "finish_close", "use_id": "u1", "check_id": "c1", "evidence": {}}))
    events = request_events(internal, rows, [DONE], None, Occupancy.RENTAL, NAMES)
    assert types(events) == ["command", "exec_result"]  # 방문자 요청이 아니면 mcp_call 도 mcp_result 도 없다
    # 사람이 있어도 서버 내부 요청은 도구 호출이 아니다
    assert types(request_events(internal, rows, [DONE], VISITOR, Occupancy.RENTAL, NAMES)) == ["command", "exec_result"]
    # request_access 는 ④ 행만 남긴다
    access_rows = trail((Step.POLICY, {"node_id": "locker-2", "verdict": "NEED_APPROVAL",
                                       "reason": "APPROVAL_REQUIRED", "policy_version": 1}))
    access = request(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, "locker-2")
    assert request_events(access, access_rows, [], VISITOR, Occupancy.RENTAL, NAMES) == []
    assert types(request_events(access, access_rows, [SENT], VISITOR, Occupancy.RENTAL, NAMES)) == ["command"]
    # 감사 행이 아직 없는 요청도 명령만 발행한다
    assert types(request_events(OK, [], [DONE], VISITOR, Occupancy.RENTAL, NAMES)) == ["command", "exec_result"]


def test_FR12B_사람이_없는_방문자_요청은_도구_호출로_발행하지_않는다():
    # mcp_call 은 사람 없이 만들 수 없다. 판정 이벤트도 도구 호출 없이 나가지 않는다
    assert request_events(OK, OK_RENTAL, [], None, Occupancy.RENTAL, NAMES) == []
    assert types(request_events(OK, OK_RENTAL, [DONE], None, Occupancy.RENTAL, NAMES)) == ["command", "exec_result"]


def test_FR12B_판정_중_요청은_도구_호출만_발행한다():
    pending = request(None, None, None)
    assert request_events(pending, [], [], VISITOR, None, NAMES) == []
    assert types(request_events(pending, OK_RENTAL[:2], [], VISITOR, None, NAMES)) == ["mcp_call"]


def test_FR12B_mcp_result는_명령이_모두_끝난_뒤에_붙는다():
    second = command(2, ExecutionStatus.IN_PROGRESS, started=14)
    done_first = command(1, ExecutionStatus.SUCCEEDED, started=10, outcome="succeeded", occurred=12)
    second_done = command(2, ExecutionStatus.SUCCEEDED, started=14, outcome="succeeded", occurred=16)
    assert "mcp_result" not in types(request_events(OK, OK_RENTAL, [], VISITOR, Occupancy.RENTAL, NAMES))
    assert "mcp_result" not in types(request_events(OK, OK_RENTAL, [SENT], VISITOR, Occupancy.RENTAL, NAMES))
    assert "mcp_result" not in types(
        request_events(OK, OK_RENTAL, [done_first, second], VISITOR, Occupancy.RENTAL, NAMES))
    events = request_events(OK, OK_RENTAL, [done_first, second_done], VISITOR, Occupancy.RENTAL, NAMES)
    assert types(events)[-5:] == ["command", "exec_result", "command", "exec_result", "mcp_result"]
    assert events[-1].at == at(16)  # 마지막 이벤트의 시각
    # 명령은 넘긴 순서가 아니라 rowid 순이다
    shuffled = request_events(OK, OK_RENTAL, [second_done, done_first], VISITOR, Occupancy.RENTAL, NAMES)
    assert shuffled == events


def test_FR12B_보내지_않고_취소한_명령은_이벤트가_없다():
    not_sent = command(1, ExecutionStatus.NOT_SENT, started=10, outcome="not_sent",
                       evidence={"source": "no_connection", "reason": "NODE_NOT_CONNECTED"}, occurred=11)
    events = request_events(OK, OK_RENTAL, [not_sent], VISITOR, Occupancy.RENTAL, NAMES)
    assert "command" not in types(events) and "exec_result" not in types(events)
    assert types(events)[-2:] == ["verdict", "mcp_result"]  # 취소로 명령이 끝났으니 도구 호출도 끝난다
    # 전송 시작 전에 취소한 명령 (send_started_at 없음)
    never_started = command(1, ExecutionStatus.NOT_SENT, outcome="not_sent", occurred=11)
    assert types(request_events(OK, OK_RENTAL, [never_started], VISITOR, Occupancy.RENTAL, NAMES))[-1] == "mcp_result"


def test_FR12B_실행_결과_이벤트는_거절을_rejected로_보낸다():
    for status, outcome, expected in [
        (ExecutionStatus.REJECTED, "rejected", "rejected"),
        (ExecutionStatus.FAILED, "failed", "failed"),
        (ExecutionStatus.UNKNOWN, "unknown", "unknown"),
    ]:
        events = request_events(
            OK, OK_RENTAL, [command(1, status, started=10, outcome=outcome, occurred=12)], VISITOR,
            Occupancy.RENTAL, NAMES)
        assert [e.fields["result"] for e in events if e.type == "exec_result"] == [expected], outcome
        assert types(events)[-1] == "mcp_result"


def test_FR12B_명령_이벤트의_시각과_기기():
    two_nodes = [
        command(1, ExecutionStatus.SUCCEEDED, started=10, outcome="succeeded", occurred=12),
        command(2, ExecutionStatus.SUCCEEDED, started=20, outcome="succeeded", occurred=None, node_id="locker-2",
                action=Action.ON),
    ]
    events = request_events(OK, OK_RENTAL, two_nodes, VISITOR, Occupancy.RENTAL, NAMES)
    rest = [e for e in events if e.type in ("command", "exec_result")]
    assert [(e.type, e.at, e.fields["nodeId"]) for e in rest] == [
        ("command", at(10), "locker-1"), ("exec_result", at(12), "locker-1"),
        ("command", at(20), "locker-2"), ("exec_result", at(20), "locker-2")]  # 결과 시각이 없으면 전송 시작 시각
    assert rest[2].fields["action"] is Action.ON
    # 전송을 시작하지 않은 명령은 command 가 없고, 결과가 있으면 요청 받은 시각으로 exec_result 를 낸다
    odd = command(1, ExecutionStatus.UNKNOWN, outcome="unknown")
    only = request_events(OK, OK_RENTAL, [odd], VISITOR, Occupancy.RENTAL, NAMES)[-2:]
    assert [(e.type, e.at) for e in only] == [("exec_result", T0), ("mcp_result", T0)]


def test_FR12B_뒤에_붙은_행은_앞_이벤트를_바꾸지_않는다():
    # 같은 요청 번호를 다른 내용으로 다시 보내면 기존 요청에 ⓪ 충돌 행이 뒤늦게 붙는다
    conflict = trail((Step.DEDUP, {"result": "REQUEST_ID_CONFLICT"}), start=7)
    exec_row = trail((Step.EXEC, {"event": "record_result", "outcome": "succeeded", "evidence": {}}), start=8)
    base = request_events(OK, OK_RENTAL, [DONE], VISITOR, Occupancy.RENTAL, NAMES)
    assert request_events(OK, [*OK_RENTAL, *conflict, *exec_row], [DONE], VISITOR, Occupancy.RENTAL, NAMES) == base


def test_FR12B_다시_판정한_요청의_이벤트는_마지막_시도의_행으로_만든다():
    # 근접 단계 뒤에서 오류로 끝난 요청을 다시 판정했더니 비콘 단계에서 거부됐다
    retry = (Step.DEDUP, {"event": "link_request_retry", "previous_verdict": "DENY",
                          "previous_reason_code": "INTERNAL_ERROR", "previous_node_id": None,
                          "previous_p_max": None, "previous_policy_ver": None})
    error = (Step.EXEC, {"result": "INTERNAL_ERROR", "error": "RuntimeError('x')"})
    rows = trail(BEACON, CANDIDATE, NEAR, error, retry, NO_BEACON, decided(Step.BEACON, "DENY", "BEACON_NOT_SEEN"))
    req = request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None)
    events = request_events(req, rows, [], VISITOR, None, NAMES)
    assert types(events) == ["mcp_call", "step", "step", "verdict", "mcp_result"]  # 앞 시도의 ③ 이 posterior 로 나오지 않는다
    assert [dict(e.fields) for e in events[1:3]] == [
        {"requestId": "i1", "step": Step.DEDUP, "result": "pass", "detail": "서버 오류로 끝난 요청을 다시 판정"},
        {"requestId": "i1", "step": Step.BEACON, "result": "stop", "detail": "비콘 수신 없음"},
    ]
    assert [e.at for e in events[1:4]] == [at(5), at(6), at(7)]  # 모두 마지막 시도의 행 시각이다


def test_FR12B_판정은_있는데_사유_코드가_없으면_오류다():
    with pytest.raises(ValueError):
        request_events(request(Verdict.OK, None), OK_RENTAL, [], VISITOR, Occupancy.RENTAL, NAMES)


def test_FR12B_행이_뒤섞여도_같은_이벤트를_만든다():
    ordered = request_events(OK, OK_RENTAL, [DONE], VISITOR, Occupancy.RENTAL, NAMES)
    assert request_events(OK, list(reversed(OK_RENTAL)), [DONE], VISITOR, Occupancy.RENTAL, NAMES) == ordered


def test_FR12B_도구_대상은_저장된_표기에서_만든다():
    explicit = request(Verdict.OK, ReasonCode.OK, target_spec="locker-2", action=Action.OFF)
    call = request_events(explicit, OK_RENTAL, [], VISITOR, Occupancy.RENTAL, NAMES)[0]
    assert dict(call.fields["target"]) == {"kind": TargetKind.EXPLICIT, "capability": Capability.LOCKER,
                                           "nodeId": "locker-2"}
    assert call.fields["action"] is Action.OFF


def test_FR12B_target_of():
    assert target_of("nearest:locker") == {"kind": TargetKind.NEAREST, "capability": Capability.LOCKER}
    assert target_of("any:locker") == {"kind": TargetKind.ANY, "capability": Capability.LOCKER}
    assert target_of("light-1") == {"kind": TargetKind.EXPLICIT, "capability": Capability.LIGHT, "nodeId": "light-1"}
    for raw in ("nearest:door", "somewhere:locker", "locker", "locker-x", ""):
        with pytest.raises(ValueError):
            target_of(raw)


# ── 기기 전이, 비콘, 승인 ──────────────────────────────────────────────


def node(device_state: DeviceState = DeviceState.ACTIVE, node_id: str = "locker-1") -> DashboardNode:
    return DashboardNode(
        node_id=node_id, name="사물함 1", place_id="p1", capability=Capability.LOCKER, occupancy=Occupancy.RENTAL,
        trust_state=TrustState.TRUSTED, device_state=device_state, last_seen_at=at(1), session_id="s1")


def test_FR12B_전이_이벤트는_있는_키만_넣는다():
    plain = transition_event(node(DeviceState.AVAILABLE), at(30), overdue=False, fault=None)
    assert plain.type == "transition" and plain.at == at(30) and plain.subject is None
    assert dict(plain.fields) == {"nodeId": "locker-1", "to": DeviceState.AVAILABLE}
    overdue = transition_event(node(), at(31), overdue=True, fault=None)
    assert dict(overdue.fields) == {"nodeId": "locker-1", "to": DeviceState.ACTIVE, "overdue": True}
    fault = transition_event(node(DeviceState.FAULT), at(32), overdue=False, fault=ReasonCode.EXEC_UNKNOWN)
    assert dict(fault.fields) == {"nodeId": "locker-1", "to": DeviceState.FAULT,
                                  "fault": {"cause": ReasonCode.EXEC_UNKNOWN}}
    both = transition_event(node(DeviceState.FAULT), at(33), overdue=True, fault=ReasonCode.TARGET_UNAVAILABLE)
    assert dict(both.fields) == {"nodeId": "locker-1", "to": DeviceState.FAULT, "overdue": True,
                                 "fault": {"cause": ReasonCode.TARGET_UNAVAILABLE}}


def test_FR12B_비콘_이벤트는_사람을_subject로_넘긴다():
    seen = beacon_event(VISITOR, True, at(5))
    assert (seen.type, seen.at, seen.subject) == ("beacon", at(5), VISITOR)
    assert dict(seen.fields) == {"seen": True}
    assert dict(beacon_event(VISITOR, False, at(6)).fields) == {"seen": False}


def approval(request_id: str = "i2", node_id: str = "locker-2") -> DashboardApproval:
    return DashboardApproval(
        approval_id="a1", node_id=node_id, requester_user_id=SECRET, internal_request_id=request_id,
        action=Action.OPEN, status=ApprovalStatus.PENDING, created_at=at(40), pending_until=at(100),
        valid_until=None, mode=ApprovalMode.ONE_TIME, used_count=0, remaining_uses=None)


def test_FR12B_승인_이벤트는_요청_기기_종류와_사람이다():
    for kind in ("requested", "approved", "denied", "expired"):
        event = approval_event(approval(), kind, VISITOR, at(41))
        assert (event.type, event.at, event.subject) == ("approval", at(41), VISITOR)
        assert dict(event.fields) == {"requestId": "i2", "nodeId": "locker-2", "kind": kind}


# ── 감사 행 ──────────────────────────────────────────────────────────


def test_FR12A_감사_행이_가리키는_기기():
    own = trail((Step.POLICY, {"node_id": "locker-2", "verdict": "OK", "reason": "OK", "policy_version": 1}))[0]
    plain = trail(BEACON)[0]
    by_command = DashboardAudit(seq=9, log_id="log-9", internal_request_id=None, command_id="c1", step=Step.EXEC,
                                payload={"event": "record_result", "outcome": "succeeded", "evidence": {}},
                                created_at=at(9))
    orphan = DashboardAudit(seq=10, log_id="log-10", internal_request_id=None, command_id=None, step=Step.ADMIN,
                            payload={"event": "register_account"}, created_at=at(10))
    requests = {"i1": request(Verdict.OK, ReasonCode.OK, "locker-1")}
    commands = {"c1": command(1, node_id="locker-2")}
    assert audit_node(own, requests, commands) == "locker-2"  # payload 가 먼저다
    assert audit_node(plain, requests, commands) == "locker-1"  # 요청의 기기
    assert audit_node(by_command, requests, commands) == "locker-2"  # 요청이 없으면 명령의 기기
    assert audit_node(orphan, requests, commands) is None
    # 요청에 기기가 없으면(거부) 명령으로, 명령도 없으면 None
    assert audit_node(plain, {"i1": request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None)}, commands) is None
    unknown = DashboardAudit(seq=11, log_id="log-11", internal_request_id="gone", command_id="c9", step=Step.EXEC,
                             payload={}, created_at=at(11))
    assert audit_node(unknown, requests, commands) is None


def test_FR12A_감사_행은_문구와_코드를_담는다():
    row = audit_row(trail(BEACON)[0], "locker-1", NAMES)
    assert row == {"id": "log-1", "at": at(1), "step": Step.BEACON, "nodeId": "locker-1", "text": "비콘 최근 수신",
                   "code": ReasonCode.OK}
    assert list(row) == ["id", "at", "step", "nodeId", "text", "code"]
    admin = DashboardAudit(seq=5, log_id="log-5", internal_request_id=None, command_id=None, step=Step.ADMIN,
                           payload={"event": "register_account", "user_id": SECRET, "role": "owner"},
                           created_at=at(5))
    row = audit_row(admin, None, NAMES)
    assert row == {"id": "log-5", "at": at(5), "step": Step.ADMIN, "text": "계정 등록", "code": ReasonCode.OK}
    assert "nodeId" not in row
    stop = trail(NO_BEACON, decided(Step.BEACON, "DENY", "BEACON_NOT_SEEN"))[1]
    assert audit_row(stop, None, NAMES)["code"] is ReasonCode.BEACON_NOT_SEEN


# ── 형식과 개인정보 ───────────────────────────────────────────────────


def every_event() -> list[MonitorEvent]:
    """조회 서비스와 따라 읽기가 만드는 이벤트 종류 전부."""
    req = request(Verdict.OK, ReasonCode.OK)
    events = request_events(req, OK_RENTAL, [DONE], VISITOR, Occupancy.RENTAL, NAMES)
    events += request_events(request(Verdict.OK, ReasonCode.OK), OK_SHARED, [DONE], VISITOR, Occupancy.SHARED, NAMES)
    events += request_events(request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None), DENY_BEACON, [], VISITOR, None,
                             NAMES)
    events += request_events(request(Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None), ASK_PROXIMITY, [], VISITOR,
                             Occupancy.RENTAL, NAMES)
    events += request_events(request(Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED, None),
                             trail(decided(Step.DEDUP, "DENY", "CAPACITY_EXCEEDED")), [], VISITOR, None, NAMES)
    events += request_events(
        request(Verdict.OK, ReasonCode.OK, target_spec="locker-2", action=Action.OFF), OK_RENTAL,
        [command(1, ExecutionStatus.REJECTED, started=10, outcome="rejected", occurred=12)], VISITOR,
        Occupancy.RENTAL, NAMES)
    events += [
        transition_event(node(DeviceState.AVAILABLE), at(30), overdue=False, fault=None),
        transition_event(node(), at(31), overdue=True, fault=None),
        transition_event(node(DeviceState.FAULT), at(32), overdue=False, fault=ReasonCode.EXEC_UNKNOWN),
        transition_event(node(DeviceState.FAULT), at(33), overdue=False, fault=ReasonCode.TARGET_UNAVAILABLE),
        beacon_event(VISITOR, True, at(5)),
        beacon_event(Subject(SECRET + "-owner", Role.OWNER), False, at(6)),
    ]
    events += [
        approval_event(approval(), kind, VISITOR, at(41)) for kind in ("requested", "approved", "denied", "expired")]
    requests = {"i1": req}
    commands = {"c1": command(1)}
    for a in [*OK_RENTAL, *DENY_BEACON,
              *trail((Step.EXEC, {"event": "record_result", "outcome": "unknown", "evidence": {}}),
                     (Step.FAULT, {"event": "isolate_device", "node_id": "locker-1",
                                   "evidence": {"source": "device_result", "state": "unknown"}}),
                     (Step.ADMIN, {"event": "register_account", "user_id": SECRET, "role": "owner"}),
                     start=20)]:
        row = audit_row(a, audit_node(a, requests, commands), NAMES)
        events.append(MonitorEvent("audit", a.created_at, {"row": row}))
    return events


def test_FR12B_이벤트는_형식_검사를_통과한다():
    names = PersonNames()
    events = every_event()
    assert {e.type for e in events} == {
        "mcp_call", "step", "posterior", "verdict", "command", "exec_result", "mcp_result", "transition", "beacon",
        "approval", "audit"}
    for event in events:
        rendered = render_event(event, names)  # 어긋나면 SchemaError
        assert rendered["type"] == event.type
        # 표에 없는 키는 검사가 버린다. 만든 키가 하나도 버려지지 않아야 한다
        assert set(event.fields) <= set(rendered), event.type


def test_FR11_이벤트에_사용자_ID가_없다():
    names = PersonNames()
    rendered = [render_event(e, names) for e in every_event()]
    text = json.dumps(rendered, ensure_ascii=False, default=str)
    assert SECRET not in text
    for event in every_event():
        assert SECRET not in repr(event)
        assert SECRET not in json.dumps(dict(event.fields), ensure_ascii=False, default=str)
    # 사람은 이름표로만 나간다
    assert all(r["personId"].startswith("p") for r in rendered if r["type"] in ("mcp_call", "beacon", "approval"))


def test_FR12B_직렬화한_이벤트의_값_종류():
    rendered = [render_event(e, PersonNames()) for e in every_event()]
    steps = [r["step"] for r in rendered if r["type"] == "step"]
    assert set(steps) <= {"0", "1", "2", "3", "4", "5", "6"}  # step 이벤트는 판정 단계 일곱 값만
    results = {r["result"] for r in rendered if r["type"] == "exec_result"}
    assert results == {"succeeded", "rejected"}  # 거절은 실패와 따로 나간다
    assert {r["result"] for r in rendered if r["type"] == "step"} == {"pass", "stop"}
