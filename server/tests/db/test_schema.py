"""스키마 제약 (FR-01). `AdminRepository`가 아직 없어 제약을 SQL로 직접 검증한다."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import insert_beacon, insert_user


def test_FR01_beacon_unique(conn):
    insert_user(conn, user_id="u1", token_hash="hash-1")
    insert_user(conn, user_id="u2", token_hash="hash-2")
    insert_beacon(conn, beacon_id="b1", user_id="u1", uuid="UUID-1", major=1, minor=1)

    with pytest.raises(sqlite3.IntegrityError):
        insert_beacon(conn, beacon_id="b2", user_id="u2", uuid="UUID-1", major=1, minor=1)


def test_FR01_one_active_beacon(conn):
    insert_user(conn, user_id="u1", token_hash="hash-1")
    insert_beacon(conn, beacon_id="b1", user_id="u1", uuid="UUID-1", is_active=1)

    with pytest.raises(sqlite3.IntegrityError):
        insert_beacon(conn, beacon_id="b2", user_id="u1", uuid="UUID-2", is_active=1)

    # 비활성 등록은 막지 않는다.
    insert_beacon(conn, beacon_id="b3", user_id="u1", uuid="UUID-3", is_active=0)
    count = conn.execute("SELECT COUNT(*) AS c FROM beacon WHERE user_id = 'u1'").fetchone()["c"]
    assert count == 2
