"""점유 중인 기기의 장소 이동·정책 변경 거부 (specs/db-admin-repository/spec.md, FR-12A)."""

from __future__ import annotations

import pytest

from nearpilot.shared.enums import (
    AccessMode,
    AdminWriteOutcome,
    AdminWriteReason,
    ApprovalMode,
    TrustState,
    Visibility,
)
from nearpilot.shared.models import PlaceAssignment, PolicyRevision

from conftest import insert_node, insert_place, insert_policy, insert_request, insert_use_session, insert_user

RELEASE_POLICY = {"check_criteria": {"id": "locker-return", "version": 1}}


def _setup(conn):
    insert_user(conn, user_id="u1", role="owner")
    insert_place(conn, place_id="place-a", owner_user_id="u1", name="a")


def _assignment(place_id, name, node_ids):
    return PlaceAssignment(place_id=place_id, owner_user_id="u1", name=name, node_ids=node_ids)


def _revision(node_id="node1", policy_id="policy1", version=2):
    return PolicyRevision(
        policy_id=policy_id, version=version, node_id=node_id, visibility=Visibility.PRIVATE,
        mode=AccessMode.RESTRICTED, approver_user_id="u1", approval_mode=ApprovalMode.ONE_TIME,
        approval_ttl_sec=3600, max_uses=None,
    )


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


@pytest.mark.parametrize("device_state", ["RESERVED", "ACTIVE", "CLOSING"])
def test_FR12A_set_policy_occupied_rejected(admin, conn, device_state):
    _setup(conn)
    insert_node(conn, node_id="node1", place_id="place-a", device_state=device_state)
    insert_policy(conn, policy_id="policy1", node_id="node1", version=1)
    revisions_before = _count(conn, "policy_revision")
    audits_before = _count(conn, "audit_log")

    result = admin.set_policy(_revision(version=2), expected_version=1)

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.NODE_OCCUPIED
    row = conn.execute("SELECT current_version FROM access_policy WHERE node_id = 'node1'").fetchone()
    assert row["current_version"] == 1
    assert _count(conn, "policy_revision") == revisions_before
    assert _count(conn, "audit_log") == audits_before


def test_FR12A_assign_place_occupied_rejected(admin, conn):
    _setup(conn)
    insert_node(conn, node_id="free", place_id="place-a", device_state="AVAILABLE")
    insert_node(conn, node_id="busy", place_id="place-a", device_state="ACTIVE")

    # 걸리지 않는 기기를 앞에 둔다 — 기기를 하나씩 옮기면 앞의 기기가 먼저 옮겨진다.
    result = admin.assign_place(_assignment("place-b", "b", ("free", "busy")))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.NODE_OCCUPIED
    assert conn.execute("SELECT 1 FROM place WHERE place_id = 'place-b'").fetchone() is None
    rows = {r["node_id"]: r["place_id"] for r in conn.execute("SELECT node_id, place_id FROM node")}
    assert rows == {"free": "place-a", "busy": "place-a"}
    assert _count(conn, "audit_log") == 0


def test_FR12A_assign_place_same_place_while_occupied(admin, conn):
    _setup(conn)
    insert_node(conn, node_id="busy", place_id="place-a", device_state="ACTIVE")

    result = admin.assign_place(_assignment("place-a", "a", ("busy",)))

    assert result.outcome == AdminWriteOutcome.APPLIED
    row = conn.execute("SELECT place_id, device_state FROM node WHERE node_id = 'busy'").fetchone()
    assert tuple(row) == ("place-a", "ACTIVE")


def test_FR12A_change_after_release_applied(admin, conn):
    _setup(conn)
    insert_node(conn, node_id="node1", place_id="place-a", device_state="AVAILABLE")
    insert_policy(conn, policy_id="policy1", node_id="node1", version=1)
    insert_request(conn, internal_request_id="req1", user_id="u1", node_id="node1")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", user_id="u1", node_id="node1", state="CLOSED",
        end_reason="release", closed_at="2026-01-01T00:10:00+00:00",
    )

    policy_result = admin.set_policy(_revision(version=2), expected_version=1)
    place_result = admin.assign_place(_assignment("place-b", "b", ("node1",)))

    assert policy_result.outcome == AdminWriteOutcome.APPLIED
    assert place_result.outcome == AdminWriteOutcome.APPLIED
    policy_row = conn.execute("SELECT current_version FROM access_policy WHERE node_id = 'node1'").fetchone()
    assert policy_row["current_version"] == 2
    node_row = conn.execute("SELECT place_id FROM node WHERE node_id = 'node1'").fetchone()
    assert node_row["place_id"] == "place-b"


def test_FR12A_fault_with_open_session_is_occupied(admin, conn):
    _setup(conn)
    insert_node(conn, node_id="with-session", place_id="place-a", device_state="FAULT")
    insert_node(conn, node_id="no-session", place_id="place-a", device_state="FAULT")
    insert_policy(conn, policy_id="policy-with", node_id="with-session", version=1)
    insert_request(conn, internal_request_id="req1", user_id="u1", node_id="with-session")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", user_id="u1", node_id="with-session", state="ACTIVE"
    )

    blocked = admin.set_policy(
        _revision(node_id="with-session", policy_id="policy-with", version=2), expected_version=1
    )
    allowed = admin.set_policy(
        _revision(node_id="no-session", policy_id="policy-no", version=1), expected_version=None
    )

    assert blocked.outcome == AdminWriteOutcome.CONFLICT
    assert blocked.reason == AdminWriteReason.NODE_OCCUPIED
    assert allowed.outcome == AdminWriteOutcome.APPLIED


def test_FR12A_other_writes_ignore_occupancy(admin, conn):
    _setup(conn)
    insert_node(conn, node_id="busy", place_id="place-a", device_state="ACTIVE")

    release_policy_result = admin.set_release_policy("busy", RELEASE_POLICY)
    node_name_result = admin.set_node_name("busy", "새 이름")
    node_trust_result = admin.set_node_trust("busy", TrustState.REVOKED)

    assert release_policy_result.outcome == AdminWriteOutcome.APPLIED
    assert node_name_result.outcome == AdminWriteOutcome.APPLIED
    assert node_trust_result.outcome == AdminWriteOutcome.APPLIED
