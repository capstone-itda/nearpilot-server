"""FR-09·FR-18 판정과 실제 DB(SqliteRepository, RepositoryPolicyChecker)의 통합 시나리오.

근접 추정, 실행, 감사만 fake 다. 실패하는 시나리오는 코드의 결함이다. 테스트를 고치지 않는다.
"""

from __future__ import annotations

import json

import pytest

from _world import NOW, OWNER, VISITOR_A, VISITOR_B, World
from nearpilot.api.mcp.response import host_response
from nearpilot.shared.enums import ApprovalStatus, ReasonCode, Verdict
from nearpilot.shared.models import ApprovalDecisionRequest


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


def test_FR18_새_대여는_OK이고_점유_세션을_만든다(world):
    d = world.pipeline.decide(world.request(VISITOR_A))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.OK, ReasonCode.OK, "locker-1")
    assert d.use_id is not None
    assert world.count("SELECT COUNT(*) FROM use_session WHERE state = 'RESERVED' AND node_id = 'locker-1'") == 1
    row = world.conn.execute("SELECT verdict, reason_code, node_id FROM request").fetchone()
    assert tuple(row) == ("OK", "OK", "locker-1")  # 판정 기록


def test_FR10_재전송은_기존_판정을_반환한다(world):
    first = world.pipeline.decide(world.request(VISITOR_A))
    again = world.pipeline.decide(world.request(VISITOR_A))
    assert again == first and again.use_id == first.use_id
    assert world.count("SELECT COUNT(*) FROM use_session") == 1
    assert world.count("SELECT COUNT(*) FROM request") == 1


def test_FR10_같은_키_다른_입력은_충돌이다(world):
    first = world.pipeline.decide(world.request(VISITOR_A, "locker-1"))
    other = world.pipeline.decide(world.request(VISITOR_A, "locker-3"))
    assert (other.verdict, other.reason) == (Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
    row = world.conn.execute("SELECT verdict, node_id FROM request").fetchone()
    assert tuple(row) == ("OK", "locker-1") and first.verdict is Verdict.OK  # 기존 판정을 덮어쓰지 않는다


def test_FR19_점유된_기기는_다른_방문자에게_BUSY(world):
    world.pipeline.decide(world.request(VISITOR_A))
    d = world.pipeline.decide(world.request(VISITOR_B, request_id="r2"))
    assert (d.verdict, d.reason) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY)
    assert world.count("SELECT COUNT(*) FROM use_session") == 1


def test_FR14_승인_없이는_NEED_APPROVAL():
    w = World({"locker-2": 0.97})
    try:
        d = w.pipeline.decide(w.request(VISITOR_A, "locker-2"))
        assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)
        assert w.count("SELECT COUNT(*) FROM use_session") == 0
    finally:
        w.close()


def test_FR03_request_access는_pending_신청을_만든다(world):
    d = world.access.request_access(VISITOR_A, "locker-2", _open(), "a1")
    assert (d.verdict, d.reason, d.node_id, d.use_id) == (
        Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, "locker-2", None)
    rows = world.conn.execute(
        "SELECT requester_user_id, node_id, status, policy_version FROM approval").fetchall()
    assert [tuple(r) for r in rows] == [(VISITOR_A, "locker-2", "pending", 1)]
    assert world.count("SELECT COUNT(*) FROM request WHERE verdict = 'NEED_APPROVAL'") == 1


def test_FR03_대기_중_재요청은_신청을_늘리지_않는다(world):
    world.access.request_access(VISITOR_A, "locker-2", _open(), "a1")
    again = world.access.request_access(VISITOR_A, "locker-2", _open(), "a2")  # 새 request_id
    assert (again.verdict, again.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING)
    assert world.count("SELECT COUNT(*) FROM approval") == 1
    resend = world.access.request_access(VISITOR_A, "locker-2", _open(), "a1")  # 같은 request_id
    assert resend.reason is ReasonCode.APPROVAL_PENDING
    assert world.count("SELECT COUNT(*) FROM approval") == 1


def test_FR03_승인_뒤_request_use는_OK이고_사용분을_예약한다():
    w = World({"locker-2": 0.97})
    try:
        w.access.request_access(VISITOR_A, "locker-2", _open(), "a1")
        approval_id = w.conn.execute("SELECT approval_id FROM approval").fetchone()[0]
        decided = w.repo.decide_approval(ApprovalDecisionRequest(
            approval_id, OWNER, ApprovalStatus.APPROVED, NOW))
        assert decided.succeeded
        d = w.pipeline.decide(w.request(VISITOR_A, "locker-2", request_id="u1"))
        assert (d.verdict, d.node_id) == (Verdict.OK, "locker-2") and d.use_id is not None
        assert w.count("SELECT COUNT(*) FROM approval_usage WHERE approval_id = ? AND state = 'reserved'",
                       approval_id) == 1
    finally:
        w.close()


def test_FR09_승인이_필요_없으면_신청하지_않는다(world):
    d = world.access.request_access(VISITOR_A, "locker-1", _open(), "a1")
    assert (d.verdict, d.reason, d.use_id) == (Verdict.OK, ReasonCode.OK, None)
    assert world.count("SELECT COUNT(*) FROM approval") == 0


def test_FR14_비공개_기기는_없는_기기와_같다(world):
    private = world.access.request_access(VISITOR_A, "locker-3", _open(), "a1")
    missing = world.access.request_access(VISITOR_A, "locker-9", _open(), "a2")
    assert (private.verdict, private.reason) == (missing.verdict, missing.reason) == (
        Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)
    assert private.node_id is None and missing.node_id is None
    assert world.count("SELECT COUNT(*) FROM approval") == 0


def test_FR11_호스트_응답에_내부_값이_없다():
    w = World({"locker-2": 0.97})
    try:
        decisions = [
            w.access.request_access(VISITOR_A, "locker-2", _open(), "a1"),
            w.pipeline.decide(w.request(VISITOR_A, "locker-2", request_id="u1")),
        ]
        approval_id = w.conn.execute("SELECT approval_id FROM approval").fetchone()[0]
        for d in decisions:
            text = json.dumps(host_response(d), ensure_ascii=False)
            assert VISITOR_A not in text and OWNER not in text and approval_id not in text
            assert d.internal_request_id not in text
    finally:
        w.close()


def _open():
    from nearpilot.shared.enums import Action

    return Action.OPEN
