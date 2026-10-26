"""`ExecutionStore.last_succeeded`: 공용형의 마지막 제어 상태 (FR-20, 2026-10-26 결정)."""

from __future__ import annotations

from nearpilot.shared.enums import Action, ExecutionStatus

from conftest import insert_node, insert_place, insert_request, insert_user


def _command(conn, command_id, action, status, send_started_at, *, node_id="light1"):
    insert_request(conn, internal_request_id=f"req-{command_id}", request_id=f"ext-{command_id}")
    conn.execute(
        "INSERT INTO node_command (command_id, internal_request_id, node_id, action, status, execution_kind, "
        "send_started_at, send_attempted) VALUES (?, ?, ?, ?, ?, 'shared', ?, 1)",
        (command_id, f"req-{command_id}", node_id, action, status, send_started_at),
    )


def _seed(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    insert_node(conn, node_id="light2", capability="light", occupancy="shared")


def test_FR20_last_succeeded_uses_send_order(store, conn):
    _seed(conn)
    _command(conn, "c1", "on", "succeeded", "2026-01-01T00:00:01+00:00")
    _command(conn, "c2", "off", "succeeded", "2026-01-01T00:00:03+00:00")
    _command(conn, "c3", "on", "failed", "2026-01-01T00:00:05+00:00")
    _command(conn, "c4", "on", "succeeded", "2026-01-01T00:00:09+00:00", node_id="light2")

    last = store.last_succeeded("light1")

    # 실패한 명령은 마지막 제어 상태가 아니다. 다른 기기의 명령도 보지 않는다.
    assert (last.command_id, last.action, last.status) == ("c2", Action.OFF, ExecutionStatus.SUCCEEDED)


def test_FR20_last_succeeded_none_without_success(store, conn):
    _seed(conn)
    _command(conn, "c1", "on", "unknown", "2026-01-01T00:00:01+00:00")

    assert store.last_succeeded("light1") is None
