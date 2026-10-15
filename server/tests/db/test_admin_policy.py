"""`AdminRepository.set_policy` (specs/db-admin-repository/spec.md, FR-14)."""

from __future__ import annotations

import threading

from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import AccessMode, AdminWriteOutcome, AdminWriteReason, ApprovalMode, Visibility
from nearpilot.shared.models import PolicyRevision

from conftest import insert_approval, insert_node, insert_place, insert_request, insert_use_session, insert_user


def _revision(policy_id="policy1", version=1, node_id="node1", approver_user_id="owner1", max_uses=None):
    return PolicyRevision(
        policy_id=policy_id, version=version, node_id=node_id, visibility=Visibility.PRIVATE,
        mode=AccessMode.RESTRICTED, approver_user_id=approver_user_id, approval_mode=ApprovalMode.ONE_TIME,
        approval_ttl_sec=3600, max_uses=max_uses,
    )


def _setup(conn, owner="owner1"):
    insert_user(conn, user_id=owner, token_hash=f"hash-{owner}")
    insert_place(conn, owner_user_id=owner)
    insert_node(conn, node_id="node1")


def test_FR14_set_policy_first(admin, conn):
    _setup(conn)

    result = admin.set_policy(_revision(version=1), expected_version=None)

    assert result.outcome == AdminWriteOutcome.APPLIED
    row = conn.execute("SELECT current_version FROM access_policy WHERE node_id = 'node1'").fetchone()
    assert row["current_version"] == 1


def test_FR14_set_policy_next_version(admin, conn):
    _setup(conn)
    admin.set_policy(_revision(version=1), expected_version=None)

    result = admin.set_policy(_revision(version=2, max_uses=5), expected_version=1)

    assert result.outcome == AdminWriteOutcome.APPLIED
    row = conn.execute("SELECT current_version FROM access_policy WHERE node_id = 'node1'").fetchone()
    assert row["current_version"] == 2
    old = conn.execute(
        "SELECT max_uses FROM policy_revision WHERE policy_id = 'policy1' AND version = 1"
    ).fetchone()
    assert old["max_uses"] is None  # 과거 리비전은 그대로다


def test_FR14_set_policy_version_mismatch(admin, conn):
    _setup(conn)
    admin.set_policy(_revision(version=1), expected_version=None)

    result = admin.set_policy(_revision(version=2), expected_version=5)

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.POLICY_VERSION_MISMATCH


def test_FR14_set_policy_version_gap(admin, conn):
    _setup(conn)
    admin.set_policy(_revision(version=1), expected_version=None)

    result = admin.set_policy(_revision(version=3), expected_version=1)

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT


def test_FR14_set_policy_first_but_exists(admin, conn):
    _setup(conn)
    admin.set_policy(_revision(version=1), expected_version=None)

    result = admin.set_policy(_revision(version=2), expected_version=None)

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.POLICY_VERSION_MISMATCH


def test_FR14_set_policy_missing_refs(admin, conn):
    _setup(conn)

    node_missing = admin.set_policy(_revision(node_id="no-such-node"), expected_version=None)
    assert node_missing.outcome == AdminWriteOutcome.REJECTED
    assert node_missing.reason == AdminWriteReason.NODE_NOT_FOUND

    approver_missing = admin.set_policy(_revision(approver_user_id="no-such-user"), expected_version=None)
    assert approver_missing.outcome == AdminWriteOutcome.REJECTED
    assert approver_missing.reason == AdminWriteReason.ACCOUNT_NOT_FOUND


def test_FR14_set_policy_no_retroactive(admin, conn):
    _setup(conn)
    admin.set_policy(_revision(version=1), expected_version=None)
    insert_request(conn, internal_request_id="req1", user_id="owner1", node_id="node1")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", user_id="owner1", node_id="node1", state="ACTIVE"
    )
    insert_approval(
        conn, approval_id="appr1", node_id="node1", requester_user_id="owner1", policy_version=1,
        status="approved", internal_request_id="req1", pending_until="2026-01-01T00:10:00+00:00",
    )

    admin.set_policy(_revision(version=2), expected_version=1)

    session_row = conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session_row["state"] == "ACTIVE"
    approval_row = conn.execute("SELECT policy_version FROM approval WHERE approval_id = 'appr1'").fetchone()
    assert approval_row["policy_version"] == 1


def test_NFR03_set_policy_concurrent(tmp_path):
    db_path = str(tmp_path / "policy.db")
    setup_conn = connect(db_path)
    setup_conn.execute("INSERT INTO user (user_id, name, token_hash, role) VALUES ('owner1', 'o', 'h1', 'owner')")
    setup_conn.execute("INSERT INTO place (place_id, owner_user_id, name) VALUES ('place1', 'owner1', 'p')")
    setup_conn.execute(
        "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
        "release_policy) VALUES ('node1', 'place1', 'locker', 'rental', 'trusted', 'AVAILABLE', '{}')"
    )
    setup_conn.execute(
        "INSERT INTO access_policy (policy_id, node_id, current_version) VALUES ('policy1', 'node1', 1)"
    )
    setup_conn.execute(
        "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
        "approval_ttl_sec, approval_mode, max_uses) VALUES "
        "('policy1', 1, 'private', 'restricted', 'owner1', 3600, 'one_time', NULL)"
    )
    setup_conn.close()

    results: list[AdminWriteOutcome] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt() -> None:
        conn = connect(db_path)
        try:
            repo = SqliteAdminRepository(conn)
            barrier.wait()
            result = repo.set_policy(_revision(version=2), expected_version=1)
            with lock:
                results.append(result.outcome)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(AdminWriteOutcome.APPLIED) == 1
    assert results.count(AdminWriteOutcome.CONFLICT) == 1
