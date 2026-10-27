"""`SqliteDashboardReads` (specs/core-dashboard-queries/spec.md 2절·6절, FR-12A).

읽기 전용 접점이다. 데이터는 `conftest`의 SQL 도우미와 기존 저장소 메서드로 넣는다.
시각은 `T0`에서 몇 분 뒤인지로 쓴다 (`at(분)`은 datetime, `ts(분)`은 DB에 저장하는 문자열).
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.db.audit import SqliteAuditLog
from nearpilot.db.repositories.dashboard_reads import SqliteDashboardReads
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
from nearpilot.shared.interfaces import DashboardReadStore
from nearpilot.shared.models import (
    AuditEvent,
    DashboardApproval,
    DashboardAudit,
    DashboardBeacon,
    DashboardCommand,
    DashboardNode,
    DashboardPlace,
    DashboardRequest,
    DashboardRssi,
    DashboardSession,
)

from conftest import (
    add_policy_revision,
    insert_approval,
    insert_approval_usage,
    insert_audit_log,
    insert_beacon,
    insert_node,
    insert_node_command,
    insert_place,
    insert_policy,
    insert_request,
    insert_use_session,
    insert_user,
)

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
CURSOR_TABLES = ("request", "audit_log", "rssi_observation")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ts(minutes: float) -> str:
    return at(minutes).isoformat()


@pytest.fixture
def reads(conn):
    return SqliteDashboardReads(conn)


def insert_rssi(conn, seq, node_id, beacon_id, rssi, received_minutes):
    received_at = ts(received_minutes)
    conn.execute(
        "INSERT INTO rssi_observation (obs_id, node_id, beacon_id, rssi, frame_ts, received_at, sent_at, "
        "session_id, seq) VALUES (?, ?, ?, ?, ?, ?, ?, 'session-1', ?)",
        (f"obs-{seq}", node_id, beacon_id, rssi, received_at, received_at, received_at, seq),
    )


def test_FR12A_대시보드_읽기_장소와_기기(reads, conn):
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    # 일부러 ID 순서와 다르게 넣는다
    insert_place(conn, place_id="place-b", owner_user_id="owner1", name="카페")
    insert_place(conn, place_id="place-a", owner_user_id="owner1", name="실험실")
    insert_node(conn, node_id="node-c", place_id=None, capability="light", occupancy="shared",
                trust_state="discovered")
    insert_node(conn, node_id="node-b", place_id="place-b", capability="light", occupancy="shared",
                device_state="ACTIVE")
    insert_node(conn, node_id="node-a", place_id="place-a", device_state="RESERVED",
                session_id="session-a", last_seen_at=ts(5))
    conn.execute("UPDATE node SET name = '사물함 1' WHERE node_id = 'node-a'")

    assert isinstance(reads, DashboardReadStore)
    assert reads.places() == [DashboardPlace("place-a", "실험실"), DashboardPlace("place-b", "카페")]
    # 시각은 시간대가 있는 datetime이다 (naive datetime과는 == 이 거짓이다)
    assert reads.nodes() == [
        DashboardNode("node-a", "사물함 1", "place-a", Capability.LOCKER, Occupancy.RENTAL,
                      TrustState.TRUSTED, DeviceState.RESERVED, at(5), "session-a"),
        DashboardNode("node-b", None, "place-b", Capability.LIGHT, Occupancy.SHARED,
                      TrustState.TRUSTED, DeviceState.ACTIVE, None, None),
        DashboardNode("node-c", None, None, Capability.LIGHT, Occupancy.SHARED,
                      TrustState.DISCOVERED, DeviceState.AVAILABLE, None, None),
    ]


def test_FR12A_대시보드_읽기_계정_역할(reads, conn):
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    insert_user(conn, user_id="visitor1", token_hash="hash-visitor1", role="visitor")

    roles = reads.roles(["visitor1", "ghost", "owner1", "visitor1"])

    assert roles == {"owner1": Role.OWNER, "visitor1": Role.VISITOR}  # 없는 ID는 빠지고 같은 ID는 한 번이다
    assert reads.roles(iter(["ghost"])) == {}


def _add_session(conn, use_id, node_id, user_id, state, *, received, reserved, due=None, closed=None):
    insert_request(conn, internal_request_id=f"req-{use_id}", user_id=user_id, request_id=f"ext-{use_id}",
                   node_id=node_id, received_at=ts(received))
    insert_use_session(conn, use_id=use_id, internal_request_id=f"req-{use_id}", user_id=user_id,
                       node_id=node_id, state=state, rental_due_at=due, closed_at=closed)
    conn.execute("UPDATE use_session SET reserved_at = ? WHERE use_id = ?", (reserved, use_id))


def test_FR12A_대시보드_읽기_점유와_승인(reads, repo, conn):
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    for number in (1, 2, 3):
        insert_user(conn, user_id=f"visitor{number}", token_hash=f"hash-visitor{number}")
    insert_place(conn, owner_user_id="owner1")
    for number in (1, 2, 3, 4):
        insert_node(conn, node_id=f"locker-{number}")

    # 끝난 세션은 빠지고, 같은 기기의 세션은 reserved_at 순이다 (use_id 순이 아니다).
    _add_session(conn, "use-closed", "locker-1", "visitor1", "CLOSED",
                 received=0, reserved=ts(0), due=ts(60), closed=ts(30))
    _add_session(conn, "use-reserved", "locker-1", "visitor2", "RESERVED", received=3, reserved=ts(3))
    _add_session(conn, "use-z-first", "locker-2", "visitor2", "ACTIVE",
                 received=1, reserved=ts(1.5), due=ts(61))  # 요청을 받은 시각과 예약 시각이 다르다
    _add_session(conn, "use-a-second", "locker-2", "visitor3", "RESERVED", received=5, reserved=ts(5))
    _add_session(conn, "use-closing", "locker-3", "visitor3", "CLOSING",
                 received=2, reserved=ts(2), due=ts(62))
    _add_session(conn, "use-legacy", "locker-4", "visitor1", "ACTIVE", received=4, reserved=None)  # 0003 이전 행

    assert reads.open_sessions() == [
        DashboardSession("use-reserved", "req-use-reserved", "visitor2", "locker-1", SessionState.RESERVED,
                         at(3), None, at(3)),
        DashboardSession("use-z-first", "req-use-z-first", "visitor2", "locker-2", SessionState.ACTIVE,
                         at(1.5), at(61), at(1)),
        DashboardSession("use-a-second", "req-use-a-second", "visitor3", "locker-2", SessionState.RESERVED,
                         at(5), None, at(5)),
        DashboardSession("use-closing", "req-use-closing", "visitor3", "locker-3", SessionState.CLOSING,
                         at(2), at(62), at(2)),
        DashboardSession("use-legacy", "req-use-legacy", "visitor1", "locker-4", SessionState.ACTIVE,
                         None, None, at(4)),
    ]

    # 승인. 정책 리비전은 승인이 따르는 버전(policy_version)에서 읽는다 — 현재 버전이 아니다.
    insert_policy(conn, policy_id="pol-1", node_id="locker-1", approver_user_id="owner1",
                  approval_mode="one_time", max_uses=None)
    insert_policy(conn, policy_id="pol-2", node_id="locker-2", approver_user_id="owner1",
                  approval_mode="time_window", max_uses=3)
    add_policy_revision(conn, policy_id="pol-2", version=2, approver_user_id="owner1",
                        approval_mode="one_time", max_uses=1)  # 현재 버전은 2다
    for approval_id, node_id, requester, version, status, created, pending, valid, used in (
        ("ap-denied", "locker-1", "visitor3", 1, "denied", 10, 20, None, 0),
        ("ap-pending", "locker-1", "visitor1", 1, "pending", 0, 10, None, 0),
        ("ap-valid", "locker-2", "visitor2", 1, "approved", 5, 15, 65, 2),
        ("ap-exhausted", "locker-2", "visitor3", 1, "approved", 5, 15, 65, 3),  # created_at이 같으면 ID 순
        ("ap-held", "locker-2", "visitor1", 1, "approved", 5, 15, 65, 1),
        ("ap-one-time", "locker-2", "visitor1", 2, "approved", 6, 16, 66, 0),
    ):
        insert_approval(
            conn, approval_id=approval_id, node_id=node_id, requester_user_id=requester,
            policy_id="pol-1" if node_id == "locker-1" else "pol-2", policy_version=version, status=status,
            created_at=ts(created), pending_until=ts(pending), valid_until=ts(valid) if valid else None,
            used_count=used, internal_request_id=f"req-{approval_id}",
        )
    # 남은 횟수는 예약·보류·소비한 사용분만 센다 (반환한 것은 세지 않는다)
    for usage_id, approval_id, state in (
        ("u-1", "ap-valid", "reserved"), ("u-2", "ap-valid", "reserved"), ("u-3", "ap-valid", "released"),
        ("u-4", "ap-exhausted", "reserved"), ("u-5", "ap-exhausted", "held"),
        ("u-6", "ap-exhausted", "consumed"), ("u-7", "ap-exhausted", "consumed"),  # 한도(3)보다 많다
        ("u-8", "ap-held", "held"),
    ):
        insert_approval_usage(conn, usage_id, approval_id, f"req-{usage_id}", state=state)

    approvals = reads.approvals()

    # one_time 은 max_uses 가 없어도 한도가 1이다 (ap-pending, ap-denied)
    assert approvals == [
        DashboardApproval("ap-pending", "locker-1", "visitor1", "req-ap-pending", Action.OPEN,
                          ApprovalStatus.PENDING, at(0), at(10), None, ApprovalMode.ONE_TIME, 0, 1),
        DashboardApproval("ap-exhausted", "locker-2", "visitor3", "req-ap-exhausted", Action.OPEN,
                          ApprovalStatus.APPROVED, at(5), at(15), at(65), ApprovalMode.TIME_WINDOW, 3, 0),
        DashboardApproval("ap-held", "locker-2", "visitor1", "req-ap-held", Action.OPEN,
                          ApprovalStatus.APPROVED, at(5), at(15), at(65), ApprovalMode.TIME_WINDOW, 1, 2),
        DashboardApproval("ap-valid", "locker-2", "visitor2", "req-ap-valid", Action.OPEN,
                          ApprovalStatus.APPROVED, at(5), at(15), at(65), ApprovalMode.TIME_WINDOW, 2, 1),
        DashboardApproval("ap-one-time", "locker-2", "visitor1", "req-ap-one-time", Action.OPEN,
                          ApprovalStatus.APPROVED, at(6), at(16), at(66), ApprovalMode.ONE_TIME, 0, 1),
        DashboardApproval("ap-denied", "locker-1", "visitor3", "req-ap-denied", Action.OPEN,
                          ApprovalStatus.DENIED, at(10), at(20), None, ApprovalMode.ONE_TIME, 0, 1),
    ]
    # 남은 횟수는 Repository 가 정책 검사에 쓰는 값과 같은 규칙이다
    for approval in approvals:
        [record] = [
            record for record in repo.approvals_in_scope(approval.requester_user_id, approval.node_id)
            if record.approval_id == approval.approval_id
        ]
        assert approval.remaining_uses == record.remaining_uses, approval.approval_id


def _add_request(conn, internal_request_id, minutes, **kwargs):
    insert_request(conn, internal_request_id=internal_request_id, request_id=f"ext-{internal_request_id}",
                   received_at=ts(minutes), **kwargs)


def test_FR12A_대시보드_읽기_요청_기간과_커서(reads, conn):
    assert reads.cursors() == {table: 0 for table in CURSOR_TABLES}  # 빈 DB는 모두 0이다

    insert_user(conn, user_id="visitor1", token_hash="hash-visitor1")
    insert_place(conn, owner_user_id="visitor1")
    insert_node(conn, node_id="locker-1")
    # rowid 순서(b, a, c, internal)가 시각 순서와 다르다
    _add_request(conn, "req-b", 10, user_id="visitor1")
    _add_request(conn, "req-a", 0, user_id="visitor1", node_id="locker-1", verdict="OK", reason_code="OK",
                 p_max=0.91)
    conn.execute("UPDATE request SET decision_context = ? WHERE internal_request_id = 'req-a'",
                 (json.dumps({"posteriors": {"locker-1": 0.91}, "message": "열었다"}),))
    _add_request(conn, "req-c", 20, user_id="visitor1", target_spec="nearest:locker", action="on",
                 verdict="DENY", reason_code="POLICY_DENIED")
    _add_request(conn, "req-internal", 30, user_id="visitor1", action="off")
    conn.execute("UPDATE request SET user_id = NULL, request_kind = 'server_internal', request_id = NULL "
                 "WHERE internal_request_id = 'req-internal'")
    seq = dict(conn.execute("SELECT internal_request_id, rowid FROM request").fetchall())
    assert seq == {"req-b": 1, "req-a": 2, "req-c": 3, "req-internal": 4}

    request_b = DashboardRequest(1, "req-b", "visitor1", "visitor", "nearest:locker", Action.OPEN,
                                 None, None, None, None, at(10))
    request_a = DashboardRequest(2, "req-a", "visitor1", "visitor", "nearest:locker", Action.OPEN,
                                 Verdict.OK, ReasonCode.OK, "locker-1",
                                 {"posteriors": {"locker-1": 0.91}, "message": "열었다"}, at(0))
    request_c = DashboardRequest(3, "req-c", "visitor1", "visitor", "nearest:locker", Action.ON,
                                 Verdict.DENY, ReasonCode.POLICY_DENIED, None, None, at(20))
    request_internal = DashboardRequest(4, "req-internal", None, "server_internal", "nearest:locker",
                                        Action.OFF, None, None, None, None, at(30))

    # received_at 내림차순, 구간은 [since, until)
    assert reads.requests(None, None) == [request_internal, request_c, request_b, request_a]
    assert reads.requests(at(10), None) == [request_internal, request_c, request_b]  # since 를 포함한다
    assert reads.requests(None, at(20)) == [request_b, request_a]  # until 은 뺀다
    assert reads.requests(at(10), at(30)) == [request_c, request_b]
    assert reads.requests(at(21), at(29)) == []
    assert reads.requests(at(20), at(20)) == []
    kst = timezone(timedelta(hours=9))
    assert reads.requests(at(10).astimezone(kst), at(20).astimezone(kst)) == [request_b]  # 시간대와 무관하다

    assert reads.request("req-a") == request_a
    assert reads.request("req-internal") == request_internal
    assert reads.request("missing") is None
    # 판정 전의 요청은 verdict·decision_context 가 없다
    assert reads.request("req-b").verdict is None and reads.request("req-b").decision_context is None

    assert reads.requests_after(0) == [request_b, request_a, request_c, request_internal]  # rowid 순
    assert reads.requests_after(2) == [request_c, request_internal]
    assert reads.requests_after(4) == []
    assert reads.requests_by_ids(["req-internal", "req-b", "req-b", "missing"]) == [request_b, request_internal]

    assert reads.cursors() == {"request": 4, "audit_log": 0, "rssi_observation": 0}
    # 정리로 행이 지워져도 커서는 행 수가 아니라 가장 큰 rowid 다
    conn.execute("DELETE FROM request WHERE internal_request_id = 'req-a'")
    assert reads.cursors()["request"] == 4
    assert [row.internal_request_id for row in reads.requests_after(0)] == ["req-b", "req-c", "req-internal"]


def test_FR12A_대시보드_읽기_명령_감사_RSSI_비콘(reads, conn):
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    for number in (1, 2, 3):
        insert_user(conn, user_id=f"visitor{number}", token_hash=f"hash-visitor{number}")
    insert_place(conn, owner_user_id="owner1")
    insert_node(conn, node_id="locker-1")
    insert_node(conn, node_id="light-1", capability="light", occupancy="shared")
    _add_request(conn, "req-1", 0, user_id="visitor1", node_id="locker-1")
    _add_request(conn, "req-2", 1, user_id="visitor2", node_id="light-1", action="on")
    insert_use_session(conn, use_id="use-1", internal_request_id="req-1", user_id="visitor1", node_id="locker-1")

    # 명령: rowid 순서(z, a, m)가 command_id 순서와 다르다
    insert_node_command(conn, command_id="cmd-z", internal_request_id="req-1", node_id="locker-1",
                        status="in_progress", execution_kind="rental_initial", use_id="use-1",
                        send_started_at=ts(0.5), send_attempted=1)
    insert_node_command(conn, command_id="cmd-a", internal_request_id="req-2", node_id="light-1", action="on",
                        status="not_sent", execution_kind="shared", outcome="not_sent")
    insert_node_command(conn, command_id="cmd-m", internal_request_id="req-1", node_id="locker-1",
                        status="succeeded", execution_kind="rental_reoperation", use_id="use-1",
                        send_started_at=ts(2), outcome="succeeded", occurred_at=ts(2.5))
    conn.execute("UPDATE node_command SET evidence = ? WHERE command_id = 'cmd-m'",
                 (json.dumps({"state": "succeeded", "detail": {"door": "open"}}),))
    command_z = DashboardCommand(1, "cmd-z", "req-1", "locker-1", Action.OPEN, ExecutionStatus.IN_PROGRESS,
                                 "use-1", at(0.5), None, None, {})
    command_a = DashboardCommand(2, "cmd-a", "req-2", "light-1", Action.ON, ExecutionStatus.NOT_SENT,
                                 None, None, "not_sent", None, {})
    command_m = DashboardCommand(3, "cmd-m", "req-1", "locker-1", Action.OPEN, ExecutionStatus.SUCCEEDED,
                                 "use-1", at(2), "succeeded", at(2.5),
                                 {"state": "succeeded", "detail": {"door": "open"}})

    assert reads.commands_of(["req-1"]) == [command_z, command_m]
    assert reads.commands_of(["req-2", "req-1", "req-1", "missing"]) == [command_z, command_a, command_m]
    assert reads.commands_of_use("use-1") == [command_z, command_m]
    assert reads.commands_of_use("missing") == []
    assert reads.commands_by_ids(["cmd-m", "cmd-z", "cmd-z", "missing"]) == [command_z, command_m]

    # 감사: 요청이 없는 행(관리 기록)도 있다
    insert_audit_log(conn, log_id="log-1", internal_request_id="req-1", step="0",
                     payload={"event": "link_request"}, created_at=ts(0))
    insert_audit_log(conn, log_id="log-2", internal_request_id="req-1", step="1",
                     payload={"result": "OK"}, created_at=ts(0.1))
    insert_audit_log(conn, log_id="log-3", internal_request_id="req-2", step="4",
                     payload={"reason": "APPROVAL_REQUIRED"}, created_at=ts(1))
    insert_audit_log(conn, log_id="log-4", step="admin", created_at=ts(2),
                     payload={"event": "set_node_name", "node_id": "locker-1", "name": "사물함 1"})
    insert_audit_log(conn, log_id="log-5", internal_request_id="req-1", command_id="cmd-m", step="exec",
                     payload={"result": "succeeded"}, created_at=ts(2.5))
    audit = {
        1: DashboardAudit(1, "log-1", "req-1", None, Step.DEDUP, {"event": "link_request"}, at(0)),
        2: DashboardAudit(2, "log-2", "req-1", None, Step.BEACON, {"result": "OK"}, at(0.1)),
        3: DashboardAudit(3, "log-3", "req-2", None, Step.POLICY, {"reason": "APPROVAL_REQUIRED"}, at(1)),
        4: DashboardAudit(4, "log-4", None, None, Step.ADMIN,
                          {"event": "set_node_name", "node_id": "locker-1", "name": "사물함 1"}, at(2)),
        5: DashboardAudit(5, "log-5", "req-1", "cmd-m", Step.EXEC, {"result": "succeeded"}, at(2.5)),
    }

    assert reads.audit_recent(2) == [audit[5], audit[4]]  # rowid 내림차순, limit 개
    assert reads.audit_recent(100) == [audit[5], audit[4], audit[3], audit[2], audit[1]]
    assert reads.audit_recent(0) == [] and reads.audit_recent(-1) == []  # 음수 LIMIT 는 "제한 없음"이 아니다
    assert reads.audit_after(0) == [audit[1], audit[2], audit[3], audit[4], audit[5]]
    assert reads.audit_after(3) == [audit[4], audit[5]]
    assert reads.audit_after(5) == []
    assert reads.audit_of(["req-1"]) == [audit[1], audit[2], audit[5]]
    assert reads.audit_of(["req-2", "req-1", "req-1"]) == [audit[1], audit[2], audit[3], audit[5]]
    assert reads.audit_of(["missing"]) == []

    # RSSI와 비콘: 비활성 비콘의 관측도 rssi_after 에 있지만 활성 비콘의 수신 시각에는 들어가지 않는다
    insert_beacon(conn, beacon_id="beacon-1", user_id="visitor1", uuid="UUID-1", major=1, minor=1)
    insert_beacon(conn, beacon_id="beacon-2-old", user_id="visitor2", uuid="UUID-2", major=1, minor=1,
                  is_active=0)
    insert_beacon(conn, beacon_id="beacon-2", user_id="visitor2", uuid="UUID-2", major=1, minor=2)
    insert_beacon(conn, beacon_id="beacon-3", user_id="visitor3", uuid="UUID-3", major=1, minor=1)
    insert_rssi(conn, 1, "locker-1", "beacon-1", -60.5, 15)
    insert_rssi(conn, 2, "light-1", "beacon-2", -70.0, 12)
    insert_rssi(conn, 3, "locker-1", "beacon-1", -58.0, 10)  # 마지막 rowid 가 가장 늦은 시각은 아니다
    insert_rssi(conn, 4, "light-1", "beacon-2-old", -80.0, 20)

    assert reads.rssi_after(0) == [
        DashboardRssi(1, "locker-1", "visitor1", -60.5, at(15)),
        DashboardRssi(2, "light-1", "visitor2", -70.0, at(12)),
        DashboardRssi(3, "locker-1", "visitor1", -58.0, at(10)),
        DashboardRssi(4, "light-1", "visitor2", -80.0, at(20)),
    ]
    assert reads.rssi_after(2) == [
        DashboardRssi(3, "locker-1", "visitor1", -58.0, at(10)),
        DashboardRssi(4, "light-1", "visitor2", -80.0, at(20)),
    ]
    assert reads.rssi_after(4) == []
    assert reads.active_beacons() == [
        DashboardBeacon("visitor1", "beacon-1", at(15)),
        DashboardBeacon("visitor2", "beacon-2", at(12)),
        DashboardBeacon("visitor3", "beacon-3", None),  # 관측이 없다
    ]

    assert reads.cursors() == {"request": 2, "audit_log": 5, "rssi_observation": 4}


def test_FR12A_대시보드_읽기_고장_근거는_기기마다_마지막_행이다(reads, conn):
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    insert_user(conn, user_id="visitor1", token_hash="hash-visitor1")
    insert_place(conn, owner_user_id="owner1")
    for node_id in ("locker-1", "locker-2", "locker-3", "locker-4", "locker-5", "locker-6"):
        insert_node(conn, node_id=node_id)
    # 기기를 요청으로 찾는 행의 요청과, 명령으로 찾는 행의 명령 (요청의 기기는 비워 두어 경로를 가른다)
    _add_request(conn, "req-1", 3, user_id="visitor1", node_id="locker-1")
    _add_request(conn, "req-2", 0, user_id="visitor1", node_id="locker-2")
    _add_request(conn, "req-3", 1, user_id="visitor1")
    insert_node_command(conn, command_id="cmd-3", internal_request_id="req-3", node_id="locker-3")
    _add_request(conn, "req-4", 2, user_id="visitor1", node_id="locker-4")
    _add_request(conn, "req-5", 4, user_id="visitor1", node_id="locker-5")

    def isolate(log_id, node_id, minutes):
        insert_audit_log(conn, log_id=log_id, step="fault", created_at=ts(minutes),
                         payload={"event": "isolate_device", "node_id": node_id, "evidence": {"why": log_id}})

    def result(log_id, outcome, minutes, **ids):
        insert_audit_log(conn, log_id=log_id, step="exec", created_at=ts(minutes), **ids,
                         payload={"event": "record_result", "outcome": outcome, "evidence": {"why": log_id}})

    isolate("log-1a", "locker-1", 10)  # payload 의 기기
    result("log-2", "unknown", 11, internal_request_id="req-2")  # 요청의 기기
    result("log-3", "failed", 12, internal_request_id=None, command_id="cmd-3")  # 명령의 기기
    result("log-4", "succeeded", 13, internal_request_id="req-4")  # 성공은 근거가 아니다
    insert_audit_log(conn, log_id="log-5", internal_request_id="req-4", step="exec", created_at=ts(14),
                     payload={"event": "link_request", "node_id": "locker-5"})  # 근거 행의 event 가 아니다
    result("log-6", "failed", 15, internal_request_id="req-2")  # locker-2 의 더 늦은 행
    result("log-1b", "failed", 5, internal_request_id="req-4")  # locker-4, 시각은 일러도 rowid 는 뒤다
    isolate("log-1c", "locker-1", 1)  # locker-1 의 마지막 근거, 시각은 가장 이르다
    # 거절된 결과는 기기를 격리하지 않으므로 근거가 아니다: 격리 뒤의 거절 행은 앞의 근거를 가리지 못하고,
    # 거절만 있는 기기는 근거가 없다
    result("log-8", "rejected", 17, internal_request_id="req-1")
    result("log-9", "rejected", 18, internal_request_id="req-5")
    isolate("log-7", "locker-6", 16)  # 묻지 않은 기기

    evidence = reads.fault_evidence(["locker-1", "locker-2", "locker-3", "locker-4", "locker-5", "missing"])

    assert evidence.keys() == {"locker-1", "locker-2", "locker-3", "locker-4"}
    assert {node_id: (row.log_id, row.step) for node_id, row in evidence.items()} == {
        "locker-1": ("log-1c", Step.FAULT),
        "locker-2": ("log-6", Step.EXEC),
        "locker-3": ("log-3", Step.EXEC),
        "locker-4": ("log-1b", Step.EXEC),
    }
    row = evidence["locker-2"]
    assert row == DashboardAudit(row.seq, "log-6", "req-2", None, Step.EXEC,
                                 {"event": "record_result", "outcome": "failed", "evidence": {"why": "log-6"}},
                                 at(15))
    assert evidence["locker-3"].command_id == "cmd-3" and evidence["locker-3"].internal_request_id is None
    # 같은 기기를 두 번 물어도, 같은 값을 두 번 넘겨도 같다
    assert reads.fault_evidence(["locker-1", "locker-1"]) == {"locker-1": evidence["locker-1"]}
    assert reads.fault_evidence(["locker-6"]).keys() == {"locker-6"}
    assert reads.fault_evidence(["locker-5", "missing"]) == {}


def test_FR12A_대시보드_읽기_모르는_감사_단계는_ValueError(reads, conn):
    insert_user(conn, user_id="visitor1", token_hash="hash-visitor1")
    _add_request(conn, "req-1", 0, user_id="visitor1")
    insert_audit_log(conn, log_id="log-ok", internal_request_id="req-1", step="1")
    insert_audit_log(conn, log_id="log-bad", internal_request_id="req-1", step="mystery")

    # 행을 조용히 버리지 않고 알린다
    for read in (lambda: reads.audit_recent(10), lambda: reads.audit_after(0),
                 lambda: reads.audit_of(["req-1"])):
        with pytest.raises(ValueError):
            read()


def test_FR12A_대시보드_읽기_저장소가_쓰는_감사_단계는_모두_읽힌다(reads, conn):
    audit_log = SqliteAuditLog(conn)
    for index, step in enumerate(Step):
        audit_log.append(AuditEvent(event_id=f"event-{index}", step=step, payload={"index": index}))

    assert [row.step for row in reads.audit_after(0)] == list(Step)


def _populate(conn):
    """모든 표에 행을 하나씩 넣는다."""
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    insert_user(conn, user_id="visitor1", token_hash="hash-visitor1")
    insert_place(conn, owner_user_id="owner1")
    insert_node(conn, node_id="locker-1")
    insert_policy(conn, policy_id="pol-1", node_id="locker-1", approver_user_id="owner1")
    insert_approval(conn, approval_id="ap-1", node_id="locker-1", requester_user_id="visitor1",
                    policy_id="pol-1", pending_until=ts(10))
    _add_session(conn, "use-1", "locker-1", "visitor1", "ACTIVE", received=0, reserved=ts(0), due=ts(60))
    insert_node_command(conn, command_id="cmd-1", internal_request_id="req-use-1", node_id="locker-1",
                        use_id="use-1")
    insert_audit_log(conn, log_id="log-1", internal_request_id="req-use-1", step="0")
    insert_beacon(conn, beacon_id="beacon-1", user_id="visitor1")
    insert_rssi(conn, 1, "locker-1", "beacon-1", -60.0, 1)


def test_FR12A_대시보드_읽기는_쓰지_않는다(reads, conn):
    _populate(conn)
    calls = {
        "places": lambda: reads.places(),
        "nodes": lambda: reads.nodes(),
        "roles": lambda: reads.roles(["visitor1"]),
        "open_sessions": lambda: reads.open_sessions(),
        "approvals": lambda: reads.approvals(),
        "requests": lambda: reads.requests(None, None),
        "request": lambda: reads.request("req-use-1"),
        "requests_after": lambda: reads.requests_after(0),
        "requests_by_ids": lambda: reads.requests_by_ids(["req-use-1"]),
        "commands_of": lambda: reads.commands_of(["req-use-1"]),
        "commands_of_use": lambda: reads.commands_of_use("use-1"),
        "commands_by_ids": lambda: reads.commands_by_ids(["cmd-1"]),
        "audit_recent": lambda: reads.audit_recent(10),
        "audit_after": lambda: reads.audit_after(0),
        "audit_of": lambda: reads.audit_of(["req-use-1"]),
        "fault_evidence": lambda: reads.fault_evidence(["locker-1"]),
        "rssi_after": lambda: reads.rssi_after(0),
        "active_beacons": lambda: reads.active_beacons(),
        "cursors": lambda: reads.cursors(),
        "ping": lambda: reads.ping(),
    }
    # 접점에 메서드가 늘면 이 시험도 함께 늘려야 한다
    protocol_methods = {
        name for name, member in vars(DashboardReadStore).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }
    assert set(calls) == protocol_methods
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    changes_before = conn.total_changes

    for call in calls.values():
        call()

    conn.set_trace_callback(None)
    assert conn.total_changes == changes_before
    assert not conn.in_transaction  # 트랜잭션은 부르는 쪽이 연다
    assert len(statements) >= len(calls)  # 추적이 실제로 걸렸다
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements), statements


def test_FR12A_대시보드_읽기_빈_ID는_질의하지_않는다(reads, conn):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    assert reads.roles([]) == {}
    assert reads.requests_by_ids([]) == []
    assert reads.commands_of(iter(())) == []
    assert reads.commands_by_ids(()) == []
    assert reads.audit_of([]) == []
    assert reads.fault_evidence([]) == {}

    conn.set_trace_callback(None)
    assert statements == []


def test_FR12A_대시보드_읽기_ping은_연결이_끊기면_sqlite3_Error(reads, conn):
    assert reads.ping() is None

    conn.close()

    with pytest.raises(sqlite3.Error):
        reads.ping()
