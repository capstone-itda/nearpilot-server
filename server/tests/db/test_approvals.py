"""승인 신청·결정: create_approval_request, decide_approval."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, ApprovalDecisionReason, ApprovalStatus
from nearpilot.shared.models import ApprovalDecisionRequest, ApprovalRequest

from conftest import add_policy_revision, insert_node, insert_place, insert_policy, insert_user


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _request(internal_request_id="areq-1", created_at=NOW, settings=None):
    return ApprovalRequest(
        requester_user_id="u1",
        node_id="node1",
        action=Action.OPEN,
        policy_id="policy1",
        policy_version=1,
        internal_request_id=internal_request_id,
        created_at=created_at,
        settings=settings or Settings(),
    )


def _setup(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_policy(conn, approval_ttl_sec=3600)


def test_FR03_create_approval_request_new(conn, repo):
    _setup(conn)

    result = repo.create_approval_request(_request())

    assert result.is_new is True
    assert result.status == ApprovalStatus.PENDING
    assert result.pending_until == NOW + timedelta(
        seconds=Settings().approval_pending_ttl_sec
    )


def test_FR14_create_approval_request_dedup(conn, repo):
    _setup(conn)

    first = repo.create_approval_request(_request(internal_request_id="areq-1"))
    second = repo.create_approval_request(_request(internal_request_id="areq-2", created_at=NOW))

    assert second.is_new is False
    assert second.approval_id == first.approval_id


def test_FR14_create_approval_request_after_expiry(conn, repo):
    _setup(conn)
    settings = Settings(approval_pending_ttl_sec=60)
    repo.create_approval_request(_request(internal_request_id="areq-1", settings=settings))

    later = NOW + timedelta(seconds=120)
    result = repo.create_approval_request(_request(internal_request_id="areq-2", created_at=later, settings=settings))

    assert result.is_new is True


def test_FR14_decide_approval_not_found(conn, repo):
    _setup(conn)

    result = repo.decide_approval(
        ApprovalDecisionRequest(
            approval_id="missing", decided_by_user_id="u1", decision=ApprovalStatus.APPROVED, decided_at=NOW
        )
    )

    assert result.applied is False
    assert result.reason == ApprovalDecisionReason.NOT_FOUND


def test_FR14_decide_approval_approves(conn, repo):
    _setup(conn)
    pending = repo.create_approval_request(_request())

    result = repo.decide_approval(
        ApprovalDecisionRequest(
            approval_id=pending.approval_id, decided_by_user_id="u1",
            decision=ApprovalStatus.APPROVED, decided_at=NOW,
        )
    )

    assert result.applied is True
    assert result.reason == ApprovalDecisionReason.OK
    assert result.status == ApprovalStatus.APPROVED
    assert result.valid_until == NOW + timedelta(seconds=3600)


def test_FR14_decide_approval_idempotent(conn, repo):
    _setup(conn)
    pending = repo.create_approval_request(_request())
    decision = ApprovalDecisionRequest(
        approval_id=pending.approval_id, decided_by_user_id="u1",
        decision=ApprovalStatus.APPROVED, decided_at=NOW,
    )
    repo.decide_approval(decision)

    second = repo.decide_approval(decision)

    assert second.applied is False
    assert second.reason == ApprovalDecisionReason.ALREADY_APPLIED
    assert second.status == ApprovalStatus.APPROVED


def test_FR14_decide_approval_conflicting(conn, repo):
    _setup(conn)
    pending = repo.create_approval_request(_request())
    repo.decide_approval(
        ApprovalDecisionRequest(
            approval_id=pending.approval_id, decided_by_user_id="u1",
            decision=ApprovalStatus.APPROVED, decided_at=NOW,
        )
    )

    second = repo.decide_approval(
        ApprovalDecisionRequest(
            approval_id=pending.approval_id, decided_by_user_id="u1",
            decision=ApprovalStatus.DENIED, decided_at=NOW,
        )
    )

    assert second.applied is False
    assert second.reason == ApprovalDecisionReason.ALREADY_DECIDED
    assert second.status == ApprovalStatus.APPROVED


def test_FR14_decide_approval_request_expired(conn, repo):
    _setup(conn)
    settings = Settings(approval_pending_ttl_sec=60)
    pending = repo.create_approval_request(_request(settings=settings))

    later = NOW + timedelta(seconds=120)
    result = repo.decide_approval(
        ApprovalDecisionRequest(
            approval_id=pending.approval_id, decided_by_user_id="u1",
            decision=ApprovalStatus.APPROVED, decided_at=later,
        )
    )

    assert result.applied is False
    assert result.reason == ApprovalDecisionReason.REQUEST_EXPIRED


def test_FR14_decide_approval_invalid(conn, repo):
    _setup(conn)
    pending = repo.create_approval_request(_request())
    add_policy_revision(conn, version=2)

    result = repo.decide_approval(
        ApprovalDecisionRequest(
            approval_id=pending.approval_id, decided_by_user_id="u1",
            decision=ApprovalStatus.APPROVED, decided_at=NOW,
        )
    )

    assert result.applied is False
    assert result.reason == ApprovalDecisionReason.POLICY_VERSION_MISMATCH
