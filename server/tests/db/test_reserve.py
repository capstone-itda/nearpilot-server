"""`reserve_if_free`: 원자적 예약, 멱등성, 재조작, 동시성 (FR-19, FR-21, NFR-03)."""

from __future__ import annotations

import threading

import pytest

from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import Action, ReserveOutcome
from nearpilot.shared.models import ReserveRequest

from conftest import insert_approval, insert_node, insert_place, insert_policy, insert_request, insert_use_session, insert_user


def _seed_request(conn, internal_request_id, **kwargs):
    kwargs.setdefault("request_id", f"ext-{internal_request_id}")
    insert_request(conn, internal_request_id=internal_request_id, **kwargs)


def _request(internal_request_id="req1", user_id="u1", node_id="node1", approval_id=None, action=Action.OPEN):
    return ReserveRequest(
        internal_request_id=internal_request_id, user_id=user_id, node_id=node_id,
        action=action, approval_id=approval_id,
    )


def _setup_rental(conn, node_id="node1", exclusive_group=None, device_state="AVAILABLE"):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id=node_id, occupancy="rental", device_state=device_state, exclusive_group=exclusive_group)


def test_FR19_reserve_new(conn, repo):
    _setup_rental(conn)
    _seed_request(conn, internal_request_id="req1")

    result = repo.reserve_if_free(_request())

    assert result.outcome == ReserveOutcome.RESERVED
    assert result.use_id is not None
    row = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert row["device_state"] == "RESERVED"


def test_FR19_reserve_idempotent(conn, repo):
    _setup_rental(conn)
    _seed_request(conn, internal_request_id="req1")

    first = repo.reserve_if_free(_request())
    second = repo.reserve_if_free(_request())

    assert second.outcome == ReserveOutcome.RESERVED
    assert second.use_id == first.use_id
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 1


def test_FR19_reserve_not_available(conn, repo):
    _setup_rental(conn, device_state="FAULT")
    _seed_request(conn, internal_request_id="req1")

    result = repo.reserve_if_free(_request())

    assert result.outcome == ReserveOutcome.DEVICE_BUSY
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 0


def test_FR19_reserve_user_busy(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", exclusive_group="group-a")
    insert_node(conn, node_id="node2", occupancy="rental", exclusive_group="group-a", device_state="ACTIVE")
    _seed_request(conn, internal_request_id="other-req", node_id="node2")
    insert_use_session(conn, use_id="other-use", internal_request_id="other-req", node_id="node2", state="ACTIVE")
    _seed_request(conn, internal_request_id="req1", node_id="node1")

    result = repo.reserve_if_free(_request(node_id="node1"))

    assert result.outcome == ReserveOutcome.USER_BUSY
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session WHERE node_id = 'node1'").fetchone()["c"] == 0


def test_FR19_reserve_reoperating(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    _seed_request(conn, internal_request_id="rental-req")
    insert_use_session(conn, use_id="use1", internal_request_id="rental-req", state="ACTIVE")
    _seed_request(conn, internal_request_id="reop-req")

    result = repo.reserve_if_free(_request(internal_request_id="reop-req"))

    assert result.outcome == ReserveOutcome.REOPERATING
    assert result.use_id == "use1"
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 1


def test_FR19_reserve_reoperating_reserves_new_usage(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_policy(conn, max_uses=None)
    insert_approval(conn, approval_id="appr1", internal_request_id="rental-req")
    _seed_request(conn, internal_request_id="rental-req")
    insert_use_session(conn, use_id="use1", internal_request_id="rental-req", state="ACTIVE")
    _seed_request(conn, internal_request_id="reop-req")

    result = repo.reserve_if_free(_request(internal_request_id="reop-req", approval_id="appr1"))

    assert result.outcome == ReserveOutcome.REOPERATING
    assert result.usage_id is not None
    usage = conn.execute(
        "SELECT internal_request_id FROM approval_usage WHERE usage_id = ?", (result.usage_id,)
    ).fetchone()
    assert usage["internal_request_id"] == "reop-req"


def test_FR19_reserve_approval_exhausted(conn, repo):
    _setup_rental(conn)
    insert_policy(conn, max_uses=1)
    insert_approval(conn, approval_id="appr1", internal_request_id="prior-req")
    _seed_request(conn, internal_request_id="prior-req")
    conn.execute(
        "INSERT INTO approval_usage (usage_id, approval_id, internal_request_id, command_id, state) "
        "VALUES ('usage-prior', 'appr1', 'prior-req', NULL, 'consumed')"
    )
    _seed_request(conn, internal_request_id="req1")

    result = repo.reserve_if_free(_request(approval_id="appr1"))

    assert result.outcome == ReserveOutcome.APPROVAL_EXHAUSTED
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 0


def test_FR19_reserve_shared_open_no_approval_writes_nothing(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared", device_state="AVAILABLE")
    _seed_request(conn, internal_request_id="req1", node_id="light1")

    result = repo.reserve_if_free(_request(node_id="light1", action=Action.ON))

    assert result.outcome == ReserveOutcome.RESERVED
    assert result.use_id is None
    assert result.usage_id is None
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM approval_usage").fetchone()["c"] == 0


def test_FR19_reserve_shared_with_approval_reserves_usage(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    insert_policy(conn, node_id="light1", max_uses=None)
    insert_approval(conn, approval_id="appr1", node_id="light1", internal_request_id="other")
    _seed_request(conn, internal_request_id="req1", node_id="light1")

    result = repo.reserve_if_free(_request(node_id="light1", approval_id="appr1", action=Action.ON))

    assert result.outcome == ReserveOutcome.RESERVED
    assert result.use_id is None
    assert result.usage_id is not None


def test_FR19_reserve_missing_node_raises(conn, repo):
    insert_user(conn)
    _seed_request(conn, internal_request_id="req1", node_id=None)

    with pytest.raises(LookupError):
        repo.reserve_if_free(_request(node_id="missing"))


def test_FR19_reserve_concurrent(tmp_path):
    db_path = str(tmp_path / "reserve.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn, user_id="u1", token_hash="hash-1")
    insert_user(setup_conn, user_id="u2", token_hash="hash-2")
    insert_place(setup_conn)
    insert_node(setup_conn, node_id="node1", occupancy="rental")
    insert_request(setup_conn, internal_request_id="req-a", user_id="u1")
    insert_request(setup_conn, internal_request_id="req-b", user_id="u2")
    setup_conn.close()

    results: list[ReserveOutcome] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt(user_id: str, internal_request_id: str) -> None:
        conn = connect(db_path)
        try:
            repository = SqliteRepository(conn)
            barrier.wait()
            result = repository.reserve_if_free(
                ReserveRequest(
                    internal_request_id=internal_request_id, user_id=user_id, node_id="node1",
                    action=Action.OPEN, approval_id=None,
                )
            )
            with lock:
                results.append(result.outcome)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=attempt, args=("u1", "req-a")),
        threading.Thread(target=attempt, args=("u2", "req-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == sorted([ReserveOutcome.RESERVED, ReserveOutcome.DEVICE_BUSY])
    check_conn = connect(db_path)
    assert check_conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 1
    check_conn.close()


def test_NFR03_reserve_stress(tmp_path):
    db_path = str(tmp_path / "stress.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn, user_id="owner", token_hash="hash-owner")
    insert_place(setup_conn, owner_user_id="owner")
    insert_node(setup_conn, node_id="node1", occupancy="rental")
    for i in range(20):
        insert_user(setup_conn, user_id=f"user-{i}", token_hash=f"hash-{i}")
        insert_request(setup_conn, internal_request_id=f"req-{i}", user_id=f"user-{i}")
    setup_conn.close()

    results: list[ReserveOutcome] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def attempt(i: int) -> None:
        conn = connect(db_path)
        try:
            repository = SqliteRepository(conn)
            barrier.wait()
            result = repository.reserve_if_free(
                ReserveRequest(
                    internal_request_id=f"req-{i}", user_id=f"user-{i}", node_id="node1",
                    action=Action.OPEN, approval_id=None,
                )
            )
            with lock:
                results.append(result.outcome)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(ReserveOutcome.RESERVED) == 1
    assert results.count(ReserveOutcome.DEVICE_BUSY) == 19
    check_conn = connect(db_path)
    assert check_conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 1
    check_conn.close()


def test_NFR03_threads(tmp_path):
    """스레드마다 연결을 따로 쓴다. 쓰기 경합은 busy_timeout 안에서 기다리고, 예외 없이 끝난다."""
    db_path = str(tmp_path / "threads.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn, user_id="owner", token_hash="hash-owner")
    insert_place(setup_conn, owner_user_id="owner")
    insert_node(setup_conn, node_id="node1", occupancy="rental", last_seen_at="2020-01-01T00:00:00+00:00")
    setup_conn.close()

    errors: list[BaseException] = []
    lock = threading.Lock()

    def touch(seen_at: str) -> None:
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE node SET last_seen_at = ? WHERE node_id = 'node1' AND last_seen_at < ?",
                (seen_at, seen_at),
            )
        except BaseException as exc:  # noqa: BLE001 - 스레드 예외를 모아서 테스트에서 검사한다
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=touch, args=(f"2026-0{i + 1}-01T00:00:00+00:00",)) for i in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    check_conn = connect(db_path)
    row = check_conn.execute("SELECT last_seen_at FROM node WHERE node_id = 'node1'").fetchone()
    assert row["last_seen_at"] == "2026-05-01T00:00:00+00:00"
    check_conn.close()
