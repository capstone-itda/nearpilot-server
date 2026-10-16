"""`RetentionStore` 구현 (specs/db-retention-capacity/spec.md, FR-21, FR-22, #164, #165).

`node_command` 행은 지우지 않는다 — "명령 중복 방지 항목"의 보존기간은 `requirements.md`
FR-22가 아직 미결로 남겨 뒀다. `use_session`을 지울 때는 그 행을 참조하는
`node_command.use_id`·완료된 `recovery.use_id`만 `NULL`로 뗀다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from nearpilot.db.repositories._common import immediate_transaction, iso, parse_iso
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import ExecutionStatus, SessionState
from nearpilot.shared.models import CleanupReport


class SqliteRetentionStore:
    """`RetentionStore` Protocol 구현.

    `jsonl_path`를 주지 않으면 `audit_log`의 보존기간 정리를 하지 않는다 — JSONL이 유일한
    사본이므로 사본이 없으면 원본을 지우지 않는다 (spec §6 결정). 서버 조립이 `db/audit/`가
    쓰는 JSONL 경로를 그대로 넘기므로 운영 중에는 이 정리가 정상적으로 돈다.
    """

    def __init__(
        self, conn: sqlite3.Connection, settings: Settings | None = None, jsonl_path: str | None = None
    ) -> None:
        self._conn = conn
        self._settings = settings or Settings()
        self._jsonl_path = jsonl_path

    def cleanup(self, now: datetime) -> CleanupReport:
        now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        with immediate_transaction(self._conn):
            cleared_requests = self.clear_expired_request_ids(now)
            deleted_sessions = self._delete_eligible_sessions(now)
            deleted_audit_rows = self._delete_eligible_audit_rows(now)
        return CleanupReport(
            cleared_requests=cleared_requests,
            deleted_sessions=deleted_sessions,
            deleted_audit_rows=deleted_audit_rows,
        )

    # ── 요청의 "완료" 판정 (spec §6) ──────────────────────────────

    def _is_complete(self, internal_request_id: str) -> bool:
        row = self._conn.execute(
            "SELECT verdict, node_id FROM request WHERE internal_request_id = ?",
            (internal_request_id,),
        ).fetchone()
        if row is None or row["verdict"] is None:
            return False
        session = self._conn.execute(
            "SELECT use_id, state FROM use_session WHERE internal_request_id = ?",
            (internal_request_id,),
        ).fetchone()
        if session is not None and session["state"] != SessionState.CLOSED.value:
            return False
        pending = self._conn.execute(
            "SELECT 1 FROM node_command WHERE internal_request_id = ? AND status IN (?, ?) LIMIT 1",
            (internal_request_id, ExecutionStatus.PENDING.value, ExecutionStatus.IN_PROGRESS.value),
        ).fetchone()
        if pending is not None:
            return False
        use_id = session["use_id"] if session is not None else None
        return not self._has_unresolved_recovery(internal_request_id, row["node_id"], use_id)

    def _has_unresolved_recovery(self, internal_request_id: str, node_id: str | None, use_id: str | None) -> bool:
        if use_id is not None:
            row = self._conn.execute(
                "SELECT 1 FROM recovery WHERE use_id = ? AND completed_at IS NULL LIMIT 1", (use_id,)
            ).fetchone()
            return row is not None
        if node_id is None:
            return False
        rows = self._conn.execute(
            "SELECT internal_request_ids FROM recovery WHERE node_id = ? AND completed_at IS NULL",
            (node_id,),
        ).fetchall()
        return any(internal_request_id in json.loads(row["internal_request_ids"]) for row in rows)

    def _completion_time(self, internal_request_id: str) -> datetime | None:
        """완료되지 않았으면 `None`. 완료됐으면 마지막 감사 행 시각으로 근사한다 (spec §6)."""
        if not self._is_complete(internal_request_id):
            return None
        row = self._conn.execute(
            "SELECT MAX(created_at) AS latest FROM audit_log WHERE internal_request_id = ?",
            (internal_request_id,),
        ).fetchone()
        latest = parse_iso(row["latest"]) if row["latest"] else None
        session = self._conn.execute(
            "SELECT use_id FROM use_session WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        if session is not None:
            recovered = self._conn.execute(
                "SELECT MAX(completed_at) AS latest FROM recovery WHERE use_id = ? AND completed_at IS NOT NULL",
                (session["use_id"],),
            ).fetchone()
            if recovered and recovered["latest"]:
                recovered_at = parse_iso(recovered["latest"])
                if latest is None or recovered_at > latest:
                    latest = recovered_at
        return latest

    # ── 외부 request_id 비우기 ───────────────────────────────────

    def clear_expired_request_ids(self, now: datetime) -> int:
        """DB 내부 접점. 호출자가 연 쓰기 트랜잭션에서 만료 ID만 비운다."""
        if not self._conn.in_transaction:
            raise RuntimeError("Request ID cleanup requires an active transaction")
        rows = self._conn.execute(
            "SELECT internal_request_id FROM request WHERE request_id IS NOT NULL"
        ).fetchall()
        ttl = self._settings.request_dedup_ttl_sec
        cleared = 0
        for row in rows:
            internal_request_id = row["internal_request_id"]
            completed_at = self._completion_time(internal_request_id)
            if completed_at is None or (now - completed_at).total_seconds() < ttl:
                continue
            self._conn.execute(
                "UPDATE request SET request_id = NULL WHERE internal_request_id = ?",
                (internal_request_id,),
            )
            cleared += 1
        return cleared

    # ── use_session 삭제와 참조 분리 ─────────────────────────────

    def _delete_eligible_sessions(self, now: datetime) -> int:
        rows = self._conn.execute(
            "SELECT use_id, closed_at FROM use_session WHERE state = ? AND closed_at IS NOT NULL",
            (SessionState.CLOSED.value,),
        ).fetchall()
        retention_sec = self._settings.use_history_retention_days * 86400
        deleted = 0
        for row in rows:
            use_id = row["use_id"]
            closed_at = parse_iso(row["closed_at"])
            if (now - closed_at).total_seconds() < retention_sec:
                continue
            unresolved = self._conn.execute(
                "SELECT 1 FROM recovery WHERE use_id = ? AND completed_at IS NULL LIMIT 1", (use_id,)
            ).fetchone()
            if unresolved is not None:
                continue
            self._conn.execute("UPDATE node_command SET use_id = NULL WHERE use_id = ?", (use_id,))
            self._conn.execute(
                "UPDATE recovery SET use_id = NULL WHERE use_id = ? AND completed_at IS NOT NULL", (use_id,)
            )
            self._conn.execute("DELETE FROM use_session WHERE use_id = ?", (use_id,))
            deleted += 1
        return deleted

    # ── audit_log 삭제 ───────────────────────────────────────────

    def _delete_eligible_audit_rows(self, now: datetime) -> int:
        if not self._jsonl_path:
            return 0
        exported_max_seq = self._exported_max_seq()
        if exported_max_seq <= 0:
            return 0
        max_rowid_row = self._conn.execute("SELECT MAX(rowid) AS m FROM audit_log").fetchone()
        max_rowid = max_rowid_row["m"]
        if max_rowid is None:
            return 0
        cutoff = now - timedelta(days=self._settings.audit_retention_days)
        rows = self._conn.execute(
            "SELECT rowid AS rid, internal_request_id FROM audit_log "
            "WHERE created_at <= ? AND rowid < ? AND rowid <= ?",
            (iso(cutoff), max_rowid, exported_max_seq),
        ).fetchall()
        deleted = 0
        for row in rows:
            internal_request_id = row["internal_request_id"]
            if internal_request_id is not None and self._completion_time(internal_request_id) is None:
                continue
            self._conn.execute("DELETE FROM audit_log WHERE rowid = ?", (row["rid"],))
            deleted += 1
        return deleted

    def _exported_max_seq(self) -> int:
        """JSONL 파일 마지막 줄의 `seq`를 읽기만 한다. `db/audit/jsonl.py`는 바꾸지 않는다

        (spec §4 경계). 끊긴 줄 복구나 파일 쓰기는 그 모듈의 내보내기 쪽 책임이라 여기서는
        하지 않는다 — 여기는 "이미 내보낸 최대 seq를 읽기만" 한다.
        """
        path = self._jsonl_path
        if not path or not os.path.exists(path):
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return 0
        if not content:
            return 0
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        last_seq = 0
        for line in lines:
            try:
                obj = json.loads(line)
                last_seq = int(obj["seq"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                break
        return last_seq
