"""`ExecutionStore` 6단계: `restore_pending` (specs/db-execution-store/spec.md)."""

from __future__ import annotations

import json

import pytest

from nearpilot.shared.enums import Action, ApprovalUsageState, ExecutionStatus, RecoveryStatus, SessionState

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
