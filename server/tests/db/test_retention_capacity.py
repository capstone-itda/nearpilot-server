"""`RetentionStore.cleanup`와 `link_request`의 한도 검사 (specs/db-retention-capacity/spec.md,
FR-18, FR-19, FR-21, FR-22, NFR-03)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.db.audit.jsonl import JsonlExporter
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.repositories.retention import SqliteRetentionStore
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, ReserveOutcome
from nearpilot.shared.errors import StoreNotApplied
from nearpilot.shared.models import ReserveRequest, TargetSpec, UseRequest

from conftest import (
    insert_audit_log,
    insert_beacon,
    insert_node,
    insert_node_command,
    insert_place,
    insert_recovery,
    insert_request,
    insert_use_session,
    insert_user,
)


def _use_request(user_id="u1", request_id="ext-1", kind="nearest", capability="locker", action=Action.OPEN):
    return UseRequest(
        user_id=user_id,
        request_id=request_id,
        target=TargetSpec.parse(f"{kind}:{capability}"),
        action=action,
        host_kind=None,
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ── RetentionStore.cleanup: use_session 삭제와 참조 분리 ────────────


def test_FR22_cleanup_skips_session_with_unresolved_recovery(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="FAULT")
    insert_request(conn, internal_request_id="req1", verdict="OK", reason_code="OK", node_id="node1")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", state="CLOSED",
        end_reason="fault", closed_at="2026-01-01T00:00:00+00:00",
    )
    insert_recovery(conn, recovery_id="rec1", node_id="node1", use_id="use1", completed_at=None)

    retention = SqliteRetentionStore(conn, settings=Settings(use_history_retention_days=1))
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.deleted_sessions == 0
    assert conn.execute("SELECT 1 FROM use_session WHERE use_id = 'use1'").fetchone() is not None


def test_FR22_cleanup_skips_in_progress_session(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_request(conn, internal_request_id="req1", verdict="OK", reason_code="OK", node_id="node1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE", closed_at=None)

    retention = SqliteRetentionStore(conn, settings=Settings(use_history_retention_days=1))
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.deleted_sessions == 0
    assert conn.execute("SELECT 1 FROM use_session WHERE use_id = 'use1'").fetchone() is not None


def test_FR21_cleanup_deletes_eligible_session_and_detaches_commands(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_request(conn, internal_request_id="req1", verdict="OK", reason_code="OK", node_id="node1")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", state="CLOSED",
        end_reason="release", closed_at="2026-01-01T00:00:00+00:00",
    )
    insert_node_command(conn, command_id="cmd1", internal_request_id="req1", node_id="node1", use_id="use1")
    insert_recovery(
        conn, recovery_id="rec1", node_id="node1", use_id="use1",
        created_at="2026-01-01T00:00:00+00:00", completed_at="2026-01-01T00:05:00+00:00",
    )

    retention = SqliteRetentionStore(conn, settings=Settings(use_history_retention_days=1))
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.deleted_sessions == 1
    assert conn.execute("SELECT 1 FROM use_session WHERE use_id = 'use1'").fetchone() is None
    command_row = conn.execute("SELECT use_id FROM node_command WHERE command_id = 'cmd1'").fetchone()
    assert command_row is not None
    assert command_row["use_id"] is None
    recovery_row = conn.execute("SELECT use_id FROM recovery WHERE recovery_id = 'rec1'").fetchone()
    assert recovery_row is not None
    assert recovery_row["use_id"] is None


# ── RetentionStore.cleanup: 요청의 "완료" 조건과 request_id 비우기 ──


def test_FR22_cleanup_skips_unfinished_request(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", verdict="OK", reason_code="OK",
        node_id="node1", received_at="2020-01-01T00:00:00+00:00",
    )
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE", closed_at=None)
    insert_audit_log(conn, internal_request_id="req1", step="exec", created_at="2020-01-01T00:00:00+00:00")

    retention = SqliteRetentionStore(conn, settings=Settings(request_dedup_ttl_sec=1))
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.cleared_requests == 0
    row = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()
    assert row["request_id"] == "ext-1"


def test_FR22_cleanup_keeps_request_id_of_retried_request_with_active_session(conn):
    """취소한 세션과 재판정의 새 ACTIVE 세션이 함께 있으면 완료가 아니다 (#76, #80)."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", verdict="OK", reason_code="OK",
        node_id="node1", received_at="2020-01-01T00:00:00+00:00",
    )
    insert_use_session(conn, use_id="use-old", internal_request_id="req1", state="CLOSED",
                       end_reason="cancelled", closed_at="2020-01-01T00:00:01+00:00")
    insert_use_session(conn, use_id="use-new", internal_request_id="req1", state="ACTIVE", closed_at=None)
    insert_audit_log(conn, internal_request_id="req1", step="exec", created_at="2020-01-01T00:00:00+00:00")

    retention = SqliteRetentionStore(conn, settings=Settings(request_dedup_ttl_sec=1))
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.cleared_requests == 0
    row = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()
    assert row["request_id"] == "ext-1"


def test_FR22_cleanup_clears_request_id_after_dedup_ttl(conn):
    insert_user(conn)
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", verdict="DENY", reason_code="LOW_CONFIDENCE",
        received_at="2026-01-01T00:00:00+00:00",
    )
    insert_audit_log(conn, internal_request_id="req1", step="3", created_at="2026-01-01T00:00:00+00:00")

    retention = SqliteRetentionStore(conn, settings=Settings(request_dedup_ttl_sec=1200))
    now = datetime(2026, 1, 1, 0, 25, 0, tzinfo=timezone.utc)  # 1500초 뒤
    report = retention.cleanup(now)

    assert report.cleared_requests == 1
    row = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()
    assert row["request_id"] is None
    assert conn.execute("SELECT 1 FROM request WHERE internal_request_id = 'req1'").fetchone() is not None


def test_FR22_cleanup_keeps_request_id_within_ttl(conn):
    insert_user(conn)
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", verdict="DENY", reason_code="LOW_CONFIDENCE",
        received_at="2026-01-01T00:00:00+00:00",
    )
    insert_audit_log(conn, internal_request_id="req1", step="3", created_at="2026-01-01T00:00:00+00:00")

    retention = SqliteRetentionStore(conn, settings=Settings(request_dedup_ttl_sec=1200))
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)  # 60초 뒤
    report = retention.cleanup(now)

    assert report.cleared_requests == 0
    row = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()
    assert row["request_id"] == "ext-1"


def test_FR22_cleared_request_id_links_as_new_request(conn, repo):
    insert_user(conn)
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", input_digest="digest-1", verdict="OK",
    )
    conn.execute("UPDATE request SET request_id = NULL WHERE internal_request_id = 'req1'")

    result = repo.link_request(_use_request(request_id="ext-1"), "digest-1")

    assert result.is_new is True
    assert result.conflict is False
    assert result.internal_request_id != "req1"
    row = conn.execute(
        "SELECT request_id FROM request WHERE internal_request_id = ?", (result.internal_request_id,)
    ).fetchone()
    assert row["request_id"] == "ext-1"


# ── RetentionStore.cleanup: audit_log 삭제와 JSONL 확인 ─────────────


def test_FR22_cleanup_skips_unexported_audit_rows(conn, tmp_path):
    insert_audit_log(conn, internal_request_id=None, step="auth", created_at="2020-01-01T00:00:00+00:00")
    jsonl_path = tmp_path / "audit.jsonl"
    jsonl_path.write_text("")  # 아직 아무 줄도 내보내지 않았다

    retention = SqliteRetentionStore(
        conn, settings=Settings(audit_retention_days=1), jsonl_path=str(jsonl_path)
    )
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.deleted_audit_rows == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 1


def test_FR22_cleanup_skips_audit_retention_without_jsonl(conn):
    insert_audit_log(conn, internal_request_id=None, step="auth", created_at="2020-01-01T00:00:00+00:00")

    retention = SqliteRetentionStore(conn, settings=Settings(audit_retention_days=1), jsonl_path=None)
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.deleted_audit_rows == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 1


def test_FR22_cleanup_deletes_exported_audit_rows_past_retention(conn, tmp_path):
    insert_audit_log(conn, log_id="log-old", event_id="evt-old", step="auth", created_at="2020-01-01T00:00:00+00:00")
    insert_audit_log(conn, log_id="log-new", event_id="evt-new", step="auth", created_at="2026-01-30T00:00:00+00:00")
    jsonl_path = tmp_path / "audit.jsonl"
    jsonl_path.write_text("\n".join(json.dumps({"seq": seq}) for seq in (1, 2)) + "\n")

    retention = SqliteRetentionStore(
        conn, settings=Settings(audit_retention_days=1), jsonl_path=str(jsonl_path)
    )
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.deleted_audit_rows == 1
    assert conn.execute("SELECT 1 FROM audit_log WHERE log_id = 'log-old'").fetchone() is None
    assert conn.execute("SELECT 1 FROM audit_log WHERE log_id = 'log-new'").fetchone() is not None


def test_FR22_cleanup_keeps_max_rowid_so_jsonl_resumes(conn, tmp_path):
    for i in range(3):
        insert_audit_log(
            conn, log_id=f"log-{i}", event_id=f"evt-{i}", step="auth",
            created_at="2020-01-01T00:00:00+00:00",
        )
    jsonl_path = tmp_path / "audit.jsonl"
    jsonl_path.write_text("\n".join(json.dumps({"seq": seq}) for seq in (1, 2, 3)) + "\n")

    retention = SqliteRetentionStore(
        conn, settings=Settings(audit_retention_days=1), jsonl_path=str(jsonl_path)
    )
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    # 가장 큰 rowid(3) 행은 보존기간이 지났어도 남는다. rowid 1, 2만 지운다.
    assert report.deleted_audit_rows == 2
    remaining = conn.execute("SELECT rowid AS rid, log_id FROM audit_log").fetchall()
    assert len(remaining) == 1
    assert remaining[0]["rid"] == 3

    # 새 감사 행을 실제 내보내기로 이어 붙인다. rowid가 재사용되지 않아 seq 4를 받는다.
    exporter = JsonlExporter(conn, str(jsonl_path))
    insert_audit_log(conn, log_id="log-new", event_id="evt-new", step="auth", created_at="2026-02-01T00:00:00+00:00")
    new_rowid = conn.execute("SELECT rowid FROM audit_log WHERE log_id = 'log-new'").fetchone()[0]
    assert new_rowid == 4
    exporter.flush()

    lines = jsonl_path.read_text().splitlines()
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == [1, 2, 3, 4]


# ── RetentionStore.cleanup: 멱등성, 수집 데이터 보존, 잠금 실패 ─────


def test_FR22_cleanup_is_idempotent(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", verdict="OK", reason_code="OK",
        node_id="node1", received_at="2026-01-01T00:00:00+00:00",
    )
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", state="CLOSED",
        end_reason="release", closed_at="2026-01-01T00:00:00+00:00",
    )
    insert_audit_log(conn, internal_request_id="req1", step="exec", created_at="2026-01-01T00:00:00+00:00")

    retention = SqliteRetentionStore(
        conn, settings=Settings(use_history_retention_days=1, request_dedup_ttl_sec=60)
    )
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)

    first = retention.cleanup(now)
    assert first.deleted_sessions == 1
    assert first.cleared_requests == 1

    second = retention.cleanup(now)
    assert second.cleared_requests == 0
    assert second.deleted_sessions == 0
    assert second.deleted_audit_rows == 0


def test_FR22_cleanup_keeps_collection_observations(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_beacon(conn, beacon_id="beacon1", user_id="u1")
    conn.execute(
        "INSERT INTO collection_run (collection_id, beacon_id, label, started_at, ended_at) "
        "VALUES ('col1', 'beacon1', 'node1', '2020-01-01T00:00:00+00:00', '2020-01-01T00:10:00+00:00')"
    )
    conn.execute(
        "INSERT INTO rssi_observation (obs_id, node_id, beacon_id, rssi, frame_ts, received_at, sent_at, "
        "session_id, seq, collection_id) VALUES ('obs1', 'node1', 'beacon1', -60.0, "
        "'2020-01-01T00:01:00+00:00', '2020-01-01T00:01:00+00:00', '2020-01-01T00:01:00+00:00', "
        "'sess1', 1, 'col1')"
    )
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", verdict="OK", reason_code="OK",
        node_id="node1", received_at="2020-01-01T00:00:00+00:00",
    )
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", state="CLOSED",
        end_reason="release", closed_at="2020-01-01T00:00:00+00:00",
    )

    retention = SqliteRetentionStore(conn, settings=Settings(use_history_retention_days=1))
    report = retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))

    assert report.deleted_sessions == 1  # 정리가 실제로 돌았다
    assert conn.execute("SELECT COUNT(*) AS n FROM collection_run").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM rssi_observation").fetchone()["n"] == 1


def test_NFR03_cleanup_lock_failure_raises_store_not_applied(tmp_path):
    db_path = str(tmp_path / "busy.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn)
    insert_place(setup_conn)
    insert_node(setup_conn, node_id="node1", occupancy="rental")
    insert_request(
        setup_conn, internal_request_id="req1", request_id="ext-1", verdict="OK", reason_code="OK",
        node_id="node1", received_at="2020-01-01T00:00:00+00:00",
    )
    insert_use_session(
        setup_conn, use_id="use1", internal_request_id="req1", state="CLOSED",
        end_reason="release", closed_at="2020-01-01T00:00:00+00:00",
    )
    setup_conn.close()

    blocker = connect(db_path)
    blocker.execute("BEGIN IMMEDIATE")
    conn = connect(db_path, busy_timeout_ms=50)
    try:
        retention = SqliteRetentionStore(conn, settings=Settings(use_history_retention_days=1))
        with pytest.raises(StoreNotApplied):
            retention.cleanup(datetime(2026, 2, 1, tzinfo=timezone.utc))
    finally:
        conn.close()
        blocker.execute("ROLLBACK")
        blocker.close()

    check_conn = connect(db_path)
    assert check_conn.execute("SELECT 1 FROM use_session WHERE use_id = 'use1'").fetchone() is not None
    check_conn.close()


# ── link_request: 중복 방지 한도(dedup_entry_limit) ──────────────────


def test_FR18_link_request_below_limit_is_unaffected(conn, repo):
    insert_user(conn)

    result = repo.link_request(_use_request(request_id="ext-1"), "digest-1")

    assert result.is_new is True
    assert result.capacity_exceeded is False
    row = conn.execute(
        "SELECT request_id FROM request WHERE internal_request_id = ?", (result.internal_request_id,)
    ).fetchone()
    assert row["request_id"] == "ext-1"


def test_FR22_link_request_capacity_exceeded_omits_request_id(conn):
    insert_user(conn)
    insert_request(conn, internal_request_id="existing", request_id="ext-existing", input_digest="d")
    repo = SqliteRepository(conn, settings=Settings(dedup_entry_limit=1))

    result = repo.link_request(_use_request(request_id="ext-new"), "digest-new")

    assert result.is_new is True
    assert result.capacity_exceeded is True
    row = conn.execute(
        "SELECT request_id FROM request WHERE internal_request_id = ?", (result.internal_request_id,)
    ).fetchone()
    assert row["request_id"] is None
    count = conn.execute("SELECT COUNT(*) AS n FROM request WHERE request_id IS NOT NULL").fetchone()["n"]
    assert count == 1  # 기존 1건에서 늘지 않았다


def test_FR22_link_request_clears_expired_ids_before_capacity_check(conn):
    insert_user(conn)
    insert_request(conn, verdict="DENY", reason_code="BEACON_NOT_SEEN")
    insert_audit_log(conn, internal_request_id="req1", created_at="2026-01-01T00:00:00+00:00")
    repo = SqliteRepository(conn, settings=Settings(dedup_entry_limit=1, request_dedup_ttl_sec=60))
    later = replace(_use_request(request_id="fresh"),
                    received_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=61))

    result = repo.link_request(later, "fresh")

    assert result.capacity_exceeded is False
    old = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()
    assert old["request_id"] is None  # 행과 감사는 남기고 외부 ID만 비운다
    assert conn.execute("SELECT COUNT(*) AS n FROM request").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 1


def test_FR22_link_request_capacity_keeps_unfinished_request_key(conn):
    insert_user(conn)
    insert_request(conn, request_id="pending", verdict=None)
    repo = SqliteRepository(conn, settings=Settings(dedup_entry_limit=1, request_dedup_ttl_sec=1))
    later = replace(_use_request(request_id="new"),
                    received_at=datetime(2026, 1, 2, tzinfo=timezone.utc))

    result = repo.link_request(later, "new")

    assert result.capacity_exceeded is True
    old = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'req1'").fetchone()
    assert old["request_id"] == "pending"


@pytest.mark.parametrize(
    ("owner", "status", "limit", "exceeded"),
    [
        ("keyed", "succeeded", 2, True),  # 요청 항목 1 + 그 요청의 명령 1 = 2
        ("cleared", "succeeded", 2, False),  # request_id를 비운 요청의 끝난 명령은 세지 않는다
        ("cleared", "pending", 2, True),  # 비운 요청이라도 진행 중 명령은 센다
        ("cleared", "in_progress", 2, True),
        ("keyed", "pending", 3, False),  # 두 조건을 모두 만족하는 명령은 한 번만 센다
    ],
)
def test_FR22_link_request_counts_command_entries(conn, owner, status, limit, exceeded):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    # 판정이 없는 진행 중 요청이라 한도 정리가 request_id를 비우지 않는다
    insert_request(conn, internal_request_id="keyed", request_id="ext-keyed", input_digest="d")
    insert_request(conn, internal_request_id="cleared", request_id=None, input_digest="d")
    insert_node_command(conn, command_id="cmd1", internal_request_id=owner, node_id="node1", status=status)
    repo = SqliteRepository(conn, settings=Settings(dedup_entry_limit=limit))

    result = repo.link_request(_use_request(request_id="ext-new"), "digest-new")

    assert result.capacity_exceeded is exceeded


def test_FR22_link_request_recount_after_clearing_counts_command_entries(conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_request(
        conn, internal_request_id="expired", request_id="ext-expired", input_digest="d",
        verdict="DENY", reason_code="BEACON_NOT_SEEN",
    )
    insert_audit_log(conn, internal_request_id="expired", created_at="2026-01-01T00:00:00+00:00")
    insert_request(conn, internal_request_id="live", request_id="ext-live", input_digest="d")
    insert_node_command(
        conn, command_id="cmd-live", internal_request_id="live", node_id="node1", status="in_progress",
    )
    repo = SqliteRepository(conn, settings=Settings(dedup_entry_limit=2, request_dedup_ttl_sec=60))
    later = replace(_use_request(request_id="new"),
                    received_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=61))

    result = repo.link_request(later, "new")

    # 만료 요청의 ID를 비워도 진행 중 요청의 항목 1 + 그 명령 1 = 2가 남아 한도 이상이다
    assert result.capacity_exceeded is True
    expired = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'expired'").fetchone()
    assert expired["request_id"] is None
    live = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'live'").fetchone()
    assert live["request_id"] == "ext-live"


def test_FR18_link_request_retransmission_ignores_limit(conn):
    insert_user(conn)
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", input_digest="digest-1", verdict=None,
    )
    repo = SqliteRepository(conn, settings=Settings(dedup_entry_limit=1))

    result = repo.link_request(_use_request(request_id="ext-1"), "digest-1")

    assert result.is_new is False
    assert result.conflict is False
    assert result.internal_request_id == "req1"


def test_FR19_link_request_reoperating_path_unaffected(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(
        conn, internal_request_id="req1", request_id="ext-1", verdict="OK", reason_code="OK", node_id="node1",
    )
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE")

    link = repo.link_request(_use_request(request_id="ext-2"), "digest-2")
    assert link.is_new is True
    assert link.capacity_exceeded is False

    reserve = repo.reserve_if_free(
        ReserveRequest(
            internal_request_id=link.internal_request_id, user_id="u1", node_id="node1",
            action=Action.OPEN, approval_id=None,
        )
    )
    assert reserve.outcome == ReserveOutcome.REOPERATING
    assert reserve.use_id == "use1"


def test_NFR03_link_request_concurrent_capacity_check(tmp_path):
    db_path = str(tmp_path / "capacity.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn)
    for i in range(4):
        insert_request(
            setup_conn, internal_request_id=f"existing{i}", request_id=f"ext-existing-{i}", input_digest="d",
        )
    setup_conn.close()

    errors: list[BaseException] = []
    results: dict[str, object] = {}
    lock = threading.Lock()

    def attempt(label: str, request_id: str) -> None:
        local_conn = connect(db_path)
        try:
            repo = SqliteRepository(local_conn, settings=Settings(dedup_entry_limit=5))
            result = repo.link_request(_use_request(request_id=request_id), f"digest-{label}")
            with lock:
                results[label] = result
        except BaseException as exc:  # noqa: BLE001 - 스레드 예외를 모아서 검사한다
            with lock:
                errors.append(exc)
        finally:
            local_conn.close()

    threads = [
        threading.Thread(target=attempt, args=("a", "ext-new-a")),
        threading.Thread(target=attempt, args=("b", "ext-new-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    exceeded_flags = sorted(r.capacity_exceeded for r in results.values())
    assert exceeded_flags == [False, True]  # 둘 중 하나만 한도를 넘긴다

    check_conn = connect(db_path)
    count = check_conn.execute("SELECT COUNT(*) AS n FROM request WHERE request_id IS NOT NULL").fetchone()["n"]
    assert count == 5  # 4(기존) + 1(한도 안에서 들어간 한 건). 6으로 넘치지 않았다
    check_conn.close()
