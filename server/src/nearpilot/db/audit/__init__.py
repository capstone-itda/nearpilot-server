"""`AuditLog` 구현 (FR-22). 테이블이 원본이다. JSONL 내보내기는 후속 작업이다 (#34)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from nearpilot.db.repositories._common import new_id, to_json
from nearpilot.shared.models import AuditEvent


class SqliteAuditLog:
    """`AuditLog` Protocol 구현. `event_id` 가 같으면 한 번만 저장한다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, event: AuditEvent) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO audit_log "
            "(log_id, event_id, internal_request_id, command_id, step, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(),
                event.event_id,
                event.internal_request_id,
                event.command_id,
                event.step.value,
                to_json(event.payload),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
