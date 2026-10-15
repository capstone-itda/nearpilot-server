"""`ExecutionStore` 3단계: `record_result` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.shared.enums import Action, CommandOutcome, StoreConflictReason, StoreOutcome

from tests.db.conftest import (
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
_DUE = _NOW + timedelta(hours=1)


def _seed_initial_in_progress(conn, store, *, with_approval=False):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="RESERVED")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")
    if with_approval:
        insert_policy(conn, policy_id="policy1", node_id="node1")
        insert_approval(conn, approval_id="approval1", node_id="node1", internal_request_id="req1")
        insert_approval_usage(conn, usage_id="usage1", approval_id="approval1", internal_request_id="req1")
    linked = store.link_command("req1", "node1", Action.OPEN)
    store.begin_send(linked.command.command_id)
    return linked.command.command_id


def _seed_reoperation_in_progress(conn, store, *, with_approval=False):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(conn, internal_request_id="req-initial")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req-initial", state="ACTIVE", rental_due_at=_DUE.isoformat(),
    )
    insert_request(conn, internal_request_id="req-reop", request_id="ext-2")
    if with_approval:
        insert_policy(conn, policy_id="policy1", node_id="node1")
        insert_approval(conn, approval_id="approval1", node_id="node1", internal_request_id="req-reop")
        insert_approval_usage(conn, usage_id="usage1", approval_id="approval1", internal_request_id="req-reop")
    linked = store.link_command("req-reop", "node1", Action.OPEN)
    store.begin_send(linked.command.command_id)
    return linked.command.command_id


def test_FR19_record_result_succeeded_initial(store, conn):
    command_id = _seed_initial_in_progress(conn, store, with_approval=True)

    result = store.record_result(command_id, CommandOutcome.SUCCEEDED, _NOW, _DUE, {"source": "test"})

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command.outcome == CommandOutcome.SUCCEEDED
    session = conn.execute("SELECT state, rental_due_at FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "ACTIVE"
    assert session["rental_due_at"] == _DUE.isoformat()
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "ACTIVE"
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "consumed"


def test_FR19_record_result_succeeded_reoperation(store, conn):
    command_id = _seed_reoperation_in_progress(conn, store, with_approval=True)

    result = store.record_result(command_id, CommandOutcome.SUCCEEDED, _NOW, None, {"source": "test"})

    assert result.outcome == StoreOutcome.APPLIED
    # 기존 대여의 점유와 rental_due_at은 그대로다.
    session = conn.execute("SELECT state, rental_due_at FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "ACTIVE"
    assert session["rental_due_at"] == _DUE.isoformat()
    # 이번 요청의 사용분만 소진한다.
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "consumed"


def test_FR07_record_result_failed_isolates(store, conn):
    command_id = _seed_initial_in_progress(conn, store, with_approval=True)

    result = store.record_result(command_id, CommandOutcome.FAILED, _NOW, None, {"source": "test"})

    assert result.outcome == StoreOutcome.APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "FAULT"
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "RESERVED"  # 세션 보존. ACTIVE로 바뀌지 않는다.
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "held"


def test_FR07_record_result_unknown_isolates(store, conn):
    command_id = _seed_initial_in_progress(conn, store, with_approval=True)

    result = store.record_result(command_id, CommandOutcome.UNKNOWN, _NOW, None, {"source": "test"})

    assert result.outcome == StoreOutcome.APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "FAULT"
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "held"


def test_FR19_record_result_rejected_initial(store, conn):
    command_id = _seed_initial_in_progress(conn, store, with_approval=True)

    result = store.record_result(
        command_id, CommandOutcome.REJECTED, _NOW, None, {"source": "device_busy"}
    )

    assert result.outcome == StoreOutcome.APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "AVAILABLE"  # FAULT로 두지 않는다.
    session = conn.execute("SELECT state, end_reason FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "CLOSED"
    assert session["end_reason"] == "cancelled"
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "released"


def test_FR19_record_result_rejected_reoperation(store, conn):
    command_id = _seed_reoperation_in_progress(conn, store, with_approval=True)

    result = store.record_result(
        command_id, CommandOutcome.REJECTED, _NOW, None, {"source": "device_busy"}
    )

    assert result.outcome == StoreOutcome.APPLIED
    # 기존 대여의 점유와 기기 상태는 그대로다. FAULT도 아니다.
    session = conn.execute("SELECT state, rental_due_at FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "ACTIVE"
    assert session["rental_due_at"] == _DUE.isoformat()
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "ACTIVE"
    # 이번 요청의 사용분만 반환한다.
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "released"


def test_FR07_record_result_idempotent(store, conn):
    command_id = _seed_initial_in_progress(conn, store)
    first = store.record_result(command_id, CommandOutcome.FAILED, _NOW, None, {"source": "test"})

    second = store.record_result(command_id, CommandOutcome.FAILED, _NOW, None, {"source": "test"})

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert second.command.command_id == first.command.command_id


def test_FR07_record_result_conflict(store, conn):
    command_id = _seed_initial_in_progress(conn, store)
    store.record_result(command_id, CommandOutcome.FAILED, _NOW, None, {"source": "a"})

    result = store.record_result(command_id, CommandOutcome.FAILED, _NOW, None, {"source": "b"})

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.CONTENT_CONFLICT
    assert result.command.evidence == {"source": "a"}


def test_FR07_record_result_late_success_keeps_fault(store, conn):
    command_id = _seed_initial_in_progress(conn, store)
    store.record_result(command_id, CommandOutcome.FAILED, _NOW, None, {"source": "test"})

    late = store.record_result(command_id, CommandOutcome.SUCCEEDED, _NOW + timedelta(seconds=5), _DUE, {})

    assert late.outcome == StoreOutcome.CONFLICT
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "FAULT"
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "RESERVED"
