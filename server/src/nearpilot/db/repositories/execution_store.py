"""`ExecutionStore` 구현 (#17, specs/db-execution-store/spec.md).

예약 이후의 사건별 저장을 맡는다. `SqliteRepository`와 같은 연결을 받아 같은 쓰기 경로를 쓴다
(`AGENTS.md` 의존 규칙 5). 함수는 spec 6절의 구현 순서대로 추가한다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from nearpilot.db.repositories._common import from_json, immediate_transaction, iso, new_id, parse_iso, to_json
from nearpilot.shared.enums import (
    Action,
    ApprovalUsageState,
    CommandOutcome,
    DeviceState,
    EndReason,
    ExecutionKind,
    ExecutionStatus,
    Occupancy,
    ReasonCode,
    SessionState,
    Step,
    StoreConflictReason,
    StoreOutcome,
)
from nearpilot.shared.models import CommandRecord, StoreResult


class SqliteExecutionStore:
    """`ExecutionStore` Protocol 구현."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 실행 (#17 1단계) ────────────────────────────────────────────

    def link_command(self, internal_request_id: str, node_id: str, action: Action) -> StoreResult:
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
                "SELECT received_at FROM request WHERE internal_request_id = ?", (internal_request_id,)
            ).fetchone()
            if request_row is None:
                raise LookupError(f"요청 {internal_request_id} 가 없다")
            node_row = self._conn.execute(
                "SELECT occupancy FROM node WHERE node_id = ?", (node_id,)
            ).fetchone()
            if node_row is None:
                raise LookupError(f"기기 {node_id} 가 없다")

            execution_kind, use_id = self._resolve_execution_kind(
                internal_request_id, node_id, node_row["occupancy"]
            )
            usage_row = self._conn.execute(
                "SELECT usage_id FROM approval_usage WHERE internal_request_id = ?", (internal_request_id,)
            ).fetchone()
            usage_id = usage_row["usage_id"] if usage_row else None

            command_id = new_id()
            self._conn.execute(
                "INSERT INTO node_command (command_id, internal_request_id, node_id, action, status, "
                "execution_kind, use_id, usage_id, reserved_at, send_started_at, send_attempted, "
                "outcome, occurred_at, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, '{}')",
                (
                    command_id, internal_request_id, node_id, action.value, ExecutionStatus.PENDING.value,
                    execution_kind.value, use_id, usage_id, request_row["received_at"],
                ),
            )
            return StoreResult(
                outcome=StoreOutcome.APPLIED,
                command=CommandRecord(
                    command_id=command_id, internal_request_id=internal_request_id, node_id=node_id,
                    action=action, status=ExecutionStatus.PENDING, execution_kind=execution_kind,
                    use_id=use_id, usage_id=usage_id, reserved_at=parse_iso(request_row["received_at"]),
                ),
            )

    def _resolve_execution_kind(
        self, internal_request_id: str, node_id: str, occupancy: str
    ) -> tuple[ExecutionKind, str | None]:
        """점유 방식과 기존 점유 세션으로 실행 종류와 연결할 `use_id`를 정한다.

        link_command는 execution_kind·use_id를 인자로 받지 않는다 (`interfaces.py`). `node.occupancy`를
        가장 먼저 본다. 승인 사용분(`approval_usage`) 행의 존재 여부로 판단하지 않는다 — `open`
        정책의 재조작은 사용분을 예약하지 않아 행이 없기 때문이다 (#27).

        1. `SHARED`면 공용형이다. 점유 세션이 없다.
        2. 대여형이고 이 `internal_request_id`로 만든 세션이 있으면 최초 대여다.
        3. 대여형이고 그 세션이 없는데 기기에 `ACTIVE` 세션이 있으면 재조작이다 (FR-19, #35).
        """
        if occupancy == Occupancy.SHARED.value:
            return ExecutionKind.SHARED, None
        own_session = self._conn.execute(
            "SELECT use_id FROM use_session WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        if own_session is not None:
            return ExecutionKind.RENTAL_INITIAL, own_session["use_id"]
        active_session = self._conn.execute(
            "SELECT use_id FROM use_session WHERE node_id = ? AND state = ?",
            (node_id, SessionState.ACTIVE.value),
        ).fetchone()
        if active_session is None:
            raise LookupError(f"기기 {node_id} 에 연결할 점유 세션이 없다")
        return ExecutionKind.RENTAL_REOPERATION, active_session["use_id"]

    def _already_cancelled(self, internal_request_id: str) -> bool:
        """`cancel_not_sent`가 명령 연결 전에 이 요청을 이미 취소했는지 본다 (spec §6 결정).

        최초 대여는 점유 세션이 `CLOSED`·`cancelled`로 남는다. 재조작·공용형은 점유 세션이
        없으므로 승인 사용분이 `released`인지로 본다. 승인이 없던 요청(`open`+공용형)은
        취소해도 아무 행도 남지 않아 판단할 근거가 없다 — 그 경우는 보호할 상태도 없었으므로
        뒤늦은 연결을 막지 않아도 안전하다.
        """
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
            if from_json(command_row["evidence"]) == dict(evidence):
                return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, command=self._command_from_row(command_row))
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
        self._append_audit(command_row["internal_request_id"], command_row["command_id"], reason, evidence)
        updated = self._conn.execute(
            "SELECT * FROM node_command WHERE command_id = ?", (command_row["command_id"],)
        ).fetchone()
        return StoreResult(outcome=StoreOutcome.APPLIED, command=self._command_from_row(updated))

    def _cancel_unlinked(
        self, internal_request_id: str, expected: ExecutionStatus, reason: ReasonCode,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        """`link_command` 전에 멈춘 예약의 취소다 (spec §6 결정, `specs/state-management/core-restore.md`).

        명령 행이 없으므로 `StoreResult.command`는 `None`이다. 예약(점유 세션)·승인 사용분
        정리만 반영한다. 이후 `link_command`가 같은 요청으로 오면 `CONFLICT`다 (6절 결정).
        """
        if expected != ExecutionStatus.PENDING:
            return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
        own_session = self._conn.execute(
            "SELECT use_id, state FROM use_session WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        usage_row = self._conn.execute(
            "SELECT usage_id, state FROM approval_usage WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        already_cancelled = (
            own_session is not None and own_session["state"] == SessionState.CLOSED.value
        ) or (
            own_session is None and usage_row is not None
            and usage_row["state"] == ApprovalUsageState.RELEASED.value
        )
        if already_cancelled:
            return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED, command=None)
        if own_session is not None and own_session["state"] != SessionState.RESERVED.value:
            return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
        self._release_reservation(internal_request_id)
        self._append_audit(internal_request_id, None, reason, evidence)
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
        self, internal_request_id: str | None, command_id: str | None, reason: ReasonCode,
        evidence: Mapping[str, Any],
    ) -> None:
        """`ExecutionStore` 안에서 쓰는 감사 행의 `event_id`는 DB가 만든다 (`architecture.md` §4)."""
        self._conn.execute(
            "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
            "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(), new_id(), internal_request_id, command_id, Step.EXEC.value,
                to_json({"event": "cancel_not_sent", "reason": reason.value, "evidence": dict(evidence)}),
                iso(datetime.now(timezone.utc)),
            ),
        )

    def execution(self, internal_request_id: str) -> CommandRecord | None:
        row = self._conn.execute(
            "SELECT * FROM node_command WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        return None if row is None else self._command_from_row(row)

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
        )
