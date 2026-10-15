"""FAULT 복구 건, 복구 준비 조회, 복구 승인 (NFR-04, NFR-03, FR-12A, FR-19).

FaultRecoveryService는 복구 건 생성·저장 재시도와 RecoveryService(readiness, approve)를 함께 맡는다.
안전 근거는 현재 연결에 보낸 검사의 passed, 동기화 근거는 그 뒤 같은 연결의 정상 status다 (#85).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from typing import Any
from uuid import uuid4

from nearpilot.core.lifecycle.check_criteria import criteria_of, evidence_valid
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    ActuatorState, ApprovalUsageState, DeviceState, EndReason, ExecutionStatus, RecoveryBlocker,
    RecoveryOutcome, RecoveryReason, RecoveryStatus, SafetyCheckOutcome, SessionState,
    StoreOutcome, TrustState,
)
from nearpilot.shared.errors import StoreError, StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.interfaces import ExecutionStore, Repository, SafetyCheckGateway
from nearpilot.shared.models import (
    ApprovalUsageSettlement, CheckCriteria, CommandRecord, NodeInfo, NodeStatus,
    RecoveryApprovalResult, RecoveryEvidence, RecoveryReadiness, RecoveryRecord,
    RecoverySettlement, RestoreBundle, SafetyCheckRequest, SafetyCheckResult, UseSession,
)

SETTLEMENT_DIGEST_VERSION = 1


def settlement_digest(
    *, recovery_id: str, node_id: str,
    expected_node_state: DeviceState, expected_recovery_status: RecoveryStatus,
    expected_commands: Mapping[str, ExecutionStatus],
    expected_sessions: Mapping[str, SessionState],
    evidence: tuple[RecoveryEvidence, ...],
    usage_settlements: tuple[ApprovalUsageSettlement, ...],
    use_id: str | None = None, closed_at: datetime | None = None,
    end_reason: EndReason | None = None,
) -> str:
    """RecoverySettlement의 논리 정산 요약값 (NFR-04, architecture.md §4).

    승인 계정과 승인 시각은 넣지 않는다. 같은 정산의 반복 승인은 같은 값을 만든다.
    정규 JSON(키 정렬, 공백 없음, UTF-8)의 SHA-256 hex다. 목록은 ID 순서로 정렬한다.
    시각은 UTC ISO 8601로 바꾼다. 시간대가 없는 시각은 받지 않는다.
    """
    for name, ids in (
        ("evidence", [e.evidence_id for e in evidence]),
        ("usage_settlements", [u.usage_id for u in usage_settlements]),
    ):
        if len(ids) != len(set(ids)):
            raise ValueError(f"정산 요약값의 {name}에 같은 ID가 있다")
    body = {
        "v": SETTLEMENT_DIGEST_VERSION,
        "recovery_id": recovery_id,
        "node_id": node_id,
        "expected_node_state": expected_node_state.value,
        "expected_recovery_status": expected_recovery_status.value,
        "expected_commands": {k: v.value for k, v in expected_commands.items()},
        "expected_sessions": {k: v.value for k, v in expected_sessions.items()},
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "session_id": item.session_id,
                "observed_at": _utc(item.observed_at),
                "execution_outcome": _value(item.execution_outcome),
                "safety_confirmed": item.safety_confirmed,
                "physical_state_synced": item.physical_state_synced,
            }
            for item in sorted(evidence, key=lambda e: e.evidence_id)
        ],
        "usage_settlements": [
            {"usage_id": u.usage_id, "expected_state": u.expected_state.value, "state": u.state.value}
            for u in sorted(usage_settlements, key=lambda u: u.usage_id)
        ],
        "use_id": use_id,
        "closed_at": None if closed_at is None else _utc(closed_at),
        "end_reason": _value(end_reason),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(text.encode("utf-8")).hexdigest()


def _utc(at: datetime) -> str:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("정산 요약값의 시각에는 시간대가 필요하다")
    return at.astimezone(timezone.utc).isoformat()


def _value(item: Any) -> Any:
    return None if item is None else item.value


@dataclass
class _Pending:
    record: RecoveryRecord


class NotPlaceOwner(PermissionError):
    """복구 준비 조회를 장소 소유자가 아닌 계정이 요청했다. 기기 정보를 담지 않는다."""


@dataclass
class _Check:
    """복구 건 하나와 기기 연결 하나에 묶인 안전 검사. 메모리에만 둔다."""

    recovery_id: str
    request: SafetyCheckRequest
    passed: SafetyCheckResult | None = None
    synced: NodeStatus | None = None


# 보류 사용분을 반환하는 명령 결과. 확인 불가(UNKNOWN)와 성공은 소진한다 (NFR-04).
_RELEASED_BY = (ExecutionStatus.FAILED, ExecutionStatus.NOT_SENT, ExecutionStatus.REJECTED)
# 결과를 확정하지 못한 명령. 복구 승인의 실행 여부 미확인이다.
_UNRESOLVED = (ExecutionStatus.PENDING, ExecutionStatus.IN_PROGRESS)


class FaultRecoveryService:
    """격리가 저장된 뒤 호출한다. 성공 반환은 복구 건 저장의 확인이다.

    lock_for는 반납·실행·관측과 공유하는 기기별 재진입 잠금이다.
    실패한 인계는 같은 내용으로 재호출하거나 외부 tick으로 재시도한다.
    재시작 뒤 첫 인계는 restore_pending으로 기존 복구 건을 확인한다.
    """

    def __init__(
        self, *, repository: Repository, store: ExecutionStore,
        clock: Callable[[], datetime],
        lock_for: Callable[[str], AbstractContextManager[Any]],
        gateway: SafetyCheckGateway | None = None,
        settings: Callable[[], Settings] = Settings,
        new_id: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._repository = repository
        self._store = store
        self._clock = clock
        self._lock_for = lock_for
        self._gateway = gateway
        self._settings = settings
        self._new_id = new_id
        self._pending: dict[str, _Pending] = {}
        self._checks: dict[str, _Check] = {}
        self._settling: dict[str, RecoverySettlement] = {}
        self._index_lock = Lock()

    # ── 복구 준비 조회와 복구 승인 (FR-12A, NFR-04) ─────────────────

    def readiness(self, user_id: str, node_id: str) -> RecoveryReadiness:
        """소유자에게 준비 상태를 반환한다. 저장 함수를 부르지 않는다.

        안전 근거가 없으면 현재 연결에 검사를 요청한다. 검사 요청은 기기 상태를 바꾸지 않는다.
        """
        with self._lock_for(node_id):
            if not self._repository.is_place_owner(user_id, node_id):
                raise NotPlaceOwner()
            return self._assess(node_id)[0]

    def approve(self, user_id: str, node_id: str, recovery_id: str) -> RecoveryApprovalResult:
        """조건을 다시 검사하고 통과하면 settle_recovery로 정산한다. 승인 주체는 user_id다."""
        with self._lock_for(node_id):
            now = self._clock()
            if not self._repository.is_place_owner(user_id, node_id):
                hidden = RecoveryReadiness(node_id, None, RecoveryStatus.NOT_READY, (), (), now)
                return RecoveryApprovalResult(recovery_id, RecoveryOutcome.REJECTED,
                                              RecoveryReason.NOT_OWNER, hidden)
            stored = self._store.recovery(recovery_id)
            if stored is not None and stored.node_id == node_id and stored.completion is not None:
                return self._completed(stored, now)
            readiness, record, bundle = self._assess(node_id)
            if record is None or record.recovery_id != recovery_id:
                return RecoveryApprovalResult(recovery_id, RecoveryOutcome.REJECTED,
                                              RecoveryReason.RECOVERY_MISMATCH, readiness)
            blockers = [b for b in readiness.blockers if b != RecoveryBlocker.STORE_OUTCOME_UNKNOWN
                        or recovery_id not in self._settling]
            if blockers:
                return RecoveryApprovalResult(recovery_id, RecoveryOutcome.REJECTED,
                                              RecoveryReason.NOT_READY, readiness)
            previous = self._settling.get(recovery_id)
            # 불명 저장의 재시도는 처음 정한 종료 시각으로 같은 논리 정산을 만든다.
            closed_at = now if previous is None or previous.closed_at is None else previous.closed_at
            settlement = self._settlement(user_id, now, record, bundle, readiness.evidence, closed_at)
            if previous is not None and previous.settlement_digest == settlement.settlement_digest:
                # 불명 저장의 재시도는 처음 승인한 정산을 그대로 보낸다.
                settlement = previous
            self._settling[recovery_id] = settlement
            try:
                result = self._store.settle_recovery(settlement)
            except StoreNotApplied:
                self._settling.pop(recovery_id, None)
                return RecoveryApprovalResult(recovery_id, RecoveryOutcome.REJECTED,
                                              RecoveryReason.STORE_NOT_APPLIED, readiness)
            except StoreOutcomeUnknown:
                return self._confirm(recovery_id, readiness, now)
            if result.outcome == StoreOutcome.CONFLICT:
                self._settling.pop(recovery_id, None)
                return RecoveryApprovalResult(recovery_id, RecoveryOutcome.REJECTED,
                                              RecoveryReason.STATE_CONFLICT, readiness)
            stored = result.recovery
            if stored is not None and stored.completion is not None:
                return self._completed(stored, now)
            return self._confirm(recovery_id, readiness, now)

    def on_check_result(self, result: SafetyCheckResult) -> bool:
        """IOT가 검증한 검사 결과다. 복구 검사의 결과이면 처리하고 True를 반환한다."""
        with self._lock_for(result.node_id):
            check = self._checks.get(result.node_id)
            if check is None or check.request.check_id != result.check_id:
                return False
            request = check.request
            if (
                result.session_id != request.target_session_id or result.criteria != request.criteria
                or not request.issued_at <= result.received_at <= request.expires_at
                or not evidence_valid(request.criteria, result.outcome, result.evidence)
            ):
                # 유효하지 않은 결과는 응답 기한 초과와 같게 다룬다.
                return True
            if result.outcome != SafetyCheckOutcome.PASSED:
                del self._checks[result.node_id]
                return True
            if check.passed is not None:
                return True
            # 근거 저장이 실패해도 저장 대기가 같은 근거를 재시도한다.
            check.passed = result
            self.record_isolated(result.node_id, evidence=(RecoveryEvidence(
                result.event_id, result.session_id, result.checked_at, safety_confirmed=True),))
            return True

    def on_status(self, status: NodeStatus) -> None:
        """안전 근거 뒤 같은 연결의 정상 status를 물리 상태 동기화 근거로 쓴다."""
        with self._lock_for(status.node_id):
            check = self._checks.get(status.node_id)
            if (
                check is None or check.passed is None or check.synced is not None
                or status.session_id != check.passed.session_id
                or status.received_at <= check.passed.received_at
                or status.physical.sensor_fault is not False
                or status.physical.actuator_state != ActuatorState.IDLE
            ):
                return
            check.synced = status
            self.record_isolated(status.node_id, evidence=(RecoveryEvidence(
                f"status:{status.node_id}:{status.session_id}:{status.seq}", status.session_id,
                status.observed_at, physical_state_synced=True),))

    def _assess(
        self, node_id: str,
    ) -> tuple[RecoveryReadiness, RecoveryRecord | None, RestoreBundle | None]:
        now = self._clock()
        bundles = [b for b in self._store.restore_pending() if b.node.node_id == node_id]
        if len(bundles) > 1:
            raise StoreError("기기의 복원 묶음이 중복이다")
        bundle = bundles[0] if bundles else None
        active = [] if bundle is None else [r for r in bundle.recoveries if r.completion is None]
        if len(active) > 1:
            raise StoreError("기기에 미완료 복구 건이 둘 이상이다")
        if not active:
            self._checks.pop(node_id, None)
            return (RecoveryReadiness(node_id, None, RecoveryStatus.NOT_READY,
                                      (RecoveryBlocker.NO_RECOVERY_REQUIRED,), (), now), None, bundle)
        record = active[0]
        node = self._node(node_id)
        settings = self._settings()
        blockers: list[RecoveryBlocker] = []
        trusted = node is not None and node.trust_state == TrustState.TRUSTED
        if not trusted:
            blockers.append(RecoveryBlocker.NOT_TRUSTED)
        online = node is not None and bool(node.session_id) and node.last_seen_at is not None and (
            node.last_seen_at <= now <= node.last_seen_at + timedelta(seconds=settings.online_ttl_sec))
        if not online:
            blockers.append(RecoveryBlocker.OFFLINE)
        if any(c.status in _UNRESOLVED for c in self._commands(record)):
            blockers.append(RecoveryBlocker.EXECUTION_UNCONFIRMED)
        check = self._checks.get(node_id)
        if check is not None and (check.recovery_id != record.recovery_id or node is None
                                  or check.request.target_session_id != node.session_id):
            # 재접속이나 새 장애는 기존 근거를 무효로 한다.
            del self._checks[node_id]
            check = None
        if check is None or check.passed is None:
            blockers.append(RecoveryBlocker.SAFETY_UNCONFIRMED)
        if check is None or check.synced is None:
            blockers.append(RecoveryBlocker.PHYSICAL_STATE_UNSYNCED)
        if self._get(node_id) is not None or record.recovery_id in self._settling:
            blockers.append(RecoveryBlocker.STORE_OUTCOME_UNKNOWN)
        # 실행 근거는 저장한 참조를 쓴다. 안전·동기화 근거는 현재 연결에서 확인한 것만 쓴다.
        evidence = [e for e in record.evidence if e.execution_outcome is not None]
        if check is not None and check.passed is not None:
            evidence.append(RecoveryEvidence(check.passed.event_id, check.passed.session_id,
                                             check.passed.checked_at, safety_confirmed=True))
        if check is not None and check.synced is not None:
            synced = check.synced
            evidence.append(RecoveryEvidence(f"status:{synced.node_id}:{synced.session_id}:{synced.seq}",
                                             synced.session_id, synced.observed_at,
                                             physical_state_synced=True))
        if (online and trusted and RecoveryBlocker.EXECUTION_UNCONFIRMED not in blockers
                and (check is None or (check.passed is None and now > check.request.expires_at))):
            self._request_check(record, node, settings, now)
        if record.recovery_id in self._settling:
            status = RecoveryStatus.RECOVERING
        else:
            status = RecoveryStatus.NOT_READY if blockers else RecoveryStatus.READY
        return (RecoveryReadiness(node_id, record.recovery_id, status, tuple(blockers),
                                  tuple(evidence), now), record, bundle)

    def _request_check(self, record: RecoveryRecord, node: NodeInfo, settings: Settings,
                       now: datetime) -> None:
        # 점유 세션이 있으면 세션 생성 당시 기준, 없거나 끝났으면 기기의 현재 기준을 쓴다 (#85).
        session = None if record.use_id is None else self._repository.session(record.use_id)
        if session is not None and session.state != SessionState.CLOSED:
            criteria: CheckCriteria | None = session.check_criteria
        else:
            criteria = criteria_of(node.release_policy)
        if criteria is None or self._gateway is None:
            return
        request = SafetyCheckRequest(self._new_id(), node.node_id, node.session_id, criteria, now,
                                     now + timedelta(seconds=settings.safety_check_timeout_sec))
        self._checks[node.node_id] = _Check(record.recovery_id, request)
        try:
            self._gateway.request_check(request)
        except Exception:
            # 전송 실패는 응답 기한 초과와 같다. 기한이 지난 뒤의 조회에서 다시 요청한다.
            pass

    def _settlement(
        self, user_id: str, now: datetime, record: RecoveryRecord, bundle: RestoreBundle,
        evidence: tuple[RecoveryEvidence, ...], close_at: datetime,
    ) -> RecoverySettlement:
        commands = {c.internal_request_id: c for c in self._commands(record)}
        usages = []
        for usage in bundle.approval_usages:
            if usage.internal_request_id not in record.internal_request_ids or usage.state not in (
                ApprovalUsageState.HELD, ApprovalUsageState.RESERVED,
            ):
                continue
            command = commands.get(usage.internal_request_id)
            # 명령이 없거나 전송을 시작하지 않은 요청은 실행되지 않았다.
            released = command is None or command.status in _RELEASED_BY or not command.send_attempted
            usages.append(ApprovalUsageSettlement(
                usage.usage_id, usage.state,
                ApprovalUsageState.RELEASED if released else ApprovalUsageState.CONSUMED))
        sessions: dict[str, SessionState] = {}
        use_id = closed_at = end_reason = None
        if record.use_id is not None:
            session = self._repository.session(record.use_id)
            if session is None:
                raise StoreError("복구 건의 점유 세션을 확인하지 못했다")
            sessions[session.use_id] = session.state
            if session.state != SessionState.CLOSED:
                use_id, closed_at, end_reason = session.use_id, close_at, EndReason.FAULT
        node_state = bundle.node.device_state
        expected_commands = {c.command_id: c.status for c in commands.values()}
        digest = settlement_digest(
            recovery_id=record.recovery_id, node_id=record.node_id,
            expected_node_state=node_state, expected_recovery_status=record.status,
            expected_commands=expected_commands, expected_sessions=sessions,
            evidence=evidence, usage_settlements=tuple(usages),
            use_id=use_id, closed_at=closed_at, end_reason=end_reason,
        )
        return RecoverySettlement(
            record.recovery_id, record.node_id, user_id, now, digest, node_state, record.status,
            expected_commands, sessions, evidence, tuple(usages), use_id, closed_at, end_reason,
        )

    def _commands(self, record: RecoveryRecord) -> list[CommandRecord]:
        commands = []
        for request_id in record.internal_request_ids:
            command = self._store.execution(request_id)
            if command is not None and command.command_id in record.command_ids:
                commands.append(command)
        return commands

    def _confirm(self, recovery_id: str, readiness: RecoveryReadiness,
                 now: datetime) -> RecoveryApprovalResult:
        """불명 저장은 같은 ID로 조회한다. None은 미반영의 증거가 아니다."""
        try:
            stored = self._store.recovery(recovery_id)
        except StoreError:
            stored = None
        if stored is not None and stored.completion is not None:
            return self._completed(stored, now)
        pending = replace(readiness, status=RecoveryStatus.RECOVERING,
                          blockers=(RecoveryBlocker.STORE_OUTCOME_UNKNOWN,))
        return RecoveryApprovalResult(recovery_id, RecoveryOutcome.PENDING,
                                      RecoveryReason.STORE_OUTCOME_UNKNOWN, pending)

    def _completed(self, stored: RecoveryRecord, now: datetime) -> RecoveryApprovalResult:
        self._settling.pop(stored.recovery_id, None)
        check = self._checks.get(stored.node_id)
        if check is not None and check.recovery_id == stored.recovery_id:
            del self._checks[stored.node_id]
        completion = stored.completion
        readiness = RecoveryReadiness(stored.node_id, stored.recovery_id, RecoveryStatus.RECOVERED,
                                      (), completion.settlement.evidence, now)
        return RecoveryApprovalResult(stored.recovery_id, RecoveryOutcome.COMPLETED, RecoveryReason.OK,
                                      readiness, completion.completed_at)

    def _node(self, node_id: str) -> NodeInfo | None:
        nodes = [n for n in self._repository.nodes() if n.node_id == node_id]
        if len(nodes) > 1:
            raise StoreError("기기 조회가 중복이다")
        return nodes[0] if nodes else None

    # ── 복구 건 생성과 저장 재시도 ──────────────────────────────────

    def record_fault(self, session: UseSession, evidence: Mapping[str, Any]) -> None:
        """ReleaseService(record_fault=service.record_fault)의 필수 접점이다.

        fault 보고의 참조만 근거로 보존한다. 확인 불능 진단은 안전 근거가 아니다.
        """
        with self._lock_for(session.node_id):
            command = self._store.execution(session.internal_request_id)
            if command is not None and (
                command.node_id != session.node_id or command.use_id != session.use_id
                or command.internal_request_id != session.internal_request_id
            ):
                raise StoreError("반납의 명령 연결이 다르다")
            references: tuple[RecoveryEvidence, ...] = ()
            if evidence.get("event_id") is not None:
                event_id = evidence["event_id"]
                connection_id = evidence.get("session_id")
                checked_at = evidence.get("checked_at")
                if (
                    not isinstance(event_id, str) or not event_id
                    or not isinstance(connection_id, str) or not connection_id
                    or not isinstance(checked_at, datetime)
                    or evidence.get("outcome") != "fault"
                ):
                    raise ValueError("반납 장애 보고의 근거 참조가 부족하다")
                references = (RecoveryEvidence(
                    event_id, connection_id, checked_at, safety_confirmed=False,
                ),)
            self.record_isolated(
                session.node_id, internal_request_ids=(session.internal_request_id,),
                command_ids=() if command is None else (command.command_id,),
                use_id=session.use_id, evidence=references,
            )

    def record_isolated(
        self, node_id: str, *, internal_request_ids: tuple[str, ...] = (),
        command_ids: tuple[str, ...] = (), use_id: str | None = None,
        evidence: tuple[RecoveryEvidence, ...] = (),
    ) -> RecoveryRecord:
        """실행·관측·복원 담당의 내부 인계다. 대상 연결은 처음 정한 값을 유지한다."""
        if not node_id:
            raise ValueError("기기 ID가 비어 있다")
        evidence = self._merge_evidence((), evidence)
        with self._lock_for(node_id):
            self._require_fault(node_id)
            pending = self._get(node_id)
            if pending is None:
                # 조회 실패를 빈 목록으로 바꾸지 않는다. 조회 뒤에만 새 ID를 만든다.
                bundles = [b for b in self._store.restore_pending() if b.node.node_id == node_id]
                if len(bundles) != 1 or bundles[0].node.device_state != DeviceState.FAULT:
                    raise StoreError("격리 기기의 복원 묶음을 확인하지 못했다")
                active = [r for r in bundles[0].recoveries if r.completion is None]
                if len(active) > 1:
                    raise StoreError("기기에 미완료 복구 건이 둘 이상이다")
                if active:
                    record = active[0]
                    if record.node_id != node_id or record.status == RecoveryStatus.RECOVERED:
                        raise StoreError("미완료 복구 건의 상태나 기기 연결이 다르다")
                else:
                    record = RecoveryRecord(
                        str(uuid4()), node_id, tuple(dict.fromkeys(internal_request_ids)),
                        tuple(dict.fromkeys(command_ids)), use_id, self._clock(),
                        RecoveryStatus.NOT_READY,
                    )
                pending = _Pending(record)
            record = pending.record
            if (
                not set(internal_request_ids).issubset(record.internal_request_ids)
                or not set(command_ids).issubset(record.command_ids)
                or (use_id is not None and use_id != record.use_id)
            ):
                raise StoreError("같은 장애의 대상 연결을 바꿀 수 없다")
            pending.record = replace(record, evidence=self._merge_evidence(record.evidence, evidence))
            self._put(node_id, pending)
            return self._persist(node_id, pending)

    def tick(self) -> None:
        """저장 대기를 한 번씩 재시도한다. 한 기기의 실패가 다른 기기를 막지 않는다."""
        with self._index_lock:
            node_ids = tuple(self._pending)
        first_error: StoreError | None = None
        for node_id in node_ids:
            with self._lock_for(node_id):
                pending = self._get(node_id)
                if pending is None:
                    continue
                try:
                    self._persist(node_id, pending)
                except StoreError as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error

    def _persist(self, node_id: str, pending: _Pending) -> RecoveryRecord:
        record = pending.record
        # 불명 저장과 완료 정산을 먼저 확인한다. None은 미반영의 증거가 아니다.
        stored = self._store.recovery(record.recovery_id)
        if stored is not None:
            self._same_target(record, stored)
            if stored.completion is not None:
                self._remove(node_id)
                return stored
            merged = self._merge_evidence(stored.evidence, record.evidence)
            if merged == stored.evidence:
                self._remove(node_id)
                return stored
            record = replace(stored, evidence=merged)
            pending.record = record
        self._require_fault(node_id)
        try:
            result = self._store.save_recovery(record)
        except StoreOutcomeUnknown:
            stored = self._store.recovery(record.recovery_id)
            if stored is not None:
                self._same_target(record, stored)
                if stored.completion is not None or self._contains(stored, record):
                    self._remove(node_id)
                    return stored
            raise
        if result.outcome == StoreOutcome.CONFLICT:
            raise StoreError("복구 건 저장이 충돌했다")
        stored = result.recovery
        if stored is not None:
            self._same_target(record, stored)
            if stored.completion is None and not self._contains(stored, record):
                raise StoreError("복구 건의 근거 저장을 확인하지 못했다")
        self._remove(node_id)
        return record if stored is None else stored

    def _require_fault(self, node_id: str) -> NodeInfo:
        nodes = [n for n in self._repository.nodes() if n.node_id == node_id]
        if len(nodes) != 1 or nodes[0].device_state != DeviceState.FAULT:
            raise StoreError("복구 건 생성 전에 기기 격리를 확인해야 한다")
        return nodes[0]

    @staticmethod
    def _same_target(expected: RecoveryRecord, actual: RecoveryRecord) -> None:
        if (
            expected.recovery_id != actual.recovery_id or expected.node_id != actual.node_id
            or expected.internal_request_ids != actual.internal_request_ids
            or expected.command_ids != actual.command_ids or expected.use_id != actual.use_id
            or expected.created_at != actual.created_at
        ):
            raise StoreError("복구 건의 대상 연결이 다르다")

    @staticmethod
    def _merge_evidence(
        existing: tuple[RecoveryEvidence, ...], incoming: tuple[RecoveryEvidence, ...],
    ) -> tuple[RecoveryEvidence, ...]:
        by_id: dict[str, RecoveryEvidence] = {}
        for item in (*existing, *incoming):
            if not item.evidence_id or not item.session_id:
                raise ValueError("복구 근거의 ID와 기기 연결 ID가 필요하다")
            if item.evidence_id in by_id and by_id[item.evidence_id] != item:
                raise StoreError("같은 근거 ID의 내용이 다르다")
            by_id[item.evidence_id] = item
        return tuple(by_id.values())

    @staticmethod
    def _contains(stored: RecoveryRecord, wanted: RecoveryRecord) -> bool:
        return all(item in stored.evidence for item in wanted.evidence)

    def _get(self, node_id: str) -> _Pending | None:
        with self._index_lock:
            return self._pending.get(node_id)

    def _put(self, node_id: str, pending: _Pending) -> None:
        with self._index_lock:
            self._pending[node_id] = pending

    def _remove(self, node_id: str) -> None:
        with self._index_lock:
            self._pending.pop(node_id, None)
