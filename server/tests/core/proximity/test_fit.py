"""근접 모델의 관측 값 적합 (FR-16). 합성 수집 회차에서 경로손실 값을 되찾는지, 잘못된 입력을 거부하는지 확인한다."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import timedelta

import pytest

from nearpilot.core.proximity import (
    NONE_LABEL,
    AnchorLayout,
    FitError,
    LabeledRun,
    ObservationParams,
    PlaceLayout,
    ProximityModel,
    fit_observation_params,
    run_frames,
)
from nearpilot.shared.models import RssiFrame

from .conftest import BEACON, OTHER_BEACON, T0, make_frame
from .runs import layout_of, make_run
from .synthetic import ANCHOR_SPACING, FRAME_OFFSET, RECEIVE_DELAY
from .test_filter import EXTREME_RSSI, MODEL_A, front_rssi

LAYOUT = layout_of(MODEL_A)
ANCHORS = sorted(MODEL_A.nodes)  # light-1, light-2, locker-1, locker-2, locker-3, speaker-1
NOISE_SIGMA = 2.0
RUN_SECONDS = 30

# 기기 a는 앵커가 (0, 0), 앞이 (1, 0)이고 기기 b는 앵커가 (11, 0), 앞이 (10, 0)이다. 두 앵커 모두 자기 기기 앞은 정확히 1 m,
# 다른 기기 앞은 정확히 10 m다. log10(1) = 0, log10(10) = 1이라서 계산에 부동소수점 오차가 끼지 않는다.
# 잡음 0 프레임이 정확히 한 직선 위에 놓이고, 손으로 정한 RSSI의 적합 결과를 손으로 계산할 수 있다.
EXACT_MODEL = replace(
    MODEL_A,
    nodes={
        "a": replace(MODEL_A.nodes["locker-1"], node_id="a", x=0.0, y=0.0, front_x=1.0, front_y=0.0),
        "b": replace(MODEL_A.nodes["locker-1"], node_id="b", x=11.0, y=0.0, front_x=10.0, front_y=0.0),
    },
    none_points=(),
)
EXACT_LAYOUT = layout_of(EXACT_MODEL)


# ── 도우미 ───────────────────────────────────────────────────────────


def make_runs(
    model: ProximityModel,
    labels: Sequence[str],
    *,
    noise_sigma: float = NOISE_SIGMA,
    copies: int = 2,
    first_seed: int = 0,
) -> list[LabeledRun]:
    """labels마다 회차를 copies개 만든다. 회차마다 회차 ID, 시작 시각, 시드가 다르다."""
    runs = []
    for i, label in enumerate(labels):
        for k in range(copies):
            index = i * copies + k
            runs.append(
                make_run(
                    model,
                    label,
                    run_id=f"{label}-{k}",
                    seconds=RUN_SECONDS,
                    start=T0 + timedelta(seconds=RUN_SECONDS * index),
                    seed=first_seed + index,
                    noise_sigma=noise_sigma,
                )
            )
    return runs


def hand_run(run_id: str, label: str, rssis: dict[str, list[float]]) -> LabeledRun:
    """앵커마다 손으로 정한 RSSI 목록으로 회차를 만든다. 프레임은 1초 간격이다."""
    frames = [
        make_frame(anchor_id, rssi, T0 + timedelta(seconds=i), seq=i + 1)
        for anchor_id, values in rssis.items()
        for i, rssi in enumerate(values)
    ]
    return LabeledRun(run_id, label, BEACON, T0, T0 + timedelta(seconds=10), frames)


FRONT_RUNS = make_runs(MODEL_A, ANCHORS)  # 기기마다 앞 회차 2개
NONE_RUNS = make_runs(MODEL_A, [NONE_LABEL], first_seed=1000)  # none 회차 2개
TRAIN_RUNS = [*FRONT_RUNS, *NONE_RUNS]
NOISE_FREE_NONE_RUNS = make_runs(MODEL_A, [NONE_LABEL], noise_sigma=0.0, first_seed=1000)

# 손으로 계산한 최소 입력이다. 앵커마다 앞 프레임 3개, 서로 다른 거리 2개, none 프레임 2개다.
# a는 1 m에서 −58, −60을, 10 m에서 −78을 받는다. u = [0, 0, −10]이라서 n = 1.9, A = −59, 잔차는 [1, −1, 0]이고
# sigma = √(2 / (3 − 2)) = √2다. b는 10 m에서 −78, −80을, 1 m에서 −58을 받는다. n = 2.1, A = −58, sigma = √2다.
# none은 앵커마다 −89, −91이다. 평균 −90, 편차 [1, −1], none_sigma = √(2 / (2 − 1)) = √2다.
MINIMUM_RUNS = [
    hand_run("a-1", "a", {"a": [-58.0, -60.0], "b": [-78.0, -80.0]}),
    hand_run("b-1", "b", {"a": [-78.0], "b": [-58.0]}),
    hand_run("none-1", NONE_LABEL, {"a": [-89.0, -91.0], "b": [-89.0, -91.0]}),
]


def fit(
    runs: Sequence[LabeledRun], *, layout: PlaceLayout = LAYOUT, min_distance_m: float = MODEL_A.min_distance_m,
) -> dict[str, ObservationParams]:
    return fit_observation_params(layout, runs, min_distance_m=min_distance_m)


def with_frames(run: LabeledRun, frames: Iterable[RssiFrame]) -> LabeledRun:
    return replace(run, frames=tuple(frames))


def drop_anchor(runs: Sequence[LabeledRun], anchor_id: str) -> list[LabeledRun]:
    """회차들에서 앵커 하나의 프레임을 모두 뺀다."""
    return [with_frames(run, (f for f in run.frames if f.node_id != anchor_id)) for run in runs]


def keep_one_frame(runs: Sequence[LabeledRun], anchor_id: str) -> list[LabeledRun]:
    """회차들에서 앵커 하나의 프레임을 모두 빼고, 첫 회차의 첫 프레임 하나만 되돌린다."""
    only = next(f for f in runs[0].frames if f.node_id == anchor_id)
    thinned = drop_anchor(runs, anchor_id)
    return [with_frames(thinned[0], (*thinned[0].frames, only)), *thinned[1:]]


def reverse_rssi(runs: Sequence[LabeledRun]) -> list[LabeledRun]:
    """1 m 기준 RSSI(−59 dBm)를 축으로 RSSI를 뒤집는다. 가까울수록 약해져서 적합한 n이 음수다."""
    return [with_frames(run, (replace(f, rssi=-118.0 - f.rssi) for f in run.frames)) for run in runs]


def with_foreign_frames(run: LabeledRun) -> LabeledRun:
    """적합에 쓰이지 않아야 하는 프레임을 회차의 프레임 목록 맨 앞에 더한다."""
    base = run.frames[0]
    foreign = [
        replace(base, beacon=OTHER_BEACON, rssi=EXTREME_RSSI),  # 다른 비콘
        replace(base, node_id="locker-9", rssi=EXTREME_RSSI),  # 배치에 없는 앵커
        # 구간 밖. seq가 달라서 중복 제거로 빠지지 않고 구간 검사로만 빠진다
        replace(base, frame_ts=run.ended_at, seq=base.seq + 1000, rssi=EXTREME_RSSI),
        replace(base, frame_ts=run.started_at - timedelta(seconds=1), seq=base.seq + 1001, rssi=EXTREME_RSSI),
        replace(base, seq=base.seq + 1002, rssi=math.nan),
        # 이미 있는 프레임과 키, 시각이 같고 RSSI만 크다. 정렬 순서가 뒤라서 버려져야 한다
        replace(base, rssi=base.rssi + 20.0),
    ]
    return with_frames(run, (*foreign, *run.frames))


# ── 적합 값 ──────────────────────────────────────────────────────────


def test_FR16_fit_recovers_path_loss():
    params = fit(TRAIN_RUNS)

    assert list(params) == ANCHORS  # 앵커 ID 정렬 순. 모델 파일의 기기 순서(locker-1부터)와 다르다
    for anchor_id, p in params.items():
        node = MODEL_A.nodes[anchor_id]
        assert p.A == pytest.approx(node.A, abs=1.5), anchor_id
        assert p.n == pytest.approx(node.n, abs=0.3), anchor_id
        assert p.sigma == pytest.approx(NOISE_SIGMA, abs=0.5), anchor_id  # 모델 파일의 sigma가 아니라 회차를 만든 잡음이다


def test_FR16_fit_none_params_from_none_runs():
    params = fit(TRAIN_RUNS)

    for anchor_id, p in params.items():
        observed = [f.rssi for run in NONE_RUNS for f in run_frames(run, LAYOUT) if f.node_id == anchor_id]
        assert len(observed) == 2 * RUN_SECONDS
        assert p.none_rssi == pytest.approx(statistics.mean(observed)), anchor_id
        assert p.none_sigma == pytest.approx(statistics.stdev(observed)), anchor_id


def test_FR16_fit_matches_hand_computed_minimum():
    # 최소 표본(앞 프레임 3개, 서로 다른 거리 2개, none 프레임 2개)에서 자유도 N − 2와 N − 1이 맞는지 손으로 계산한 값으로 본다
    params = fit(MINIMUM_RUNS, layout=EXACT_LAYOUT)

    assert list(params) == ["a", "b"]
    expected = {"a": (-59.0, 1.9, math.sqrt(2)), "b": (-58.0, 2.1, math.sqrt(2))}
    for anchor_id, p in params.items():
        assert (p.A, p.n, p.sigma) == pytest.approx(expected[anchor_id]), anchor_id
        assert (p.none_rssi, p.none_sigma) == pytest.approx((-90.0, math.sqrt(2))), anchor_id


def test_FR16_fit_clamps_distance_at_min_distance():
    # min_distance_m=1.0이면 기기 바로 앞(0.5 m)도 1 m로 본다. 회차를 만든 모델도 같은 값이다. 자르지 않으면 sigma가 크게 틀어진다.
    model = replace(MODEL_A, min_distance_m=1.0)
    runs = [*make_runs(model, ANCHORS, noise_sigma=0.5), *make_runs(model, [NONE_LABEL], noise_sigma=0.5, first_seed=1000)]

    params = fit(runs, layout=layout_of(model), min_distance_m=1.0)

    for anchor_id, p in params.items():
        assert p.A == pytest.approx(-59.0, abs=0.5), anchor_id
        assert p.n == pytest.approx(2.0, abs=0.15), anchor_id
        assert p.sigma == pytest.approx(0.5, abs=0.2), anchor_id


# ── 입력 검사 ────────────────────────────────────────────────────────


def test_FR16_fit_rejects_unknown_label():
    stray = replace(FRONT_RUNS[0], run_id="run-stray", label="locker-9")

    with pytest.raises(FitError) as info:
        fit([*TRAIN_RUNS, stray])

    assert "run-stray" in str(info.value)
    assert "locker-9" in str(info.value)


@pytest.mark.parametrize(
    ("runs", "missing"),
    [
        pytest.param(FRONT_RUNS, "none 회차가 없다", id="no_none_runs"),
        pytest.param(NONE_RUNS, "기기 앞 회차가 없다", id="no_front_runs"),
        pytest.param([], "기기 앞 회차와 none 회차가 없다", id="no_runs"),
    ],
)
def test_FR16_fit_rejects_missing_label_kind(runs, missing):
    with pytest.raises(FitError, match=missing):
        fit(runs)


@pytest.mark.parametrize(
    ("runs", "options", "expected"),
    [
        # 앞 회차가 한 기기뿐이면 앵커마다 거리가 하나다. 첫 앵커에서 멈춘다
        pytest.param(
            [*(r for r in FRONT_RUNS if r.label == "locker-1"), *NONE_RUNS], {}, r"'light-1'.*서로 다른 거리 1개",
            id="single_front_device",
        ),
        pytest.param(
            [*drop_anchor(FRONT_RUNS, "speaker-1"), *NONE_RUNS], {}, r"'speaker-1'.*기기 앞 회차.*프레임 0개",
            id="anchor_without_front_frames",
        ),
        # 거리는 둘이어도 앞 프레임이 2개면 모자란다
        pytest.param(
            [
                hand_run("a-1", "a", {"a": [-58.0], "b": [-78.0]}),
                hand_run("b-1", "b", {"a": [-78.0], "b": [-58.0]}),
                MINIMUM_RUNS[2],
            ],
            {"layout": EXACT_LAYOUT},
            r"'a'.*기기 앞 회차.*프레임 2개, 서로 다른 거리 2개",
            id="two_front_frames",
        ),
        pytest.param(
            [*FRONT_RUNS, *keep_one_frame(NONE_RUNS, "locker-2")], {}, r"'locker-2'.*none 회차.*프레임 1개",
            id="anchor_with_one_none_frame",
        ),
        # 모든 거리가 min_distance_m보다 짧으면 자른 뒤에는 거리가 하나다
        pytest.param(TRAIN_RUNS, {"min_distance_m": 100.0}, r"'light-1'.*서로 다른 거리 1개", id="clamped_to_one_distance"),
    ],
)
def test_FR16_fit_rejects_underdetermined_anchor(runs, options, expected):
    with pytest.raises(FitError, match=expected):
        fit(runs, **options)


@pytest.mark.parametrize(
    ("runs", "options", "expected"),
    [
        pytest.param([*reverse_rssi(FRONT_RUNS), *NONE_RUNS], {}, r"'light-1'.*n=-", id="front_rssi_reversed"),
        # 거리와 상관없이 RSSI가 같으면 n이 정확히 0이다. sigma는 0이 아니다
        pytest.param(
            [
                hand_run("a-1", "a", {"a": [-58.0, -60.0], "b": [-58.0, -60.0]}),
                hand_run("b-1", "b", {"a": [-58.0, -60.0], "b": [-58.0, -60.0]}),
                MINIMUM_RUNS[2],
            ],
            {"layout": EXACT_LAYOUT},
            r"'a'.*n=0\.0, sigma=1\.4",
            id="flat_rssi",
        ),
        # 정확히 한 직선 위의 프레임이라 sigma가 정확히 0이고, n은 2.0이다
        pytest.param(
            [
                *make_runs(EXACT_MODEL, ["a", "b"], noise_sigma=0.0),
                *make_runs(EXACT_MODEL, [NONE_LABEL], first_seed=1000),
            ],
            {"layout": EXACT_LAYOUT},
            r"'a'.*n=2\.0, sigma=0\.0",
            id="front_noise_free",
        ),
        pytest.param([*FRONT_RUNS, *NOISE_FREE_NONE_RUNS], {}, r"'light-1'.*none_sigma=0\.0", id="none_noise_free"),
        # 앵커 하나는 n·sigma를 먼저 본다. none 프레임이 없거나 none_sigma가 0이어도 n 오류가 먼저 나온다
        pytest.param(
            [*reverse_rssi(FRONT_RUNS), *drop_anchor(NONE_RUNS, "light-1")], {}, r"'light-1'.*n=-",
            id="n_checked_before_none_frames",
        ),
        pytest.param(
            [*reverse_rssi(FRONT_RUNS), *NOISE_FREE_NONE_RUNS], {}, r"'light-1'.*n=-", id="n_checked_before_none_sigma"
        ),
    ],
)
def test_FR16_fit_rejects_nonpositive_n(runs, options, expected):
    with pytest.raises(FitError, match=expected):
        fit(runs, **options)


# ── 프레임 고르기 ────────────────────────────────────────────────────


def test_FR16_fit_ignores_foreign_frames():
    clean = fit(TRAIN_RUNS)

    polluted = [with_foreign_frames(run) for run in TRAIN_RUNS]

    assert fit(polluted) == clean
    # 같은 프레임이 둘이면 먼저 정렬되는 쪽(RSSI가 작은 쪽)이 남는다. 목록 순서는 상관없다
    run = FRONT_RUNS[0]
    base = run.frames[0]
    kept = [f for f in run_frames(with_foreign_frames(run), LAYOUT) if (f.node_id, f.seq) == (base.node_id, base.seq)]
    assert kept == [base]
    assert run_frames(with_foreign_frames(run), LAYOUT) == run_frames(run, LAYOUT)


def test_FR16_run_frames_interval_is_half_open():
    run = hand_run("r", "a", {})
    one_us = timedelta(microseconds=1)
    edges = [run.started_at - one_us, run.started_at, run.ended_at - one_us, run.ended_at]
    edge_run = with_frames(run, (make_frame("a", -60.0, ts, seq=i + 1) for i, ts in enumerate(edges)))

    # started_at <= frame_ts < ended_at
    assert [f.frame_ts for f in run_frames(edge_run, EXACT_LAYOUT)] == edges[1:3]


def test_FR16_run_frames_does_not_filter_by_received_at():
    run = FRONT_RUNS[0]
    late = replace(run.frames[0], received_at=run.ended_at + timedelta(hours=1))

    reversed_run = with_frames(run, (*reversed(run.frames[1:]), late))

    # 늦게 도착한 프레임도 남고, 입력 순서를 뒤집어도 정렬된 결과는 같다
    assert run_frames(reversed_run, LAYOUT) == [late, *run_frames(run, LAYOUT)[1:]]


def test_FR16_fit_inputs_are_normalized():
    run = FRONT_RUNS[0]
    from_list = LabeledRun(run.run_id, run.label, run.beacon, run.started_at, run.ended_at, list(run.frames))
    assert from_list == run
    assert isinstance(from_list.frames, tuple)

    nodes = {"a": AnchorLayout(0.0, 0.0, 1.0, 0.0)}
    layout = PlaceLayout("place-x", nodes, none_points=[(1, 2)])
    nodes["b"] = AnchorLayout(5.0, 0.0, 6.0, 0.0)  # 넘긴 매핑을 바꿔도 배치는 그대로다
    assert list(layout.nodes) == ["a"]
    assert layout.none_points == ((1.0, 2.0),)
    assert all(isinstance(v, float) for point in layout.none_points for v in point)


# ── 합성 회차 생성기 ─────────────────────────────────────────────────


def test_FR16_make_run_follows_generator_contract():
    kwargs = {"run_id": "r", "seconds": 10, "start": T0, "seed": 7, "noise_sigma": 2.0}
    run = make_run(MODEL_A, "locker-2", **kwargs)

    # 같은 인자에는 같은 회차가 나오고, 시드가 다르면 다르다
    assert run == make_run(MODEL_A, "locker-2", **kwargs)
    assert run != make_run(MODEL_A, "locker-2", **{**kwargs, "seed": 8})
    assert (run.run_id, run.label, run.beacon) == ("r", "locker-2", BEACON)
    assert (run.started_at, run.ended_at) == (T0, T0 + timedelta(seconds=10))

    # 초마다 앵커를 정렬 순서로 프레임 하나씩 만든다
    assert len(run.frames) == 10 * len(ANCHORS)
    for i, frame in enumerate(run.frames):
        t, order = divmod(i, len(ANCHORS))
        assert frame.node_id == ANCHORS[order]
        assert frame.frame_ts == T0 + timedelta(seconds=t) + order * ANCHOR_SPACING + FRAME_OFFSET
        assert frame.received_at == frame.frame_ts + RECEIVE_DELAY
        assert frame.seq == t + 1
        assert run.started_at <= frame.frame_ts < run.ended_at

    # none 라벨은 앵커의 none_rssi가 평균이다 (MODEL_A는 모든 앵커가 -90)
    assert {f.rssi for f in make_run(MODEL_A, NONE_LABEL, **{**kwargs, "noise_sigma": 0.0}).frames} == {-90.0}

    # 오프셋은 회차마다 앵커별로 한 번 뽑는다. 잡음이 없으면 앵커마다 평균에서 같은 값만큼 벗어나고, 앵커끼리는 다르다
    shifted = make_run(MODEL_A, "locker-2", **{**kwargs, "noise_sigma": 0.0}, run_offset_sigma=3.0)
    gaps = {
        anchor_id: {f.rssi - front_rssi(MODEL_A, anchor_id, "locker-2") for f in shifted.frames if f.node_id == anchor_id}
        for anchor_id in ANCHORS
    }
    assert all(len(gap) == 1 for gap in gaps.values())
    assert len({next(iter(gap)) for gap in gaps.values()}) == len(ANCHORS)

    # 프레임이 빠져도 남은 프레임의 값은 탈락이 없을 때와 같다
    thinned = make_run(MODEL_A, "locker-2", **kwargs, drop_prob=0.5)
    by_key = {(f.node_id, f.seq): f for f in run.frames}
    assert 0 < len(thinned.frames) < len(run.frames)
    assert all(by_key[(f.node_id, f.seq)] == f for f in thinned.frames)
