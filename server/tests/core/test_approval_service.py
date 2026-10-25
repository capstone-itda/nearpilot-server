"""FR-03 승인 조회·결정 서비스 (specs/core-approval-service/spec.md, #186). 저장소는 fake 다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.approval import OwnerApprovalService
from nearpilot.shared.enums import Action, ApprovalDecisionReason, ApprovalStatus
from nearpilot.shared.errors import StoreError
from nearpilot.shared.models import ApprovalDecisionResult, OwnerApproval

NOW = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
PENDING = OwnerApproval(
    "a1", "locker-1", Action.OPEN, ApprovalStatus.PENDING, NOW - timedelta(minutes=5),
    NOW + timedelta(minutes=5), None,
)


class FakeRepo:
    def __init__(self, approvals=(PENDING,), approver="owner"):
        self.approvals = {a.approval_id: a for a in approvals}
        self.approver = approver
        self.decide_result = None
        self.decided = []
        self.pending_calls = []
        self.error = None

    def pending_approvals_for(self, user_id, now):
        self.pending_calls.append((user_id, now))
        return [a for a in self.approvals.values() if user_id == self.approver]

    def approval(self, approval_id):
        if self.error:
            raise self.error
        return self.approvals.get(approval_id)

    def is_approver(self, user_id, approval_id):
        return approval_id in self.approvals and user_id == self.approver

    def decide_approval(self, req):
        self.decided.append(req)
        result = self.decide_result or ApprovalDecisionResult(True, ApprovalDecisionReason.OK, req.decision, None)
        if result.reason in (ApprovalDecisionReason.OK, ApprovalDecisionReason.ALREADY_APPLIED):
            old = self.approvals[req.approval_id]
            self.approvals[req.approval_id] = OwnerApproval(
                old.approval_id, old.node_id, old.action, req.decision, old.created_at, old.pending_until,
                result.valid_until)
        return result


def _service(repo):
    return OwnerApprovalService(repo, lambda: NOW)


def test_FR03_pending_for_passes_clock():
    repo = FakeRepo()

    assert _service(repo).pending_for("owner") == [PENDING]
    assert repo.pending_calls == [("owner", NOW)]


def test_FR03_get_returns_approval():
    result = _service(FakeRepo()).get("owner", "a1")

    assert result.reason is ApprovalDecisionReason.OK
    assert result.succeeded
    assert result.approval == PENDING


def test_FR03_get_unknown_is_not_found():
    result = _service(FakeRepo()).get("owner", "missing")

    assert result.reason is ApprovalDecisionReason.NOT_FOUND
    assert result.approval is None


def test_FR03_get_by_non_approver_is_not_authorized():
    result = _service(FakeRepo()).get("visitor", "a1")

    assert result.reason is ApprovalDecisionReason.NOT_AUTHORIZED
    assert result.approval is None
    assert not result.succeeded


def test_FR03_decide_sends_request_with_clock():
    repo = FakeRepo()

    result = _service(repo).decide("owner", "a1", ApprovalStatus.APPROVED)

    [req] = repo.decided
    assert (req.approval_id, req.decided_by_user_id, req.decision, req.decided_at) == (
        "a1", "owner", ApprovalStatus.APPROVED, NOW)
    assert result.reason is ApprovalDecisionReason.OK
    assert result.approval.status is ApprovalStatus.APPROVED


def test_FR03_decide_unknown_is_not_found():
    repo = FakeRepo()

    result = _service(repo).decide("owner", "missing", ApprovalStatus.DENIED)

    assert result.reason is ApprovalDecisionReason.NOT_FOUND
    assert repo.decided == []


def test_FR03_decide_by_non_approver_is_not_authorized():
    repo = FakeRepo()

    result = _service(repo).decide("visitor", "a1", ApprovalStatus.APPROVED)

    assert result.reason is ApprovalDecisionReason.NOT_AUTHORIZED
    assert result.approval is None
    assert repo.decided == []


def test_FR03_decide_same_twice_is_already_applied():
    repo = FakeRepo()
    service = _service(repo)
    service.decide("owner", "a1", ApprovalStatus.APPROVED)
    repo.decide_result = ApprovalDecisionResult(
        False, ApprovalDecisionReason.ALREADY_APPLIED, ApprovalStatus.APPROVED, None)

    again = service.decide("owner", "a1", ApprovalStatus.APPROVED)
    # 재호출에서도 권한을 검사한다.
    other = service.decide("visitor", "a1", ApprovalStatus.APPROVED)

    assert again.reason is ApprovalDecisionReason.ALREADY_APPLIED
    assert again.succeeded
    assert again.approval.status is ApprovalStatus.APPROVED
    assert other.reason is ApprovalDecisionReason.NOT_AUTHORIZED
    assert len(repo.decided) == 2


@pytest.mark.parametrize("reason", [
    ApprovalDecisionReason.ALREADY_DECIDED,
    ApprovalDecisionReason.POLICY_VERSION_MISMATCH,
    ApprovalDecisionReason.REQUEST_EXPIRED,
])
def test_FR03_decide_passes_store_reason(reason):
    repo = FakeRepo()
    repo.decide_result = ApprovalDecisionResult(False, reason, None, None)

    result = _service(repo).decide("owner", "a1", ApprovalStatus.DENIED)

    assert result.reason is reason
    assert not result.succeeded
    assert result.approval == PENDING


def test_FR03_decide_rejects_pending_value():
    repo = FakeRepo()

    with pytest.raises(ValueError):
        _service(repo).decide("owner", "a1", ApprovalStatus.PENDING)
    assert repo.decided == []


def test_FR03_store_error_propagates():
    repo = FakeRepo()
    repo.error = StoreError("db down")

    with pytest.raises(StoreError):
        _service(repo).get("owner", "a1")
    with pytest.raises(StoreError):
        _service(repo).decide("owner", "a1", ApprovalStatus.APPROVED)
