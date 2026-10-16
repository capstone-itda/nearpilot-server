"""평가 도구의 읽기 전용 DB 연결 (specs/db-evaluation-foundation/spec.md)."""

from __future__ import annotations

import sqlite3

import pytest

from nearpilot.db.schema import connect as write_connect

from tools.evaluation.db import EXPECTED_SCHEMA_VERSION, SchemaVersionMismatch, open_readonly


def _make_db(path: str, *, user_version: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    conn.close()


def test_NFR13_opens_connection_readonly(tmp_path):
    db_path = str(tmp_path / "eval.db")
    _make_db(db_path, user_version=EXPECTED_SCHEMA_VERSION)

    conn = open_readonly(db_path)
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_NFR13_readonly_connection_rejects_writes(tmp_path):
    db_path = str(tmp_path / "eval.db")
    _make_db(db_path, user_version=EXPECTED_SCHEMA_VERSION)

    conn = open_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO sample (id) VALUES (1)")
    finally:
        conn.close()

    check_conn = sqlite3.connect(db_path)
    count = check_conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
    check_conn.close()
    assert count == 0


def test_NFR13_schema_version_mismatch_is_rejected(tmp_path):
    db_path = str(tmp_path / "eval.db")
    _make_db(db_path, user_version=EXPECTED_SCHEMA_VERSION + 1)

    with pytest.raises(SchemaVersionMismatch):
        open_readonly(db_path)


def test_NFR13_missing_db_file_is_rejected(tmp_path):
    missing_path = str(tmp_path / "does-not-exist.db")

    with pytest.raises(sqlite3.OperationalError):
        open_readonly(missing_path)


def test_NFR13_expected_schema_version_matches_migrations(tmp_path):
    """`db/schema`의 마이그레이션 수와 이 도구의 `EXPECTED_SCHEMA_VERSION`이 어긋나지 않는다."""
    db_path = str(tmp_path / "migrated.db")
    conn = write_connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert version == EXPECTED_SCHEMA_VERSION
