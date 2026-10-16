"""FR-03·FR-22 승인 신청과 조작의 용량·외부 키 경계."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

import nearpilot.db.schema as schema_module
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action
from nearpilot.shared.models import TargetSpec, UseRequest
from conftest import insert_audit_log, insert_request, insert_user

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def request(key, *, purpose="use", at=NOW):
    return UseRequest("u1", key, TargetSpec.parse("nearest:locker"), Action.OPEN, None, at,
                      purpose=purpose)


def test_FR03_FR22_access_keeps_external_key_at_capacity(conn):
    insert_user(conn)
    repo = SqliteRepository(conn, Settings(dedup_entry_limit=1))
    repo.link_request(request("filled"), "fill")
    access = repo.link_request(request("approval", purpose="access"), "access")
    assert access.is_new and not access.capacity_exceeded
    row = conn.execute("SELECT request_id, purpose FROM request WHERE internal_request_id = ?",
                       (access.internal_request_id,)).fetchone()
    assert tuple(row) == ("approval", "access")
    replay = repo.link_request(request("approval", purpose="access"), "access")
    assert not replay.is_new and not replay.conflict and not replay.capacity_exceeded
    assert replay.internal_request_id == access.internal_request_id


def test_FR22_access_rows_do_not_consume_operation_capacity(conn):
    insert_user(conn)
    repo = SqliteRepository(conn, Settings(dedup_entry_limit=1))
    for i in range(3):
        assert not repo.link_request(request(f"access-{i}", purpose="access"), f"a-{i}").capacity_exceeded
    first_use = repo.link_request(request("use-1"), "use-1")
    rejected_use = repo.link_request(request("use-2"), "use-2")
    assert not first_use.capacity_exceeded
    assert rejected_use.capacity_exceeded
    assert conn.execute("SELECT COUNT(*) FROM request WHERE purpose = 'access' AND request_id IS NOT NULL").fetchone()[0] == 3


def test_FR18_FR22_same_key_with_different_purpose_conflicts(conn):
    insert_user(conn)
    repo = SqliteRepository(conn)
    first = repo.link_request(request("same", purpose="access"), "same-digest")
    conflict = repo.link_request(request("same"), "same-digest")
    assert conflict.conflict and not conflict.is_new
    assert conflict.internal_request_id == first.internal_request_id
    assert conn.execute("SELECT COUNT(*) FROM request").fetchone()[0] == 1


def test_FR22_capacity_clears_expired_ids_before_rejecting(conn):
    insert_user(conn)
    insert_request(conn, verdict="DENY", reason_code="BEACON_NOT_SEEN")
    insert_audit_log(conn, internal_request_id="req1", created_at=NOW.isoformat())
    repo = SqliteRepository(conn, Settings(dedup_entry_limit=1, request_dedup_ttl_sec=60))
    linked = repo.link_request(request("fresh", at=NOW + timedelta(seconds=61)), "fresh")
    assert not linked.capacity_exceeded
    old = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()
    assert old[0] is None
    assert conn.execute("SELECT COUNT(*) FROM request").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_FR22_capacity_keeps_unfinished_request_key(conn):
    insert_user(conn)
    insert_request(conn, request_id="pending", verdict=None)
    repo = SqliteRepository(conn, Settings(dedup_entry_limit=1, request_dedup_ttl_sec=1))
    result = repo.link_request(request("new", at=NOW + timedelta(days=1)), "new")
    assert result.capacity_exceeded
    assert conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()[0] == "pending"


def test_FR22_invalid_purpose_cannot_bypass_capacity(conn):
    insert_user(conn)
    repo = SqliteRepository(conn)
    with pytest.raises(ValueError):
        repo.link_request(replace(request("invalid"), purpose="other"), "invalid")
    assert conn.execute("SELECT COUNT(*) FROM request").fetchone()[0] == 0


def test_FR22_schema6_keeps_legacy_key_and_replay(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    old = sqlite3.connect(db_path, isolation_level=None)
    migrations = sorted((Path(schema_module.__file__).parent / "sql").glob("*.sql"))
    for migration in migrations[:5]:
        old.executescript(migration.read_text(encoding="utf-8"))
    old.execute("PRAGMA user_version = 5")
    old.execute("INSERT INTO user (user_id,name,token_hash,role) VALUES ('u1','old','hash','visitor')")
    old.execute("INSERT INTO request (internal_request_id,user_id,request_id,input_digest,target_spec,action,received_at) "
                "VALUES ('legacy','u1','old-key','digest','nearest:locker','open',?)", (NOW.isoformat(),))
    old.close()
    upgraded = connect(db_path)
    try:
        repo = SqliteRepository(upgraded, Settings(dedup_entry_limit=1))
        old_request = request("old-key", purpose="access")
        replay = repo.link_request(old_request, "digest")
        assert replay.internal_request_id == "legacy" and not replay.is_new
        assert not replay.conflict and not replay.capacity_exceeded
        row = upgraded.execute("SELECT request_id,purpose FROM request WHERE internal_request_id='legacy'").fetchone()
        assert tuple(row) == ("old-key", None)
        assert repo.link_request(request("new"), "new").capacity_exceeded
    finally:
        upgraded.close()
