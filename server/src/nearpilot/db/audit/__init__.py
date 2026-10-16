"""`AuditLog` 구현 (FR-22). 테이블이 원본이다. JSONL은 커밋 뒤 사본이다 (#34)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from nearpilot.db.audit.jsonl import JsonlExporter
from nearpilot.db.repositories._common import new_id, to_json
from nearpilot.shared.models import AuditEvent


class SqliteAuditLog:
    """`AuditLog` Protocol 구현. `event_id` 가 같으면 한 번만 저장한다.

    `jsonl_path` 를 주면 이 연결의 모든 쓰기(`AuditLog.append` 뿐 아니라 `Repository`·
    `ExecutionStore`·`AdminRepository` 의 `immediate_transaction` 커밋도)를 JSONL 로 함께
    내보낸다 (#34). 주지 않으면 지금처럼 테이블에만 저장한다.
    """

    def __init__(self, conn: sqlite3.Connection, jsonl_path: str | None = None) -> None:
        self._conn = conn
        self._exporter = JsonlExporter(conn, jsonl_path) if jsonl_path else None

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
        # 단일 문장 INSERT는 자동 커밋이라 immediate_transaction 훅을 거치지 않는다.
        # 여기서 직접 내보낸다 (#34, spec 3절·6절 결정).
        if self._exporter is not None:
            self._exporter.flush()
