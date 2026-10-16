"""`tests/db/` 공통 fixture. `AdminRepository`가 아직 없어 SQL로 직접 데이터를 넣는다.

(spec.md §6 결정: "테스트 데이터는 tests/db/의 fixture가 SQL로 직접 넣는다")
"""

from __future__ import annotations

import json

import pytest

from nearpilot.db.repositories._common import new_id
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def repo(conn):
    return SqliteRepository(conn)


@pytest.fixture
def store(conn):
    return SqliteExecutionStore(conn)


@pytest.fixture
def admin(conn):
    return SqliteAdminRepository(conn)


def insert_user(conn, user_id="u1", token_hash="hash-u1", role="visitor", name="tester"):
    conn.execute(
        "INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, name, token_hash, role),
    )


def insert_beacon(conn, beacon_id="beacon1", user_id="u1", uuid="UUID-1", major=1, minor=1, is_active=1):
    conn.execute(
        "INSERT INTO beacon (beacon_id, user_id, uuid, major, minor, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (beacon_id, user_id, uuid, major, minor, is_active),
    )


def insert_place(conn, place_id="place1", owner_user_id="u1", name="place"):
    conn.execute(
        "INSERT INTO place (place_id, owner_user_id, name) VALUES (?, ?, ?)",
        (place_id, owner_user_id, name),
    )


def insert_node(
    conn,
    node_id="node1",
    place_id="place1",
    capability="locker",
    occupancy="rental",
    trust_state="trusted",
    device_state="AVAILABLE",
    exclusive_group=None,
    session_id=None,
    session_first_seen_at=None,
    last_seen_at=None,
):
    conn.execute(
        "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
        "exclusive_group, release_policy, session_id, session_first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)",
        (
            node_id, place_id, capability, occupancy, trust_state, device_state,
            exclusive_group, session_id, session_first_seen_at, last_seen_at,
        ),
    )


def insert_policy(
    conn,
    policy_id="policy1",
    node_id="node1",
    version=1,
    visibility="private",
    mode="restricted",
    approver_user_id="u1",
    approval_mode="one_time",
    approval_ttl_sec=3600,
    max_uses=None,
):
    conn.execute(
        "INSERT INTO access_policy (policy_id, node_id, current_version) VALUES (?, ?, ?)",
        (policy_id, node_id, version),
    )
    conn.execute(
        "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
        "approval_ttl_sec, approval_mode, max_uses) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (policy_id, version, visibility, mode, approver_user_id, approval_ttl_sec, approval_mode, max_uses),
    )


def add_policy_revision(
    conn,
    policy_id="policy1",
    version=2,
    visibility="private",
    mode="restricted",
    approver_user_id="u1",
    approval_mode="one_time",
    approval_ttl_sec=3600,
    max_uses=None,
    make_current=True,
):
    conn.execute(
        "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
        "approval_ttl_sec, approval_mode, max_uses) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (policy_id, version, visibility, mode, approver_user_id, approval_ttl_sec, approval_mode, max_uses),
    )
    if make_current:
        conn.execute(
            "UPDATE access_policy SET current_version = ? WHERE policy_id = ?", (version, policy_id)
        )


def insert_approval(
    conn,
    approval_id="approval1",
    node_id="node1",
    requester_user_id="u1",
    action="open",
    policy_id="policy1",
    policy_version=1,
    status="approved",
    created_at="2026-01-01T00:00:00+00:00",
    pending_until=None,
    valid_until="2099-01-01T00:00:00+00:00",
    used_count=0,
    internal_request_id="approval-req-1",
    approved_by_user_id=None,
):
    conn.execute(
        "INSERT INTO approval (approval_id, node_id, requester_user_id, internal_request_id, "
        "approved_by_user_id, action, policy_id, policy_version, status, created_at, pending_until, "
        "valid_until, used_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            approval_id, node_id, requester_user_id, internal_request_id, approved_by_user_id,
            action, policy_id, policy_version, status, created_at, pending_until, valid_until, used_count,
        ),
    )


def insert_approval_usage(conn, usage_id, approval_id, internal_request_id, state="reserved", command_id=None):
    conn.execute(
        "INSERT INTO approval_usage (usage_id, approval_id, internal_request_id, command_id, state) "
        "VALUES (?, ?, ?, ?, ?)",
        (usage_id, approval_id, internal_request_id, command_id, state),
    )


def insert_request(
    conn,
    internal_request_id="req1",
    user_id="u1",
    request_id="ext-1",
    input_digest="digest-1",
    target_spec="nearest:locker",
    action="open",
    node_id=None,
    verdict=None,
    reason_code=None,
    p_max=None,
    host_kind=None,
    received_at="2026-01-01T00:00:00+00:00",
    parent_internal_request_id=None,
):
    conn.execute(
        "INSERT INTO request (internal_request_id, user_id, request_id, input_digest, "
        "parent_internal_request_id, node_id, target_spec, action, verdict, reason_code, p_max, "
        "host_kind, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            internal_request_id, user_id, request_id, input_digest, parent_internal_request_id,
            node_id, target_spec, action, verdict, reason_code, p_max, host_kind, received_at,
        ),
    )


def insert_use_session(
    conn,
    use_id="use1",
    internal_request_id="req1",
    user_id="u1",
    node_id="node1",
    state="ACTIVE",
    end_reason=None,
    rental_due_at=None,
    closed_at=None,
):
    conn.execute(
        "INSERT INTO use_session (use_id, internal_request_id, user_id, node_id, state, end_reason, "
        "rental_due_at, closed_at, release_policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}')",
        (use_id, internal_request_id, user_id, node_id, state, end_reason, rental_due_at, closed_at),
    )


def insert_node_command(
    conn,
    command_id="cmd1",
    internal_request_id="req1",
    node_id="node1",
    action="open",
    status="succeeded",
    execution_kind="rental",
    use_id=None,
    usage_id=None,
    reserved_at=None,
    send_started_at=None,
    send_attempted=0,
    outcome=None,
    occurred_at=None,
):
    conn.execute(
        "INSERT INTO node_command (command_id, internal_request_id, node_id, action, status, "
        "execution_kind, use_id, usage_id, reserved_at, send_started_at, send_attempted, outcome, "
        "occurred_at, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')",
        (
            command_id, internal_request_id, node_id, action, status, execution_kind, use_id, usage_id,
            reserved_at, send_started_at, send_attempted, outcome, occurred_at,
        ),
    )


def insert_recovery(
    conn,
    recovery_id="recovery1",
    node_id="node1",
    use_id=None,
    internal_request_ids=(),
    command_ids=(),
    created_at="2026-01-01T00:00:00+00:00",
    status="not_ready",
    evidence=(),
    completed_at=None,
):
    conn.execute(
        "INSERT INTO recovery (recovery_id, node_id, use_id, internal_request_ids, command_ids, "
        "created_at, status, evidence, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            recovery_id, node_id, use_id, json.dumps(list(internal_request_ids)),
            json.dumps(list(command_ids)), created_at, status, json.dumps(list(evidence)), completed_at,
        ),
    )


def insert_audit_log(
    conn,
    log_id=None,
    event_id=None,
    internal_request_id=None,
    command_id=None,
    step="exec",
    payload=None,
    created_at="2026-01-01T00:00:00+00:00",
):
    conn.execute(
        "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
        "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            log_id or new_id(), event_id or new_id(), internal_request_id, command_id, step,
            json.dumps(payload or {}), created_at,
        ),
    )
