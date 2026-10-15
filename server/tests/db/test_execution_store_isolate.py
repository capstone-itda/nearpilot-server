"""`ExecutionStore` 4단계: `isolate_device` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

import pytest

from nearpilot.shared.enums import Action, StoreOutcome

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


def test_NFR04_isolate_device_applied(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE")
    insert_policy(conn, policy_id="policy1", node_id="node1")
    insert_approval(conn, approval_id="approval1", node_id="node1", internal_request_id="req1")
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval1", internal_request_id="req1", state="held")
    linked = store.link_command("req1", "node1", Action.OPEN)

    result = store.isolate_device("node1", {"source": "sensor_fault"})

    assert result.outcome == StoreOutcome.APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "FAULT"
    # 점유 세션·승인 사용분·명령은 그대로다.
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "ACTIVE"
    usage = conn.execute("SELECT state FROM approval_usage WHERE usage_id = 'usage1'").fetchone()
    assert usage["state"] == "held"
    command = conn.execute("SELECT status FROM node_command WHERE command_id = ?", (linked.command.command_id,)).fetchone()
    assert command["status"] == "pending"
    audit = conn.execute("SELECT step, payload_json FROM audit_log WHERE step = 'fault'").fetchall()
    assert len(audit) == 1


def test_NFR04_isolate_device_already_fault(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", device_state="ACTIVE")

    first = store.isolate_device("node1", {"source": "first"})
    second = store.isolate_device("node1", {"source": "second"})

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "FAULT"
    # 두 번 다 근거를 감사에 남긴다.
    audit = conn.execute("SELECT payload_json FROM audit_log WHERE step = 'fault' ORDER BY created_at").fetchall()
    assert len(audit) == 2
    assert '"first"' in audit[0]["payload_json"]
    assert '"second"' in audit[1]["payload_json"]


def test_NFR04_isolate_device_from_closing(store, conn):
    """`CLOSING`에서도 `FAULT`로 전이한다 (FR-19: "어느 상태에서든 기기는 FAULT가 된다")."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="CLOSING")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="CLOSING")

    result = store.isolate_device("node1", {"source": "safety_unconfirmed"})

    assert result.outcome == StoreOutcome.APPLIED
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "FAULT"
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session["state"] == "CLOSING"  # 점유 세션은 보존한다.


def test_NFR04_isolate_device_missing_node_raises(store, conn):
    with pytest.raises(LookupError):
        store.isolate_device("no-such-node", {"source": "test"})
