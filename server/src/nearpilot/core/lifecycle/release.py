"""대여형 명시적 반납과 논리적 종료 (FR-19, FR-20, FR-21).

반납 완료는 서버의 논리적 점유 해제다. 문 닫힘, 잠금이나 센서 응답을 기다리지 않는다.
호출자는 tick을 반복 호출해 저장 결과가 불명인 종료를 다시 확인한다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    DeviceState, EndReason, ReasonCode, SessionState, StoreConflictReason, StoreOutcome, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreOutcomeUnknown
from nearpilot.shared.interfaces import ExecutionStore, Repository
from nearpilot.shared.models import CloseResult, Decision, NodeInfo, UseSession

_DONE = "반납을 완료했다. 서버의 점유를 해제했다."
_CONFIRMING = "종료 기록을 확인 중이다."


class ReleaseService:
    """CORE 반납 서비스. 다른 계층에는 shared Protocol로만 의존한다.

    lock_for는 node_id별 동일한 재진입 잠금을 반환해야 한다.
    메모리에는 종료를 저장하지 못한 반납과 처음 정한 종료 시각만 둔다.
    """

    def __init__(
        self, *, repository: Repository, store: ExecutionStore,
        settings: Callable[[], Settings], clock: Callable[[], datetime],
        lock_for: Callable[[str], AbstractContextManager[Any]],
    ) -> None:
        self._repository = repository
        self._store = store
        self._settings = settings
        self._clock = clock
        self._lock_for = lock_for
        # use_id → 처음 정한 종료 시각. 재시도는 같은 시각으로 저장한다.
        self._closing: dict[str, datetime] = {}
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
                return self._closed(session)
            node = self._node(session.node_id)
            if node is None or node.device_state == DeviceState.FAULT:
                # FAULT 기기의 점유는 복구 절차가 정산한다 (NFR-04).
                return self._unavailable(session)
            if session.state == SessionState.ACTIVE:
                try:
                    result = self._store.begin_close(use_id, EndReason.RELEASE)
                except StoreOutcomeUnknown:
                    # 저장이 불명이어도 확인한 CLOSING에서만 종료를 이어 간다.
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
            if session.end_reason != EndReason.RELEASE:
                return self._unavailable(session)
            return self._finish(session)

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
        """복원 담당이 현재 기기를 확인한 뒤 호출한다. 같은 논리 조건을 다시 검사하고 저장한다."""
        session = self._repository.session(use_id)
        if session is None:
            return
        with self._lock_for(session.node_id):
            session = self._repository.session(use_id)
            if (
                session is None or session.state != SessionState.CLOSING
                or session.end_reason != EndReason.RELEASE
            ):
                return
            node = self._node(session.node_id)
            if node is None or node.device_state == DeviceState.FAULT:
                return
            self._finish(session)

    def tick(self) -> None:
        """기기별 잠금 안에서 저장하지 못한 종료를 다시 확인하고 저장한다."""
        with self._index_lock:
            use_ids = tuple(self._closing)
        for use_id in use_ids:
            session = self._repository.session(use_id)
            if session is None:
                self._forget(use_id)
                continue
            with self._lock_for(session.node_id):
                session = self._repository.session(use_id)
                if session is None:
                    self._forget(use_id)
                elif session.state == SessionState.CLOSED:
                    self._closed(session)
                elif session.state != SessionState.CLOSING or session.end_reason != EndReason.RELEASE:
                    self._forget(use_id)
                else:
                    node = self._node(session.node_id)
                    if node is None or node.device_state == DeviceState.FAULT:
                        # FAULT 기기의 점유는 복구 절차가 정산한다.
                        self._forget(use_id)
                    else:
                        self._finish(session)

    def _finish(self, session: UseSession) -> Decision:
        """종료 직전에 기기 상태를 다시 검사하고 논리적 종료를 저장한다 (FR-20)."""
        use_id = session.use_id
        node = self._node(session.node_id)
        if node is None or node.device_state != DeviceState.CLOSING:
            self._forget(use_id)
            return self._unavailable(session)
        with self._index_lock:
            closed_at = self._closing.setdefault(use_id, self._clock())
        try:
            result = self._store.finish_close(use_id, closed_at, EndReason.RELEASE)
        except StoreOutcomeUnknown:
            # 현재 AVAILABLE 여부로 저장 성공을 추정하지 않는다. 완료 기록을 조회한다.
            if self._released(self._store.close_result(use_id), use_id):
                self._forget(use_id)
                return self._decision(session, Verdict.OK, ReasonCode.OK, _DONE)
            return self._decision(session, Verdict.BUSY, ReasonCode.OK, _CONFIRMING)
        except StoreError:
            # 변경 없음이 확실한 저장 오류다. CLOSING은 tick과 다음 요청이 다시 저장한다.
            return self._decision(session, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
        if result.outcome in (StoreOutcome.APPLIED, StoreOutcome.ALREADY_APPLIED):
            self._forget(use_id)
            return self._decision(session, Verdict.OK, ReasonCode.OK, _DONE)
        self._forget(use_id)
        if result.conflict_reason == StoreConflictReason.COMMAND_IN_PROGRESS:
            return self._decision(session, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS)
        return self._unavailable(session)

    def _closed(self, session: UseSession) -> Decision:
        """CLOSED 세션의 재요청은 저장한 완료 기록으로 답한다. 다시 쓰지 않는다."""
        completed = self._store.close_result(session.use_id)
        if self._released(completed, session.use_id):
            self._forget(session.use_id)
            return self._decision(session, Verdict.OK, ReasonCode.OK, _DONE)
        if completed is None:
            return self._decision(session, Verdict.BUSY, ReasonCode.OK, _CONFIRMING)
        # 복구나 취소로 끝난 세션은 반납 완료가 아니다.
        self._forget(session.use_id)
        return self._unavailable(session)

    @staticmethod
    def _released(completed: CloseResult | None, use_id: str) -> bool:
        return completed is not None and completed.use_id == use_id and completed.end_reason == EndReason.RELEASE

    def _node(self, node_id: str) -> NodeInfo | None:
        return next((node for node in self._repository.nodes() if node.node_id == node_id), None)

    def _forget(self, use_id: str) -> None:
        with self._index_lock:
            self._closing.pop(use_id, None)

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
