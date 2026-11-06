"""DDL 적용과 연결 생성 (specs/db-repository/spec.md §6 결정).

연결은 스레드마다 하나씩 연다. 트랜잭션은 `isolation_level=None`과 `BEGIN IMMEDIATE`로
직접 연다. 스키마 버전은 `PRAGMA user_version`으로 관리하고, DDL은 이 디렉터리의
`sql/*.sql` 파일에 둔다. 마이그레이션 도구는 추가하지 않는다.

예외는 SQL로 할 수 없는 백필 하나다. 0010은 SQLite에 SHA-256이 없어서 Python 후처리가
기기 선언과 선언 해시를 채운다 (specs/iot-device-descriptor/spec.md 결정 9). 후처리가 있는
버전은 DDL, 후처리, 버전 기록을 한 트랜잭션에서 한다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from nearpilot.shared.descriptor import DescriptorError, legacy_descriptor

_SQL_DIR = Path(__file__).parent / "sql"
log = logging.getLogger(__name__)


def _migration_files() -> list[Path]:
    return sorted(_SQL_DIR.glob("*.sql"))


def _backfill_descriptor(conn: sqlite3.Connection) -> None:
    """기존 기기의 기능·점유 방식·제어 정보 → v2 템플릿 선언과 해시. 신뢰 상태는 유지한다 (FR-04).

    템플릿을 만들 수 없는 행(역할 규칙에 맞지 않는 조합)은 선언 없이 남긴다. 다음 발표가 채운다.
    """
    rows = conn.execute(
        "SELECT node_id, capability, occupancy, light_controls, speaker_controls FROM node").fetchall()
    for node_id, capability, occupancy, light, speaker in rows:
        controls: dict = {}
        try:
            if light is not None:
                data = json.loads(light)
                controls.update(brightness_max=data["brightness_max"], color=data["color"])
            if speaker is not None:
                controls.update(volume_max=json.loads(speaker)["volume_max"])
            descriptor = legacy_descriptor(capability, occupancy, **controls)
        except (DescriptorError, ValueError, KeyError, TypeError):
            log.warning("기기 선언 이전 실패, 다음 발표를 기다린다: node=%s", node_id)
            continue
        conn.execute(
            "UPDATE node SET descriptor = ?, descriptor_hash = ? WHERE node_id = ?",
            (json.dumps(descriptor.to_json(), sort_keys=True, ensure_ascii=False), descriptor.hash, node_id))
    conn.execute("ALTER TABLE node DROP COLUMN light_controls")
    conn.execute("ALTER TABLE node DROP COLUMN speaker_controls")


# 버전 → DDL 뒤에 같은 트랜잭션에서 부르는 후처리.
_POST_STEPS: dict[int, Callable[[sqlite3.Connection], None]] = {10: _backfill_descriptor}


def migrate(conn: sqlite3.Connection) -> None:
    """현재 `user_version`보다 번호가 큰 DDL 파일을 순서대로 적용한다."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, path in enumerate(_migration_files(), start=1):
        if index <= current:
            continue
        post = _POST_STEPS.get(index)
        if post is None:
            conn.executescript(path.read_text(encoding="utf-8"))
            _check_foreign_keys(conn, path)
            conn.execute(f"PRAGMA user_version = {index}")
            continue
        conn.executescript("BEGIN IMMEDIATE;\n" + path.read_text(encoding="utf-8"))
        try:
            post(conn)
            _check_foreign_keys(conn, path)
            conn.execute(f"PRAGMA user_version = {index}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def _check_foreign_keys(conn: sqlite3.Connection, path: Path) -> None:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"{path.name} 적용 뒤 외래키 위반: {violations}")


def connect(db_path: str, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """연결 하나를 만들고 WAL·외래키·대기시간을 설정한 뒤 스키마를 최신으로 맞춘다."""
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    migrate(conn)
    return conn
