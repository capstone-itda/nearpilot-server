"""#23 ④ 정책 검사 shared 계약 검사."""

from dataclasses import fields
from datetime import datetime

from nearpilot.shared.enums import Action, ReasonCode, Verdict
from nearpilot.shared.interfaces import PolicyChecker
from nearpilot.shared.models import PolicyCheckResult


def test_fr14_policy_check_result_carries_policy_version_and_approval_id():
    result = PolicyCheckResult(Verdict.OK, ReasonCode.OK, 3, "ap-1")
    assert result.policy_version == 3
    assert result.approval_id == "ap-1"
    assert PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, 3).approval_id is None


def test_fr14_fr18_need_approval_reasons_are_distinguishable():
    reasons = {
        ReasonCode.APPROVAL_REQUIRED,
        ReasonCode.APPROVAL_PENDING,
        ReasonCode.APPROVAL_REJECTED,
        ReasonCode.APPROVAL_EXPIRED,
        ReasonCode.APPROVAL_REQUEST_EXPIRED,
        ReasonCode.APPROVAL_EXHAUSTED,
        ReasonCode.ACTION_NOT_ALLOWED,
    }
    results = [PolicyCheckResult(Verdict.NEED_APPROVAL, r, 1) for r in reasons]
    assert len({r.reason for r in results}) == 7


def test_fr03_pending_expiry_reason_is_separate_from_rejection_and_approval_expiry():
    assert ReasonCode.APPROVAL_REQUEST_EXPIRED.value == "APPROVAL_REQUEST_EXPIRED"
    assert ReasonCode.APPROVAL_REQUEST_EXPIRED is not ReasonCode.APPROVAL_REJECTED
    assert ReasonCode.APPROVAL_REQUEST_EXPIRED is not ReasonCode.APPROVAL_EXPIRED


def test_fr14_policy_check_result_allows_no_policy_version_on_deny():
    result = PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, None)
    assert result.policy_version is None


def test_fr11_policy_check_result_has_no_user_fields():
    names = {f.name for f in fields(PolicyCheckResult)}
    assert not names & {"user_id", "requester_user_id", "approver_user_id"}


def test_fr18_fake_policy_checker_satisfies_protocol():
    class FakePolicyChecker:
        def check(self, user_id, node_id, action, now):
            return PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)

    checker = FakePolicyChecker()
    assert isinstance(checker, PolicyChecker)
    assert checker.check("u1", "locker-1", Action.OPEN, datetime(2026, 10, 1)).verdict is Verdict.OK
