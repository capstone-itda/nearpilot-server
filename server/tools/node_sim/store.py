"""Per-device SQLite command records (FR-06, FR-07).

Separate from the server DB. Each write is one transaction, and the simulator
starts actuation only after the accepted record is committed. Records are
never deleted automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading


class StorageError(RuntimeError):
    """The record could not be read or written."""


@dataclass(frozen=True)
class CommandRecord:
    command_id: str
    content: str  # Canonical JSON of the command content fields.
    state: str  # Last result event state.
    event_id: str
    occurred_at: str
    error_code: str | None = None


class CommandStore:
    """One SQLite file per simulated device. `fail_writes` simulates storage failure."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.fail_writes = False
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY, content TEXT NOT NULL, state TEXT NOT NULL,
                event_id TEXT NOT NULL, occurred_at TEXT NOT NULL, error_code TEXT);
        """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, args: tuple) -> None:
        with self._lock:
            if self.fail_writes:
                raise StorageError("Simulated storage failure")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = self._db.execute(sql, args)
                    if cursor.rowcount != 1:
                        raise StorageError("Record write did not apply")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                self._db.execute("COMMIT")
            except sqlite3.Error as error:
                raise StorageError("Record write failed") from error

    def _read(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            try:
                return self._db.execute(sql, args).fetchall()
            except sqlite3.Error as error:
                raise StorageError("Record read failed") from error

    def command(self, command_id: str) -> CommandRecord | None:
        rows = self._read("SELECT command_id, content, state, event_id, occurred_at, error_code"
                          " FROM commands WHERE command_id = ?", (command_id,))
        return CommandRecord(*rows[0]) if rows else None

    def insert_command(self, record: CommandRecord) -> None:
        self._write("INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?)",
                    (record.command_id, record.content, record.state, record.event_id,
                     record.occurred_at, record.error_code))

    def update_command(self, record: CommandRecord) -> None:
        self._write("UPDATE commands SET state = ?, event_id = ?, occurred_at = ?, error_code = ?"
                    " WHERE command_id = ? AND content = ?",
                    (record.state, record.event_id, record.occurred_at, record.error_code,
                     record.command_id, record.content))

    def unfinished(self) -> list[CommandRecord]:
        rows = self._read("SELECT command_id, content, state, event_id, occurred_at, error_code"
                          " FROM commands WHERE state IN ('accepted', 'running')")
        return [CommandRecord(*row) for row in rows]
