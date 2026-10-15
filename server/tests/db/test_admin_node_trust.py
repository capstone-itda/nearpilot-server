"""`AdminRepository.set_node_trust` (specs/db-admin-repository/spec.md, FR-16)."""

from __future__ import annotations

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason, TrustState

from conftest import insert_node, insert_place, insert_user


def test_FR16_set_node_trust_only_trust(admin, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", trust_state="discovered", device_state="AVAILABLE")

    result = admin.set_node_trust("node1", TrustState.TRUSTED)

    assert result.outcome == AdminWriteOutcome.APPLIED
    row = conn.execute("SELECT trust_state, device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert tuple(row) == ("trusted", "AVAILABLE")


def test_FR16_set_node_trust_missing(admin, conn):
    result = admin.set_node_trust("no-such-node", TrustState.TRUSTED)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.NODE_NOT_FOUND
