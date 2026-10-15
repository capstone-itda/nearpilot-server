"""`AdminRepository.register_beacon` (specs/db-admin-repository/spec.md, FR-01)."""

from __future__ import annotations

import threading

from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import BeaconKey, BeaconRegistration

from conftest import insert_beacon, insert_user


def _reg(beacon_id="b1", user_id="u1", uuid="UUID-1", major=1, minor=1, is_active=True):
    return BeaconRegistration(
        beacon_id=beacon_id, user_id=user_id, beacon=BeaconKey(uuid=uuid, major=major, minor=minor),
        is_active=is_active,
    )


def test_FR01_register_beacon_swaps_active(admin, conn):
    insert_user(conn, user_id="u1")
    insert_beacon(conn, beacon_id="old", user_id="u1", uuid="UUID-OLD", is_active=1)

    result = admin.register_beacon(_reg(beacon_id="new", uuid="UUID-NEW"))

    assert result.outcome == AdminWriteOutcome.APPLIED
    rows = {r["beacon_id"]: r["is_active"] for r in conn.execute("SELECT beacon_id, is_active FROM beacon")}
    assert rows == {"old": 0, "new": 1}


def test_FR01_register_beacon_inactive_keeps_active(admin, conn):
    insert_user(conn, user_id="u1")
    insert_beacon(conn, beacon_id="old", user_id="u1", uuid="UUID-OLD", is_active=1)

    result = admin.register_beacon(_reg(beacon_id="new", uuid="UUID-NEW", is_active=False))

    assert result.outcome == AdminWriteOutcome.APPLIED
    rows = {r["beacon_id"]: r["is_active"] for r in conn.execute("SELECT beacon_id, is_active FROM beacon")}
    assert rows == {"old": 1, "new": 0}


def test_FR01_register_beacon_taken_by_other(admin, conn):
    insert_user(conn, user_id="u1", token_hash="hash-1")
    insert_user(conn, user_id="u2", token_hash="hash-2")
    insert_beacon(conn, beacon_id="b-u1", user_id="u1", uuid="UUID-SAME", is_active=1)

    result = admin.register_beacon(_reg(beacon_id="b-u2", user_id="u2", uuid="UUID-SAME"))

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.BEACON_ALREADY_REGISTERED
    row = conn.execute("SELECT is_active FROM beacon WHERE beacon_id = 'b-u1'").fetchone()
    assert row["is_active"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM beacon WHERE beacon_id = 'b-u2'").fetchone()["c"] == 0


def test_FR01_register_beacon_account_missing(admin, conn):
    result = admin.register_beacon(_reg())

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.ACCOUNT_NOT_FOUND
    assert conn.execute("SELECT COUNT(*) AS c FROM beacon").fetchone()["c"] == 0


def test_FR01_register_beacon_reactivate(admin, conn):
    insert_user(conn, user_id="u1")
    insert_beacon(conn, beacon_id="b1", user_id="u1", uuid="UUID-1", is_active=0)
    insert_beacon(conn, beacon_id="b2", user_id="u1", uuid="UUID-2", is_active=1)

    result = admin.register_beacon(_reg(beacon_id="b1", uuid="UUID-1", is_active=True))

    assert result.outcome == AdminWriteOutcome.APPLIED
    rows = {r["beacon_id"]: r["is_active"] for r in conn.execute("SELECT beacon_id, is_active FROM beacon")}
    assert rows == {"b1": 1, "b2": 0}


def test_FR01_register_beacon_same_id_other_account(admin, conn):
    insert_user(conn, user_id="u1", token_hash="hash-1")
    insert_user(conn, user_id="u2", token_hash="hash-2")
    insert_beacon(conn, beacon_id="b1", user_id="u1", uuid="UUID-1", is_active=1)

    result = admin.register_beacon(_reg(beacon_id="b1", user_id="u2", uuid="UUID-1"))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT
    row = conn.execute("SELECT user_id FROM beacon WHERE beacon_id = 'b1'").fetchone()
    assert row["user_id"] == "u1"


def test_FR01_register_beacon_same_id_other_key(admin, conn):
    insert_user(conn, user_id="u1")
    insert_beacon(conn, beacon_id="b1", user_id="u1", uuid="UUID-1", is_active=1)

    result = admin.register_beacon(_reg(beacon_id="b1", uuid="UUID-DIFFERENT"))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT
    row = conn.execute("SELECT uuid FROM beacon WHERE beacon_id = 'b1'").fetchone()
    assert row["uuid"] == "UUID-1"


def test_FR01_register_beacon_same_account_new_id(admin, conn):
    insert_user(conn, user_id="u1")
    insert_beacon(conn, beacon_id="b1", user_id="u1", uuid="UUID-1", is_active=1)

    result = admin.register_beacon(_reg(beacon_id="b2", uuid="UUID-1"))

    assert result.outcome == AdminWriteOutcome.CONFLICT
    assert result.reason == AdminWriteReason.STATE_CONFLICT
    assert conn.execute("SELECT COUNT(*) AS c FROM beacon WHERE beacon_id = 'b2'").fetchone()["c"] == 0


def test_NFR03_register_beacon_concurrent_one_active(tmp_path):
    db_path = str(tmp_path / "beacon.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn, user_id="u1")
    setup_conn.close()

    barrier = threading.Barrier(2)

    def attempt(beacon_id: str, uuid: str) -> None:
        conn = connect(db_path)
        try:
            repo = SqliteAdminRepository(conn)
            barrier.wait()
            repo.register_beacon(_reg(beacon_id=beacon_id, uuid=uuid, is_active=True))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=attempt, args=("b1", "UUID-1")),
        threading.Thread(target=attempt, args=("b2", "UUID-2")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check_conn = connect(db_path)
    active_count = check_conn.execute(
        "SELECT COUNT(*) AS c FROM beacon WHERE user_id = 'u1' AND is_active = 1"
    ).fetchone()["c"]
    assert active_count == 1
    check_conn.close()
