"""`Repository`의 승인 권한자 조회 (specs/core-approval-service/spec.md, #186)."""

from __future__ import annotations

from datetime import datetime, timezone

from nearpilot.shared.enums import Action, ApprovalStatus

from conftest import add_policy_revision, insert_approval, insert_node, insert_place, insert_policy, insert_user

NOW = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)


def _seed(conn, approver="owner"):
    insert_user(conn, user_id="owner", token_hash="hash-owner", role="owner")
    insert_user(conn, user_id="visitor", token_hash="hash-visitor")
    insert_user(conn, user_id="other", token_hash="hash-other", role="owner")
    insert_place(conn, owner_user_id="owner")
    insert_node(conn, node_id="node1")
    insert_policy(conn, policy_id="policy1", node_id="node1", approver_user_id=approver)


def _pending(conn, approval_id, created_at="2026-01-01T00:00:00+00:00",
             pending_until="2026-01-01T00:10:00+00:00", **kwargs):
    insert_approval(
        conn, approval_id=approval_id, requester_user_id="visitor", status="pending",
        created_at=created_at, pending_until=pending_until, valid_until=None,
        internal_request_id=f"req-{approval_id}", **kwargs,
    )


def test_FR03_pending_for_only_approver(repo, conn):
    _seed(conn)
    _pending(conn, "a1")

    owner = repo.pending_approvals_for("owner", NOW)

    assert [a.approval_id for a in owner] == ["a1"]
    assert owner[0].node_id == "node1"
    assert owner[0].action == Action.OPEN
    assert owner[0].status == ApprovalStatus.PENDING
    assert owner[0].pending_until == datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)
    assert owner[0].valid_until is None
    assert repo.pending_approvals_for("other", NOW) == []
    assert repo.pending_approvals_for("visitor", NOW) == []


def test_FR03_pending_excludes_expired_and_decided(repo, conn):
    _seed(conn)
    _pending(conn, "a-live")
    _pending(conn, "a-edge", pending_until="2026-01-01T00:05:00+00:00")
    _pending(conn, "a-expired", pending_until="2026-01-01T00:04:59+00:00")
    insert_approval(conn, approval_id="a-approved", requester_user_id="visitor", status="approved",
                    pending_until="2026-01-01T00:10:00+00:00", internal_request_id="req-approved")
    insert_approval(conn, approval_id="a-denied", requester_user_id="visitor", status="denied",
                    pending_until="2026-01-01T00:10:00+00:00", valid_until=None,
                    internal_request_id="req-denied")

    ids = [a.approval_id for a in repo.pending_approvals_for("owner", NOW)]

    # 대기 만료와 같은 시각은 decide_approval 도 아직 반영하므로 목록에 남는다.
    assert sorted(ids) == ["a-edge", "a-live"]


def test_FR03_pending_order(repo, conn):
    _seed(conn)
    _pending(conn, "a-b", created_at="2026-01-01T00:02:00+00:00")
    _pending(conn, "a-c", created_at="2026-01-01T00:01:00+00:00")
    _pending(conn, "a-a", created_at="2026-01-01T00:02:00+00:00")

    assert [a.approval_id for a in repo.pending_approvals_for("owner", NOW)] == ["a-c", "a-a", "a-b"]


def test_FR03_approval_by_id(repo, conn):
    _seed(conn)
    insert_approval(conn, approval_id="a1", requester_user_id="visitor", status="approved",
                    pending_until="2026-01-01T00:10:00+00:00", valid_until="2026-01-01T01:00:00+00:00")

    found = repo.approval("a1")

    assert found.approval_id == "a1"
    assert found.status == ApprovalStatus.APPROVED
    assert found.valid_until == datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    assert repo.approval("missing") is None


def test_FR14_is_approver_uses_approval_revision(repo, conn):
    _seed(conn)
    _pending(conn, "a1")
    # 새 리비전의 권한자는 옛 리비전의 승인을 결정할 권한이 없다.
    add_policy_revision(conn, policy_id="policy1", version=2, approver_user_id="other")

    assert repo.is_approver("owner", "a1") is True
    assert repo.is_approver("other", "a1") is False
    assert repo.is_approver("visitor", "a1") is False
    assert repo.is_approver("owner", "missing") is False


def test_FR14_no_approver_means_no_authority(repo, conn):
    _seed(conn, approver=None)
    _pending(conn, "a1")

    assert repo.is_approver("owner", "a1") is False
    assert repo.pending_approvals_for("owner", NOW) == []
