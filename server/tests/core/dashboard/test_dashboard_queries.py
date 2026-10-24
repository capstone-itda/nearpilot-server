"""FR-11·FR-12A·FR-12B 대시보드 조회 서비스. DB 대신 행 목록을 돌려주는 fake `DashboardReadStore` 와 고정 시각을 쓴다.
손으로 만든 감사 행은 pipeline.py·execution_store.py 가 실제로 쓰는 모양이다."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import RESPONSE_KINDS, render_response
from nearpilot.core.dashboard import DashboardQueryService, ModelVersions, fault_of
from nearpilot.core.lifecycle.shared_light import SharedHolder
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action,
    ApprovalMode,
    ApprovalStatus,
    Capability,
    DeviceState,
    ExecutionStatus,
    NodeResultState,
    Occupancy,
    ReasonCode,
    Role,
    SessionState,
    Step,
    TargetKind,
    TrustState,
    Verdict,
)
from nearpilot.shared.interfaces import DashboardQueries, DashboardReadStore, MonitorSnapshot
from nearpilot.shared.models import (
    DashboardApproval,
    DashboardAudit,
    DashboardBeacon,
    DashboardCommand,
    DashboardNode,
    DashboardPlace,
    DashboardRequest,
    DashboardSession,
    Subject,
)

T0 = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)  # 서버가 시작한 시각
NOW = T0 + timedelta(minutes=90)
KST = timezone(timedelta(hours=9))
VISITOR_ID, VISITOR2_ID, OWNER_ID = "user-visitor-1", "user-visitor-2", "user-owner-1"  # 어떤 결과에도 나오면 안 된다
VISITOR = Subject(VISITOR_ID, Role.VISITOR)
VISITOR2 = Subject(VISITOR2_ID, Role.VISITOR)
OWNER = Subject(OWNER_ID, Role.OWNER)
LOCKER_TARGET = {"kind": TargetKind.NEAREST, "capability": Capability.LOCKER}


def ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


# ── 행 만들기 ───────────────────────────────────────────────────────


def make_node(
    node_id: str = "locker-1", *, name: str | None = None, place_id: str | None = "place-1",
    capability: Capability = Capability.LOCKER, occupancy: Occupancy = Occupancy.RENTAL,
    trust: TrustState = TrustState.TRUSTED, state: DeviceState = DeviceState.AVAILABLE,
    last_seen: datetime | None = ago(5), session_id: str | None = "conn-1",
) -> DashboardNode:
    return DashboardNode(node_id, name, place_id, capability, occupancy, trust, state, last_seen, session_id)


def make_session(
    use_id: str = "use-1", *, node_id: str = "locker-1", user_id: str = VISITOR_ID,
    state: SessionState = SessionState.ACTIVE, reserved: datetime | None = ago(1800),
    due: datetime | None = None, received: datetime = ago(1810),
) -> DashboardSession:
    return DashboardSession(use_id, f"req-{use_id}", user_id, node_id, state, reserved, due, received)


def make_approval(
    approval_id: str, *, status: ApprovalStatus = ApprovalStatus.APPROVED, created: datetime = ago(3600),
    pending_until: datetime | None = None, valid_until: datetime | None = None, used: int = 0,
    remaining: int | None = None, node_id: str = "locker-1", user_id: str = VISITOR_ID,
    action: Action = Action.OPEN, mode: ApprovalMode = ApprovalMode.ONE_TIME,
) -> DashboardApproval:
    return DashboardApproval(approval_id, node_id, user_id, f"req-{approval_id}", action, status, created,
                             pending_until, valid_until, mode, used, remaining)


def make_request(
    rid: str, seq: int = 1, *, kind: str = "visitor", user_id: str = VISITOR_ID,
    verdict: Verdict | None = Verdict.OK, reason: ReasonCode | None = ReasonCode.OK,
    node_id: str | None = "locker-1", target: str = "nearest:locker", action: Action = Action.OPEN,
    received: datetime = ago(600),
) -> DashboardRequest:
    if kind == "server_internal":  # 서버 내부 요청은 사용자도 판정도 저장하지 않는다
        return DashboardRequest(seq, rid, None, kind, target, action, None, None, node_id, None, received)
    return DashboardRequest(seq, rid, user_id, kind, target, action, verdict, reason, node_id, None, received)


def make_command(
    rid: str, seq: int = 1, *, status: ExecutionStatus = ExecutionStatus.SUCCEEDED, node_id: str = "locker-1",
    action: Action = Action.OPEN, use_id: str | None = None, started: datetime | None = None,
    outcome: str | None = "succeeded", occurred: datetime | None = None, evidence: dict | None = None,
) -> DashboardCommand:
    return DashboardCommand(seq, f"cmd-{seq}", rid, node_id, action, status, use_id, started, outcome, occurred,
                            evidence or {})


def trail(rid: str, received: datetime, *steps: tuple[Step, dict], seq0: int = 1) -> list[DashboardAudit]:
    """감사 행을 seq0 부터 차례로 붙인다. 행 시각은 요청을 받은 시각 뒤 1초 간격이다."""
    return [
        DashboardAudit(seq0 + i, f"log-{rid}-{i}", rid, None, step, payload, received + timedelta(seconds=i + 1))
        for i, (step, payload) in enumerate(steps)
    ]


def decided(step: Step, verdict: str, reason: str) -> tuple[Step, dict]:
    return step, {"verdict": verdict, "reason": reason, "settings_version": "v1"}


# 판정 흐름이 남기는 감사 행 (pipeline.py 의 _audit_step 과 같은 모양)
BEACON = (Step.BEACON, {"beacons": 1, "frames": 3, "anchors": ["locker-1"], "beacon_recent_sec": 10})
CANDIDATE = (Step.CANDIDATE, {"place_id": "p1", "candidates": ["locker-1", "locker-2"], "online_ttl_sec": 10})
NEAR = (Step.PROXIMITY, {"posteriors": {"locker-1": 0.91, "locker-2": 0.05}, "threshold": 0.5,
                         "model_version": "m1", "calib_version": "c1", "best": ["locker-1", 0.91]})
WEAK = (Step.PROXIMITY, {"posteriors": {"locker-1": 0.31}, "threshold": 0.5, "model_version": "m1",
                         "calib_version": "c1", "best": ["locker-1", 0.31]})
POLICY = (Step.POLICY, {"node_id": "locker-1", "verdict": "OK", "reason": "OK", "policy_version": 1})
RESERVED = (Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "reserved"})
OK_RENTAL = (BEACON, CANDIDATE, NEAR, POLICY, RESERVED, decided(Step.USER_OCCUPANCY, "OK", "OK"))
ASK_FLOW = (BEACON, CANDIDATE, WEAK, decided(Step.PROXIMITY, "ASK", "LOW_CONFIDENCE"))
BUSY_FLOW = (
    BEACON, CANDIDATE, NEAR, POLICY, (Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "device_busy"}),
    decided(Step.DEVICE_OCCUPANCY, "BUSY", "DEVICE_BUSY"),
)
ACCESS_FLOW = (
    (Step.POLICY, {"node_id": "locker-2", "verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED",
                   "policy_version": 1}),
)
UNKNOWN_RESULT = (Step.EXEC, {"event": "record_result", "outcome": "unknown", "evidence": {"state": "unknown"}})


# ── fake 저장소와 서비스 ────────────────────────────────────────────────


class FakeReads:
    """`DashboardReadStore` fake. 행 목록을 돌려주고, 읽기 트랜잭션을 열고 닫은 횟수를 센다.

    `error` 가 있으면 모든 메서드가 던지고, `ping_error` 가 있으면 `ping` 만 던진다.
    """

    def __init__(
        self, *, places=(), nodes=(), sessions=(), approvals=(), requests=(), commands=(), audits=(),
        evidence=None, beacons=(),
    ) -> None:
        self.place_rows, self.node_rows, self.session_rows = list(places), list(nodes), list(sessions)
        self.approval_rows, self.request_rows = list(approvals), list(requests)
        self.command_rows, self.audit_rows, self.beacon_rows = list(commands), list(audits), list(beacons)
        self.evidence = dict(evidence or {})
        self.roles_of = {VISITOR_ID: Role.VISITOR, VISITOR2_ID: Role.VISITOR, OWNER_ID: Role.OWNER}
        self.error: Exception | None = None
        self.ping_error: Exception | None = None
        self.opened = self.closed = 0
        self.inside = False

    @contextmanager
    def open(self):
        self.opened += 1
        self.inside = True
        try:
            yield self
        finally:
            self.inside = False
            self.closed += 1

    def _read(self) -> None:
        if self.error is not None:
            raise self.error

    def places(self):
        self._read()
        return sorted(self.place_rows, key=lambda p: p.place_id)

    def nodes(self):
        self._read()
        return sorted(self.node_rows, key=lambda n: n.node_id)

    def roles(self, user_ids):
        self._read()
        return {u: self.roles_of[u] for u in user_ids if u in self.roles_of}

    def open_sessions(self):
        self._read()
        return sorted(self.session_rows, key=lambda s: (s.node_id, s.reserved_at or s.received_at))

    def approvals(self):
        self._read()
        return sorted(self.approval_rows, key=lambda a: (a.created_at, a.approval_id))

    def requests(self, since, until):
        self._read()
        rows = [r for r in self.request_rows
                if (since is None or r.received_at >= since) and (until is None or r.received_at < until)]
        return sorted(rows, key=lambda r: r.received_at, reverse=True)

    def request(self, internal_request_id):
        self._read()
        return next((r for r in self.request_rows if r.internal_request_id == internal_request_id), None)

    def requests_after(self, seq):
        self._read()
        return sorted((r for r in self.request_rows if r.seq > seq), key=lambda r: r.seq)

    def requests_by_ids(self, ids):
        self._read()
        wanted = set(ids)
        return sorted((r for r in self.request_rows if r.internal_request_id in wanted), key=lambda r: r.seq)

    def commands_of(self, internal_request_ids):
        self._read()
        wanted = set(internal_request_ids)
        return sorted((c for c in self.command_rows if c.internal_request_id in wanted), key=lambda c: c.seq)

    def commands_of_use(self, use_id):
        self._read()
        return sorted((c for c in self.command_rows if c.use_id == use_id), key=lambda c: c.seq)

    def commands_by_ids(self, command_ids):
        self._read()
        wanted = set(command_ids)
        return sorted((c for c in self.command_rows if c.command_id in wanted), key=lambda c: c.seq)

    def audit_recent(self, limit):
        self._read()
        return sorted(self.audit_rows, key=lambda a: a.seq, reverse=True)[:max(limit, 0)]

    def audit_after(self, seq):
        self._read()
        return sorted((a for a in self.audit_rows if a.seq > seq), key=lambda a: a.seq)

    def audit_of(self, internal_request_ids):
        self._read()
        wanted = set(internal_request_ids)
        return sorted((a for a in self.audit_rows if a.internal_request_id in wanted), key=lambda a: a.seq)

    def fault_evidence(self, node_ids):
        self._read()
        return {node_id: self.evidence[node_id] for node_id in node_ids if node_id in self.evidence}

    def rssi_after(self, seq):
        self._read()
        return []

    def active_beacons(self):
        self._read()
        return sorted(self.beacon_rows, key=lambda b: b.user_id)

    def cursors(self):
        self._read()
        return {"request": 0, "audit_log": 0, "rssi_observation": 0}

    def ping(self):
        self._read()
        if self.ping_error is not None:
            raise self.ping_error


def make_service(
    reads: FakeReads, *, holders=(), models=None, mqtt: bool = True, reports: Path | None = None,
    tz=timezone.utc, now: datetime = NOW, started: datetime = T0, weak: float = -80.0,
) -> DashboardQueryService:
    def outside(value):
        def read():
            assert not reads.inside, "읽기 트랜잭션 밖에서 불러야 한다"
            return value
        return read

    return DashboardQueryService(
        open_reads=reads.open, settings=Settings, weak_rssi_dbm=weak, models=outside(models or {}),
        mqtt_ready=outside(mqtt), holders=outside(list(holders)),
        reports_dir=reports or Path("no-such-reports"), clock=lambda: now, started_at=lambda: started, tz=tz,
    )


def strings(value) -> list[str]:
    """결과 안의 모든 문자열(키 포함). 사람(Subject)은 API 가 이름표로 바꾸므로 건너뛴다."""
    if isinstance(value, Subject):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for key, item in value.items() for s in (*strings(key), *strings(item))]
    if isinstance(value, (list, tuple, set)):
        return [s for item in value for s in strings(item)]
    return []


# ── 장소 ─────────────────────────────────────────────────────────────


def test_FR12A_이름이_없는_기기는_ID를_이름으로_쓴다():
    reads = FakeReads(
        places=[DashboardPlace("place-2", "옆방"), DashboardPlace("place-1", "라운지")],
        nodes=[
            make_node("locker-2"), make_node("mood-1", name="무드등", place_id="place-2",
                                             capability=Capability.LIGHT, occupancy=Occupancy.SHARED),
            make_node("light-1", name="조명 1", capability=Capability.LIGHT, occupancy=Occupancy.SHARED),
            make_node("locker-1", name="사물함 1"),
        ],
    )

    result = make_service(reads, weak=-82.5).places()

    assert result["weakRssiDbm"] == -82.5
    assert [(p["placeId"], p["placeName"]) for p in result["places"]] == [("place-1", "라운지"), ("place-2", "옆방")]
    assert result["places"][0]["devices"] == [  # node_id 순이다
        {"nodeId": "light-1", "name": "조명 1", "capability": Capability.LIGHT, "occupancy": Occupancy.SHARED},
        {"nodeId": "locker-1", "name": "사물함 1", "capability": Capability.LOCKER, "occupancy": Occupancy.RENTAL},
        {"nodeId": "locker-2", "name": "locker-2", "capability": Capability.LOCKER, "occupancy": Occupancy.RENTAL},
    ]
    assert result["places"][1]["devices"] == [
        {"nodeId": "mood-1", "name": "무드등", "capability": Capability.LIGHT, "occupancy": Occupancy.SHARED},
    ]


def test_FR12A_장소가_없는_기기는_장소_목록에_없다():
    reads = FakeReads(
        places=[DashboardPlace("place-1", "라운지"), DashboardPlace("place-2", "빈 장소")],
        nodes=[make_node("locker-1"), make_node("orphan-1", place_id=None)],
    )

    places = make_service(reads).places()["places"]

    assert [d["nodeId"] for p in places for d in p["devices"]] == ["locker-1"]
    assert places[1]["devices"] == []  # 기기가 없는 장소는 그대로 있다


def test_FR12A_모델이_없는_장소는_버전이_없음():
    reads = FakeReads(places=[DashboardPlace("place-1", "라운지"), DashboardPlace("place-2", "옆방")])
    service = make_service(reads, models={"place-1": ModelVersions("model-1", "cal-1")})

    places = service.places()["places"]

    assert [(p["modelVersion"], p["calibrationVersion"]) for p in places] == [("model-1", "cal-1"), ("없음", "없음")]
    assert service.health()["server"]["model"] == "model-1 · cal-1 / 없음"
    assert make_service(FakeReads()).places() == {"weakRssiDbm": -80.0, "places": []}
    assert make_service(FakeReads()).health()["server"]["model"] == "없음"  # 장소가 없다


# ── 점유 ─────────────────────────────────────────────────────────────


def test_FR12A_점유_목록은_대여와_공용_유지_대상자다():
    reads = FakeReads(
        nodes=[
            make_node("locker-1", state=DeviceState.ACTIVE), make_node("locker-2", state=DeviceState.RESERVED),
            make_node("locker-3", state=DeviceState.CLOSING), make_node("locker-4", state=DeviceState.FAULT),
            make_node("light-1", capability=Capability.LIGHT, occupancy=Occupancy.SHARED),
        ],
        sessions=[
            make_session("use-4", node_id="locker-4", user_id=OWNER_ID, reserved=ago(300)),
            make_session("use-2", node_id="locker-2", user_id=VISITOR2_ID, state=SessionState.RESERVED,
                         reserved=None, received=ago(120)),
            make_session("use-1", node_id="locker-1", due=ago(600)),
            make_session("use-3", node_id="locker-3", user_id=VISITOR2_ID, state=SessionState.CLOSING,
                         reserved=ago(2400), due=ago(60)),
        ],
    )
    holders = [
        SharedHolder("light-1", OWNER_ID, ago(30)), SharedHolder("light-1", VISITOR_ID, ago(5)),
        SharedHolder("ghost-1", VISITOR_ID, ago(5)),  # 기기가 없으면 보이지 않는다
    ]

    uses = make_service(reads, holders=holders).uses()

    assert uses == [  # 대여형이 먼저(기기 순), 공용형이 나중이다
        {"subject": VISITOR, "nodeId": "locker-1", "occupancy": Occupancy.RENTAL, "session": SessionState.ACTIVE,
         "sessionNote": "대여 시간 초과", "startedAt": ago(1800), "overdueMinutes": 10.0},
        {"subject": VISITOR2, "nodeId": "locker-2", "occupancy": Occupancy.RENTAL, "session": SessionState.RESERVED,
         "startedAt": ago(120)},  # 예약 시각이 없으면 요청 시각이다. 메모와 초과는 키를 뺀다
        {"subject": VISITOR2, "nodeId": "locker-3", "occupancy": Occupancy.RENTAL, "session": SessionState.CLOSING,
         "sessionNote": "반납 확인 중", "startedAt": ago(2400), "overdueMinutes": 1.0},
        {"subject": OWNER, "nodeId": "locker-4", "occupancy": Occupancy.RENTAL, "session": SessionState.ACTIVE,
         "sessionNote": "고장으로 보존", "startedAt": ago(300)},
        {"subject": OWNER, "nodeId": "light-1", "occupancy": Occupancy.SHARED, "session": SessionState.ACTIVE,
         "startedAt": ago(30)},
        {"subject": VISITOR, "nodeId": "light-1", "occupancy": Occupancy.SHARED, "session": SessionState.ACTIVE,
         "startedAt": ago(5)},
    ]


# ── 승인 ─────────────────────────────────────────────────────────────


def test_FR12A_승인_분류와_끝난_사유():
    pending, approved, denied = ApprovalStatus.PENDING, ApprovalStatus.APPROVED, ApprovalStatus.DENIED
    soon, later = NOW + timedelta(seconds=330), NOW + timedelta(seconds=5459)
    approvals = [  # 만든 시각 순서가 저장소 순서다
        make_approval("pend-ok", status=pending, created=ago(900), pending_until=soon, node_id="pend-ok"),
        make_approval("pend-edge", status=pending, created=ago(895), pending_until=NOW, node_id="pend-edge"),
        make_approval("pend-late", status=pending, created=ago(890), pending_until=ago(1), node_id="pend-late"),
        make_approval("live-edge", created=ago(885), valid_until=NOW, node_id="live-edge"),
        make_approval("live", created=ago(880), valid_until=later, used=1, node_id="live",
                      mode=ApprovalMode.TIME_WINDOW),
        make_approval("time-over", created=ago(870), valid_until=ago(1), remaining=2, node_id="time-over"),
        make_approval("spent", created=ago(860), valid_until=NOW + timedelta(hours=1), used=1, remaining=0,
                      node_id="spent"),
        make_approval("both", created=ago(850), valid_until=ago(1), used=1, remaining=0, node_id="both"),
        make_approval("no-deadline", created=ago(840), valid_until=None, node_id="no-deadline"),
        make_approval("rejected", status=denied, created=ago(830), node_id="rejected", user_id=VISITOR2_ID),
        make_approval("limited", created=ago(820), valid_until=NOW + timedelta(minutes=30), remaining=3,
                      node_id="limited"),
    ]

    result = make_service(FakeReads(approvals=approvals)).approvals()

    assert result.keys() == {"pending", "active", "ended"}
    assert result["pending"] == [{"subject": VISITOR, "nodeId": "pend-ok", "mode": ApprovalMode.ONE_TIME,
                                  "requestedAt": ago(900), "remainingMinutes": 5, "uses": 0}]
    assert result["active"] == [
        {"subject": VISITOR, "nodeId": "live", "mode": ApprovalMode.TIME_WINDOW, "requestedAt": ago(880),
         "remainingMinutes": 90, "validUntil": later, "uses": 1},
        {"subject": VISITOR, "nodeId": "limited", "mode": ApprovalMode.ONE_TIME, "requestedAt": ago(820),
         "remainingMinutes": 30, "validUntil": NOW + timedelta(minutes=30), "uses": 0},
    ]
    assert {row["nodeId"]: row["endReason"] for row in result["ended"]} == {
        "pend-edge": ReasonCode.APPROVAL_REQUEST_EXPIRED,  # 기한과 같은 순간부터 끝난 것이다
        "pend-late": ReasonCode.APPROVAL_REQUEST_EXPIRED,
        "live-edge": ReasonCode.APPROVAL_EXPIRED,
        "time-over": ReasonCode.APPROVAL_EXPIRED,
        "spent": ReasonCode.APPROVAL_EXHAUSTED,
        "both": ReasonCode.APPROVAL_EXPIRED,  # 둘 다면 시간을 먼저 본다
        "no-deadline": ReasonCode.APPROVAL_EXHAUSTED,
        "rejected": ReasonCode.APPROVAL_REJECTED,
    }
    assert [row["nodeId"] for row in result["ended"]] == [
        "pend-edge", "pend-late", "live-edge", "time-over", "spent", "both", "no-deadline", "rejected",
    ]
    assert result["ended"][-1] == {"subject": VISITOR2, "nodeId": "rejected", "mode": ApprovalMode.ONE_TIME,
                                   "requestedAt": ago(830), "uses": 0, "endReason": ReasonCode.APPROVAL_REJECTED}


# ── 감사 ─────────────────────────────────────────────────────────────


def test_FR12A_감사_목록은_최근_것부터_기기를_찾는다():
    reads = FakeReads(
        nodes=[make_node("locker-1", name="사물함 1")],
        requests=[make_request("req-1")],
        commands=[make_command("req-1", 1, status=ExecutionStatus.UNKNOWN, outcome="unknown")],
        audits=[
            DashboardAudit(1, "log-1", "req-1", None, *BEACON, ago(50)),
            DashboardAudit(2, "log-2", None, None, Step.ADMIN,
                           {"event": "set_node_name", "node_id": "locker-1", "name": "사물함 1"}, ago(40)),
            DashboardAudit(3, "log-3", None, "cmd-1", *UNKNOWN_RESULT, ago(30)),
            DashboardAudit(4, "log-4", None, None, Step.ADMIN,
                           {"event": "register_account", "user_id": VISITOR_ID, "role": "visitor"}, ago(20)),
        ],
    )
    service = make_service(reads)

    rows = service.audit(10)

    assert rows == [
        {"id": "log-4", "at": ago(20), "step": Step.ADMIN, "text": "계정 등록", "code": ReasonCode.OK},
        {"id": "log-3", "at": ago(30), "step": Step.EXEC, "nodeId": "locker-1", "text": "결과 불명 → 고장 격리",
         "code": ReasonCode.EXEC_UNKNOWN},  # 명령으로 기기를 찾는다
        {"id": "log-2", "at": ago(40), "step": Step.ADMIN, "nodeId": "locker-1", "text": "기기 이름 변경",
         "code": ReasonCode.OK},  # payload 로 기기를 찾는다
        {"id": "log-1", "at": ago(50), "step": Step.BEACON, "nodeId": "locker-1", "text": "비콘 최근 수신",
         "code": ReasonCode.OK},  # 요청으로 기기를 찾는다
    ]
    assert VISITOR_ID not in "".join(strings(rows))
    assert [row["id"] for row in service.audit(2)] == ["log-4", "log-3"]
    assert service.audit(0) == []


# ── 이력 ─────────────────────────────────────────────────────────────


def history_world() -> FakeReads:
    requests = [
        make_request("req-ok", 1, received=ago(600)),
        make_request("req-ask", 2, user_id=VISITOR2_ID, verdict=Verdict.ASK, reason=ReasonCode.LOW_CONFIDENCE,
                     received=ago(500)),
        make_request("req-busy", 3, user_id=VISITOR2_ID, verdict=Verdict.BUSY, reason=ReasonCode.DEVICE_BUSY,
                     node_id="locker-2", received=ago(400)),
        make_request("req-unknown", 4, node_id="locker-3", target="nearest:locker", received=ago(100)),
        # 아래는 이력에 나오지 않는다
        make_request("req-pending", 5, verdict=None, reason=None, node_id=None, received=ago(90)),
        make_request("req-access", 6, verdict=Verdict.NEED_APPROVAL, reason=ReasonCode.APPROVAL_REQUIRED,
                     received=ago(80)),
        make_request("req-internal", 7, kind="server_internal", node_id="light-1", action=Action.OFF,
                     target="light-1", received=ago(70)),
        make_request("req-purged", 8, received=ago(60)),  # 보존 정리로 감사 행이 지워졌다
    ]
    audits = [
        *trail("req-ok", ago(600), *OK_RENTAL, seq0=10),
        *trail("req-ask", ago(500), *ASK_FLOW, seq0=20),
        *trail("req-busy", ago(400), *BUSY_FLOW, seq0=30),
        *trail("req-unknown", ago(100), *OK_RENTAL, UNKNOWN_RESULT, seq0=40),
        *trail("req-pending", ago(90), BEACON, CANDIDATE, seq0=50),
        *trail("req-access", ago(80), *ACCESS_FLOW, seq0=60),
        *trail("req-internal", ago(70), (Step.EXEC, {"event": "record_result", "outcome": "succeeded",
                                                      "evidence": {}}), seq0=70),
    ]
    commands = [
        make_command("req-ok", 1, outcome="succeeded", occurred=ago(598)),
        make_command("req-unknown", 2, node_id="locker-3", status=ExecutionStatus.UNKNOWN, outcome="unknown",
                     occurred=ago(95)),
    ]
    return FakeReads(requests=requests, audits=audits, commands=commands)


def test_FR12A_이력은_방문자_request_use_가운데_판정이_저장된_요청이다():
    rows = make_service(history_world()).history("today", None, None)

    assert rows == [
        {"subject": VISITOR, "requestId": "req-unknown", "at": ago(100), "target": LOCKER_TARGET,
         "action": Action.OPEN, "verdict": Verdict.OK, "nodeId": "locker-3", "outcome": "결과 불명 → 고장 격리",
         "code": ReasonCode.EXEC_UNKNOWN},
        {"subject": VISITOR2, "requestId": "req-busy", "at": ago(400), "target": LOCKER_TARGET,
         "action": Action.OPEN, "verdict": Verdict.BUSY, "nodeId": "locker-2", "outcome": "실행 안 함",
         "code": ReasonCode.DEVICE_BUSY},
        {"subject": VISITOR2, "requestId": "req-ask", "at": ago(500), "target": LOCKER_TARGET,
         "action": Action.OPEN, "verdict": Verdict.ASK, "nodeId": "locker-1", "outcome": "다시 질문",
         "code": ReasonCode.LOW_CONFIDENCE},
        {"subject": VISITOR, "requestId": "req-ok", "at": ago(600), "target": LOCKER_TARGET, "action": Action.OPEN,
         "verdict": Verdict.OK, "nodeId": "locker-1", "outcome": "열림"},
    ]


def test_FR12A_판정_중인_요청은_이력에_없고_단계는_pending():
    received = ago(10)
    reads = FakeReads(
        nodes=[make_node()],
        requests=[make_request("req-1", verdict=None, reason=None, node_id=None, received=received)],
        audits=trail("req-1", received, BEACON, CANDIDATE),
    )
    service = make_service(reads)

    assert service.history("today", None, None) == []
    result = service.request("req-1")
    assert [step["result"] for step in result["steps"]] == ["pass", "pass", "pass"] + ["pending"] * 4
    assert result["steps"][4] == {"result": "pending"}
    assert not {"verdict", "reason", "nodeId", "execution", "respondedAt", "posteriors", "threshold"} & result.keys()
    assert (result["id"], result["tool"], result["at"]) == ("req-1", "request_use", received)


def test_FR12A_접근_신청_요청은_이력에_없다():
    reads = FakeReads(
        requests=[
            make_request("req-access", 1, verdict=Verdict.NEED_APPROVAL, reason=ReasonCode.APPROVAL_REQUIRED,
                         node_id="locker-2"),
            make_request("req-use", 2, node_id="locker-1", received=ago(300)),
        ],
        audits=[*trail("req-access", ago(600), *ACCESS_FLOW, seq0=1), *trail("req-use", ago(300), *OK_RENTAL, seq0=10)],
    )
    service = make_service(reads)

    assert [row["requestId"] for row in service.history("today", None, None)] == ["req-use"]
    assert service.request("req-access") is None
    assert service.request("req-use") is not None


def test_FR12A_이력과_신뢰도는_방문자가_아닌_요청을_판정이_있어도_뺀다():
    received = ago(50)
    # 서버 내부 요청은 판정을 저장하지 않는다. 저장돼 있어도 방문자 요청이 아니면 센 적이 없다
    internal = DashboardRequest(2, "req-internal", None, "server_internal", "light-1", Action.OFF, Verdict.OK,
                                ReasonCode.OK, "light-1", None, received)
    reads = FakeReads(
        requests=[make_request("req-use", 1, received=ago(60)), internal],
        audits=[*trail("req-use", ago(60), *OK_RENTAL), *trail("req-internal", received, *OK_RENTAL, seq0=20)],
    )
    service = make_service(reads)

    assert [row["requestId"] for row in service.history("today", None, None)] == ["req-use"]
    assert service.trust()["live"]["decisions"] == 1


def test_FR12A_이력_거르기():
    service = make_service(history_world())

    assert [r["requestId"] for r in service.history("today", Verdict.ASK, None)] == ["req-ask"]
    assert [r["requestId"] for r in service.history("today", None, "locker-1")] == ["req-ask", "req-ok"]
    assert [r["requestId"] for r in service.history("today", Verdict.BUSY, "locker-2")] == ["req-busy"]
    assert service.history("today", Verdict.BUSY, "locker-1") == []
    assert service.history("today", Verdict.DENY, None) == []


@pytest.mark.parametrize(
    "period",
    ["", "yesterday", "TODAY", "today ", "9:00~10:30", "09:00-10:30", "09:00~10:30 ", "09:00~", "10:30~09:00",
     "09:00~09:00", "24:00~25:00", "09:60~10:00", "０９:00~10:30"],
)
def test_FR12A_이력_기간_형식_틀린_값은_ValueError(period):
    with pytest.raises(ValueError):
        make_service(FakeReads()).history(period, None, None)


def test_FR12A_이력_기간_형식():
    day = NOW.replace(hour=0, minute=0)
    received = {
        "yesterday": day - timedelta(seconds=1), "midnight": day, "before": T0 - timedelta(seconds=1),
        "start": T0, "inside": NOW - timedelta(seconds=1), "end": NOW,
    }
    reads = FakeReads(
        requests=[make_request(f"req-{name}", seq, received=at) for seq, (name, at) in enumerate(received.items())],
        audits=[row for seq, (name, at) in enumerate(received.items())
                for row in trail(f"req-{name}", at, *OK_RENTAL, seq0=seq * 10)],
    )
    service = make_service(reads)

    def ids(period):
        return [row["requestId"] for row in service.history(period, None, None)]

    assert ids("today") == ["req-end", "req-inside", "req-start", "req-before", "req-midnight"]
    assert ids("09:00~10:30") == ["req-inside", "req-start"]  # 끝은 포함하지 않는다
    assert ids("00:00~09:00") == ["req-before", "req-midnight"]


def test_FR12A_이력_오늘은_주입한_시간대의_0시부터다():
    now = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)  # 서울 10월 24일 05:00
    received = {
        "before": datetime(2026, 10, 23, 14, 59, 59, tzinfo=timezone.utc),  # 서울 10월 23일 23:59:59
        "midnight": datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc),  # 서울 10월 24일 00:00
        "dawn": now,
    }
    reads = FakeReads(
        requests=[make_request(f"req-{name}", seq, received=at) for seq, (name, at) in enumerate(received.items())],
        audits=[row for seq, (name, at) in enumerate(received.items())
                for row in trail(f"req-{name}", at, *OK_RENTAL, seq0=seq * 10)],
    )

    def ids(period, tz):
        return [r["requestId"] for r in make_service(reads, tz=tz, now=now).history(period, None, None)]

    assert ids("today", KST) == ["req-dawn", "req-midnight"]
    assert ids("00:00~06:00", KST) == ["req-dawn", "req-midnight"]
    assert ids("today", timezone.utc) == ["req-dawn", "req-midnight", "req-before"]  # UTC 로는 모두 10월 23일이다
    assert ids("20:00~21:00", timezone.utc) == ["req-dawn"]


def test_FR12A_이력_기간의_시간대를_주지_않으면_서버_지역_시간대를_쓴다():
    reads = FakeReads(
        requests=[make_request("req-now", 1, received=NOW),
                  make_request("req-old", 2, received=NOW - timedelta(days=2))],
        audits=[*trail("req-now", NOW, *OK_RENTAL, seq0=10),
                *trail("req-old", NOW - timedelta(days=2), *OK_RENTAL, seq0=20)],
    )

    rows = make_service(reads, tz=None).history("today", None, None)  # 서버가 어느 시간대에 있어도 이틀 전은 오늘이 아니다

    assert [row["requestId"] for row in rows] == ["req-now"]


# ── 요청 한 건 ────────────────────────────────────────────────────────


def test_FR12A_없는_요청은_None():
    reads = FakeReads(
        requests=[make_request("req-internal", kind="server_internal", node_id="light-1", action=Action.OFF)],
        audits=trail("req-internal", ago(600), (Step.EXEC, {"event": "record_result", "outcome": "succeeded"})),
    )
    service = make_service(reads)

    assert service.request("missing") is None
    assert service.request("req-internal") is None  # 서버 내부 요청은 도구 호출이 아니다
    assert reads.opened == reads.closed == 2


def test_FR12A_요청_한_건은_단계_결과와_근접_실행_접근_신청을_담는다():
    received = ago(600)
    reads = FakeReads(
        nodes=[make_node("locker-1", name="사물함 1")],
        requests=[make_request("req-ok", received=received)],
        audits=[
            *trail("req-ok", received, *OK_RENTAL),
            DashboardAudit(99, "log-close", "req-ok", None, Step.CLOSE, {"event": "finish_close"}, ago(30)),
        ],
        commands=[make_command("req-ok", 1, outcome="succeeded", occurred=ago(594))],
        approvals=[
            make_approval("early", status=ApprovalStatus.PENDING, created=ago(1000), pending_until=ago(800)),
            make_approval("latest", created=ago(900), pending_until=ago(300), valid_until=NOW),
            make_approval("after", created=ago(100)),  # 요청보다 늦게 만든 승인
            make_approval("same-time", created=received),  # 같은 순간에 만든 승인은 먼저가 아니다
            make_approval("other-user", created=ago(700), user_id=VISITOR2_ID),
            make_approval("other-action", created=ago(650), action=Action.ON),
            make_approval("other-node", created=ago(640), node_id="locker-2"),
        ],
    )

    result = make_service(reads).request("req-ok")

    steps = result.pop("steps")
    assert result == {
        "subject": VISITOR, "id": "req-ok", "tool": "request_use", "target": LOCKER_TARGET, "action": Action.OPEN,
        "at": received, "verdict": Verdict.OK, "reason": ReasonCode.OK, "nodeId": "locker-1",
        "posteriors": {"locker-1": 0.91, "locker-2": 0.05}, "threshold": 0.5, "execution": ExecutionStatus.SUCCEEDED,
        "accessRequestedAt": ago(900), "accessExpiredAt": ago(300),
        "respondedAt": received + timedelta(seconds=6),  # 반납 완료 행은 응답 시각이 아니다
    }
    assert [step["result"] for step in steps] == ["pass"] * 7
    assert steps[0] == {"result": "pass"}  # 감사 행이 없는 ⓪ 은 문구가 없다
    assert steps[3] == {"result": "pass", "detail": "근접 통과 0.91"}
    assert steps[5] == {"result": "pass", "detail": "비어 있음"}


def test_FR12A_멈춘_요청은_멈춘_단계와_사유를_담는다():
    received = ago(200)
    reads = FakeReads(
        requests=[make_request("req-ask", user_id=VISITOR2_ID, verdict=Verdict.ASK,
                               reason=ReasonCode.LOW_CONFIDENCE, received=received)],
        audits=trail("req-ask", received, *ASK_FLOW),
    )

    result = make_service(reads).request("req-ask")

    assert [step["result"] for step in result["steps"]] == ["pass", "pass", "pass", "stop", "skip", "skip", "skip"]
    assert (result["subject"], result["verdict"], result["reason"]) == (
        VISITOR2, Verdict.ASK, ReasonCode.LOW_CONFIDENCE)
    assert result["threshold"] == 0.5 and result["posteriors"] == {"locker-1": 0.31}
    assert "execution" not in result and "accessRequestedAt" not in result
    assert result["respondedAt"] == received + timedelta(seconds=4)


# ── 신뢰도 ───────────────────────────────────────────────────────────


def test_FR12A_판정이_없으면_지연은_None():
    reads = FakeReads(
        requests=[
            make_request("req-pending", 1, verdict=None, reason=None, received=ago(10)),
            make_request("req-old", 2, received=T0 - timedelta(minutes=10)),  # 서버가 시작하기 전
        ],
        audits=[*trail("req-pending", ago(10), BEACON), *trail("req-old", T0 - timedelta(minutes=10), *OK_RENTAL)],
    )

    result = make_service(reads).trust()

    assert result == {"live": {
        "since": T0, "decisions": 0, "doubleOccupancy": 0, "latencyMedianSec": None, "latencyP95Sec": None,
        "counts": {verdict: 0 for verdict in Verdict},
    }}


def test_FR12A_신뢰도는_시작_뒤_판정의_수와_지연을_센다():
    def flow(rid, seq0, latency, verdict, *, step=Step.USER_OCCUPANCY, reason="OK"):
        received = ago(1000 - seq0)
        rows = [
            DashboardAudit(seq0, f"log-{rid}-0", rid, None, *BEACON, received + timedelta(seconds=0.1)),
            DashboardAudit(seq0 + 1, f"log-{rid}-1", rid, None, *decided(step, verdict.value, reason),
                           received + timedelta(seconds=latency)),
        ]
        return make_request(rid, seq0, verdict=verdict, reason=ReasonCode(reason), received=received), rows

    flows = [
        flow("r1", 10, 0.5, Verdict.OK), flow("r2", 20, 1.0, Verdict.OK), flow("r3", 30, 1.5, Verdict.OK),
        flow("r4", 40, 2.0, Verdict.ASK, step=Step.PROXIMITY, reason="LOW_CONFIDENCE"),
        flow("r5", 50, 10.0, Verdict.BUSY, step=Step.DEVICE_OCCUPANCY, reason="DEVICE_BUSY"),
    ]
    requests = [request for request, _ in flows]
    audits = [row for _, rows in flows for row in rows]
    # r1 의 반납 완료 행은 한참 뒤에 붙는다. 지연에 넣지 않는다
    audits.append(DashboardAudit(900, "log-close", "r1", None, Step.CLOSE, {"event": "finish_close"}, ago(30)))
    # 세지 않는 요청들
    requests += [
        make_request("r-access", 60, verdict=Verdict.NEED_APPROVAL, reason=ReasonCode.APPROVAL_REQUIRED),
        make_request("r-internal", 61, kind="server_internal", node_id="light-1"),
        make_request("r-pending", 62, verdict=None, reason=None),
        make_request("r-old", 63, received=T0 - timedelta(minutes=1)),
    ]
    audits += [*trail("r-access", ago(500), *ACCESS_FLOW, seq0=100), *trail("r-pending", ago(500), BEACON, seq0=110),
               *trail("r-old", T0 - timedelta(minutes=1), *OK_RENTAL, seq0=120)]
    sessions = [  # 끝나지 않은 점유가 둘 이상인 기기가 둘이다
        make_session("use-1", node_id="locker-1"), make_session("use-2", node_id="locker-1", user_id=VISITOR2_ID),
        make_session("use-3", node_id="locker-2"),
        *(make_session(f"use-3{n}", node_id="locker-3") for n in range(3)),
    ]

    result = make_service(FakeReads(requests=requests, audits=audits, sessions=sessions)).trust()

    assert result["live"] == {
        "since": T0, "decisions": 5, "doubleOccupancy": 2, "latencyMedianSec": 1.5, "latencyP95Sec": 10.0,
        "counts": {Verdict.OK: 3, Verdict.ASK: 1, Verdict.BUSY: 1, Verdict.DENY: 0, Verdict.NEED_APPROVAL: 0},
    }
    assert "evaluation" not in result


@pytest.mark.parametrize(
    ("latencies", "median", "p95"),
    [
        ([2.0], 2.0, 2.0),
        ([1.0, 3.0], 2.0, 3.0),  # 짝수 개의 중앙값은 가운데 두 값의 평균이다
        ([float(n) for n in range(1, 21)], 10.5, 19.0),  # 20개: 순위 올림(0.95 × 20 = 19)
        ([float(n) for n in range(1, 22)], 11.0, 20.0),  # 21개: 순위 올림(19.95 → 20)
    ],
)
def test_FR12A_중앙값과_p95는_가장_가까운_순위다(latencies, median, p95):
    requests, audits = [], []
    for seq, latency in enumerate(latencies, start=1):
        received = ago(3000)
        requests.append(make_request(f"r{seq}", seq, received=received))
        audits.append(
            DashboardAudit(seq, f"log-{seq}", f"r{seq}", None, *BEACON, received + timedelta(seconds=latency)))

    live = make_service(FakeReads(requests=requests, audits=audits)).trust()["live"]

    assert (live["latencyMedianSec"], live["latencyP95Sec"]) == (median, p95)


def test_FR12A_신뢰도는_가장_늦은_평가_리포트를_담는다(tmp_path):
    (tmp_path / "report.csv").write_text(
        "# generated_at: 2026-10-20T01:00:00+00:00\nmetric,passed,trials,note\nece,True,12,\n", encoding="utf-8")

    result = make_service(FakeReads(), reports=tmp_path).trust()

    assert result["evaluation"] == {
        "date": "2026-10-20",
        "metrics": [{"id": 1, "name": "보정 오차", "pass": True, "label": "통과", "sample": "12"}],
    }


# ── 대여 ─────────────────────────────────────────────────────────────


def test_FR12A_대여가_없으면_None():
    reads = FakeReads(sessions=[make_session("use-1", node_id="locker-1")])
    service = make_service(reads)

    assert service.rental("locker-2") is None
    assert make_service(FakeReads()).rental("locker-1") is None


def test_FR12A_대여_타임라인은_시각_순이고_마지막에_now가_붙는다():
    reads = FakeReads(
        sessions=[make_session("use-1", due=ago(300))],
        commands=[
            make_command("req-use-1", 1, use_id="use-1", outcome="succeeded", occurred=ago(1790)),
            make_command("req-use-1", 2, use_id="use-1", status=ExecutionStatus.UNKNOWN, outcome="unknown",
                         occurred=ago(100)),
            make_command("req-use-1", 3, use_id="use-1", status=ExecutionStatus.NOT_SENT, outcome="not_sent",
                         occurred=ago(50)),  # 보내지 않고 취소한 명령은 결과가 없다
            make_command("req-use-1", 4, use_id="use-1", status=ExecutionStatus.IN_PROGRESS, outcome=None,
                         started=ago(40)),
            make_command("req-other", 5, use_id="use-2", outcome="succeeded", occurred=ago(30)),
        ],
    )

    result = make_service(reads).rental("locker-1")

    assert result == {
        "subject": VISITOR, "nodeId": "locker-1", "startedAt": ago(1800), "overdueMinutes": 5.0,
        "timeline": [
            {"at": ago(1800), "text": "예약 · RESERVED"},
            {"at": ago(1790), "text": "열림 · ACTIVE"},
            {"at": ago(300), "text": "대여 시간 초과"},
            {"at": ago(100), "text": "결과 불명 · 고장 격리", "now": True},
        ],
    }


def test_FR12A_대여가_초과되지_않았고_명령이_없으면_예약만_있다():
    reads = FakeReads(sessions=[make_session("use-1", state=SessionState.RESERVED, reserved=None, received=ago(90),
                                             due=NOW + timedelta(minutes=5))])

    result = make_service(reads).rental("locker-1")

    assert result["startedAt"] == ago(90)  # 예약 시각이 없으면 요청 시각이다
    assert result["overdueMinutes"] == 0.0  # 필수 키이고 없으면 0이다
    assert result["timeline"] == [{"at": ago(90), "text": "예약 · RESERVED", "now": True}]


# ── 점검 ─────────────────────────────────────────────────────────────


def test_FR12A_점검은_연결과_비콘_수신을_센다():
    reads = FakeReads(
        nodes=[
            make_node("a-online", last_seen=ago(15)),  # 마지막 수신이 online_ttl_sec 안이다
            make_node("b-stale", last_seen=ago(15.001), trust=TrustState.DISCOVERED),
            make_node("c-no-session", last_seen=ago(1), session_id=None),
            make_node("d-never", last_seen=None, session_id=None, trust=TrustState.REVOKED),
        ],
        beacons=[
            DashboardBeacon(VISITOR_ID, "beacon-1", ago(1.5)),
            DashboardBeacon(VISITOR2_ID, "beacon-2", ago(3.001)),
            DashboardBeacon(OWNER_ID, "beacon-3", None),
        ],
    )

    health = make_service(reads, mqtt=False).health()

    assert health["devices"] == [
        {"nodeId": "a-online", "connected": True, "trust": TrustState.TRUSTED},
        {"nodeId": "b-stale", "connected": False, "trust": TrustState.DISCOVERED},
        {"nodeId": "c-no-session", "connected": False, "trust": TrustState.TRUSTED},
        {"nodeId": "d-never", "connected": False, "trust": TrustState.REVOKED},
    ]
    assert health["beacons"] == [  # 사람 ID 순이다
        {"subject": OWNER, "lastSeenSec": None, "note": "최근 신호 없음"},
        {"subject": VISITOR, "lastSeenSec": 1.5},
        {"subject": VISITOR2, "lastSeenSec": pytest.approx(3.001), "note": "최근 신호 없음"},
    ]
    assert health["server"] == {"database": True, "mqtt": False, "model": "없음"}


def test_FR12A_점검_비콘은_최근_기준과_같은_수신까지_메모가_없다():
    reads = FakeReads(beacons=[DashboardBeacon(VISITOR_ID, "beacon-1", ago(Settings().beacon_recent_sec))])

    assert make_service(reads).health()["beacons"] == [{"subject": VISITOR, "lastSeenSec": 3.0}]


def test_FR12A_점검_DB가_실패하면_database_False():
    reads = FakeReads(nodes=[make_node()], beacons=[DashboardBeacon(VISITOR_ID, "beacon-1", ago(1))])
    reads.ping_error = sqlite3.OperationalError("disk I/O error")

    health = make_service(reads).health()

    assert health["server"]["database"] is False
    assert [d["nodeId"] for d in health["devices"]] == ["locker-1"]  # 나머지 키는 그대로 만든다
    assert len(health["beacons"]) == 1
    reads.ping_error = None
    assert make_service(reads).health()["server"]["database"] is True


# ── 지금 상태 ─────────────────────────────────────────────────────────


def rich_world() -> FakeReads:
    """기기 다섯, 점유 둘, 승인 셋, 비콘 셋, 요청 여덟(진행 중 셋)."""
    unknown_audit = DashboardAudit(900, "log-unknown", "req-unknown", None, *UNKNOWN_RESULT, ago(295))
    return FakeReads(
        places=[DashboardPlace("place-1", "라운지"), DashboardPlace("place-2", "옆방")],
        nodes=[
            make_node("light-1", name="조명 1", capability=Capability.LIGHT, occupancy=Occupancy.SHARED),
            make_node("locker-1", name="사물함 1", state=DeviceState.ACTIVE),
            make_node("locker-2", state=DeviceState.FAULT, last_seen=ago(40)),
            make_node("locker-3", place_id="place-2", state=DeviceState.CLOSING),
            make_node("orphan-1", place_id=None, last_seen=None, session_id=None),
        ],
        sessions=[
            make_session("use-1", node_id="locker-1", due=ago(300)),
            make_session("use-3", node_id="locker-3", user_id=VISITOR2_ID, state=SessionState.CLOSING,
                         due=ago(60)),
        ],
        approvals=[
            make_approval("ap-active", created=ago(900), pending_until=ago(300),
                          valid_until=NOW + timedelta(minutes=30)),
            make_approval("ap-denied", status=ApprovalStatus.DENIED, created=ago(3000), user_id=VISITOR2_ID),
            make_approval("ap-pending", status=ApprovalStatus.PENDING, created=ago(100), node_id="locker-2",
                          pending_until=NOW + timedelta(seconds=500)),
        ],
        requests=[
            make_request("req-ok", 1, received=ago(600)),
            make_request("req-ask", 2, user_id=VISITOR2_ID, verdict=Verdict.ASK, reason=ReasonCode.LOW_CONFIDENCE,
                         received=ago(500)),
            make_request("req-unknown", 4, node_id="locker-2", received=ago(300)),
            make_request("req-access", 5, verdict=Verdict.NEED_APPROVAL, reason=ReasonCode.APPROVAL_REQUIRED,
                         node_id="locker-2", received=ago(120)),
            make_request("req-run", 6, user_id=VISITOR2_ID, node_id="locker-3", received=ago(60)),
            make_request("req-pending", 7, user_id=VISITOR2_ID, verdict=None, reason=None, node_id=None,
                         received=ago(30)),
            make_request("req-internal", 8, kind="server_internal", node_id="light-1", action=Action.OFF,
                         target="light-1", received=ago(20)),
            make_request("req-old", 9, verdict=None, reason=None, received=T0 - timedelta(minutes=10)),
        ],
        audits=[
            *trail("req-ok", ago(600), *OK_RENTAL, seq0=10),
            *trail("req-ask", ago(500), *ASK_FLOW, seq0=20),
            *trail("req-unknown", ago(300), *OK_RENTAL, seq0=30),
            *trail("req-access", ago(120), *ACCESS_FLOW, seq0=60),
            *trail("req-run", ago(60), *OK_RENTAL, seq0=70),
            *trail("req-pending", ago(30), BEACON, CANDIDATE, seq0=80),
            *trail("req-old", T0 - timedelta(minutes=10), BEACON, seq0=90),
            unknown_audit,
        ],
        commands=[
            make_command("req-ok", 1, use_id="use-1", outcome="succeeded", started=ago(599), occurred=ago(598)),
            make_command("req-unknown", 2, node_id="locker-2", status=ExecutionStatus.UNKNOWN, outcome="unknown",
                         started=ago(299), occurred=ago(295)),
            make_command("req-run", 3, node_id="locker-3", status=ExecutionStatus.IN_PROGRESS, outcome=None,
                         started=ago(55)),
            make_command("req-internal", 4, node_id="light-1", action=Action.OFF,
                         status=ExecutionStatus.IN_PROGRESS, outcome=None, started=ago(19)),
        ],
        evidence={"locker-2": unknown_audit},
        beacons=[
            DashboardBeacon(VISITOR_ID, "beacon-1", ago(1)),
            DashboardBeacon(VISITOR2_ID, "beacon-2", ago(20)),
            DashboardBeacon(OWNER_ID, "beacon-3", None),
        ],
    )


def test_FR12B_지금_상태는_기기_비콘_승인_진행_중_요청이다():
    reads = rich_world()
    holders = [SharedHolder("light-1", VISITOR_ID, ago(30))]

    events = make_service(reads, holders=holders).current_events()

    assert [e.type for e in events] == (
        ["transition"] * 5 + ["beacon"] * 3 + ["approval"]
        + ["mcp_call"] + ["step"] * 7 + ["posterior", "verdict", "command"]  # req-run: 판정이 났고 명령이 나가는 중이다
        + ["mcp_call"]  # req-pending: 판정 중이다
        + ["command"]  # req-internal: 서버 내부 요청은 명령만 발행한다
    )
    transitions, beacons, approval = events[:5], events[5:8], events[8]
    run, pending, internal = events[9:20], events[20], events[21]
    assert [(e.fields["nodeId"], e.fields["to"]) for e in transitions] == [
        ("light-1", DeviceState.AVAILABLE), ("locker-1", DeviceState.ACTIVE), ("locker-2", DeviceState.FAULT),
        ("locker-3", DeviceState.CLOSING), ("orphan-1", DeviceState.AVAILABLE),
    ]
    assert dict(transitions[1].fields) == {"nodeId": "locker-1", "to": DeviceState.ACTIVE, "overdue": True}
    assert transitions[2].fields["fault"] == {"cause": ReasonCode.EXEC_UNKNOWN}
    assert "overdue" not in transitions[3].fields  # 반납 중인 세션은 ACTIVE 가 아니다
    assert {e.at for e in transitions} == {NOW}
    assert [(e.subject, e.fields["seen"], e.at) for e in beacons] == [
        (OWNER, False, NOW), (VISITOR, True, ago(1)), (VISITOR2, False, ago(20)),
    ]
    assert (approval.subject, approval.at, dict(approval.fields)) == (
        VISITOR, ago(100), {"requestId": "req-ap-pending", "nodeId": "locker-2", "kind": "requested"})
    assert {e.fields["requestId"] for e in run if "requestId" in e.fields} == {"req-run"}
    assert run[0].subject == VISITOR2 and run[-1].fields["action"] == Action.OPEN
    assert pending.subject == VISITOR2 and pending.fields["requestId"] == "req-pending"
    assert internal.subject is None and internal.fields["requestId"] == "req-internal"
    assert VISITOR_ID not in "".join(strings([dict(e.fields) for e in events]))


def test_FR12B_비콘은_최근_기준과_같은_수신까지_본_것이다():
    recent = Settings().beacon_recent_sec
    reads = FakeReads(beacons=[
        DashboardBeacon(VISITOR_ID, "beacon-1", ago(recent)),
        DashboardBeacon(VISITOR2_ID, "beacon-2", ago(recent + 0.001)),
    ])

    events = make_service(reads).current_events()

    assert [(e.subject, e.fields["seen"]) for e in events] == [(VISITOR, True), (VISITOR2, False)]


def test_FR12B_진행_중이_아닌_요청과_옛_요청은_지금_상태에_없다():
    reads = rich_world()

    events = make_service(reads).current_events()
    ids = {e.fields["requestId"] for e in events if "requestId" in e.fields and e.type != "approval"}

    # 끝난 요청, 접근 신청, 서버가 시작하기 전의 판정 중 행은 없다
    assert ids == {"req-run", "req-pending", "req-internal"}


def test_FR12B_끝난_서버_내부_요청은_진행_중이_아니다():
    reads = FakeReads(
        nodes=[make_node("light-1", capability=Capability.LIGHT, occupancy=Occupancy.SHARED)],
        requests=[make_request("req-internal", kind="server_internal", node_id="light-1", action=Action.OFF)],
        commands=[make_command("req-internal", 1, node_id="light-1", action=Action.OFF, outcome="succeeded",
                               started=ago(9), occurred=ago(8))],
        audits=trail("req-internal", ago(10), (Step.EXEC, {"event": "record_result", "outcome": "succeeded"})),
    )

    events = make_service(reads).current_events()

    assert [e.type for e in events] == ["transition"]


# ── 고장 원인 ─────────────────────────────────────────────────────────


def fault_row(payload: dict) -> DashboardAudit:
    return DashboardAudit(1, "log-1", None, None, Step.FAULT, payload, NOW)


@pytest.mark.parametrize(
    ("row", "cause"),
    [
        (None, ReasonCode.TARGET_UNAVAILABLE),
        (fault_row({"event": "record_result", "outcome": "unknown"}), ReasonCode.EXEC_UNKNOWN),
        (fault_row({"event": "record_result", "outcome": "failed"}), ReasonCode.EXEC_FAILED),
        (fault_row({"event": "record_result", "outcome": "rejected"}), ReasonCode.TARGET_UNAVAILABLE),  # 근거가 아니다
        (fault_row({"event": "record_result", "outcome": "succeeded"}), ReasonCode.TARGET_UNAVAILABLE),
        (fault_row({"event": "isolate_device", "node_id": "locker-1",
                    "evidence": {"source": "device_result", "state": NodeResultState.UNKNOWN.value}}),
         ReasonCode.EXEC_UNKNOWN),
        (fault_row({"event": "isolate_device", "node_id": "locker-1",
                    "evidence": {"source": "device_result", "state": NodeResultState.FAILED.value}}),
         ReasonCode.EXEC_FAILED),
        (fault_row({"event": "isolate_device", "node_id": "locker-1", "evidence": {"source": "sensor_fault"}}),
         ReasonCode.TARGET_UNAVAILABLE),
        (fault_row({"event": "isolate_device", "node_id": "locker-1"}), ReasonCode.TARGET_UNAVAILABLE),
        (fault_row({"event": "isolate_device", "node_id": "locker-1", "evidence": "text"}),
         ReasonCode.TARGET_UNAVAILABLE),
        (fault_row({"event": "settle_recovery"}), ReasonCode.TARGET_UNAVAILABLE),
    ],
)
def test_FR12B_고장_원인은_마지막_근거_행으로_정한다(row, cause):
    assert fault_of(row) == cause


# ── 읽기 규칙 ─────────────────────────────────────────────────────────

CALLS = {
    "places": lambda s: s.places(),
    "uses": lambda s: s.uses(),
    "approvals": lambda s: s.approvals(),
    "audit": lambda s: s.audit(10),
    "history": lambda s: s.history("today", None, None),
    "request": lambda s: s.request("req-ok"),
    "trust": lambda s: s.trust(),
    "rental": lambda s: s.rental("locker-1"),
    "health": lambda s: s.health(),
    "current_events": lambda s: s.current_events(),
}


@pytest.mark.parametrize("name", CALLS)
def test_FR12A_조회_중_DB_오류는_예외를_올린다(name):
    reads = rich_world()
    reads.error = sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        CALLS[name](make_service(reads))

    assert reads.opened == reads.closed == 1  # 오류가 나도 읽기 트랜잭션은 닫힌다


@pytest.mark.parametrize("name", CALLS)
def test_FR12A_조회_하나는_읽기_트랜잭션_하나다(name):
    reads = rich_world()
    service = make_service(reads, holders=[SharedHolder("light-1", VISITOR_ID, ago(30))],
                           models={"place-1": ModelVersions("model-1", "cal-1")})

    CALLS[name](service)  # 모델·연결·유지 대상자는 트랜잭션 밖에서 읽는다 (make_service 가 확인한다)

    assert reads.opened == reads.closed == 1


def test_FR12A_조회_서비스는_접점을_구현한다():
    service = make_service(FakeReads())

    assert isinstance(FakeReads(), DashboardReadStore)
    assert isinstance(service, DashboardQueries)
    assert isinstance(service, MonitorSnapshot)


def test_FR11_조회_결과와_이벤트에_사용자_ID가_없다(tmp_path):
    reads = rich_world()
    service = make_service(reads, holders=[SharedHolder("light-1", VISITOR_ID, ago(30))],
                           models={"place-1": ModelVersions("model-1", "cal-1")}, reports=tmp_path)
    results = [name for name in CALLS if name != "current_events"]

    texts = [s for name in results for s in strings(CALLS[name](service))]
    texts += [s for e in service.current_events() for s in strings(dict(e.fields))]

    assert texts
    assert not [s for s in texts if any(user_id in s for user_id in (VISITOR_ID, VISITOR2_ID, OWNER_ID))]


def test_FR12A_조회_결과가_형식_검사를_통과한다(tmp_path):
    (tmp_path / "report.csv").write_text(
        "# generated_at: 2026-10-20T01:00:00+00:00\nmetric,passed,trials,note\nece,True,12,\n", encoding="utf-8")
    service = make_service(rich_world(), holders=[SharedHolder("light-1", VISITOR_ID, ago(30))],
                           models={"place-1": ModelVersions("model-1", "cal-1")}, reports=tmp_path)
    names = PersonNames()
    data = {
        "places": service.places(), "uses": service.uses(), "approvals": service.approvals(),
        "audit": service.audit(10), "history": service.history("today", None, None),
        "request": service.request("req-ok"), "trust": service.trust(), "rental": service.rental("locker-1"),
        "health": service.health(),
    }

    assert data.keys() == RESPONSE_KINDS
    for kind, value in data.items():
        assert render_response(kind, value, names) is not None, kind
