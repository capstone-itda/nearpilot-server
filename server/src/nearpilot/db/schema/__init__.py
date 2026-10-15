"""DDL 적용과 연결 생성 (specs/db-repository/spec.md §6 결정).

연결은 스레드마다 하나씩 연다. 트랜잭션은 `isolation_level=None`과 `BEGIN IMMEDIATE`로
직접 연다. 스키마 버전은 `PRAGMA user_version`으로 관리하고, DDL은 이 디렉터리의
`sql/*.sql` 파일에 둔다. 마이그레이션 도구는 추가하지 않는다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SQL_DIR = Path(__file__).parent / "sql"


def _migration_files() -> list[Path]:
    return sorted(_SQL_DIR.glob("*.sql"))


def migrate(conn: sqlite3.Connection) -> None:
    """현재 `user_version`보다 번호가 큰 DDL 파일을 순서대로 적용한다."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, path in enumerate(_migration_files(), start=1):
        if index <= current:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"{path.name} 적용 뒤 외래키 위반: {violations}")
        conn.execute(f"PRAGMA user_version = {index}")


def connect(db_path: str, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """연결 하나를 만들고 WAL·외래키·대기시간을 설정한 뒤 스키마를 최신으로 맞춘다."""
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    migrate(conn)
    return conn
