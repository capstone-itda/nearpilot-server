"""`AdminRepository.start_collection`의 비활성 비콘 거부 (specs/db-collection-active-beacon/spec.md, #138)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import BeaconKey, BeaconRegistration

from conftest import insert_beacon, insert_node, insert_place, insert_user

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = BeaconKey(uuid="UUID-1", major=1, minor=1)


def _setup(conn, is_active=1):
    insert_user(conn, user_id="u1")
    insert_place(conn)
    insert_node(conn, node_id="node1")
    insert_beacon(
        conn, beacon_id="b1", user_id="u1", uuid=KEY.uuid, major=KEY.major, minor=KEY.minor,
        is_active=is_active,
    )


def test_FR16_start_collection_inactive_beacon_rejected(admin, conn):
    _setup(conn, is_active=0)

    result = admin.start_collection("c1", KEY, "node1", NOW)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.BEACON_NOT_FOUND
    assert conn.execute("SELECT COUNT(*) AS c FROM collection_run").fetchone()["c"] == 0


def test_FR16_start_collection_active_beacon_applied(admin, conn):
    _setup(conn, is_active=1)

    result = admin.start_collection("c1", KEY, "node1", NOW)

    assert result.outcome == AdminWriteOutcome.APPLIED


def test_FR16_start_collection_after_beacon_swap(admin, conn):
    _setup(conn, is_active=1)
    # 새 비콘을 활성으로 등록하면 같은 계정의 기존 활성 비콘(b1)이 꺼진다 (FR-01).
    admin.register_beacon(
        BeaconRegistration(beacon_id="b2", user_id="u1", beacon=BeaconKey("UUID-2", 1, 1), is_active=True)
    )

    result = admin.start_collection("c1", KEY, "node1", NOW)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.BEACON_NOT_FOUND
    assert conn.execute("SELECT COUNT(*) AS c FROM collection_run").fetchone()["c"] == 0


def test_FR16_end_collection_after_beacon_deactivated(admin, conn):
    _setup(conn, is_active=1)
    admin.start_collection("c1", KEY, "node1", NOW)
    admin.register_beacon(
        BeaconRegistration(beacon_id="b2", user_id="u1", beacon=BeaconKey("UUID-2", 1, 1), is_active=True)
    )  # b1이 도중에 꺼진다.

    result = admin.end_collection("c1", NOW + timedelta(hours=1))

    assert result.outcome == AdminWriteOutcome.APPLIED
    row = conn.execute("SELECT ended_at FROM collection_run WHERE collection_id = 'c1'").fetchone()
    assert row["ended_at"] is not None
