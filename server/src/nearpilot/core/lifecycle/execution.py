"""실행 인계와 사건별 결과 저장 (FR-06, FR-07, FR-18, FR-19).

공동 계약이 없는 취소 경로는 예약을 보존한다. 서버 조립은 별도 작업이다.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from nearpilot.shared.enums import (
    CommandOutcome, DeviceState, ExecutionKind, ExecutionStatus, NodeResultState,
    ReasonCode, RejectCode, StoreOutcome, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreOutcomeUnknown
from nearpilot.shared.interfaces import (
    DecisionRechecker, ExecutionStore, NodeCommandGateway, Repository,
)
from nearpilot.shared.models import (
    CommandRecord, CommandResult, Decision, ExecutionContext, ExecutionResult,
    NodeCommand, NodeResult, RecoveryEvidence,
)


@dataclass
class _Result:
    outcome: CommandOutcome
    occurred_at: datetime
    rental_due_at: datetime | None
    evidence: dict[str, Any]
    done: bool = False


@dataclass
class _Attempt:
    context: ExecutionContext
    command: CommandRecord | None = None
    claimed: bool = False
    target_session_id: str | None = None
    recheck: Decision | None = None
    cancel: dict[str, Any] | None = None
    events: dict[str, _Result] = field(default_factory=dict)
    recovery: CommandRecord | None = None
    saw_progress: bool = False
    blockers: tuple[str, ...] = ()


class ExecutionService:
    """공통 기기 잠금으로 준비·저장을 직렬화한다. 통신 대기 중에는 잠금을 푼다.

    can_execute는 RestoreService.can_execute, record_isolated는 복구 건 생성에 연결한다.
    tick은 저장만 재시도한다. 전송 시작 불명이나 중복 인계에서 재전송하지 않는다.
    """

    def __init__(
        self, *, repository: Repository, store: ExecutionStore,
        rechecker: DecisionRechecker, gateway: NodeCommandGateway,
        clock: Callable[[], datetime],
        lock_for: Callable[[str], AbstractContextManager[Any]],
        can_execute: Callable[[str], bool], record_isolated: Callable[..., Any],
    ) -> None:
        self._repository, self._store = repository, store
        self._rechecker, self._gateway = rechecker, gateway
        self._clock, self._lock_for = clock, lock_for
        self._can_execute, self._record_isolated = can_execute, record_isolated
        self._attempts: dict[str, _Attempt] = {}
        self._index_lock = Lock()

    def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        attempt = self._attempt(ctx)
        with self._lock_for(ctx.node_id):
            try:
                self._retry(attempt)
                command = self._prepare(attempt)
            except StoreError:
                attempt.blockers = ("store_unconfirmed",)
                return self._view(attempt)
            if command is None:
                return self._view(attempt)

        # IOT가 결과를 다른 스레드에서 인계해도 기기 잠금을 기다리지 않게 한다.
        source = "gateway"
        try:
            result = self._gateway.send(command, ctx.settings.exec_timeout_sec)
        except Exception:
            source = "gateway_error"
            result = CommandResult(command.command_id, CommandOutcome.UNKNOWN,
                                   reported_at=self._clock())
        with self._lock_for(ctx.node_id):
            try:
                self._receive(attempt, result, source=source)
                self._retry(attempt)
            except StoreError:
                attempt.blockers = ("store_unconfirmed",)
            return self._view(attempt)

    def on_result(self, ctx: ExecutionContext, report: NodeResult) -> None:
        """검증을 마친 기기 보고를 받는다. 저장 실패는 호출자에게 다시 알린다."""
        attempt = self._attempt(ctx)
        with self._lock_for(ctx.node_id):
            current = self._store.execution(ctx.internal_request_id)
            self._bind(attempt, current)
            if current.status == ExecutionStatus.PENDING:
                raise StoreError("전송 시작 전의 명령 결과다")
            self._receive(attempt, CommandResult(report.command_id, CommandOutcome.UNKNOWN,
                                                 report=report), source="device")
            self._retry(attempt)

    def tick(self) -> None:
        """미완료 저장을 재시도한다. 한 기기의 실패 뒤에도 다른 기기를 시도한다."""
        with self._index_lock:
            attempts = tuple(self._attempts.values())
        first_error: StoreError | None = None
        for attempt in attempts:
            with self._lock_for(attempt.context.node_id):
                try:
                    self._retry(attempt)
                except StoreError as error:
                    attempt.blockers = ("store_unconfirmed",)
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error

    def blockers(self, internal_request_id: str) -> tuple[str, ...]:
        with self._index_lock:
            attempt = self._attempts.get(internal_request_id)
        if attempt is None:
            return ()
        with self._lock_for(attempt.context.node_id):
            return attempt.blockers

    def _attempt(self, ctx: ExecutionContext) -> _Attempt:
        with self._index_lock:
            attempt = self._attempts.setdefault(ctx.internal_request_id, _Attempt(ctx))
            if attempt.context != ctx:
                raise ValueError("같은 내부 요청의 실행 문맥이 다르다")
            return attempt

    def _prepare(self, attempt: _Attempt) -> NodeCommand | None:
        ctx = attempt.context
        current = self._store.execution(ctx.internal_request_id)
        if current is None:
            try:
                linked = self._store.link_command(ctx.internal_request_id, ctx.node_id, ctx.request.action)
            except StoreOutcomeUnknown:
                current = self._store.execution(ctx.internal_request_id)
                if current is None:
                    raise
            else:
                if linked.outcome == StoreOutcome.CONFLICT:
                    attempt.blockers = ("link_conflict",)
                    return None
                current = linked.command or self._store.execution(ctx.internal_request_id)
        self._bind(attempt, current)
        if current.status != ExecutionStatus.PENDING or attempt.claimed:
            return None
        if not self._can_execute(ctx.node_id):
            attempt.blockers = ("restore_unconfirmed",)
            return None
        if not self._within_wait(attempt):
            return None
        try:
            decision = self._rechecker.recheck(ctx)
        except Exception:
            decision = Decision(ctx.internal_request_id, ctx.request.request_id,
                                Verdict.DENY, ReasonCode.RECHECK_FAILED, node_id=ctx.node_id)
        if (decision.internal_request_id != ctx.internal_request_id
            or decision.request_id != ctx.request.request_id
            or decision.node_id not in (None, ctx.node_id)):
            raise StoreError("재검사 판정의 요청·대상 연결이 다르다")
        if decision.verdict != Verdict.OK:
            attempt.recheck = decision
            attempt.cancel = {"source": "recheck", "verdict": decision.verdict.value,
                              "reason": decision.reason.value, "confirmed_at": self._clock()}
            self._retry(attempt)
            return None
        # 재검사 중의 시간 경과와 연결 변경을 전송 직전에 다시 확인한다.
        if not self._within_wait(attempt):
            return None
        if not self._can_execute(ctx.node_id):
            attempt.blockers = ("restore_unconfirmed",)
            return None
        nodes = [node for node in self._repository.nodes() if node.node_id == ctx.node_id]
        if len(nodes) != 1 or not nodes[0].session_id:
            attempt.blockers = ("connection_unconfirmed",)
            return None
        try:
            started = self._store.begin_send(current.command_id)
        except StoreOutcomeUnknown:
            attempt.claimed = True
            confirmed = self._store.execution(ctx.internal_request_id)
            if confirmed is not None:
                self._bind(attempt, confirmed)
            raise
        if started.outcome != StoreOutcome.APPLIED:
            if started.command is not None:
                self._bind(attempt, started.command)
            attempt.claimed = started.outcome == StoreOutcome.ALREADY_APPLIED
            attempt.blockers = ("begin_send_" + started.outcome.value,)
            return None
        attempt.claimed = True
        self._bind(attempt, started.command or self._store.execution(ctx.internal_request_id))
        if attempt.command.status != ExecutionStatus.IN_PROGRESS:
            raise StoreError("전송 시작 상태를 확인하지 못했다")
        attempt.target_session_id = nodes[0].session_id
        attempt.blockers = ()
        now = self._clock()
        return NodeCommand(current.command_id, ctx.node_id, ctx.request.action,
                           attempt.target_session_id, now,
                           now + timedelta(seconds=ctx.settings.exec_timeout_sec))

    def _within_wait(self, attempt: _Attempt) -> bool:
        reserved_at = attempt.command.reserved_at
        now = self._clock()
        if reserved_at is None or reserved_at > now:
            attempt.blockers = ("reservation_time_unconfirmed",)
            return False
        if now >= reserved_at + timedelta(seconds=attempt.context.settings.reservation_wait_sec):
            attempt.blockers = ("reservation_expired_contract_pending",)
            return False
        return True

    def _receive(self, attempt: _Attempt, result: CommandResult, *, source: str) -> None:
        if result.command_id != attempt.command.command_id:
            raise StoreError("실행 결과의 명령 연결이 다르다")
        report = result.report
        if report is not None:
            if (report.command_id != result.command_id or report.node_id != attempt.context.node_id
                or not report.event_id or not report.session_id
                or (attempt.target_session_id is not None
                    and report.session_id != attempt.target_session_id)):
                raise StoreError("기기 결과의 대상 연결이 다르다")
            if report.state in (NodeResultState.ACCEPTED, NodeResultState.RUNNING):
                attempt.saw_progress = True
                return
            outcome = CommandOutcome(report.state.value)
            if outcome == CommandOutcome.REJECTED and (
                report.error_code != RejectCode.DEVICE_BUSY or attempt.saw_progress
            ):
                outcome = CommandOutcome.UNKNOWN
            key = report.event_id
            occurred_at = report.occurred_at
            evidence = {"source": "device_result", "event_id": key,
                        "session_id": report.session_id, "state": report.state.value,
                        "error_code": report.error_code, "detail": dict(report.detail)}
        else:
            outcome = result.outcome
            if outcome == CommandOutcome.NOT_SENT:
                attempt.blockers = ("not_sent_reason_contract_pending",)
                return
            # 기기 완료 근거가 없는 성공·실패·거부는 결과 불명이다.
            outcome = CommandOutcome.UNKNOWN
            key = "server:" + attempt.command.command_id
            occurred_at = result.reported_at or self._clock()
            evidence = {"source": source, "outcome": result.outcome.value}
        previous = attempt.events.get(key)
        if previous is not None:
            if (previous.outcome != outcome or previous.occurred_at != occurred_at
                or previous.evidence != evidence):
                raise StoreError("같은 결과 사건의 내용이 다르다")
            return
        current = attempt.command
        if (current.outcome == outcome and current.occurred_at == occurred_at
            and current.evidence == evidence):
            # 재시작 뒤 같은 보고를 다시 받아도 대여시간 기준을 새로 계산하지 않는다.
            return
        due_at = None
        if (outcome == CommandOutcome.SUCCEEDED
            and attempt.context.execution_kind == ExecutionKind.RENTAL_INITIAL):
            due_at = self._clock() + timedelta(seconds=attempt.context.settings.rental_duration_sec)
        attempt.events[key] = _Result(outcome, occurred_at, due_at, evidence)

    def _retry(self, attempt: _Attempt) -> None:
        ctx = attempt.context
        if attempt.cancel is not None:
            conflict = False
            try:
                saved = self._store.cancel_not_sent(ctx.internal_request_id, ExecutionStatus.PENDING,
                                                    attempt.recheck.reason, attempt.cancel)
                conflict = saved.outcome == StoreOutcome.CONFLICT
                current = saved.command or self._store.execution(ctx.internal_request_id)
            except StoreOutcomeUnknown:
                current = self._store.execution(ctx.internal_request_id)
                if (current is None or current.status != ExecutionStatus.NOT_SENT
                    or current.evidence != attempt.cancel):
                    raise
            if not conflict and (
                current is None or current.status != ExecutionStatus.NOT_SENT
                or current.evidence != attempt.cancel
            ):
                raise StoreError("미전송 취소 저장을 확인하지 못했다")
            self._bind(attempt, current)
            attempt.cancel = None
            attempt.blockers = ()
        for event in attempt.events.values():
            if event.done:
                continue
            conflict = False
            try:
                saved = self._store.record_result(attempt.command.command_id, event.outcome,
                                                  event.occurred_at, event.rental_due_at, event.evidence)
                current = saved.command or self._store.execution(ctx.internal_request_id)
                conflict = saved.outcome == StoreOutcome.CONFLICT
            except StoreOutcomeUnknown:
                current = self._store.execution(ctx.internal_request_id)
                if (current is None or current.outcome != event.outcome
                    or current.occurred_at != event.occurred_at or current.evidence != event.evidence):
                    raise
            if (not conflict and (
                current is None or current.status != ExecutionStatus(event.outcome.value)
                or current.outcome != event.outcome or current.occurred_at != event.occurred_at
                or current.evidence != event.evidence
            )):
                raise StoreError("실행 결과 저장을 확인하지 못했다")
            self._bind(attempt, current)
            event.done = True
            attempt.blockers = ()
            if not conflict and current.status in (ExecutionStatus.FAILED, ExecutionStatus.UNKNOWN):
                attempt.recovery = current
        if attempt.recovery is not None:
            current = attempt.recovery
            nodes = [node for node in self._repository.nodes() if node.node_id == ctx.node_id]
            if len(nodes) != 1:
                raise StoreError("격리 기기의 현재 상태를 확인하지 못했다")
            if nodes[0].device_state == DeviceState.FAULT:
                evidence = ()
                if current.evidence.get("event_id") and current.evidence.get("session_id"):
                    evidence = (RecoveryEvidence(current.evidence["event_id"],
                                current.evidence["session_id"], current.occurred_at,
                                execution_outcome=current.outcome),)
                self._record_isolated(ctx.node_id, internal_request_ids=(ctx.internal_request_id,),
                                      command_ids=(current.command_id,), use_id=ctx.use_id,
                                      evidence=evidence)
            attempt.recovery = None
            attempt.blockers = ()

    @staticmethod
    def _bind(attempt: _Attempt, current: CommandRecord | None) -> None:
        ctx = attempt.context
        if current is None or (
            current.internal_request_id != ctx.internal_request_id or current.node_id != ctx.node_id
            or current.action != ctx.request.action or current.use_id != ctx.use_id
            or current.usage_id != ctx.usage_id or current.execution_kind != ctx.execution_kind
            or (attempt.command is not None and current.command_id != attempt.command.command_id)
        ):
            raise StoreError("저장한 명령의 요청·대상 연결이 다르다")
        attempt.command = current

    @staticmethod
    def _view(attempt: _Attempt) -> ExecutionResult:
        current = attempt.command
        status = ExecutionStatus.PENDING if current is None else current.status
        return ExecutionResult(attempt.context.internal_request_id, status,
                               None if current is None else current.command_id,
                               attempt.context.use_id if status == ExecutionStatus.SUCCEEDED else None,
                               attempt.recheck)
