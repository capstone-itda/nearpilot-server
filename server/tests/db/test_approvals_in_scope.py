"""`Repository.approvals_in_scope` (specs/db-contract-followups/spec.md, #60)."""

from __future__ import annotations

from nearpilot.shared.enums import Action, ApprovalStatus

from conftest import insert_approval, insert_node, insert_place, insert_policy, insert_user


def _seed_policy(conn, node_id="node1", policy_id="policy1"):
    insert_user(conn, user_id="u1")
    insert_place(conn)
    insert_node(conn, node_id=node_id)
    insert_policy(conn, policy_id=policy_id, node_id=node_id)


def test_FR14_approvals_in_scope_empty(repo, conn):
    _seed_policy(conn)

    assert repo.approvals_in_scope("u1", "node1") == []


def test_FR14_approvals_in_scope_returns_all(repo, conn):
    _seed_policy(conn)
    insert_approval(
        conn, approval_id="a-pending", requester_user_id="u1", node_id="node1", action="open",
        status="pending", created_at="2026-01-01T00:00:00+00:00",
        pending_until="2026-01-01T00:10:00+00:00", valid_until=None,
    )
    insert_approval(
        conn, approval_id="a-approved", requester_user_id="u1", node_id="node1", action="open",
        status="approved", created_at="2026-01-02T00:00:00+00:00",
        pending_until="2026-01-02T00:10:00+00:00", valid_until="2099-01-01T00:00:00+00:00",
    )
    insert_approval(
        conn, approval_id="a-denied", requester_user_id="u1", node_id="node1", action="on",
        status="denied", created_at="2026-01-03T00:00:00+00:00",
        pending_until="2026-01-03T00:10:00+00:00", valid_until=None,
    )

    records = repo.approvals_in_scope("u1", "node1")

    assert [r.approval_id for r in records] == ["a-pending", "a-approved", "a-denied"]
    assert [r.status for r in records] == [ApprovalStatus.PENDING, ApprovalStatus.APPROVED, ApprovalStatus.DENIED]
    assert records[1].action == Action.OPEN
    assert records[2].action == Action.ON
    # 사용자 ID 필드가 없다.
    assert not hasattr(records[0], "user_id")
    assert not hasattr(records[0], "requester_user_id")


def test_FR14_approvals_in_scope_expired_pending(repo, conn):
    _seed_policy(conn)
    insert_approval(
        conn, approval_id="a-expired", requester_user_id="u1", node_id="node1", action="open",
        status="pending", created_at="2020-01-01T00:00:00+00:00",
        pending_until="2020-01-01T00:10:00+00:00", valid_until=None,
    )

    records = repo.approvals_in_scope("u1", "node1")

    assert len(records) == 1
    assert records[0].status == ApprovalStatus.PENDING
    assert records[0].pending_until.isoformat() == "2020-01-01T00:10:00+00:00"


def test_FR14_approvals_in_scope_scope_only(repo, conn):
    _seed_policy(conn)
    insert_user(conn, user_id="u2", token_hash="hash-u2")
    insert_node(conn, node_id="node2")
    insert_policy(conn, policy_id="policy2", node_id="node2")
    insert_approval(
        conn, approval_id="a-other-user", requester_user_id="u2", node_id="node1", action="open",
        status="approved", pending_until="2026-01-01T00:10:00+00:00", valid_until="2099-01-01T00:00:00+00:00",
    )
    insert_approval(
        conn, approval_id="a-other-node", requester_user_id="u1", node_id="node2", action="open",
        status="approved", policy_id="policy2", pending_until="2026-01-01T00:10:00+00:00",
        valid_until="2099-01-01T00:00:00+00:00",
    )

    assert repo.approvals_in_scope("u1", "node1") == []
