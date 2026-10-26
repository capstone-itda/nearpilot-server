"""상태 관리의 `ReleaseService`를 fake 없이 실제 SQLite 저장소로 돌린다.

`ExecutionStore`·`Repository`만 진짜로 바꿔 `begin_close`·`finish_close`·`close_result`가
논리적 종료 흐름에서 맞물리는지 확인한다. 반납 검사는 쓰지 않는다 (2026-10-26 결정)
(specs/db-execution-store/spec.md §5 "release.py를 실제 SQLite 저장소로 돌린다").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock, RLock

import pytest

from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import EndReason, Verdict

# tests/db/conftest.py와 같은 역할의 최소 seed 함수다. pytest는 tests/db/, tests/integration/를
# 각각 독립된 최상위 모듈로 다루므로(둘 다 __init__.py가 없다) 디렉터리를 넘는 임포트를 하지
# 않는다 (tests/db/test_reserve.py 등의 기존 관례 — bare `from conftest import ...`).


def insert_user(conn, user_id="visitor-1", token_hash="hash-1", role="visitor", name="tester"):
    conn.execute(
        "INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, name, token_hash, role),
    )


def insert_place(conn, place_id="place1", owner_user_id="visitor-1", name="place"):
    conn.execute(
        "INSERT INTO place (place_id, owner_user_id, name) VALUES (?, ?, ?)",
        (place_id, owner_user_id, name),
    )


def insert_node(
    conn, node_id="locker-1", place_id="place1", capability="locker", occupancy="rental",
    trust_state="trusted", device_state="ACTIVE", session_id=None,
):
    conn.execute(
        "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
        "exclusive_group, release_policy, session_id, session_first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, '{}', ?, NULL, NULL)",
        (node_id, place_id, capability, occupancy, trust_state, device_state, session_id),
    )


def insert_request(
    conn, internal_request_id="internal-1", user_id="visitor-1", request_id="ext-1",
    input_digest="digest-1", target_spec="nearest:locker", action="open",
    received_at="2026-01-01T00:00:00+00:00",
):
    conn.execute(
        "INSERT INTO request (internal_request_id, user_id, request_id, input_digest, "
        "parent_internal_request_id, node_id, target_spec, action, received_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
        (internal_request_id, user_id, request_id, input_digest, target_spec, action, received_at),
    )


def insert_use_session(
    conn, use_id="use-1", internal_request_id="internal-1", user_id="visitor-1",
    node_id="locker-1", state="ACTIVE", rental_due_at=None,
):
    conn.execute(
        "INSERT INTO use_session (use_id, internal_request_id, user_id, node_id, state, "
        "rental_due_at, release_policy) VALUES (?, ?, ?, ?, ?, ?, '{}')",
        (use_id, internal_request_id, user_id, node_id, state, rental_due_at),
    )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "release.db")
    conn = connect(db_path)
    insert_user(conn, user_id="visitor-1", token_hash="hash-1", role="visitor")
    insert_place(conn, owner_user_id="visitor-1")
    insert_node(
        conn, node_id="locker-1", capability="locker", occupancy="rental",
        device_state="ACTIVE", session_id="connection-1",
    )
    insert_request(conn, internal_request_id="internal-1", user_id="visitor-1")
    insert_use_session(
        conn, use_id="use-1", internal_request_id="internal-1", user_id="visitor-1",
        node_id="locker-1", state="ACTIVE", rental_due_at=(datetime(2026, 10, 15, tzinfo=timezone.utc)
                                                            + timedelta(hours=1)).isoformat(),
    )

    clock = Clock()
    repository = SqliteRepository(conn)
    store = SqliteExecutionStore(conn)
    locks: dict[str, RLock] = {}
    lock_index = Lock()

    def lock_for(node_id):
        with lock_index:
            return locks.setdefault(node_id, RLock())

    service = ReleaseService(
        repository=repository, store=store, settings=lambda: Settings(), clock=clock, lock_for=lock_for,
    )
    try:
        yield conn, store, service, clock
    finally:
        conn.close()


def test_integration_release_with_sqlite_store(env):
    conn, store, service, clock = env

    completed = service.release_use("visitor-1", "use-1")

    assert completed.verdict == Verdict.OK
    session = conn.execute(
        "SELECT state, closed_at, end_reason FROM use_session WHERE use_id = 'use-1'"
    ).fetchone()
    assert session["state"] == "CLOSED"
    assert session["closed_at"] == clock().isoformat()
    assert session["end_reason"] == "release"
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'locker-1'").fetchone()
    assert node["device_state"] == "AVAILABLE"

    close_result = store.close_result("use-1")
    assert close_result is not None
    assert (close_result.closed_at, close_result.end_reason) == (clock(), EndReason.RELEASE)

    clock.advance(10)
    again = service.release_use("visitor-1", "use-1")
    assert again == completed  # 같은 반납의 재요청은 기존 완료 결과를 돌려준다.
    assert store.close_result("use-1").closed_at == clock() - timedelta(seconds=10)


def test_integration_release_on_fault_device_keeps_occupancy(env):
    conn, store, service, clock = env
    conn.execute("UPDATE node SET device_state = 'FAULT' WHERE node_id = 'locker-1'")

    decision = service.release_use("visitor-1", "use-1")

    assert decision.verdict == Verdict.DENY
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use-1'").fetchone()
    assert session["state"] == "ACTIVE"  # FAULT 기기의 점유는 복구 절차가 정산한다 (NFR-04).
