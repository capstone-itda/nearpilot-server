"""`RepositoryPolicyChecker` (FR-14, FR-18). fake Repository로 확인한다."""

from __future__ import annotations

from datetime import datetime, timezone

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
from nearpilot.shared.models import Approval, PolicyRevision

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakePolicyRepository:
    """`current_policy`·`valid_approval`만 구현한다. 다른 메서드가 없어도 된다 —

    `check()`가 그 외의 어떤 메서드도 부르지 않는다는 사실 자체가 "저장소를 바꾸지 않는다"를
    증명한다. 불렀다면 `AttributeError`로 테스트가 실패했을 것이다.
    """

    def __init__(self, policy: PolicyRevision | None, approval: Approval | None = None):
        self._policy = policy
        self._approval = approval
        self.current_policy_calls: list[str] = []
        self.valid_approval_calls: list[tuple] = []

    def current_policy(self, node_id: str) -> PolicyRevision:
        self.current_policy_calls.append(node_id)
        if self._policy is None:
            raise LookupError(f"policy not found for {node_id}")
        return self._policy

    def valid_approval(self, user_id, node_id, action, policy_version, now):
        self.valid_approval_calls.append((user_id, node_id, action, policy_version, now))
        return self._approval


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


def test_FR14_check_no_policy():
    repo = FakePolicyRepository(policy=None)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.DENY
    assert result.reason == ReasonCode.POLICY_DENIED
    assert result.policy_version is None
    assert result.approval_id is None


def test_FR14_check_open():
    repo = FakePolicyRepository(policy=_open_policy(version=3))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.OK
    assert result.reason == ReasonCode.OK
    assert result.policy_version == 3
    assert result.approval_id is None
    assert repo.valid_approval_calls == []  # open이면 승인 조회를 아예 안 한다


def test_FR14_check_valid_approval():
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=2))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.OK
    assert result.reason == ReasonCode.OK
    assert result.approval_id == "appr1"


def test_FR14_check_valid_approval_unlimited_uses():
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=None))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.OK
    assert result.approval_id == "appr1"


def test_FR14_check_exhausted():
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=_approval(remaining_uses=0))
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.NEED_APPROVAL
    assert result.reason == ReasonCode.APPROVAL_EXHAUSTED
    assert result.approval_id is None


def test_FR14_check_no_approval():
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.verdict == Verdict.NEED_APPROVAL
    assert result.reason == ReasonCode.APPROVAL_REQUIRED
    assert result.approval_id is None


@pytest.mark.parametrize(
    "actual_status_hint",
    ["pending", "rejected", "expired", "wrong_version", "wrong_action"],
)
def test_FR14_check_falls_back_to_required(actual_status_hint):
    """valid_approval()이 None이면 실제 이유(대기·거부·만료 등)와 상관없이 지금은 APPROVAL_REQUIRED다.

    Repository에 이유를 구분할 조회 접점이 아직 없다 (spec 7절 가정).
    """
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=None)
    checker = RepositoryPolicyChecker(repo)

    result = checker.check("u1", "node1", Action.OPEN, NOW)

    assert result.reason == ReasonCode.APPROVAL_REQUIRED


def test_FR14_check_uses_single_valid_approval():
    approval = _approval(remaining_uses=1)
    repo = FakePolicyRepository(policy=_restricted_policy(), approval=approval)
    checker = RepositoryPolicyChecker(repo)

    checker.check("u1", "node1", Action.OPEN, NOW)

    assert len(repo.valid_approval_calls) == 1


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
    repo = FakePolicyRepository(policy=_restricted_policy(version=5), approval=None)
    checker = RepositoryPolicyChecker(repo)

    checker.check("u1", "node1", Action.OPEN, NOW)

    assert repo.valid_approval_calls[0][3] == 5
