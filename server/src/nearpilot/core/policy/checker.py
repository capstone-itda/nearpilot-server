"""`PolicyChecker` 구현 (specs/core-policy-check/spec.md, specs/core-policy-reasons/spec.md).

`core` 계층이므로 SQL을 직접 쓰지 않고 `shared.Repository` Protocol에만 의존한다
(`AGENTS.md` 의존 규칙 4). 정책 모드, 승인 범위, 정책 버전 비교 규칙은 이 함수 안에 둔다.
저장소를 바꾸지 않는다. 승인 사용분 예약은 `reserve_if_free()`의 몫이다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from nearpilot.shared.enums import AccessMode, Action, ApprovalStatus, ReasonCode, Verdict
from nearpilot.shared.interfaces import Repository
from nearpilot.shared.models import ApprovalRecord, PolicyCheckResult


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
        if approval is not None:
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

        # 유효한 승인이 없다 (#60). 승인 이력으로 사유 7개 중 하나를 고른다.
        records = self._repository.approvals_in_scope(user_id, node_id)
        reason = self._reason_from_history(records, action, policy.version, now)
        return PolicyCheckResult(verdict=Verdict.NEED_APPROVAL, reason=reason, policy_version=policy.version)

    @staticmethod
    def _reason_from_history(
        records: Sequence[ApprovalRecord], action: Action, policy_version: int, now: datetime
    ) -> ReasonCode:
        """승인 필요 사유를 고른다 (`architecture.md` §4 "승인 이력과 승인 필요 사유", #60)."""
        current = [r for r in records if r.policy_version == policy_version]
        if not current:
            return ReasonCode.APPROVAL_REQUIRED
        same_action = [r for r in current if r.action == action]
        if not same_action:
            return ReasonCode.ACTION_NOT_ALLOWED
        if any(r.status == ApprovalStatus.PENDING and now < r.pending_until for r in same_action):
            return ReasonCode.APPROVAL_PENDING

        # 만료된 pending도 포함해 생성 시각이 가장 늦은 승인으로 정한다. 생성 시각이 같으면
        # approval_id 오름차순의 마지막 것을 고른다 — 같은 입력에 같은 결과를 낸다 (FR-18).
        latest = max(same_action, key=lambda r: (r.created_at, r.approval_id))
        if latest.status == ApprovalStatus.PENDING:
            return ReasonCode.APPROVAL_REQUEST_EXPIRED
        if latest.status == ApprovalStatus.DENIED:
            return ReasonCode.APPROVAL_REJECTED
        # approved: valid_approval()이 이미 이 승인을 찾지 못했으므로 만료됐거나 소진됐다.
        # 둘 다라면 시간을 먼저 본다 (spec §6 결정).
        if latest.valid_until is not None and now >= latest.valid_until:
            return ReasonCode.APPROVAL_EXPIRED
        return ReasonCode.APPROVAL_EXHAUSTED
