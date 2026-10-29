"""`ExecutionStore` 구현 (#17, specs/db-execution-store/spec.md).

예약 이후의 사건별 저장을 맡는다. `SqliteRepository`와 같은 연결을 받아 같은 쓰기 경로를 쓴다
(`AGENTS.md` 의존 규칙 5). 함수는 spec 6절의 구현 순서대로 추가한다.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from nearpilot.db.repositories._common import (
    deferred_transaction, from_json, immediate_transaction, iso, light_controls_from_json,
    light_from_json, light_to_json, new_id, parse_iso, settings_from_json, settings_to_json,
    speaker_controls_from_json, speaker_from_json, speaker_to_json, to_json,
)
from nearpilot.shared.enums import (
    Action,
    ApprovalUsageState,
    Capability,
    CommandOutcome,
    DeviceState,
    EndReason,
    ExecutionKind,
    ExecutionStatus,
    InternalRequestCause,
    Occupancy,
    ReasonCode,
    RecoveryStatus,
    SessionState,
    Step,
    StoreConflictReason,
    StoreOutcome,
    TrustState,
    Verdict,
)
from nearpilot.shared.errors import StoreError
from nearpilot.shared.models import (
    ApprovalUsageRecord,
    ApprovalUsageSettlement,
    CloseResult,
    CommandRecord,
    Decision,
    ExecutionContext,
    InternalRequest,
    LightSetting,
    NodeInfo,
    PendingRequestRecord,
    RecoveryCompletion,
    RecoveryEvidence,
    RecoveryRecord,
    RecoverySettlement,
    RestoreBundle,
    RestoredSession,
    SpeakerSetting,
    StoreResult,
    TargetSpec,
    UseRequest,
    UseSession,
)


def _evidence_matches(stored_json: str | None, evidence: Mapping[str, Any]) -> bool:
    """저장된 근거와 이번 호출의 근거가 같은 사건인지 본다.

    `evidence`에 `datetime`이 섞여 있으면(JSON은 문자열로만 담는다) 호출 쪽 값도
    `to_json`으로 한 번 거쳐서 같은 문자열화를 적용한 뒤 비교한다. 그래야 `datetime` 객체와
    저장된 문자열의 타입 차이만으로 다른 사건으로 오판하지 않는다.
    """
    return from_json(to_json(evidence)) == from_json(stored_json)


class _RentalClosed(LookupError):
    """재조작을 붙일 대여가 닫혔다. 기기에 `ACTIVE` 세션이 없다.

    `link_command`만 이것을 연결 거부로 바꾼다. 복원 조회는 `LookupError`로 그대로 둔다.
    """


class SqliteExecutionStore:
    """`ExecutionStore` Protocol 구현."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 실행 (#17 1단계) ────────────────────────────────────────────

    def link_command(
        self, internal_request_id: str, node_id: str, action: Action, light: LightSetting | None = None,
        speaker: SpeakerSetting | None = None,
    ) -> StoreResult:
        with immediate_transaction(self._conn):
            existing = self._conn.execute(
                "SELECT * FROM node_command WHERE internal_request_id = ?", (internal_request_id,)
            ).fetchone()
            if existing is not None:
                return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, command=self._command_from_row(existing))
            if self._already_cancelled(internal_request_id):
                # cancel_not_sent 가 명령 연결 전에 취소했다. 뒤늦게 온 link_command 는 CONFLICT
                # 다 (spec §6 결정). 미전송 확정 뒤에 새 명령을 만들면 의미가 없어진다.
                return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)

            request_row = self._conn.execute(
                "SELECT received_at, request_kind FROM request WHERE internal_request_id = ?",
                (internal_request_id,),
            ).fetchone()
            if request_row is None:
                raise LookupError(f"요청 {internal_request_id} 가 없다")
            node_row = self._conn.execute(
                "SELECT occupancy FROM node WHERE node_id = ?", (node_id,)
            ).fetchone()
            if node_row is None:
                raise LookupError(f"기기 {node_id} 가 없다")

            if request_row["request_kind"] == "server_internal":
                # 서버 내부 요청은 세션·사용분이 없다 (#86). occupancy로 판단하지 않는다.
                execution_kind, use_id, usage_id = ExecutionKind.SERVER_INTERNAL, None, None
            else:
                try:
                    execution_kind, use_id = self._resolve_execution_kind(
                        internal_request_id, node_id, node_row["occupancy"]
                    )
                except _RentalClosed:
                    return self._refuse_closed_rental(
                        internal_request_id, node_id, action, request_row["received_at"]
                    )
                usage_row = self._live_usage(internal_request_id)
                usage_id = usage_row["usage_id"] if usage_row else None

            command_id = new_id()
            self._conn.execute(
                "INSERT INTO node_command (command_id, internal_request_id, node_id, action, status, "
                "execution_kind, use_id, usage_id, reserved_at, send_started_at, send_attempted, "
                "outcome, occurred_at, evidence, light, speaker) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, '{}', ?, ?)",
                (
                    command_id, internal_request_id, node_id, action.value, ExecutionStatus.PENDING.value,
                    execution_kind.value, use_id, usage_id, request_row["received_at"], light_to_json(light),
                    speaker_to_json(speaker),
                ),
            )
            return StoreResult(
                outcome=StoreOutcome.APPLIED,
                command=CommandRecord(
                    command_id=command_id, internal_request_id=internal_request_id, node_id=node_id,
                    action=action, status=ExecutionStatus.PENDING, execution_kind=execution_kind,
                    use_id=use_id, usage_id=usage_id, reserved_at=parse_iso(request_row["received_at"]),
                    light=light, speaker=speaker,
                ),
            )

    def link_internal_request(self, req: InternalRequest) -> StoreResult:
        """서버 내부 요청을 사용자 ID와 판정 기록 없이 저장한다 (#86).

        `request_kind = 'server_internal'`, `user_id = NULL`로 저장한다. `input_digest`·`target_spec`은
        방문자 요청의 중복 방지·근접 대상 지정에 쓰는 값이라 이 요청에는 의미가 없다. `target_spec`은
        `node_id`를 그대로 담아 NOT NULL을 만족시키되, `execution_context`는 내부 요청을 `UseRequest`로
        복원하지 않으므로 다시 해석되지 않는다. 적용 설정은 `settings_json`에 보존한다.
        """
        with immediate_transaction(self._conn):
            existing = self._conn.execute(
                "SELECT node_id, action, internal_cause, settings_json "
                "FROM request WHERE internal_request_id = ?",
                (req.internal_request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["node_id"] == req.node_id
                    and existing["action"] == req.action.value
                    and existing["internal_cause"] == req.cause.value
                    and existing["settings_json"] == settings_to_json(req.settings)
                ):
                    return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED)
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.CONTENT_CONFLICT
                )
            self._conn.execute(
                "INSERT INTO request (internal_request_id, user_id, request_kind, request_id, "
                "input_digest, node_id, target_spec, action, internal_cause, received_at, settings_json) "
                "VALUES (?, NULL, 'server_internal', NULL, '', ?, ?, ?, ?, ?, ?)",
                (
                    req.internal_request_id, req.node_id, req.node_id, req.action.value,
                    req.cause.value, iso(req.created_at), settings_to_json(req.settings),
                ),
            )
            return StoreResult(outcome=StoreOutcome.APPLIED)

    def _resolve_execution_kind(
        self, internal_request_id: str, node_id: str, occupancy: str
    ) -> tuple[ExecutionKind, str | None]:
        """점유 방식과 기존 점유 세션으로 실행 종류와 연결할 `use_id`를 정한다.

        link_command는 execution_kind·use_id를 인자로 받지 않는다 (`interfaces.py`). `node.occupancy`를
        가장 먼저 본다. 승인 사용분(`approval_usage`) 행의 존재 여부로 판단하지 않는다 — `open`
        정책의 재조작은 사용분을 예약하지 않아 행이 없기 때문이다 (#27).

        1. `SHARED`면 공용형이다. 점유 세션이 없다.
        2. 대여형이고 이 `internal_request_id`로 만든 살아 있는 세션이 있으면 최초 대여다.
        3. 대여형이고 그 세션이 없는데 기기에 `ACTIVE` 세션이 있으면 재조작이다 (FR-19, #35).
        4. `ACTIVE` 세션도 없으면 `_RentalClosed`를 던진다. 재조작 판정 뒤 반납이 대여를 닫은 경우다.
           반납을 시작한 `CLOSING` 세션만 남은 경우도 같다.
        """
        if occupancy == Occupancy.SHARED.value:
            return ExecutionKind.SHARED, None
        own_session = self._live_session(internal_request_id)
        if own_session is not None:
            return ExecutionKind.RENTAL_INITIAL, own_session["use_id"]
        active_session = self._conn.execute(
            "SELECT use_id FROM use_session WHERE node_id = ? AND state = ?",
            (node_id, SessionState.ACTIVE.value),
        ).fetchone()
        if active_session is None:
            raise _RentalClosed(f"기기 {node_id} 에 연결할 점유 세션이 없다")
        return ExecutionKind.RENTAL_REOPERATION, active_session["use_id"]

    def _live_session(self, internal_request_id: str) -> sqlite3.Row | None:
        """이 요청의 살아 있는 점유 세션이다. 취소한(`CLOSED`·`cancelled`) 세션은 뺀다.

        `use_session.internal_request_id`에는 유일 제약이 없다. 오류 뒤 취소한 요청을 재판정하면(#76)
        취소한 세션과 새 세션이 함께 남는다. 요청 ID로 세션을 찾는 곳은 새 세션을 골라야 한다.
        """
        return self._conn.execute(
            "SELECT use_id, state FROM use_session WHERE internal_request_id = ? "
            "AND (state != ? OR end_reason IS NOT ?)",
            (internal_request_id, SessionState.CLOSED.value, EndReason.CANCELLED.value),
        ).fetchone()

    def _live_usage(self, internal_request_id: str) -> sqlite3.Row | None:
        """이 요청의 살아 있는 승인 사용분이다. 취소해서 반환한(`released`) 사용분은 뺀다."""
        return self._conn.execute(
            "SELECT usage_id, state FROM approval_usage WHERE internal_request_id = ? AND state != ?",
            (internal_request_id, ApprovalUsageState.RELEASED.value),
        ).fetchone()

    def _refuse_closed_rental(
        self, internal_request_id: str, node_id: str, action: Action, reserved_at: str
    ) -> StoreResult:
        """재조작 판정 뒤 연결 전에 반납이 대여를 닫았다. 연결을 거부하고 `NOT_SENT` 명령을 남긴다 (FR-19).

        사용자의 정상 반납이 겹친 경우라 예외를 내지 않는다. 명령 행이 있어야 같은 요청의 재호출,
        복원, 대시보드가 같은 결과를 본다. 붙일 점유 세션이 없으므로 `use_id`는 비운다. 이번 요청의
        `reserved` 사용분은 같은 트랜잭션에서 반환한다. 남기면 승인 횟수가 묶인다. 승인이 없던
        재조작은 반환할 사용분도 감사도 없다. 같은 요청의 재호출은 `link_command`의 기존 명령 경로가 받는다.
        """
        command_id = new_id()
        usage_row = self._live_usage(internal_request_id)
        self._conn.execute(
            "INSERT INTO node_command (command_id, internal_request_id, node_id, action, status, "
            "execution_kind, use_id, usage_id, reserved_at, send_started_at, send_attempted, "
            "outcome, occurred_at, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, 0, ?, ?, ?)",
            (
                command_id, internal_request_id, node_id, action.value, ExecutionStatus.NOT_SENT.value,
                ExecutionKind.RENTAL_REOPERATION.value, usage_row["usage_id"] if usage_row else None,
                reserved_at, CommandOutcome.NOT_SENT.value, iso(datetime.now(timezone.utc)),
                to_json({"source": "link_command", "reason": ReasonCode.USE_NOT_FOUND.value}),
            ),
        )
        if usage_row is not None and usage_row["state"] == ApprovalUsageState.RESERVED.value:
            self._release_reservation(internal_request_id)
            self._append_audit(
                internal_request_id, command_id,
                {
                    "event": "cancel_not_sent", "reason": ReasonCode.USE_NOT_FOUND.value,
                    "evidence": {"source": "link_command", "cause": "rental_closed"},
                },
            )
        return StoreResult(
            outcome=StoreOutcome.CONFLICT, command=self._reload_command(command_id),
            conflict_reason=StoreConflictReason.STATE_CONFLICT,
        )

    def _already_cancelled(self, internal_request_id: str) -> bool:
        """`cancel_not_sent`가 명령 연결 전에 이 요청을 이미 취소했는지 본다 (spec §6 결정).

        살아 있는 세션이나 살아 있는 사용분이 있으면 취소된 요청이 아니다. 오류 뒤 취소한 요청을
        재판정하면(#76) 취소한 행과 새 예약이 함께 있기 때문이다. 살아 있는 행이 없을 때만 아래를 본다.

        최초 대여는 점유 세션이 `CLOSED`·`cancelled`로 남는다. 재조작·공용형은 점유 세션이
        없으므로 승인 사용분이 `released`인지로 본다. 승인이 없던 요청(`open`+공용형)은
        취소해도 아무 행도 남지 않아 판단할 근거가 없다 — 그 경우는 보호할 상태도 없었으므로
        뒤늦은 연결을 막지 않아도 안전하다.
        """
        if self._live_session(internal_request_id) is not None or self._live_usage(internal_request_id) is not None:
            return False
        session_row = self._conn.execute(
            "SELECT state, end_reason FROM use_session WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        if session_row is not None:
            return (
                session_row["state"] == SessionState.CLOSED.value
                and session_row["end_reason"] == EndReason.CANCELLED.value
            )
        usage_row = self._conn.execute(
            "SELECT state FROM approval_usage WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        return usage_row is not None and usage_row["state"] == ApprovalUsageState.RELEASED.value

    # ── 전송 시작·미전송 취소 (#17 2단계) ────────────────────────────

    def begin_send(self, command_id: str) -> StoreResult:
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM node_command WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"명령 {command_id} 가 없다")
            status = ExecutionStatus(row["status"])
            if status == ExecutionStatus.IN_PROGRESS:
                return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, command=self._command_from_row(row))
            if status != ExecutionStatus.PENDING:
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, command=self._command_from_row(row),
                    conflict_reason=StoreConflictReason.STATE_CONFLICT,
                )
            node_row = self._conn.execute(
                "SELECT device_state FROM node WHERE node_id = ?", (row["node_id"],)
            ).fetchone()
            if node_row is None:
                raise LookupError(f"기기 {row['node_id']} 가 없다")
            if node_row["device_state"] in (DeviceState.CLOSING.value, DeviceState.FAULT.value):
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, command=self._command_from_row(row),
                    conflict_reason=StoreConflictReason.STATE_CONFLICT,
                )
            other_in_progress = self._conn.execute(
                "SELECT 1 FROM node_command WHERE node_id = ? AND command_id != ? AND status = ?",
                (row["node_id"], command_id, ExecutionStatus.IN_PROGRESS.value),
            ).fetchone()
            if other_in_progress is not None:
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, command=self._command_from_row(row),
                    conflict_reason=StoreConflictReason.COMMAND_IN_PROGRESS,
                )
            now = datetime.now(timezone.utc)
            self._conn.execute(
                "UPDATE node_command SET status = ?, send_started_at = ?, send_attempted = 1 "
                "WHERE command_id = ? AND status = ?",
                (ExecutionStatus.IN_PROGRESS.value, iso(now), command_id, ExecutionStatus.PENDING.value),
            )
            updated = self._conn.execute(
                "SELECT * FROM node_command WHERE command_id = ?", (command_id,)
            ).fetchone()
            return StoreResult(outcome=StoreOutcome.APPLIED, command=self._command_from_row(updated))

    def cancel_not_sent(
        self, internal_request_id: str, expected: ExecutionStatus, reason: ReasonCode,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        with immediate_transaction(self._conn):
            command_row = self._conn.execute(
                "SELECT * FROM node_command WHERE internal_request_id = ?", (internal_request_id,)
            ).fetchone()
            if command_row is not None:
                return self._cancel_linked(command_row, expected, reason, evidence)
            return self._cancel_unlinked(internal_request_id, expected, reason, evidence)

    def _cancel_linked(
        self, command_row: sqlite3.Row, expected: ExecutionStatus, reason: ReasonCode,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        status = ExecutionStatus(command_row["status"])
        if status == ExecutionStatus.NOT_SENT:
            if _evidence_matches(command_row["evidence"], evidence):
                return StoreResult(
                    outcome=StoreOutcome.ALREADY_APPLIED,
                    command=replace(self._command_from_row(command_row), evidence=dict(evidence)),
                )
            return StoreResult(
                outcome=StoreOutcome.CONFLICT, command=self._command_from_row(command_row),
                conflict_reason=StoreConflictReason.CONTENT_CONFLICT,
            )
        if status != expected:
            return StoreResult(
                outcome=StoreOutcome.CONFLICT, command=self._command_from_row(command_row),
                conflict_reason=StoreConflictReason.STATE_CONFLICT,
            )
        now = datetime.now(timezone.utc)
        self._conn.execute(
            "UPDATE node_command SET status = ?, outcome = ?, occurred_at = ?, evidence = ? "
            "WHERE command_id = ? AND status = ?",
            (
                ExecutionStatus.NOT_SENT.value, CommandOutcome.NOT_SENT.value, iso(now), to_json(evidence),
                command_row["command_id"], status.value,
            ),
        )
        self._release_reservation(command_row["internal_request_id"])
        self._append_audit(
            command_row["internal_request_id"], command_row["command_id"],
            {"event": "cancel_not_sent", "reason": reason.value, "evidence": dict(evidence)},
        )
        updated = self._conn.execute(
            "SELECT * FROM node_command WHERE command_id = ?", (command_row["command_id"],)
        ).fetchone()
        return StoreResult(
            outcome=StoreOutcome.APPLIED,
            command=replace(self._command_from_row(updated), evidence=dict(evidence)),
        )

    def _cancel_unlinked(
        self, internal_request_id: str, expected: ExecutionStatus, reason: ReasonCode,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        """`link_command` 전에 멈춘 예약의 취소다 (spec §6 결정, `specs/state-management/core-restore.md`).

        명령 행이 없으므로 `StoreResult.command`는 `None`이다. 예약(점유 세션)·승인 사용분
        정리만 반영한다. 이후 `link_command`가 같은 요청으로 오면 `CONFLICT`다 (6절 결정).
        같은 요청에 취소한 행과 새 예약이 함께 있으면(#76 재판정) 살아 있는 행만 본다.
        """
        if expected != ExecutionStatus.PENDING:
            return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
        own_session = self._live_session(internal_request_id)
        already_cancelled = (
            own_session is not None and own_session["state"] == SessionState.CLOSED.value
        ) or (
            own_session is None and self._already_cancelled(internal_request_id)
        )
        if already_cancelled:
            return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, command=None)
        if own_session is not None and own_session["state"] != SessionState.RESERVED.value:
            return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
        self._release_reservation(internal_request_id)
        self._append_audit(
            internal_request_id, None,
            {"event": "cancel_not_sent", "reason": reason.value, "evidence": dict(evidence)},
        )
        return StoreResult(outcome=StoreOutcome.APPLIED, command=None)

    def _release_reservation(self, internal_request_id: str) -> None:
        """최초 대여의 예약을 풀고, 이번 요청의 승인 사용분을 반환한다 (FR-19, #35).

        재조작은 점유 세션이 이 `internal_request_id`로 연결돼 있지 않으므로 세션을
        건드리지 않는다. 기존 대여의 사용분도 바꾸지 않는다 — 조회 키가 이번 요청뿐이다.
        """
        now = datetime.now(timezone.utc)
        own_session = self._conn.execute(
            "SELECT use_id, node_id FROM use_session WHERE internal_request_id = ? AND state = ?",
            (internal_request_id, SessionState.RESERVED.value),
        ).fetchone()
        if own_session is not None:
            self._conn.execute(
                "UPDATE use_session SET state = ?, end_reason = ?, closed_at = ? "
                "WHERE use_id = ?",
                (SessionState.CLOSED.value, EndReason.CANCELLED.value, iso(now), own_session["use_id"]),
            )
            self._conn.execute(
                "UPDATE node SET device_state = ? WHERE node_id = ?",
                (DeviceState.AVAILABLE.value, own_session["node_id"]),
            )
        self._conn.execute(
            "UPDATE approval_usage SET state = ? "
            "WHERE internal_request_id = ? AND state = ?",
            (ApprovalUsageState.RELEASED.value, internal_request_id, ApprovalUsageState.RESERVED.value),
        )

    def _append_audit(
        self, internal_request_id: str | None, command_id: str | None, payload: Mapping[str, Any],
        *, step: Step = Step.EXEC,
    ) -> None:
        """`ExecutionStore` 안에서 쓰는 감사 행의 `event_id`는 DB가 만든다 (`architecture.md` §4)."""
        self._conn.execute(
            "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
            "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(), new_id(), internal_request_id, command_id, step.value,
                to_json(payload), iso(datetime.now(timezone.utc)),
            ),
        )

    # ── 이상 격리 (#17 4단계) ────────────────────────────────────────

    def isolate_device(self, node_id: str, evidence: Mapping[str, Any]) -> StoreResult:
        """명령 결과가 아닌 이상으로 기기를 `FAULT`로 둔다 (NFR-04).

        `FR-19`의 "이상을 감지하면 어느 상태에서든 기기는 FAULT가 된다"를 따른다 — `CLOSING`
        등 다른 상태에서도 전이를 막지 않는다. 점유 세션·승인 사용분·명령은 건드리지 않는다.
        이미 `FAULT`면 `ALREADY_APPLIED`다. 그때도 새 근거는 감사 기록에 남긴다.
        """
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT device_state FROM node WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"기기 {node_id} 가 없다")
            already_fault = row["device_state"] == DeviceState.FAULT.value
            if not already_fault:
                self._conn.execute(
                    "UPDATE node SET device_state = ? WHERE node_id = ?",
                    (DeviceState.FAULT.value, node_id),
                )
            self._append_audit(
                None, None, {"event": "isolate_device", "node_id": node_id, "evidence": dict(evidence)},
                step=Step.FAULT,
            )
            outcome = StoreOutcome.ALREADY_APPLIED if already_fault else StoreOutcome.APPLIED
            return StoreResult(outcome=outcome)

    # ── 반납 시작·종료 (#17 5단계) ───────────────────────────────────

    def begin_close(self, use_id: str, reason: EndReason) -> StoreResult:
        """대여형 기기·세션을 `ACTIVE` → `CLOSING`으로 둔다. 키: `use_id`.

        같은 기기의 결과를 확정하지 못한 구동 명령이 있으면 `CONFLICT`·`COMMAND_IN_PROGRESS`다
        (FR-19 "구동 중 추가 명령 제한의 저장"). 전송 시작(`begin_send`)과 반납 시작의 검사·쓰기는
        같은 `node_command` 조건을 보는 한 트랜잭션이라 서로 경합하면 한쪔만 성공한다.
        """
        with immediate_transaction(self._conn):
            row = self._conn.execute("SELECT * FROM use_session WHERE use_id = ?", (use_id,)).fetchone()
            if row is None:
                raise LookupError(f"점유 세션 {use_id} 가 없다")
            state = SessionState(row["state"])
            if state == SessionState.CLOSING:
                if row["end_reason"] == reason.value:
                    return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, session=self._session_from_row(row))
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, session=self._session_from_row(row),
                    conflict_reason=StoreConflictReason.CONTENT_CONFLICT,
                )
            if state != SessionState.ACTIVE:
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, session=self._session_from_row(row),
                    conflict_reason=StoreConflictReason.STATE_CONFLICT,
                )
            node_row = self._conn.execute(
                "SELECT device_state FROM node WHERE node_id = ?", (row["node_id"],)
            ).fetchone()
            if node_row is None:
                raise LookupError(f"기기 {row['node_id']} 가 없다")
            if node_row["device_state"] != DeviceState.ACTIVE.value:
                return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
            in_progress = self._conn.execute(
                "SELECT 1 FROM node_command WHERE node_id = ? AND status = ?",
                (row["node_id"], ExecutionStatus.IN_PROGRESS.value),
            ).fetchone()
            if in_progress is not None:
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.COMMAND_IN_PROGRESS
                )
            self._conn.execute(
                "UPDATE use_session SET state = ?, end_reason = ? WHERE use_id = ?",
                (SessionState.CLOSING.value, reason.value, use_id),
            )
            self._conn.execute(
                "UPDATE node SET device_state = ? WHERE node_id = ?",
                (DeviceState.CLOSING.value, row["node_id"]),
            )
            updated = self._conn.execute("SELECT * FROM use_session WHERE use_id = ?", (use_id,)).fetchone()
            return StoreResult(outcome=StoreOutcome.APPLIED, session=self._session_from_row(updated))

    def finish_close(self, use_id: str, closed_at: datetime, end_reason: EndReason) -> StoreResult:
        """세션 `CLOSED`, 기기 `AVAILABLE`, `closed_at`, 종료 사유와 감사를 한 트랜잭션에서 저장한다.

        같은 `use_id`·같은 종료 사유의 재호출은 `ALREADY_APPLIED`이고 처음 저장한 종료 시각을 유지한다.
        다른 종료 사유는 `CONFLICT`다. 같은 기기의 결과를 확정하지 못한 구동 명령이 있으면
        `CONFLICT`·`COMMAND_IN_PROGRESS`다. 검사 ID와 센서 근거를 받지 않는다 (FR-20).
        """
        with immediate_transaction(self._conn):
            row = self._conn.execute("SELECT * FROM use_session WHERE use_id = ?", (use_id,)).fetchone()
            if row is None:
                raise LookupError(f"점유 세션 {use_id} 가 없다")
            state = SessionState(row["state"])
            if state == SessionState.CLOSED:
                if row["end_reason"] == end_reason.value:
                    return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, session=self._session_from_row(row))
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, session=self._session_from_row(row),
                    conflict_reason=StoreConflictReason.CONTENT_CONFLICT,
                )
            if state != SessionState.CLOSING:
                return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
            if row["end_reason"] != end_reason.value:
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, session=self._session_from_row(row),
                    conflict_reason=StoreConflictReason.CONTENT_CONFLICT,
                )
            node_row = self._conn.execute(
                "SELECT device_state FROM node WHERE node_id = ?", (row["node_id"],)
            ).fetchone()
            if node_row is None:
                raise LookupError(f"기기 {row['node_id']} 가 없다")
            if node_row["device_state"] != DeviceState.CLOSING.value:
                return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
            in_progress = self._conn.execute(
                "SELECT 1 FROM node_command WHERE node_id = ? AND status = ?",
                (row["node_id"], ExecutionStatus.IN_PROGRESS.value),
            ).fetchone()
            if in_progress is not None:
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.COMMAND_IN_PROGRESS
                )
            self._conn.execute(
                "UPDATE use_session SET state = ?, closed_at = ? WHERE use_id = ?",
                (SessionState.CLOSED.value, iso(closed_at), use_id),
            )
            self._conn.execute(
                "UPDATE node SET device_state = ? WHERE node_id = ?",
                (DeviceState.AVAILABLE.value, row["node_id"]),
            )
            self._append_audit(
                row["internal_request_id"], None,
                {"event": "finish_close", "use_id": use_id, "end_reason": end_reason.value},
                step=Step.CLOSE,
            )
            updated = self._conn.execute("SELECT * FROM use_session WHERE use_id = ?", (use_id,)).fetchone()
            return StoreResult(outcome=StoreOutcome.APPLIED, session=self._session_from_row(updated))

    def close_result(self, use_id: str) -> CloseResult | None:
        """세션의 저장된 종료 기록을 돌려준다. 기기의 현재 상태가 아니다 (FR-20).

        `CLOSED`가 아니면 `None`이다. 반납 완료인지는 호출자가 `end_reason`으로 확인한다.
        """
        row = self._conn.execute(
            "SELECT state, closed_at, end_reason FROM use_session WHERE use_id = ?", (use_id,),
        ).fetchone()
        if row is None or row["state"] != SessionState.CLOSED.value or row["closed_at"] is None:
            return None
        return CloseResult(
            use_id=use_id, closed_at=parse_iso(row["closed_at"]), end_reason=EndReason(row["end_reason"]),
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> UseSession:
        return UseSession(
            use_id=row["use_id"],
            internal_request_id=row["internal_request_id"],
            node_id=row["node_id"],
            state=SessionState(row["state"]),
            rental_due_at=parse_iso(row["rental_due_at"]) if row["rental_due_at"] else None,
            closed_at=parse_iso(row["closed_at"]) if row["closed_at"] else None,
            end_reason=EndReason(row["end_reason"]) if row["end_reason"] else None,
            release_policy=from_json(row["release_policy"]),
        )

    # ── 실행 결과 (#17 3단계) ────────────────────────────────────────

    def record_result(
        self, command_id: str, outcome: CommandOutcome, occurred_at: datetime,
        rental_due_at: datetime | None, evidence: Mapping[str, Any],
    ) -> StoreResult:
        if outcome == CommandOutcome.NOT_SENT:
            raise ValueError("NOT_SENT 는 cancel_not_sent 로 반영한다")
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM node_command WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"명령 {command_id} 가 없다")
            if ExecutionStatus(row["status"]) != ExecutionStatus.IN_PROGRESS:
                return self._record_result_not_in_progress(row, outcome, occurred_at, evidence)
            if outcome == CommandOutcome.SUCCEEDED:
                return self._apply_succeeded(row, occurred_at, rental_due_at, evidence)
            if outcome == CommandOutcome.REJECTED:
                return self._apply_rejected(row, occurred_at, evidence)
            return self._apply_failed_or_unknown(row, outcome, occurred_at, evidence)

    def _record_result_not_in_progress(
        self, row: sqlite3.Row, outcome: CommandOutcome, occurred_at: datetime, evidence: Mapping[str, Any],
    ) -> StoreResult:
        """전송 전(`PENDING`)이거나 이미 결과가 있는 명령이다. 같은 결과의 재호출만 `ALREADY_APPLIED`다.

        다른 결과는 `CONFLICT`다. 늦은 성공도 기존 결과(`FAILED`·`UNKNOWN` 등)를 덮어쓰지
        않는다 — 늦은 성공으로 `FAULT`를 해제하지 않는다 (FR-07).
        """
        if (
            row["outcome"] == outcome.value and row["occurred_at"] == iso(occurred_at)
            and _evidence_matches(row["evidence"], evidence)
        ):
            return StoreResult(
                outcome=StoreOutcome.ALREADY_APPLIED,
                command=replace(self._command_from_row(row), evidence=dict(evidence)),
            )
        return StoreResult(
            outcome=StoreOutcome.CONFLICT, command=self._command_from_row(row),
            conflict_reason=StoreConflictReason.CONTENT_CONFLICT,
        )

    def _apply_succeeded(
        self, row: sqlite3.Row, occurred_at: datetime, rental_due_at: datetime | None,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        """최초 대여는 기기·세션을 `ACTIVE`로 두고 `rental_due_at`을 저장한다 (FR-19).

        재조작·공용형은 기존 점유를 보존한다 — 건드릴 세션이 없거나 이미 `ACTIVE`다.
        """
        self._set_result(row, CommandOutcome.SUCCEEDED, occurred_at, evidence)
        if row["execution_kind"] == ExecutionKind.RENTAL_INITIAL.value:
            self._conn.execute(
                "UPDATE use_session SET state = ?, rental_due_at = ? WHERE use_id = ?",
                (SessionState.ACTIVE.value, iso(rental_due_at) if rental_due_at else None, row["use_id"]),
            )
            self._conn.execute(
                "UPDATE node SET device_state = ? WHERE node_id = ?",
                (DeviceState.ACTIVE.value, row["node_id"]),
            )
        self._consume_usage(row["usage_id"])
        self._append_audit(
            row["internal_request_id"], row["command_id"],
            {"event": "record_result", "outcome": CommandOutcome.SUCCEEDED.value, "evidence": dict(evidence)},
        )
        return StoreResult(
            outcome=StoreOutcome.APPLIED,
            command=replace(self._reload_command(row["command_id"]), evidence=dict(evidence)),
        )

    def _apply_rejected(
        self, row: sqlite3.Row, occurred_at: datetime, evidence: Mapping[str, Any],
    ) -> StoreResult:
        """검증된 `DEVICE_BUSY` 접수·실행 전 거부다. 기기를 `FAULT`로 두지 않는다 (FR-19).

        최초 대여는 이번 예약만 취소한다. 재조작·공용형은 기존 점유를 보존하고 이번
        요청의 사용분만 돌려준다.
        """
        self._set_result(row, CommandOutcome.REJECTED, occurred_at, evidence)
        if row["execution_kind"] == ExecutionKind.RENTAL_INITIAL.value:
            self._conn.execute(
                "UPDATE use_session SET state = ?, end_reason = ?, closed_at = ? WHERE use_id = ?",
                (SessionState.CLOSED.value, EndReason.CANCELLED.value, iso(occurred_at), row["use_id"]),
            )
            self._conn.execute(
                "UPDATE node SET device_state = ? WHERE node_id = ?",
                (DeviceState.AVAILABLE.value, row["node_id"]),
            )
        self._release_usage(row["usage_id"])
        self._append_audit(
            row["internal_request_id"], row["command_id"],
            {"event": "record_result", "outcome": CommandOutcome.REJECTED.value, "evidence": dict(evidence)},
        )
        return StoreResult(
            outcome=StoreOutcome.APPLIED,
            command=replace(self._reload_command(row["command_id"]), evidence=dict(evidence)),
        )

    def _apply_failed_or_unknown(
        self, row: sqlite3.Row, outcome: CommandOutcome, occurred_at: datetime, evidence: Mapping[str, Any],
    ) -> StoreResult:
        """기기를 `FAULT`로 격리한다. 점유 세션은 어느 실행 종류든 그대로 보존한다 (FR-07, NFR-04)."""
        self._set_result(row, outcome, occurred_at, evidence)
        self._conn.execute(
            "UPDATE node SET device_state = ? WHERE node_id = ?", (DeviceState.FAULT.value, row["node_id"])
        )
        self._hold_usage(row["usage_id"])
        self._append_audit(
            row["internal_request_id"], row["command_id"],
            {"event": "record_result", "outcome": outcome.value, "evidence": dict(evidence)},
        )
        return StoreResult(
            outcome=StoreOutcome.APPLIED,
            command=replace(self._reload_command(row["command_id"]), evidence=dict(evidence)),
        )

    def _set_result(
        self, row: sqlite3.Row, outcome: CommandOutcome, occurred_at: datetime, evidence: Mapping[str, Any],
    ) -> None:
        self._conn.execute(
            "UPDATE node_command SET status = ?, outcome = ?, occurred_at = ?, evidence = ? "
            "WHERE command_id = ? AND status = ?",
            (
                outcome.value, outcome.value, iso(occurred_at), to_json(evidence),
                row["command_id"], ExecutionStatus.IN_PROGRESS.value,
            ),
        )

    def _consume_usage(self, usage_id: str | None) -> None:
        if usage_id is None:
            return
        self._conn.execute(
            "UPDATE approval_usage SET state = ? WHERE usage_id = ? AND state = ?",
            (ApprovalUsageState.CONSUMED.value, usage_id, ApprovalUsageState.RESERVED.value),
        )

    def _release_usage(self, usage_id: str | None) -> None:
        if usage_id is None:
            return
        self._conn.execute(
            "UPDATE approval_usage SET state = ? WHERE usage_id = ? AND state = ?",
            (ApprovalUsageState.RELEASED.value, usage_id, ApprovalUsageState.RESERVED.value),
        )

    def _hold_usage(self, usage_id: str | None) -> None:
        if usage_id is None:
            return
        self._conn.execute(
            "UPDATE approval_usage SET state = ? WHERE usage_id = ? AND state = ?",
            (ApprovalUsageState.HELD.value, usage_id, ApprovalUsageState.RESERVED.value),
        )

    def _reload_command(self, command_id: str) -> CommandRecord:
        row = self._conn.execute(
            "SELECT * FROM node_command WHERE command_id = ?", (command_id,)
        ).fetchone()
        return self._command_from_row(row)

    def execution(self, internal_request_id: str) -> CommandRecord | None:
        row = self._conn.execute(
            "SELECT * FROM node_command WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        return None if row is None else self._command_from_row(row)

    def last_succeeded(self, node_id: str) -> CommandRecord | None:
        """기기에서 가장 늦게 전송을 시작한 `succeeded` 명령. 공용형의 마지막 제어 상태다 (FR-20).

        전송 시작 시각은 서버 시계다. 기기 시각인 `occurred_at`으로 순서를 정하지 않는다.
        """
        row = self._conn.execute(
            "SELECT * FROM node_command WHERE node_id = ? AND status = ? "
            "ORDER BY send_started_at DESC, rowid DESC LIMIT 1",
            (node_id, ExecutionStatus.SUCCEEDED.value),
        ).fetchone()
        return None if row is None else self._command_from_row(row)

    def execution_context(self, command_id: str) -> ExecutionContext | None:
        """명령 ID로 판정이 인계한 원래 실행 문맥을 조회한다 (#83).

        저장한 설정 스냅샷(`request.settings_json`)으로 문맥을 만든다. 현재 설정으로 채우지 않는다.
        """
        try:
            command_row = self._conn.execute(
                "SELECT internal_request_id, node_id, use_id, usage_id, execution_kind, reserved_at, light, "
                "speaker FROM node_command WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if command_row is None:
                return None
            request_row = self._conn.execute(
                "SELECT * FROM request WHERE internal_request_id = ?",
                (command_row["internal_request_id"],),
            ).fetchone()
        except sqlite3.Error as error:
            raise StoreError(str(error)) from error
        if request_row is None:
            raise LookupError(f"요청 {command_row['internal_request_id']} 가 없다")
        is_internal = request_row["request_kind"] == "server_internal"
        execution_kind = ExecutionKind(command_row["execution_kind"])
        # 판정이 넘긴 문맥과 같게, 예약이 없는 요청(서버 내부, 승인 없는 공용형)은 예약 시각을 비운다
        # (core-execution.md "reserved_at 이 None 인 요청"). 명령 행에는 받은 시각이 남는다.
        no_reservation = is_internal or (
            execution_kind == ExecutionKind.SHARED and command_row["usage_id"] is None)
        return ExecutionContext(
            internal_request_id=command_row["internal_request_id"],
            request=None if is_internal else self._use_request_from_row(request_row),
            internal_request=(
                self._internal_request_from_row(request_row, command_row["internal_request_id"])
                if is_internal else None
            ),
            node_id=command_row["node_id"],
            use_id=command_row["use_id"],
            usage_id=command_row["usage_id"],
            settings=settings_from_json(request_row["settings_json"]),
            execution_kind=execution_kind,
            reserved_at=(
                parse_iso(command_row["reserved_at"])
                if command_row["reserved_at"] and not no_reservation else None
            ),
            light=light_from_json(command_row["light"]),
            speaker=speaker_from_json(command_row["speaker"]),
        )

    @staticmethod
    def _internal_request_from_row(row: sqlite3.Row, internal_request_id: str) -> InternalRequest:
        return InternalRequest(
            internal_request_id=internal_request_id, node_id=row["node_id"],
            action=Action(row["action"]), cause=InternalRequestCause(row["internal_cause"]),
            settings=settings_from_json(row["settings_json"]), created_at=parse_iso(row["received_at"]),
        )

    @staticmethod
    def _command_from_row(row: sqlite3.Row) -> CommandRecord:
        return CommandRecord(
            command_id=row["command_id"],
            internal_request_id=row["internal_request_id"],
            node_id=row["node_id"],
            action=Action(row["action"]),
            status=ExecutionStatus(row["status"]),
            execution_kind=ExecutionKind(row["execution_kind"]),
            use_id=row["use_id"],
            usage_id=row["usage_id"],
            reserved_at=parse_iso(row["reserved_at"]) if row["reserved_at"] else None,
            send_started_at=parse_iso(row["send_started_at"]) if row["send_started_at"] else None,
            send_attempted=bool(row["send_attempted"]),
            outcome=CommandOutcome(row["outcome"]) if row["outcome"] else None,
            occurred_at=parse_iso(row["occurred_at"]) if row["occurred_at"] else None,
            evidence=from_json(row["evidence"]),
            light=light_from_json(row["light"]),
            speaker=speaker_from_json(row["speaker"]),
        )

    # ── 복원 조회 (#17 6단계) ────────────────────────────────────────

    def restore_pending(self) -> Sequence[RestoreBundle]:
        """미완료 요청·명령·세션·사용분·복구 건과 `FAULT` 기기를 일관된 읽기 시점에 조회한다.

        조회 오류는 예외로 그대로 올라간다 — 빈 목록으로 바꾸지 않는다 (FR-19 재시작 복원).
        """
        with deferred_transaction(self._conn):
            read_at = datetime.now(timezone.utc)
            commands_by_node = self._pending_commands_by_node()
            linked_ids = {
                row["internal_request_id"]
                for row in self._conn.execute("SELECT internal_request_id FROM node_command")
            }
            requests_by_node = self._pending_requests_by_node(linked_ids)
            sessions_by_node = self._restored_sessions_by_node()
            usages_by_node = self._pending_usages_by_node()
            recoveries_by_node = self._unresolved_recoveries_by_node()
            fault_node_ids = {
                row["node_id"] for row in self._conn.execute(
                    "SELECT node_id FROM node WHERE device_state = ?", (DeviceState.FAULT.value,)
                )
            }
            node_ids = (
                set(commands_by_node) | set(requests_by_node) | set(sessions_by_node)
                | set(usages_by_node) | set(recoveries_by_node) | fault_node_ids
            )
            bundles = []
            for node_id in node_ids:
                node_row = self._conn.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
                if node_row is None:
                    continue
                bundles.append(RestoreBundle(
                    node=self._node_from_row(node_row), read_at=read_at,
                    requests=tuple(requests_by_node.get(node_id, ())),
                    commands=tuple(commands_by_node.get(node_id, ())),
                    sessions=tuple(sessions_by_node.get(node_id, ())),
                    approval_usages=tuple(usages_by_node.get(node_id, ())),
                    recoveries=tuple(recoveries_by_node.get(node_id, ())),
                ))
            return tuple(bundles)

    def _pending_commands_by_node(self) -> dict[str, list[CommandRecord]]:
        """전송 전(`PENDING`)이거나 결과를 확정하지 못한(`IN_PROGRESS`) 명령이다."""
        by_node: dict[str, list[CommandRecord]] = defaultdict(list)
        for row in self._conn.execute(
            "SELECT * FROM node_command WHERE status IN (?, ?)",
            (ExecutionStatus.PENDING.value, ExecutionStatus.IN_PROGRESS.value),
        ):
            by_node[row["node_id"]].append(self._command_from_row(row))
        return by_node

    def _pending_requests_by_node(self, linked_ids: set[str]) -> dict[str, list[PendingRequestRecord]]:
        """`link_command` 전에 멈춘, **예약이 실제로 남아있는** 요청이다 (6절 결정).

        판정 값으로 거르지 않는다. 판정이 `OK`가 아니거나(오류로 끝난 `INTERNAL_ERROR` 등) 비어
        있어도(`NULL`) 예약이 남았으면 넣는다 — 오류 뒤 바로 취소(#80)가 실패한 요청이다. `open`
        정책의 공용형 성공은 아무것도 쓰지 않는다(#27) — 그런 요청은 세션도 사용분도 없어
        복원할 예약이 없다. 점유 세션이 `RESERVED`이거나 승인 사용분이 `reserved`인 경우만
        넣는다. 이미 `cancel_not_sent`로 취소된 요청도 이 조건에서 자연히 빠진다(세션은
        `CLOSED`, 사용분은 `released`로 바뀌어 있다) — `_already_cancelled`를 또 부르지
        않아도 된다.

        오류 판정은 `request.node_id`를 비운 채 저장된다. 대상 기기는 `request.node_id`, 남은
        세션의 `node_id`, 남은 사용분의 승인 `node_id` 순서로 찾는다. 기기 행이 없으면 넣지 않는다.
        `decision`은 저장된 판정이고, 판정이 비었으면 `None`이다.
        """
        by_node: dict[str, list[PendingRequestRecord]] = defaultdict(list)
        for row in self._conn.execute(
            "SELECT * FROM request WHERE internal_request_id IN ("
            "SELECT internal_request_id FROM use_session WHERE state = ? "
            "UNION SELECT internal_request_id FROM approval_usage WHERE state = ?)",
            (SessionState.RESERVED.value, ApprovalUsageState.RESERVED.value),
        ):
            internal_request_id = row["internal_request_id"]
            if internal_request_id in linked_ids:
                continue
            session_row = self._conn.execute(
                "SELECT node_id FROM use_session WHERE internal_request_id = ? AND state = ?",
                (internal_request_id, SessionState.RESERVED.value),
            ).fetchone()
            usage_row = self._conn.execute(
                "SELECT au.usage_id, a.node_id FROM approval_usage au "
                "JOIN approval a ON a.approval_id = au.approval_id "
                "WHERE au.internal_request_id = ? AND au.state = ?",
                (internal_request_id, ApprovalUsageState.RESERVED.value),
            ).fetchone()
            node_id = (
                row["node_id"]
                or (session_row["node_id"] if session_row else None)
                or (usage_row["node_id"] if usage_row else None)
            )
            node_row = self._conn.execute(
                "SELECT occupancy FROM node WHERE node_id = ?", (node_id,)
            ).fetchone()
            if node_row is None:
                continue
            try:
                execution_kind, use_id = self._resolve_execution_kind(
                    internal_request_id, node_id, node_row["occupancy"]
                )
            except LookupError:
                # 재조작 사용분만 남았는데 대여가 이미 닫혔다. 이 요청만 뺀다. 남은 사용분이 그 기기만
                # 막는다(`unlinked_approval_usage`). 다른 기기의 복원까지 막지 않는다.
                continue
            by_node[node_id].append(PendingRequestRecord(
                internal_request_id=internal_request_id,
                request=self._use_request_from_row(row),
                input_digest=row["input_digest"],
                execution_kind=execution_kind,
                use_id=use_id,
                usage_id=usage_row["usage_id"] if usage_row else None,
                reserved_at=parse_iso(row["received_at"]),
                decision=self._decision_from_row(row, use_id),
            ))
        return by_node

    def _restored_sessions_by_node(self) -> dict[str, list[RestoredSession]]:
        """`RESERVED`·`ACTIVE`·`CLOSING` 점유 세션이다. 생성 시각은 원래 요청의 수신 시각이다."""
        by_node: dict[str, list[RestoredSession]] = defaultdict(list)
        for row in self._conn.execute(
            "SELECT * FROM use_session WHERE state IN (?, ?, ?)",
            (SessionState.RESERVED.value, SessionState.ACTIVE.value, SessionState.CLOSING.value),
        ):
            request_row = self._conn.execute(
                "SELECT received_at FROM request WHERE internal_request_id = ?",
                (row["internal_request_id"],),
            ).fetchone()
            if request_row is None:
                raise LookupError(f"점유 세션 {row['use_id']} 의 원래 요청을 확인하지 못했다")
            by_node[row["node_id"]].append(RestoredSession(
                session=self._session_from_row(row), user_id=row["user_id"],
                created_at=parse_iso(request_row["received_at"]),
            ))
        return by_node

    def _pending_usages_by_node(self) -> dict[str, list[ApprovalUsageRecord]]:
        """예약(`reserved`)·보류(`held`) 상태의 승인 사용분이다. 점유 세션이 없는 공용형도 포함한다."""
        by_node: dict[str, list[ApprovalUsageRecord]] = defaultdict(list)
        for row in self._conn.execute(
            "SELECT au.usage_id, au.approval_id, au.internal_request_id, au.command_id, au.state, "
            "a.node_id FROM approval_usage au JOIN approval a ON a.approval_id = au.approval_id "
            "WHERE au.state IN (?, ?)",
            (ApprovalUsageState.RESERVED.value, ApprovalUsageState.HELD.value),
        ):
            by_node[row["node_id"]].append(ApprovalUsageRecord(
                usage_id=row["usage_id"], approval_id=row["approval_id"],
                internal_request_id=row["internal_request_id"], command_id=row["command_id"],
                state=ApprovalUsageState(row["state"]),
            ))
        return by_node

    def _unresolved_recoveries_by_node(self) -> dict[str, list[RecoveryRecord]]:
        """완료하지 못한(`completed_at IS NULL`) 복구 건이다. 완료 결과는 항상 `None`이다."""
        by_node: dict[str, list[RecoveryRecord]] = defaultdict(list)
        for row in self._conn.execute("SELECT * FROM recovery WHERE completed_at IS NULL"):
            by_node[row["node_id"]].append(self._recovery_from_row(row))
        return by_node

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> NodeInfo:
        return NodeInfo(
            node_id=row["node_id"],
            place_id=row["place_id"],
            capability=Capability(row["capability"]),
            occupancy=Occupancy(row["occupancy"]),
            trust_state=TrustState(row["trust_state"]),
            device_state=DeviceState(row["device_state"]),
            exclusive_group=row["exclusive_group"],
            last_seen_at=parse_iso(row["last_seen_at"]) if row["last_seen_at"] else None,
            release_policy=from_json(row["release_policy"]),
            session_id=row["session_id"],
            session_first_seen_at=(
                parse_iso(row["session_first_seen_at"]) if row["session_first_seen_at"] else None
            ),
            light_controls=light_controls_from_json(row["light_controls"]),
            speaker_controls=speaker_controls_from_json(row["speaker_controls"]),
        )

    @staticmethod
    def _use_request_from_row(row: sqlite3.Row) -> UseRequest:
        return UseRequest(
            user_id=row["user_id"], request_id=row["request_id"],
            target=TargetSpec.parse(row["target_spec"]), action=Action(row["action"]),
            host_kind=row["host_kind"], received_at=parse_iso(row["received_at"]),
            parent_internal_request_id=row["parent_internal_request_id"],
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row, use_id: str | None) -> Decision | None:
        if row["verdict"] is None:
            return None
        return Decision(
            internal_request_id=row["internal_request_id"], request_id=row["request_id"] or "",
            verdict=Verdict(row["verdict"]), reason=ReasonCode(row["reason_code"]),
            node_id=row["node_id"], use_id=use_id, p_max=row["p_max"],
        )

    @staticmethod
    def _recovery_from_row(row: sqlite3.Row) -> RecoveryRecord:
        """`evidence` 컬럼은 `RecoveryEvidence` 필드를 그대로 담은 JSON 배열이다 (6절 결정).

        `completed_at`이 있으면 `completion`을 함께 채운다. `restore_pending`은
        `completed_at IS NULL`인 행만 읽으므로 그 호출에서는 항상 `None`이다.
        """
        evidence = SqliteExecutionStore._evidence_tuple_from_json(row["evidence"])
        completion = None
        if row["completed_at"] is not None:
            completion = RecoveryCompletion(
                settlement=SqliteExecutionStore._settlement_from_row(row),
                completed_at=parse_iso(row["completed_at"]),
            )
        return RecoveryRecord(
            recovery_id=row["recovery_id"], node_id=row["node_id"],
            internal_request_ids=tuple(json.loads(row["internal_request_ids"] or "[]")),
            command_ids=tuple(json.loads(row["command_ids"] or "[]")),
            use_id=row["use_id"], created_at=parse_iso(row["created_at"]),
            status=RecoveryStatus(row["status"]), evidence=evidence, completion=completion,
        )

    @staticmethod
    def _evidence_tuple_from_json(value: str | None) -> tuple[RecoveryEvidence, ...]:
        return tuple(
            RecoveryEvidence(
                evidence_id=item["evidence_id"], session_id=item["session_id"],
                observed_at=parse_iso(item["observed_at"]),
                execution_outcome=(
                    CommandOutcome(item["execution_outcome"]) if item.get("execution_outcome") else None
                ),
                command_idle=item.get("command_idle"),
            )
            for item in json.loads(value or "[]")
        )

    @staticmethod
    def _settlement_from_row(row: sqlite3.Row) -> RecoverySettlement:
        """`settlement_json` 컬럼 하나에 정산 내용을 담는다 (6절 결정, 담당자 지시).

        `settlement_digest`·`approved_by_user_id`·`approved_at`은 `0001_initial.sql`의
        기존 컬럼을 그대로 쓴다.
        """
        content = json.loads(row["settlement_json"])
        usage_settlements = tuple(
            ApprovalUsageSettlement(
                usage_id=item["usage_id"], expected_state=ApprovalUsageState(item["expected_state"]),
                state=ApprovalUsageState(item["state"]),
            )
            for item in content["usage_settlements"]
        )
        return RecoverySettlement(
            recovery_id=row["recovery_id"], node_id=row["node_id"],
            approved_by_user_id=row["approved_by_user_id"], approved_at=parse_iso(row["approved_at"]),
            settlement_digest=row["settlement_digest"],
            expected_node_state=DeviceState(content["expected_node_state"]),
            expected_recovery_status=RecoveryStatus(content["expected_recovery_status"]),
            expected_commands={k: ExecutionStatus(v) for k, v in content["expected_commands"].items()},
            expected_sessions={k: SessionState(v) for k, v in content["expected_sessions"].items()},
            evidence=SqliteExecutionStore._evidence_tuple_from_json(json.dumps(content["evidence"])),
            usage_settlements=usage_settlements,
            use_id=content["use_id"],
            closed_at=parse_iso(content["closed_at"]) if content["closed_at"] else None,
            end_reason=EndReason(content["end_reason"]) if content["end_reason"] else None,
        )

    @staticmethod
    def _evidence_json(evidence: Sequence[RecoveryEvidence]) -> str:
        return json.dumps([
            {
                "evidence_id": item.evidence_id, "session_id": item.session_id,
                "observed_at": iso(item.observed_at),
                "execution_outcome": item.execution_outcome.value if item.execution_outcome else None,
                "command_idle": item.command_idle,
            }
            for item in evidence
        ])

    @staticmethod
    def _settlement_to_json(settlement: RecoverySettlement) -> str:
        """`RecoverySettlement`에서 전용 컬럼(`settlement_digest`·`approved_by_user_id`·
        `approved_at`)이 아닌 나머지를 JSON TEXT 컬럼 `settlement_json` 하나로 담는다 (6절 결정)."""
        return json.dumps({
            "expected_node_state": settlement.expected_node_state.value,
            "expected_recovery_status": settlement.expected_recovery_status.value,
            "expected_commands": {k: v.value for k, v in settlement.expected_commands.items()},
            "expected_sessions": {k: v.value for k, v in settlement.expected_sessions.items()},
            "evidence": json.loads(SqliteExecutionStore._evidence_json(settlement.evidence)),
            "usage_settlements": [
                {"usage_id": u.usage_id, "expected_state": u.expected_state.value, "state": u.state.value}
                for u in settlement.usage_settlements
            ],
            "use_id": settlement.use_id,
            "closed_at": iso(settlement.closed_at) if settlement.closed_at else None,
            "end_reason": settlement.end_reason.value if settlement.end_reason else None,
        })

    # ── 복구 건과 복구 정산 (#17 7단계) ──────────────────────────────

    def save_recovery(self, record: RecoveryRecord) -> StoreResult:
        """복구 건과 근거를 저장한다. 대상 연결은 바꾸지 않고 완료 근거는 덮어쓰지 않는다 (NFR-04).

        같은 대상 연결·같은 근거·같은 상태는 `ALREADY_APPLIED`다. 대상 연결이 다르면
        `CONFLICT`다. 점유·사용분·기기 상태는 건드리지 않는다 — 근거 저장과 복구 정산은
        별개다. 완료된 복구 건은 새 근거로 덮어쓰지 않고 기존 완료 결과를 그대로 돌려준다.
        """
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM recovery WHERE recovery_id = ?", (record.recovery_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO recovery (recovery_id, node_id, use_id, internal_request_ids, "
                    "command_ids, created_at, status, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.recovery_id, record.node_id, record.use_id,
                        json.dumps(list(record.internal_request_ids)), json.dumps(list(record.command_ids)),
                        iso(record.created_at), record.status.value, self._evidence_json(record.evidence),
                    ),
                )
                updated = self._conn.execute(
                    "SELECT * FROM recovery WHERE recovery_id = ?", (record.recovery_id,)
                ).fetchone()
                return StoreResult(outcome=StoreOutcome.APPLIED, recovery=self._recovery_from_row(updated))

            stored_request_ids = tuple(json.loads(row["internal_request_ids"] or "[]"))
            stored_command_ids = tuple(json.loads(row["command_ids"] or "[]"))
            if (
                row["node_id"] != record.node_id or row["use_id"] != record.use_id
                or stored_request_ids != record.internal_request_ids
                or stored_command_ids != record.command_ids or row["created_at"] != iso(record.created_at)
            ):
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, recovery=self._recovery_from_row(row),
                    conflict_reason=StoreConflictReason.LINK_MISMATCH,
                )
            if row["completed_at"] is not None:
                return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, recovery=self._recovery_from_row(row))
            new_evidence_json = self._evidence_json(record.evidence)
            if row["evidence"] == new_evidence_json and row["status"] == record.status.value:
                return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, recovery=self._recovery_from_row(row))
            self._conn.execute(
                "UPDATE recovery SET status = ?, evidence = ? WHERE recovery_id = ?",
                (record.status.value, new_evidence_json, record.recovery_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM recovery WHERE recovery_id = ?", (record.recovery_id,)
            ).fetchone()
            return StoreResult(outcome=StoreOutcome.APPLIED, recovery=self._recovery_from_row(updated))

    def recovery(self, recovery_id: str) -> RecoveryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM recovery WHERE recovery_id = ?", (recovery_id,)
        ).fetchone()
        return None if row is None else self._recovery_from_row(row)

    def settle_recovery(self, settlement: RecoverySettlement) -> StoreResult:
        """연결과 예상 상태를 검사하고 승인·근거·사용분·점유 종료·기기 복구·감사를 한 트랜잭션에서 커밋한다.

        `finish_close`를 거치지 않는다 — 복구 근거로 점유를 종료한다. 같은
        `recovery_id`·같은 논리 정산(`settlement_digest`로 비교)은 `ALREADY_APPLIED`다.
        다른 연결·요약값·예상 상태는 `CONFLICT`다. 늦은 결과로 완료 정산을 뒤집지 않는다
        (NFR-04).
        """
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM recovery WHERE recovery_id = ?", (settlement.recovery_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"복구 건 {settlement.recovery_id} 가 없다")
            if row["completed_at"] is not None:
                if row["settlement_digest"] == settlement.settlement_digest:
                    return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, recovery=self._recovery_from_row(row))
                return StoreResult(
                    outcome=StoreOutcome.CONFLICT, recovery=self._recovery_from_row(row),
                    conflict_reason=StoreConflictReason.CONTENT_CONFLICT,
                )
            if row["node_id"] != settlement.node_id or row["use_id"] != settlement.use_id:
                return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.LINK_MISMATCH)

            node_row = self._conn.execute(
                "SELECT device_state FROM node WHERE node_id = ?", (settlement.node_id,)
            ).fetchone()
            if node_row is None:
                raise LookupError(f"기기 {settlement.node_id} 가 없다")
            if node_row["device_state"] != settlement.expected_node_state.value:
                return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
            if RecoveryStatus(row["status"]) != settlement.expected_recovery_status:
                return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
            for command_id, expected_status in settlement.expected_commands.items():
                command_row = self._conn.execute(
                    "SELECT status FROM node_command WHERE command_id = ?", (command_id,)
                ).fetchone()
                if command_row is None or command_row["status"] != expected_status.value:
                    return StoreResult(
                        outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT
                    )
            for use_id_key, expected_state in settlement.expected_sessions.items():
                session_row = self._conn.execute(
                    "SELECT state FROM use_session WHERE use_id = ?", (use_id_key,)
                ).fetchone()
                if session_row is None or session_row["state"] != expected_state.value:
                    return StoreResult(
                        outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT
                    )
            for usage_settlement in settlement.usage_settlements:
                usage_row = self._conn.execute(
                    "SELECT state FROM approval_usage WHERE usage_id = ?", (usage_settlement.usage_id,)
                ).fetchone()
                if usage_row is None or usage_row["state"] != usage_settlement.expected_state.value:
                    return StoreResult(
                        outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT
                    )

            for usage_settlement in settlement.usage_settlements:
                self._conn.execute(
                    "UPDATE approval_usage SET state = ? WHERE usage_id = ?",
                    (usage_settlement.state.value, usage_settlement.usage_id),
                )
            if settlement.use_id is not None:
                self._conn.execute(
                    "UPDATE use_session SET state = ?, closed_at = ?, end_reason = ? WHERE use_id = ?",
                    (
                        SessionState.CLOSED.value, iso(settlement.closed_at), settlement.end_reason.value,
                        settlement.use_id,
                    ),
                )
            self._conn.execute(
                "UPDATE node SET device_state = ? WHERE node_id = ?",
                (DeviceState.AVAILABLE.value, settlement.node_id),
            )
            self._conn.execute(
                "UPDATE recovery SET approved_by_user_id = ?, approved_at = ?, settlement_digest = ?, "
                "settlement_json = ?, completed_at = ? WHERE recovery_id = ?",
                (
                    settlement.approved_by_user_id, iso(settlement.approved_at), settlement.settlement_digest,
                    self._settlement_to_json(settlement), iso(datetime.now(timezone.utc)),
                    settlement.recovery_id,
                ),
            )
            self._append_audit(
                None, None,
                {
                    "event": "settle_recovery", "recovery_id": settlement.recovery_id,
                    "node_id": settlement.node_id, "settlement_digest": settlement.settlement_digest,
                },
                step=Step.FAULT,
            )
            updated = self._conn.execute(
                "SELECT * FROM recovery WHERE recovery_id = ?", (settlement.recovery_id,)
            ).fetchone()
            return StoreResult(outcome=StoreOutcome.APPLIED, recovery=self._recovery_from_row(updated))
