"""FR-09 FR-16 지표 10 거리 순서 평가의 순수 계산 (specs/db-evaluation-distance-order/spec.md 5절, 6절 결정 1~14).

`order_points`는 서버의 `HmmProximityEstimator.distance_order`와 같은 프레임을 넣어 결과를 직접 견준다. 나머지는 손으로 만든 시점과 쌍으로
손으로 센 값과 맞추고, σ 추정은 알려진 값으로 만든 합성 프레임과 합성 쌍에서 값을 되찾는지 본다. 무작위는 `random.Random(고정 시드)`뿐이라
같은 입력에는 같은 결과가 나온다. 좌표는 서는 위치 P가 원점이고 기기가 x축 위에 있는 작은 좌표표다 (좌표표 파일은 `test_eval_layout.py`가 본다).
"""

from __future__ import annotations

import csv
import itertools
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.proximity import HmmProximityEstimator
from nearpilot.core.proximity.common_fit import LabeledRun
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import DeviceState, Occupancy, TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, RssiFrame, RssiReport
from tools.evaluation.layout import Layout
from tools.evaluation.order import (
    CANDIDATES,
    MIN_GAP_M,
    STATIC_SIGMA_GRID,
    BoundaryStat,
    OrderPoint,
    ScoredPair,
    StaticSigmaFit,
    boundary_table,
    choose_threshold,
    estimate_frame_sigma,
    estimate_static_sigma,
    order_points,
    order_rows,
    pair_agreement,
    scored_pairs,
)
from tools.evaluation.proximity import EvaluationError
from tools.evaluation.report import write_report
from tools.evaluation.rows import metric_row

SETTINGS = Settings()
WINDOW_SEC = SETTINGS.anchor_recent_sec  # 3초
STEP_SEC = SETTINGS.rssi_frame_sec  # 1초
T0 = datetime(2026, 11, 13, 9, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("11111111-2222-3333-4444-555555555555", 1, 1)
OTHER_BEACON = BeaconKey(BEACON.uuid, 1, 2)
ROW_NAMES = [
    "m10_frame_sigma_db", "m10_static_sigma_db", "m10_pair_order_agreement",
    "m10_boundary_min_prob", "m10_boundary_pass_rate", "m10_boundary_error_rate",
]


# ── 도우미 ────────────────────────────────────────────────────────────


def after(seconds: float, *, micro: int = 0) -> datetime:
    """T0에서 `seconds`초(+ `micro` 마이크로초) 뒤의 시각."""
    return T0 + timedelta(seconds=seconds, microseconds=micro)


def raw_frame(
    node_id: str, rssi: float, received: datetime, *, seq: int, ts: datetime | None = None, beacon: BeaconKey = BEACON,
) -> RssiFrame:
    """서버가 `received`에 받은 프레임. 기기 시각 `ts`의 기본값은 받은 시각보다 0.25초 앞이다."""
    frame_ts = received - timedelta(seconds=0.25) if ts is None else ts
    return RssiFrame(node_id, beacon, rssi, frame_ts, f"s-{node_id}", seq, frame_ts, received)


def make_run(
    run_id: str, levels: Mapping[str, float], *, seconds: int = 20, wobble: Sequence[float] = (0.0,),
    starts: Mapping[str, int] | None = None, jitter: Callable[[], float] | None = None,
) -> LabeledRun:
    """기기마다 1초에 하나씩(t + 0.5초에 서버가 받았다) RSSI를 받은 회차. 구간은 T0부터 `seconds`초다.

    RSSI는 `levels`의 값에 `wobble`을 초마다 돌려 가며 더하고, `jitter`가 있으면 그 호출값을 더한다.
    `starts`는 기기 ID → 그 기기가 처음 보고하는 초다 (없으면 0초).
    """
    frames = []
    for node_id, level in levels.items():
        for t in range((starts or {}).get(node_id, 0), seconds):
            rssi = level + wobble[t % len(wobble)] + (jitter() if jitter else 0.0)
            frames.append(raw_frame(node_id, rssi, after(t + 0.5), seq=t + 1))
    return LabeledRun(run_id, "a", BEACON, T0, after(seconds), frames)


def line_layout(**distances: float) -> Layout:
    """서는 위치 P가 원점이고, 기기가 x축 위에서 P까지 `distances`만큼 떨어진 좌표표."""
    return Layout("place-a", {node_id: (distance, 0.0) for node_id, distance in distances.items()}, {"P": (0.0, 0.0)})


def hand_point(order: Sequence[str], values: Mapping[str, float | None], counts: Mapping[str, int] | None = None) -> OrderPoint:
    """손으로 만든 시점. 프레임 수의 기본값은 정렬 값이 있으면 3, 없으면 0이다."""
    if counts is None:
        counts = {node_id: 0 if value is None else 3 for node_id, value in values.items()}
    return OrderPoint("run-1", "P", T0, tuple(order), dict(values), dict(counts))


def pair(earlier_value: float, later_value: float | None, correct: bool) -> ScoredPair:
    """손으로 만든 쌍. 프레임 수는 두 기기 모두 2이고, 뒤 기기를 못 봤으면 0이다."""
    return ScoredPair("e", "l", earlier_value, later_value, 2, 0 if later_value is None else 2, correct)


def stat(min_prob: float, passed: int, wrong: int, boundaries: int = 200) -> BoundaryStat:
    """통과 `passed`개, 그중 틀림 `wrong`개인 후보의 통계."""
    return BoundaryStat(
        min_prob, boundaries, passed, wrong, passed / boundaries if boundaries else None, wrong / passed if passed else None,
    )


def node_info(node_id: str) -> NodeInfo:
    """근접 추정이 읽는 필드(`node_id`, `trust_state`)만 뜻이 있다. 나머지는 자리 채움 값이다."""
    return NodeInfo(
        node_id=node_id, place_id="place-a", kind="light", occupancy=Occupancy.SHARED, trust_state=TrustState.TRUSTED,
        device_state=DeviceState.AVAILABLE, exclusive_group=None, last_seen_at=None,
    )


def synthetic_pairs(count: int, *, frame_sigma: float, static_sigma: float, spread: float, seed: int) -> list[ScoredPair]:
    """알려진 σf, σs로 만든 합성 쌍. 쌍마다 기기 x, y의 관측 차이를 이렇게 만든다.

    진짜 신호 차이는 N(0, spread²)이고 (양수면 x가 실제로 더 가깝다), 관측 차이는 거기에 기기별 고정 오차
    N(0, σs²) 둘(x, y)과 프레임 평균의 잡음 N(0, σf²/N) 둘을 더한 값이다. 관측이 더 센 기기가 앞선 기기이고,
    `correct`는 그 기기가 진짜로 더 가깝다는 뜻이다. `spread`는 크게 잡아서 경계 확률 식 Φ(D/s)가 가정하는 평평한 사전분포에 가깝게 한다.
    """
    rng = random.Random(seed)
    pairs = []
    for _ in range(count):
        count_x, count_y = rng.choice((2, 3, 4, 5)), rng.choice((2, 3, 4, 5))
        true_gap = rng.gauss(0.0, spread)
        observed = (
            true_gap + rng.gauss(0.0, static_sigma) - rng.gauss(0.0, static_sigma)
            + rng.gauss(0.0, frame_sigma / math.sqrt(count_x)) - rng.gauss(0.0, frame_sigma / math.sqrt(count_y))
        )
        correct = (true_gap > 0) == (observed > 0)
        earlier_count, later_count = (count_x, count_y) if observed >= 0 else (count_y, count_x)
        pairs.append(ScoredPair("e", "l", -70.0 + abs(observed), -70.0, earlier_count, later_count, correct))
    return pairs


# ── order_points ─────────────────────────────────────────────────────


def server_rule_frames() -> list[RssiFrame]:
    """서버 규칙의 갈림길을 모은 프레임 (T0 기준 12초 회차, 기기 다섯). 기기 시각은 받은 시각보다 0.25초 앞이 기본이다.

    - n1: 1초마다 −50, −52, −54를 돈다. 3프레임 평균이 늘 −52다.
    - n2: 앞 6프레임은 n1과 같은 값이라 평균이 같다 (기기 ID 순서). 그 뒤는 −75다. 9.2초에 받은 −40은 기기 시각이 1.0초인 늦은 프레임이다.
    - n3: 창의 경계에 놓은 프레임이다. 정수 초(4.0, 7.0, 9.0초)와 그 1 마이크로초 앞뒤(4.000001, 8.999999초)에 받았다.
    - n4: 다른 비콘 프레임과 유한하지 않은 RSSI뿐이라 이 비콘은 한 번도 보지 못한다.
    - n5: 유한한 프레임은 6.5초와 9.5초뿐이고, 사이(7.5, 8.5초)에 NaN과 무한대가 있다.
    """
    frames: list[RssiFrame] = []
    seqs = itertools.count(1)

    def add(node_id: str, rssi: float, received: datetime, **kwargs) -> None:
        frames.append(raw_frame(node_id, rssi, received, seq=next(seqs), **kwargs))

    for k in range(12):
        level = (-50.0, -52.0, -54.0)[k % 3]
        add("n1", level, after(k + 0.5))
        add("n2", level if k < 6 else -75.0, after(k + 0.5))
    add("n2", -40.0, after(9.2), ts=after(1.0))
    for seconds, micro, rssi in ((2, 0, -70.0), (4, 0, -72.0), (4, 1, -74.0), (7, 0, -76.0), (8, 999_999, -78.0), (9, 0, -80.0)):
        add("n3", rssi, after(seconds, micro=micro))
    for seconds in (1.5, 5.5, 9.5):
        add("n4", -30.0, after(seconds), beacon=OTHER_BEACON)
    for seconds, rssi in ((3.5, math.nan), (6.5, math.inf), (10.5, -math.inf)):
        add("n4", rssi, after(seconds))
    for seconds, rssi in ((6.5, -65.0), (7.5, math.nan), (8.5, math.inf), (9.5, -66.0)):
        add("n5", rssi, after(seconds))
    return frames


def test_FR16_order_at_matches_server_rule():
    anchors = ("n1", "n2", "n3", "n4", "n5")
    frames = server_rule_frames()
    run = LabeledRun("run-1", "n1", BEACON, T0, after(12), frames)

    points = order_points(run, "P", anchors, window_sec=WINDOW_SEC, step_sec=STEP_SEC)

    # 같은 프레임을 서버 추정기에 시각 순서로 넣는다. 시점마다 그 시각까지 받은 프레임을 넣고, 모든 기기가 그 시각에 보고했다고 기록한다.
    estimator = HmmProximityEstimator(None, SETTINGS)
    nodes = {node_id: node_info(node_id) for node_id in anchors}
    pending = sorted(frames, key=lambda frame: frame.received_at)
    fed = 0
    assert [point.at for point in points] == [after(t) for t in range(3, 13)]
    for point in points:
        while fed < len(pending) and pending[fed].received_at <= point.at:
            estimator.observe(pending[fed], node=nodes[pending[fed].node_id])
            fed += 1
        for node_id, node in nodes.items():
            report = RssiReport(node_id, f"s-{node_id}", 1, point.at, point.at, point.at)
            estimator.observe_report(report, node=node)

        server = estimator.distance_order(BEACON, list(nodes.values()), point.at)

        assert point.order == server.nodes, point.at
        assert dict(point.values) == dict(server.values), point.at
        assert dict(point.counts) == dict(server.counts), point.at

    # 갈림길이 실제로 있었는지 손으로 센 값으로 확인한다 (둘 다 같은 오류를 내지 않았는지도 본다)
    by_second = {int((point.at - T0).total_seconds()): point for point in points}
    assert by_second[3].order[:2] == ("n1", "n2") and by_second[3].values["n1"] == by_second[3].values["n2"]  # 같은 값은 기기 ID 순서
    seven = by_second[7]  # 창 [4, 7]: n3의 4.0초(창 시작)와 7.0초(창 끝)는 쓰고, 다른 비콘과 NaN은 쓰지 않는다
    assert seven.order == ("n1", "n2", "n5", "n3", "n4")
    assert seven.values == {"n1": -52.0, "n2": math.fsum([-52.0, -54.0, -75.0]) / 3, "n5": -65.0, "n3": -74.0, "n4": None}
    assert seven.counts == {"n1": 3, "n2": 3, "n5": 1, "n3": 3, "n4": 0}
    twelve = by_second[12]  # 창 [9, 12]: n3의 9.0초는 쓰고 8.999999초는 쓰지 않는다. n2는 늦게 받은 프레임까지 4개다
    assert twelve.order == ("n1", "n5", "n2", "n3", "n4")
    assert twelve.values == {"n1": -52.0, "n5": -66.0, "n2": -66.25, "n3": -80.0, "n4": None}
    assert twelve.counts == {"n1": 3, "n5": 1, "n2": 4, "n3": 1, "n4": 0}
    assert [by_second[t].values["n5"] for t in (3, 4, 5, 6)] == [None] * 4  # 프레임이 창에 들기 전에는 못 본 기기다


def test_FR16_order_points_time_grid():
    run = make_run("run-1", {"a": -50.0, "b": -60.0}, seconds=20)

    points = order_points(run, "P", {"a", "b"}, window_sec=3.0, step_sec=1.0)

    # 시점은 시작 + 창부터 회차 끝(포함)까지 한 칸씩이고, 시점마다 구간 안 프레임 3개의 평균이다
    assert [point.at for point in points] == [after(t) for t in range(3, 21)]
    assert all(point.run_id == "run-1" and point.spot == "P" for point in points)
    assert all(point.order == ("a", "b") and point.values == {"a": -50.0, "b": -60.0} for point in points)
    assert all(point.counts == {"a": 3, "b": 3} for point in points)
    # 칸이 창의 약수가 아니어도 첫 시점에서 정수 마이크로초로 센다 (3, 5.5, …, 18)
    stepped = order_points(run, "P", {"a", "b"}, window_sec=3.0, step_sec=2.5)
    assert [point.at for point in stepped] == [after(3 + 2.5 * k) for k in range(7)]
    # 회차가 창보다 짧으면 시점이 없고, 창과 같으면 시점 하나다
    assert order_points(make_run("run-2", {"a": -50.0}, seconds=2), "P", {"a"}, window_sec=3.0, step_sec=1.0) == []
    exact = order_points(make_run("run-3", {"a": -50.0}, seconds=3), "P", {"a"}, window_sec=3.0, step_sec=1.0)
    assert [point.at for point in exact] == [after(3)]


def test_FR16_order_points_unheard_anchor_last():
    # c는 8.5초에 처음 받았고, d는 프레임이 없다. 못 본 기기는 값 None, 프레임 수 0으로 끝에 기기 ID 순서로 선다.
    run = make_run("run-1", {"a": -70.0, "b": -50.0, "c": -60.0}, seconds=12, starts={"c": 8})

    points = order_points(run, "P", {"a", "b", "c", "d"}, window_sec=3.0, step_sec=1.0)

    by_second = {int((point.at - T0).total_seconds()): point for point in points}
    for second in range(3, 9):  # 창 [t−3, t]가 8.5초를 아직 담지 못한다
        assert by_second[second].order == ("b", "a", "c", "d")
        assert by_second[second].values == {"b": -50.0, "a": -70.0, "c": None, "d": None}
        assert by_second[second].counts == {"b": 3, "a": 3, "c": 0, "d": 0}
    for second in range(9, 13):
        assert by_second[second].order == ("b", "c", "a", "d")
        assert by_second[second].values["c"] == -60.0 and by_second[second].values["d"] is None
    assert by_second[9].counts["c"] == 1 and by_second[12].counts["c"] == 3

    # 아무도 못 본 시점도 돌려준다. 쌍을 고를 때 빠진다.
    silent = order_points(LabeledRun("run-4", "a", BEACON, T0, after(10), ()), "P", {"b", "a"}, window_sec=3.0, step_sec=1.0)
    assert [point.at for point in silent] == [after(t) for t in range(3, 11)]
    assert all(point.order == ("a", "b") and point.values == {"a": None, "b": None} for point in silent)
    assert all(point.counts == {"a": 0, "b": 0} for point in silent)
    assert scored_pairs(silent[0], line_layout(a=2.0, b=5.0), adjacent_only=False) == []


def test_FR16_order_points_uses_only_frames_inside_run_interval():
    base = make_run("run-1", {"a": -50.0}, seconds=10)
    before_start = raw_frame("a", -10.0, after(2.0), seq=100, ts=after(-1.0))  # 기기 시각이 회차 시작보다 앞이다
    at_end = raw_frame("a", -10.0, after(10.0), seq=101, ts=after(10.0))  # 기기 시각이 회차 끝과 같다 (끝은 구간 밖이다)
    run = replace(base, frames=(*base.frames, before_start, at_end))

    points = order_points(run, "P", {"a"}, window_sec=3.0, step_sec=1.0)

    # 두 프레임은 서버가 받은 시각이 창 안이어도 회차의 구간(frame_ts)을 벗어나 쓰지 않는다
    assert [point.at for point in points] == [after(t) for t in range(3, 11)]
    assert all(point.values == {"a": -50.0} and point.counts == {"a": 3} for point in points)


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
def test_FR16_order_points_rejects_bad_window_or_step(bad):
    run = make_run("run-1", {"a": -50.0}, seconds=10)

    with pytest.raises(EvaluationError, match="window_sec"):
        order_points(run, "P", {"a"}, window_sec=bad, step_sec=1.0)
    with pytest.raises(EvaluationError, match="step_sec"):
        order_points(run, "P", {"a"}, window_sec=3.0, step_sec=bad)


# ── scored_pairs ─────────────────────────────────────────────────────


def test_FR09_order_pairs_skip_both_unheard():
    layout = line_layout(a=2.0, b=5.0, c=8.0, d=11.0)
    point = hand_point(("a", "b", "c", "d"), {"a": -50.0, "b": -60.0, "c": None, "d": None})

    every = scored_pairs(point, layout, adjacent_only=False)
    neighbors = scored_pairs(point, layout, adjacent_only=True)

    # c와 d는 둘 다 못 봐서 순서가 없다. (c, d)는 빠지고 나머지 쌍은 모두 순서대로 맞다.
    assert [(p.earlier, p.later) for p in every] == [("a", "b"), ("a", "c"), ("a", "d"), ("b", "c"), ("b", "d")]
    assert [(p.earlier, p.later) for p in neighbors] == [("a", "b"), ("b", "c")]
    assert all(p.correct for p in every)
    assert every[0] == ScoredPair("a", "b", -50.0, -60.0, 3, 3, True)


def test_FR09_order_pairs_one_unheard():
    layout = line_layout(a=2.0, b=5.0, c=3.0)  # c는 b보다 실제로 가깝다
    point = hand_point(("a", "b", "c"), {"a": -50.0, "b": -60.0, "c": None})

    pairs = scored_pairs(point, layout, adjacent_only=True)

    # 뒤 기기만 못 본 쌍은 센다. 못 본 기기가 실제로 더 가까웠으니 틀린 쌍이다.
    assert pairs == [ScoredPair("a", "b", -50.0, -60.0, 3, 3, True), ScoredPair("b", "c", -60.0, None, 3, 0, False)]
    assert pair_agreement(pairs) == (1, 2)
    # σs 추정에서는 뺀다 (경계 확률이 1이다)
    assert estimate_static_sigma(pairs, 4.0).pairs == 1
    with pytest.raises(EvaluationError, match="σs를 추정할 쌍이 없다"):
        estimate_static_sigma(pairs[1:], 4.0)


def test_FR09_order_pairs_skip_under_one_meter():
    assert MIN_GAP_M == 1.0
    # P까지: a 5.0, e 5.0(좌표는 다르다), b 6.0, c 6.5, d 7.49
    layout = Layout(
        "place-a", {"a": (5.0, 0.0), "e": (0.0, 5.0), "b": (6.0, 0.0), "c": (6.5, 0.0), "d": (7.49, 0.0)}, {"P": (0.0, 0.0)},
    )
    point = hand_point(("a", "e", "b", "c", "d"), {"a": -50.0, "e": -52.0, "b": -54.0, "c": -56.0, "d": -58.0})

    every = scored_pairs(point, layout, adjacent_only=False)
    neighbors = scored_pairs(point, layout, adjacent_only=True)

    # 차이가 정확히 1.0 m인 쌍(a-b, e-b)은 센다. 같은 거리(a-e), 0.5 m(b-c), 0.99 m(c-d)는 뺀다.
    assert [(p.earlier, p.later) for p in every] == [
        ("a", "b"), ("a", "c"), ("a", "d"), ("e", "b"), ("e", "c"), ("e", "d"), ("b", "d"),
    ]
    assert [(p.earlier, p.later) for p in neighbors] == [("e", "b")]
    assert all(p.correct for p in every)
    # 앞선 기기가 더 멀면 틀린 쌍이다
    reversed_point = hand_point(("b", "a"), {"b": -50.0, "a": -60.0})
    assert [p.correct for p in scored_pairs(reversed_point, layout, adjacent_only=True)] == [False]


# ── σf ───────────────────────────────────────────────────────────────


def test_FR09_estimate_frame_sigma():
    rng = random.Random(279)
    levels = {"a": -50.0, "b": -58.0, "c": -66.0, "d": -74.0, "e": -82.0, "f": -90.0}
    runs = [make_run(f"run-{i}", levels, seconds=30, jitter=lambda: rng.gauss(0.0, 4.0)) for i in range(10)]

    sigma = estimate_frame_sigma(runs, set(levels), window_sec=3.0)

    # 기기마다 수준이 달라도 구간 평균에서 벗어난 정도만 모으니 흔들림의 표준편차 4 dB가 나온다 (자유도 1200, 표준오차 약 0.08 dB)
    assert sigma == pytest.approx(4.0, abs=0.3)


def test_FR09_estimate_frame_sigma_known_values():
    frames = [  # (기기, 서버가 받은 시각(초), 기기 시각(초), RSSI, 비콘)
        ("a", 0.6, 0.5, -60.0, BEACON), ("a", 1.6, 1.5, -64.0, BEACON),  # 구간 [0, 3): 평균 −62, 제곱합 8, 자유도 1
        ("a", 3.5, 2.5, -100.0, BEACON),  # 기기 시각은 첫 구간이지만 서버가 둘째 구간에 받았다
        ("a", 3.6, 3.5, -90.0, BEACON),  # 구간 [3, 6): 평균 −95, 제곱합 50, 자유도 1
        ("a", 0.7, 0.7, math.nan, BEACON), ("a", 1.0, 1.0, -5.0, OTHER_BEACON),  # 유한하지 않은 RSSI, 다른 비콘은 쓰지 않는다
        ("b", 0.6, 0.5, -10.0, BEACON), ("b", 4.6, 4.5, -10.0, BEACON),  # 구간마다 프레임이 하나라서 자유도가 없다
        ("z", 0.6, 0.5, -10.0, BEACON), ("z", 1.6, 1.5, -90.0, BEACON),  # 앵커가 아닌 기기다
    ]
    run = LabeledRun(
        "run-1", "a", BEACON, T0, after(10),
        [raw_frame(node_id, rssi, after(received), seq=seq, ts=after(ts), beacon=beacon)
         for seq, (node_id, received, ts, rssi, beacon) in enumerate(frames, 1)],
    )

    sigma = estimate_frame_sigma([run], {"a", "b"}, window_sec=3.0)

    assert sigma == pytest.approx(math.sqrt((8.0 + 50.0) / (1 + 1)))  # 구간은 서버가 받은 시각으로 나눈다 (기기 시각이면 ≈ 22)


def test_FR09_estimate_frame_sigma_needs_degrees_of_freedom():
    run = make_run("run-1", {"a": -50.0, "b": -60.0}, seconds=10)

    # 1초 구간에는 기기마다 프레임이 하나뿐이다
    with pytest.raises(EvaluationError, match="σf를 추정할 수 없다"):
        estimate_frame_sigma([run], {"a", "b"}, window_sec=1.0)
    with pytest.raises(EvaluationError, match="σf를 추정할 수 없다"):
        estimate_frame_sigma([], {"a", "b"}, window_sec=3.0)
    with pytest.raises(EvaluationError, match="σf를 추정할 수 없다"):
        estimate_frame_sigma([run], set(), window_sec=3.0)  # 앵커가 없으면 프레임도 없다


@pytest.mark.parametrize("bad", [0.0, -3.0, math.nan, math.inf])
def test_FR09_estimate_frame_sigma_rejects_bad_window(bad):
    run = make_run("run-1", {"a": -50.0}, seconds=10)

    with pytest.raises(EvaluationError, match="window_sec"):
        estimate_frame_sigma([run], {"a"}, window_sec=bad)


# ── σs ───────────────────────────────────────────────────────────────


def test_FR09_estimate_static_sigma_recovers_truth():
    pairs = synthetic_pairs(5000, frame_sigma=4.0, static_sigma=3.0, spread=30.0, seed=279)

    fit = estimate_static_sigma(pairs, 4.0)

    # 가능도 최대인 σs가 쌍을 만든 σs=3 dB에 가깝다 (5000쌍에서 오차의 표준편차는 약 0.2 dB)
    assert fit.sigma_db == pytest.approx(3.0, abs=0.5)
    assert fit.pairs == 5000
    assert not fit.at_edge
    assert fit.sigma_db in STATIC_SIGMA_GRID


def test_FR09_estimate_static_sigma_needs_pairs():
    with pytest.raises(EvaluationError, match="σs를 추정할 쌍이 없다"):
        estimate_static_sigma([], 4.0)
    # 뒤 기기를 못 본 쌍은 경계 확률이 1이라 쓰지 않는다
    with pytest.raises(EvaluationError, match="σs를 추정할 쌍이 없다"):
        estimate_static_sigma([pair(-60.0, None, True), pair(-62.0, None, False)], 4.0)


def test_FR09_estimate_static_sigma_edge_noted():
    gaps = range(1, 21)

    # 모든 쌍을 맞혔다: σs가 작을수록 가능도가 커서 격자의 첫 값이다
    right = estimate_static_sigma([pair(-60.0 + gap, -60.0, True) for gap in gaps], 4.0)
    assert (right.sigma_db, right.pairs, right.at_edge) == (0.0, 20, True)
    # 모두 틀렸다: σs가 클수록 가능도가 커서 격자의 끝 값이다
    wrong = estimate_static_sigma([pair(-60.0 + gap, -60.0, False) for gap in gaps], 4.0)
    assert (wrong.sigma_db, wrong.pairs, wrong.at_edge) == (15.0, 20, True)
    # 격자는 0.00부터 15.00까지 0.05 간격이다
    assert (STATIC_SIGMA_GRID[0], STATIC_SIGMA_GRID[1], STATIC_SIGMA_GRID[-1], len(STATIC_SIGMA_GRID)) == (0.0, 0.05, 15.0, 301)


def test_FR09_estimate_static_sigma_maximizes_likelihood_over_grid(monkeypatch):
    # 판정의 경계 확률 자리에 σs만 보는 값을 끼워서, 맞은 쌍 셋과 틀린 쌍 하나의 로그 가능도 3 log P + log(1 − P)가 P = 0.75에서 가장 큰지 본다
    calls = []

    def fake(a_value, a_count, b_value, b_count, frame_sigma_db, static_sigma_db):
        calls.append((a_value, a_count, b_value, b_count, frame_sigma_db))
        return {2.0: 0.9, 4.0: 0.75}.get(static_sigma_db, 0.5)

    monkeypatch.setattr("tools.evaluation.order.boundary_probability", fake)
    pairs = [pair(-50.0, -60.0, True)] * 3 + [pair(-55.0, -60.0, False)]

    fit = estimate_static_sigma(pairs, 3.5)

    assert (fit.sigma_db, fit.pairs, fit.at_edge) == (4.0, 4, False)  # 맞은 쌍만 보고 log P로 센다면 0.9인 2.0을 골랐을 것이다
    assert calls[0] == (-50.0, 2, -60.0, 2, 3.5)  # 앞선 기기가 a, 뒤 기기가 b이고, 쓴 σf를 그대로 넘긴다
    assert len(calls) == 4 * len(STATIC_SIGMA_GRID)

    # 가능도가 같으면 작은 σs를 고른다
    monkeypatch.setattr("tools.evaluation.order.boundary_probability", lambda *args: 0.8 if args[-1] in (2.0, 7.0) else 0.5)
    assert estimate_static_sigma([pair(-50.0, -60.0, True)], 3.5).sigma_db == 2.0


def test_FR09_estimate_static_sigma_clips_probability():
    # 앞선 기기가 100 dB나 더 센데 틀렸다: 경계 확률이 1이라 log(1 − P)가 log(0)이 될 수 있다
    fit = estimate_static_sigma([pair(40.0, -60.0, False), pair(-50.0, -60.0, True)], 4.0)

    assert math.isfinite(fit.sigma_db) and fit.pairs == 2


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
def test_FR09_estimate_static_sigma_rejects_bad_frame_sigma(bad):
    # σf가 0이고 σs도 0이면 경계 확률이 0으로 나눈다. 서버의 order_frame_sigma_db도 0 초과다.
    with pytest.raises(EvaluationError, match="frame_sigma_db"):
        estimate_static_sigma([pair(-50.0, -60.0, True)], bad)


# ── 경계 표와 기준 확률 ────────────────────────────────────────────────


def test_FR09_pair_agreement_and_boundary_table():
    # σf=3, σs=0, 프레임 수 2와 2라서 s = 3이다. D=6이면 P=Φ(2)=0.977, D=3이면 Φ(1)=0.841, D=0이면 0.5, 뒤 기기를 못 봤으면 1이다.
    pairs = [
        pair(-54.0, -60.0, True), pair(-54.0, -60.0, False),  # D=6
        pair(-57.0, -60.0, True), pair(-57.0, -60.0, False),  # D=3
        pair(-60.0, -60.0, True),  # D=0
        pair(-60.0, None, True),  # 뒤 기기를 못 봤다
    ]

    assert pair_agreement(pairs) == (4, 6)
    assert pair_agreement([]) == (0, 0)
    table = boundary_table(pairs, 3.0, 0.0, candidates=(0.8, 0.95))

    assert table == [
        BoundaryStat(0.8, 6, 5, 2, 5 / 6, 2 / 5),  # D=0인 쌍만 통과하지 못한다
        BoundaryStat(0.95, 6, 3, 1, 3 / 6, 1 / 3),  # D=6인 두 쌍과 못 본 쌍
    ]
    # 기본 후보는 0.50부터 0.99까지 0.01 간격이다. P가 후보와 같으면 통과한다 (D=0의 0.5는 0.50을 통과한다)
    default = boundary_table(pairs, 3.0, 0.0)
    assert [row.min_prob for row in default] == list(CANDIDATES)
    assert (CANDIDATES[0], CANDIDATES[1], CANDIDATES[-1], len(CANDIDATES)) == (0.5, 0.51, 0.99, 50)
    assert default[0] == BoundaryStat(0.5, 6, 6, 2, 1.0, 2 / 6)
    assert default[-1] == BoundaryStat(0.99, 6, 1, 0, 1 / 6, 0.0)
    # 통과한 경계가 없으면 오류 비율은 None이고, 경계가 없으면 통과 비율도 None이다
    assert boundary_table([pair(-60.0, -60.0, True)], 3.0, 0.0, candidates=(0.9,)) == [BoundaryStat(0.9, 1, 0, 0, 0.0, None)]
    assert boundary_table([], 3.0, 0.0, candidates=(0.9,)) == [BoundaryStat(0.9, 0, 0, 0, None, None)]


def test_FR09_boundary_table_uses_boundary_probability(monkeypatch):
    # 식을 다시 쓰지 않고 판정의 boundary_probability를 그대로 부른다: 값을 끼워 넣고 인자와 결과 사용을 본다
    calls = []

    def fake(a_value, a_count, b_value, b_count, frame_sigma_db, static_sigma_db):
        calls.append((a_value, a_count, b_value, b_count, frame_sigma_db, static_sigma_db))
        return 0.9 if a_value == -50.0 else 0.6

    monkeypatch.setattr("tools.evaluation.order.boundary_probability", fake)
    pairs = [
        ScoredPair("a", "b", -50.0, -60.0, 4, 5, True),
        ScoredPair("b", "c", -60.0, None, 3, 0, False),
    ]

    table = boundary_table(pairs, 4.0, 3.5, candidates=(0.8,))

    assert calls == [(-50.0, 4, -60.0, 5, 4.0, 3.5), (-60.0, 3, None, 0, 4.0, 3.5)]
    assert table == [BoundaryStat(0.8, 2, 1, 0, 0.5, 0.0)]  # 둘째 쌍의 P는 0.6이라 통과하지 못한다


@pytest.mark.parametrize(
    ("frame_sigma", "static_sigma", "match"),
    [(0.0, 3.0, "frame_sigma_db"), (math.nan, 3.0, "frame_sigma_db"), (4.0, -0.1, "static_sigma_db"), (4.0, math.inf, "static_sigma_db")],
)
def test_FR09_boundary_table_rejects_bad_sigma(frame_sigma, static_sigma, match):
    with pytest.raises(EvaluationError, match=match):
        boundary_table([pair(-50.0, -60.0, True)], frame_sigma, static_sigma)


def test_FR09_choose_threshold_lowest_within_target():
    table = [stat(0.6, 100, 8), stat(0.7, 100, 4), stat(0.8, 100, 3), stat(0.9, 0, 0)]  # 오류 비율 0.08, 0.04, 0.03, None

    # 0.05 이하인 후보는 0.7과 0.8이고 가장 낮은 값을 고른다. 통과한 경계가 없는(오류 비율 None) 0.9는 고르지 않는다.
    assert choose_threshold(table, 0.05) == 0.7
    assert choose_threshold(list(reversed(table)), 0.05) == 0.7  # 표의 순서가 아니라 값으로 고른다
    assert choose_threshold(table, 0.03) == 0.8
    assert choose_threshold([stat(0.65, 100, 5), stat(0.7, 100, 4)], 0.05) == 0.65  # 오류 비율이 목표와 같으면 만족한다
    assert choose_threshold([stat(0.6, 100, 8), stat(0.9, 0, 0)], 0.5) == 0.6
    # 통과한 경계가 없는 후보는 목표가 아무리 커도 만족하지 않는다
    assert choose_threshold([stat(0.9, 0, 0)], 1.0) is None


def test_FR09_choose_threshold_none_satisfies():
    assert choose_threshold([stat(0.6, 100, 8), stat(0.7, 100, 6), stat(0.9, 0, 0)], 0.05) is None
    assert choose_threshold([], 0.05) is None


# ── 보고서 행 ────────────────────────────────────────────────────────


def test_FR09_order_rows():
    fit = StaticSigmaFit(3.25, 480, False)

    rows = order_rows(
        frame_sigma_db=4.1, static_fit=fit, agreement=(90, 100), chosen=0.9,
        test_stat=BoundaryStat(0.9, 200, 120, 3, 0.6, 0.025), max_error=0.05,
    )

    assert [row["metric"] for row in rows] == ROW_NAMES
    assert rows == [
        metric_row("m10_frame_sigma_db", value=4.1),
        metric_row("m10_static_sigma_db", value=3.25, trials=480),
        metric_row("m10_pair_order_agreement", value=0.9, trials=100, failures=10),
        metric_row("m10_boundary_min_prob", value=0.9),
        metric_row("m10_boundary_pass_rate", value=0.6, trials=200),
        metric_row("m10_boundary_error_rate", value=0.025, limit=0.05, passed=True, trials=120, failures=3),
    ]
    # 통과 칸은 오류 비율 행만 쓴다 (순서 일치율의 기준은 미결이다)
    assert [row["passed"] for row in rows] == ["", "", "", "", "", True]
    # 시험의 오류 비율이 목표보다 크면 통과 칸이 False다
    worse = order_rows(
        frame_sigma_db=4.1, static_fit=fit, agreement=(90, 100), chosen=0.9,
        test_stat=BoundaryStat(0.9, 200, 100, 10, 0.5, 0.1), max_error=0.05,
    )
    assert worse[-1] == metric_row("m10_boundary_error_rate", value=0.1, limit=0.05, passed=False, trials=100, failures=10)


def test_FR09_order_rows_chosen_none_and_edge_notes():
    edge = StaticSigmaFit(15.0, 480, True)

    rows = order_rows(
        frame_sigma_db=4.1, static_fit=edge, agreement=(0, 0), chosen=None, test_stat=None, max_error=0.05,
    )

    assert [row["metric"] for row in rows] == ROW_NAMES
    assert rows[1]["note"] == "탐색 범위 끝" and rows[1]["value"] == 15.0
    # 고르지 못했으면 기준 확률 행은 값을 비우고 이유를 쓴다. 시험 두 행은 값과 통과 칸을 비우고 이유를 쓴다
    assert rows[3]["value"] == "" and rows[3]["note"] == "검증 회차에서 목표 오류 비율 이하인 후보 없음"
    for row in rows[4:]:
        assert row["value"] == "" and row["passed"] == "" and "기준 확률을 고르지 못해" in row["note"]
        assert row["trials"] == "" and row["failures"] == ""
    assert rows[5]["limit"] == 0.05  # 목표는 그대로 보인다
    # 채점한 쌍이 없으면 일치율도 비운다 (failures를 비워야 write_report가 신뢰구간을 계산하려 들지 않는다)
    assert rows[2]["value"] == "" and rows[2]["trials"] == 0 and rows[2]["failures"] == "" and rows[2]["note"] == "채점한 쌍 없음"
    assert all(row["passed"] == "" for row in rows)


def test_FR09_order_rows_no_passed_boundary():
    rows = order_rows(
        frame_sigma_db=4.1, static_fit=StaticSigmaFit(3.0, 10, False), agreement=(10, 10), chosen=0.99,
        test_stat=BoundaryStat(0.99, 200, 0, 0, 0.0, None), max_error=0.05,
    )

    assert rows[2] == metric_row("m10_pair_order_agreement", value=1.0, trials=10, failures=0)
    assert rows[4] == metric_row("m10_boundary_pass_rate", value=0.0, trials=200)  # 통과 비율 0.0은 빈 값이 아니라 값이다
    # 통과한 경계가 없으면 오류 비율을 알 수 없다: 값, 통과 칸, failures를 비운다
    assert rows[5] == metric_row("m10_boundary_error_rate", limit=0.05, trials=0, note="통과한 경계 없음")
    # 시험 회차에 경계가 없으면 통과 비율도 비운다
    empty = order_rows(
        frame_sigma_db=4.1, static_fit=StaticSigmaFit(3.0, 10, False), agreement=(10, 10), chosen=0.9,
        test_stat=BoundaryStat(0.9, 0, 0, 0, None, None), max_error=0.05,
    )
    assert empty[4] == metric_row("m10_boundary_pass_rate", trials=0, note="채점한 경계 없음")


def test_FR09_order_rows_written_by_report(tmp_path):
    cases = {
        "normal": dict(agreement=(100, 100), chosen=0.9, test_stat=BoundaryStat(0.9, 200, 120, 0, 0.6, 0.0)),
        "not-chosen": dict(agreement=(0, 0), chosen=None, test_stat=None),
        "no-passed": dict(agreement=(3, 10), chosen=0.99, test_stat=BoundaryStat(0.99, 200, 0, 0, 0.0, None)),
    }
    for name, case in cases.items():
        rows = order_rows(frame_sigma_db=4.1, static_fit=StaticSigmaFit(3.0, 10, False), max_error=0.05, **case)

        write_report(
            rows, db_path="test.db", schema_version=1, sample_count=1, generated_at=T0,
            csv_path=str(tmp_path / f"{name}.csv"), md_path=str(tmp_path / f"{name}.md"), git_commit="abc",
        )

        lines = (tmp_path / f"{name}.csv").read_text(encoding="utf-8").splitlines()
        written = list(csv.DictReader(line for line in lines if not line.startswith("#")))
        assert [row["metric"] for row in written] == ROW_NAMES
    # 실패 0건인 행(오류 비율 0.0)에는 신뢰구간 상한이 붙는다
    assert "confidence_bound_95" in (tmp_path / "normal.csv").read_text(encoding="utf-8")


# ── 이어 붙이기 ──────────────────────────────────────────────────────


def test_FR09_order_evaluation_end_to_end():
    layout = line_layout(a=2.0, b=5.0, c=9.0)
    anchors = {"a", "b", "c"}
    run = make_run("run-1", {"a": -50.0, "b": -60.0, "c": -70.0}, seconds=21, wobble=(1.0, -1.0, 0.0))

    sigma_f = estimate_frame_sigma([run], anchors, window_sec=3.0)
    points = order_points(run, "P", anchors, window_sec=3.0, step_sec=1.0)
    every = [p for point in points for p in scored_pairs(point, layout, adjacent_only=False)]
    neighbors = [p for point in points for p in scored_pairs(point, layout, adjacent_only=True)]

    # 흔들림 (+1, −1, 0)이 3프레임 구간마다 한 바퀴라 (21초 회차는 구간 일곱이 모두 3프레임이다) 구간 평균 0, 제곱합 2, 자유도 2다
    assert sigma_f == pytest.approx(1.0)
    assert len(points) == 19  # 3초부터 21초까지
    assert pair_agreement(every) == (57, 57)  # 19시점 × 쌍 3개. 신호가 센 기기가 늘 더 가깝다
    assert len(neighbors) == 38  # 19시점 × 이웃 쌍 2개
    fit = estimate_static_sigma(every, sigma_f)
    assert (fit.sigma_db, fit.pairs, fit.at_edge) == (0.0, 57, True)  # 모두 맞혔다

    # 이웃한 두 기기의 값은 늘 10 dB 차이이고 프레임은 3개씩이다. σs=3.5이면 s² = 1 × 2/3 + 2 × 3.5²이라 z = 10 / 5.02 ≈ 1.99, P ≈ 0.977이다
    table = boundary_table(neighbors, sigma_f, 3.5, candidates=(0.9, 0.99))
    assert table == [BoundaryStat(0.9, 38, 38, 0, 1.0, 0.0), BoundaryStat(0.99, 38, 0, 0, 0.0, None)]
    chosen = choose_threshold(boundary_table(neighbors, sigma_f, 3.5), 0.05)
    assert chosen == 0.5  # 통과한 경계가 틀린 적이 없어서 가장 낮은 후보다
    rows = order_rows(
        frame_sigma_db=sigma_f, static_fit=fit, agreement=pair_agreement(every), chosen=chosen,
        test_stat=boundary_table(neighbors, sigma_f, 3.5, candidates=(chosen,))[0], max_error=0.05,
    )
    assert [row["metric"] for row in rows] == ROW_NAMES
    assert rows[-1]["passed"] is True and rows[-1]["failures"] == 0
