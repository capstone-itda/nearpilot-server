"""`RepositoryPolicyChecker` (FR-11, FR-14, FR-18). fake Repository로 확인한다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.policy import RepositoryPolicyChecker
from nearpilot.shared.enums import (
    AccessMode,
    Action,
    ApprovalMode,
    ApprovalStatus,
    ReasonCode,
    Verdict,
    Visibility,
)
from nearpilot.shared.models import Approval, ApprovalRecord, PolicyCheckResult, PolicyRevision

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakePolicyRepository:
    """`current_policy`·`valid_approval`·`approvals_in_scope`만 구현한다. 다른 메서드가 없어도 된다 —

    `check()`가 그 외의 어떤 메서드도 부르지 않는다는 사실 자체가 "저장소를 바꾸지 않는다"를
    증명한다. 불렀다면 `AttributeError`로 테스트가 실패했을 것이다.
    """

    def __init__(
        self,
        policy: PolicyRevision | None,
        approval: Approval | None = None,
        approvals: list[ApprovalRecord] | None = None,
        approvals_in_scope_error: Exception | None = None,
    ):
        self._policy = policy
        self._approval = approval
        self._approvals = approvals if approvals is not None else []
        self._approvals_in_scope_error = approvals_in_scope_error
        self.current_policy_calls: list[str] = []
        self.valid_approval_calls: list[tuple] = []
        self.valid_approval_request_ids: list[str | None] = []
        self.approvals_in_scope_calls: list[tuple] = []

    def current_policy(self, node_id: str) -> PolicyRevision:
        self.current_policy_calls.append(node_id)
        if self._policy is None:
            raise LookupError(f"policy not found for {node_id}")
        return self._policy

    def valid_approval(self, user_id, node_id, action, policy_version, now, *, internal_request_id=None):
        self.valid_approval_calls.append((user_id, node_id, action, policy_version, now))
        self.valid_approval_request_ids.append(internal_request_id)
        return self._approval

    def approvals_in_scope(self, user_id, node_id):
        self.approvals_in_scope_calls.append((user_id, node_id))
        if self._approvals_in_scope_error is not None:
            raise self._approvals_in_scope_error
        return self._approvals


def _open_policy(node_id="node1", version=1):
    return PolicyRevision(
        policy_id="policy1", version=version, node_id=node_id, visibility=Visibility.PUBLIC,
        mode=AccessMode.OPEN, approver_user_id=None, approval_mode=ApprovalMode.ONE_TIME,
        approval_ttl_sec=None, max_uses=None,
    )


def _restricted_policy(node_id="node1", version=1, approver_user_id="owner1"):
    return PolicyRevision(
        policy_id="policy1", version=version, node_id=node_id, visibility=Visibility.PRIVATE,
        mode=AccessMode.RESTRICTED, approver_user_id=approver_user_id,
        approval_mode=ApprovalMode.ONE_TIME, approval_ttl_sec=3600, max_uses=1,
    )


def _approval(remaining_uses, approval_id="appr1", action=Action.OPEN, policy_version=1):
    return Approval(
        approval_id=approval_id, node_id="node1", requester_user_id="u1", action=action,
        policy_id="policy1", policy_version=policy_version,
        status=ApprovalStatus.APPROVED, valid_until=datetime(2099, 1, 1, tzinfo=timezone.utc),
        remaining_uses=remaining_uses,
    )


def _record(
    approval_id, status, created_at=NOW, action=Action.OPEN, policy_version=1,
    pending_until=None, valid_until=None, remaining_uses=None,
):
    return ApprovalRecord(
        approval_id=approval_id, action=action, policy_version=policy_version, status=status,
        created_at=created_at,
        pending_until=pending_until if pending_until is not None else created_at + timedelta(minutes=10),
        valid_until=valid_until, remaining_uses=remaining_uses,
    )


def test_FR14_policy_missing_deny():
    repo = FakePolicyRepository(policy=None)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.DENY
    assert result.reason == ReasonCode.POLICY_DENIED
    assert result.policy_version is None
    assert result.approval_id is None


def test_FR14_policy_open_ok():
    repo = FakePolicyRepository(policy=_open_policy(version=3))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.OK
    assert result.reason == ReasonCode.OK
    assert result.policy_version == 3
    assert result.approval_id is None
    assert repo.valid_approval_calls == []  # open이면 승인 조회를 아예 안 한다


def test_FR14_policy_valid_approval_ok():
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=2))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.OK
    assert result.reason == ReasonCode.OK
    assert result.approval_id == "appr1"
    assert repo.approvals_in_scope_calls == []  # 유효한 승인이 있으면 이력을 조회하지 않는다


def test_FR14_check_valid_approval_unlimited_uses():
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=None))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.OK
    assert result.approval_id == "appr1"


def test_FR14_check_exhausted():
    """valid_approval()이 시간상 유효한 승인을 반환했지만 남은 횟수가 0인 기존 경로다."""
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=0))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.NEED_APPROVAL
    assert result.reason == ReasonCode.APPROVAL_EXHAUSTED
    assert result.approval_id is None


def test_FR14_reason_required_no_history():
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=[])
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.NEED_APPROVAL
    assert result.reason == ReasonCode.APPROVAL_REQUIRED
    assert result.approval_id is None


def test_FR14_reason_required_old_version():
    approvals = [_record("a1", ApprovalStatus.APPROVED, policy_version=1, remaining_uses=5)]
    repo = FakePolicyRepository(policy=_restricted_policy(version=2), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_REQUIRED


def test_FR14_reason_action_not_allowed():
    approvals = [_record("a1", ApprovalStatus.APPROVED, action=Action.ON, remaining_uses=5)]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.ACTION_NOT_ALLOWED


def test_FR14_reason_pending():
    approvals = [_record("a1", ApprovalStatus.PENDING, pending_until=NOW + timedelta(minutes=5))]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_PENDING


def test_FR14_reason_pending_over_older_denied():
    approvals = [
        _record("a1", ApprovalStatus.DENIED, created_at=NOW - timedelta(days=1)),
        _record("a2", ApprovalStatus.PENDING, created_at=NOW, pending_until=NOW + timedelta(minutes=5)),
    ]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_PENDING


def test_FR14_reason_request_expired():
    approvals = [_record("a1", ApprovalStatus.PENDING, pending_until=NOW - timedelta(minutes=1))]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_REQUEST_EXPIRED


def test_FR14_reason_rejected():
    approvals = [_record("a1", ApprovalStatus.DENIED)]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_REJECTED


def test_FR14_reason_expired():
    approvals = [
        _record("a1", ApprovalStatus.APPROVED, valid_until=NOW - timedelta(minutes=1), remaining_uses=5)
    ]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_EXPIRED


def test_FR14_reason_exhausted():
    approvals = [
        _record("a1", ApprovalStatus.APPROVED, valid_until=NOW + timedelta(days=1), remaining_uses=0)
    ]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_EXHAUSTED


def test_FR14_reason_expired_before_exhausted():
    approvals = [
        _record("a1", ApprovalStatus.APPROVED, valid_until=NOW - timedelta(minutes=1), remaining_uses=0)
    ]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_EXPIRED


def test_FR14_reason_latest_wins():
    approvals = [
        _record(
            "a1", ApprovalStatus.APPROVED, created_at=NOW - timedelta(days=2),
            valid_until=NOW - timedelta(days=1), remaining_uses=5,
        ),
        _record("a2", ApprovalStatus.DENIED, created_at=NOW - timedelta(hours=1)),
    ]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_REJECTED


def test_FR14_reason_tie_deterministic():
    approvals = [
        _record("b-later-id", ApprovalStatus.DENIED, created_at=NOW),
        _record("a-earlier-id", ApprovalStatus.APPROVED, created_at=NOW, valid_until=NOW + timedelta(days=1), remaining_uses=5),
    ]
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None, approvals=approvals)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    # 생성 시각이 같으면 approval_id 오름차순의 마지막("b-later-id")을 고른다.
    assert result.reason == ReasonCode.APPROVAL_REJECTED


def test_FR14_reason_lookup_error_raises():
    repo = FakePolicyRepository(
        policy=_restricted_policy(), approval=None, approvals_in_scope_error=RuntimeError("db down")
    )
    checker = RepositoryPolicyChecker(repo)

    with pytest.raises(RuntimeError):
        checker.check("u1", "node1", Action.OPEN, NOW)


def test_FR11_policy_result_has_no_user():
    assert "user_id" not in PolicyCheckResult.__dataclass_fields__


def test_FR14_check_does_not_write():
    """fake Repository에 쓰기 메서드가 하나도 없다. check()가 예외 없이 끝나면 쓰지 않은 것이다."""
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=1))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.OK


def test_FR18_check_does_not_substitute_node():
    repo = FakePolicyRepository(policy=_restricted_policy(node_id="node1"), approval=_approval(remaining_uses=1))
    checker = RepositoryPolicyChecker(repo)

    checker.check("u1", "node1", Action.OPEN, NOW)

    assert repo.current_policy_calls == ["node1"]
    assert all(call[1] == "node1" for call in repo.valid_approval_calls)


def test_FR14_check_passes_policy_version_to_valid_approval():
    repo = FakePolicyRepository(policy=_restricted_policy(version=5), approval=None, approvals=[])
    checker = RepositoryPolicyChecker(repo)

    checker.check("u1", "node1", Action.OPEN, NOW)

    assert repo.valid_approval_calls[0][3] == 5


def test_FR14_check_uses_single_valid_approval():
    approval = _approval(remaining_uses=1)
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=approval)
    checker = RepositoryPolicyChecker(repo)

    checker.check("u1", "node1", Action.OPEN, NOW)

    assert len(repo.valid_approval_calls) == 1


def test_FR14_check_passes_internal_request_id():
    """받은 요청 ID를 저장소에 그대로 넘긴다. 주지 않으면 None 이라 모든 사용분을 센다."""
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=1))
    checker = RepositoryPolicyChecker(repo)

    checker.check("u1", "node1", Action.OPEN, NOW)
    checker.check("u1", "node1", Action.OPEN, NOW, internal_request_id="req-1")

    assert repo.valid_approval_request_ids == [None, "req-1"]
