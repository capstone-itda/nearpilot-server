"""`reserve_if_free`: 원자적 예약, 멱등성, 재조작, 동시성 (FR-19, FR-21, NFR-03)."""

from __future__ import annotations

import threading
import pytest

from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import Action, ExecutionStatus, ReasonCode, ReserveOutcome
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


@pytest.mark.parametrize("approval_id", [None, "appr1"], ids=["no_approval", "one_time_approval"])
def test_FR19_reserve_after_cancel_reserves_again(conn, repo, store, approval_id):
    _setup_rental(conn)
    if approval_id is not None:
        insert_policy(conn, approval_mode="one_time", max_uses=1)
        insert_approval(conn, approval_id=approval_id, internal_request_id="prior-req")
    _seed_request(conn, internal_request_id="req1")
    first = repo.reserve_if_free(_request(approval_id=approval_id))
    store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR, {"source": "decision_error"})

    again = repo.reserve_if_free(_request(approval_id=approval_id))

    assert again.outcome == ReserveOutcome.RESERVED
    assert again.use_id is not None
    assert again.use_id != first.use_id
    node = conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()
    assert node["device_state"] == "RESERVED"
    sessions = {r["use_id"]: (r["state"], r["end_reason"]) for r in conn.execute("SELECT * FROM use_session")}
    assert sessions == {first.use_id: ("CLOSED", "cancelled"), again.use_id: ("RESERVED", None)}
    if approval_id is None:
        assert again.usage_id is None
    else:
        # 취소해서 반환한 사용분은 한도 1에 세지 않는다. 새 사용분을 받는다.
        assert again.usage_id is not None
        assert again.usage_id != first.usage_id
        usages = {r["usage_id"]: r["state"] for r in conn.execute("SELECT usage_id, state FROM approval_usage")}
        assert usages == {first.usage_id: "released", again.usage_id: "reserved"}

    third = repo.reserve_if_free(_request(approval_id=approval_id))

    assert (third.outcome, third.use_id, third.usage_id) == (ReserveOutcome.RESERVED, again.use_id, again.usage_id)
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 2


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


def test_FR19_close_reoperates_own_active_rental(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    _seed_request(conn, internal_request_id="rental-req")
    insert_use_session(conn, use_id="use1", internal_request_id="rental-req", state="ACTIVE")
    _seed_request(conn, internal_request_id="close-req")

    result = repo.reserve_if_free(_request(internal_request_id="close-req", action=Action.CLOSE))

    assert (result.outcome, result.use_id, result.usage_id) == (ReserveOutcome.REOPERATING, "use1", None)
    assert conn.execute("SELECT state FROM use_session WHERE use_id = 'use1'").fetchone()["state"] == "ACTIVE"


@pytest.mark.parametrize("case", ["free", "foreign", "shared"])
def test_FR19_close_without_own_rental_is_use_not_found(conn, repo, case):
    insert_user(conn)
    insert_user(conn, user_id="u2", token_hash="hash-2")
    insert_place(conn)
    occupancy = "shared" if case == "shared" else "rental"
    state = "ACTIVE" if case == "foreign" else "AVAILABLE"
    insert_node(conn, node_id="node1", occupancy=occupancy, device_state=state)
    if case == "foreign":
        _seed_request(conn, internal_request_id="other-req", user_id="u2")
        insert_use_session(conn, use_id="other-use", internal_request_id="other-req", user_id="u2", state="ACTIVE")
    _seed_request(conn, internal_request_id="close-req")

    result = repo.reserve_if_free(_request(internal_request_id="close-req", action=Action.CLOSE))

    # 빈 기기에 대여를 시작하지 않고, 타인 점유를 드러내지 않는다 (FR-21)
    assert (result.outcome, result.use_id, result.usage_id) == (ReserveOutcome.USE_NOT_FOUND, None, None)
    sessions = conn.execute("SELECT COUNT(*) AS c FROM use_session WHERE user_id = 'u1'").fetchone()["c"]
    assert sessions == 0
    assert conn.execute("SELECT device_state FROM node WHERE node_id = 'node1'").fetchone()["device_state"] == state


def test_FR19_reserve_reoperating_without_approval_has_reserved_at(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    _seed_request(conn, internal_request_id="rental-req")
    insert_use_session(conn, use_id="use1", internal_request_id="rental-req", state="ACTIVE")
    _seed_request(conn, internal_request_id="reop-req", received_at="2026-01-01T00:00:05+00:00")

    result = repo.reserve_if_free(_request(internal_request_id="reop-req"))

    assert (result.outcome, result.usage_id) == (ReserveOutcome.REOPERATING, None)
    assert result.reserved_at.isoformat() == "2026-01-01T00:00:05+00:00"


def test_FR19_reserve_reoperating_fault(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="FAULT")
    _seed_request(conn, internal_request_id="rental-req")
    insert_use_session(conn, use_id="use1", internal_request_id="rental-req", state="ACTIVE")
    _seed_request(conn, internal_request_id="reop-req")

    result = repo.reserve_if_free(_request(internal_request_id="reop-req"))

    assert result.outcome == ReserveOutcome.DEVICE_BUSY
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM approval_usage").fetchone()["c"] == 0


def test_FR19_reserve_resend_after_active(conn, repo):
    _setup_rental(conn)
    _seed_request(conn, internal_request_id="req1")

    first = repo.reserve_if_free(_request(internal_request_id="req1"))
    assert first.outcome == ReserveOutcome.RESERVED

    # 실행 성공(ExecutionStore 몫)을 흉내 내 세션과 기기를 ACTIVE로 바꾼다.
    conn.execute("UPDATE use_session SET state = 'ACTIVE' WHERE use_id = ?", (first.use_id,))
    conn.execute("UPDATE node SET device_state = 'ACTIVE' WHERE node_id = 'node1'")

    retry = repo.reserve_if_free(_request(internal_request_id="req1"))

    assert retry.outcome == ReserveOutcome.RESERVED
    assert retry.use_id == first.use_id
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


def test_FR19_reserve_returns_reserved_at(conn, repo):
    _setup_rental(conn)
    _seed_request(conn, internal_request_id="req1", received_at="2026-01-01T00:00:00+00:00")

    result = repo.reserve_if_free(_request())

    assert result.reserved_at is not None
    assert result.reserved_at.isoformat() == "2026-01-01T00:00:00+00:00"
    row = conn.execute("SELECT reserved_at FROM use_session WHERE use_id = ?", (result.use_id,)).fetchone()
    assert row["reserved_at"] == "2026-01-01T00:00:00+00:00"


def test_FR19_reserve_shared_usage_reserved_at(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    insert_policy(conn, node_id="light1", max_uses=None)
    insert_approval(conn, approval_id="appr1", node_id="light1", internal_request_id="other")
    _seed_request(
        conn, internal_request_id="req1", node_id="light1", received_at="2026-01-01T00:00:00+00:00"
    )

    result = repo.reserve_if_free(_request(node_id="light1", approval_id="appr1", action=Action.ON))

    assert result.reserved_at is not None
    assert result.reserved_at.isoformat() == "2026-01-01T00:00:00+00:00"
    row = conn.execute(
        "SELECT reserved_at FROM approval_usage WHERE usage_id = ?", (result.usage_id,)
    ).fetchone()
    assert row["reserved_at"] == "2026-01-01T00:00:00+00:00"


def test_FR19_reserve_no_write_reserved_at_none(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared", device_state="AVAILABLE")
    _seed_request(conn, internal_request_id="req1", node_id="light1")

    open_result = repo.reserve_if_free(_request(node_id="light1", action=Action.ON))
    assert open_result.reserved_at is None

    insert_node(conn, node_id="node1", occupancy="rental", device_state="FAULT")
    _seed_request(conn, internal_request_id="req2")
    busy_result = repo.reserve_if_free(_request(internal_request_id="req2"))
    assert busy_result.reserved_at is None


def test_FR19_reserve_duplicate_keeps_reserved_at(conn, repo):
    _setup_rental(conn)
    _seed_request(conn, internal_request_id="req1", received_at="2026-01-01T00:00:00+00:00")

    first = repo.reserve_if_free(_request())
    second = repo.reserve_if_free(_request())

    assert first.reserved_at is not None
    assert second.reserved_at == first.reserved_at


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



# ── 승인 뒤 재요청과 원 요청의 연결 (FR-09) ─────────────────────────────


def test_FR09_reserve_links_new_request_to_approval_request(conn, repo):
    """승인 사용분이 새 요청을 승인과, 승인이 승인 신청 요청과 잇는다. 요청 행의 원 요청 ID는 바꾸지 않는다.

    원 요청 ID를 채우면 `execution_context()`가 판정이 인계한 문맥과 달라져 실행 결과 인계가 실패한다 (#83).
    """
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1")
    insert_policy(conn)
    _seed_request(conn, internal_request_id="approval-req")
    insert_approval(conn, approval_id="appr1", internal_request_id="approval-req")
    _seed_request(conn, internal_request_id="new-req")

    result = repo.reserve_if_free(_request(internal_request_id="new-req", approval_id="appr1"))

    assert result.outcome is ReserveOutcome.RESERVED
    row = conn.execute(
        "SELECT a.internal_request_id AS origin FROM approval_usage u JOIN approval a USING (approval_id) "
        "WHERE u.internal_request_id = ?", ("new-req",),
    ).fetchone()
    assert row["origin"] == "approval-req"
    parent = conn.execute(
        "SELECT parent_internal_request_id FROM request WHERE internal_request_id = ?", ("new-req",),
    ).fetchone()["parent_internal_request_id"]
    assert parent is None
