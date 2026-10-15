"""FR-18 오류로 거부한 요청의 재판정(#76)과 실제 DB(SqliteRepository)의 통합 시나리오.

`link_request`가 `INTERNAL_ERROR`로 끝난 키를 새 시도로 넘기는 DB 변경이 들어오기 전에는 실패한다.
DB 변경이 들어오면 `xfail`을 지운다 (strict 라서 통과하면 알려준다).
"""

from __future__ import annotations

import pytest

from _world import VISITOR_A, World
from nearpilot.shared.enums import ReasonCode, Verdict

DB_PENDING = pytest.mark.xfail(
    strict=True, reason="DB 담당의 link_request 변경 전 (specs/core-internal-error-retry)")


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


@DB_PENDING
def test_FR18_오류_거부_뒤_같은_입력_재전송은_같은_내부_요청으로_다시_판정한다(world):
    _break_proximity(world)
    failed = world.pipeline.decide(world.request(VISITOR_A))
    assert (failed.verdict, failed.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    _fix_proximity(world)
    d = world.pipeline.decide(world.request(VISITOR_A))
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.internal_request_id == failed.internal_request_id
    assert world.count("SELECT COUNT(*) FROM request") == 1
    row = world.conn.execute("SELECT verdict, reason_code FROM request").fetchone()
    assert tuple(row) == ("OK", "OK")


@DB_PENDING
def test_FR19_오류_거부_뒤_재판정은_예약을_하나만_둔다(world):
    _break_proximity(world)
    world.pipeline.decide(world.request(VISITOR_A))
    _fix_proximity(world)
    world.pipeline.decide(world.request(VISITOR_A))
    assert world.count("SELECT COUNT(*) FROM use_session WHERE state = 'RESERVED'") == 1


def test_FR18_확정_판정은_재전송해도_다시_판정하지_않는다(world):
    first = world.pipeline.decide(world.request(VISITOR_A))
    _break_proximity(world)  # 다시 판정하면 INTERNAL_ERROR 가 된다
    assert world.pipeline.decide(world.request(VISITOR_A)) == first
