"""`ExecutionStore` 7단계: `save_recovery`, `recovery`, `settle_recovery` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.schema import connect
from nearpilot.shared.enums import (
    Action,
    ApprovalUsageState,
    CommandOutcome,
    DeviceState,
    EndReason,
    ExecutionStatus,
    RecoveryStatus,
    SessionState,
    StoreConflictReason,
    StoreOutcome,
)
from nearpilot.shared.models import ApprovalUsageSettlement, RecoveryEvidence, RecoveryRecord, RecoverySettlement

from conftest import (
    insert_approval,
    insert_approval_usage,
    insert_node,
    insert_place,
    insert_policy,
    insert_request,
    insert_use_session,
    insert_user,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _seed_faulted_rental(conn, store):
    """ACTIVE 대여 중 명령 결과가 UNKNOWN으로 확정돼 기기가 FAULT로 격리된 상황."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE")
    insert_policy(conn, policy_id="policy1", node_id="node1")
    insert_approval(conn, approval_id="approval1", node_id="node1", internal_request_id="req1")
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval1", internal_request_id="req1")
    linked = store.link_command("req1", "node1", Action.OPEN)
    store.begin_send(linked.command.command_id)
    store.record_result(linked.command.command_id, CommandOutcome.UNKNOWN, _NOW, None, {"source": "test"})
    return linked.command.command_id


def _record(command_id):
    return RecoveryRecord(
        recovery_id="recovery1", node_id="node1", internal_request_ids=("req1",),
        command_ids=(command_id,), use_id="use1", created_at=_NOW, status=RecoveryStatus.READY,
        evidence=(
            RecoveryEvidence(
                evidence_id="ev1", session_id="connection-1", observed_at=_NOW,
                execution_outcome=CommandOutcome.UNKNOWN, command_idle=True,
            ),
        ),
    )


def _settlement(command_id, *, digest=_DIGEST_A):
    return RecoverySettlement(
        recovery_id="recovery1", node_id="node1", approved_by_user_id="u1", approved_at=_NOW,
        settlement_digest=digest, expected_node_state=DeviceState.FAULT,
        expected_recovery_status=RecoveryStatus.READY,
        expected_commands={command_id: ExecutionStatus.UNKNOWN},
        expected_sessions={"use1": SessionState.ACTIVE},
        evidence=_record(command_id).evidence,
        usage_settlements=(
            ApprovalUsageSettlement(
                usage_id="usage1", expected_state=ApprovalUsageState.HELD, state=ApprovalUsageState.CONSUMED
            ),
        ),
        use_id="use1", closed_at=_NOW + timedelta(minutes=1), end_reason=EndReason.FAULT,
    )


def test_NFR04_save_recovery_applied(store, conn):
    command_id = _seed_faulted_rental(conn, store)

    result = store.save_recovery(_record(command_id))

    assert result.outcome == StoreOutcome.APPLIED
    assert result.recovery.recovery_id == "recovery1"
    assert result.recovery.completion is None
    row = conn.execute("SELECT * FROM recovery WHERE recovery_id = 'recovery1'").fetchone()
    assert row["status"] == "ready"


def test_NFR04_save_recovery_idempotent(store, conn):
    command_id = _seed_faulted_rental(conn, store)
    first = store.save_recovery(_record(command_id))

    second = store.save_recovery(_record(command_id))

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    count = conn.execute("SELECT COUNT(*) AS n FROM recovery").fetchone()
    assert count["n"] == 1


def test_NFR04_save_recovery_conflict(store, conn):
    command_id = _seed_faulted_rental(conn, store)
    store.save_recovery(_record(command_id))
    different = RecoveryRecord(
        recovery_id="recovery1", node_id="node1", internal_request_ids=("req1", "req-other"),
        command_ids=(command_id,), use_id="use1", created_at=_NOW, status=RecoveryStatus.READY, evidence=(),
    )

    result = store.save_recovery(different)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.LINK_MISMATCH


def test_NFR04_recovery_lookup(store, conn):
    command_id = _seed_faulted_rental(conn, store)

    assert store.recovery("recovery1") is None

    store.save_recovery(_record(command_id))
    found = store.recovery("recovery1")

    assert found is not None
    assert found.recovery_id == "recovery1"
    assert len(found.evidence) == 1
    assert found.evidence[0].evidence_id == "ev1"


def test_NFR04_settle_recovery_applied(store, conn):
    command_id = _seed_faulted_rental(conn, store)
    store.save_recovery(_record(command_id))

    result = store.settle_recovery(_settlement(command_id))

    assert result.outcome == StoreOutcome.APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "AVAILABLE"
    session = conn.execute(
        "SELECT state, closed_at, end_reason FROM use_session WHERE use_id = 'use1'"
    ).fetchone()
    assert session["state"] == "CLOSED"
    assert session["end_reason"] == "fault"
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "consumed"
    recovery = store.recovery("recovery1")
    assert recovery.completion is not None
    assert recovery.completion.settlement.settlement_digest == _DIGEST_A


def test_NFR04_settle_recovery_idempotent(store, conn):
    command_id = _seed_faulted_rental(conn, store)
    store.save_recovery(_record(command_id))
    first = store.settle_recovery(_settlement(command_id))

    second = store.settle_recovery(_settlement(command_id))

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert second.recovery.completion is not None


def test_NFR04_settle_recovery_conflict(store, conn):
    command_id = _seed_faulted_rental(conn, store)
    store.save_recovery(_record(command_id))
    store.settle_recovery(_settlement(command_id))

    result = store.settle_recovery(_settlement(command_id, digest=_DIGEST_B))

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.CONTENT_CONFLICT


def test_NFR04_settle_recovery_state_conflict(store, conn):
    """정산 전에 기기 상태가 바뀌면(예상과 다르면) CONFLICT다."""
    command_id = _seed_faulted_rental(conn, store)
    store.save_recovery(_record(command_id))
    conn.execute("UPDATE node SET device_state = 'ACTIVE' WHERE node_id = 'node1'")

    result = store.settle_recovery(_settlement(command_id))

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.STATE_CONFLICT
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "ACTIVE"  # 바꾸지 않았다.


def test_NFR04_settle_recovery_shared_no_session(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared", device_state="FAULT")
    record = RecoveryRecord(
        recovery_id="recovery-light", node_id="light1", internal_request_ids=(), command_ids=(),
        use_id=None, created_at=_NOW, status=RecoveryStatus.READY, evidence=(),
    )
    store.save_recovery(record)
    settlement = RecoverySettlement(
        recovery_id="recovery-light", node_id="light1", approved_by_user_id="u1", approved_at=_NOW,
        settlement_digest=_DIGEST_A, expected_node_state=DeviceState.FAULT,
        expected_recovery_status=RecoveryStatus.READY, expected_commands={}, expected_sessions={},
        evidence=(), usage_settlements=(),
    )

    result = store.settle_recovery(settlement)

    assert result.outcome == StoreOutcome.APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'light1'").fetchone()
    assert node["device_state"] == "AVAILABLE"
    count = conn.execute("SELECT COUNT(*) AS n FROM use_session").fetchone()
    assert count["n"] == 0  # 공용형은 점유 세션을 만들거나 건드리지 않는다.


def test_NFR03_settle_recovery_concurrent_once(tmp_path):
    db_path = str(tmp_path / "settle.db")
    setup_conn = connect(db_path)
    setup_store = SqliteExecutionStore(setup_conn)
    command_id = _seed_faulted_rental(setup_conn, setup_store)
    setup_store.save_recovery(_record(command_id))
    setup_conn.close()

    outcomes: list[StoreOutcome] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            result = SqliteExecutionStore(conn).settle_recovery(_settlement(command_id))
            with lock:
                outcomes.append(result.outcome)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt), threading.Thread(target=attempt)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == sorted([StoreOutcome.APPLIED, StoreOutcome.ALREADY_APPLIED])
    check_conn = connect(db_path)
    usage = check_conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "consumed"  # 두 번 정산되지 않았다.
    check_conn.close()
