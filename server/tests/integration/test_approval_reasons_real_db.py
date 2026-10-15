"""FR-14 승인 필요 사유 7개와 실제 DB(SqliteRepository, RepositoryPolicyChecker)의 통합 시나리오.

`test_decision_real_db.py`가 이미 확인한 `APPROVAL_REQUIRED`, `APPROVAL_PENDING`, `APPROVAL_REJECTED`는 뺀다.
시간은 요청의 `received_at`으로 움직인다. 근접 추정, 실행, 감사만 fake 다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from _world import NOW, OWNER, VISITOR_A, World, beacon_of
from nearpilot.shared.enums import Action, ApprovalStatus, ReasonCode, Verdict
from nearpilot.shared.models import ApprovalDecisionRequest, RssiFrame

LOCKER = "locker-2"  # 승인이 필요한 사물함


@pytest.fixture
def world():
    w = World({LOCKER: 0.97})
    yield w
    w.close()


def _apply(world, action=Action.OPEN):
    world.access.request_access(VISITOR_A, LOCKER, action, "a1")
    return world.conn.execute("SELECT approval_id FROM approval").fetchone()[0]


def _approve(world, approval_id):
    assert world.repo.decide_approval(
        ApprovalDecisionRequest(approval_id, OWNER, ApprovalStatus.APPROVED, NOW)).succeeded


def _request_at(world, seconds, request_id, action=Action.OPEN):
    at = NOW + timedelta(seconds=seconds)
    # 비콘 관측이 요청 시각 가까이에 있어야 ①을 지나 ④까지 간다
    world.repo.save_rssi(RssiFrame(
        LOCKER, beacon_of(VISITOR_A), -60.0, at, f"s-{request_id}", 1000 + seconds, at, at))
    world.conn.execute("UPDATE node SET last_seen_at = ?", (at.isoformat(),))  # 기기도 온라인이다
    world.conn.commit()
    req = world.request(VISITOR_A, LOCKER, request_id=request_id, action=action)
    return world.pipeline.decide(replace(req, received_at=at))


def test_FR14_대기_중인_신청은_APPROVAL_PENDING(world):
    _apply(world)
    d = _request_at(world, 1, "u1")
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING)


def test_FR14_대기_신청이_만료되면_APPROVAL_REQUEST_EXPIRED(world):
    _apply(world)
    ttl = world.settings.approval_pending_ttl_sec
    d = _request_at(world, ttl + 1, "u1")
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUEST_EXPIRED)
    assert world.count("SELECT COUNT(*) FROM use_session") == 0


def test_FR14_승인의_유효시간이_지나면_APPROVAL_EXPIRED(world):
    approval_id = _apply(world)
    _approve(world, approval_id)
    ttl = world.conn.execute("SELECT approval_ttl_sec FROM policy_revision LIMIT 1").fetchone()[0]
    d = _request_at(world, ttl + 1, "u1")
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXPIRED)
    assert world.count("SELECT COUNT(*) FROM use_session") == 0


def test_FR14_횟수를_다_쓰면_APPROVAL_EXHAUSTED(world):
    approval_id = _apply(world)
    _approve(world, approval_id)
    first = _request_at(world, 1, "u1")
    assert first.verdict is Verdict.OK
    world.conn.execute("UPDATE use_session SET state = 'RELEASED'")  # 사용을 마쳤다
    world.conn.commit()
    d = _request_at(world, 2, "u2")
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED)


def test_FR14_다른_동작의_승인만_있으면_ACTION_NOT_ALLOWED(world):
    approval_id = _apply(world, Action.OPEN)
    _approve(world, approval_id)
    d = _request_at(world, 1, "u1", Action.ON)
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.ACTION_NOT_ALLOWED)
