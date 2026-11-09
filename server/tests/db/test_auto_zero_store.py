"""`AutoZeroStore` 구현: `auto_zero` 테이블과 개정 번호 (specs/core-proximity-common-model/spec.md,
FR-16, FR-22, #252)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.db.repositories.retention import SqliteRetentionStore
from nearpilot.shared.config import Settings
from nearpilot.shared.models import AutoZero

from conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user

T0 = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _devices(conn, *node_ids: str) -> None:
    insert_user(conn)
    insert_place(conn)
    for node_id in node_ids:
        insert_node(conn, node_id=node_id)


def _save(repo, node_id: str, zero_db: float | None, seconds: int = 0) -> int:
    return repo.save_auto_zero(node_id, zero_db, "model-a", "1.0.0", _at(seconds))


def _rows(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM auto_zero ORDER BY revision").fetchall()


# ── 마이그레이션 0013 ───────────────────────────────────────────────


def test_FR22_migration_0013_auto_zero(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    columns = [(c["name"], c["type"], c["notnull"], c["pk"]) for c in conn.execute("PRAGMA table_info(auto_zero)")]
    assert columns == [
        ("revision", "INTEGER", 0, 1),
        ("node_id", "TEXT", 1, 0),
        ("zero_db", "REAL", 0, 0),
        ("vendor_model", "TEXT", 0, 0),
        ("vendor_firmware", "TEXT", 0, 0),
        ("recorded_at", "TEXT", 1, 0),
    ]
    # 개정 번호는 지운 행의 번호도 다시 쓰지 않는다 — 판정 기록이 참조하는 개정은 저절로 남는다 (FR-22).
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'auto_zero'").fetchone()["sql"]
    assert "AUTOINCREMENT" in sql
    foreign_keys = [(k["table"], k["from"], k["to"]) for k in conn.execute("PRAGMA foreign_key_list(auto_zero)")]
    assert foreign_keys == [("node", "node_id", "node_id")]
    assert "auto_zero_node" in {i["name"] for i in conn.execute("PRAGMA index_list(auto_zero)")}
    assert [c["name"] for c in conn.execute("PRAGMA index_info(auto_zero_node)")] == ["node_id", "revision"]


# ── 개정 시점의 기기별 자동 영점 ────────────────────────────────────


def test_FR22_auto_zero_revision_snapshot(conn, repo):
    _devices(conn, "a", "b")
    empty = repo.auto_zeros()
    assert (empty.revision, dict(empty.zeros)) == (0, {})  # 행이 없으면 개정 0

    assert [_save(repo, "a", 1.5), _save(repo, "b", -2), _save(repo, "a", None), _save(repo, "a", 0.5)] == [1, 2, 3, 4]

    latest = repo.auto_zeros(None)
    assert latest.revision == 4
    assert {k: (z.zero_db, z.revision) for k, z in latest.zeros.items()} == {"a": (0.5, 4), "b": (-2, 2)}

    second = repo.auto_zeros(2)
    assert second.revision == 2
    assert {k: (z.zero_db, z.revision) for k, z in second.zeros.items()} == {"a": (1.5, 1), "b": (-2, 2)}

    discarded = repo.auto_zeros(3)  # 개정 3은 a 의 버림이다. 버림 행도 담는다
    assert discarded.revision == 3
    assert discarded.zeros["a"].zero_db is None and discarded.zeros["a"].revision == 3
    assert discarded.zeros["b"].zero_db == -2

    before_any = repo.auto_zeros(0)
    assert (before_any.revision, dict(before_any.zeros)) == (0, {})

    beyond = repo.auto_zeros(100)  # 없는 개정이어도 R 을 그대로 담는다
    assert beyond.revision == 100
    assert dict(beyond.zeros) == dict(latest.zeros)

    with pytest.raises(ValueError):
        repo.auto_zeros(-1)


def test_FR22_auto_zero_snapshot_ignores_later_revisions(conn, repo):
    """같은 개정을 다시 읽으면 그 뒤에 더한 행과 상관없이 같다 — 판정 기록의 개정으로 재현한다 (FR-22)."""
    _devices(conn, "a", "b")
    _save(repo, "a", 1.0)
    first = repo.auto_zeros(1)
    _save(repo, "a", 2.0)
    _save(repo, "b", 3.0)
    assert repo.auto_zeros(1) == first


def test_FR22_auto_zero_row_fields_roundtrip(conn, repo):
    _devices(conn, "a")
    kst = timezone(timedelta(hours=9))
    recorded_at = datetime(2026, 11, 9, 9, 30, tzinfo=kst)
    revision = repo.save_auto_zero("a", -3.25, "Beacon-X", "2.1.0", recorded_at)
    discard = repo.save_auto_zero("a", None, None, None, _at(60))

    row = _rows(conn)[0]
    assert row["recorded_at"] == "2026-11-09T00:30:00+00:00"  # UTC ISO 문자열
    zeros = repo.auto_zeros(revision).zeros
    assert zeros["a"] == AutoZero("a", -3.25, "Beacon-X", "2.1.0", revision, recorded_at)
    assert zeros["a"].recorded_at.utcoffset() == timedelta(0)
    # 선언이 없는 기기의 vendor 는 NULL 이다. 버림 행도 그대로 읽는다
    assert repo.auto_zeros(discard).zeros["a"] == AutoZero("a", None, None, None, discard, _at(60))


# ── 보존 정리 ───────────────────────────────────────────────────────


def test_FR22_auto_zero_rows_survive_cleanup(conn, repo):
    _devices(conn, "a", "b")
    _save(repo, "a", 1.5, seconds=-86400 * 365 * 5)  # 5년 전
    _save(repo, "b", None, seconds=-86400 * 365 * 5)
    _save(repo, "a", 0.5, seconds=-86400 * 365 * 4)
    before = [tuple(r) for r in _rows(conn)]
    # 정리가 실제로 지우는 기록도 함께 둔다. 정리가 돌았는데 auto_zero 만 남는 것을 본다
    insert_request(conn, internal_request_id="req1", verdict="OK", reason_code="OK", node_id="a")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", node_id="a", state="CLOSED",
        end_reason="release", closed_at="2026-01-01T00:00:00+00:00",
    )

    retention = SqliteRetentionStore(
        conn, settings=Settings(use_history_retention_days=1, request_dedup_ttl_sec=1, audit_retention_days=1))
    report = retention.cleanup(datetime(2026, 11, 9, tzinfo=timezone.utc))

    assert report.deleted_sessions == 1
    assert [tuple(r) for r in _rows(conn)] == before
    assert len(before) == 3


# ── save_auto_zero ──────────────────────────────────────────────────


def test_FR16_save_auto_zero_returns_increasing_revisions(conn, repo):
    _devices(conn, "a", "b")
    revisions = [_save(repo, "a", 1.0, 0), _save(repo, "b", 2.0, 1), _save(repo, "a", None, 2), _save(repo, "a", 3.0, 3)]
    assert revisions == [1, 2, 3, 4]
    assert [(r["revision"], r["node_id"], r["zero_db"]) for r in _rows(conn)] == [
        (1, "a", 1.0), (2, "b", 2.0), (3, "a", None), (4, "a", 3.0)]
    assert not conn.in_transaction


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_FR16_save_auto_zero_rejects_non_finite_zero_db(conn, repo, bad):
    _devices(conn, "a")
    with pytest.raises(ValueError):
        _save(repo, "a", bad)
    assert _rows(conn) == []  # 행을 쓰지 않았고 개정 번호도 쓰지 않았다
    assert _save(repo, "a", 1.0) == 1
    assert not conn.in_transaction


def test_FR16_save_auto_zero_unknown_node_raises_integrity_error(conn, repo):
    _devices(conn, "a")
    with pytest.raises(sqlite3.IntegrityError):
        _save(repo, "missing", 1.0)
    assert _rows(conn) == []
    assert not conn.in_transaction  # 롤백했다. 이어서 쓸 수 있다
    assert _save(repo, "a", 1.0) == 1
