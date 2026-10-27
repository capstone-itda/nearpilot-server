"""`ExecutionStore` 6단계: `restore_pending` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

import json

import pytest

from nearpilot.shared.enums import (
    Action, ApprovalUsageState, ExecutionKind, ExecutionStatus, ReasonCode, RecoveryStatus, SessionState,
)
from nearpilot.shared.models import ReserveRequest

from conftest import (
    insert_approval,
    insert_approval_usage,
    insert_node,
    insert_place,
    insert_policy,
    insert_request,
    insert_use_session,
    insert_user,
)


def _bundle_for(bundles, node_id):
    return next((b for b in bundles if b.node.node_id == node_id), None)


def test_FR19_restore_pending_bundle(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="RESERVED")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")
    conn.execute("UPDATE request SET verdict = 'OK', reason_code = 'OK', node_id = 'node1' WHERE internal_request_id = 'req1'")
    store.link_command("req1", "node1", Action.OPEN)  # PENDING 명령

    bundles = store.restore_pending()

    bundle = _bundle_for(bundles, "node1")
    assert bundle is not None
    assert bundle.node.node_id == "node1"
    assert bundle.read_at is not None
    assert len(bundle.commands) == 1
    assert bundle.commands[0].status == ExecutionStatus.PENDING
    assert len(bundle.sessions) == 1
    assert bundle.sessions[0].session.use_id == "use1"
    assert bundle.sessions[0].user_id == "u1"
    assert bundle.requests == ()  # 이미 명령에 연결됐다.


def test_FR19_restore_pending_includes_unlinked_request(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="RESERVED")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")
    conn.execute("UPDATE request SET verdict = 'OK', reason_code = 'OK', node_id = 'node1' WHERE internal_request_id = 'req1'")
    # link_command를 부르지 않는다 — 서버가 예약 뒤 명령 연결 전에 중단된 상황이다.

    bundles = store.restore_pending()

    bundle = _bundle_for(bundles, "node1")
    assert bundle is not None
    assert bundle.commands == ()
    assert len(bundle.requests) == 1
    pending = bundle.requests[0]
    assert pending.internal_request_id == "req1"
    assert pending.request.user_id == "u1"
    assert pending.use_id == "use1"
    assert pending.decision is not None
    assert pending.decision.verdict.value == "OK"


def test_FR19_restore_pending_uses_new_reservation_after_cancel(store, repo, conn):
    """오류 뒤 취소한 요청을 다시 예약하면(#76 재판정) 복원은 취소된 세션이 아니라 새 예약을 돌려준다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    insert_request(conn, internal_request_id="req1", verdict="OK", reason_code="OK", node_id="node1")
    reserve = ReserveRequest(
        internal_request_id="req1", user_id="u1", node_id="node1", action=Action.OPEN, approval_id=None,
    )
    repo.reserve_if_free(reserve)
    store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR, {"source": "decision_error"})
    again = repo.reserve_if_free(reserve)

    bundle = _bundle_for(store.restore_pending(), "node1")

    assert [r.internal_request_id for r in bundle.requests] == ["req1"]
    pending = bundle.requests[0]
    assert (pending.execution_kind, pending.use_id) == (ExecutionKind.RENTAL_INITIAL, again.use_id)
    assert [s.session.use_id for s in bundle.sessions] == [again.use_id]


def test_FR19_restore_pending_includes_shared_usage(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    insert_request(conn, internal_request_id="req-shared", node_id="light1", action="on")
    insert_policy(conn, policy_id="policy-light", node_id="light1")
    insert_approval(
        conn, approval_id="approval-light", node_id="light1", policy_id="policy-light",
        internal_request_id="req-shared",
    )
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval-light", internal_request_id="req-shared")
    conn.execute(
        "UPDATE request SET verdict = 'OK', reason_code = 'OK', node_id = 'light1' "
        "WHERE internal_request_id = 'req-shared'"
    )
    # link_command를 부르지 않는다. 공용형은 점유 세션이 없다.

    bundles = store.restore_pending()

    bundle = _bundle_for(bundles, "light1")
    assert bundle is not None
    assert bundle.sessions == ()
    assert len(bundle.requests) == 1
    assert bundle.requests[0].execution_kind.value == "shared"
    assert bundle.requests[0].use_id is None
    assert bundle.requests[0].usage_id == "usage1"
    assert len(bundle.approval_usages) == 1
    assert bundle.approval_usages[0].usage_id == "usage1"
    assert bundle.approval_usages[0].state == ApprovalUsageState.RESERVED


def _seed_stale_reservation(conn, shape, verdict, reason, request_node_id):
    """오류 등으로 끝난 요청의 예약이 남은 상태를 만든다. 오류 판정은 `request.node_id`를 비워 저장한다."""
    insert_user(conn)
    insert_place(conn)
    if shape == "session":
        insert_node(conn, node_id="node1", occupancy="rental", device_state="RESERVED")
        insert_request(
            conn, internal_request_id="req1", verdict=verdict, reason_code=reason, node_id=request_node_id,
        )
        insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")
        return
    shared = shape == "shared_usage"
    insert_node(
        conn, node_id="node1", capability="light" if shared else "locker",
        occupancy="shared" if shared else "rental", device_state="AVAILABLE" if shared else "ACTIVE",
    )
    if not shared:
        # 본인 재조작: 기존 대여의 ACTIVE 세션이 있고, 이번 요청은 사용분만 예약했다.
        insert_request(conn, internal_request_id="req0", request_id="ext-0")
        insert_use_session(conn, use_id="active1", internal_request_id="req0", state="ACTIVE")
    insert_request(
        conn, internal_request_id="req1", verdict=verdict, reason_code=reason, node_id=request_node_id,
    )
    insert_policy(conn, policy_id="policy1", node_id="node1")
    insert_approval(
        conn, approval_id="approval1", node_id="node1", policy_id="policy1", internal_request_id="req1",
    )
    insert_approval_usage(conn, usage_id="usage1", approval_id="approval1", internal_request_id="req1")


@pytest.mark.parametrize(
    "shape, verdict, reason, request_node_id, kind, use_id, usage_id",
    [
        pytest.param("session", "DENY", "INTERNAL_ERROR", None, ExecutionKind.RENTAL_INITIAL, "use1", None,
                     id="error-session"),
        pytest.param("session", None, None, None, ExecutionKind.RENTAL_INITIAL, "use1", None,
                     id="null-session"),
        pytest.param("session", "ASK", "MOVE_CLOSER", "node1", ExecutionKind.RENTAL_INITIAL, "use1", None,
                     id="other-verdict-session"),
        pytest.param("shared_usage", "DENY", "INTERNAL_ERROR", None, ExecutionKind.SHARED, None, "usage1",
                     id="error-shared-usage"),
        pytest.param("reoperation_usage", "DENY", "INTERNAL_ERROR", None, ExecutionKind.RENTAL_REOPERATION,
                     "active1", "usage1", id="error-reoperation-usage"),
    ],
)
def test_FR19_restore_pending_includes_stale_reservation_of_failed_request(
    store, conn, shape, verdict, reason, request_node_id, kind, use_id, usage_id,
):
    """판정이 OK가 아니거나 비어도 예약이 남았으면 복원 묶음에 넣는다. 취소되면 빠진다."""
    _seed_stale_reservation(conn, shape, verdict, reason, request_node_id)

    bundle = _bundle_for(store.restore_pending(), "node1")

    assert bundle is not None
    assert bundle.commands == ()
    assert [r.internal_request_id for r in bundle.requests] == ["req1"]
    pending = bundle.requests[0]
    assert (pending.execution_kind, pending.use_id, pending.usage_id) == (kind, use_id, usage_id)
    if verdict is None:
        assert pending.decision is None
    else:
        assert (pending.decision.verdict.value, pending.decision.reason.value) == (verdict, reason)
        assert pending.decision.node_id == request_node_id

    store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED, {})

    cancelled = _bundle_for(store.restore_pending(), "node1")
    assert cancelled is None or cancelled.requests == ()


def test_FR19_restore_pending_skips_reoperation_usage_of_closed_rental(store, conn):
    """재조작 사용분만 남았는데 대여가 닫혔으면 그 요청만 빼고 조회는 실패하지 않는다."""
    _seed_stale_reservation(conn, "reoperation_usage", "DENY", "INTERNAL_ERROR", None)
    conn.execute("UPDATE use_session SET state = 'CLOSED', end_reason = 'explicit' WHERE use_id = 'active1'")
    insert_node(conn, node_id="node2", occupancy="rental", device_state="RESERVED")
    insert_request(conn, internal_request_id="req2", request_id="ext-2", verdict="OK", reason_code="OK",
                   node_id="node2")
    insert_use_session(conn, use_id="use2", internal_request_id="req2", node_id="node2", state="RESERVED")

    bundles = store.restore_pending()

    node1 = _bundle_for(bundles, "node1")
    assert node1 is None or node1.requests == ()
    assert [r.internal_request_id for r in _bundle_for(bundles, "node2").requests] == ["req2"]


def test_NFR04_restore_pending_includes_unresolved_recovery(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", device_state="FAULT")
    conn.execute(
        "INSERT INTO recovery (recovery_id, node_id, use_id, internal_request_ids, command_ids, "
        "created_at, status, evidence) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
        (
            "recovery1", "node1", json.dumps(["req1"]), json.dumps([]),
            "2026-01-01T00:00:00+00:00", RecoveryStatus.NOT_READY.value, json.dumps([]),
        ),
    )

    bundles = store.restore_pending()

    bundle = _bundle_for(bundles, "node1")
    assert bundle is not None
    assert len(bundle.recoveries) == 1
    recovery = bundle.recoveries[0]
    assert recovery.recovery_id == "recovery1"
    assert recovery.internal_request_ids == ("req1",)
    assert recovery.completion is None


def test_FR19_restore_pending_excludes_resolved_node(store, conn):
    """정상 완료된 기기는 복원 대상이 아니다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE")
    conn.execute("UPDATE request SET verdict = 'OK', reason_code = 'OK', node_id = 'node1' WHERE internal_request_id = 'req1'")
    linked = store.link_command("req1", "node1", Action.OPEN)
    store.begin_send(linked.command.command_id)
    conn.execute(
        "UPDATE node_command SET status = 'succeeded', outcome = 'succeeded' WHERE command_id = ?",
        (linked.command.command_id,),
    )
    # ACTIVE 세션은 여전히 "미완료"로 취급하지 않는다 — 대여 중인 정상 상태다.
    # 그래서 세션은 포함되지만, 다른 조건이 전혀 없는 깨끗한 기기는 비교군으로 둔다.

    bundles = store.restore_pending()

    bundle = _bundle_for(bundles, "node1")
    # ACTIVE 세션 자체는 복원 대상(점유 유지 확인)이라 bundle은 있지만, 명령과 요청은 없다.
    assert bundle is not None
    assert bundle.commands == ()
    assert bundle.requests == ()
    assert len(bundle.sessions) == 1


def test_FR19_restore_pending_excludes_denied_request(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="AVAILABLE")
    insert_request(conn, internal_request_id="req1")
    # 대상은 확정됐지만(node_id 저장) 거부돼서 예약이 전혀 없다.
    conn.execute(
        "UPDATE request SET verdict = 'DENY', reason_code = 'POLICY_DENIED', node_id = 'node1' "
        "WHERE internal_request_id = 'req1'"
    )

    bundles = store.restore_pending()

    assert _bundle_for(bundles, "node1") is None


def test_FR19_restore_pending_excludes_open_shared_without_write(store, conn):
    """`open` 정책의 공용형 성공은 세션도 사용분도 쓰지 않는다(#27) — 복원할 예약이 없다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared", device_state="AVAILABLE")
    insert_request(conn, internal_request_id="req-open", node_id="light1", action="on")
    conn.execute(
        "UPDATE request SET verdict = 'OK', reason_code = 'OK', node_id = 'light1' "
        "WHERE internal_request_id = 'req-open'"
    )
    # link_command도, use_session도, approval_usage도 전혀 없다.

    bundles = store.restore_pending()

    assert _bundle_for(bundles, "light1") is None


def test_FR19_restore_pending_error_raises(store, conn):
    conn.close()

    with pytest.raises(Exception):
        store.restore_pending()
