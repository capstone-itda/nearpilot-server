"""`ExecutionStore` 2단계: `begin_send`, `cancel_not_sent` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.schema import connect
from nearpilot.shared.enums import (
    Action,
    ExecutionStatus,
    ReasonCode,
    StoreConflictReason,
    StoreOutcome,
)

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


def _seed_initial(conn, *, device_state="RESERVED", session_state="RESERVED"):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, device_state=device_state)
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state=session_state)


def _seed_reoperation(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(conn, internal_request_id="req-initial")
    insert_use_session(conn, use_id="use1", internal_request_id="req-initial", state="ACTIVE")
    insert_request(conn, internal_request_id="req-reop", request_id="ext-2")


def test_FR19_begin_send_applied(store, conn):
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)

    result = store.begin_send(linked.command.command_id)

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command.status == ExecutionStatus.IN_PROGRESS
    assert result.command.send_attempted is True
    assert result.command.send_started_at is not None


def test_FR19_begin_send_idempotent(store, conn):
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)
    first = store.begin_send(linked.command.command_id)

    second = store.begin_send(linked.command.command_id)

    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert second.command.status == ExecutionStatus.IN_PROGRESS
    assert second.command.send_started_at == first.command.send_started_at


def test_FR19_begin_send_command_in_progress(store, conn):
    _seed_reoperation(conn)
    command_a = store.link_command("req-reop", "node1", Action.OPEN).command
    store.begin_send(command_a.command_id)
    insert_request(conn, internal_request_id="req-reop-2", request_id="ext-3")
    command_b = store.link_command("req-reop-2", "node1", Action.ON).command

    result = store.begin_send(command_b.command_id)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.COMMAND_IN_PROGRESS


def test_FR19_begin_send_state_conflict(store, conn):
    _seed_initial(conn, device_state="CLOSING", session_state="CLOSING")
    linked = store.link_command("req1", "node1", Action.OPEN)

    result = store.begin_send(linked.command.command_id)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.STATE_CONFLICT


def test_FR19_begin_send_after_cancel_conflict(store, conn):
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)
    store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})

    result = store.begin_send(linked.command.command_id)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.STATE_CONFLICT


def test_NFR03_begin_send_cancel_race(tmp_path):
    db_path = str(tmp_path / "race.db")
    setup_conn = connect(db_path)
    _seed_reoperation(setup_conn)
    command_id = SqliteExecutionStore(setup_conn).link_command("req-reop", "node1", Action.OPEN).command.command_id
    setup_conn.close()

    outcomes: list[StoreOutcome] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def run_begin_send() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            result = SqliteExecutionStore(conn).begin_send(command_id)
            with lock:
                outcomes.append(result.outcome)
        finally:
            conn.close()

    def run_cancel() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            result = SqliteExecutionStore(conn).cancel_not_sent(
                "req-reop", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "race"}
            )
            with lock:
                outcomes.append(result.outcome)
        finally:
            conn.close()

    threads = [threading.Thread(target=run_begin_send), threading.Thread(target=run_cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == sorted([StoreOutcome.APPLIED, StoreOutcome.CONFLICT])
    check_conn = connect(db_path)
    row = check_conn.execute("SELECT status FROM node_command WHERE command_id = ?", (command_id,)).fetchone()
    assert row["status"] in (ExecutionStatus.IN_PROGRESS.value, ExecutionStatus.NOT_SENT.value)
    check_conn.close()


def test_FR19_cancel_not_sent_without_command(store, conn):
    _seed_initial(conn)

    result = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command is None
    session = conn.execute("SELECT state, end_reason FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "CLOSED"
    assert session["end_reason"] == "cancelled"
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "AVAILABLE"

    again = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})
    assert again.outcome == StoreOutcome.ALREADY_APPLIED
    assert again.command is None


def test_FR19_link_command_after_cancel_conflict(store, conn):
    _seed_initial(conn)
    store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})

    result = store.link_command("req1", "node1", Action.OPEN)

    assert result.outcome == StoreOutcome.CONFLICT
    count = conn.execute("SELECT COUNT(*) AS n FROM node_command WHERE internal_request_id = 'req1'").fetchone()
    assert count["n"] == 0


def test_FR19_cancel_not_sent_idempotent(store, conn):
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)
    first = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})

    second = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert second.command.command_id == linked.command.command_id
    assert second.command.status == ExecutionStatus.NOT_SENT


def test_FR19_cancel_not_sent_state_conflict(store, conn):
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)
    store.begin_send(linked.command.command_id)

    result = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.STATE_CONFLICT
    assert result.command.status == ExecutionStatus.IN_PROGRESS


def test_FR19_cancel_not_sent_initial_rental(store, conn):
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)

    result = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"})

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command.status == ExecutionStatus.NOT_SENT
    session = conn.execute("SELECT state, end_reason FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "CLOSED"
    assert session["end_reason"] == "cancelled"
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "AVAILABLE"
    assert linked.command.command_id == result.command.command_id


def test_FR19_cancel_not_sent_reoperation_scope(store, conn):
    _seed_reoperation(conn)
    insert_policy(conn, policy_id="policy1", node_id="node1")
    insert_approval(conn, approval_id="approval1", node_id="node1", internal_request_id="req-reop")
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval1", internal_request_id="req-reop")
    linked = store.link_command("req-reop", "node1", Action.OPEN)

    result = store.cancel_not_sent(
        "req-reop", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, {"source": "test"}
    )

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command.command_id == linked.command.command_id
    # 기존 대여의 점유와 기기 상태는 그대로다.
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "ACTIVE"
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "ACTIVE"
    # 이번 요청의 사용분만 반환한다.
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "released"


def test_FR19_cancel_not_sent_evidence_with_datetime(store, conn):
    """`evidence`에 `datetime`이 섞여 있어도 저장이 터지지 않고, 재호출은 `ALREADY_APPLIED`다.

    상태 관리의 `execution.py`는 `attempt.cancel`에 `confirmed_at`(datetime)을 그대로 담아
    넘긴다. 저장 뒤 돌아온 `StoreResult.command.evidence`는 원래 넘긴 객체와 같아야, CORE가
    `current.evidence != attempt.cancel`로 저장을 확인하는 검증을 통과한다.
    """
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)
    evidence = {"source": "recheck", "confirmed_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    first = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, evidence)
    second = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, evidence)

    assert first.outcome == StoreOutcome.APPLIED
    assert first.command.evidence == evidence
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert second.command.evidence == evidence
    assert second.command.command_id == linked.command.command_id


def test_FR19_cancel_not_sent_result_exists_conflict(store, conn):
    _seed_initial(conn)
    linked = store.link_command("req1", "node1", Action.OPEN)
    store.begin_send(linked.command.command_id)
    # record_result는 3단계에서 구현한다. 결과가 이미 있는 상태를 직접 만든다.
    conn.execute(
        "UPDATE node_command SET status = 'succeeded', outcome = 'succeeded' WHERE command_id = ?",
        (linked.command.command_id,),
    )

    result = store.cancel_not_sent(
        "req1", ExecutionStatus.IN_PROGRESS, ReasonCode.RECHECK_FAILED, {"source": "test"}
    )

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.STATE_CONFLICT
