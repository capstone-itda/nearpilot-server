"""FR-23 any:locker 와 실제 DB(SqliteRepository)·실제 대기 구간(AllocationWindow)의 통합 시나리오.

스레드마다 DB 연결을 따로 연다. 근접 추정, 실행, 감사만 fake 다. 실패하는 시나리오는 코드의 결함이다.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest

from _world import SETTINGS, VISITOR_A, VISITOR_B, World
from nearpilot.api.mcp.response import host_response
from nearpilot.core.allocation import AllocationWindow
from nearpilot.shared.enums import ReasonCode, Verdict

ANY_SETTINGS = replace(SETTINGS, threshold_locker=0.4, threshold_explicit_locker=0.3)


@pytest.fixture
def make_world(tmp_path):
    worlds = []

    def make(posteriors):
        w = World(posteriors, db_path=str(tmp_path / "world.db"), settings=ANY_SETTINGS,
                  window=AllocationWindow(60))
        worlds.append(w)
        return w

    yield make
    for w in worlds:
        w.close()


def run_together(world, requests):
    """요청마다 스레드와 DB 연결을 따로 쓴다. 모두 같은 대기 구간에 들어가게 보낸다."""
    out, threads = {}, []

    def run(user_id, request_id):
        conn, pipeline = world.fork()
        try:
            out[user_id] = pipeline.decide(world.request(user_id, "any:locker", request_id))
        finally:
            conn.close()

    for i, (user_id, request_id) in enumerate(requests, start=1):
        t = threading.Thread(target=run, args=(user_id, request_id))
        t.start()
        threads.append(t)
        deadline = time.monotonic() + 3
        while world.window.pending() < i:  # 이 요청이 구간에 들어올 때까지 기다린다
            assert time.monotonic() < deadline, "요청이 대기 구간에 들어오지 않았다"
            time.sleep(0.002)
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    return out


def test_FR23_허용_사물함이_둘이면_서로_다른_기기를_받는다(make_world):
    w = make_world({"locker-1": 0.45, "locker-4": 0.5})
    out = run_together(w, [(VISITOR_A, "a1"), (VISITOR_B, "b1")])
    assert all(d.verdict is Verdict.OK and d.use_id for d in out.values())
    assert sorted(d.node_id for d in out.values()) == ["locker-1", "locker-4"]
    rows = w.conn.execute("SELECT node_id, state FROM use_session ORDER BY node_id").fetchall()
    assert [tuple(r) for r in rows] == [("locker-1", "RESERVED"), ("locker-4", "RESERVED")]
    assert w.count("SELECT COUNT(*) FROM request WHERE verdict = 'OK'") == 2


def test_FR23_허용_사물함이_하나면_한쪽만_받는다(make_world):
    w = make_world({"locker-1": 0.9})
    out = run_together(w, [(VISITOR_A, "a1"), (VISITOR_B, "b1")])
    assert sorted(d.verdict.value for d in out.values()) == ["BUSY", "OK"]
    busy = next(d for d in out.values() if d.verdict is Verdict.BUSY)
    assert (busy.reason, busy.node_id, busy.use_id) == (ReasonCode.DEVICE_BUSY, None, None)
    assert w.count("SELECT COUNT(*) FROM use_session WHERE node_id = 'locker-1'") == 1
    assert w.count("SELECT COUNT(*) FROM use_session") == 1
    assert w.count("SELECT COUNT(*) FROM request WHERE verdict = 'BUSY'") == 1


def test_FR23_승인이_필요한_사물함은_배정하지_않는다(make_world):
    # locker-2 는 승인이 필요하고 더 가깝다. 배정에서 빠지고 locker-1 을 받는다
    w = make_world({"locker-1": 0.45, "locker-2": 0.5})
    out = run_together(w, [(VISITOR_A, "a1")])
    d = out[VISITOR_A]
    assert (d.verdict, d.node_id) == (Verdict.OK, "locker-1")
    assert w.count("SELECT COUNT(*) FROM use_session WHERE node_id = 'locker-2'") == 0


def test_FR23_방문자_하나도_배정받는다(make_world):
    w = make_world({"locker-4": 0.9})
    d = run_together(w, [(VISITOR_B, "b1")])[VISITOR_B]
    assert (d.verdict, d.node_id) == (Verdict.OK, "locker-4")
    assert w.count("SELECT COUNT(*) FROM use_session") == 1


def test_FR10_any_재전송은_기존_판정을_반환한다(make_world):
    w = make_world({"locker-1": 0.9})
    first = run_together(w, [(VISITOR_A, "a1")])[VISITOR_A]
    conn, pipeline = w.fork()
    try:
        again = pipeline.decide(w.request(VISITOR_A, "any:locker", "a1"))
    finally:
        conn.close()
    assert again == first
    assert w.count("SELECT COUNT(*) FROM use_session") == 1


def test_FR11_any_호스트_응답에_내부_값이_없다(make_world):
    w = make_world({"locker-1": 0.45, "locker-4": 0.5})
    out = run_together(w, [(VISITOR_A, "a1"), (VISITOR_B, "b1")])
    for user_id, d in out.items():
        text = json.dumps(host_response(d), ensure_ascii=False)
        other = VISITOR_B if user_id == VISITOR_A else VISITOR_A
        assert user_id not in text and other not in text
        assert d.internal_request_id not in text and "allocation" not in text
