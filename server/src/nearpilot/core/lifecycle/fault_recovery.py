"""FAULT 격리 뒤 복구 건 생성과 저장 재시도 (NFR-04, NFR-03, FR-19).

조회·승인·정산은 선행 계약을 기다린다. 이 서비스는 RecoveryService가 아니다.
정산 요약값의 계산(settlement_digest)은 승인 경로가 쓸 수 있게 먼저 둔다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from threading import Lock
from typing import Any
from uuid import uuid4

from nearpilot.shared.enums import (
    DeviceState, EndReason, ExecutionStatus, RecoveryStatus, SessionState, StoreOutcome,
)
from nearpilot.shared.errors import StoreError, StoreOutcomeUnknown
from nearpilot.shared.interfaces import ExecutionStore, Repository
from nearpilot.shared.models import (
    ApprovalUsageSettlement, NodeInfo, RecoveryEvidence, RecoveryRecord, UseSession,
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
    ) -> None:
        self._repository = repository
        self._store = store
        self._clock = clock
        self._lock_for = lock_for
        self._pending: dict[str, _Pending] = {}
        self._index_lock = Lock()

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
