"""기존 DB를 새 마이그레이션으로 올려도 기존 요청 행이 보존되는지 본다 (#86, spec §6 결정)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import nearpilot.db.schema as schema_module
from nearpilot.db.schema import connect


def test_schema_migration_keeps_requests(tmp_path):
    db_path = str(tmp_path / "upgrade.db")
    sql_dir = Path(schema_module.__file__).parent / "sql"
    files = sorted(sql_dir.glob("*.sql"))

    setup_conn = sqlite3.connect(db_path, isolation_level=None)
    setup_conn.row_factory = sqlite3.Row
    setup_conn.execute("PRAGMA foreign_keys = ON")
    # request_kind·settings_json이 없던 시절(0004까지)의 스키마를 흉내 낸다.
    for path in files[:4]:
        setup_conn.executescript(path.read_text(encoding="utf-8"))
    setup_conn.execute("PRAGMA user_version = 4")
    setup_conn.execute(
        "INSERT INTO user (user_id, name, token_hash, role) VALUES ('u1', 'tester', 'hash-1', 'visitor')"
    )
    setup_conn.execute(
        "INSERT INTO request (internal_request_id, user_id, request_id, input_digest, "
        "node_id, target_spec, action, received_at) VALUES "
        "('req1', 'u1', 'ext-1', 'digest-1', NULL, 'nearest:locker', 'open', '2026-01-01T00:00:00+00:00')"
    )
    setup_conn.close()

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM request WHERE internal_request_id = 'req1'").fetchone()
        assert row is not None
        assert row["request_kind"] == "visitor"
        assert row["user_id"] == "u1"
        assert row["request_id"] == "ext-1"
        assert row["input_digest"] == "digest-1"
        assert row["target_spec"] == "nearest:locker"
    finally:
        conn.close()
