"""`ExecutionStore` 5단계: `begin_close`, `finish_close`, `close_result` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

from datetime import datetime, timezone

from nearpilot.shared.enums import Action, EndReason, StoreConflictReason, StoreOutcome

from conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user

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

    result = store.finish_close("use1", _CLOSED_AT, EndReason.RELEASE)

    assert result.outcome == StoreOutcome.APPLIED
    session = conn.execute("SELECT state, closed_at, end_reason FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "CLOSED"
    assert session["closed_at"] == _CLOSED_AT.isoformat()
    assert session["end_reason"] == "release"
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "AVAILABLE"


def test_FR20_finish_close_has_no_check_columns(store, conn):
    """센서 기반 반납 검사를 제외했다. 종료 기록에 검사 ID와 근거 열이 없다 (2026-10-26 결정)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(use_session)")}

    assert not {"check_id", "check_evidence"} & columns


def test_FR20_finish_close_idempotent_keeps_first_closed_at(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    first = store.finish_close("use1", _CLOSED_AT, EndReason.RELEASE)

    second = store.finish_close("use1", datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc), EndReason.RELEASE)

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert store.close_result("use1").closed_at == _CLOSED_AT  # 종료 시각을 다시 쓰지 않는다.


def test_FR20_finish_close_conflict_different_reason(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)

    result = store.finish_close("use1", _CLOSED_AT, EndReason.FAULT)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.CONTENT_CONFLICT
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "CLOSING"  # 기존 기록을 바꾸지 않는다.


def test_FR20_finish_close_state_conflict(store, conn):
    _seed_active(conn)
    # begin_close 전, 즉 ACTIVE 상태에서 바로 finish_close를 부른다.

    result = store.finish_close("use1", _CLOSED_AT, EndReason.RELEASE)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.STATE_CONFLICT


def test_FR19_finish_close_command_in_progress(store, conn):
    _seed_active(conn)
    insert_request(conn, internal_request_id="req-reop", request_id="ext-2")
    linked = store.link_command("req-reop", "node1", Action.OPEN)
    store.begin_close("use1", EndReason.RELEASE)
    # 반납 시작 뒤 구동 명령이 진행 중인 기록을 직접 만든다. 종료 직전 검사가 막는다.
    conn.execute("UPDATE node_command SET status = 'in_progress' WHERE command_id = ?",
                 (linked.command.command_id,))

    result = store.finish_close("use1", _CLOSED_AT, EndReason.RELEASE)

    assert result.outcome == StoreOutcome.CONFLICT
    assert result.conflict_reason == StoreConflictReason.COMMAND_IN_PROGRESS
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "CLOSING"


def test_FR20_close_result_found(store, conn):
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    store.finish_close("use1", _CLOSED_AT, EndReason.RELEASE)

    result = store.close_result("use1")

    assert result is not None
    assert result.use_id == "use1"
    assert result.closed_at == _CLOSED_AT
    assert result.end_reason == EndReason.RELEASE


def test_FR20_close_result_none(store, conn):
    _seed_active(conn)

    assert store.close_result("use1") is None  # 아직 CLOSING도 안 됐다.

    store.begin_close("use1", EndReason.RELEASE)
    assert store.close_result("use1") is None  # CLOSING. 완료 기록이 아니다.


def test_FR20_close_result_reports_other_end_reason(store, conn):
    """취소로 CLOSED된 세션도 종료 기록이다. 반납 완료인지는 호출자가 종료 사유로 가린다."""
    _seed_active(conn)
    store.begin_close("use1", EndReason.RELEASE)
    conn.execute(
        "UPDATE use_session SET state = 'CLOSED', end_reason = 'cancelled', closed_at = ? WHERE use_id = 'use1'",
        (_CLOSED_AT.isoformat(),),
    )

    assert store.close_result("use1").end_reason == EndReason.CANCELLED
