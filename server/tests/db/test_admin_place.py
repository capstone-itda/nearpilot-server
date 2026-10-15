"""`AdminRepository.assign_place` (specs/db-admin-repository/spec.md, FR-12A)."""

from __future__ import annotations

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import PlaceAssignment

from conftest import insert_node, insert_user


def _assignment(place_id="place1", owner_user_id="u1", name="place", node_ids=()):
    return PlaceAssignment(place_id=place_id, owner_user_id=owner_user_id, name=name, node_ids=node_ids)


def test_FR12A_assign_place_applied(admin, conn):
    insert_user(conn, user_id="u1")
    insert_node(conn, node_id="node1", place_id=None, occupancy="rental", device_state="ACTIVE")

    result = admin.assign_place(_assignment(node_ids=("node1",)))

    assert result.outcome == AdminWriteOutcome.APPLIED
    place_row = conn.execute("SELECT owner_user_id, name FROM place WHERE place_id = 'place1'").fetchone()
    assert tuple(place_row) == ("u1", "place")
    node_row = conn.execute("SELECT place_id, device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert tuple(node_row) == ("place1", "ACTIVE")  # 점유 상태는 그대로다


def test_FR12A_assign_place_owner_missing(admin, conn):
    result = admin.assign_place(_assignment())

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.ACCOUNT_NOT_FOUND
    assert conn.execute("SELECT COUNT(*) AS c FROM place").fetchone()["c"] == 0


def test_FR12A_assign_place_node_missing_atomic(admin, conn):
    insert_user(conn, user_id="u1")
    insert_node(conn, node_id="node1", place_id=None)

    result = admin.assign_place(_assignment(node_ids=("node1", "no-such-node")))

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.NODE_NOT_FOUND
    assert conn.execute("SELECT COUNT(*) AS c FROM place").fetchone()["c"] == 0
    row = conn.execute("SELECT place_id FROM node WHERE node_id = 'node1'").fetchone()
    assert row["place_id"] is None


def test_FR12A_assign_place_owner_conflict(admin, conn):
    insert_user(conn, user_id="u1", token_hash="hash-1")
    insert_user(conn, user_id="u2", token_hash="hash-2")
    admin.assign_place(_assignment(owner_user_id="u1"))

    result = admin.assign_place(_assignment(owner_user_id="u2"))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT
    row = conn.execute("SELECT owner_user_id FROM place WHERE place_id = 'place1'").fetchone()
    assert row["owner_user_id"] == "u1"


def test_FR12A_assign_place_idempotent_adds_nodes(admin, conn):
    insert_user(conn, user_id="u1")
    insert_node(conn, node_id="node1", place_id=None)
    insert_node(conn, node_id="node2", place_id=None)
    admin.assign_place(_assignment(node_ids=("node1",)))

    result = admin.assign_place(_assignment(node_ids=("node1", "node2")))

    assert result.outcome == AdminWriteOutcome.APPLIED
    rows = {r["node_id"]: r["place_id"] for r in conn.execute("SELECT node_id, place_id FROM node")}
    assert rows == {"node1": "place1", "node2": "place1"}
    assert conn.execute("SELECT COUNT(*) AS c FROM place").fetchone()["c"] == 1


def test_FR12A_assign_place_name_conflict(admin, conn):
    insert_user(conn, user_id="u1")
    admin.assign_place(_assignment(name="first"))

    result = admin.assign_place(_assignment(name="second"))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT
    row = conn.execute("SELECT name FROM place WHERE place_id = 'place1'").fetchone()
    assert row["name"] == "first"
