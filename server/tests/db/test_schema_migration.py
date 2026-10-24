"""기존 DB를 새 마이그레이션으로 올려도 기존 요청 행이 보존되는지 본다 (#86, spec §6 결정)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import nearpilot.db.schema as schema_module
from nearpilot.db.repositories.dashboard_reads import SqliteDashboardReads
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


def test_FR12A_schema_migration_rebuilt_request_table_keeps_rowid_cursor(tmp_path):
    """0005가 `request`를 다시 만들었어도 옛 행의 rowid 가 대시보드 읽기의 커서로 쓰인다."""
    db_path = str(tmp_path / "upgrade.db")
    sql_dir = Path(schema_module.__file__).parent / "sql"

    setup_conn = sqlite3.connect(db_path, isolation_level=None)
    setup_conn.row_factory = sqlite3.Row
    setup_conn.execute("PRAGMA foreign_keys = ON")
    for path in sorted(sql_dir.glob("*.sql"))[:4]:  # request_kind 가 없던 시절(0004까지)
        setup_conn.executescript(path.read_text(encoding="utf-8"))
    setup_conn.execute("PRAGMA user_version = 4")
    setup_conn.execute(
        "INSERT INTO user (user_id, name, token_hash, role) VALUES ('u1', 'tester', 'hash-1', 'visitor')"
    )
    for number in (1, 2):
        setup_conn.execute(
            "INSERT INTO request (internal_request_id, user_id, request_id, input_digest, "
            "node_id, target_spec, action, received_at) VALUES "
            f"('req{number}', 'u1', 'ext-{number}', 'digest-{number}', NULL, 'nearest:locker', 'open', "
            f"'2026-01-01T00:0{number}:00+00:00')"
        )
    setup_conn.close()

    conn = connect(db_path)
    try:
        reads = SqliteDashboardReads(conn)
        assert reads.cursors()["request"] == 2
        assert [(row.seq, row.internal_request_id, row.kind) for row in reads.requests_after(0)] == [
            (1, "req1", "visitor"), (2, "req2", "visitor")]
        assert [row.internal_request_id for row in reads.requests_after(1)] == ["req2"]
    finally:
        conn.close()


def test_FR12A_schema_migration_0006_adds_node_name(tmp_path):
    db_path = str(tmp_path / "upgrade.db")
    sql_dir = Path(schema_module.__file__).parent / "sql"
    files = sorted(sql_dir.glob("*.sql"))
    assert files[5].name == "0006_node_name.sql"

    setup_conn = sqlite3.connect(db_path, isolation_level=None)
    setup_conn.row_factory = sqlite3.Row
    setup_conn.execute("PRAGMA foreign_keys = ON")
    # node.name이 없던 시절(0005까지)의 스키마를 흉내 낸다.
    for path in files[:5]:
        setup_conn.executescript(path.read_text(encoding="utf-8"))
    setup_conn.execute("PRAGMA user_version = 5")
    assert "name" not in [column["name"] for column in setup_conn.execute("PRAGMA table_info(node)")]
    setup_conn.execute(
        "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, release_policy) "
        "VALUES ('node1', NULL, 'locker', 'rental', 'trusted', 'AVAILABLE', '{}')"
    )
    setup_conn.close()

    conn = connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(files)
        row = conn.execute("SELECT * FROM node WHERE node_id = 'node1'").fetchone()
        assert row["name"] is None  # 기존 행의 이름은 NULL이다
        assert (row["capability"], row["trust_state"], row["device_state"]) == ("locker", "trusted", "AVAILABLE")
        conn.execute("UPDATE node SET name = '사물함 1' WHERE node_id = 'node1'")
        assert conn.execute("SELECT name FROM node").fetchone()["name"] == "사물함 1"
    finally:
        conn.close()
