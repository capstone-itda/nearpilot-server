"""FR-09 request_use 조립: 판정에서 실행 인계까지.

판정(`DecisionPipeline`)이 새로 OK 를 낸 요청만 `ExecutionService.execute()` 로 인계하고,
실행 결과를 확인한 뒤의 판정을 호스트에 돌려준다. OK 와 use_id 는 실행 성공일 때만 나간다 (#16).
재전송에는 저장된 실행 상태로 다시 만든 판정을 돌려준다. 기본값은 거부다.
거리 선택자는 하위 요청마다 같은 규칙을 적용하고 기기별 결과를 상위 판정 하나로 묶는다 (#253).
묶음 요청도 항목마다 고른 기기의 하위 요청에 같은 규칙을 적용하고, 기기별 결과를 항목 순서와 함께 묶는다 (#284).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from enum import Enum
from typing import Any, TypeVar

from nearpilot.core.decision.pipeline import DecisionPipeline, make_decision
from nearpilot.shared.enums import ExecutionStatus, ReasonCode, Verdict
from nearpilot.shared.interfaces import ExecutionService, ExecutionStore
from nearpilot.shared.models import (
    Decision, DeviceOutcome, ExecutionContext, ExecutionResult, UseRequest,
)

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
    ReasonCode.USE_NOT_FOUND: (Verdict.DENY, ReasonCode.USE_NOT_FOUND),
    ReasonCode.TARGET_UNAVAILABLE: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    ReasonCode.NODE_NOT_CONNECTED: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    ReasonCode.RESERVATION_EXPIRED: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    ReasonCode.COMMAND_NOT_SENT: (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
}

_E = TypeVar("_E", bound=Enum)


def _member(kind: type[_E], value: object) -> _E | None:
    """저장된 근거의 값으로 열거형 항목을 찾는다. 열거형에 없으면 None 이다."""
    try:
        return kind(value)
    except ValueError:
        return None


def aggregate_selector(parent: Decision, outcomes: Sequence[Decision]) -> Decision:
    """하위 요청들의 결과를 상위 판정 하나로 묶는다 (FR-09, #253).

    outcomes 는 거리 순서의 하위 결과다. 모두 OK 면 OK 다. 아니면 처음 OK 가 아닌 결과의 판정과 사유다.
    devices 는 결과마다 하나이고 use_id 는 OK 결과에만 둔다. selected_count·similar 는 parent 의 값이다.
    상위의 node_id·use_id·p_max 는 None 이다.
    """
    devices = tuple(
        DeviceOutcome(o.node_id, o.verdict, o.reason, o.use_id if o.verdict is Verdict.OK else None)
        for o in outcomes)
    failed = next((o for o in outcomes if o.verdict is not Verdict.OK), None)
    verdict, reason = (Verdict.OK, ReasonCode.OK) if failed is None else (failed.verdict, failed.reason)
    return make_decision(parent.request_id, parent.internal_request_id, verdict, reason,
                         devices=devices, selected_count=parent.selected_count, similar=parent.similar)


def aggregate_batch(parent: Decision, outcomes: Sequence[Decision], items: Sequence[int]) -> Decision:
    """묶음 요청의 하위 요청들의 결과를 상위 판정 하나로 묶는다 (FR-09, #284).

    `aggregate_selector` 와 같고 devices 의 기기마다 `item` 을 담는다. items[i] 는 outcomes[i] 의 1부터 센 항목 순서다.
    outcomes 는 항목 순서의 하위 결과다. 모두 OK 면 OK 고, 아니면 항목 순서에서 처음 OK 가 아닌 결과의 판정과 사유다.
    outcomes 와 items 의 길이가 다르면 ValueError 다.
    """
    devices = tuple(
        DeviceOutcome(o.node_id, o.verdict, o.reason, o.use_id if o.verdict is Verdict.OK else None, item=item)
        for o, item in zip(outcomes, items, strict=True))
    failed = next((o for o in outcomes if o.verdict is not Verdict.OK), None)
    verdict, reason = (Verdict.OK, ReasonCode.OK) if failed is None else (failed.verdict, failed.reason)
    return make_decision(parent.request_id, parent.internal_request_id, verdict, reason,
                         devices=devices, selected_count=parent.selected_count, similar=parent.similar)


class RequestUseService:
    def __init__(
        self, pipeline: DecisionPipeline, execution: ExecutionService, store: ExecutionStore
    ) -> None:
        self._pipeline = pipeline
        self._execution = execution
        self._store = store

    def request_use(self, req: UseRequest) -> Decision:
        if req.target.is_batch:
            return self._request_batch(req)
        if req.target.is_selector:
            return self._request_selector(req)
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

    # ── 거리 선택자 (FR-09, #253) ───────────────────────────────────

    def _request_selector(self, req: UseRequest) -> Decision:
        """새로 OK 가 난 하위 요청들을 동시에 실행하고 기기별 결과를 상위 판정 하나로 묶는다.

        재전송에는 하위 요청마다 저장된 실행 상태를 묶어 돌려준다. 새 명령을 만들지 않는다 (FR-22).
        한 하위 요청의 재검사 거부나 실행 실패로 다른 하위 요청의 실행을 취소하거나 되돌리지 않는다.
        """
        sd = self._pipeline.decide_selector(req)
        if sd.decision.verdict is not Verdict.OK or not sd.children:
            return sd.decision
        try:
            if sd.contexts:  # 이번에 새로 OK 가 난 요청
                outcomes = self._execute_children(sd.children, sd.contexts)
            else:  # 재전송: 이미 인계한 하위 요청들의 현재 실행 상태
                outcomes = [self._replay(child) for child in sd.children]
            return aggregate_selector(sd.decision, outcomes)
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            log.exception("거리 선택자 request_use 조립 중 예외")
            d = sd.decision
            return make_decision(d.request_id, d.internal_request_id, Verdict.DENY, ReasonCode.INTERNAL_ERROR,
                                 devices=(), selected_count=d.selected_count, similar=d.similar)

    # ── 묶음 요청 (FR-09, #284) ─────────────────────────────────────

    def _request_batch(self, req: UseRequest) -> Decision:
        """`_request_selector` 와 같다. 항목마다 고른 기기의 하위 요청을 동시에 실행하고 기기별 결과를 항목 순서와 묶는다.

        재전송에는 하위 요청마다 저장된 실행 상태를 묶어 돌려준다. 새 명령을 만들지 않는다 (FR-22).
        한 하위 요청의 재검사 거부나 실행 실패로 다른 하위 요청의 실행을 취소하거나 되돌리지 않는다.
        """
        sd = self._pipeline.decide_batch(req)
        if sd.decision.verdict is not Verdict.OK or not sd.children:
            return sd.decision
        try:
            if sd.contexts:  # 이번에 새로 OK 가 난 요청
                outcomes = self._execute_children(sd.children, sd.contexts)
            else:  # 재전송: 이미 인계한 하위 요청들의 현재 실행 상태
                outcomes = [self._replay(child) for child in sd.children]
            # OK 면 상위 판정의 devices 가 하위 요청마다 하나이고 항목 순서를 담는다
            items = [device.item for device in sd.decision.devices if device.item is not None]
            return aggregate_batch(sd.decision, outcomes, items)
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            log.exception("묶음 request_use 조립 중 예외")
            d = sd.decision
            return make_decision(d.request_id, d.internal_request_id, Verdict.DENY, ReasonCode.INTERNAL_ERROR,
                                 devices=(), selected_count=d.selected_count, similar=d.similar)

    def _execute_children(
        self, children: Sequence[Decision], contexts: Sequence[ExecutionContext]
    ) -> list[Decision]:
        """하위 요청마다 `execute` 를 동시에 실행하고 거리 순서대로 결과를 모은다.

        한 기기의 실행은 결과를 기다리며 막힌다. 차례로 실행하면 기기 수만큼 응답이 늦어진다 (NFR-01).
        """
        with ThreadPoolExecutor(max_workers=len(contexts)) as pool:
            futures = [pool.submit(self._execution.execute, ctx) for ctx in contexts]
            return [self._child_outcome(child, future)
                    for child, future in zip(children, futures, strict=True)]

    def _child_outcome(self, child: Decision, future: Future[ExecutionResult]) -> Decision:
        """하위 요청 하나의 실행 결과. 단일 요청과 같은 변환이다. `execute` 가 예외를 던지면 그 기기만 거부한다."""
        try:
            result = future.result()
        except Exception:  # noqa: BLE001 - 다른 하위 요청의 결과는 그대로 둔다
            log.exception("거리 선택자 하위 요청 실행 중 예외")
            return self._failure(child, ReasonCode.INTERNAL_ERROR)
        return self._from_result(child, result)

    # ── 실행 결과 → 판정 ────────────────────────────────────────────

    def _replay(self, decision: Decision) -> Decision:
        record = self._store.execution(decision.internal_request_id)
        if record is None:  # 인계가 끝나지 않았다. context 를 다시 만들 수 없다
            return self._failure(decision, ReasonCode.REQUEST_IN_PROGRESS, Verdict.BUSY)
        if record.status is ExecutionStatus.NOT_SENT:
            return self._from_evidence(decision, record.evidence)
        return self._map(decision, record.status, record.use_id, None, None)

    def _from_evidence(self, decision: Decision, evidence: Mapping[str, Any]) -> Decision:
        """미전송 취소 근거로 첫 응답과 같은 판정을 다시 만든다. 근거로 알 수 없으면 거부한다."""
        reason = _member(ReasonCode, evidence.get("reason"))
        if evidence.get("source") == "recheck":  # 재검사 판정과 사유를 그대로 전한다
            verdict = _member(Verdict, evidence.get("verdict"))
            if verdict is None or verdict is Verdict.OK or reason is None:  # 알 수 없는 근거는 OK 가 되지 않는다
                return self._failure(decision, ReasonCode.INTERNAL_ERROR)
            return make_decision(decision.request_id, decision.internal_request_id, verdict, reason,
                                 node_id=decision.node_id, p_max=decision.p_max)
        by_reason = _BY_REASON.get(reason) if reason is not None else None
        if by_reason is None:
            return self._failure(decision, ReasonCode.INTERNAL_ERROR)
        return self._failure(decision, by_reason[1], by_reason[0])

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
