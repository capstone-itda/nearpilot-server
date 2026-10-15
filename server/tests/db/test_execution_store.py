"""`ExecutionStore` 1단계: `link_command`, `execution` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

from nearpilot.shared.enums import Action, ExecutionKind, ExecutionStatus, StoreOutcome

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


def test_FR18_link_command_creates(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")

    result = store.link_command("req1", "node1", Action.OPEN)

    assert result.outcome == StoreOutcome.APPLIED
    command = result.command
    assert command is not None
    assert command.internal_request_id == "req1"
    assert command.node_id == "node1"
    assert command.action == Action.OPEN
    assert command.status == ExecutionStatus.PENDING
    assert command.execution_kind == ExecutionKind.RENTAL_INITIAL
    assert command.use_id == "use1"
    assert command.send_attempted is False
    assert command.outcome is None

    row = conn.execute("SELECT * FROM node_command WHERE internal_request_id = 'req1'").fetchone()
    assert row["command_id"] == command.command_id


def test_FR18_link_command_idempotent(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")

    first = store.link_command("req1", "node1", Action.OPEN)
    second = store.link_command("req1", "node1", Action.OPEN)

    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    assert second.command is not None
    assert second.command.command_id == first.command.command_id
    count = conn.execute("SELECT COUNT(*) AS n FROM node_command WHERE internal_request_id = 'req1'").fetchone()
    assert count["n"] == 1


def test_FR19_link_command_shared(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    insert_request(conn, internal_request_id="req-shared", node_id="light1", action="on")
    insert_policy(conn, policy_id="policy-light", node_id="light1")
    insert_approval(
        conn, approval_id="approval-light", node_id="light1", policy_id="policy-light",
        internal_request_id="req-shared",
    )
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval-light", internal_request_id="req-shared")

    result = store.link_command("req-shared", "light1", Action.ON)

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command.execution_kind == ExecutionKind.SHARED
    assert result.command.use_id is None
    assert result.command.usage_id == "usage1"


def test_FR19_link_command_reoperation(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    # 최초 대여 요청과 그 점유 세션
    insert_request(conn, internal_request_id="req-initial")
    insert_use_session(conn, use_id="use1", internal_request_id="req-initial", state="ACTIVE")
    # 같은 점유에 새 조작을 요청하는 재조작
    insert_request(conn, internal_request_id="req-reop", request_id="ext-2")

    result = store.link_command("req-reop", "node1", Action.OPEN)

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command.execution_kind == ExecutionKind.RENTAL_REOPERATION
    assert result.command.use_id == "use1"
    assert result.command.internal_request_id == "req-reop"


def test_FR19_link_command_open_reoperation(store, conn):
    """`open` 정책의 재조작은 승인 사용분을 예약하지 않는다 (#27). 실행 종류 판단이
    `approval_usage` 행의 존재가 아니라 `node.occupancy`와 점유 세션만으로 이뤄지는지 확인한다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_policy(conn, policy_id="policy-open", node_id="node1", mode="open")
    insert_request(conn, internal_request_id="req-initial")
    insert_use_session(conn, use_id="use1", internal_request_id="req-initial", state="ACTIVE")
    insert_request(conn, internal_request_id="req-reop", request_id="ext-2")

    result = store.link_command("req-reop", "node1", Action.OPEN)

    assert result.outcome == StoreOutcome.APPLIED
    assert result.command.execution_kind == ExecutionKind.RENTAL_REOPERATION
    assert result.command.use_id == "use1"
    assert result.command.usage_id is None


def test_FR19_execution_lookup(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")

    assert store.execution("req1") is None

    linked = store.link_command("req1", "node1", Action.OPEN)
    found = store.execution("req1")

    assert found is not None
    assert found.command_id == linked.command.command_id
    assert store.execution("no-such-request") is None
