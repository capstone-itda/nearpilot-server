"""상태 관리의 `ReleaseService`를 fake 없이 실제 SQLite 저장소로 돌린다.

`core/lifecycle/release.py`는 바꾸지 않는다. `ExecutionStore`·`Repository`만 진짜로 바꿔
`begin_close`·`finish_close`·`close_result`가 실제 반납 흐름에서 맞물리는지 확인한다
(specs/db-execution-store/spec.md §5 "release.py를 실제 SQLite 저장소로 돌린다").
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Lock, RLock

import pytest

from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import SafetyCheckOutcome, Verdict
from nearpilot.shared.models import SafetyCheckResult

from tests.db.conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class GatewayFake:
    def __init__(self) -> None:
        self.requests = []

    def request_check(self, request) -> None:
        self.requests.append(request)


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
    conn.execute(
        "UPDATE use_session SET release_policy = ? WHERE use_id = ?",
        (json.dumps({"check_criteria": {"id": "locker-return", "version": 1}}), "use-1"),
    )

    clock = Clock()
    repository = SqliteRepository(conn)
    store = SqliteExecutionStore(conn)
    gateway = GatewayFake()
    locks: dict[str, RLock] = {}
    lock_index = Lock()
    faults = []

    def lock_for(node_id):
        with lock_index:
            return locks.setdefault(node_id, RLock())

    def record_fault(session, evidence):
        faults.append((session, evidence))

    service = ReleaseService(
        repository=repository, store=store, gateway=gateway,
        settings=lambda: Settings(), clock=clock, lock_for=lock_for, record_fault=record_fault,
    )
    try:
        yield conn, store, service, gateway, clock, faults
    finally:
        conn.close()


def _report(gateway, clock, outcome=SafetyCheckOutcome.PASSED, **changes):
    request = gateway.requests[-1]
    value = SafetyCheckResult(
        request.node_id, request.target_session_id, 1, clock(), clock(), request.check_id,
        "event-1", request.criteria, clock(), outcome,
        {"door_closed": True, "actuator_state": "idle", "sensor_fault": False},
    )
    return replace(value, **changes)


def test_integration_release_with_sqlite_store(env):
    conn, store, service, gateway, clock, faults = env

    pending = service.release_use("visitor-1", "use-1")

    assert pending.verdict == Verdict.BUSY
    assert len(gateway.requests) == 1
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use-1'").fetchone()
    assert session["state"] == "CLOSING"
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'locker-1'").fetchone()
    assert node["device_state"] == "CLOSING"

    result = _report(gateway, clock)
    service.on_safety_check_result(result)

    completed = service.release_use("visitor-1", "use-1")
    assert completed.verdict == Verdict.OK

    session = conn.execute(
        "SELECT state, closed_at, check_id, check_evidence FROM use_session WHERE use_id = 'use-1'"
    ).fetchone()
    assert session["state"] == "CLOSED"
    assert session["check_id"] == result.check_id
    assert json.loads(session["check_evidence"]) == dict(result.evidence)
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'locker-1'").fetchone()
    assert node["device_state"] == "AVAILABLE"

    close_result = store.close_result("use-1")
    assert close_result is not None
    assert close_result.check_id == result.check_id

    assert faults == []  # 정상 흐름에서는 격리가 일어나지 않는다.


def test_integration_release_isolates_on_fault_report(env):
    conn, store, service, gateway, clock, faults = env

    service.release_use("visitor-1", "use-1")
    fault_report = _report(gateway, clock, outcome=SafetyCheckOutcome.FAULT)
    service.on_safety_check_result(fault_report)

    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'locker-1'").fetchone()
    assert node["device_state"] == "FAULT"
    session = conn.execute("SELECT state FROM use_session WHERE use_id = 'use-1'").fetchone()
    assert session["state"] == "CLOSING"  # 점유는 보존한다 (NFR-04).
    assert len(faults) == 1
