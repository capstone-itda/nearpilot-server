"""`AdminRepository` 구현 (specs/db-admin-repository/spec.md).

관리 기능, 시드 스크립트, 수집 도구가 쓰는 쓰기 접점이다. `SqliteRepository`와 같은 연결을
받는다 — DB 쓰기 경로는 하나다 (`AGENTS.md` 의존 규칙 5). 인증·권한 검사는 하지 않는다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from nearpilot.db.repositories._common import immediate_transaction, iso, new_id, to_json
from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason, Step
from nearpilot.shared.models import AccountRegistration, AdminWriteResult


class SqliteAdminRepository:
    """`AdminRepository` Protocol 구현."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def register_account(self, req: AccountRegistration) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            try:
                self._conn.execute(
                    "INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
                    (req.user_id, req.name, req.token_hash, req.role.value),
                )
            except sqlite3.IntegrityError:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.ACCOUNT_ALREADY_EXISTS
                )
            self._append_audit({"event": "register_account", "user_id": req.user_id, "role": req.role.value})
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def _append_audit(self, payload: Mapping[str, Any]) -> None:
        """감사 payload에 토큰 원문·`token_hash`를 넣지 않는다 (NFR-08, #29)."""
        self._conn.execute(
            "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
            "payload_json, created_at) VALUES (?, ?, NULL, NULL, ?, ?, ?)",
            (new_id(), new_id(), Step.ADMIN.value, to_json(payload), iso(datetime.now(timezone.utc))),
        )
