"""실행 인계와 사건별 결과 저장 (FR-06, FR-07, FR-18, FR-19, FR-20).

확실한 미전송은 원인별 사유로 취소하고, 재검사 밖의 중단은 ExecutionResult.reason 으로 알린다.
서버 내부 요청(#86)도 같은 경로로 실행한다. 서버 조립은 별도 작업이다.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from nearpilot.shared.enums import (
    Action, CommandOutcome, DeviceState, ExecutionKind, ExecutionStatus, NodeResultState,
    ReasonCode, RejectCode, StoreConflictReason, StoreOutcome, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreOutcomeUnknown
from nearpilot.shared.interfaces import (
    DecisionRechecker, ExecutionStore, NodeCommandGateway, Repository,
)
from nearpilot.shared.models import (
    CommandRecord, CommandResult, Decision, ExecutionContext, ExecutionResult,
    NodeCommand, NodeResult, RecoveryEvidence,
)

# begin_send 충돌 사유 → 판정에 알리는 중단 사유 (architecture.md §4 "실행 인계와 재검사")
_BEGIN_REASONS = {
    StoreConflictReason.COMMAND_IN_PROGRESS: ReasonCode.COMMAND_IN_PROGRESS,
    StoreConflictReason.STATE_CONFLICT: ReasonCode.DEVICE_BUSY,
}


@dataclass
class _Result:
    outcome: CommandOutcome
    occurred_at: datetime
    rental_due_at: datetime | None
    evidence: dict[str, Any]
    done: bool = False


@dataclass(frozen=True)
class _Cancel:
    expected: ExecutionStatus
    reason: ReasonCode
    evidence: dict[str, Any]


@dataclass
class _Attempt:
    context: ExecutionContext
    command: CommandRecord | None = None
    claimed: bool = False
    target_session_id: str | None = None
    recheck: Decision | None = None
    reason: ReasonCode | None = None
    cancel: _Cancel | None = None
    events: dict[str, _Result] = field(default_factory=dict)
    recovery: CommandRecord | None = None
    saw_progress: bool = False
    blockers: tuple[str, ...] = ()
    internal_linked: bool = False
    refused: bool = False


def _action(ctx: ExecutionContext) -> Action:
    return ctx.internal_request.action if ctx.request is None else ctx.request.action


def _internal(ctx: ExecutionContext) -> bool:
    return ctx.execution_kind == ExecutionKind.SERVER_INTERNAL


def _closed_rental_refusal(command: CommandRecord) -> bool:
    """`link_command`가 반납으로 닫힌 대여의 재조작을 거부하며 남긴 `NOT_SENT` 명령인가.

    붙일 점유 세션이 없어 `use_id`가 비어 있다.
    """
    return (command.status == ExecutionStatus.NOT_SENT
            and command.execution_kind == ExecutionKind.RENTAL_REOPERATION
            and command.evidence.get("source") == "link_command"
            and command.evidence.get("reason") == ReasonCode.USE_NOT_FOUND.value)


class ExecutionService:
    """공통 기기 잠금으로 준비·저장을 직렬화한다. 통신 대기 중에는 잠금을 푼다.

    can_execute는 ExecutionReadiness.can_execute(RestoreService), record_isolated는 복구 건 생성,
    confirm_internal은 서버 내부 요청의 전송 전 확인(SharedLightService.can_request_off)에 연결한다.
    tick은 저장 재시도와 예약 대기 만료 취소만 한다. 전송 시작 불명이나 중복 인계에서 재전송하지 않는다.
    """

    def __init__(
        self, *, repository: Repository, store: ExecutionStore,
        rechecker: DecisionRechecker, gateway: NodeCommandGateway,
        clock: Callable[[], datetime],
        lock_for: Callable[[str], AbstractContextManager[Any]],
        can_execute: Callable[[str], bool], record_isolated: Callable[..., Any],
        confirm_internal: Callable[[ExecutionContext], bool] | None = None,
    ) -> None:
        self._repository, self._store = repository, store
        self._rechecker, self._gateway = rechecker, gateway
        self._clock, self._lock_for = clock, lock_for
        self._can_execute, self._record_isolated = can_execute, record_isolated
        self._confirm_internal = confirm_internal
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
        """미완료 저장과 예약 대기 만료 취소를 재시도한다. 한 기기의 실패 뒤에도 다른 기기를 시도한다."""
        with self._index_lock:
            attempts = tuple(self._attempts.values())
        first_error: StoreError | None = None
        for attempt in attempts:
            with self._lock_for(attempt.context.node_id):
                try:
                    self._retry(attempt)
                    self._expire(attempt)
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
        if attempt.refused:
            return None
        current = self._store.execution(ctx.internal_request_id)
        if current is None:
            if _internal(ctx) and not self._link_internal(attempt):
                return None
            try:
                linked = self._store.link_command(ctx.internal_request_id, ctx.node_id, _action(ctx))
            except StoreOutcomeUnknown:
                current = self._store.execution(ctx.internal_request_id)
                if current is None:
                    raise
            else:
                if linked.outcome == StoreOutcome.CONFLICT:
                    if (linked.conflict_reason == StoreConflictReason.STATE_CONFLICT
                        and ctx.execution_kind == ExecutionKind.RENTAL_REOPERATION):
                        # 반납이 재조작할 대여를 먼저 닫았다. DB가 사용분을 반환하고 미전송 명령을 남겼으니
                        # 다시 연결하지 않는다. 연결 전 취소의 충돌에는 명령이 없다.
                        if linked.command is not None:
                            self._bind(attempt, linked.command)
                        attempt.refused, attempt.reason = True, ReasonCode.USE_NOT_FOUND
                        return None
                    attempt.blockers = ("link_conflict",)
                    return None
                current = linked.command or self._store.execution(ctx.internal_request_id)
        self._bind(attempt, current)
        if _closed_rental_refusal(current):
            # 앞선 호출이 연결을 거부하고 남긴 명령이다. 재인계(재시작 뒤 포함)도 전송하지 않고 같은 결과를 낸다.
            attempt.refused, attempt.reason = True, ReasonCode.USE_NOT_FOUND
            return None
        if current.status != ExecutionStatus.PENDING or attempt.claimed or attempt.cancel is not None:
            return None
        if not self._ready(attempt) or not self._within_wait(attempt):
            return None
        if not _internal(ctx):
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
                attempt.reason = None
                attempt.cancel = _Cancel(ExecutionStatus.PENDING, decision.reason, {
                    "source": "recheck", "verdict": decision.verdict.value,
                    "reason": decision.reason.value, "confirmed_at": self._clock()})
                self._retry(attempt)
                return None
            # 재검사 중의 시간 경과와 연결 변경을 전송 직전에 다시 확인한다.
            if not self._within_wait(attempt) or not self._ready(attempt):
                return None
        session_id = self._session_id(ctx.node_id)
        if session_id is None:
            attempt.blockers = ("connection_unconfirmed",)
            self._cancel(attempt, ExecutionStatus.PENDING, ReasonCode.NODE_NOT_CONNECTED, "no_connection")
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
            reason = _BEGIN_REASONS.get(started.conflict_reason)
            if started.outcome == StoreOutcome.CONFLICT and reason is not None:
                attempt.reason = reason
                if _internal(ctx) and attempt.command.status == ExecutionStatus.PENDING:
                    self._cancel(attempt, ExecutionStatus.PENDING, reason, "begin_send_conflict")
            return None
        attempt.claimed = True
        attempt.reason = None
        self._bind(attempt, started.command or self._store.execution(ctx.internal_request_id))
        if attempt.command.status != ExecutionStatus.IN_PROGRESS:
            raise StoreError("전송 시작 상태를 확인하지 못했다")
        attempt.target_session_id = session_id
        attempt.blockers = ()
        now = self._clock()
        return NodeCommand(current.command_id, ctx.node_id, _action(ctx),
                           attempt.target_session_id, now,
                           now + timedelta(seconds=ctx.settings.exec_timeout_sec))

    def _link_internal(self, attempt: _Attempt) -> bool:
        """서버 내부 요청은 저장 전에 확인한다. 확인과 전송 시작은 같은 기기 잠금 안에서 잇는다."""
        ctx = attempt.context
        if not attempt.internal_linked:
            refused: ReasonCode | None = None
            blocker = ""
            if self._confirm_internal is None or not self._confirm_internal(ctx):
                blocker = "internal_check"
            elif not self._can_execute(ctx.node_id):
                blocker, refused = "restore_unconfirmed", ReasonCode.TARGET_UNAVAILABLE
            elif self._session_id(ctx.node_id) is None:
                blocker, refused = "connection_unconfirmed", ReasonCode.NODE_NOT_CONNECTED
            if blocker:
                attempt.refused, attempt.reason, attempt.blockers = True, refused, (blocker,)
                return False
        saved = self._store.link_internal_request(ctx.internal_request)
        if saved.outcome == StoreOutcome.CONFLICT:
            attempt.blockers = ("link_conflict",)
            return False
        attempt.internal_linked = True
        return True

    def _ready(self, attempt: _Attempt) -> bool:
        if self._can_execute(attempt.context.node_id):
            return True
        attempt.blockers = ("restore_unconfirmed",)
        attempt.reason = ReasonCode.TARGET_UNAVAILABLE
        if _internal(attempt.context):
            self._cancel(attempt, ExecutionStatus.PENDING, ReasonCode.TARGET_UNAVAILABLE, "restore_unconfirmed")
        return False

    def _within_wait(self, attempt: _Attempt) -> bool:
        state = self._wait_state(attempt)
        if state == "expired":
            attempt.blockers = ("reservation_expired",)
            self._cancel(attempt, ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED,
                         "reservation_expired")
        elif state == "unconfirmed":
            attempt.blockers = ("reservation_time_unconfirmed",)
        return state == "ok"

    def _wait_state(self, attempt: _Attempt) -> str:
        ctx = attempt.context
        reserved_at = ctx.reserved_at
        if reserved_at is None:
            # DB 쓰기가 없는 공용형 요청과 서버 내부 요청에는 예약이 없다.
            no_reservation = _internal(ctx) or (
                ctx.execution_kind == ExecutionKind.SHARED and ctx.usage_id is None)
            return "ok" if no_reservation else "unconfirmed"
        now = self._clock()
        if reserved_at > now:
            return "unconfirmed"
        if now >= reserved_at + timedelta(seconds=ctx.settings.reservation_wait_sec):
            return "expired"
        return "ok"

    def _expire(self, attempt: _Attempt) -> None:
        current = attempt.command
        if (current is None or current.status != ExecutionStatus.PENDING or attempt.claimed
            or attempt.cancel is not None or self._wait_state(attempt) != "expired"):
            return
        attempt.blockers = ("reservation_expired",)
        self._cancel(attempt, ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED,
                     "reservation_expired")

    def _cancel(self, attempt: _Attempt, expected: ExecutionStatus, reason: ReasonCode,
                source: str) -> None:
        attempt.reason = reason
        attempt.cancel = _Cancel(expected, reason, {
            "source": source, "reason": reason.value, "confirmed_at": self._clock()})
        self._retry(attempt)

    def _session_id(self, node_id: str) -> str | None:
        nodes = [node for node in self._repository.nodes() if node.node_id == node_id]
        if len(nodes) != 1 or not nodes[0].session_id:
            return None
        return nodes[0].session_id

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
                # IOT의 미전송 증명만 전송 시작 뒤의 취소 근거다.
                if (attempt.cancel is None and not attempt.events
                    and attempt.command.status == ExecutionStatus.IN_PROGRESS):
                    attempt.reason = ReasonCode.COMMAND_NOT_SENT
                    attempt.cancel = _Cancel(ExecutionStatus.IN_PROGRESS, ReasonCode.COMMAND_NOT_SENT, {
                        "source": source, "reason": ReasonCode.COMMAND_NOT_SENT.value,
                        "confirmed_at": result.reported_at or self._clock()})
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
            cancel = attempt.cancel
            conflict = False
            try:
                saved = self._store.cancel_not_sent(ctx.internal_request_id, cancel.expected,
                                                    cancel.reason, cancel.evidence)
                conflict = saved.outcome == StoreOutcome.CONFLICT
                current = saved.command or self._store.execution(ctx.internal_request_id)
            except StoreOutcomeUnknown:
                current = self._store.execution(ctx.internal_request_id)
                if (current is None or current.status != ExecutionStatus.NOT_SENT
                    or current.evidence != cancel.evidence):
                    raise
            if not conflict and (
                current is None or current.status != ExecutionStatus.NOT_SENT
                or current.evidence != cancel.evidence
            ):
                raise StoreError("미전송 취소 저장을 확인하지 못했다")
            self._bind(attempt, current)
            attempt.cancel = None
            attempt.blockers = ()
            if conflict:
                # 저장된 상태 전이를 따른다. 취소하지 못한 중단 사유를 판정에 알리지 않는다.
                attempt.reason = None
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
            or current.action != _action(ctx)
            or (current.use_id != ctx.use_id and not _closed_rental_refusal(current))
            or current.usage_id != ctx.usage_id or current.execution_kind != ctx.execution_kind
            or (attempt.command is not None and current.command_id != attempt.command.command_id)
        ):
            raise StoreError("저장한 명령의 요청·대상 연결이 다르다")
        attempt.command = current

    @staticmethod
    def _view(attempt: _Attempt) -> ExecutionResult:
        current = attempt.command
        if current is None:
            status = ExecutionStatus.NOT_SENT if attempt.refused else ExecutionStatus.PENDING
        else:
            status = current.status
        reason = None
        if attempt.recheck is None and status != ExecutionStatus.SUCCEEDED:
            reason = attempt.reason
        use_id = None
        if status == ExecutionStatus.SUCCEEDED and not _internal(attempt.context):
            use_id = attempt.context.use_id
        return ExecutionResult(attempt.context.internal_request_id, status,
                               None if current is None else current.command_id,
                               use_id, attempt.recheck, reason)
