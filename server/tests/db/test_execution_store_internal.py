"""`ExecutionStore.link_internal_request`와 서버 내부 요청의 실행 흐름 (#86, FR-20, FR-21)."""

from __future__ import annotations

from datetime import datetime, timezone

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, CommandOutcome, ExecutionKind, ExecutionStatus, InternalRequestCause, StoreOutcome,
)
from nearpilot.shared.models import InternalRequest

from conftest import insert_node, insert_place, insert_user


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _request(internal_request_id="int-1", node_id="light1", action=Action.OFF, settings=None):
    return InternalRequest(
        internal_request_id=internal_request_id, node_id=node_id, action=action,
        cause=InternalRequestCause.LEAVE_OFF, settings=settings or Settings(), created_at=NOW,
    )


def _setup_shared(conn, node_id="light1"):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id=node_id, capability="light", occupancy="shared")


def test_FR20_link_internal_request_applied(conn, store):
    _setup_shared(conn)

    result = store.link_internal_request(_request())

    assert result.outcome == StoreOutcome.APPLIED
    row = conn.execute("SELECT * FROM request WHERE internal_request_id = 'int-1'").fetchone()
    assert row["user_id"] is None
    assert row["request_kind"] == "server_internal"
    assert row["node_id"] == "light1"
    assert row["action"] == "off"
    assert row["internal_cause"] == "leave_off"
    assert row["settings_json"] is not None


def test_FR20_link_internal_request_idempotent(conn, store):
    _setup_shared(conn)

    first = store.link_internal_request(_request())
    second = store.link_internal_request(_request())

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert conn.execute("SELECT COUNT(*) AS c FROM request WHERE internal_request_id = 'int-1'").fetchone()["c"] == 1


def test_FR20_link_internal_request_conflict(conn, store):
    _setup_shared(conn)
    insert_node(conn, node_id="light2", capability="light", occupancy="shared")

    store.link_internal_request(_request())
    result = store.link_internal_request(_request(node_id="light2"))

    assert result.outcome == StoreOutcome.CONFLICT
    row = conn.execute("SELECT node_id FROM request WHERE internal_request_id = 'int-1'").fetchone()
    assert row["node_id"] == "light1"


def test_FR20_server_internal_command_flow(conn, store):
    _setup_shared(conn)
    store.link_internal_request(_request())

    link_result = store.link_command("int-1", "light1", Action.OFF)
    command = link_result.command

    assert command.execution_kind == ExecutionKind.SERVER_INTERNAL
    assert command.use_id is None
    assert command.usage_id is None

    begin = store.begin_send(command.command_id)
    assert begin.outcome == StoreOutcome.APPLIED
    assert begin.command.status == ExecutionStatus.IN_PROGRESS

    result = store.record_result(command.command_id, CommandOutcome.SUCCEEDED, NOW, None, {})

    assert result.outcome == StoreOutcome.APPLIED
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM approval_usage").fetchone()["c"] == 0
    row = conn.execute("SELECT device_state FROM node WHERE node_id = 'light1'").fetchone()
    assert row["device_state"] == "AVAILABLE"


def test_FR20_execution_context_server_internal(conn, store):
    _setup_shared(conn)
    settings = Settings(version="int-settings")
    store.link_internal_request(_request(settings=settings))
    command = store.link_command("int-1", "light1", Action.OFF).command

    ctx = store.execution_context(command.command_id)

    assert ctx is not None
    assert ctx.request is None
    assert ctx.internal_request is not None
    assert ctx.internal_request.internal_request_id == "int-1"
    assert ctx.internal_request.node_id == "light1"
    assert ctx.internal_request.action == Action.OFF
    assert ctx.internal_request.cause == InternalRequestCause.LEAVE_OFF
    assert ctx.internal_request.settings == settings
    assert ctx.execution_kind == ExecutionKind.SERVER_INTERNAL
    assert ctx.use_id is None
    assert ctx.usage_id is None
    assert ctx.settings == settings


def test_FR21_server_internal_hidden(conn, store):
    _setup_shared(conn)
    store.link_internal_request(_request())

    command = store.link_command("int-1", "light1", Action.OFF).command

    assert command.use_id is None
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 0
    row = conn.execute("SELECT user_id FROM request WHERE internal_request_id = 'int-1'").fetchone()
    assert row["user_id"] is None
