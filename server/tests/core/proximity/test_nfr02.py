"""NFR-02 합성 경로 회귀 테스트 (NFR-02, FR-16). 시드를 고정한 합성 경로로 추정기의 반응을 잰다.

이 테스트는 합성 데이터의 회귀 테스트다. NFR-02의 인수는 실데이터(지표 8)로 한다.
생성기 값, 시드, 기준은 고정이다. 실패해도 바꾸지 않고 측정값을 그대로 보고한다.
합성 잡음은 모델의 sigma와 같다. 모델이 잡음을 맞게 아는 경우의 회귀를 잰다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import numpy
import pytest
from scipy.stats import norm

from nearpilot.core.proximity import NONE_STATE, HmmProximityEstimator, compute_posteriors, load_models
from nearpilot.shared.config import Settings
from nearpilot.shared.models import RssiFrame

from .conftest import BEACON, MODEL_ROOT, OTHER_BEACON, T0, make_frame, place_a_nodes
from .synthetic import Segment, generate_frames
from .test_filter import front_rssi

SETTINGS = Settings()
MODELS = load_models(MODEL_ROOT, {"place-a": "syn-a-1"})
MODEL = MODELS["place-a"]
SECOND = timedelta(seconds=1)  # 시간 칸이다. Settings.rssi_frame_sec의 기본값과 같다

NOISE_SIGMA = 4.0  # 합성 잡음의 표준편차, dB. 모델 파일의 sigma와 같다
DROP_PROB = 0.3
SEEDS = range(20)
SETTLED_SEC = 10  # 앞 10초는 관측 구간이 차기 전이라 깜빡임을 세지 않는다
MIN_BASELINE_FLICKERS = 50  # 기준선 깜빡임이 이보다 적으면 80% 감소를 잴 수 없다
MAX_FLICKER_RATIO = 0.2  # NFR-02: 깜빡임 80% 감소
MAX_SWITCH_DELAY_SEC = 2  # NFR-02: 대상 전환은 2초 이내


# ── 측정 도우미 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Trace:
    """초 t(0부터)마다의 대상. 기준선은 첫 프레임이 오기 전까지 None이다."""

    hmm: list[str]
    baseline: list[str | None]


def _log_normal(x: float, mean: float, sigma: float) -> float:
    z = (x - mean) / sigma
    return -0.5 * z * z - math.log(sigma) - 0.5 * math.log(2.0 * math.pi)


def log_likelihood(frame: RssiFrame, state: str) -> float:
    """기기 앞 상태 state일 때 frame의 앵커가 이 RSSI를 볼 로그 우도다."""
    anchor = MODEL.nodes[frame.node_id]
    return _log_normal(frame.rssi, front_rssi(MODEL, frame.node_id, state), anchor.sigma)


def none_log_likelihood(frames: Sequence[RssiFrame]) -> float:
    """한 칸의 프레임들이 NONE_STATE에서 나올 로그 우도다. 먼 위치와 none_points의 같은 비중 혼합이다."""
    components = [
        math.fsum(
            _log_normal(f.rssi, MODEL.nodes[f.node_id].none_rssi, MODEL.nodes[f.node_id].none_sigma) for f in frames
        )
    ]
    for x, y in MODEL.none_points:
        components.append(math.fsum(log_likelihood_at(f, x, y) for f in frames))
    peak = max(components)
    return peak + math.log(math.fsum(math.exp(c - peak) for c in components)) - math.log(len(components))


def log_likelihood_at(frame: RssiFrame, x: float, y: float) -> float:
    """(x, y)에 선 방문자를 frame의 앵커가 이 RSSI로 볼 로그 우도다."""
    anchor = MODEL.nodes[frame.node_id]
    distance = max(math.hypot(anchor.x - x, anchor.y - y), MODEL.min_distance_m)
    return _log_normal(frame.rssi, anchor.A - 10.0 * anchor.n * math.log10(distance), anchor.sigma)


def baseline_target(frames: Sequence[RssiFrame], previous: str | None) -> str | None:
    """기준선 대상. 한 칸의 프레임만으로 상태별 로그 우도를 더해 가장 큰 상태를 고른다.

    상태는 기기 앞 상태들(node_id 순)과 NONE_STATE이고, 프레임이 없는 앵커는 합에서 빠진다.
    프레임이 하나도 없으면 previous를 유지한다.
    """
    if not frames:
        return previous
    scores = {state: math.fsum(log_likelihood(frame, state) for frame in frames) for state in sorted(MODEL.nodes)}
    scores[NONE_STATE] = none_log_likelihood(frames)
    return max(scores, key=scores.__getitem__)


def run_path(path: Sequence[Segment], seed: int) -> Trace:
    """path의 합성 프레임을 초마다 observe한 뒤 그 초의 끝(at)으로 posterior를 불러 대상을 모은다.

    실제 서버와 같은 순서다. 경로 전체를 먼저 observe하면 버퍼가 오래된 프레임을 버려서 앞쪽 시각의 결과가 틀린다.
    시드(경로)마다 추정기를 새로 만든다. 시드끼리 session_id, seq, 시각이 같아서 이어 넣으면 중복으로 버려진다.
    """
    frames = generate_frames(MODEL, path, seed=seed, noise_sigma=NOISE_SIGMA, drop_prob=DROP_PROB)
    nodes = place_a_nodes()
    by_id = {node.node_id: node for node in nodes}
    estimator = HmmProximityEstimator(MODELS, SETTINGS)

    hmm: list[str] = []
    baseline: list[str | None] = []
    previous = None
    for t in range(sum(segment.seconds for segment in path)):
        at = T0 + (t + 1) * SECOND
        in_second = [frame for frame in frames if at - SECOND < frame.frame_ts <= at]
        for frame in in_second:
            estimator.observe(frame, node=by_id[frame.node_id])
        hmm.append(estimator.posterior([BEACON], nodes, at, place_nodes=nodes).best[0])
        previous = baseline_target(in_second, previous)
        baseline.append(previous)
    return Trace(hmm, baseline)


def flicker_count(targets: Sequence[str | None]) -> int:
    """t = SETTLED_SEC부터 끝까지, 직전 초와 대상이 다른 횟수다."""
    return sum(targets[t] != targets[t - 1] for t in range(SETTLED_SEC, len(targets)))


def switch_delay(targets: Sequence[str | None], new_target: str, moved_at: int) -> int | None:
    """방문자가 t = moved_at에 이동한 뒤 대상 전환 지연(초, at 기준)이다.

    대상이 new_target이 된 뒤 경로 끝까지 계속 new_target인 첫 초 t*를 찾아 `t* + 1 - moved_at`을 돌려준다.
    마지막 초의 대상이 new_target이 아니면 None이다.
    """
    first = None
    for t in range(len(targets) - 1, -1, -1):
        if targets[t] != new_target:
            break
        first = t
    return None if first is None else first + 1 - moved_at


# ── NFR-02 ───────────────────────────────────────────────────────────


def test_NFR02_flicker_reduced():
    path = [Segment("locker-3", 120)]
    counts = {}
    for seed in SEEDS:
        trace = run_path(path, seed)
        counts[seed] = (flicker_count(trace.baseline), flicker_count(trace.hmm))
    f_base = sum(base for base, _ in counts.values())
    f_hmm = sum(hmm for _, hmm in counts.values())
    per_seed = ", ".join(f"{seed}: 기준선 {base} HMM {hmm}" for seed, (base, hmm) in counts.items())

    assert f_base >= MIN_BASELINE_FLICKERS, (
        f"기준선 깜빡임이 너무 적어서 잴 수 없다 (F_base={f_base}, F_hmm={f_hmm}; 시드별 {per_seed})"
    )
    assert f_hmm <= MAX_FLICKER_RATIO * f_base, (
        f"깜빡임이 80% 줄지 않았다 (F_base={f_base}, F_hmm={f_hmm}, 기준 F_hmm <= {MAX_FLICKER_RATIO * f_base:g}; 시드별 {per_seed})"
    )


@pytest.mark.xfail(
    strict=True,
    reason="머무를 확률 0.9, 관측 구간 10초의 합성 모델은 전환이 늦은 시드가 있다. "
    "stay와 window_sec는 feat/core-proximity-calibration에서 검증셋으로 고른다 (FR-17, 지표 8).",
)
def test_NFR02_switch_within_two_seconds():
    path = [Segment("locker-2", 30), Segment("light-1", 30)]
    moved_at = path[0].seconds
    delays = {seed: switch_delay(run_path(path, seed).hmm, "light-1", moved_at) for seed in SEEDS}
    too_slow = {seed: delay for seed, delay in delays.items() if delay is None or delay > MAX_SWITCH_DELAY_SEC}

    assert not too_slow, f"전환이 {MAX_SWITCH_DELAY_SEC}초를 넘은 시드: {too_slow} (None은 끝까지 light-1이 아님; 시드별 지연 {delays})"


# ── 측정 도구 ─────────────────────────────────────────────────────────
# 위 두 테스트의 값은 생성기와 기준선이 계약대로일 때만 뜻이 있다. 도구가 바뀌면 값도 조용히 바뀌므로 따로 고정한다.


def test_NFR02_generator_follows_layout():
    path = [Segment("locker-2", 2), Segment("light-1", 1)]
    standing = ["locker-2", "locker-2", "light-1"]
    start = T0 + timedelta(hours=1)  # start와 beacon 인자를 따른다

    frames = generate_frames(MODEL, path, seed=3, noise_sigma=0.0, drop_prob=0.0, start=start, beacon=OTHER_BEACON)

    anchor_ids = sorted(MODEL.nodes)
    assert len(frames) == len(standing) * len(anchor_ids)
    for index, frame in enumerate(frames):
        t, order = divmod(index, len(anchor_ids))  # 초 t마다 앵커 순서대로다
        anchor_id = anchor_ids[order]
        frame_ts = start + timedelta(seconds=t, milliseconds=10 * order + 500)
        assert frame.node_id == anchor_id
        assert frame.beacon == OTHER_BEACON
        assert frame.rssi == front_rssi(MODEL, anchor_id, standing[t])  # 잡음이 0이면 그 초에 서 있는 기기 앞의 평균이다
        assert frame.frame_ts == frame_ts
        assert frame.received_at == frame_ts + timedelta(milliseconds=50)
        assert (frame.session_id, frame.seq) == (f"s-{anchor_id}", t + 1)
        assert start + timedelta(seconds=t) < frame.frame_ts <= start + timedelta(seconds=t + 1)  # 초 t의 칸 안이다


def test_NFR02_generator_is_seeded_and_drop_keeps_stream():
    path = [Segment("locker-3", 120)]

    def generate(seed: int, drop_prob: float) -> list[RssiFrame]:
        return generate_frames(MODEL, path, seed=seed, noise_sigma=NOISE_SIGMA, drop_prob=drop_prob)

    full = generate(3, 0.0)
    thinned = generate(3, DROP_PROB)

    assert thinned == generate(3, DROP_PROB)  # 같은 인자에는 같은 프레임이다
    assert [frame.rssi for frame in full] != [frame.rssi for frame in generate(4, 0.0)]
    assert generate(3, 1.0) == []
    # 빠진 프레임이 있어도 남은 프레임은 빠지지 않았을 때와 같다. 잡음과 탈락 난수를 프레임마다 항상 뽑기 때문이다.
    assert set(thinned) <= set(full)
    assert 0.2 < 1 - len(thinned) / len(full) < 0.4  # 탈락 비율이 drop_prob 근처다
    # 난수는 default_rng(seed) 하나이고 프레임마다 잡음(정규), 탈락(균등) 순서로 뽑는다.
    rng = numpy.random.default_rng(3)
    noise_first = rng.normal(0.0, NOISE_SIGMA)
    rng.random()
    noise_second = rng.normal(0.0, NOISE_SIGMA)
    assert full[0].rssi == front_rssi(MODEL, "light-1", "locker-3") + noise_first
    assert full[1].rssi == front_rssi(MODEL, "light-2", "locker-3") + noise_second


def test_NFR02_baseline_picks_maximum_likelihood_state():
    for device_id in MODEL.nodes:
        frames = generate_frames(MODEL, [Segment(device_id, 1)], seed=0, noise_sigma=0.0, drop_prob=0.0)
        assert baseline_target(frames, None) == device_id  # 잡음이 없으면 서 있는 기기다
    in_the_open = [make_frame(anchor_id, anchor.none_rssi, T0 + SECOND) for anchor_id, anchor in MODEL.nodes.items()]
    assert baseline_target(in_the_open, "locker-1") == NONE_STATE  # 어느 기기 앞도 아닌 값이다
    assert baseline_target([], "locker-1") == "locker-1"  # 프레임이 없으면 직전 대상이다
    assert baseline_target([], None) is None

    # 한 앵커의 프레임 하나만으로도 우도가 가장 큰 상태를 고른다. locker-3 앵커가 locker-3 앞의 평균을 본 경우다.
    only_locker_3 = [make_frame("locker-3", front_rssi(MODEL, "locker-3", "locker-3"), T0 + SECOND)]
    assert baseline_target(only_locker_3, None) == "locker-3"

    # 로그 우도는 정규분포의 로그 밀도다. 기기 앞 상태는 앵커의 sigma를 쓴다.
    # NONE_STATE는 먼 위치(none_rssi, none_sigma)와 none_points(경로손실 식, sigma)의 같은 비중 혼합이다.
    frame = make_frame("locker-2", -61.5, T0 + SECOND)
    anchor = MODEL.nodes["locker-2"]
    front_mean = front_rssi(MODEL, "locker-2", "locker-1")
    assert log_likelihood(frame, "locker-1") == pytest.approx(norm.logpdf(-61.5, front_mean, anchor.sigma))
    densities = [norm.pdf(-61.5, anchor.none_rssi, anchor.none_sigma)] + [
        norm.pdf(-61.5, -59.0 - 20.0 * math.log10(max(math.hypot(anchor.x - x, anchor.y - y), 0.3)), anchor.sigma)
        for x, y in MODEL.none_points
    ]
    assert none_log_likelihood([frame]) == pytest.approx(math.log(sum(densities) / len(densities)))


def test_NFR02_measures_follow_definitions():
    # 깜빡임은 t = 10부터 직전 초와 대상이 다른 횟수다. 앞 10초의 변화는 세지 않고 t = 10의 변화는 센다.
    assert flicker_count(["a", "b"] * 5) == 0
    assert flicker_count(["a"] * 10 + ["b"]) == 1
    assert flicker_count(["a", "b"] * 5 + ["b", "a", "a", "b", "b"]) == 2

    # 전환 지연은 끝까지 계속 new_target인 첫 초 t*로 `t* + 1 - moved_at`이다. 중간에 잠깐 new_target이었던 것은 세지 않는다.
    assert switch_delay(["locker-2"] * 33 + ["light-1", "locker-2", "light-1"] + ["light-1"] * 24, "light-1", 30) == 6  # t* = 35
    assert switch_delay(["locker-2"] * 30 + ["light-1"] * 30, "light-1", 30) == 1  # t* = 30
    assert switch_delay(["locker-2"] * 31 + ["light-1"] * 29, "light-1", 30) == 2  # t* = 31
    assert switch_delay(["locker-2"] * 59 + ["light-1"], "light-1", 30) == 30  # t* = 59
    assert switch_delay(["light-1"] * 59 + ["locker-2"], "light-1", 30) is None  # 끝까지 유지하지 못했다


def test_NFR02_run_path_matches_replay():
    # 초마다 observe한 뒤 그 초의 끝으로 물은 대상이, 전체 프레임으로 재현한 대상과 같다.
    path = [Segment("locker-2", 15), Segment("light-1", 15)]
    seconds = sum(segment.seconds for segment in path)
    frames = generate_frames(MODEL, path, seed=5, noise_sigma=NOISE_SIGMA, drop_prob=DROP_PROB)
    nodes = place_a_nodes()

    replayed = [
        compute_posteriors(
            MODEL, frames, BEACON, nodes, nodes, T0 + (t + 1) * SECOND, bin_sec=SETTINGS.rssi_frame_sec,
        ).best[0]
        for t in range(seconds)
    ]
    # 기준선은 프레임을 초 번호로 묶어 따로 구한다. 생성기의 프레임은 초 t 안쪽 0.5초 부근에 있다.
    by_second = {t: [] for t in range(seconds)}
    for frame in frames:
        by_second[int((frame.frame_ts - T0).total_seconds())].append(frame)
    baseline: list[str | None] = []
    previous = None
    for t in range(seconds):
        previous = baseline_target(by_second[t], previous)
        baseline.append(previous)

    trace = run_path(path, 5)
    assert trace.hmm == replayed
    assert trace.baseline == baseline
