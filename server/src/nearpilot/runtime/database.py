"""스레드가 소유하는 SQLite 연결 범위 (spec 6절 "DB와 서비스 연결", FR-19, NFR-03).

최상위 동기 호출마다 그 스레드에서 연결을 열고, 중첩 호출은 같은 연결을 쓴다.
최상위 호출이 끝나면 같은 스레드에서 JSONL 내보내기 등록을 해제하고 연결을 닫는다.
트랜잭션은 각 DB 함수가 연다. 연결 범위는 트랜잭션이 아니다.
서비스에는 `shared` Protocol 모양의 위임 객체를 넘긴다. 위임 객체는 호출마다 현재 범위의 구현을 쓴다.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nearpilot.db.repositories import SqliteExecutionStore, SqliteRepository, SqliteRetentionStore
from nearpilot.db.audit import SqliteAuditLog
from nearpilot.db.audit.jsonl import JsonlExporter
from nearpilot.db.repositories._common import unregister_jsonl_export
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.interfaces import (
    AccountLookup, AuditLog, ExecutionStore, Repository, RetentionStore,
)
from nearpilot.shared.models import BeaconKey

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bound:
    """한 연결 위의 DB 구현 묶음. 만든 스레드에서만 쓴다."""

    conn: sqlite3.Connection
    repo: SqliteRepository
    store: SqliteExecutionStore
    audit: SqliteAuditLog
    retention: SqliteRetentionStore


class ThreadDatabase:
    def __init__(
        self, path: str | Path, *, settings: Settings, audit_jsonl: str | Path,
        busy_timeout_ms: int, connect_fn: Callable[..., sqlite3.Connection] = connect,
    ) -> None:
        self._path = str(path)
        self._settings = settings
        # 모든 연결이 같은 파일 잠금을 공유하도록 같은 절대 경로로 정규화한다.
        self.audit_jsonl = os.path.abspath(os.fspath(audit_jsonl))
        self._busy_timeout_ms = busy_timeout_ms
        self._connect = connect_fn
        self._local = threading.local()
        self._count_lock = threading.Lock()
        self._open = 0

    @property
    def open_connections(self) -> int:
        """아직 닫지 않은 최상위 범위 수. 종료 진단용이다."""
        with self._count_lock:
            return self._open

    @contextmanager
    def scope(self) -> Iterator[Bound]:
        current: Bound | None = getattr(self._local, "bound", None)
        if current is not None:
            yield current
            return
        conn = self._connect(self._path, busy_timeout_ms=self._busy_timeout_ms)
        with self._count_lock:
            self._open += 1
        try:
            bound = Bound(
                conn=conn,
                repo=SqliteRepository(conn, self._settings),
                store=SqliteExecutionStore(conn),
                audit=SqliteAuditLog(conn, self.audit_jsonl),
                retention=SqliteRetentionStore(conn, self._settings, self.audit_jsonl),
            )
            self._local.bound = bound
            yield bound
        finally:
            self._local.bound = None
            try:
                unregister_jsonl_export(conn)
                conn.close()
            finally:
                with self._count_lock:
                    self._open -= 1

    def export_jsonl(self) -> None:
        """시작·종료 내보내기. 실패해도 DB 결과를 바꾸지 않는다 (FR-22, #34)."""
        try:
            with self.scope() as bound:
                JsonlExporter(bound.conn, self.audit_jsonl).flush()
        except Exception as error:  # noqa: BLE001 - 사본 실패는 저장 실패가 아니다
            log.warning("감사 로그 JSONL 내보내기 실패: %s", type(error).__name__)

    def active_beacons(self) -> Sequence[BeaconKey]:
        """근접 추정 복원용 활성 비콘 목록. runtime 의 유일한 직접 조회다 (spec 6절 결정)."""
        with self.scope() as bound:
            rows = bound.conn.execute(
                "SELECT uuid, major, minor FROM beacon WHERE is_active = 1 ORDER BY uuid, major, minor"
            ).fetchall()
        return [BeaconKey(row["uuid"], row["major"], row["minor"]) for row in rows]

    # ── 위임 객체 ──────────────────────────────────────────────────

    def repository(self) -> Repository:
        return _scoped(self, "repo", Repository)

    def accounts(self) -> AccountLookup:
        return _scoped(self, "repo", AccountLookup)

    def execution_store(self) -> ExecutionStore:
        return _scoped(self, "store", ExecutionStore)

    def audit_log(self) -> AuditLog:
        return _scoped(self, "audit", AuditLog)

    def retention_store(self) -> RetentionStore:
        return _scoped(self, "retention", RetentionStore)


def _members(protocol: type) -> tuple[str, ...]:
    return tuple(sorted({
        name for klass in protocol.__mro__ if klass is not object
        for name, value in vars(klass).items() if not name.startswith("_") and callable(value)
    }))


def _delegate(role: str, name: str) -> Callable[..., Any]:
    def call(self: _ScopedBase, *args: Any, **kwargs: Any) -> Any:
        with self._db.scope() as bound:
            return getattr(getattr(bound, role), name)(*args, **kwargs)

    call.__name__ = name
    return call


class _ScopedBase:
    """Protocol 함수만 위임한다. 범위 밖 호출은 그 호출 하나를 최상위 범위로 연다."""

    def __init__(self, db: ThreadDatabase) -> None:
        self._db = db


_classes: dict[tuple[str, type], type] = {}


def _scoped(db: ThreadDatabase, role: str, protocol: type) -> Any:
    klass = _classes.get((role, protocol))
    if klass is None:
        namespace = {name: _delegate(role, name) for name in _members(protocol)}
        klass = _classes[(role, protocol)] = type(f"Scoped{protocol.__name__}", (_ScopedBase,), namespace)
    return klass(db)
