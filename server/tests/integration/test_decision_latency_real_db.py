"""NFR-01 서버 판정 시간. 실제 DB(SqliteRepository)로 판정 100회의 시간을 잰다.

근접 추정, 실행, 감사는 fake 라서 이 시간에 들어가지 않는다 (specs/core-decision-latency).
"""

from __future__ import annotations

import statistics
import time

from _world import VISITOR_A, World
from nearpilot.shared.enums import Verdict

RUNS = 100
MEDIAN_LIMIT_SEC = 1.0  # NFR-01, 지표 7
P95_LIMIT_SEC = 2.0


def test_NFR01_판정_100회의_median과_p95가_기준_이내다():
    elapsed = []
    for i in range(RUNS):
        world = World()
        try:
            request = world.request(VISITOR_A, request_id=f"r{i}")
            start = time.perf_counter()
            decision = world.pipeline.decide(request)
            elapsed.append(time.perf_counter() - start)
            assert decision.verdict is Verdict.OK, f"{i}번째 판정이 OK 가 아니다: {decision.reason}"
        finally:
            world.close()
    elapsed.sort()
    median = statistics.median(elapsed)
    p95 = elapsed[int(RUNS * 0.95) - 1]
    assert median <= MEDIAN_LIMIT_SEC, f"median {median:.3f}s"
    assert p95 <= P95_LIMIT_SEC, f"p95 {p95:.3f}s"
