"""FR-18 오류로 거부한 요청의 재판정(#76)과 실제 DB(SqliteRepository)의 통합 시나리오.

DB 변경(`specs/db-internal-error-retry`, PR #144)이 들어온 뒤의 동작을 확인한다.
"""

from __future__ import annotations

import pytest

from _world import VISITOR_A, World
from nearpilot.shared.enums import ReasonCode, Verdict


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


def _break_proximity(world):
    def boom(*args, **kwargs):
        raise RuntimeError("일시 장애")

    world.prox.posterior = boom


def _fix_proximity(world):
    del world.prox.posterior  # 인스턴스 속성을 지우면 정상 메서드로 돌아간다


def test_FR18_오류_거부_뒤_같은_입력_재전송은_같은_내부_요청으로_다시_판정한다(world):
    _break_proximity(world)
    failed = world.pipeline.decide(world.request(VISITOR_A, "nearest:locker"))
    assert (failed.verdict, failed.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    _fix_proximity(world)
    d = world.pipeline.decide(world.request(VISITOR_A, "nearest:locker"))
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.internal_request_id == failed.internal_request_id
    assert world.count("SELECT COUNT(*) FROM request") == 1
    row = world.conn.execute("SELECT verdict, reason_code FROM request").fetchone()
    assert tuple(row) == ("OK", "OK")


def test_FR19_오류_거부_뒤_재판정은_예약을_하나만_둔다(world):
    _break_proximity(world)
    world.pipeline.decide(world.request(VISITOR_A, "nearest:locker"))
    _fix_proximity(world)
    world.pipeline.decide(world.request(VISITOR_A, "nearest:locker"))
    assert world.count("SELECT COUNT(*) FROM use_session WHERE state = 'RESERVED'") == 1


def test_FR18_확정_판정은_재전송해도_다시_판정하지_않는다(world):
    first = world.pipeline.decide(world.request(VISITOR_A, "nearest:locker"))
    _break_proximity(world)  # 다시 판정하면 INTERNAL_ERROR 가 된다
    assert world.pipeline.decide(world.request(VISITOR_A, "nearest:locker")) == first
