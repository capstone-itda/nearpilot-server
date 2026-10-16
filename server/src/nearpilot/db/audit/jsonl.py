"""`audit_log` 테이블의 커밋된 행을 JSONL 사본으로 내보낸다 (#34, FR-22, NFR-13).

줄마다 `seq`(=`rowid`)를 담는다. 내보낼 때마다 파일 마지막 줄의 `seq`를 다시 읽어 그 다음부터
이어 쓴다 — 메모리에 커서를 캐시하지 않는다. 그래야 같은 파일을 가리키는 서로 다른 연결(스레드)의
내보내기 객체도 파일 자체를 유일한 진실로 삼아 안전하게 공유할 수 있다 (spec §6 결정).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Any

from nearpilot.db.repositories._common import from_json, register_jsonl_export

logger = logging.getLogger(__name__)

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    """경로마다 잠금 하나를 공유한다 — 같은 파일을 가리키는 여러 연결도 직렬화된다 (spec §6)."""
    with _path_locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[path] = lock
        return lock


class JsonlExporter:
    """`AuditLog`의 JSONL 사본 하나를 관리한다. 연결 하나당 인스턴스 하나를 만든다."""

    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self._conn = conn
        self._path = path
        self._lock = _lock_for(path)
        register_jsonl_export(conn, self)

    def flush(self) -> None:
        """커서 다음 행을 모두 내보낸다. 실패해도 예외를 올리지 않는다 (#34)."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        cursor = self._last_seq_locked()
        try:
            rows = self._conn.execute(
                "SELECT rowid AS seq, event_id, internal_request_id, command_id, step, "
                "payload_json, created_at FROM audit_log WHERE rowid > ? ORDER BY rowid",
                (cursor,),
            ).fetchall()
        except sqlite3.Error as error:
            logger.warning("감사 로그 JSONL 조회 실패: %s", error)
            return
        if not rows:
            return
        text = "".join(self._serialize(row) + "\n" for row in rows)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError as error:
            logger.warning("감사 로그 JSONL 쓰기 실패: %s", error)

    def rebuild(self) -> None:
        """`audit_log` 전체로 파일을 다시 만든다. 기존 파일은 원자적으로 바뀐다 (#34)."""
        with self._lock:
            self._rebuild_locked()

    def _rebuild_locked(self) -> None:
        tmp_path = f"{self._path}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            rows = self._conn.execute(
                "SELECT rowid AS seq, event_id, internal_request_id, command_id, step, "
                "payload_json, created_at FROM audit_log ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as error:
            logger.warning("감사 로그 JSONL 재구성 조회 실패: %s", error)
            return
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("".join(self._serialize(row) + "\n" for row in rows))
            os.replace(tmp_path, self._path)
        except OSError as error:
            logger.warning("감사 로그 JSONL 재구성 실패: %s", error)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _serialize(row: sqlite3.Row) -> str:
        record: dict[str, Any] = {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "internal_request_id": row["internal_request_id"],
            "command_id": row["command_id"],
            "step": row["step"],
            "payload": from_json(row["payload_json"]),
            "created_at": row["created_at"],
        }
        return json.dumps(record, ensure_ascii=False)

    def _last_seq_locked(self) -> int:
        """파일 마지막 줄의 `seq`를 반환한다. 끊긴 마지막 줄은 잘라내고 그 앞 줄의 `seq`를 쓴다."""
        if not os.path.exists(self._path):
            return 0
        try:
            with open(self._path, encoding="utf-8") as f:
                content = f.read()
        except OSError as error:
            logger.warning("감사 로그 JSONL 읽기 실패: %s", error)
            return 0
        if not content:
            return 0
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()

        valid_count = 0
        last_seq = 0
        for line in lines:
            try:
                obj = json.loads(line)
                last_seq = int(obj["seq"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                break
            valid_count += 1

        if valid_count < len(lines):
            kept = "".join(line + "\n" for line in lines[:valid_count])
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    f.write(kept)
            except OSError as error:
                logger.warning("감사 로그 JSONL 끊긴 줄 정리 실패: %s", error)
        return last_seq
