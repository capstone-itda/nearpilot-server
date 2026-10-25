"""FR-12B 모니터링 따라 읽기. DB 대신 행 목록을 돌려주는 fake `DashboardReadStore`, 이벤트를 모으는 fake feed, 고정
시각을 쓴다. 손으로 만든 감사 행은 pipeline.py·execution_store.py 가 실제로 쓰는 모양이다."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import render_event
from nearpilot.core.dashboard import MonitorTail
from nearpilot.shared.config import Settings
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
    TrustState,
    Verdict,
)
from nearpilot.shared.models import (
    DashboardApproval,
    DashboardAudit,
    DashboardBeacon,
    DashboardCommand,
    DashboardNode,
    DashboardRequest,
    DashboardRssi,
    DashboardSession,
    Subject,
)

T0 = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)  # 따라 읽기를 시작하는 시각
VISITOR_ID, VISITOR2_ID, OWNER_ID = "user-visitor-1", "user-visitor-2", "user-owner-1"  # 어떤 이벤트에도 나오면 안 된다
VISITOR = Subject(VISITOR_ID, Role.VISITOR)
VISITOR2 = Subject(VISITOR2_ID, Role.VISITOR)
OWNER = Subject(OWNER_ID, Role.OWNER)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ── 행 만들기 ───────────────────────────────────────────────────────


def make_node(
    node_id: str = "locker-1", *, state: DeviceState = DeviceState.AVAILABLE, occupancy: Occupancy = Occupancy.RENTAL,
    capability: Capability = Capability.LOCKER, name: str | None = None,
) -> DashboardNode:
    return DashboardNode(node_id, name, "place-1", capability, occupancy, TrustState.TRUSTED, state, at(-1), "conn-1")


def make_session(
    use_id: str = "use-1", *, node_id: str = "locker-1", user_id: str = VISITOR_ID,
    state: SessionState = SessionState.ACTIVE, due: datetime | None = None,
) -> DashboardSession:
    return DashboardSession(use_id, f"req-{use_id}", user_id, node_id, state, at(-100), due, at(-110))


def make_approval(
    approval_id: str, *, status: ApprovalStatus = ApprovalStatus.PENDING, created: datetime = T0,
    pending_until: datetime | None = at(600), valid_until: datetime | None = None, node_id: str = "locker-2",
    user_id: str = VISITOR_ID,
) -> DashboardApproval:
    return DashboardApproval(approval_id, node_id, user_id, f"req-{approval_id}", Action.OPEN, status, created,
                             pending_until, valid_until, ApprovalMode.ONE_TIME, 0, None)


def make_request(
    rid: str, seq: int, *, kind: str = "visitor", user_id: str = VISITOR_ID, verdict: Verdict | None = Verdict.OK,
    reason: ReasonCode | None = ReasonCode.OK, node_id: str | None = "locker-1", action: Action = Action.OPEN,
    received: datetime = T0,
) -> DashboardRequest:
    if kind == "server_internal":  # 서버 내부 요청은 사용자도 판정도 저장하지 않고 target_spec 에 기기 ID 를 둔다
        return DashboardRequest(seq, rid, None, kind, node_id or "", action, None, None, node_id, None, received)
    return DashboardRequest(seq, rid, user_id, kind, "nearest:locker", action, verdict, reason, node_id, None, received)


def make_command(
    rid: str, seq: int = 1, *, status: ExecutionStatus = ExecutionStatus.PENDING, node_id: str = "locker-1",
    action: Action = Action.OPEN, started: datetime | None = None, outcome: str | None = None,
    occurred: datetime | None = None,
) -> DashboardCommand:
    return DashboardCommand(seq, f"cmd-{rid}", rid, node_id, action, status, None, started, outcome, occurred, {})


def make_rssi(seq: int, user_id: str, node_id: str, rssi: float, received: datetime) -> DashboardRssi:
    return DashboardRssi(seq, node_id, user_id, rssi, received)


def trail(rid: str, received: datetime, *steps: tuple[Step, dict], seq0: int = 1) -> list[DashboardAudit]:
    """감사 행을 seq0 부터 차례로 붙인다. 행 시각은 요청을 받은 시각 뒤 1초 간격이고 log_id 는 `log-{rid}-{i}` 다."""
    return [
        DashboardAudit(seq0 + i, f"log-{rid}-{i}", rid, None, step, payload, received + timedelta(seconds=i + 1))
        for i, (step, payload) in enumerate(steps)
    ]


def decided(step: Step, verdict: str, reason: str) -> tuple[Step, dict]:
    return step, {"verdict": verdict, "reason": reason, "settings_version": "v1"}


def edit(rows: list, match, **changes) -> None:
    """조건에 맞는 행을 바꾼 새 행으로 갈아 끼운다 (행은 고칠 수 없는 값이다)."""
    rows[:] = [replace(row, **changes) if match(row) else row for row in rows]


# 판정 흐름이 남기는 감사 행 (pipeline.py 의 _audit_step 과 같은 모양)
BEACON = (Step.BEACON, {"beacons": 1, "frames": 3, "anchors": ["locker-1"], "beacon_recent_sec": 10})
NO_BEACON = (Step.BEACON, {"beacons": 1, "frames": 0, "anchors": [], "beacon_recent_sec": 10})
CANDIDATE = (Step.CANDIDATE, {"place_id": "p1", "candidates": ["locker-1", "locker-2"], "online_ttl_sec": 10})
NEAR = (Step.PROXIMITY, {"posteriors": {"locker-1": 0.91, "locker-2": 0.05}, "threshold": 0.5,
                         "model_version": "m1", "calib_version": "c1", "best": ["locker-1", 0.91]})
POLICY = (Step.POLICY, {"node_id": "locker-1", "verdict": "OK", "reason": "OK", "policy_version": 1})
RESERVED = (Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "reserved"})
OK_RENTAL = (BEACON, CANDIDATE, NEAR, POLICY, RESERVED, decided(Step.USER_OCCUPANCY, "OK", "OK"))
DENY_BEACON = (NO_BEACON, decided(Step.BEACON, "DENY", "BEACON_NOT_SEEN"))
ACCESS_FLOW = (
    (Step.POLICY, {"node_id": "locker-2", "verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED",
                   "policy_version": 1}),
)


# ── fake 저장소와 feed ──────────────────────────────────────────────


class FakeReads:
    """`DashboardReadStore` fake. 행 목록을 돌려주고 읽기 트랜잭션을 연 횟수와 요청을 찾은 ID 를 센다.

    `fail(메서드)` 로 그 메서드가 던지게 한다 (`"*"` 은 열기를 포함해 모든 메서드). 따라 읽기가 쓰는 메서드만 갖는다.
    """

    def __init__(
        self, *, nodes=(), sessions=(), approvals=(), requests=(), commands=(), audits=(), observations=(),
        beacons=(), evidence=None,
    ) -> None:
        self.node_rows, self.session_rows, self.approval_rows = list(nodes), list(sessions), list(approvals)
        self.request_rows, self.command_rows, self.audit_rows = list(requests), list(commands), list(audits)
        self.rssi_rows, self.beacon_rows = list(observations), list(beacons)
        self.evidence = dict(evidence or {})
        self.roles_of = {VISITOR_ID: Role.VISITOR, VISITOR2_ID: Role.VISITOR, OWNER_ID: Role.OWNER}
        self.failing: dict[str, Exception] = {}
        self.opened = 0
        self.inside = False
        self.lookups: list[set[str]] = []  # `requests_by_ids` 로 찾은 ID 모음 (주기마다 하나씩이 아니다)

    def fail(self, method: str = "*") -> None:
        self.failing[method] = RuntimeError(f"읽기 실패 {VISITOR_ID}")  # 메시지에 사용자 ID 가 있어도 로그에는 안 나온다

    def _read(self, method: str) -> None:
        error = self.failing.get(method) or self.failing.get("*")
        if error is not None:
            raise error

    @contextmanager
    def open(self):
        self._read("open")
        self.opened += 1
        self.inside = True
        try:
            yield self
        finally:
            self.inside = False

    def cursors(self):
        self._read("cursors")
        return {
            "request": max((r.seq for r in self.request_rows), default=0),
            "audit_log": max((a.seq for a in self.audit_rows), default=0),
            "rssi_observation": max((o.seq for o in self.rssi_rows), default=0),
        }

    def nodes(self):
        self._read("nodes")
        return sorted(self.node_rows, key=lambda n: n.node_id)

    def roles(self, user_ids):
        self._read("roles")
        return {u: self.roles_of[u] for u in user_ids if u in self.roles_of}

    def open_sessions(self):
        self._read("open_sessions")
        return sorted(self.session_rows, key=lambda s: (s.node_id, s.use_id))

    def approvals(self):
        self._read("approvals")
        return sorted(self.approval_rows, key=lambda a: (a.created_at, a.approval_id))

    def requests_after(self, seq):
        self._read("requests_after")
        return sorted((r for r in self.request_rows if r.seq > seq), key=lambda r: r.seq)

    def requests_by_ids(self, ids):
        self._read("requests_by_ids")
        wanted = set(ids)
        self.lookups.append(wanted)
        return sorted((r for r in self.request_rows if r.internal_request_id in wanted), key=lambda r: r.seq)

    def commands_of(self, internal_request_ids):
        self._read("commands_of")
        wanted = set(internal_request_ids)
        return sorted((c for c in self.command_rows if c.internal_request_id in wanted), key=lambda c: c.seq)

    def commands_by_ids(self, command_ids):
        self._read("commands_by_ids")
        wanted = set(command_ids)
        return sorted((c for c in self.command_rows if c.command_id in wanted), key=lambda c: c.seq)

    def audit_after(self, seq):
        self._read("audit_after")
        return sorted((a for a in self.audit_rows if a.seq > seq), key=lambda a: a.seq)

    def audit_of(self, internal_request_ids):
        self._read("audit_of")
        wanted = set(internal_request_ids)
        return sorted((a for a in self.audit_rows if a.internal_request_id in wanted), key=lambda a: a.seq)

    def fault_evidence(self, node_ids):
        self._read("fault_evidence")
        return {node_id: self.evidence[node_id] for node_id in node_ids if node_id in self.evidence}

    def rssi_after(self, seq):
        self._read("rssi_after")
        return sorted((o for o in self.rssi_rows if o.seq > seq), key=lambda o: o.seq)

    def active_beacons(self):
        self._read("active_beacons")
        return sorted(self.beacon_rows, key=lambda b: b.user_id)


class Feed:
    """`MonitorFeed` fake. 낸 이벤트를 모으고, 읽기 트랜잭션이 열린 채 낸 일이 있으면 기록한다."""

    def __init__(self, reads: FakeReads) -> None:
        self.events: list = []
        self.published_while_reading = False
        self._reads = reads

    def publish(self, event) -> None:
        self.published_while_reading = self.published_while_reading or self._reads.inside
        self.events.append(event)

    def take(self) -> list:
        """지금까지 모은 이벤트를 꺼내고 비운다."""
        taken, self.events = self.events, []
        return taken


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_tail(reads: FakeReads, *, settings: Settings | None = None) -> tuple[MonitorTail, Feed, Clock]:
    feed, clock = Feed(reads), Clock(T0)
    tail = MonitorTail(open_reads=reads.open, feed=feed, settings=lambda: settings or Settings(), clock=clock)
    return tail, feed, clock


def kinds(events) -> list[str]:
    return [event.type for event in events]


def of(events, kind: str) -> list:
    return [event for event in events if event.type == kind]


def add_flow(reads: FakeReads, rid: str, seq: int, *steps, received: datetime = T0, **request) -> None:
    """요청 하나와 그 감사 행을 붙인다. 감사 행의 rowid 는 지금 있는 행 다음부터다."""
    reads.request_rows.append(make_request(rid, seq, received=received, **request))
    reads.audit_rows += trail(rid, received, *steps, seq0=max((a.seq for a in reads.audit_rows), default=0) + 1)


# ── 시작 ─────────────────────────────────────────────────────────────


def test_FR12B_따라_읽기는_시작_뒤의_행만_발행한다():
    reads = FakeReads(
        nodes=[make_node("locker-1")], beacons=[DashboardBeacon(VISITOR_ID, "b1", at(-5))],
        observations=[make_rssi(1, VISITOR_ID, "locker-1", -60.0, at(-5))],
        approvals=[make_approval("a-old", created=at(-30), pending_until=at(570))],
    )
    add_flow(reads, "old", 1, *DENY_BEACON, received=at(-60), verdict=Verdict.DENY, reason=ReasonCode.BEACON_NOT_SEEN)
    tail, feed, _ = make_tail(reads)

    tail.start()
    tail.tick()

    assert feed.events == []  # 이미 있는 행은 지금 상태로 따로 보낸다. 따라 읽기는 내지 않는다

    add_flow(reads, "new", 2, *DENY_BEACON, received=at(1), verdict=Verdict.DENY, reason=ReasonCode.BEACON_NOT_SEEN)
    reads.rssi_rows.append(make_rssi(2, VISITOR_ID, "locker-1", -58.0, at(2)))
    tail.tick()

    assert kinds(feed.events) == ["mcp_call", "step", "step", "verdict", "mcp_result", "rssi", "audit", "audit"]
    assert {e.fields["requestId"] for e in feed.events if "requestId" in e.fields} == {"new"}
    assert [e.fields["row"]["id"] for e in of(feed.events, "audit")] == ["log-new-0", "log-new-1"]


def test_FR12B_start_전의_tick은_아무것도_하지_않는다():
    reads = FakeReads(nodes=[make_node("locker-1")])
    tail, feed, _ = make_tail(reads)

    tail.tick()

    assert (reads.opened, feed.events) == (0, [])


def test_FR12B_start가_실패하면_예외를_올리고_tick은_아무것도_하지_않는다():
    reads = FakeReads(nodes=[make_node("locker-1")])
    reads.fail("cursors")
    tail, feed, _ = make_tail(reads)

    with pytest.raises(RuntimeError):
        tail.start()
    tail.tick()

    assert (reads.opened, feed.events) == (1, [])  # 시작 때 한 번 열었고 tick 은 열지 않았다


# ── 요청 ─────────────────────────────────────────────────────────────


def test_FR12B_요청의_이벤트는_저장되는_대로_늘어나고_같은_이벤트를_두_번_내지_않는다():
    reads = FakeReads(nodes=[make_node("locker-1")])
    tail, feed, _ = make_tail(reads)
    tail.start()

    # 판정 중: 첫 감사 행이 나오면 도구 호출을 낸다. 단계는 판정이 저장된 뒤에 나온다
    add_flow(reads, "r1", 1, BEACON, verdict=None, reason=None)
    tail.tick()
    assert kinds(feed.take()) == ["mcp_call", "audit"]

    # 판정이 저장됐다. 명령은 아직 없다
    reads.audit_rows += trail("r1", T0, CANDIDATE, NEAR, POLICY, RESERVED, decided(Step.USER_OCCUPANCY, "OK", "OK"),
                              seq0=2)
    edit(reads.request_rows, lambda r: True, verdict=Verdict.OK, reason_code=ReasonCode.OK)
    tail.tick()
    taken = feed.take()
    assert kinds(taken) == ["step"] * 7 + ["posterior", "verdict"] + ["audit"] * 5
    assert [e.fields["step"] for e in of(taken, "step")] == [Step(str(i)) for i in range(7)]
    assert [e.fields["result"] for e in of(taken, "step")] == ["pass"] * 7

    tail.tick()
    assert feed.take() == []  # 바뀐 것이 없으면 아무것도 내지 않는다

    # 보내기 시작했다
    reads.command_rows.append(make_command("r1", status=ExecutionStatus.IN_PROGRESS, started=at(2)))
    tail.tick()
    assert [(e.type, e.fields["nodeId"], e.fields["action"]) for e in feed.take()] == [
        ("command", "locker-1", Action.OPEN)]

    # 결과가 저장됐다. 요청이 끝나서 추적을 마친다
    edit(reads.command_rows, lambda c: True, status=ExecutionStatus.SUCCEEDED, outcome="succeeded", occurred_at=at(3))
    tail.tick()
    assert [(e.type, e.fields.get("result")) for e in feed.take()] == [
        ("exec_result", "succeeded"), ("mcp_result", None)]

    reads.lookups.clear()
    tail.tick()
    assert feed.take() == []
    assert all("r1" not in ids for ids in reads.lookups)  # 끝난 요청은 더 읽지 않는다


def test_FR12B_서버_내부_요청은_도구_호출로_발행하지_않는다():
    light = make_node("light-1", state=DeviceState.ACTIVE, occupancy=Occupancy.SHARED, capability=Capability.LIGHT)
    reads = FakeReads(nodes=[light])
    tail, feed, _ = make_tail(reads)
    tail.start()

    reads.request_rows.append(make_request("off", 1, kind="server_internal", node_id="light-1", action=Action.OFF))
    tail.tick()
    assert feed.take() == []  # 요청 행이 먼저 저장되고 명령 행은 조금 뒤에 붙는다. 그동안 추적을 끝내지 않는다

    reads.command_rows.append(make_command("off", status=ExecutionStatus.IN_PROGRESS, node_id="light-1",
                                           action=Action.OFF, started=at(1)))
    tail.tick()
    assert [(e.type, e.fields["action"]) for e in feed.take()] == [("command", Action.OFF)]

    edit(reads.command_rows, lambda c: True, status=ExecutionStatus.SUCCEEDED, outcome="succeeded", occurred_at=at(2))
    tail.tick()
    assert [(e.type, e.fields["result"]) for e in feed.take()] == [("exec_result", "succeeded")]

    reads.lookups.clear()
    tail.tick()
    assert feed.take() == []
    assert all("off" not in ids for ids in reads.lookups)  # 명령이 끝나서 추적을 마쳤다


def test_FR12B_접근_신청_요청은_도구_호출로_발행하지_않는다():
    reads = FakeReads(nodes=[make_node("locker-2")])
    tail, feed, _ = make_tail(reads)
    tail.start()

    add_flow(reads, "ask", 1, *ACCESS_FLOW, node_id="locker-2", verdict=Verdict.NEED_APPROVAL,
             reason=ReasonCode.APPROVAL_REQUIRED)
    reads.approval_rows.append(make_approval("a1", node_id="locker-2"))
    tail.tick()

    # 요청의 도구는 첫 감사 행이 ④여서 request_access 다. 승인 신청과 감사는 발행한다
    assert kinds(feed.take()) == ["approval", "audit"]
    reads.lookups.clear()
    tail.tick()
    assert all("ask" not in ids for ids in reads.lookups)  # 판정이 저장돼서 추적을 마쳤다


def test_FR12B_거절된_명령은_거절로_발행한다():
    reads = FakeReads(nodes=[make_node("locker-1")])
    tail, feed, _ = make_tail(reads)
    tail.start()

    add_flow(reads, "busy", 1, *OK_RENTAL)
    reads.command_rows.append(make_command("busy", status=ExecutionStatus.REJECTED, started=at(2), outcome="rejected",
                                           occurred=at(3)))
    tail.tick()

    taken = feed.take()
    assert [e.type for e in taken if e.type in ("command", "exec_result", "mcp_result")] == [
        "command", "exec_result", "mcp_result"]
    assert of(taken, "exec_result")[0].fields["result"] == "rejected"


def test_FR12B_보내지_않은_명령은_발행하지_않는다():
    reads = FakeReads(nodes=[make_node("locker-1"), make_node("locker-2")])
    tail, feed, _ = make_tail(reads)
    tail.start()

    # 보내기를 시작한 뒤에 보내지 않은 것으로 바뀐다: 낸 command 는 거두지 않고 다시 내지도 않는다
    add_flow(reads, "late", 1, *OK_RENTAL)
    reads.command_rows.append(make_command("late", status=ExecutionStatus.IN_PROGRESS, started=at(2)))
    tail.tick()
    assert of(feed.take(), "command") != []

    edit(reads.command_rows, lambda c: True, status=ExecutionStatus.NOT_SENT, outcome="not_sent", occurred_at=at(3))
    tail.tick()
    taken = feed.take()
    assert [e.type for e in taken if e.type in ("command", "exec_result", "mcp_result")] == ["mcp_result"]

    # 처음 볼 때 이미 보내지 않은 것이면 command 도 exec_result 도 없다
    add_flow(reads, "never", 2, *OK_RENTAL, node_id="locker-2")
    reads.command_rows.append(make_command("never", 2, status=ExecutionStatus.NOT_SENT, node_id="locker-2",
                                           started=at(5), outcome="not_sent", occurred=at(6)))
    tail.tick()
    taken = [e for e in feed.take() if e.fields.get("requestId") == "never"]
    assert [e.type for e in taken if e.type in ("command", "exec_result", "mcp_result")] == ["mcp_result"]


# ── 실패 ─────────────────────────────────────────────────────────────


def add_everything(reads: FakeReads) -> None:
    """시작 뒤에 모든 종류의 행이 생긴다: 진행 중인 요청, 기기 전이, 승인, 관측, 비콘."""
    add_flow(reads, "r1", 1, *OK_RENTAL)
    reads.command_rows.append(make_command("r1", status=ExecutionStatus.IN_PROGRESS, started=at(2)))
    edit(reads.node_rows, lambda n: n.node_id == "locker-1", device_state=DeviceState.RESERVED)
    reads.approval_rows.append(make_approval("a1", created=at(1)))
    reads.rssi_rows.append(make_rssi(1, VISITOR_ID, "locker-1", -58.0, at(2)))
    edit(reads.beacon_rows, lambda b: b.user_id == VISITOR_ID, last_seen_at=at(1))


def test_NFR07_따라_읽기_실패는_다음_주기에_이어_간다(caplog):
    reads = FakeReads(nodes=[make_node("locker-1")], beacons=[DashboardBeacon(VISITOR_ID, "b1", at(-60))])
    tail, feed, _ = make_tail(reads)
    reference, reference_feed, _ = make_tail(reads)  # 한 번도 실패하지 않는 따라 읽기
    tail.start()
    reference.start()
    add_everything(reads)
    reference.tick()
    assert {"mcp_call", "transition", "approval", "rssi", "beacon", "audit"} <= set(kinds(reference_feed.events))

    for method in ("*", "nodes", "audit_after"):  # 열기부터 / 읽는 도중 / 가장 마지막에 읽는 것
        reads.fail(method)
        with caplog.at_level(logging.WARNING):
            tail.tick()  # 예외를 올리지 않는다
        reads.failing.clear()
        assert feed.events == []  # 읽는 도중에 실패한 주기에는 앞에서 읽은 것도 내지 않는다

    tail.tick()
    assert feed.take() == reference_feed.events  # 같은 행을 같은 순서로, 빠짐없이 낸다
    tail.tick()
    assert feed.take() == []  # 두 번 내지 않는다

    assert "따라 읽기 실패" in caplog.text
    assert VISITOR_ID not in caplog.text  # 예외 종류만 남긴다 (NFR-08)


def test_NFR07_형식이_틀린_요청은_그_요청만_버린다(caplog):
    reads = FakeReads(nodes=[make_node("locker-1")])
    tail, feed, _ = make_tail(reads)
    tail.start()
    add_flow(reads, "bad", 1, *DENY_BEACON, verdict=Verdict.DENY, reason=None)  # 판정은 있는데 사유 코드가 없다
    add_flow(reads, "r1", 2, *DENY_BEACON, verdict=Verdict.DENY, reason=ReasonCode.BEACON_NOT_SEEN)
    with caplog.at_level(logging.WARNING):
        tail.tick()

    calls = [e.fields["requestId"] for e in of(feed.take(), "mcp_call")]
    assert calls == ["r1"]  # 다른 요청과 감사 행은 그대로 나간다
    assert "형식이 틀린 요청" in caplog.text
    tail.tick()
    assert of(feed.take(), "mcp_call") == []  # 버린 요청을 주기마다 다시 시도하지 않는다


def test_NFR07_발행_예외가_주기를_멈추지_않는다(caplog):
    reads = FakeReads(nodes=[make_node("locker-1")])
    tail, feed, _ = make_tail(reads)
    tail.start()
    add_flow(reads, "r1", 1, *DENY_BEACON, verdict=Verdict.DENY, reason=ReasonCode.BEACON_NOT_SEEN)
    publish = feed.publish

    def flaky(event) -> None:
        if event.type == "step":
            raise RuntimeError(VISITOR_ID)
        publish(event)

    feed.publish = flaky
    with caplog.at_level(logging.ERROR):
        tail.tick()  # 예외를 올리지 않는다

    assert kinds(feed.events) == ["mcp_call", "verdict", "mcp_result", "audit", "audit"]  # step 만 빠진다
    assert VISITOR_ID not in caplog.text
    tail.tick()
    assert kinds(feed.events) == ["mcp_call", "verdict", "mcp_result", "audit", "audit"]  # 낸 것은 다시 내지 않는다


def test_FR12B_읽기_트랜잭션을_닫은_뒤에_이벤트를_낸다():
    reads = FakeReads(nodes=[make_node("locker-1")])
    tail, feed, _ = make_tail(reads)
    tail.start()
    add_everything(reads)
    opened = reads.opened

    tail.tick()

    assert feed.events != []
    assert reads.opened == opened + 1  # 주기마다 한 번만 연다
    assert not feed.published_while_reading


def test_FR12B_한_주기의_이벤트는_요청_기기_초과_승인_RSSI_비콘_감사_순으로_나간다():
    reads = FakeReads(
        nodes=[make_node("locker-1"), make_node("locker-2", state=DeviceState.ACTIVE)],
        sessions=[make_session("use-2", node_id="locker-2", due=at(60))],
        beacons=[DashboardBeacon(VISITOR_ID, "b1", at(-60))],
    )
    tail, feed, clock = make_tail(reads)
    tail.start()
    add_everything(reads)  # 요청, locker-1 의 예약, 승인 신청, 관측, 비콘 수신
    edit(reads.beacon_rows, lambda b: True, last_seen_at=at(60))
    clock.now = at(61)  # locker-2 의 대여 시간도 지났다
    tail.tick()

    taken = feed.take()
    assert kinds(taken) == (
        ["mcp_call"] + ["step"] * 7 + ["posterior", "verdict", "command"]  # 요청
        + ["transition", "transition"]  # 기기의 상태 전이, 대여 초과
        + ["approval", "rssi", "beacon"] + ["audit"] * 6
    )
    assert [(e.fields["nodeId"], e.fields.get("overdue")) for e in of(taken, "transition")] == [
        ("locker-1", None), ("locker-2", True)]


# ── 기기 ─────────────────────────────────────────────────────────────


def test_FR12B_상태가_같으면_전이를_발행하지_않는다():
    reads = FakeReads(nodes=[make_node("locker-1"), make_node("light-1", state=DeviceState.ACTIVE)])
    tail, feed, clock = make_tail(reads)
    tail.start()

    tail.tick()
    edit(reads.node_rows, lambda n: n.node_id == "locker-1", last_seen_at=at(5))  # 같은 상태가 다시 쓰였다
    tail.tick()
    assert feed.events == []

    clock.now = at(10)
    edit(reads.node_rows, lambda n: n.node_id == "locker-1", device_state=DeviceState.RESERVED)
    tail.tick()
    (event,) = feed.take()
    assert (event.type, event.at, dict(event.fields)) == ("transition", at(10), {
        "nodeId": "locker-1", "to": DeviceState.RESERVED})  # overdue 와 fault 키가 없다

    tail.tick()
    assert feed.take() == []

    reads.node_rows.append(make_node("locker-2"))  # 새로 생긴 기기도 낸다
    tail.tick()
    assert [(e.type, e.fields["nodeId"], e.fields["to"]) for e in feed.take()] == [
        ("transition", "locker-2", DeviceState.AVAILABLE)]


@pytest.mark.parametrize(("payload", "cause"), [
    ({"event": "record_result", "outcome": "unknown"}, ReasonCode.EXEC_UNKNOWN),
    ({"event": "record_result", "outcome": "failed"}, ReasonCode.EXEC_FAILED),
    ({"event": "isolate_device", "evidence": {"source": "sensor_fault"}}, ReasonCode.TARGET_UNAVAILABLE),
    (None, ReasonCode.TARGET_UNAVAILABLE),  # 근거 행이 없다
])
def test_FR12B_고장_전이는_고장_원인을_담는다(payload, cause):
    evidence = {}
    if payload is not None:
        evidence = {"locker-1": DashboardAudit(9, "log-9", None, None, Step.FAULT, payload, at(1))}
    reads = FakeReads(nodes=[make_node("locker-1", state=DeviceState.ACTIVE)], evidence=evidence)
    tail, feed, _ = make_tail(reads)
    tail.start()

    edit(reads.node_rows, lambda n: True, device_state=DeviceState.FAULT)
    tail.tick()

    (event,) = feed.take()
    assert event.fields["fault"] == {"cause": cause}
    assert render_event(event, PersonNames())["fault"] == {"cause": cause.value}


def test_FR12B_대여_시간을_넘기면_한_번_발행한다():
    reads = FakeReads(
        nodes=[make_node("locker-1", state=DeviceState.ACTIVE), make_node("locker-2", state=DeviceState.ACTIVE)],
        sessions=[make_session("use-1", due=at(60)), make_session("use-old", node_id="locker-2", due=at(-30))],
    )
    tail, feed, clock = make_tail(reads)
    tail.start()  # use-old 는 시작할 때 이미 지났다

    for seconds in (30, 60):  # 대여 시간 안 (정확히 만료 시각도 아직 넘기지 않았다)
        clock.now = at(seconds)
        tail.tick()
        assert feed.events == []

    clock.now = at(61)
    tail.tick()
    (event,) = feed.take()
    assert (event.type, event.at, dict(event.fields)) == ("transition", at(61), {
        "nodeId": "locker-1", "to": DeviceState.ACTIVE, "overdue": True})

    for seconds in (62, 120):
        clock.now = at(seconds)
        tail.tick()
    assert feed.events == []  # 세션마다 한 번이다

    # 같은 기기의 다음 대여 세션이 넘기면 그 세션도 한 번 낸다
    reads.session_rows[:] = [make_session("use-2", due=at(200))]
    clock.now = at(201)
    tail.tick()
    assert [(e.type, e.fields["nodeId"], e.fields["overdue"]) for e in feed.take()] == [
        ("transition", "locker-1", True)]


def test_FR12B_대여_시간을_넘기고_상태도_바뀌면_전이_하나에_초과를_담는다():
    reads = FakeReads(nodes=[make_node("locker-1", state=DeviceState.ACTIVE)], sessions=[make_session(due=at(60))])
    tail, feed, clock = make_tail(reads)
    tail.start()

    clock.now = at(61)
    edit(reads.node_rows, lambda n: True, device_state=DeviceState.FAULT)  # 고장은 세션을 바꾸지 않는다
    tail.tick()
    assert [(e.fields["to"], e.fields.get("overdue"), e.fields["fault"]) for e in feed.take()] == [
        (DeviceState.FAULT, True, {"cause": ReasonCode.TARGET_UNAVAILABLE})]

    tail.tick()
    assert feed.take() == []  # 초과를 따로 한 번 더 내지 않는다


def test_FR12B_이미_고장인_기기가_대여_시간을_넘기면_초과_전이에도_고장_원인을_담는다():
    evidence = {"locker-1": DashboardAudit(9, "log-9", None, None, Step.FAULT,
                                           {"event": "record_result", "outcome": "unknown"}, at(1))}
    reads = FakeReads(nodes=[make_node("locker-1", state=DeviceState.FAULT)], sessions=[make_session(due=at(60))],
                      evidence=evidence)
    tail, feed, clock = make_tail(reads)
    tail.start()

    clock.now = at(61)
    tail.tick()

    assert [(e.fields["to"], e.fields["overdue"], e.fields["fault"]) for e in feed.take()] == [
        (DeviceState.FAULT, True, {"cause": ReasonCode.EXEC_UNKNOWN})]


def test_FR12B_반납_중인_세션은_대여_초과로_세지_않는다():
    reads = FakeReads(nodes=[make_node("locker-1", state=DeviceState.ACTIVE)], sessions=[make_session(due=at(60))])
    tail, feed, clock = make_tail(reads)
    tail.start()

    clock.now = at(61)
    edit(reads.node_rows, lambda n: True, device_state=DeviceState.CLOSING)
    edit(reads.session_rows, lambda s: True, state=SessionState.CLOSING)
    tail.tick()

    assert [(e.fields["to"], e.fields.get("overdue")) for e in feed.take()] == [(DeviceState.CLOSING, None)]
    tail.tick()
    assert feed.take() == []


# ── 승인 ─────────────────────────────────────────────────────────────


def test_FR12B_승인_대기가_만료되면_한_번_발행한다():
    reads = FakeReads(approvals=[make_approval("a-old", created=at(-700), pending_until=at(-100), node_id="locker-1")])
    tail, feed, clock = make_tail(reads)
    tail.start()  # a-old 는 시작할 때 이미 만료됐다

    reads.approval_rows.append(make_approval("a1", created=at(10), pending_until=at(610)))
    clock.now = at(11)
    tail.tick()
    (event,) = feed.take()
    assert (event.type, event.at, event.subject, dict(event.fields)) == (
        "approval", at(10), VISITOR, {"requestId": "req-a1", "nodeId": "locker-2", "kind": "requested"})

    clock.now = at(609)
    tail.tick()
    assert feed.events == []

    clock.now = at(610)  # 대기 기한이다
    tail.tick()
    (event,) = feed.take()
    assert (event.type, event.at, event.subject, event.fields["kind"]) == ("approval", at(610), VISITOR, "expired")

    clock.now = at(700)
    tail.tick()
    assert feed.events == []  # 한 번이다


def test_FR12B_승인_결정은_바뀔_때_한_번_발행한다():
    reads = FakeReads(approvals=[make_approval("a1", node_id="locker-1"), make_approval("a2", node_id="locker-2")])
    tail, feed, clock = make_tail(reads)
    tail.start()
    tail.tick()
    assert feed.events == []

    clock.now = at(5)
    edit(reads.approval_rows, lambda a: a.approval_id == "a1", status=ApprovalStatus.APPROVED, valid_until=at(3600))
    edit(reads.approval_rows, lambda a: a.approval_id == "a2", status=ApprovalStatus.DENIED)
    tail.tick()
    taken = feed.take()
    assert [(e.fields["nodeId"], e.fields["kind"], e.at) for e in taken] == [
        ("locker-1", "approved", at(5)), ("locker-2", "denied", at(5))]
    assert {e.subject for e in taken} == {VISITOR}

    # 결정 뒤에 바뀌는 것(사용 횟수)과 대기 기한은 이벤트가 아니다
    clock.now = at(700)
    edit(reads.approval_rows, lambda a: a.approval_id == "a1", used_count=1)
    tail.tick()
    assert feed.take() == []

    # 처음 볼 때 이미 결정된 행은 신청 뒤에 결정도 낸다
    reads.approval_rows.append(make_approval("a3", status=ApprovalStatus.APPROVED, created=at(710),
                                             valid_until=at(4000)))
    tail.tick()
    assert [e.fields["kind"] for e in feed.take()] == ["requested", "approved"]


# ── 비콘과 RSSI ──────────────────────────────────────────────────────


def test_FR12B_비콘_수신이_바뀔_때만_발행한다():
    reads = FakeReads(beacons=[DashboardBeacon(VISITOR_ID, "b1", at(-1)), DashboardBeacon(OWNER_ID, "b2", None)])
    tail, feed, clock = make_tail(reads)  # beacon_recent_sec 기본값 3 초
    tail.start()

    clock.now = at(1)
    tail.tick()  # 마지막 관측이 2 초 전이라 아직 수신 중이다
    assert feed.events == []

    clock.now = at(10)
    tail.tick()
    (event,) = feed.take()
    assert (event.type, event.at, event.subject, dict(event.fields)) == ("beacon", at(-1), VISITOR, {"seen": False})
    tail.tick()
    assert feed.events == []

    edit(reads.beacon_rows, lambda b: b.user_id == VISITOR_ID, last_seen_at=at(9))
    tail.tick()
    (event,) = feed.take()
    assert (event.subject, dict(event.fields)) == (VISITOR, {"seen": True})

    reads.beacon_rows.append(DashboardBeacon(VISITOR2_ID, "b3", at(9.5)))  # 새로 생긴 활성 비콘의 사람도 낸다
    tail.tick()
    (event,) = feed.take()
    assert (event.subject, dict(event.fields)) == (VISITOR2, {"seen": True})

    reads.beacon_rows[:] = [b for b in reads.beacon_rows if b.user_id != OWNER_ID]  # 비활성이 된 사람은 내지 않는다
    tail.tick()
    assert feed.events == []


def test_FR12B_따라_읽기의_비콘은_최근_기준과_같은_수신까지_본_것이다():
    # 지금 상태(`current_events`)의 같은 경계는 test_dashboard_queries.py 의
    # test_FR12B_비콘은_최근_기준과_같은_수신까지_본_것이다 가 고정한다. 둘은 같은 규칙이다
    recent = Settings().beacon_recent_sec
    reads = FakeReads(beacons=[DashboardBeacon(VISITOR_ID, "b1", at(-60)), DashboardBeacon(VISITOR2_ID, "b2", at(-60))])
    tail, feed, _ = make_tail(reads)  # 시각은 T0 에 선다
    tail.start()  # 두 사람 모두 수신 중이 아니다

    edit(reads.beacon_rows, lambda b: b.user_id == VISITOR_ID, last_seen_at=at(-recent))  # 정확히 기준만큼 전이다
    edit(reads.beacon_rows, lambda b: b.user_id == VISITOR2_ID, last_seen_at=at(-recent - 0.001))  # 기준을 조금 넘었다
    tail.tick()

    assert [(e.subject, dict(e.fields)) for e in feed.take()] == [(VISITOR, {"seen": True})]  # 기준과 같으면 본 것이다


def test_FR12B_RSSI는_사람마다_기기별_마지막_값으로_묶는다():
    reads = FakeReads(observations=[make_rssi(1, VISITOR_ID, "locker-1", -40.0, at(-5))])
    tail, feed, _ = make_tail(reads)
    tail.start()

    reads.rssi_rows += [
        make_rssi(2, VISITOR_ID, "locker-1", -60.0, at(1)),
        make_rssi(3, VISITOR2_ID, "locker-1", -70.0, at(1.2)),
        make_rssi(4, VISITOR_ID, "locker-2", -80.0, at(2)),
        make_rssi(5, VISITOR_ID, "locker-1", -55.0, at(3)),  # 같은 기기는 마지막 값이다
        make_rssi(6, VISITOR2_ID, "locker-2", -65.0, at(2.5)),
    ]
    tail.tick()

    # 사람마다 하나이고, 마지막 관측이 이른 사람부터다. at 은 그 묶음의 마지막 관측 시각이다
    assert [(e.type, e.subject, e.at, dict(e.fields)) for e in feed.take()] == [
        ("rssi", VISITOR, at(3), {"rssi": {"locker-1": -55.0, "locker-2": -80.0}}),
        ("rssi", VISITOR2, at(2.5), {"rssi": {"locker-1": -70.0, "locker-2": -65.0}}),
    ]
    tail.tick()
    assert feed.events == []  # 이미 본 관측은 다시 묶지 않는다


# ── 감사와 사용자 ID ─────────────────────────────────────────────────


def test_FR12B_감사_행은_rowid_순으로_발행한다():
    reads = FakeReads(nodes=[make_node("locker-1", name="사물함 1"), make_node("locker-2")])
    tail, feed, _ = make_tail(reads)
    tail.start()

    reads.request_rows.append(make_request("r2", 2, node_id="locker-2"))
    reads.audit_rows += [
        DashboardAudit(7, "log-b", "r2", None, Step.POLICY, {"verdict": "OK", "reason": "OK"}, at(2)),
        DashboardAudit(8, "log-c", None, "cmd-x", Step.EXEC, {"event": "record_result", "outcome": "succeeded"},
                       at(3)),
        DashboardAudit(9, "log-d", None, None, Step.ADMIN, {"event": "set_node_name", "user_id": VISITOR_ID}, at(4)),
        DashboardAudit(5, "log-a", None, None, Step.ANNOUNCE, {"node_id": "locker-1"}, at(1)),
    ]
    reads.command_rows.append(make_command("r2", node_id="locker-2", status=ExecutionStatus.SUCCEEDED,
                                           outcome="succeeded"))
    edit(reads.command_rows, lambda c: True, command_id="cmd-x")
    tail.tick()

    audits = of(feed.take(), "audit")
    assert [e.fields["row"]["id"] for e in audits] == ["log-a", "log-b", "log-c", "log-d"]
    assert [e.at for e in audits] == [at(1), at(2), at(3), at(4)]
    # 기기는 payload 의 node_id, 요청의 node_id, 명령의 node_id 순으로 찾고 없으면 키를 뺀다
    assert [e.fields["row"].get("nodeId") for e in audits] == ["locker-1", "locker-2", "locker-2", None]
    assert VISITOR_ID not in repr(audits)  # 관리 기록의 payload 문자열은 옮기지 않는다

    tail.tick()
    assert feed.take() == []


def test_FR12B_기기_이름이_단계_문구와_감사_문구에_들어간다():
    reads = FakeReads(nodes=[make_node("locker-1", name="사물함 1")])
    tail, feed, _ = make_tail(reads)
    tail.start()

    allocated = (Step.DEVICE_OCCUPANCY, {"allocation": {"allowed": ["locker-1"], "assigned": "locker-1"}})
    add_flow(reads, "any", 1, BEACON, CANDIDATE, NEAR, POLICY, allocated, decided(Step.USER_OCCUPANCY, "OK", "OK"))
    tail.tick()

    taken = feed.take()
    assert [e.fields["detail"] for e in of(taken, "step") if e.fields["step"] == Step.DEVICE_OCCUPANCY] == [
        "배정 사물함 1"]
    assert of(taken, "audit")[4].fields["row"]["text"] == "배정 사물함 1"


def test_FR11_이벤트에_사용자_ID가_없다():
    reads = FakeReads(
        nodes=[make_node("locker-1", state=DeviceState.ACTIVE)], sessions=[make_session(due=at(5))],
        beacons=[DashboardBeacon(VISITOR_ID, "b1", at(-60)), DashboardBeacon(OWNER_ID, "b2", None)],
    )
    tail, feed, clock = make_tail(reads)
    tail.start()
    add_everything(reads)
    reads.rssi_rows.append(make_rssi(2, OWNER_ID, "locker-1", -70.0, at(2)))
    edit(reads.beacon_rows, lambda b: True, last_seen_at=at(9))  # 두 사람 모두 다시 수신된다
    clock.now = at(10)
    tail.tick()

    names = PersonNames()
    rendered = [render_event(event, names) for event in feed.events]  # 형식 검사도 통과한다

    assert {"mcp_call", "step", "posterior", "verdict", "command", "transition", "approval", "rssi", "beacon",
            "audit"} <= {event["type"] for event in rendered}
    for secret in (VISITOR_ID, OWNER_ID):
        assert secret not in repr(rendered)
        assert secret not in repr(feed.events)
