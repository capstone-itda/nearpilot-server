"""대여형 반납과 반납 검사 (FR-07, FR-19, FR-20, FR-21).

호출자는 tick을 반복 호출하고, 기기 이벤트·복구와 같은 기기 잠금을 주입한다.
복구 건 생성은 별도 상태 관리 모듈의 필수 접점이다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any
from uuid import uuid4

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    DeviceState, EndReason, ReasonCode, SafetyCheckOutcome, SessionState,
    StoreConflictReason, StoreOutcome, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreOutcomeUnknown
from nearpilot.shared.interfaces import ExecutionStore, Repository, SafetyCheckGateway
from nearpilot.shared.models import (
    CloseResult, Decision, NodeInfo, SafetyCheckRequest, SafetyCheckResult, UseSession,
)


@dataclass
class _Closing:
    session: UseSession
    settings: Settings
    request: SafetyCheckRequest | None = None
    retry_at: datetime | None = None
    unconfirmed_since: datetime | None = None
    finish: tuple[datetime, SafetyCheckResult] | None = None
    finish_unknown: bool = False
    fault_evidence: Mapping[str, Any] | None = None


class ReleaseService:
    """CORE 반납 서비스. 다른 계층에는 shared Protocol로만 의존한다.

    record_fault(session, evidence)는 복구 건의 저장 완료를 확인하거나 예외를
    던져야 한다. 격리 뒤 실패해도 tick과 재인계에서 같은 장애를 다시 전달한다.
    lock_for는 node_id별 동일한 재진입 잠금을 반환해야 한다.
    """

    def __init__(
        self, *, repository: Repository, store: ExecutionStore,
        gateway: SafetyCheckGateway, settings: Callable[[], Settings],
        clock: Callable[[], datetime],
        lock_for: Callable[[str], AbstractContextManager[Any]],
        record_fault: Callable[[UseSession, Mapping[str, Any]], None],
    ) -> None:
        self._repository = repository
        self._store = store
        self._gateway = gateway
        self._settings = settings
        self._clock = clock
        self._lock_for = lock_for
        self._record_fault = record_fault
        self._closing: dict[str, _Closing] = {}
        self._index_lock = Lock()

    def release_use(self, user_id: str, use_id: str) -> Decision:
        # 소유권 확인 전에는 기기 정보나 완료 기록을 읽지 않는다 (FR-21).
        if not self._repository.session_owner_matches(use_id, user_id):
            return self._missing()
        session = self._repository.session(use_id)
        if session is None:
            return self._missing()
        with self._lock_for(session.node_id):
            if not self._repository.session_owner_matches(use_id, user_id):
                return self._missing()
            session = self._repository.session(use_id)
            if session is None:
                return self._missing()
            if session.state == SessionState.CLOSED:
                completed = self._store.close_result(use_id)
                if completed is not None and completed.use_id == use_id:
                    return self._decision(session, Verdict.OK, ReasonCode.OK, "반납을 완료했다.")
                return self._decision(session, Verdict.BUSY, ReasonCode.OK, "종료 기록을 확인 중이다.")
            node = self._node(session.node_id)
            if node is None or node.device_state == DeviceState.FAULT:
                closing = self._get(session.node_id)
                if closing is not None and closing.fault_evidence is not None:
                    self._isolate(closing, closing.fault_evidence)
                return self._unavailable(session)
            if session.state == SessionState.ACTIVE:
                try:
                    result = self._store.begin_close(use_id, EndReason.RELEASE)
                except StoreOutcomeUnknown:
                    # 저장이 불명이어도 확인한 CLOSING에서만 검사를 시작한다.
                    session = self._repository.session(use_id)
                    if session is None or session.state != SessionState.CLOSING:
                        return self._decision(session, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
                except StoreError:
                    return self._decision(session, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
                else:
                    if result.outcome == StoreOutcome.CONFLICT:
                        if result.conflict_reason == StoreConflictReason.COMMAND_IN_PROGRESS:
                            return self._decision(session, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS)
                        return self._unavailable(session)
                    session = self._repository.session(use_id)
            if session is None or session.state != SessionState.CLOSING:
                return self._unavailable(session)
            closing = self._get(session.node_id)
            if closing is None or closing.session.use_id != use_id:
                closing = _Closing(session, self._settings())
                self._put(closing)
                self._request(closing)
            elif closing.finish is not None:
                self._finish(closing)
            node = self._node(session.node_id)
            if node is None or node.device_state == DeviceState.FAULT:
                return self._unavailable(session)
            if self._get(session.node_id) is None:
                return self._decision(session, Verdict.OK, ReasonCode.OK, "반납을 완료했다.")
            if closing.fault_evidence is not None:
                return self._unavailable(session)
            return self._decision(session, Verdict.BUSY, ReasonCode.OK, "반납 검사 중이다.")

    def get_use_status(self, user_id: str, use_id: str) -> Mapping[str, Any]:
        if not self._repository.session_owner_matches(use_id, user_id):
            return {"reason_code": ReasonCode.USE_NOT_FOUND.value}
        session = self._repository.session(use_id)
        if session is None:
            return {"reason_code": ReasonCode.USE_NOT_FOUND.value}
        with self._lock_for(session.node_id):
            if not self._repository.session_owner_matches(use_id, user_id):
                return {"reason_code": ReasonCode.USE_NOT_FOUND.value}
            session = self._repository.session(use_id)
            if session is None:
                return {"reason_code": ReasonCode.USE_NOT_FOUND.value}
            now = self._clock()
            if session.state == SessionState.CLOSED:
                if session.closed_at is None or now > session.closed_at + timedelta(
                    days=self._settings().use_history_retention_days
                ):
                    return {"reason_code": ReasonCode.USE_NOT_FOUND.value}
            end = session.closed_at if session.state == SessionState.CLOSED else now
            overtime = 0.0 if session.rental_due_at is None or end is None else max(
                0.0, (end - session.rental_due_at).total_seconds()
            )
            return {
                "use_id": use_id, "state": session.state.value,
                "rental_due_at": session.rental_due_at, "overtime_sec": overtime,
                "closed_at": session.closed_at,
            }

    def resume_close(self, use_id: str) -> None:
        """복원 담당이 현재 기기를 확인한 뒤 호출한다. 이전 검사는 재사용하지 않는다."""
        session = self._repository.session(use_id)
        if session is None:
            return
        with self._lock_for(session.node_id):
            session = self._repository.session(use_id)
            if session is None or session.state != SessionState.CLOSING:
                return
            closing = self._get(session.node_id)
            # 반복한 복원 호출은 진행 중 검사와 재검사 대기 시간을 바꾸지 않는다.
            if closing is not None:
                return
            closing = _Closing(session, self._settings())
            self._put(closing)
            self._request(closing)

    def on_safety_check_result(self, report: SafetyCheckResult) -> None:
        with self._lock_for(report.node_id):
            closing = self._get(report.node_id)
            if closing is None:
                return
            if closing.fault_evidence is not None:
                self._isolate(closing, closing.fault_evidence)
                return
            if closing.finish is not None:
                # 이미 채택한 결과만 재시도한다. 다른 결과로 완료 내용을 바꾸지 않는다.
                self._finish(closing)
                return
            request = closing.request
            node = self._node(report.node_id)
            if node is None or node.device_state == DeviceState.FAULT:
                closing.request = None
                return
            if request is None or report.check_id != request.check_id:
                # 과거 결과는 현재 검사를 교체하거나 타이머를 연장하지 않는다.
                return
            age = (self._clock() - report.checked_at).total_seconds()
            valid = (
                node.session_id == request.target_session_id == report.session_id
                and request.criteria == report.criteria
                and request.issued_at <= report.received_at <= request.expires_at
                and request.issued_at <= report.checked_at <= report.received_at
                and 0 <= age <= closing.settings.safety_report_max_age_sec
            )
            if not valid or report.outcome == SafetyCheckOutcome.UNKNOWN:
                self._unconfirmed(closing, self._clock())
                return
            if report.outcome == SafetyCheckOutcome.FAULT:
                self._isolate(closing, {
                    "source": "release", "check_id": report.check_id,
                    "event_id": report.event_id, "outcome": report.outcome.value,
                    "session_id": report.session_id, "checked_at": report.checked_at,
                    "evidence": report.evidence,
                })
                return
            closing.unconfirmed_since = None
            if report.outcome == SafetyCheckOutcome.NOT_READY:
                closing.request = None
                closing.retry_at = self._clock() + timedelta(seconds=closing.settings.safety_check_retry_sec)
                return
            if report.outcome == SafetyCheckOutcome.PASSED:
                closing.finish = (self._clock(), report)
                self._finish(closing)

    def tick(self) -> None:
        """기기별 잠금 안에서 저장 재시도, 응답 기한, 재검사와 격리 제한을 확인한다."""
        with self._index_lock:
            node_ids = tuple(self._closing)
        for node_id in node_ids:
            with self._lock_for(node_id):
                closing = self._get(node_id)
                if closing is None:
                    continue
                if closing.fault_evidence is not None:
                    self._isolate(closing, closing.fault_evidence)
                    continue
                if closing.finish is not None:
                    self._finish(closing)
                    continue
                session = self._repository.session(closing.session.use_id)
                if session is None or session.state != SessionState.CLOSING:
                    self._remove(node_id)
                    continue
                node = self._node(node_id)
                if node is None or node.device_state == DeviceState.FAULT:
                    closing.request = None
                    continue
                now = self._clock()
                request = closing.request
                if request is not None:
                    if node.session_id != request.target_session_id:
                        self._unconfirmed(closing, now)
                    elif now >= request.expires_at:
                        self._unconfirmed(closing, request.expires_at)
                if self._get(node_id) is not closing:
                    continue
                if self._limit_reached(closing, now):
                    self._isolate(closing, {
                        "source": "release", "use_id": closing.session.use_id,
                        "unconfirmed_since": closing.unconfirmed_since,
                        "reason": "safety_unconfirmed",
                    })
                elif closing.request is None and (closing.retry_at is None or now >= closing.retry_at):
                    self._request(closing)

    def _request(self, closing: _Closing) -> None:
        node = self._node(closing.session.node_id)
        if node is not None and node.device_state == DeviceState.FAULT:
            closing.request = None
            return
        now = self._clock()
        criteria = closing.session.check_criteria
        if node is None or not node.session_id or criteria is None:
            self._unconfirmed(closing, now)
            return
        request = SafetyCheckRequest(
            check_id=str(uuid4()), node_id=node.node_id,
            target_session_id=node.session_id, criteria=criteria, issued_at=now,
            expires_at=now + timedelta(seconds=closing.settings.safety_check_timeout_sec),
        )
        closing.request = request
        closing.retry_at = None
        try:
            self._gateway.request_check(request)
        except StoreError:
            # 동기 인계 중 저장 실패는 전송 실패로 감추지 않는다.
            raise
        except Exception:
            # 동기 콜백이 결과를 이미 확정했으면 전송 예외로 되돌리지 않는다.
            if self._get(node.node_id) is closing and closing.request is request and closing.finish is None:
                self._unconfirmed(closing, self._clock())

    def _unconfirmed(self, closing: _Closing, at: datetime) -> None:
        if closing.unconfirmed_since is None:
            closing.unconfirmed_since = at
        closing.request = None
        closing.retry_at = at + timedelta(seconds=closing.settings.safety_check_retry_sec)
        if self._limit_reached(closing, self._clock()):
            self._isolate(closing, {
                "source": "release", "use_id": closing.session.use_id,
                "unconfirmed_since": closing.unconfirmed_since, "reason": "safety_unconfirmed",
            })

    @staticmethod
    def _limit_reached(closing: _Closing, now: datetime) -> bool:
        return closing.unconfirmed_since is not None and (
            now - closing.unconfirmed_since
        ).total_seconds() >= closing.settings.safety_unconfirmed_limit_sec

    def _isolate(self, closing: _Closing, evidence: Mapping[str, Any]) -> None:
        closing.fault_evidence = evidence
        closing.request = None
        closing.retry_at = None
        result = self._store.isolate_device(closing.session.node_id, evidence)
        if result.outcome == StoreOutcome.CONFLICT:
            raise StoreError("반납 장애 격리를 확인하지 못했다")
        # ALREADY_APPLIED여도 복구 건 저장 실패를 보완한다.
        self._record_fault(closing.session, evidence)
        self._remove(closing.session.node_id)

    def _finish(self, closing: _Closing) -> None:
        assert closing.finish is not None
        closed_at, report = closing.finish
        use_id = closing.session.use_id
        if closing.finish_unknown:
            completed = self._store.close_result(use_id)
            if self._matches(completed, use_id, report):
                self._remove(closing.session.node_id)
                return
        session = self._repository.session(use_id)
        node = self._node(closing.session.node_id)
        if (
            session is None or session.state != SessionState.CLOSING
            or node is None or node.device_state != DeviceState.CLOSING
            or node.session_id != report.session_id
            or session.check_criteria != report.criteria
        ):
            # 저장 불명은 다른 검사로 대체하지 않는다. 완료 기록을 계속 조회한다.
            if not closing.finish_unknown:
                closing.finish = None
                self._unconfirmed(closing, self._clock())
            return
        try:
            result = self._store.finish_close(use_id, closed_at, report.check_id, report.evidence)
        except StoreOutcomeUnknown:
            closing.finish_unknown = True
            if self._matches(self._store.close_result(use_id), use_id, report):
                self._remove(closing.session.node_id)
        except StoreError:
            # IOT는 다시 인계한다. tick도 같은 시각·검사·근거로 재시도한다.
            raise
        else:
            if result.outcome in (StoreOutcome.APPLIED, StoreOutcome.ALREADY_APPLIED):
                self._remove(closing.session.node_id)
            elif not closing.finish_unknown:
                closing.finish = None
                self._unconfirmed(closing, self._clock())

    @staticmethod
    def _matches(completed: CloseResult | None, use_id: str, report: SafetyCheckResult) -> bool:
        return completed is not None and (
            completed.use_id == use_id and completed.check_id == report.check_id
            and completed.end_reason == EndReason.RELEASE and completed.evidence == report.evidence
        )

    def _node(self, node_id: str) -> NodeInfo | None:
        return next((node for node in self._repository.nodes() if node.node_id == node_id), None)

    def _get(self, node_id: str) -> _Closing | None:
        with self._index_lock:
            return self._closing.get(node_id)

    def _put(self, closing: _Closing) -> None:
        with self._index_lock:
            self._closing[closing.session.node_id] = closing

    def _remove(self, node_id: str) -> None:
        with self._index_lock:
            self._closing.pop(node_id, None)

    @staticmethod
    def _missing() -> Decision:
        return Decision("", "", Verdict.DENY, ReasonCode.USE_NOT_FOUND)

    @staticmethod
    def _decision(
        session: UseSession | None, verdict: Verdict, reason: ReasonCode,
        message: str | None = None,
    ) -> Decision:
        return Decision(
            internal_request_id=session.internal_request_id if session else "", request_id="",
            verdict=verdict, reason=reason, node_id=session.node_id if session else None,
            use_id=session.use_id if session else None, message=message,
        )

    def _unavailable(self, session: UseSession | None) -> Decision:
        return self._decision(session, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)
