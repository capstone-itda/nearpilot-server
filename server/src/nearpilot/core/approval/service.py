"""FR-03 승인 조회·결정 서비스 (specs/core-approval-service/spec.md, #186).

승인 REST가 부르는 `ApprovalService` 구현이다. 존재, 승인 권한을 먼저 검사하고 결정은
`Repository.decide_approval`로만 반영한다. 반환값에는 요청자 정보가 없다 (NFR-08).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from nearpilot.shared.enums import ApprovalDecisionReason, ApprovalStatus
from nearpilot.shared.interfaces import Repository
from nearpilot.shared.models import ApprovalDecisionRequest, OwnerApproval, OwnerApprovalResult

_DECISIONS = (ApprovalStatus.APPROVED, ApprovalStatus.DENIED)


class OwnerApprovalService:
    """`ApprovalService` Protocol 구현. 저장소 오류(`StoreError`)는 그대로 올린다."""

    def __init__(self, repo: Repository, clock: Callable[[], datetime]) -> None:
        self._repo = repo
        self._clock = clock

    def pending_for(self, user_id: str) -> Sequence[OwnerApproval]:
        return self._repo.pending_approvals_for(user_id, self._clock())

    def get(self, user_id: str, approval_id: str) -> OwnerApprovalResult:
        approval = self._repo.approval(approval_id)
        if approval is None:
            return OwnerApprovalResult(ApprovalDecisionReason.NOT_FOUND, None)
        if not self._repo.is_approver(user_id, approval_id):
            return OwnerApprovalResult(ApprovalDecisionReason.NOT_AUTHORIZED, None)
        return OwnerApprovalResult(ApprovalDecisionReason.OK, approval)

    def decide(self, user_id: str, approval_id: str, decision: ApprovalStatus) -> OwnerApprovalResult:
        if decision not in _DECISIONS:
            raise ValueError("소유자 결정은 approved 또는 denied 다")
        found = self.get(user_id, approval_id)
        if found.reason is not ApprovalDecisionReason.OK:
            return found
        result = self._repo.decide_approval(
            ApprovalDecisionRequest(approval_id, user_id, decision, self._clock()))
        # REST 의 성공 응답이 승인 한 건이라서, 결정 뒤의 상태를 다시 읽는다.
        return OwnerApprovalResult(result.reason, self._repo.approval(approval_id))
