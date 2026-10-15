"""`AdminRepository.start_collection`·`end_collection` (specs/db-admin-repository/spec.md, FR-16, #131)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import BeaconKey, RssiFrame

from conftest import insert_beacon, insert_node, insert_place, insert_user

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = BeaconKey(uuid="UUID-1", major=1, minor=1)


def _setup(conn, beacon_id="b1", node_id="node1"):
    insert_user(conn, user_id="u1")
    insert_place(conn)
    insert_node(conn, node_id=node_id)
    insert_beacon(conn, beacon_id=beacon_id, user_id="u1", uuid=KEY.uuid, major=KEY.major, minor=KEY.minor)


def test_FR16_start_collection_applied(admin, conn):
    _setup(conn)

    result = admin.start_collection("c1", KEY, "node1", NOW)

    assert result.outcome == AdminWriteOutcome.APPLIED
    row = conn.execute("SELECT beacon_id, label, started_at, ended_at FROM collection_run WHERE collection_id = 'c1'").fetchone()
    assert row["beacon_id"] == "b1"
    assert row["label"] == "node1"
    assert row["ended_at"] is None


def test_FR16_start_collection_overlap_open(admin, conn):
    _setup(conn)
    admin.start_collection("c1", KEY, "node1", NOW)

    result = admin.start_collection("c2", KEY, "node1", NOW + timedelta(minutes=1))

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.COLLECTION_OVERLAP


def test_FR16_start_collection_overlap_closed(admin, conn):
    _setup(conn)
    admin.start_collection("c1", KEY, "node1", NOW)
    admin.end_collection("c1", NOW + timedelta(hours=1))

    result = admin.start_collection("c2", KEY, "node1", NOW + timedelta(minutes=30))

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.COLLECTION_OVERLAP


def test_FR16_start_collection_touching_ok(admin, conn):
    _setup(conn)
    admin.start_collection("c1", KEY, "node1", NOW)
    admin.end_collection("c1", NOW + timedelta(hours=1))

    result = admin.start_collection("c2", KEY, "node1", NOW + timedelta(hours=1))

    assert result.outcome == AdminWriteOutcome.APPLIED


def test_FR16_start_collection_beacon_missing(admin, conn):
    insert_user(conn, user_id="u1")
    insert_place(conn)
    insert_node(conn, node_id="node1")

    result = admin.start_collection("c1", KEY, "node1", NOW)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.BEACON_NOT_FOUND


def test_FR16_start_collection_label_missing(admin, conn):
    _setup(conn)

    result = admin.start_collection("c1", KEY, "no-such-node", NOW)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.NODE_NOT_FOUND


def test_FR16_start_collection_label_none(admin, conn):
    _setup(conn)

    result = admin.start_collection("c1", KEY, "none", NOW)

    assert result.outcome == AdminWriteOutcome.APPLIED


def test_FR16_start_collection_duplicate_id(admin, conn):
    _setup(conn)
    admin.start_collection("c1", KEY, "node1", NOW)
    admin.end_collection("c1", NOW + timedelta(hours=1))

    result = admin.start_collection("c1", KEY, "node1", NOW + timedelta(hours=2))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT


def test_NFR03_start_collection_concurrent(tmp_path):
    db_path = str(tmp_path / "collection.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn, user_id="u1")
    insert_place(setup_conn)
    insert_node(setup_conn, node_id="node1")
    insert_beacon(setup_conn, beacon_id="b1", user_id="u1", uuid=KEY.uuid, major=KEY.major, minor=KEY.minor)
    setup_conn.close()

    results: list[AdminWriteOutcome] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt(collection_id: str) -> None:
        conn = connect(db_path)
        try:
            repo = SqliteAdminRepository(conn)
            barrier.wait()
            result = repo.start_collection(collection_id, KEY, "node1", NOW)
            with lock:
                results.append(result.outcome)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(f"c{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(AdminWriteOutcome.APPLIED) == 1
    check_conn = connect(db_path)
    assert check_conn.execute("SELECT COUNT(*) AS c FROM collection_run").fetchone()["c"] == 1
    check_conn.close()


def test_FR16_end_collection_applied(admin, conn):
    _setup(conn)
    admin.start_collection("c1", KEY, "node1", NOW)

    result = admin.end_collection("c1", NOW + timedelta(hours=1))

    assert result.outcome == AdminWriteOutcome.APPLIED
    row = conn.execute("SELECT ended_at FROM collection_run WHERE collection_id = 'c1'").fetchone()
    assert row["ended_at"] is not None


def test_FR16_end_collection_invalid_interval(admin, conn):
    _setup(conn)
    admin.start_collection("c1", KEY, "node1", NOW)

    result = admin.end_collection("c1", NOW)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.INVALID_INTERVAL


def test_FR16_end_collection_missing(admin, conn):
    result = admin.end_collection("no-such-collection", NOW)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.COLLECTION_NOT_FOUND


def test_FR16_end_collection_already_ended(admin, conn):
    _setup(conn)
    admin.start_collection("c1", KEY, "node1", NOW)
    admin.end_collection("c1", NOW + timedelta(hours=1))

    result = admin.end_collection("c1", NOW + timedelta(hours=2))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT


def test_FR16_collection_links_observations(admin, conn):
    _setup(conn)
    repo = SqliteRepository(conn)
    admin.start_collection("c1", KEY, "node1", NOW)

    def _frame(frame_ts, seq):
        return RssiFrame(
            node_id="node1", beacon=KEY, rssi=-60.0, frame_ts=frame_ts, session_id="sess1", seq=seq,
            sent_at=frame_ts, received_at=frame_ts,
        )

    repo.save_rssi(_frame(NOW + timedelta(minutes=10), seq=1))  # 구간 안
    admin.end_collection("c1", NOW + timedelta(hours=1))
    repo.save_rssi(_frame(NOW + timedelta(minutes=30), seq=2))  # 종료 뒤 늦게 왔지만 구간 안
    repo.save_rssi(_frame(NOW + timedelta(hours=2), seq=3))  # 구간 밖

    rows = {
        r["seq"]: r["collection_id"]
        for r in conn.execute("SELECT seq, collection_id FROM rssi_observation")
    }
    assert rows[1] == "c1"
    assert rows[2] == "c1"
    assert rows[3] is None
