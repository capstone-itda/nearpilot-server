"""`ExecutionStore` 5단계: `begin_close`, `finish_close`, `close_result` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

from datetime import datetime, timezone

from nearpilot.shared.enums import Action, EndReason, StoreConflictReason, StoreOutcome

from tests.db.conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user

_CLOSED_AT = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)


def _seed_active(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE")


def test_FR20_begin_close_applied(store, conn):
    _seed_active(conn)

    result = store.begin_close("use1", EndReason.RELEASE)

    assert result.outcome == StoreOutcome.APPLIED
    assert result.session.state.value == "CLOSING"
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "CLOSING"
    session = conn.execute("SELECT end_reason FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["end_reason"] == "release"


def test_FR20_begin_close_idempotent(store, conn):
    _seed_active(conn)
    first = store.begin_close("use1", EndReason.RELEASE)

    second = store.begin_close("use1", EndReason.RELEASE)

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert second.session.state.value == "CLOSING"


def test_FR19_begin_close_command_in_progress(store, conn):
    _seed_active(conn)
    insert_request(conn, internal_request_id="req-reop", request_id="ext-2")
    linked = store.link_command("req-reop", "node1", Action.OPEN)
    store.begin_send(linked.command.command_id)

    result = store.begin_close("use1", EndReason.RELEASE)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.COMMAND_IN_PROGRESS
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "ACTIVE"  # 반납 시작을 보류한다.


def test_FR20_finish_close_applied(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)

    result = store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": True})

    assert result.outcome == StoreOutcome.APPLIED
    session = conn.execute(
        "SELECT state, closed_at, check_id, check_evidence FROM use_session WHERE use_id = 'use1'"
    ).fetchone()
    assert session["state"] == "CLOSED"
    assert session["closed_at"] == _CLOSED_AT.isoformat()
    assert session["check_id"] == "check1"
    assert session["check_evidence"] == '{"door_closed": true}'
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "AVAILABLE"


def test_FR20_finish_close_idempotent(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    first = store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": True})

    second = store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": True})

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED


def test_FR20_finish_close_conflict_different_check(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": True})

    result = store.finish_close("use1", _CLOSED_AT, "check2", {"door_closed": True})

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.CONTENT_CONFLICT
    session = conn.execute("SELECT check_id FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["check_id"] == "check1"  # 기존 기록을 바꾸지 않는다.


def test_FR20_finish_close_conflict_different_evidence(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": True})

    result = store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": False})

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.CONTENT_CONFLICT


def test_FR20_finish_close_state_conflict(store, conn):
    _seed_active(conn)
    # begin_close 전, 즉 ACTIVE 상태에서 바로 finish_close를 부른다.

    result = store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": True})

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.STATE_CONFLICT


def test_FR20_close_result_found(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    store.finish_close("use1", _CLOSED_AT, "check1", {"door_closed": True})

    result = store.close_result("use1")

    assert result is not None
    assert result.use_id == "use1"
    assert result.closed_at == _CLOSED_AT
    assert result.check_id == "check1"
    assert result.evidence == {"door_closed": True}
    assert result.end_reason == EndReason.RELEASE


def test_FR20_close_result_none(store, conn):
    _seed_active(conn)

    assert store.close_result("use1") is None  # 아직 CLOSING도 안 됐다.

    store.begin_close("use1", EndReason.RELEASE)
    assert store.close_result("use1") is None  # CLOSING. 완료 기록이 아니다.


def test_FR20_close_result_not_a_safety_close(store, conn):
    """취소·거부로 CLOSED된 세션은 반납 완료 기록이 아니다 (check_id가 없다)."""
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    # finish_close를 거치지 않고 다른 경로로 CLOSED/cancelled가 된 상황을 직접 만든다.
    conn.execute(
        "UPDATE use_session SET state = 'CLOSED', end_reason = 'cancelled' WHERE use_id = 'use1'"
    )

    assert store.close_result("use1") is None
