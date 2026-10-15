"""FR-09 request_use 조립: 판정에서 실행 인계까지.

판정(`DecisionPipeline`)이 새로 OK 를 낸 요청만 `ExecutionService.execute()` 로 인계하고,
실행 결과를 확인한 뒤의 판정을 호스트에 돌려준다. OK 와 use_id 는 실행 성공일 때만 나간다 (#16).
재전송에는 저장된 실행 상태로 다시 만든 판정을 돌려준다. 기본값은 거부다.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from nearpilot.core.decision.pipeline import DecisionPipeline, make_decision
from nearpilot.shared.enums import ExecutionStatus, ReasonCode, Verdict
from nearpilot.shared.interfaces import ExecutionService, ExecutionStore
from nearpilot.shared.models import Decision, ExecutionResult, UseRequest

log = logging.getLogger(__name__)

# 실행 결과 → 판정과 사유. SUCCEEDED 와 NOT_SENT 는 따로 처리한다.
_BY_STATUS: dict[ExecutionStatus, tuple[Verdict, ReasonCode]] = {
    ExecutionStatus.REJECTED: (Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    ExecutionStatus.FAILED: (Verdict.DENY, ReasonCode.EXEC_FAILED),
    ExecutionStatus.UNKNOWN: (Verdict.DENY, ReasonCode.EXEC_UNKNOWN),
    ExecutionStatus.PENDING: (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    ExecutionStatus.IN_PROGRESS: (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
}


# 재검사 밖의 중단 사유 → 호스트 판정과 사유 (#79). 내부 판단 근거는 공개하지 않는다.
_BY_REASON: dict[ReasonCode, tuple[Verdict, ReasonCode]] = {
    ReasonCode.COMMAND_IN_PROGRESS: (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    ReasonCode.DEVICE_BUSY: (Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    ReasonCode.TARGET_UNAVAILABLE: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    ReasonCode.NODE_NOT_CONNECTED: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    ReasonCode.RESERVATION_EXPIRED: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    ReasonCode.COMMAND_NOT_SENT: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
}


class RequestUseService:
    def __init__(
        self, pipeline: DecisionPipeline, execution: ExecutionService, store: ExecutionStore
    ) -> None:
        self._pipeline = pipeline
        self._execution = execution
        self._store = store

    def request_use(self, req: UseRequest) -> Decision:
        decision, ctx = self._pipeline.decide_for_execution(req)
        try:
            if ctx is not None:  # 이번에 새로 OK 가 난 요청
                return self._from_result(decision, self._execution.execute(ctx))
            if decision.verdict is not Verdict.OK:
                return decision
            return self._replay(decision)  # 재전송: 이미 인계한 요청의 현재 실행 상태
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            log.exception("request_use 조립 중 예외")
            return self._failure(decision, ReasonCode.INTERNAL_ERROR)

    # ── 실행 결과 → 판정 ────────────────────────────────────────────

    def _replay(self, decision: Decision) -> Decision:
        record = self._store.execution(decision.internal_request_id)
        if record is None:  # 인계가 끝나지 않았다. context 를 다시 만들 수 없다
            return self._failure(decision, ReasonCode.INTERNAL_ERROR)
        return self._map(decision, record.status, record.use_id, None, None)

    def _from_result(self, decision: Decision, result: ExecutionResult) -> Decision:
        return self._map(decision, result.status, result.use_id, result.recheck, result.reason)

    def _map(
        self, decision: Decision, status: ExecutionStatus, use_id: str | None,
        recheck: Decision | None, reason: ReasonCode | None,
    ) -> Decision:
        if status is ExecutionStatus.SUCCEEDED:
            return replace(decision, verdict=Verdict.OK, reason=ReasonCode.OK, use_id=use_id,
                           message=None)
        if recheck is not None and status is ExecutionStatus.NOT_SENT:
            return recheck  # 재검사 판정이 중단 사유보다 먼저다
        if reason is not None:
            by_reason = _BY_REASON.get(reason)  # 알 수 없는 사유는 OK 가 되지 않는다
            if by_reason is None:
                return self._failure(decision, ReasonCode.INTERNAL_ERROR)
            return self._failure(decision, by_reason[1], by_reason[0])
        if status is ExecutionStatus.NOT_SENT:
            return self._failure(decision, ReasonCode.INTERNAL_ERROR)
        mapped = _BY_STATUS.get(status)
        if mapped is None:  # 알 수 없는 상태는 OK 가 되지 않는다
            return self._failure(decision, ReasonCode.INTERNAL_ERROR)
        return self._failure(decision, mapped[1], mapped[0])

    @staticmethod
    def _failure(
        decision: Decision, reason: ReasonCode, verdict: Verdict = Verdict.DENY
    ) -> Decision:
        """OK 가 아닌 결과. use_id 는 지우고 나머지 식별 값은 유지한다."""
        return make_decision(decision.request_id, decision.internal_request_id, verdict, reason,
                             node_id=decision.node_id, p_max=decision.p_max)
