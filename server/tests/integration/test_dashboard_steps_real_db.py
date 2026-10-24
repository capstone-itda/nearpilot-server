"""FR-12A·FR-12B 실제 판정 흐름이 남긴 감사 행으로 요청 한 건의 단계와 이벤트를 만든다.

판정, 정책, 점유는 실제 DB 위의 실제 코드이고 근접 추정과 실행만 fake 다. 감사는 같은 연결의 `SqliteAuditLog` 로
저장하고 `SqliteDashboardReads` 로 읽는다. 실패하는 시나리오는 단계 복원이나 감사 payload 의 어긋남이다. 테스트를 고치지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from _world import NOW, OWNER, SETTINGS, VISITOR_A, VISITOR_B, FakeReadiness, World
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import render_event
from nearpilot.core.allocation import AllocationWindow
from nearpilot.core.dashboard import (
    audit_code,
    audit_node,
    audit_text,
    fault_cause,
    outcome,
    request_events,
    request_steps,
    request_tool,
    transition_event,
)
from nearpilot.core.decision.access import AccessRequestService
from nearpilot.core.decision.digest import input_digest
from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.core.policy.checker import RepositoryPolicyChecker
from nearpilot.db.audit import SqliteAuditLog
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.repositories.dashboard_reads import SqliteDashboardReads
from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.shared.enums import (
    AccessMode,
    Action,
    AdminWriteOutcome,
    ApprovalMode,
    ApprovalStatus,
    CommandOutcome,
    DeviceState,
    ExecutionStatus,
    ReasonCode,
    Role,
    Step,
    StoreOutcome,
    TrustState,
    Verdict,
    Visibility,
)
from nearpilot.shared.models import (
    AccountRegistration,
    ApprovalDecisionRequest,
    BeaconKey,
    BeaconRegistration,
    PlaceAssignment,
    PolicyRevision,
    Subject,
)

STEPS = [Step.DEDUP, Step.BEACON, Step.CANDIDATE, Step.PROXIMITY, Step.POLICY, Step.DEVICE_OCCUPANCY,
         Step.USER_OCCUPANCY]
ANY_SETTINGS = replace(SETTINGS, threshold_locker=0.4, threshold_explicit_locker=0.3)  # 명시 지정이 더 낮아야 한다


@pytest.fixture
def make_world():
    """판정 흐름의 감사를 같은 연결의 audit_log 에 저장하는 세계를 만든다. 설정은 저장소에도 닿는다."""
    worlds = []

    def make(posteriors=None, *, settings=SETTINGS, window=None):
        w = World(posteriors, settings=settings, window=window)
        w.repo = SqliteRepository(w.conn, settings)
        w.policy = RepositoryPolicyChecker(w.repo)
        audit = SqliteAuditLog(w.conn)
        w.pipeline = DecisionPipeline(w.repo, w.prox, w.policy, audit, settings, FakeReadiness(), window=window)
        w.access = AccessRequestService(w.repo, w.policy, audit, settings, lambda: NOW)
        w.reads = SqliteDashboardReads(w.conn)
        worlds.append(w)
        return w

    yield make
    for w in worlds:
        w.close()


def read(world, internal_request_id):
    """DB 에서 요청과 감사 행, 기기의 점유형을 읽는다."""
    req = world.reads.request(internal_request_id)
    audits = world.reads.audit_of([internal_request_id])
    nodes = {n.node_id: n for n in world.reads.nodes()}
    occupancy = nodes[req.node_id].occupancy if req.node_id in nodes else None
    return req, audits, occupancy


def cells(world, internal_request_id):
    req, audits, occupancy = read(world, internal_request_id)
    return [(s.result, s.detail) for s in request_steps(req, audits, occupancy, {})]


def events_of(world, internal_request_id):
    """저장된 요청·감사·명령에서 요청 하나의 이벤트를 만든다 (조회 서비스가 하는 일)."""
    req, audits, occupancy = read(world, internal_request_id)
    subject = Subject(req.user_id, world.reads.roles([req.user_id])[req.user_id])
    commands = world.reads.commands_of([internal_request_id])
    return request_events(req, audits, commands, subject, occupancy, {})


def add_shared_light(world):
    """공용 조명 light-1 (공개, 승인 불필요). World 는 사물함만 심는다."""
    c = world.conn
    c.execute(
        "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
        "exclusive_group, release_policy, last_seen_at) "
        "VALUES ('light-1', 'p1', 'light', 'shared', 'trusted', 'AVAILABLE', NULL, '{}', ?)",
        ((NOW - timedelta(seconds=1)).isoformat(),))
    c.execute("INSERT INTO access_policy (policy_id, node_id, current_version) VALUES ('pol-light-1', 'light-1', 1)")
    c.execute(
        "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
        "approval_ttl_sec, approval_mode, max_uses) VALUES ('pol-light-1', 1, 'public', 'open', ?, 3600, "
        "'one_time', 1)", (OWNER,))


# ── 시나리오: (세계, 마지막 판정) 을 돌려준다 ────────────────────────────


def ok_rental(make_world):
    w = make_world()
    return w, w.pipeline.decide(w.request(VISITOR_A))


def ask_proximity(make_world):
    w = make_world({"locker-1": 0.3})
    return w, w.pipeline.decide(w.request(VISITOR_A))


def nearest_ok(make_world):
    w = make_world({"locker-1": 0.9}, settings=ANY_SETTINGS)
    return w, w.pipeline.decide(w.request(VISITOR_A, "nearest:locker"))


def nearest_ask(make_world):
    w = make_world({"locker-1": 0.3}, settings=ANY_SETTINGS)
    return w, w.pipeline.decide(w.request(VISITOR_A, "nearest:locker"))


def busy_device(make_world):
    w = make_world()
    w.pipeline.decide(w.request(VISITOR_A))
    return w, w.pipeline.decide(w.request(VISITOR_B, request_id="r2"))


def busy_user(make_world):
    w = make_world({"locker-1": 0.5, "locker-4": 0.5})  # 후보 확률의 합은 1 이하다
    w.conn.execute("UPDATE node SET exclusive_group = 'g1' WHERE node_id IN ('locker-1', 'locker-4')")
    w.pipeline.decide(w.request(VISITOR_A))
    return w, w.pipeline.decide(w.request(VISITOR_A, "locker-4", "r2"))


def ok_shared(make_world):
    w = make_world({"light-1": 0.97})
    add_shared_light(w)
    return w, w.pipeline.decide(w.request(VISITOR_A, "light-1", action=Action.ON))


def capacity(make_world):
    w = make_world(settings=replace(SETTINGS, dedup_entry_limit=1))
    unfinished = w.request(VISITOR_A, request_id="unfinished")
    w.repo.link_request(unfinished, input_digest(unfinished))
    return w, w.pipeline.decide(w.request(VISITOR_B, request_id="new-use"))


def deny_beacon(make_world):
    w = make_world()
    w.conn.execute("UPDATE beacon SET is_active = 0 WHERE user_id = ?", (VISITOR_A,))
    return w, w.pipeline.decide(w.request(VISITOR_A))


def need_approval(make_world):
    w = make_world({"locker-2": 0.97})
    return w, w.pipeline.decide(w.request(VISITOR_A, "locker-2"))


def approval_exhausted(make_world):
    w = make_world({"locker-2": 0.97})
    w.access.request_access(VISITOR_A, "locker-2", Action.OPEN, "a1")
    approval_id = w.conn.execute("SELECT approval_id FROM approval").fetchone()[0]
    assert w.repo.decide_approval(ApprovalDecisionRequest(approval_id, OWNER, ApprovalStatus.APPROVED, NOW)).succeeded
    assert w.pipeline.decide(w.request(VISITOR_A, "locker-2", "u1")).verdict is Verdict.OK  # 한 번 쓴다
    return w, w.pipeline.decide(w.request(VISITOR_A, "locker-2", "u2"))


def any_ok(make_world):
    # locker-4 가 가장 가깝다. locker-2 는 승인이 필요해 배정에서 빠지고, ④ 의 마지막 행이 된다
    w = make_world({"locker-4": 0.5, "locker-2": 0.45}, settings=ANY_SETTINGS,
                   window=AllocationWindow(60, wait=lambda sec: None))
    return w, w.pipeline.decide(w.request(VISITOR_A, "any:locker"))


def any_busy(make_world):
    w = make_world({"locker-1": 0.9}, settings=ANY_SETTINGS, window=AllocationWindow(60, wait=lambda sec: None))
    w.pipeline.decide(w.request(VISITOR_B, "locker-1", "b1"))  # 허용 후보 하나뿐인 locker-1 을 먼저 점유한다
    return w, w.pipeline.decide(w.request(VISITOR_A, "any:locker"))


def any_lost(make_world):
    # 같은 구간의 다른 요청이 허용 기기를 가져가 배정을 받지 못한다 (배정 결과를 비워서 만든다)
    window = AllocationWindow(60, wait=lambda sec: None, allocate_fn=lambda requests: {})
    w = make_world({"locker-1": 0.9}, settings=ANY_SETTINGS, window=window)
    return w, w.pipeline.decide(w.request(VISITOR_A, "any:locker"))


P, S, K = "pass", "stop", "skip"
OPEN_STEPS = [(P, None), (P, "비콘 최근 수신"), (P, "후보 4대")]  # 사물함 넷이 후보다

# 시나리오 → (만드는 함수, 기대하는 일곱 칸의 (결과, 상세 문구))
SCENARIOS = {
    "OK": (ok_rental, [*OPEN_STEPS, (P, "근접 통과 0.97"), (P, "정책 통과"), (P, "비어 있음"),
                       (P, "다른 점유 없음")]),
    "ASK ③": (ask_proximity, [*OPEN_STEPS, (S, "근접 부족 0.30"), (K, None), (K, None), (K, None)]),
    "nearest OK": (nearest_ok, [*OPEN_STEPS, (P, "근접 통과 0.90"), (P, "정책 통과"), (P, "비어 있음"),
                                (P, "다른 점유 없음")]),
    "nearest ASK ③": (nearest_ask, [*OPEN_STEPS, (S, "근접 부족 0.30"), (K, None), (K, None), (K, None)]),
    "BUSY ⑤": (busy_device, [*OPEN_STEPS, (P, "근접 통과 0.97"), (P, "정책 통과"), (S, "다른 사람이 점유 중"),
                             (K, None)]),
    "USER_BUSY ⑥": (busy_user, [*OPEN_STEPS, (P, "근접 통과 0.50"), (P, "정책 통과"), (P, "비어 있음"),
                                (S, "이미 다른 기기를 쓰는 중")]),
    "공용 OK": (ok_shared, [(P, None), (P, "비콘 최근 수신"), (P, "후보 1대"), (P, "근접 통과 0.97"),
                            (P, "정책 통과"), (K, None), (K, None)]),
    "⓪ 한도 초과": (capacity, [(S, "서버가 바쁨"), (K, None), (K, None), (K, None), (K, None), (K, None),
                           (K, None)]),
    "① 비콘 없음": (deny_beacon, [(P, None), (S, "활성 비콘 없음"), (K, None), (K, None), (K, None), (K, None),
                              (K, None)]),
    "④ 승인 필요": (need_approval, [*OPEN_STEPS, (P, "근접 통과 0.97"), (S, "승인 필요"), (K, None), (K, None)]),
    "④ 승인 소진": (approval_exhausted, [*OPEN_STEPS, (P, "근접 통과 0.97"), (S, "사용 횟수를 다 씀"), (K, None),
                                     (K, None)]),
    "any: OK": (any_ok, [*OPEN_STEPS, (P, "근접 통과 0.50"), (P, "정책 통과"), (P, "비어 있음"),
                         (P, "다른 점유 없음")]),
    "any: 배정 못 받음 ⑤": (any_lost, [*OPEN_STEPS, (P, "근접 통과 0.90"), (P, "정책 통과"), (S, "배정 못 받음"),
                                  (K, None)]),
    "any: BUSY ⑤": (any_busy, [*OPEN_STEPS, (P, "근접 통과 0.90"), (P, "정책 통과"), (S, "다른 사람이 점유 중"),
                               (K, None)]),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_FR12A_요청_한_건의_단계는_판정_흐름과_같다(make_world, name):
    scenario, expected = SCENARIOS[name]
    world, decision = scenario(make_world)
    req, audits, occupancy = read(world, decision.internal_request_id)
    states = request_steps(req, audits, occupancy, {})
    assert [s.step for s in states] == STEPS
    assert [(s.result, s.detail) for s in states] == expected
    assert (req.verdict, req.reason_code) == (decision.verdict, decision.reason)
    assert request_tool(audits) == "request_use"
    # 문구가 있는 칸은 감사 행 시각이 있고, 없는 칸은 시각도 없다
    assert [(s.detail is None) for s in states] == [(s.at is None) for s in states]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_FR12A_실제_감사_행은_모두_문구와_코드를_가진다(make_world, name):
    world, decision = SCENARIOS[name][0](make_world)
    _, audits, _ = read(world, decision.internal_request_id)
    assert audits
    for a in audits:
        assert audit_text(a, {}), a
        assert isinstance(audit_code(a), ReasonCode)
        assert VISITOR_A not in audit_text(a, {}) and VISITOR_B not in audit_text(a, {})
    # 판정 행의 code 는 그 요청의 판정 사유다 (REST 감사 행과 verdict 이벤트가 같은 사유를 보인다)
    decisions = [a for a in audits if "verdict" in a.payload and "settings_version" in a.payload]
    assert [audit_code(a) for a in decisions][-1] is decision.reason


def test_FR12A_충돌_재전송은_기존_요청의_단계를_바꾸지_않는다(make_world):
    # 같은 request_id 에 다른 대상을 보내면 기존 요청에 ⓪ 충돌 행이 붙고 기존 판정은 그대로다
    w = make_world()
    first = w.pipeline.decide(w.request(VISITOR_A, "locker-1"))
    before = cells(w, first.internal_request_id)
    conflict = w.pipeline.decide(w.request(VISITOR_A, "locker-3"))
    assert (conflict.verdict, conflict.reason) == (Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
    assert conflict.internal_request_id == first.internal_request_id
    assert cells(w, first.internal_request_id) == before == SCENARIOS["OK"][1]
    row = w.reads.audit_of([first.internal_request_id])[-1]
    assert (row.step, audit_code(row), audit_text(row, {})) == (
        Step.DEDUP, ReasonCode.REQUEST_ID_CONFLICT, "같은 요청 번호에 다른 내용")
    req, audits, occupancy = read(w, first.internal_request_id)
    assert (req.verdict, req.node_id) == (Verdict.OK, "locker-1")
    # 이벤트도 같다. 뒤에 붙은 충돌 행이 앞 이벤트를 바꾸지 않는다
    subject = Subject(VISITOR_A, w.reads.roles([VISITOR_A])[VISITOR_A])
    assert request_events(req, audits, [], subject, occupancy, {}) == request_events(
        req, audits[:-1], [], subject, occupancy, {})


def test_FR12A_판정_중인_요청의_재전송은_닿은_단계를_늘리지_않는다(make_world):
    w = make_world()
    req = w.request(VISITOR_A)
    iid = w.repo.link_request(req, input_digest(req)).internal_request_id  # 판정 전의 요청
    w.pipeline.decide(req)  # 판정 중인 요청의 재전송
    stored, audits, _ = read(w, iid)
    assert stored.verdict is None
    assert cells(w, iid) == [(P, "판정 중인 요청을 다시 보냄")] + [("pending", None)] * 6
    assert [audit_code(a) for a in audits] == [ReasonCode.COMMAND_IN_PROGRESS]


def test_FR12A_오류로_끝난_요청은_닿은_마지막_단계에서_멈추고_다시_판정하면_마지막_판정을_보인다(make_world):
    w = make_world()

    def boom(*args, **kwargs):
        raise RuntimeError("일시 장애")

    w.prox.posterior = boom  # ③ 근접 추정이 실패한다
    failed = w.pipeline.decide(w.request(VISITOR_A))
    assert (failed.verdict, failed.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    states = cells(w, failed.internal_request_id)
    assert [result for result, _ in states] == [P, P, S, K, K, K, K]  # 판정 행이 없어 닿은 ② 에서 멈춘 것으로 본다
    _, audits, _ = read(w, failed.internal_request_id)
    error = audits[-1]
    assert (error.step, audit_code(error), audit_text(error, {})) == (Step.EXEC, ReasonCode.INTERNAL_ERROR, "서버 오류")
    assert "일시 장애" not in audit_text(error, {})  # payload 의 repr(exc) 를 옮기지 않는다

    del w.prox.posterior
    again = w.pipeline.decide(w.request(VISITOR_A))
    assert (again.verdict, again.internal_request_id) == (Verdict.OK, failed.internal_request_id)
    assert cells(w, again.internal_request_id) == [
        (P, "서버 오류로 끝난 요청을 다시 판정"), *SCENARIOS["OK"][1][1:]]


def test_FR12A_오류_뒤_다시_판정해_앞_단계에서_거부되면_거기서_멈춘다(make_world):
    w = make_world()

    def boom(*args, **kwargs):
        raise RuntimeError("일시 장애")

    w.policy.check = boom  # ④ 정책 검사가 실패한다
    failed = w.pipeline.decide(w.request(VISITOR_A))  # 앞 시도의 ①②③ 행이 남는다
    del w.policy.check
    w.conn.execute("UPDATE beacon SET is_active = 0 WHERE user_id = ?", (VISITOR_A,))
    again = w.pipeline.decide(w.request(VISITOR_A))
    assert (again.verdict, again.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert again.internal_request_id == failed.internal_request_id
    assert cells(w, again.internal_request_id) == [
        (P, "서버 오류로 끝난 요청을 다시 판정"), (S, "활성 비콘 없음"), (K, None), (K, None), (K, None), (K, None),
        (K, None)]
    types = [e.type for e in events_of(w, again.internal_request_id)]
    assert types == ["mcp_call", "step", "step", "verdict", "mcp_result"]  # 앞 시도의 ③ 이 posterior 로 나오지 않는다


def test_FR12A_접근_신청의_감사는_정책_행만이라_request_access로_가려진다(make_world):
    w = make_world({"locker-2": 0.97})
    d = w.access.request_access(VISITOR_A, "locker-2", Action.OPEN, "a1")
    req, audits, occupancy = read(w, d.internal_request_id)
    assert audits and {a.step for a in audits} == {Step.POLICY}
    assert request_tool(audits) == "request_access"
    subject = Subject(VISITOR_A, w.reads.roles([VISITOR_A])[VISITOR_A])
    assert request_events(req, audits, [], subject, occupancy, {}) == []  # 접근 신청은 도구 호출로 발행하지 않는다


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_FR12B_실제_판정_흐름의_이벤트는_형식_검사를_통과한다(make_world, name):
    world, decision = SCENARIOS[name][0](make_world)
    req, audits, occupancy = read(world, decision.internal_request_id)
    subject = Subject(req.user_id, world.reads.roles([req.user_id])[req.user_id])
    commands = world.reads.commands_of([req.internal_request_id])
    events = request_events(req, audits, commands, subject, occupancy, {})
    states = request_steps(req, audits, occupancy, {})
    assert events[0].type == "mcp_call" and events[0].subject == subject
    assert sum(e.type == "step" for e in events) == sum(s.result in (P, S) for s in states)
    verdict = next(e for e in events if e.type == "verdict")
    assert (verdict.fields["verdict"], verdict.fields["reason"]) == (decision.verdict, decision.reason)
    assert events[-1].type == ("mcp_result" if decision.verdict is not Verdict.OK else "verdict")  # 실행 전의 OK
    names = PersonNames()
    rendered = [render_event(e, names) for e in events]  # 어긋나면 SchemaError
    text = json.dumps(rendered, ensure_ascii=False)
    assert VISITOR_A not in text and VISITOR_B not in text and OWNER not in text


# ── 실행 저장소와 관리 저장소가 남기는 행 ─────────────────────────────────


def started_command(world, decision):
    """OK 판정에 명령을 연결하고 보내기 시작한다 (실제 ExecutionStore)."""
    store = SqliteExecutionStore(world.conn)
    command = store.link_command(decision.internal_request_id, decision.node_id, Action.OPEN).command
    assert store.begin_send(command.command_id).outcome is StoreOutcome.APPLIED
    return store, command


DEVICE_RESULT = {"source": "device_result", "event_id": "e1", "session_id": "s1", "error_code": None, "detail": {}}


def test_FR12B_실제_명령_진행에_따라_이벤트가_앞에서부터_늘어난다(make_world):
    w, decision = ok_rental(make_world)
    iid = decision.internal_request_id
    store = SqliteExecutionStore(w.conn)
    stages = [events_of(w, iid)]  # 판정 저장
    command = store.link_command(iid, "locker-1", Action.OPEN).command
    stages.append(events_of(w, iid))  # 명령 연결 (보내기 전)
    assert store.begin_send(command.command_id).outcome is StoreOutcome.APPLIED
    stages.append(events_of(w, iid))  # 보내기 시작
    saved = store.record_result(
        command.command_id, CommandOutcome.SUCCEEDED, NOW, NOW + timedelta(hours=1),
        {**DEVICE_RESULT, "state": "succeeded"})
    assert saved.outcome is StoreOutcome.APPLIED
    stages.append(events_of(w, iid))  # 결과 저장
    for earlier, later in zip(stages, stages[1:]):
        assert later[: len(earlier)] == earlier
    assert [len(events) for events in stages] == [10, 10, 11, 13]
    assert [e.type for e in stages[-1]] == [
        "mcp_call", *["step"] * 7, "posterior", "verdict", "command", "exec_result", "mcp_result"]
    req, audits, _ = read(w, iid)
    commands = w.reads.commands_of([iid])
    assert outcome(req, commands) == ("열림", None)
    row = audits[-1]
    assert (row.step, audit_text(row, {}), audit_code(row)) == (Step.EXEC, "실행 완료", ReasonCode.OK)
    rendered = [render_event(e, PersonNames()) for e in stages[-1]]
    assert VISITOR_A not in json.dumps(rendered, ensure_ascii=False)


def test_FR12B_결과_불명은_고장_격리로_보인다(make_world):
    w, decision = ok_rental(make_world)
    iid = decision.internal_request_id
    store, command = started_command(w, decision)
    evidence = {**DEVICE_RESULT, "state": "unknown"}
    assert store.record_result(command.command_id, CommandOutcome.UNKNOWN, NOW, None, evidence).outcome \
        is StoreOutcome.APPLIED
    req, audits, _ = read(w, iid)
    commands = w.reads.commands_of([iid])
    assert outcome(req, commands) == ("결과 불명 → 고장 격리", ReasonCode.EXEC_UNKNOWN)
    events = events_of(w, iid)
    assert [e.fields["result"] for e in events if e.type == "exec_result"] == ["unknown"]
    assert events[-1].type == "mcp_result"
    row = audits[-1]
    assert (row.step, audit_text(row, {}), audit_code(row)) == (Step.EXEC, "결과 불명 → 고장 격리", ReasonCode.EXEC_UNKNOWN)
    node = next(n for n in w.reads.nodes() if n.node_id == "locker-1")
    assert node.device_state is DeviceState.FAULT
    cause = fault_cause(commands[0].evidence)  # 기기가 보고한 결과 불명이 근거다
    assert cause is ReasonCode.EXEC_UNKNOWN
    transition = render_event(transition_event(node, NOW, overdue=False, fault=cause), PersonNames())
    assert transition["fault"] == {"cause": "EXEC_UNKNOWN"} and transition["to"] == "FAULT"


def test_FR12B_기기가_거절한_명령은_실패로_보인다(make_world):
    w, decision = ok_rental(make_world)
    iid = decision.internal_request_id
    store, command = started_command(w, decision)
    evidence = {**DEVICE_RESULT, "state": "rejected", "error_code": "DEVICE_BUSY"}
    assert store.record_result(command.command_id, CommandOutcome.REJECTED, NOW, None, evidence).outcome \
        is StoreOutcome.APPLIED
    req, audits, _ = read(w, iid)
    commands = w.reads.commands_of([iid])
    assert outcome(req, commands) == ("실행 안 함", ReasonCode.EXEC_FAILED)
    assert [e.fields["result"] for e in events_of(w, iid) if e.type == "exec_result"] == ["failed"]
    row = audits[-1]
    assert (audit_text(row, {}), audit_code(row)) == ("기기가 실행을 거절", ReasonCode.EXEC_FAILED)
    assert fault_cause(commands[0].evidence) is ReasonCode.EXEC_FAILED  # 거절은 결과 불명이 아니다


def test_FR12B_보내지_않고_취소한_명령은_이벤트가_없고_취소_사유가_보인다(make_world):
    w, decision = ok_rental(make_world)
    iid = decision.internal_request_id
    store = SqliteExecutionStore(w.conn)
    store.link_command(iid, "locker-1", Action.OPEN)
    evidence = {"source": "node_not_connected", "reason": "NODE_NOT_CONNECTED", "confirmed_at": NOW}
    saved = store.cancel_not_sent(iid, ExecutionStatus.PENDING, ReasonCode.NODE_NOT_CONNECTED, evidence)
    assert saved.outcome is StoreOutcome.APPLIED
    req, audits, _ = read(w, iid)
    commands = w.reads.commands_of([iid])
    assert (commands[0].status, commands[0].outcome) == (ExecutionStatus.NOT_SENT, "not_sent")
    types = [e.type for e in events_of(w, iid)]
    assert "command" not in types and "exec_result" not in types and types[-2:] == ["verdict", "mcp_result"]
    assert outcome(req, commands) == ("실행 안 함", ReasonCode.NODE_NOT_CONNECTED)
    row = audits[-1]
    assert (audit_text(row, {}), audit_code(row)) == ("보내지 않고 취소 · 기기 연결 없음", ReasonCode.NODE_NOT_CONNECTED)


def test_FR12A_격리_기록은_기기를_가리키고_문구에_근거가_없다(make_world):
    w = make_world()
    store = SqliteExecutionStore(w.conn)
    evidence = {"source": "sensor_fault", "session_id": "s1", "seq": 3, "user_id": "user-secret"}
    assert store.isolate_device("locker-1", evidence).outcome is StoreOutcome.APPLIED
    row = w.reads.audit_recent(1)[0]
    assert (row.step, row.internal_request_id) == (Step.FAULT, None)
    assert (audit_text(row, {"locker-1": "사물함 1"}), audit_code(row)) == ("고장으로 격리", ReasonCode.OK)
    assert audit_node(row, {}, {}) == "locker-1"  # 요청이 없어도 payload 의 기기를 가리킨다
    assert fault_cause(row.payload["evidence"]) is ReasonCode.TARGET_UNAVAILABLE
    assert "user-secret" not in audit_text(row, {})


def test_FR11_실제_관리_감사_행의_문구에_사용자_ID와_이름이_없다(make_world):
    w = make_world()
    admin = SqliteAdminRepository(w.conn)
    secret = "user-secret-7f3a"
    revision = PolicyRevision(
        policy_id="pol-locker-1", version=2, node_id="locker-1", visibility=Visibility.PUBLIC, mode=AccessMode.OPEN,
        approver_user_id=OWNER, approval_mode=ApprovalMode.ONE_TIME, approval_ttl_sec=3600, max_uses=1)
    writes = [
        ("계정 등록", admin.register_account(AccountRegistration(secret, secret, Role.VISITOR, "hash-secret"))),
        ("비콘 등록", admin.register_beacon(BeaconRegistration("b-secret", secret, BeaconKey("UUID", 9, 9)))),
        ("기기 신뢰 변경", admin.set_node_trust("locker-1", TrustState.TRUSTED)),
        ("장소 지정", admin.assign_place(PlaceAssignment("p-secret", secret, secret, ("locker-1",)))),
        ("접근 정책 변경", admin.set_policy(revision, 1)),
        ("반납 정책 변경", admin.set_release_policy("locker-1", {"holder": secret})),
        ("기기 이름 변경", admin.set_node_name("locker-1", secret)),
        ("수집 시작", admin.start_collection("run-1", BeaconKey("UUID", 9, 9), "none", NOW)),
        ("수집 종료", admin.end_collection("run-1", NOW + timedelta(minutes=1))),
    ]
    assert all(result.outcome is AdminWriteOutcome.APPLIED for _, result in writes)
    rows = [a for a in reversed(w.reads.audit_recent(100)) if a.step is Step.ADMIN]
    assert [audit_text(a, {}) for a in rows] == [text for text, _ in writes]
    assert {audit_code(a) for a in rows} == {ReasonCode.OK}
    # 이 payload 에는 사용자 ID 와 계정 이름이 실제로 들어 있다. 문구에는 나오지 않는다
    assert sum(secret in json.dumps(dict(a.payload), ensure_ascii=False) for a in rows) >= 4
    assert all(secret not in audit_text(a, {}) for a in rows)
