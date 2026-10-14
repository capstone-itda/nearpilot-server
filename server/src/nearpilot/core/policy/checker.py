"""`PolicyChecker` 구현 (specs/db-policy-check/spec.md).

`core` 계층이므로 SQL을 직접 쓰지 않고 `shared.Repository` Protocol에만 의존한다
(`AGENTS.md` 의존 규칙 4). 정책 모드, 승인 범위, 정책 버전 비교 규칙은 이 함수 안에 둔다.
저장소를 바꾸지 않는다. 승인 사용분 예약은 `reserve_if_free()`의 몫이다.
"""

from __future__ import annotations

from datetime import datetime

from nearpilot.shared.enums import AccessMode, Action, ReasonCode, Verdict
from nearpilot.shared.interfaces import Repository
from nearpilot.shared.models import PolicyCheckResult


class RepositoryPolicyChecker:
    """`PolicyChecker` Protocol 구현."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def check(self, user_id: str, node_id: str, action: Action, now: datetime) -> PolicyCheckResult:
        try:
            policy = self._repository.current_policy(node_id)
        except LookupError:
            return PolicyCheckResult(verdict=Verdict.DENY, reason=ReasonCode.POLICY_DENIED, policy_version=None)

        if policy.mode == AccessMode.OPEN:
            return PolicyCheckResult(verdict=Verdict.OK, reason=ReasonCode.OK, policy_version=policy.version)

        approval = self._repository.valid_approval(user_id, node_id, action, policy.version, now)
        if approval is None:
            return PolicyCheckResult(
                verdict=Verdict.NEED_APPROVAL,
                reason=ReasonCode.APPROVAL_REQUIRED,
                policy_version=policy.version,
            )

        # valid_approval()은 시간상 유효성만 본다. 남은 횟수는 여기서 직접 확인한다 (FR-14).
        if approval.remaining_uses == 0:
            return PolicyCheckResult(
                verdict=Verdict.NEED_APPROVAL,
                reason=ReasonCode.APPROVAL_EXHAUSTED,
                policy_version=policy.version,
            )

        return PolicyCheckResult(
            verdict=Verdict.OK,
            reason=ReasonCode.OK,
            policy_version=policy.version,
            approval_id=approval.approval_id,
        )
