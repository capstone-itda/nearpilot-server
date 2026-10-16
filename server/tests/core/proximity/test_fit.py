"""근접 모델의 관측 값 적합과 조합 선택 (FR-16).

합성 수집 회차에서 경로손실 값을 되찾는지, 검증 손실이 가장 작은 조합을 고르는지, 잘못된 입력을 거부하는지 확인한다.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from nearpilot.core.proximity import (
    NONE_LABEL,
    AnchorLayout,
    FitConfig,
    FitError,
    FitResult,
    LabeledRun,
    NodeModel,
    ObservationParams,
    PlaceLayout,
    ProximityModel,
    compute_posteriors,
    fit_model,
    fit_observation_params,
    run_frames,
    validation_log_loss,
)
from nearpilot.shared.models import RssiFrame

from .conftest import BEACON, OTHER_BEACON, T0, make_frame, make_node
from .runs import layout_of, make_run
from .synthetic import ANCHOR_SPACING, FRAME_OFFSET, RECEIVE_DELAY
from .test_filter import BIN_SEC, EXTREME_RSSI, MODEL_A, front_rssi

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


# ── 검증 손실과 조합 선택 ────────────────────────────────────────────

OFFSET_SIGMA = 4.0  # dB. 회차마다 앵커별로 한 번 뽑는 위치 오차다. 같은 자리에 선 회차의 위치 오차를 흉내 낸다
MODEL_W5 = replace(MODEL_A, window_sec=5.0, calibration_method="temperature", calibration_tau=2.0)  # 구간 5초, τ = 2
BASE_CONFIG = FitConfig("fit-1", "temp-1", MODEL_A.min_distance_m, taus=(1.0, 2.0), stays=(0.9,), window_secs=(5.0,))
# 학습 회차와 ID와 시드가 다른 검증 회차. 기기마다 하나와 none 하나다
VALIDATION_RUNS = [
    replace(run, run_id=f"va-{run.run_id}")
    for run in make_runs(MODEL_A, [*ANCHORS, NONE_LABEL], copies=1, first_seed=5000)
]


def make_offset_runs(prefix: str, *, first_seed: int, copies: int, seconds: int) -> list[LabeledRun]:
    """기기마다 앞 회차와 none 회차를 copies개씩 만든다. 회차마다 앵커별 위치 오차(OFFSET_SIGMA)가 있다.

    회차 ID는 `{prefix}-{라벨}-{k}`이고, 시작 시각은 회차마다 seconds초씩 뒤로 간다.
    """
    runs = []
    for i, label in enumerate([*ANCHORS, NONE_LABEL]):
        for k in range(copies):
            index = i * copies + k
            runs.append(
                make_run(
                    MODEL_A,
                    label,
                    run_id=f"{prefix}-{label}-{k}",
                    seconds=seconds,
                    start=T0 + timedelta(seconds=seconds * index),
                    seed=first_seed + index,
                    noise_sigma=NOISE_SIGMA,
                    run_offset_sigma=OFFSET_SIGMA,
                )
            )
    return runs


def standing_run(run_id: str, seconds: int, *, label: str = "locker-1", seed: int = 1) -> LabeledRun:
    """label 앞에 seconds초 서 있는 회차. 잡음만 있고 위치 오차는 없다."""
    return make_run(MODEL_A, label, run_id=run_id, seconds=seconds, start=T0, seed=seed, noise_sigma=NOISE_SIGMA)


def moving_run(first: str, then: str, *, run_id: str, seconds: int, switch: int, seed: int) -> LabeledRun:
    """처음 switch초는 first 앞에, 그 뒤는 then 앞에 서 있는 회차. 라벨은 first다. 평가 시각마다 정답 확률이 다르다."""
    before = make_run(MODEL_A, first, run_id=run_id, seconds=seconds, start=T0, seed=seed, noise_sigma=NOISE_SIGMA)
    after = make_run(MODEL_A, then, run_id=run_id, seconds=seconds, start=T0, seed=seed + 1, noise_sigma=NOISE_SIGMA)
    cut = T0 + timedelta(seconds=switch)
    frames = [*(f for f in before.frames if f.frame_ts < cut), *(f for f in after.frames if f.frame_ts >= cut)]
    return replace(before, frames=tuple(frames))


def mean_loss_at(model: ProximityModel, run: LabeledRun, times: Sequence[datetime], bin_sec: float) -> float:
    """times의 시각마다 `compute_posteriors`를 직접 불러 −log P(정답)의 평균을 낸다. place-a의 기기 전부가 후보다."""
    nodes = [make_node(node_id) for node_id in model.nodes]
    losses = []
    for at in times:
        posteriors = compute_posteriors(model, run.frames, run.beacon, nodes, nodes, at, bin_sec=bin_sec).posteriors
        p = 1.0 - math.fsum(posteriors.values()) if run.label == NONE_LABEL else posteriors[run.label]
        losses.append(-math.log(p))
    return math.fsum(losses) / len(losses)


def run_fit(
    *,
    train: Sequence[LabeledRun] = TRAIN_RUNS,
    validation: Sequence[LabeledRun] = VALIDATION_RUNS[:1],
    config: FitConfig = BASE_CONFIG,
    bin_sec: float = BIN_SEC,
) -> FitResult:
    return fit_model(LAYOUT, train, validation, config, bin_sec=bin_sec)


# ── 검증 로그 손실 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bin_sec", "count"),
    [pytest.param(1.0, 8, id="bin_1s"), pytest.param(0.5, 15, id="bin_0_5s"), pytest.param(0.1, 71, id="bin_0_1s")],
)
def test_FR16_validation_log_loss_follows_evaluation_times(bin_sec, count):
    run = moving_run("locker-2", "speaker-1", run_id="va-move", seconds=12, switch=6, seed=3)
    # 평가 시각은 시작 + 5초(window_sec)부터 bin_sec 간격으로 끝 이하까지다.
    # 기대 시각은 정수 마이크로초로 세서 float 오차가 낄 틈이 없다.
    step_us = round(bin_sec * 1_000_000)
    times = [T0 + timedelta(microseconds=5_000_000 + k * step_us) for k in range(count)]
    assert times[-1] == run.ended_at

    expected = mean_loss_at(MODEL_W5, run, times, bin_sec)

    assert validation_log_loss(MODEL_W5, LAYOUT, [run], bin_sec=bin_sec) == pytest.approx(expected)
    # 끝 시각을 빼거나 시작 시각을 한 칸 늦춰도 값이 달라지는 입력이다
    assert mean_loss_at(MODEL_W5, run, times[:-1], bin_sec) != pytest.approx(expected, rel=1e-3)
    assert mean_loss_at(MODEL_W5, run, times[1:], bin_sec) != pytest.approx(expected, rel=1e-3)


@pytest.mark.parametrize("label", ["locker-2", "locker-1", NONE_LABEL])
def test_FR16_validation_log_loss_scores_correct_state(label):
    # locker-2 앞에 선 프레임에 라벨만 바꿔 붙인다. 구간이 window_sec와 같아서 평가 시각이 끝 하나뿐이다.
    run = replace(standing_run("va-one", 5, label="locker-2", seed=9), label=label)
    nodes = [make_node(node_id) for node_id in MODEL_W5.nodes]
    posteriors = compute_posteriors(
        MODEL_W5, run.frames, run.beacon, nodes, nodes, run.ended_at, bin_sec=BIN_SEC
    ).posteriors

    # 정답이 기기이면 그 기기의 확률이고, none이면 후보 확률을 뺀 나머지다
    p = 1.0 - math.fsum(posteriors.values()) if label == NONE_LABEL else posteriors[label]

    assert 1e-6 < p < 1.0  # none의 나머지도 반올림 잡음(1e-16)보다 훨씬 크다
    assert validation_log_loss(MODEL_W5, LAYOUT, [run], bin_sec=BIN_SEC) == pytest.approx(-math.log(p))


def test_FR16_validation_log_loss_averages_runs_equally():
    short = standing_run("va-short", 6)  # 평가 시각 2개
    long = moving_run("locker-1", "speaker-1", run_id="va-long", seconds=16, switch=8, seed=5)  # 평가 시각 12개
    loss = {
        run.run_id: validation_log_loss(MODEL_W5, LAYOUT, [run], bin_sec=BIN_SEC) for run in (short, long)
    }
    assert abs(loss["va-short"] - loss["va-long"]) > 0.1  # 회차 손실이 달라야 단순 평균과 시각 수 가중 평균이 갈린다

    both = validation_log_loss(MODEL_W5, LAYOUT, [short, long], bin_sec=BIN_SEC)

    # 회차가 독립 단위다. 회차 손실의 단순 평균이고, 평가 시각이 많은 회차에 끌려가지 않는다
    assert both == pytest.approx((loss["va-short"] + loss["va-long"]) / 2)
    assert both != pytest.approx((2 * loss["va-short"] + 12 * loss["va-long"]) / 14, rel=1e-3)


def test_FR16_validation_log_loss_ignores_unusable_frames():
    run = moving_run("locker-1", "speaker-1", run_id="va-move", seconds=12, switch=6, seed=3)
    clean = validation_log_loss(MODEL_W5, LAYOUT, [run], bin_sec=BIN_SEC)
    # 다른 비콘, 배치에 없는 앵커, 구간 밖(끝 시각 포함), 중복 프레임, 평가 시각보다 늦게 도착한 프레임을 더한다
    late = replace(
        run.frames[-1], seq=run.frames[-1].seq + 2000, rssi=EXTREME_RSSI, received_at=run.ended_at + timedelta(hours=1)
    )
    foreign = with_foreign_frames(run)
    polluted = with_frames(foreign, (*foreign.frames, late))

    assert validation_log_loss(MODEL_W5, LAYOUT, [polluted], bin_sec=BIN_SEC) == clean
    # 회차의 프레임을 그대로 넘기면 끝 시각의 프레임이 마지막 평가 시각에 쓰여서 값이 달라진다. 거르는 쪽이 `run_frames`다.
    times = [T0 + timedelta(seconds=5 + k) for k in range(8)]
    assert mean_loss_at(MODEL_W5, polluted, times, BIN_SEC) != pytest.approx(clean, rel=1e-3)


@pytest.mark.parametrize(
    ("runs", "bin_sec", "expected"),
    [
        pytest.param([], BIN_SEC, "검증 회차가 없다", id="no_runs"),
        pytest.param([standing_run("va-short", 4)], BIN_SEC, "va-short", id="run_shorter_than_window"),
        pytest.param(
            [standing_run("va-ok", 8), standing_run("va-short", 4)],
            BIN_SEC,
            "va-short",
            id="second_run_shorter_than_window",
        ),
        pytest.param(
            [replace(standing_run("va-stray", 8), label="locker-9")], BIN_SEC, "va-stray", id="unknown_label"
        ),
        pytest.param([standing_run("va-ok", 8)], 0.0, "bin_sec", id="bin_sec_zero"),
        pytest.param([standing_run("va-ok", 8)], -1.0, "bin_sec", id="bin_sec_negative"),
        pytest.param([standing_run("va-ok", 8)], math.nan, "bin_sec", id="bin_sec_nan"),
        pytest.param([standing_run("va-ok", 8)], math.inf, "bin_sec", id="bin_sec_inf"),
    ],
)
def test_FR16_validation_log_loss_rejects_invalid_input(runs, bin_sec, expected):
    with pytest.raises(FitError, match=expected):
        validation_log_loss(MODEL_W5, LAYOUT, runs, bin_sec=bin_sec)


# ── 설정과 회차 검사 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("train", "validation", "shared"),
    [
        pytest.param(
            TRAIN_RUNS,
            [replace(VALIDATION_RUNS[0], run_id=TRAIN_RUNS[3].run_id)],
            TRAIN_RUNS[3].run_id,
            id="train_and_validation",
        ),
        pytest.param(
            [*TRAIN_RUNS, replace(TRAIN_RUNS[1], run_id=TRAIN_RUNS[0].run_id)],
            VALIDATION_RUNS[:1],
            TRAIN_RUNS[0].run_id,
            id="twice_in_train",
        ),
        pytest.param(
            TRAIN_RUNS,
            [VALIDATION_RUNS[0], replace(VALIDATION_RUNS[1], run_id=VALIDATION_RUNS[0].run_id)],
            VALIDATION_RUNS[0].run_id,
            id="twice_in_validation",
        ),
    ],
)
def test_FR16_fit_rejects_shared_run(train, validation, shared):
    with pytest.raises(FitError, match="겹친") as info:
        run_fit(train=train, validation=validation)

    assert shared in str(info.value)


@pytest.mark.parametrize(
    ("overrides", "bin_sec", "field"),
    [
        pytest.param({"taus": ()}, BIN_SEC, "taus", id="empty_taus"),
        pytest.param({"stays": ()}, BIN_SEC, "stays", id="empty_stays"),
        pytest.param({"window_secs": ()}, BIN_SEC, "window_secs", id="empty_window_secs"),
        pytest.param({"taus": (0.0,)}, BIN_SEC, "taus", id="tau_zero"),
        pytest.param({"taus": (1.0, -1.0)}, BIN_SEC, "taus", id="tau_negative_after_valid"),
        pytest.param({"taus": (math.nan,)}, BIN_SEC, "taus", id="tau_nan"),
        pytest.param({"taus": (math.inf,)}, BIN_SEC, "taus", id="tau_inf"),
        pytest.param({"taus": (True,)}, BIN_SEC, "taus", id="tau_bool"),
        pytest.param({"taus": ("2.0",)}, BIN_SEC, "taus", id="tau_string"),
        pytest.param({"taus": (10**400,)}, BIN_SEC, "taus", id="tau_int_too_large_for_float"),
        pytest.param({"stays": (0.0,)}, BIN_SEC, "stays", id="stay_zero"),
        pytest.param({"stays": (1.0,)}, BIN_SEC, "stays", id="stay_one"),
        pytest.param({"stays": (1.5,)}, BIN_SEC, "stays", id="stay_above_one"),
        pytest.param({"stays": (-0.5,)}, BIN_SEC, "stays", id="stay_negative"),
        pytest.param({"stays": (math.nan,)}, BIN_SEC, "stays", id="stay_nan"),
        pytest.param({"window_secs": (0.0,)}, BIN_SEC, "window_secs", id="window_zero"),
        pytest.param({"window_secs": (-5.0,)}, BIN_SEC, "window_secs", id="window_negative"),
        pytest.param({"window_secs": (math.inf,)}, BIN_SEC, "window_secs", id="window_inf"),
        pytest.param({"window_secs": (math.nan,)}, BIN_SEC, "window_secs", id="window_nan"),
        pytest.param({"window_secs": (True,)}, BIN_SEC, "window_secs", id="window_bool"),
        pytest.param({"model_version": ""}, BIN_SEC, "model_version", id="empty_model_version"),
        pytest.param({"model_version": None}, BIN_SEC, "model_version", id="model_version_none"),
        pytest.param({"calib_version": ""}, BIN_SEC, "calib_version", id="empty_calib_version"),
        pytest.param({"min_distance_m": 0.0}, BIN_SEC, "min_distance_m", id="min_distance_zero"),
        pytest.param({"min_distance_m": -0.3}, BIN_SEC, "min_distance_m", id="min_distance_negative"),
        pytest.param({"min_distance_m": math.nan}, BIN_SEC, "min_distance_m", id="min_distance_nan"),
        pytest.param({"min_distance_m": math.inf}, BIN_SEC, "min_distance_m", id="min_distance_inf"),
        pytest.param({"min_distance_m": True}, BIN_SEC, "min_distance_m", id="min_distance_bool"),
        pytest.param({}, 0.0, "bin_sec", id="bin_sec_zero"),
        pytest.param({}, -1.0, "bin_sec", id="bin_sec_negative"),
        pytest.param({}, math.nan, "bin_sec", id="bin_sec_nan"),
        pytest.param({}, math.inf, "bin_sec", id="bin_sec_inf"),
        pytest.param({}, True, "bin_sec", id="bin_sec_bool"),
    ],
)
def test_FR16_fit_rejects_invalid_grid(overrides, bin_sec, field):
    with pytest.raises(FitError, match=field):
        run_fit(config=replace(BASE_CONFIG, **overrides), bin_sec=bin_sec)


@pytest.mark.parametrize(
    ("overrides", "bin_sec", "field"),
    [
        pytest.param({"min_distance_m": 0.0}, BIN_SEC, "min_distance_m", id="min_distance"),
        pytest.param({"taus": ()}, BIN_SEC, "taus", id="taus"),
        pytest.param({}, 0.0, "bin_sec", id="bin_sec"),
    ],
)
def test_FR16_fit_checks_config_before_fitting(overrides, bin_sec, field):
    # 학습 회차에 none 회차가 없다. 적합을 먼저 하면 "none 회차가 없다"는 다른 FitError가 난다.
    with pytest.raises(FitError, match=field):
        run_fit(train=FRONT_RUNS, config=replace(BASE_CONFIG, **overrides), bin_sec=bin_sec)


def test_FR16_fit_checks_min_distance_before_log10():
    # 앵커 a의 기기 앞이 앵커 위치와 같다. min_distance_m가 0이면 적합이 log10(0)에서 ValueError로 막힌다.
    # 설정 검사가 먼저여야 FitError가 난다.
    layout = PlaceLayout("place-x", {"a": AnchorLayout(0.0, 0.0, 0.0, 0.0), "b": AnchorLayout(11.0, 0.0, 10.0, 0.0)})
    config = FitConfig("fit-1", "temp-1", 0.0, taus=(1.0,), stays=(0.9,), window_secs=(5.0,))
    validation = [hand_run("va-1", "a", {"a": [-58.0, -60.0], "b": [-78.0, -80.0]})]

    with pytest.raises(FitError, match="min_distance_m"):
        fit_model(layout, MINIMUM_RUNS, validation, config, bin_sec=BIN_SEC)


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        pytest.param([replace(VALIDATION_RUNS[0], run_id=FRONT_RUNS[0].run_id)], "겹친", id="shared_run"),
        pytest.param(
            [replace(VALIDATION_RUNS[0], run_id="va-stray", label="locker-9")], "va-stray", id="unknown_label"
        ),
        pytest.param([standing_run("va-short", 4)], "va-short", id="short_run"),
        pytest.param([], "검증 회차가 없다", id="no_validation_runs"),
    ],
)
def test_FR16_fit_checks_runs_before_fitting(validation, expected):
    # 학습 회차에 none 회차가 없다. 적합을 먼저 하면 "none 회차가 없다"는 다른 FitError가 난다.
    # 4초 회차는 window_sec 3초에는 충분하지만 가장 긴 8초에는 짧다.
    config = replace(BASE_CONFIG, window_secs=(3.0, 8.0))

    with pytest.raises(FitError, match=expected):
        run_fit(train=FRONT_RUNS, validation=validation, config=config)


def test_FR16_fit_rejects_unknown_validation_label():
    stray = replace(VALIDATION_RUNS[0], run_id="va-stray", label="locker-9")

    with pytest.raises(FitError) as info:
        run_fit(validation=[VALIDATION_RUNS[1], stray])

    assert "va-stray" in str(info.value)
    assert "locker-9" in str(info.value)


@pytest.mark.parametrize(
    ("train", "expected"),
    [
        pytest.param(FRONT_RUNS, "none 회차가 없다", id="no_none_runs"),
        pytest.param(
            [*TRAIN_RUNS, replace(FRONT_RUNS[0], run_id="run-stray", label="locker-9")], "run-stray", id="unknown_label"
        ),
    ],
)
def test_FR16_fit_model_propagates_observation_errors(train, expected):
    with pytest.raises(FitError, match=expected):
        run_fit(train=train)


@pytest.mark.parametrize(
    ("validation", "short_id"),
    [
        # 5초 회차는 window_sec 3초에는 충분하지만 가장 긴 8초에는 짧다
        pytest.param([standing_run("va-short", 5)], "va-short", id="shorter_than_longest_window"),
        pytest.param([standing_run("va-ok", 20), standing_run("va-short", 7)], "va-short", id="second_run_is_short"),
        pytest.param(
            [replace(standing_run("va-edge", 8), ended_at=T0 + timedelta(seconds=8) - timedelta(microseconds=1))],
            "va-edge",
            id="one_microsecond_short",
        ),
        pytest.param([], None, id="no_validation_runs"),
    ],
)
def test_FR16_fit_rejects_short_validation_run(validation, short_id):
    config = replace(BASE_CONFIG, window_secs=(3.0, 8.0))

    with pytest.raises(FitError) as info:
        run_fit(validation=validation, config=config)

    if short_id is not None:
        assert short_id in str(info.value)


def test_FR16_fit_accepts_validation_run_as_long_as_longest_window():
    config = replace(BASE_CONFIG, window_secs=(3.0, 8.0))

    # 8초 회차의 가장 긴 window_sec(8초) 평가 시각은 끝 하나다
    result = run_fit(validation=[standing_run("va-edge", 8)], config=config)

    assert len(result.losses) == 4
    assert all(math.isfinite(grid.loss) for grid in result.losses)


# ── 조합 선택 ────────────────────────────────────────────────────────


def test_FR16_zero_probability_loses():
    # 학습 잡음이 0.5 dB라서 sigma가 약 0.5다. 잡음 2 dB로 적합하면 sigma가 약 2라서
    # τ = 1에서도 정답 확률이 약 e^-140으로 0이 되지 않는다.
    train = [
        *make_runs(MODEL_A, ANCHORS, noise_sigma=0.5),
        *make_runs(MODEL_A, [NONE_LABEL], noise_sigma=0.5, first_seed=1000),
    ]
    # 라벨은 locker-1 앞인데 프레임은 speaker-1 앞의 것이다 (잡음 0). τ = 1이면 locker-1의 확률이 0으로 내려간다.
    standing = make_run(MODEL_A, "speaker-1", run_id="va-speaker", seconds=20, start=T0, seed=77, noise_sigma=0.0)
    mislabeled = replace(standing, run_id="va-mislabeled", label="locker-1")
    config = replace(BASE_CONFIG, taus=(1.0, 1000.0))

    result = run_fit(train=train, validation=[mislabeled], config=config)

    assert result.losses[0].loss == math.inf  # 전제: τ = 1에서 손실이 무한대다
    assert math.isfinite(result.losses[1].loss)
    assert result.model.calibration_tau == 1000.0  # 무한대인 조합이 아니라 유한한 조합을 고른다
    grid = result.model.validation["grid"]
    assert grid[0]["loss"] is None  # 표준 JSON에는 무한대가 없다
    assert grid[1]["loss"] == result.losses[1].loss
    assert result.model.validation["loss"] == result.losses[1].loss
    # 정답 확률이 0인 회차가 하나라도 있으면 다른 회차가 멀쩡해도 조합의 손실이 무한대다
    sharp = replace(result.model, calibration_tau=1.0)
    assert validation_log_loss(sharp, LAYOUT, [standing], bin_sec=BIN_SEC) < 1.0
    assert validation_log_loss(sharp, LAYOUT, [standing, mislabeled], bin_sec=BIN_SEC) == math.inf

    with pytest.raises(FitError, match="무한대"):  # 후보가 무한대뿐이면 고를 근거가 없다
        run_fit(train=train, validation=[mislabeled], config=replace(config, taus=(1.0,)))


def test_FR16_select_minimizes_validation_log_loss():
    # 회차마다 앵커별 위치 오차가 있으면 칸마다 같은 오차가 증거로 쌓여서, 증거를 덜 믿는 쪽(τ > 1)이 검증 손실이 작다
    train = make_offset_runs("tr", first_seed=0, copies=2, seconds=30)
    validation = make_offset_runs("va", first_seed=50, copies=2, seconds=20)
    config = replace(BASE_CONFIG, taus=(1.0, 2.0, 4.0))

    result = run_fit(train=train, validation=validation, config=config)

    model = result.model
    losses = [grid.loss for grid in result.losses]
    combination = (model.calibration_tau, model.stay, model.window_sec)
    chosen = next(g for g in result.losses if (g.tau, g.stay, g.window_sec) == combination)
    assert chosen.loss == min(losses)
    assert model.calibration_tau > 1.0, f"고른 τ가 1이다. 관측한 손실: {losses}"
    # 각 손실은 같은 조합으로 직접 만든 모델의 검증 손실이다
    for grid in result.losses:
        direct = replace(model, calibration_tau=grid.tau, stay=grid.stay, window_sec=grid.window_sec)
        assert grid.loss == validation_log_loss(direct, LAYOUT, validation, bin_sec=BIN_SEC)
    summary = model.validation
    assert summary["criterion"] == "log_loss"
    assert summary["loss"] == chosen.loss
    assert list(summary["train_runs"]) == [run.run_id for run in train]  # 입력 순서 그대로다
    assert list(summary["validation_runs"]) == [run.run_id for run in validation]
    assert [dict(entry) for entry in summary["grid"]] == [
        {"tau": g.tau, "stay": g.stay, "window_sec": g.window_sec, "loss": g.loss} for g in result.losses
    ]


def test_FR16_select_picks_minimum_at_any_position():
    train = make_offset_runs("tr", first_seed=0, copies=2, seconds=30)
    validation = make_offset_runs("va", first_seed=50, copies=1, seconds=20)
    orders = [(1.0, 2.0, 4.0), (4.0, 2.0, 1.0), (2.0, 4.0, 1.0)]

    results = [run_fit(train=train, validation=validation, config=replace(BASE_CONFIG, taus=taus)) for taus in orders]

    # 조합의 손실은 후보 순서와 상관없고, 가장 작은 조합은 어느 자리에 있어도 고른다
    by_tau = {grid.tau: grid.loss for grid in results[0].losses}
    best_tau = min(by_tau, key=by_tau.get)
    for taus, result in zip(orders, results):
        assert [grid.tau for grid in result.losses] == list(taus)
        assert {grid.tau: grid.loss for grid in result.losses} == by_tau
        assert result.model.calibration_tau == best_tau
    # 최솟값이 자리를 옮기는 입력이다
    assert len({taus.index(best_tau) for taus in orders}) > 1


def test_FR16_grid_loss_belongs_to_its_combination():
    validation = [standing_run("va-locker", 12), standing_run("va-speaker", 12, label="speaker-1", seed=2)]
    config = replace(BASE_CONFIG, taus=(1.0, 2.0), stays=(0.7, 0.9), window_secs=(3.0, 5.0))

    result = run_fit(validation=validation, config=config)

    assert [(g.tau, g.stay, g.window_sec) for g in result.losses] == [
        (tau, stay, window_sec) for tau in (1.0, 2.0) for stay in (0.7, 0.9) for window_sec in (3.0, 5.0)
    ]
    assert len({g.loss for g in result.losses}) == 8  # 조합마다 손실이 달라야 필드가 뒤바뀌면 드러난다
    for grid in result.losses:
        direct = replace(result.model, calibration_tau=grid.tau, stay=grid.stay, window_sec=grid.window_sec)
        assert grid.loss == validation_log_loss(direct, LAYOUT, validation, bin_sec=BIN_SEC), grid


@pytest.mark.parametrize("label", ["locker-1", NONE_LABEL])
def test_FR16_tie_breaks_by_grid_order(label):
    # 프레임이 없으면 모든 조합의 사후확률이 처음 분포(1/7: 기기 6개와 none)라서 손실이 모두 같다
    blank = LabeledRun("va-blank", label, BEACON, T0, T0 + timedelta(seconds=20), ())
    taus, stays, window_secs = (4.0, 1.0), (0.95, 0.8), (5.0, 3.0)

    result = run_fit(validation=[blank], config=FitConfig("fit-1", "temp-1", 0.3, taus, stays, window_secs))

    assert len({grid.loss for grid in result.losses}) == 1
    assert result.losses[0].loss == pytest.approx(math.log(7))
    nested = [(tau, stay, window_sec) for tau in taus for stay in stays for window_sec in window_secs]
    assert [(g.tau, g.stay, g.window_sec) for g in result.losses] == nested
    assert [(e["tau"], e["stay"], e["window_sec"]) for e in result.model.validation["grid"]] == nested
    model = result.model
    assert (model.calibration_tau, model.stay, model.window_sec) == (4.0, 0.95, 5.0)
    # 후보 순서를 뒤집으면 고른 조합도 뒤집힌 목록의 첫 조합이다
    flipped_config = FitConfig("fit-1", "temp-1", 0.3, taus[::-1], stays[::-1], window_secs[::-1])
    flipped = run_fit(validation=[blank], config=flipped_config).model
    assert (flipped.calibration_tau, flipped.stay, flipped.window_sec) == (1.0, 0.8, 3.0)


# ── 모델 ─────────────────────────────────────────────────────────────


def test_FR16_fit_model_follows_layout_and_params():
    config = FitConfig("fit-7", "temp-7", 0.5, taus=(1.0,), stays=(0.8,), window_secs=(4.0,))

    result = run_fit(config=config)

    model = result.model
    params = fit(TRAIN_RUNS, min_distance_m=0.5)
    assert (model.place_id, model.model_version, model.calib_version) == ("place-a", "fit-7", "temp-7")
    assert (model.window_sec, model.stay, model.min_distance_m) == (4.0, 0.8, 0.5)
    assert (model.calibration_method, model.calibration_tau) == ("temperature", 1.0)  # τ = 1이어도 temperature다
    assert list(model.nodes) == ANCHORS  # 배치의 순서(locker-1부터)가 아니라 node_id 정렬 순이다
    for node_id, node in model.nodes.items():
        anchor, p = LAYOUT.nodes[node_id], params[node_id]
        assert node == NodeModel(
            node_id, anchor.x, anchor.y, anchor.front_x, anchor.front_y, p.A, p.n, p.sigma, p.none_rssi, p.none_sigma
        )
    assert model.none_points == LAYOUT.none_points
    assert run_fit(config=config) == result  # 같은 입력에는 같은 결과다


def test_FR16_fit_config_is_normalized():
    config = FitConfig("fit-1", "temp-1", 1, taus=[2], stays=[0.5], window_secs=[5])

    assert (config.taus, config.stays, config.window_secs) == ((2,), (0.5,), (5,))  # list를 받아도 tuple이다
    result = run_fit(config=config)

    # 정수로 쓴 값도 모델과 손실에는 float로 들어간다
    model, grid = result.model, result.losses[0]
    assert all(type(v) is float for v in (model.calibration_tau, model.stay, model.window_sec, model.min_distance_m))
    assert all(type(v) is float for v in (grid.tau, grid.stay, grid.window_sec))


# ── 검토 반영: 프레임 선택, 칸 길이, 이름과 배치 검사 ─────────────────


def test_FR16_run_frames_keys_by_node_and_session():
    run = hand_run("r", "a", {})
    frames = [
        # 같은 앵커가 다시 연결해서 seq가 1부터 다시 시작한다. 연결 ID가 달라서 둘 다 쓴다
        make_frame("a", -60.0, T0 + timedelta(seconds=1), seq=1, session_id="s-old"),
        make_frame("a", -61.0, T0 + timedelta(seconds=2), seq=1, session_id="s-new"),
        # 서로 다른 앵커의 연결 ID와 seq가 같다. 앵커가 달라서 둘 다 쓴다
        make_frame("a", -62.0, T0 + timedelta(seconds=3), seq=7, session_id="s-same"),
        make_frame("b", -63.0, T0 + timedelta(seconds=3), seq=7, session_id="s-same"),
    ]

    assert run_frames(with_frames(run, frames), EXACT_LAYOUT) == frames


@pytest.mark.parametrize("rssi", [math.inf, -math.inf, math.nan])
def test_FR16_run_frames_drops_non_finite_rssi(rssi):
    run = hand_run("r", "a", {"a": [-60.0]})
    bad = make_frame("a", rssi, T0 + timedelta(seconds=5), seq=99)

    assert run_frames(with_frames(run, (*run.frames, bad)), EXACT_LAYOUT) == list(run.frames)


def test_FR16_fit_model_uses_given_bin_sec():
    config = replace(BASE_CONFIG, taus=(1.0, 2.0))
    result = run_fit(config=config, bin_sec=0.5)

    for grid in result.losses:
        direct = replace(result.model, calibration_tau=grid.tau, stay=grid.stay, window_sec=grid.window_sec)
        assert grid.loss == validation_log_loss(direct, LAYOUT, VALIDATION_RUNS[:1], bin_sec=0.5)
        # 칸 길이가 다르면 평가 시각과 칸이 달라서 손실도 다르다. 받은 값을 그대로 넘겼는지 드러난다
        assert grid.loss != validation_log_loss(direct, LAYOUT, VALIDATION_RUNS[:1], bin_sec=1.0)


NON_COMPONENT_NAMES = ["", ".", "..", "sub/v1", "../../escaped", "a\\b"]


@pytest.mark.parametrize("name", NON_COMPONENT_NAMES)
def test_FR16_fit_rejects_non_component_names(name):
    with pytest.raises(FitError, match="model_version"):
        run_fit(config=replace(BASE_CONFIG, model_version=name))
    with pytest.raises(FitError, match="place_id"):
        fit_model(replace(LAYOUT, place_id=name), TRAIN_RUNS, VALIDATION_RUNS[:1], BASE_CONFIG, bin_sec=BIN_SEC)


@pytest.mark.parametrize("min_distance_m", [0.0, -1.0, math.nan, math.inf])
def test_FR16_fit_observation_params_checks_min_distance(min_distance_m):
    with pytest.raises(FitError, match="min_distance_m"):
        fit(MINIMUM_RUNS, layout=EXACT_LAYOUT, min_distance_m=min_distance_m)


@pytest.mark.parametrize("field", ["x", "y", "front_x", "front_y"])
def test_FR16_fit_rejects_non_finite_layout(field):
    nodes = {**EXACT_LAYOUT.nodes, "a": replace(EXACT_LAYOUT.nodes["a"], **{field: math.nan})}

    with pytest.raises(FitError, match=f"'a'.{field}"):
        fit(MINIMUM_RUNS, layout=replace(EXACT_LAYOUT, nodes=nodes))
    with pytest.raises(FitError, match="none_points"):
        fit(MINIMUM_RUNS, layout=replace(EXACT_LAYOUT, none_points=((0.0, math.inf),)))


def test_FR16_fit_rejects_bin_sec_below_timedelta_resolution():
    with pytest.raises(FitError, match="bin_sec"):
        run_fit(bin_sec=1e-7)
    with pytest.raises(FitError, match="bin_sec"):
        validation_log_loss(MODEL_W5, LAYOUT, VALIDATION_RUNS[:1], bin_sec=1e-7)


def test_FR16_fit_counts_distances_after_log():
    # 두 앞 위치의 거리는 다르지만(5.0과 그보다 한 ulp 큰 값) log10 뒤 값이 같다. 최소제곱의 분모가 0이 된다
    far = math.nextafter(5.0, math.inf)
    layout = PlaceLayout(
        "place-x", {"a": AnchorLayout(0.0, 0.0, 5.0, 0.0), "b": AnchorLayout(0.0, 0.0, far, 0.0)},
    )
    assert -10.0 * math.log10(5.0) == -10.0 * math.log10(far)  # 전제

    with pytest.raises(FitError, match="'a'.*서로 다른 거리 1개"):
        fit(MINIMUM_RUNS, layout=layout)
