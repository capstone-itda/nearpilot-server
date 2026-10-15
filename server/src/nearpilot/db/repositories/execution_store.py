"""`ExecutionStore` 구현 (#17, specs/db-execution-store/spec.md).

예약 이후의 사건별 저장을 맡는다. `SqliteRepository`와 같은 연결을 받아 같은 쓰기 경로를 쓴다
(`AGENTS.md` 의존 규칙 5). 함수는 spec 6절의 구현 순서대로 추가한다.
"""

from __future__ import annotations

import sqlite3

from nearpilot.db.repositories._common import from_json, immediate_transaction, new_id, parse_iso
from nearpilot.shared.enums import (
    Action,
    CommandOutcome,
    ExecutionKind,
    ExecutionStatus,
    Occupancy,
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

        link_command는 execution_kind·use_id를 인자로 받지 않는다 (`interfaces.py`). 공용형은
        점유 세션이 없다. 대여형은 이번 요청이 만든 세션이 있으면 최초 대여, 없으면 이미
        ACTIVE인 세션에 붙는 재조작이다 (FR-19, #35).
        """
        if occupancy == Occupancy.SHARED.value:
            return ExecutionKind.SHARED, None
        own_session = self._conn.execute(
            "SELECT use_id FROM use_session WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        if own_session is not None:
            return ExecutionKind.RENTAL_INITIAL, own_session["use_id"]
        active_session = self._conn.execute(
            "SELECT use_id FROM use_session WHERE node_id = ? AND state = 'ACTIVE'", (node_id,)
        ).fetchone()
        if active_session is None:
            raise LookupError(f"기기 {node_id} 에 연결할 점유 세션이 없다")
        return ExecutionKind.RENTAL_REOPERATION, active_session["use_id"]

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
