"""배치 없는 공통 근접 모델의 적합과 검증 손실 (FR-16).

손으로 만든 작은 회차로 확인한다. 회차의 값은 기대값을 손으로 계산할 수 있게 짠 패턴이다 (`pattern_bins`):
역할마다 정해진 RSSI에 칸마다 공통 이동(−1, 0, +1)을 더하고, 한 기기만 6 dB 약하게 듣는다.
칸 안의 두 프레임은 평균에서 ±1.5 dB 벌어져 있어서, 역할 표본이 프레임이 아니라 칸 평균인지 갈린다.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from nearpilot.core.proximity import common_fit
from nearpilot.core.proximity.autozero import zeros_from_frames
from nearpilot.core.proximity.common_fit import (
    NONE_LABEL,
    CommonFitConfig,
    FitError,
    GridLoss,
    LabeledRun,
    PlaceRuns,
    fit_common_model,
    place_anchors,
    place_frames,
    place_zeros,
    validation_log_loss,
)
from nearpilot.core.proximity.common_model import (
    CommonModel,
    RoleParams,
    common_model_to_dict,
    load_common_model,
    write_common_model_file,
)
from nearpilot.core.proximity.posterior import compute_posteriors
from nearpilot.core.proximity.synthetic import SyntheticSpec, generate_places

from .conftest import BEACON, OTHER_BEACON, T0, make_frame, make_node

BIN_SEC = 1.0
JITTER = 1.5  # 칸 안의 두 프레임이 칸 평균에서 벌어진 정도, dB
ROLE_BASE = {"front": -50.0, "other": -70.0, "none": -80.0}
WEAK_DB = -6.0  # 패턴의 셋째 기기가 어느 역할에서나 더 약하게 듣는 정도
IDS_A = ("north-1", "north-2", "north-3", "north-4")  # 앞의 둘이 라벨 기기, 셋째가 약한 기기
IDS_B = ("south-1", "south-2", "south-3", "south-4")
MIN_BINS = 12
NOISY = SyntheticSpec(seed=11, rooms=2, devices=(3, 3), train_runs=3, validation_runs=2, run_bins=15, min_bins=20)


# ── 도우미 ───────────────────────────────────────────────────────────


class RunMaker:
    """손으로 만드는 회차. 기기마다 `seq`를 이어 붙여서 모든 프레임의 키가 다르다. 회차는 10초씩 띄워 이어진다."""

    def __init__(self, *, start=T0, first_seq: int = 1) -> None:
        self._seq: dict[str, int] = {}
        self._first_seq = first_seq
        self._start = start

    def run(
        self, run_id: str, label: str, bins: Sequence[Mapping[str, float]], *, beacon=BEACON, jitter: float = JITTER,
    ) -> LabeledRun:
        """칸 k의 {기기: 칸 평균 RSSI}마다 프레임 둘을 만든다. 칸 k는 `start + k초`부터 1초이고, 두 프레임은 평균에서 ±jitter다."""
        started_at = self._start
        frames = []
        for k, values in enumerate(bins):
            for node_id, value in values.items():
                for offset, delta in ((0.25, -jitter), (0.75, jitter)):
                    self._seq[node_id] = self._seq.get(node_id, self._first_seq - 1) + 1
                    ts = started_at + timedelta(seconds=k + offset)
                    frames.append(make_frame(node_id, value + delta, ts, beacon=beacon, seq=self._seq[node_id]))
        ended_at = started_at + timedelta(seconds=len(bins))
        self._start = ended_at + timedelta(seconds=10)
        return LabeledRun(run_id, label, beacon, started_at, ended_at, tuple(frames))


def pattern_bins(label: str, count: int, ids: Sequence[str], *, shift: float = 0.0) -> list[dict[str, float]]:
    """칸 count개의 {기기: 칸 평균 RSSI}. label이 ids의 기기이면 그 기기 앞이고, `NONE_LABEL`이면 어느 기기 앞도 아니다.

    칸 k의 모든 값에 공통 이동 `shift + (k % 3) − 1`이 더해진다. 기기 사이의 차이는 그대로라서 자동 영점은 안 바뀌고 역할 값만 흔들린다.
    ids[2]는 어느 역할에서나 6 dB 약하게 듣는다. 이 패턴의 자동 영점은 ids[0], ids[1], ids[3]이 2 dB, ids[2]가 −4 dB다
    (1등이 아닌 기기 셋의 평균이 −72일 때 −70은 +2, −76은 −4). none 칸은 1등이 셋이라서 표본이 없다.
    """
    bins = []
    for k in range(count):
        common = shift + (k % 3) - 1
        bins.append({
            node_id: ROLE_BASE["none" if label == NONE_LABEL else "front" if node_id == label else "other"]
            + (WEAK_DB if node_id == ids[2] else 0.0)
            + common
            for node_id in ids
        })
    return bins


def pattern_place(
    place_id: str, ids: Sequence[str], *, train_shift: float = 0.0, validation_shift: float = 3.0,
    train_bins: int = 9, validation_bins: int = 12,
) -> tuple[PlaceRuns, PlaceRuns]:
    """(학습, 검증) 묶음. 라벨마다(ids[0], ids[1], none) 회차 하나씩이다. 검증 회차는 학습보다 `validation_shift`만큼 세게 들린다."""
    maker = RunMaker()
    labels = (ids[0], ids[1], NONE_LABEL)
    train = PlaceRuns(place_id, [
        maker.run(f"{place_id}-{label}-train", label, pattern_bins(label, train_bins, ids, shift=train_shift))
        for label in labels
    ])
    validation = PlaceRuns(place_id, [
        maker.run(f"{place_id}-{label}-validation", label, pattern_bins(label, validation_bins, ids, shift=validation_shift))
        for label in labels
    ])
    return train, validation


def noisy_places() -> tuple[list[PlaceRuns], list[PlaceRuns]]:
    """잡음이 있는 작은 합성 장소 둘 (기기 셋, 회차 15칸). 후보 조합마다 검증 손실이 다르다 — 손으로 만든 패턴은 손실이 모두 0이다."""
    return generate_places(NOISY, bin_sec=BIN_SEC)


def two_places() -> tuple[list[PlaceRuns], list[PlaceRuns]]:
    """장소 둘. 둘째 장소는 학습 회차가 2 dB 세게 들린다."""
    train_a, validation_a = pattern_place("place-north", IDS_A)
    train_b, validation_b = pattern_place("place-south", IDS_B, train_shift=2.0)
    return [train_a, train_b], [validation_a, validation_b]


def make_config(**overrides: Any) -> CommonFitConfig:
    values: dict[str, Any] = {
        "model_version": "test-1",
        "taus": (1.0, 2.0),
        "stays": (0.9,),
        "window_secs": (3.0,),
        "auto_zero_min_bins": MIN_BINS,
    }
    values.update(overrides)
    return CommonFitConfig(**values)


def hand_model(**overrides: Any) -> CommonModel:
    """역할 평균이 패턴을 보정한 값(-52, -72, -82)이고 σ가 20 dB인 모델. 무뎌서 손실이 0.1 안팎이다 (또렷하면 손실이 1e-12라 비교가 무의미하다)."""
    values: dict[str, Any] = {
        "model_version": "hand",
        "window_sec": 3.0,
        "stay": 0.9,
        "calibration_method": "none",
        "calibration_tau": 1.0,
        "front": RoleParams(-52.0, 20.0),
        "other": RoleParams(-72.0, 20.0),
        "none": RoleParams(-82.0, 20.0),
        "auto_zero_min_bins": MIN_BINS,
        "auto_zero_bound_db": 0.0,
        "validation": {},
    }
    values.update(overrides)
    return CommonModel(**values)


def oracle_run_loss(
    model: CommonModel, run: LabeledRun, anchors: Sequence[str], zeros: Mapping[str, float], *, bin_sec: float = BIN_SEC,
) -> tuple[float, int]:
    """`compute_posteriors`를 직접 불러 센 회차 하나의 평균 손실과 평가 시각 수. 평가 시각은 `started_at + window`부터 `bin_sec`씩 `ended_at`까지다."""
    nodes = [make_node(node_id, "place", capability="light") for node_id in anchors]
    losses = []
    at = run.started_at + timedelta(seconds=model.window_sec)
    while at <= run.ended_at:
        posteriors = compute_posteriors(
            model, run.frames, run.beacon, nodes, nodes, at,
            bin_sec=bin_sec, participants=anchors, zeros=zeros, calib_version="oracle",
        ).posteriors
        p = 1.0 - sum(posteriors.values()) if run.label == NONE_LABEL else posteriors[run.label]
        losses.append(-math.log(p))
        at += timedelta(seconds=bin_sec)
    return sum(losses) / len(losses), len(losses)


# ── 평가 도구가 쓰는 이름 ───────────────────────────────────────────


def test_FR16_evaluation_runs_use_common_fit_names():
    """평가 도구의 회차 읽기는 `LabeledRun`과 `NONE_LABEL`을 `common_fit`에서 가져온다."""
    from tools.evaluation import runs as evaluation_runs

    assert evaluation_runs.LabeledRun is common_fit.LabeledRun
    assert evaluation_runs.NONE_LABEL == "none"


def test_FR16_fit_inputs_freeze_lists():
    """list로 받은 회차, 프레임, 후보는 tuple로 바뀐다."""
    run = LabeledRun("r", "a", BEACON, T0, T0 + timedelta(seconds=5), [make_frame("a", -60.0, T0)])
    assert isinstance(run.frames, tuple)
    assert isinstance(PlaceRuns("p", [run]).runs, tuple)
    config = make_config(taus=[1.0, 2.0], stays=[0.9], window_secs=[3.0])
    assert (config.taus, config.stays, config.window_secs) == ((1.0, 2.0), (0.9,), (3.0,))
    assert config.validation == {}


# ── 프레임, 앵커, 자동 영점 ─────────────────────────────────────────


def test_FR16_place_frames_selection_rules():
    """비콘, 앵커, 유한한 RSSI, 반열린 구간, 같은 키는 처음 하나만 남긴다."""
    start = T0
    end = T0 + timedelta(seconds=10)

    def at(seconds: float):
        return start + timedelta(seconds=seconds)

    frames = [
        make_frame("a", -60.0, at(5), seq=2),
        make_frame("a", -61.0, at(3), seq=1),
        make_frame("a", -99.0, at(4), seq=1),  # 키가 위와 같다. frame_ts가 늦어서 버린다
        make_frame("b", -62.0, at(3), seq=1),
        make_frame("a", -63.0, at(0), seq=3),  # 시작 시각은 구간 안이다
        make_frame("a", -64.0, at(10), seq=4),  # 끝 시각은 구간 밖이다
        make_frame("a", -65.0, at(-1), seq=5),
        make_frame("a", -66.0, at(6), seq=6, beacon=OTHER_BEACON),
        make_frame("x", -67.0, at(6), seq=1),  # 앵커가 아니다
        make_frame("a", math.nan, at(7), seq=7),
        make_frame("a", math.inf, at(7), seq=8),
        make_frame("a", -math.inf, at(7), seq=9),
        # 중복 키는 (기기, 연결, 순번, 비콘) 전부가 같을 때만이다: 연결이나 기기가 다르면 순번이 같아도 둘 다 쓴다.
        make_frame("b", -72.0, at(7), seq=12, session_id="shared"),
        make_frame("a", -73.0, at(7.5), seq=12, session_id="shared"),
        make_frame("a", -70.0, at(8), seq=11, session_id="old"),
        make_frame("a", -71.0, at(8.5), seq=11, session_id="new"),
        # 키와 frame_ts가 모두 같으면 received_at이 이른 쪽이 처음이다. 입력 순서는 결과를 바꾸지 않는다.
        make_frame("b", -80.0, at(9), seq=13, received_at=at(9.5)),
        make_frame("b", -81.0, at(9), seq=13, received_at=at(9.2)),
    ]
    run = LabeledRun("r", "a", BEACON, start, end, tuple(frames))

    selected = place_frames(run, {"a", "b"})

    assert [(f.node_id, f.seq) for f in selected] == [
        ("a", 3), ("a", 1), ("b", 1), ("a", 2), ("b", 12), ("a", 12), ("a", 11), ("a", 11), ("b", 13),
    ]
    assert [f.rssi for f in selected if (f.node_id, f.seq) == ("b", 13)] == [-81.0]
    assert place_frames(replace(run, frames=tuple(reversed(frames))), ["b", "a"]) == selected  # 입력 순서와 앵커 모음의 모양은 결과를 바꾸지 않는다


def test_FR16_place_anchors_union_of_frames_and_labels():
    """장소의 앵커는 회차 프레임에 나온 기기와 `none`이 아닌 라벨의 합집합이다. 묶음이 여럿이어도 한 집합이다."""
    run_a = LabeledRun("r1", "a", BEACON, T0, T0 + timedelta(seconds=5), (make_frame("a", -60.0, T0), make_frame("b", -70.0, T0)))
    run_none = LabeledRun("r2", NONE_LABEL, BEACON, T0, T0 + timedelta(seconds=5), (make_frame("c", -80.0, T0),))
    # 라벨 기기가 보고하지 않았거나, 다른 비콘의 프레임만 있어도 앵커다. 프레임은 거르지 않고 모두 본다.
    run_silent = LabeledRun("r3", "d", BEACON, T0, T0 + timedelta(seconds=5), (make_frame("e", -75.0, T0, beacon=OTHER_BEACON),))

    assert place_anchors(PlaceRuns("p", [run_a, run_none]), PlaceRuns("p", [run_silent])) == frozenset("abcde")
    assert place_anchors(PlaceRuns("p", [run_none])) == frozenset({"c"})  # NONE_LABEL은 앵커가 아니다
    assert place_anchors(PlaceRuns("p", [])) == frozenset()
    assert place_anchors() == frozenset()


def test_FR16_place_zeros_pools_all_runs_and_ignores_labels():
    """장소의 자동 영점은 묶음들의 모든 회차 프레임에서 찾고 라벨을 쓰지 않는다. `zeros_from_frames`와 같다."""
    train, validation = pattern_place("place-north", IDS_A)
    expected = {IDS_A[0]: 2.0, IDS_A[1]: 2.0, IDS_A[2]: -4.0, IDS_A[3]: 2.0}

    both = place_zeros([train, validation], bin_sec=BIN_SEC, min_bins=MIN_BINS)

    assert both == expected
    # 학습만으로는 앞 둘의 표본(다른 하나가 앞인 칸 9개)이 모자라 못 찾는다. 검증 회차의 프레임도 쓴다는 뜻이다.
    assert place_zeros([train], bin_sec=BIN_SEC, min_bins=MIN_BINS) == {IDS_A[2]: -4.0, IDS_A[3]: 2.0}
    assert place_zeros([train, validation], bin_sec=BIN_SEC, min_bins=10_000) == {}
    relabeled = [PlaceRuns("place-north", [replace(run, label=NONE_LABEL) for run in group.runs]) for group in (train, validation)]
    assert place_zeros(relabeled, bin_sec=BIN_SEC, min_bins=MIN_BINS) == expected

    frames = [frame for group in (train, validation) for run in group.runs for frame in run.frames]
    assert both == zeros_from_frames(frames, IDS_A, bin_sec=BIN_SEC, min_bins=MIN_BINS)


def test_FR16_place_zeros_anchors_cover_every_group():
    """앵커는 묶음들의 합집합이다: 검증 묶음에만 나온 기기도 영점을 갖는다."""
    extra_ids = (*IDS_A, "north-5")
    maker = RunMaker()
    labels = (IDS_A[0], IDS_A[1], NONE_LABEL)
    train = PlaceRuns("place-north", [maker.run(f"t-{label}", label, pattern_bins(label, 9, IDS_A)) for label in labels])
    validation = PlaceRuns("place-north", [
        maker.run(f"v-{label}", label, pattern_bins(label, 12, extra_ids, shift=3.0)) for label in labels  # 다섯째 기기는 여기서만 보고한다
    ])

    zeros = place_zeros([train, validation], bin_sec=BIN_SEC, min_bins=MIN_BINS)

    frames = [frame for group in (train, validation) for run in group.runs for frame in run.frames]
    assert "north-5" not in place_zeros([train], bin_sec=BIN_SEC, min_bins=MIN_BINS)
    assert "north-5" in zeros
    assert zeros == zeros_from_frames(frames, extra_ids, bin_sec=BIN_SEC, min_bins=MIN_BINS)


def test_FR16_place_zeros_ignores_frames_the_fit_does_not_use():
    """회차 구간 밖의 프레임과 다른 비콘의 프레임은 영점의 표본이 되지 않는다 (`place_frames`가 고른 프레임만 쓴다)."""
    train, _ = pattern_place("place-north", IDS_A)
    target = train.runs[1]  # 라벨 IDS_A[1]: IDS_A[0]이 1등이 아닌 칸이 9개뿐이라 영점을 못 찾는다
    decoys = []
    seq = 1000
    for beacon, origin in ((BEACON, target.ended_at), (OTHER_BEACON, target.started_at)):  # 구간 밖(회차 뒤), 다른 비콘(구간 안)
        for k in range(6):
            for node_id, value in zip(IDS_A, (-70.0, -50.0, -76.0, -70.0)):  # IDS_A[1]이 1등인 칸
                seq += 1
                decoys.append(make_frame(node_id, value, origin + timedelta(seconds=k + 0.5), beacon=beacon, seq=seq))
    noisy = PlaceRuns("place-north", [replace(run, frames=(*run.frames, *decoys)) if run is target else run for run in train.runs])

    expected = {IDS_A[2]: -4.0, IDS_A[3]: 2.0}
    assert place_zeros([train], bin_sec=BIN_SEC, min_bins=MIN_BINS) == expected
    assert place_zeros([noisy], bin_sec=BIN_SEC, min_bins=MIN_BINS) == expected


def test_FR16_place_zeros_follows_bin_sec():
    """칸 길이로 칸을 묶는다: 2초 칸은 1초 칸보다 칸 수가 적어서 같은 기준 표본 수를 못 채우는 기기가 생긴다."""
    train, validation = pattern_place("place-north", IDS_A)
    frames = [frame for group in (train, validation) for run in group.runs for frame in run.frames]

    one = place_zeros([train, validation], bin_sec=1.0, min_bins=18)
    two = place_zeros([train, validation], bin_sec=2.0, min_bins=18)

    assert two == zeros_from_frames(frames, IDS_A, bin_sec=2.0, min_bins=18)
    assert set(one) == set(IDS_A)  # 1초 칸에서는 앞 둘의 표본이 21개다
    assert set(two) == {IDS_A[2], IDS_A[3]}  # 2초 칸에서는 앞 둘의 표본이 절반쯤이라 18개에 못 미친다


# ── 검증 손실 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("bin_sec", [1.0, 0.5])
def test_FR16_validation_log_loss_matches_posterior(bin_sec):
    """손실은 `compute_posteriors`로 센 정답 확률의 −log를 시각마다 구해 회차마다 평균한 뒤 회차끼리 평균한 값이다. none은 1 − 후보 합이다."""
    train, validation = pattern_place("place-north", IDS_A)
    zeros = place_zeros([train, validation], bin_sec=BIN_SEC, min_bins=MIN_BINS)
    model = hand_model()

    actual = validation_log_loss(model, [validation], {"place-north": zeros}, bin_sec=bin_sec)

    run_losses = [oracle_run_loss(model, run, IDS_A, zeros, bin_sec=bin_sec)[0] for run in validation.runs]
    assert {run.label for run in validation.runs} == {IDS_A[0], IDS_A[1], NONE_LABEL}  # 기기 앞 회차와 none 회차를 모두 센다
    assert actual == pytest.approx(sum(run_losses) / len(run_losses), rel=1e-12)
    assert actual > 0.0


def test_FR16_validation_log_loss_averages_runs_not_times():
    """시각 손실은 회차마다 평균하고, 그 평균을 회차끼리(모든 장소에 걸쳐) 평균한다. 회차가 길면 시각이 많아도 한 회차다."""
    train_a, validation_a = pattern_place("place-north", IDS_A, validation_bins=9)
    train_b, validation_b = pattern_place("place-south", IDS_B, validation_shift=8.0, validation_bins=18)  # 손실이 더 크고 시각이 더 많다
    zeros = {
        "place-north": place_zeros([train_a, validation_a], bin_sec=BIN_SEC, min_bins=MIN_BINS),
        "place-south": place_zeros([train_b, validation_b], bin_sec=BIN_SEC, min_bins=MIN_BINS),
    }
    model = hand_model()

    actual = validation_log_loss(model, [validation_a, validation_b], zeros, bin_sec=BIN_SEC)

    per_run = []
    pooled = []
    for group, ids in ((validation_a, IDS_A), (validation_b, IDS_B)):
        for run in group.runs:
            loss, count = oracle_run_loss(model, run, ids, zeros[group.place_id])
            per_run.append(loss)
            pooled.extend([loss] * count)
    assert abs(sum(per_run) / len(per_run) - sum(pooled) / len(pooled)) > 1e-3  # 두 평균이 다른 데이터라서 이 시험이 갈린다
    assert actual == pytest.approx(sum(per_run) / len(per_run), rel=1e-12)


def test_FR16_validation_log_loss_uses_each_places_zeros():
    """자동 영점은 장소마다 `zeros[place_id]`를 쓴다. 영점이 없는 장소는 모든 기기가 영점 없음이다."""
    train, validation = pattern_place("place-north", IDS_A, validation_shift=0.0)
    zeros = place_zeros([train, validation], bin_sec=BIN_SEC, min_bins=MIN_BINS)
    model = hand_model()

    def mean_loss(zero_map: Mapping[str, float]) -> float:
        losses = [oracle_run_loss(model, run, IDS_A, zero_map)[0] for run in validation.runs]
        return sum(losses) / len(losses)

    with_zeros = validation_log_loss(model, [validation], {"place-north": zeros}, bin_sec=BIN_SEC)
    without = validation_log_loss(model, [validation], {}, bin_sec=BIN_SEC)
    other_place = validation_log_loss(model, [validation], {"place-south": zeros}, bin_sec=BIN_SEC)

    assert with_zeros == pytest.approx(mean_loss(zeros), rel=1e-12)
    assert without == pytest.approx(mean_loss({}), rel=1e-12)
    assert other_place == without  # 다른 장소의 영점은 쓰지 않는다
    assert abs(with_zeros - without) > 1e-3  # 영점이 손실을 바꾼다. 위 두 줄이 영점을 정말 쓴다는 증거다


def test_FR16_validation_log_loss_silent_label_device_is_a_state():
    """라벨 기기가 보고하지 않아도 앵커다 (보고를 멈춘 기기도 상태로 남는다). 손실은 그 기기를 상태에 넣은 계산이다."""
    _, validation = pattern_place("place-north", IDS_A)
    silent = replace(validation.runs[0], run_id="silent", label="north-silent")  # 프레임에 나온 적 없는 기기가 라벨이다
    model = hand_model()

    actual = validation_log_loss(model, [PlaceRuns("place-north", [silent])], {}, bin_sec=BIN_SEC)

    expected, _ = oracle_run_loss(model, silent, (*IDS_A, "north-silent"), {})
    assert actual == pytest.approx(expected, rel=1e-12)


def test_FR16_validation_log_loss_infinite_when_correct_probability_is_zero():
    """정답 확률이 0 이하인 시각이 있으면 손실은 무한대다 (none 정답인데 후보 합이 1이 되는 경우)."""
    maker = RunMaker()
    sure = [{"d1": -50.0, "d2": -90.0, "d3": -90.0} for _ in range(8)]  # d1 앞이 확실하다: 후보 합이 1.0으로 올림된다
    run = maker.run("none-but-sure", NONE_LABEL, sure, jitter=0.0)
    model = hand_model(front=RoleParams(-50.0, 0.01), other=RoleParams(-90.0, 0.01), none=RoleParams(-70.0, 0.01))

    assert validation_log_loss(model, [PlaceRuns("p", [run])], {}, bin_sec=BIN_SEC) == math.inf


def test_FR16_validation_log_loss_rejects_missing_runs():
    """회차가 없거나, 평가 시각이 없는 회차가 있거나, 칸 길이가 잘못되면 `FitError`다."""
    train, validation = pattern_place("place-north", IDS_A)
    model = hand_model()
    short = replace(validation.runs[0], ended_at=validation.runs[0].started_at + timedelta(seconds=2))  # window_sec 3.0보다 짧다

    with pytest.raises(FitError):
        validation_log_loss(model, [], {}, bin_sec=BIN_SEC)
    with pytest.raises(FitError):
        validation_log_loss(model, [PlaceRuns("place-north", [])], {}, bin_sec=BIN_SEC)
    with pytest.raises(FitError, match="평가 시각"):
        validation_log_loss(model, [PlaceRuns("place-north", [validation.runs[1], short])], {}, bin_sec=BIN_SEC)
    for bad in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(FitError):
            validation_log_loss(model, [validation], {}, bin_sec=bad)


# ── 공통 적합 ───────────────────────────────────────────────────────


def test_FR16_common_fit_no_layout():
    """장소 둘의 학습·검증 회차로 모델을 만든다. 모델에는 장소 ID와 기기 ID가 없고, 후보 조합마다 손실이 하나 있다."""
    train, validation = two_places()
    config = make_config(taus=(0.5, 1.0, 2.0), stays=(0.8, 0.9), window_secs=(3.0, 4.0))

    result = fit_common_model(train, validation, config, bin_sec=BIN_SEC)

    text = json.dumps(common_model_to_dict(result.model), ensure_ascii=False)
    assert [name for name in ("place-north", "place-south", *IDS_A, *IDS_B) if name in text] == []
    assert "north" not in text and "south" not in text
    assert result.model.front.mean > result.model.other.mean
    assert len(result.losses) == 3 * 2 * 2
    assert result.model.model_version == "test-1"
    assert result.model.auto_zero_min_bins == MIN_BINS


def test_FR16_common_fit_role_samples_use_absolute_bins():
    """역할 표본의 칸은 회차 시작이 아니라 절대 시각 기준이다 (자동 영점, 사후확률과 같은 칸). 칸 평균을 영점으로 보정한다."""
    start = T0 + timedelta(seconds=0.5)  # 칸 경계에서 반 칸 어긋난 시작
    readings = [
        ("a", 0.6, -60.0), ("a", 0.9, -62.0), ("a", 1.4, -70.0), ("a", 1.6, -72.0),
        ("b", 0.6, -80.0), ("b", 0.9, -82.0), ("b", 1.4, -90.0), ("b", 1.6, -92.0),
    ]
    frames = tuple(
        make_frame(node_id, value, T0 + timedelta(seconds=offset), seq=seq)
        for seq, (node_id, offset, value) in enumerate(readings, start=1)
    )
    run = LabeledRun("r", "a", BEACON, start, T0 + timedelta(seconds=2.5), frames)
    front_group = PlaceRuns("p", [run])
    none_group = PlaceRuns("p", [replace(run, label=NONE_LABEL)])

    def sorted_samples(group: PlaceRuns, zero: Mapping[str, float], bin_sec: float) -> dict[str, list[float]]:
        samples = common_fit._role_samples(group, {"a", "b"}, zero, bin_sec)
        return {role: sorted(values) for role, values in samples.items()}

    # 절대 1초 칸: a는 [-61, -71], b는 [-81, -91]. 시작 기준 칸이면 a가 [-64, -72]로 갈린다.
    assert sorted_samples(front_group, {}, 1.0) == {"front": [-71.0, -61.0], "other": [-91.0, -81.0], "none": []}
    # 영점은 앵커마다 빼고 없는 앵커는 0 dB다. 라벨이 none이면 모든 값이 none이다.
    assert sorted_samples(none_group, {"a": 1.0}, 1.0) == {"front": [], "other": [], "none": [-91.0, -81.0, -72.0, -62.0]}
    # 2초 칸 하나에 네 프레임이 모두 든다 (T0는 2초의 배수다).
    assert sorted_samples(front_group, {}, 2.0) == {"front": [-66.0], "other": [-86.0], "none": []}


def test_FR16_common_fit_role_params_do_not_depend_on_sample_order():
    """역할 값은 `math.fsum`으로 낸 정확한 합이라서 표본을 어떤 순서로 넣어도 비트까지 같다.

    아래 표본은 1e-16이 반올림의 동점을 깨는 값이다. 순서대로 더하면 평균이 1428571442857142.8로 어긋난다.
    (파이썬 3.12부터 `sum`은 오차를 보정하지만 이런 값에서는 여전히 어긋난다. 표준편차의 합은 이런 값을 찾지 못했다.)
    """
    samples = [1e-16, -1e16, 0.5, 1e16, 0.5, 1e8, 1e16]
    mean = 1428571442857143.2
    sigma = math.sqrt(math.fsum((value - mean) ** 2 for value in samples) / len(samples))
    assert sum(samples) / len(samples) != mean  # 이 표본이 순서대로 더한 합과 fsum을 가른다

    for ordered in (samples, samples[::-1], sorted(samples), sorted(samples, key=abs)):
        params = common_fit._role_params(ordered, "front")
        assert (params.mean, params.sigma) == (mean, sigma)


def test_FR16_common_fit_role_values_are_corrected_bin_means():
    """역할 값은 학습 회차의 칸 평균을 자동 영점으로 보정한 표본의 평균과 모표준편차다. 장소를 가리지 않고 모으고 검증 회차는 쓰지 않는다."""
    train, validation = two_places()
    result = fit_common_model(train, validation, make_config(), bin_sec=BIN_SEC)

    # 장소마다 영점이 앞 둘 2, 셋째 -4, 넷째 2이다 (검증 회차의 프레임도 써야 앞 둘이 12칸을 넘긴다).
    assert result.zeros == {
        "place-north": {IDS_A[0]: 2.0, IDS_A[1]: 2.0, IDS_A[2]: -4.0, IDS_A[3]: 2.0},
        "place-south": {IDS_B[0]: 2.0, IDS_B[1]: 2.0, IDS_B[2]: -4.0, IDS_B[3]: 2.0},
    }
    # 보정하면 앞 -52, 옆 -72, 없음 -82에 장소의 이동(학습 회차: 북쪽 0, 남쪽 2)과 칸의 공통 이동 (k % 3) − 1이 더해진다.
    samples: dict[str, list[float]] = {"front": [], "other": [], "none": []}
    for shift in (0.0, 2.0):
        for k in range(9):
            common = shift + (k % 3) - 1
            samples["front"] += [ROLE_BASE["front"] - 2.0 + common] * 2  # 라벨 기기마다 칸 하나에 앞 표본 하나, 회차 둘
            samples["other"] += [ROLE_BASE["other"] - 2.0 + common] * 6  # 회차 둘의 칸마다 옆 표본 셋
            samples["none"] += [ROLE_BASE["none"] - 2.0 + common] * 4  # none 회차의 칸마다 네 기기
    for role in ("front", "other", "none"):
        actual = getattr(result.model, role)
        assert actual.mean == pytest.approx(statistics.fmean(samples[role]), rel=1e-12), role
        assert actual.sigma == pytest.approx(statistics.pstdev(samples[role]), rel=1e-12), role
    assert result.model.front.sigma < statistics.pstdev([v + d for v in samples["front"] for d in (-JITTER, JITTER)])  # 프레임 값이 아니다


def test_FR16_common_fit_without_zeros_leaves_roles_uncorrected():
    """자동 영점을 못 찾으면(표본 부족) 보정 없이 역할 값을 낸다. 영점이 있으면 값이 달라진다."""
    train, validation = two_places()

    uncorrected = fit_common_model(train, validation, make_config(auto_zero_min_bins=10_000), bin_sec=BIN_SEC)
    corrected = fit_common_model(train, validation, make_config(), bin_sec=BIN_SEC)

    assert uncorrected.zeros == {"place-north": {}, "place-south": {}}
    assert uncorrected.model.front.mean == pytest.approx(-50.0 + 1.0, rel=1e-12)  # 앞 -50에 장소 이동 평균 1
    assert corrected.model.front.mean == pytest.approx(-52.0 + 1.0, rel=1e-12)
    assert uncorrected.model.other.sigma > corrected.model.other.sigma  # 약한 기기를 보정하면 옆 값의 흩어짐이 줄어든다


def test_FR16_fit_sets_auto_zero_bound_from_found_zeros(monkeypatch):
    """Δ(`auto_zero_bound_db`)는 학습 장소마다 찾은 자동 영점의 절댓값 최댓값이다. 부호는 보지 않고, 검증에만 있는 장소는 쓰지 않는다."""
    train, validation = two_places()
    _, validation_only = pattern_place("place-east", ("east-1", "east-2", "east-3", "east-4"))
    every_validation = [*validation, validation_only]

    # 찾은 영점은 장소마다 앞 둘 2, 셋째 −4, 넷째 2다. 절댓값 최댓값은 음수 쪽이다
    result = fit_common_model(train, every_validation, make_config(), bin_sec=BIN_SEC)
    assert result.model.auto_zero_bound_db == 4.0
    assert result.model.auto_zero_min_bins == MIN_BINS
    # 영점을 일부만 찾아도 찾은 값으로 정한다
    assert fit_common_model(train, validation, make_config(auto_zero_min_bins=22), bin_sec=BIN_SEC).model.auto_zero_bound_db == 4.0
    # Δ는 검증 손실에 쓰지 않는다: Δ가 0인 같은 모델의 손실과 같다
    assert validation_log_loss(
        replace(result.model, auto_zero_bound_db=0.0), every_validation, result.zeros, bin_sec=BIN_SEC,
    ) == result.model.validation["validation_loss"]

    # 손으로 정한 영점. 학습 장소(north, south)에서 찾은 값의 절댓값 최댓값이 Δ이고, 검증에만 있는 place-east의 9.0은 쓰지 않는다
    found = {
        "place-north": {IDS_A[0]: 1.5, IDS_A[1]: -3.25},
        "place-south": {IDS_B[0]: 2.5},
        "place-east": {"east-1": 9.0},
    }
    monkeypatch.setattr(common_fit, "place_zeros", lambda groups, *, bin_sec, min_bins: dict(found[groups[0].place_id]))
    result = fit_common_model(train, every_validation, make_config(), bin_sec=BIN_SEC)
    assert result.zeros == found
    assert result.model.auto_zero_bound_db == 3.25
    # 부호만 바뀌어도 같다
    found["place-north"][IDS_A[1]] = 3.25
    assert fit_common_model(train, every_validation, make_config(), bin_sec=BIN_SEC).model.auto_zero_bound_db == 3.25
    found["place-south"][IDS_B[0]] = -3.5
    assert fit_common_model(train, every_validation, make_config(), bin_sec=BIN_SEC).model.auto_zero_bound_db == 3.5


def test_FR16_fit_leaves_auto_zero_bound_unknown_without_zeros(monkeypatch, tmp_path):
    """학습 장소에서 자동 영점을 찾은 기기가 하나도 없으면 Δ를 정할 수 없다. 적합은 실패하지 않고 Δ는 `None`(모름)이며 모델 파일에는 `null`로 쓴다."""
    train, validation = two_places()

    # 표본이 모자라 영점을 하나도 못 찾았다 (칸 43개 이상이 필요한데 가장 많은 기기가 42개다)
    result = fit_common_model(train, validation, make_config(auto_zero_min_bins=43), bin_sec=BIN_SEC)
    assert result.zeros == {"place-north": {}, "place-south": {}}
    assert result.model.auto_zero_bound_db is None
    # Δ를 몰라도 검증 손실은 그대로다 (Δ는 손실에 쓰지 않는다)
    assert validation_log_loss(
        replace(result.model, auto_zero_bound_db=0.0), validation, result.zeros, bin_sec=BIN_SEC,
    ) == result.model.validation["validation_loss"]
    # 모델 파일에는 null로 쓰고, 읽으면 같은 모델이다
    path = write_common_model_file(tmp_path, result.model)
    assert json.loads(path.read_text(encoding="utf-8"))["auto_zero"]["bound_db"] is None
    assert load_common_model(path) == result.model
    # 하나라도 찾으면 정한다
    assert fit_common_model(train, validation, make_config(auto_zero_min_bins=42), bin_sec=BIN_SEC).model.auto_zero_bound_db == 4.0

    # 검증에만 있는 장소에서 찾은 영점은 학습 장소의 것이 아니라서 Δ는 여전히 모른다
    _, validation_only = pattern_place("place-east", ("east-1", "east-2", "east-3", "east-4"))
    monkeypatch.setattr(
        common_fit, "place_zeros",
        lambda groups, *, bin_sec, min_bins: {"east-1": 2.0} if groups[0].place_id == "place-east" else {},
    )
    result = fit_common_model(train, [*validation, validation_only], make_config(), bin_sec=BIN_SEC)
    assert result.zeros == {"place-north": {}, "place-south": {}, "place-east": {"east-1": 2.0}}
    assert result.model.auto_zero_bound_db is None


def test_FR16_common_fit_zeros_cover_every_place():
    """`zeros`는 학습이나 검증에 나온 모든 장소의 자동 영점이다. 검증에만 있는 장소도 든다."""
    train, validation = two_places()
    _, validation_only = pattern_place("place-east", ("east-1", "east-2", "east-3", "east-4"))

    result = fit_common_model(train, [*validation, validation_only], make_config(auto_zero_min_bins=3), bin_sec=BIN_SEC)

    assert sorted(result.zeros) == ["place-east", "place-north", "place-south"]
    assert result.zeros["place-east"] == place_zeros([validation_only], bin_sec=BIN_SEC, min_bins=3)
    assert result.zeros["place-north"] == place_zeros([train[0], validation[0]], bin_sec=BIN_SEC, min_bins=3)


def test_FR16_common_fit_follows_bin_sec():
    """칸 길이는 자동 영점, 역할 값, 검증 손실에 모두 쓰인다: 0.5초 칸으로 적합한 결과는 그 칸으로 다시 잰 값과 같고, 1초 칸의 결과와 다르다."""
    half = 0.5
    train, validation = generate_places(NOISY, bin_sec=half)
    config = make_config(taus=(1.0, 2.0), stays=(0.8, 0.95), window_secs=(2.0, 3.0), auto_zero_min_bins=NOISY.min_bins)

    result = fit_common_model(train, validation, config, bin_sec=half)
    unit = fit_common_model(train, validation, config, bin_sec=1.0)

    samples: dict[str, list[float]] = {"front": [], "other": [], "none": []}
    for train_group, validation_group in zip(train, validation):
        zero = result.zeros[train_group.place_id]
        assert zero == place_zeros([train_group, validation_group], bin_sec=half, min_bins=NOISY.min_bins)
        anchors = place_anchors(train_group, validation_group)
        for role, values in common_fit._role_samples(train_group, anchors, zero, half).items():
            samples[role].extend(values)
    for role in ("front", "other", "none"):
        assert getattr(result.model, role).mean == pytest.approx(statistics.fmean(samples[role]), rel=1e-12), role
        assert getattr(result.model, role).sigma == pytest.approx(statistics.pstdev(samples[role]), rel=1e-12), role
    best = min(result.losses, key=lambda grid: grid.loss)
    again = validation_log_loss(replace(result.model, validation={}), validation, result.zeros, bin_sec=half)
    assert again == pytest.approx(best.loss, rel=1e-12)
    # 같은 데이터를 1초 칸으로 적합하면 영점, 역할 값, 손실이 모두 달라서 위 비교가 칸 길이를 가린다.
    assert unit.zeros != result.zeros
    assert unit.model.front != result.model.front and unit.model.other != result.model.other
    assert [grid.loss for grid in unit.losses] != [grid.loss for grid in result.losses]


def test_FR16_common_fit_accepts_label_devices_heard_anywhere_in_the_place():
    """라벨 기기는 그 장소의 어느 회차에서든 한 번 보고했으면 된다: 검증 묶음의 마지막 회차에서만 나온 기기도 라벨이 될 수 있다."""
    train, validation = two_places()
    extra_ids = (*IDS_A, "north-5")
    maker = RunMaker(start=validation[0].runs[-1].ended_at + timedelta(seconds=10), first_seq=1000)  # 다른 회차와 프레임 키가 겹치지 않게 한다
    late = maker.run("late-north-5", "north-5", pattern_bins("north-5", 12, extra_ids, shift=3.0))
    with_late = [PlaceRuns("place-north", [*validation[0].runs, late]), validation[1]]

    result = fit_common_model(train, with_late, make_config(), bin_sec=BIN_SEC)

    assert len(result.losses) == 2
    assert "north-5" in place_anchors(with_late[0])


def test_FR16_common_fit_chooses_minimum_loss_and_earlier_on_ties(monkeypatch):
    """손실이 가장 작은 조합을 고르고, 같으면 taus → stays → window_secs 순서에서 앞선 조합이다."""
    train, validation = two_places()
    taus, stays, windows = (0.5, 1.0, 2.0), (0.8, 0.9), (2.0, 3.0)
    combos = [(tau, stay, window) for tau in taus for stay in stays for window in windows]
    table = {combo: 5.0 for combo in combos}
    table[combos[9]] = table[combos[5]] = 1.0  # 손실이 같은 둘. 앞선 것이 이긴다
    table[combos[0]] = 2.0
    calls: list[tuple[float, float, float]] = []

    def fake_loss(model, places, zeros, *, bin_sec):
        combo = (model.calibration_tau, model.stay, model.window_sec)
        calls.append(combo)
        return table[combo]

    monkeypatch.setattr(common_fit, "validation_log_loss", fake_loss)
    result = fit_common_model(train, validation, make_config(taus=taus, stays=stays, window_secs=windows), bin_sec=BIN_SEC)

    assert calls == combos  # 중첩 순서대로 한 번씩
    assert result.losses == tuple(GridLoss(tau, stay, window, table[(tau, stay, window)]) for tau, stay, window in combos)
    tau, stay, window = combos[5]
    assert (result.model.calibration_tau, result.model.stay, result.model.window_sec) == (tau, stay, window)
    assert result.model.validation["validation_loss"] == 1.0


def test_FR16_common_fit_picks_the_real_minimum():
    """진짜 손실에서도 고른 조합이 손실 최소이고 모델의 `validation_loss`가 그 손실이다. 손실은 모두 유한하고 서로 다르다."""
    train, validation = noisy_places()
    config = make_config(
        taus=(0.5, 1.0, 2.0), stays=(0.8, 0.95), window_secs=(3.0, 5.0), auto_zero_min_bins=NOISY.min_bins,
    )

    result = fit_common_model(train, validation, config, bin_sec=BIN_SEC)

    best = min(result.losses, key=lambda grid: grid.loss)
    assert len(result.losses) == 3 * 2 * 2
    assert all(math.isfinite(grid.loss) and grid.loss > 0.0 for grid in result.losses)
    assert len({grid.loss for grid in result.losses}) == len(result.losses)
    assert result.model.validation["validation_loss"] == best.loss
    assert (result.model.calibration_tau, result.model.stay, result.model.window_sec) == (best.tau, best.stay, best.window_sec)
    # 같은 모델의 손실을 다시 재면 표의 값과 같다.
    again = validation_log_loss(replace(result.model, validation={}), validation, result.zeros, bin_sec=BIN_SEC)
    assert again == pytest.approx(best.loss, rel=1e-12)


def test_FR16_common_fit_calibration_follows_tau():
    """τ가 1.0이면 보정은 `none`이고 아니면 `temperature`다."""
    train, validation = two_places()

    one = fit_common_model(train, validation, make_config(taus=(1.0,)), bin_sec=BIN_SEC).model
    two = fit_common_model(train, validation, make_config(taus=(2.0,)), bin_sec=BIN_SEC).model

    assert (one.calibration_method, one.calibration_tau) == ("none", 1.0)
    assert (two.calibration_method, two.calibration_tau) == ("temperature", 2.0)
    assert common_model_to_dict(one)["calibration"] == {"method": "none"}
    assert common_model_to_dict(two)["calibration"] == {"method": "temperature", "tau": 2.0}


def test_FR16_common_fit_validation_adds_loss_to_config_values():
    """모델의 `validation`은 설정의 값에 `validation_loss`를 더한 것이다. 설정의 값은 바뀌지 않는다."""
    train, validation = two_places()
    extra = {"source": "SYNTHETIC_NOT_MEASURED", "quality_verified": False, "generator": {"seed": 1, "devices": [3, 7]}}
    config = make_config(validation=extra)

    result = fit_common_model(train, validation, config, bin_sec=BIN_SEC)

    loss = result.model.validation["validation_loss"]
    assert common_model_to_dict(result.model)["validation"] == {**extra, "validation_loss": loss}
    assert result.model.validation["generator"]["devices"] == (3, 7)  # 읽기 전용 모델은 list를 tuple로 둔다
    assert extra == {"source": "SYNTHETIC_NOT_MEASURED", "quality_verified": False, "generator": {"seed": 1, "devices": [3, 7]}}
    assert make_config().validation == {}
    assert "validation" in common_model_to_dict(fit_common_model(train, validation, make_config(), bin_sec=BIN_SEC).model)
    # 설정에 같은 이름의 값이 있어도 적합이 잰 손실이 이긴다.
    stale = fit_common_model(train, validation, make_config(validation={"validation_loss": 99.0, "note": "kept"}), bin_sec=BIN_SEC)
    assert stale.model.validation["validation_loss"] == min(grid.loss for grid in stale.losses) != 99.0
    assert stale.model.validation["note"] == "kept"


def test_FR16_common_fit_is_deterministic_and_order_free():
    """같은 입력에는 같은 모델이다. 장소, 회차, 프레임의 순서를 바꿔도 같다."""
    train, validation = noisy_places()
    config = make_config(taus=(0.5, 1.0, 2.0), stays=(0.8, 0.95), window_secs=(3.0, 5.0), auto_zero_min_bins=NOISY.min_bins)
    first = fit_common_model(train, validation, config, bin_sec=BIN_SEC)
    again = fit_common_model(train, validation, config, bin_sec=BIN_SEC)

    def reversed_groups(groups: Sequence[PlaceRuns]) -> list[PlaceRuns]:
        return [
            PlaceRuns(group.place_id, [replace(run, frames=tuple(reversed(run.frames))) for run in reversed(group.runs)])
            for group in reversed(groups)
        ]

    shuffled = fit_common_model(reversed_groups(train), reversed_groups(validation), config, bin_sec=BIN_SEC)

    assert len({grid.loss for grid in first.losses}) == len(first.losses)  # 손실이 모두 다른 데이터라서 순서를 바꾼 비교가 의미 있다
    assert common_model_to_dict(again.model) == common_model_to_dict(first.model)
    assert again.losses == first.losses
    assert common_model_to_dict(shuffled.model) == common_model_to_dict(first.model)
    assert shuffled.losses == first.losses
    assert shuffled.zeros == first.zeros


# ── 잘못된 입력 ─────────────────────────────────────────────────────


def _without_runs(groups: Sequence[PlaceRuns], keep: Callable[[LabeledRun], bool]) -> list[PlaceRuns]:
    return [PlaceRuns(group.place_id, [run for run in group.runs if keep(run)]) for group in groups]


def _front_with_one_sample(train: list[PlaceRuns], validation: list[PlaceRuns]) -> tuple[list[PlaceRuns], list[PlaceRuns]]:
    """북쪽 장소의 기기 앞 회차를 하나만 두고 한 칸으로 줄인다. 앞 표본이 하나다."""
    cut = PlaceRuns("place-north", [
        replace(run, ended_at=run.started_at + timedelta(seconds=1)) if run.label == IDS_A[0] else run
        for run in train[0].runs
        if run.label != IDS_A[1]
    ])
    return [cut], validation[:1]


def _constant_roles(train: list[PlaceRuns], validation: list[PlaceRuns]) -> tuple[list[PlaceRuns], list[PlaceRuns]]:
    """칸마다 공통 이동이 없는 평평한 회차. 역할 표본이 모두 같아서 표준편차가 0이다."""
    maker = RunMaker()
    labels = (IDS_A[0], IDS_A[1], NONE_LABEL)

    def flat(suffix: str, count: int) -> PlaceRuns:
        return PlaceRuns("place-north", [
            maker.run(f"flat-{label}-{suffix}", label, pattern_bins(label, 1, IDS_A, shift=1.0) * count) for label in labels
        ])

    return [flat("train", 9)], [flat("validation", 12)]


def _ghost_label(train: list[PlaceRuns], validation: list[PlaceRuns]) -> tuple[list[PlaceRuns], list[PlaceRuns]]:
    ghost = replace(train[0].runs[0], run_id="ghost-run", label="north-ghost")  # 프레임에 나온 적 없는 기기
    return [PlaceRuns(train[0].place_id, [*train[0].runs, ghost])], validation


@pytest.mark.parametrize(
    ("build", "message"),
    [
        pytest.param(lambda t, v: ([], v), "학습", id="empty_train"),
        pytest.param(lambda t, v: (_without_runs(t, lambda run: run.label != NONE_LABEL), v), "none", id="no_none_run"),
        pytest.param(lambda t, v: (_without_runs(t, lambda run: run.label == NONE_LABEL), v), "front", id="no_front_run"),
        pytest.param(lambda t, v: ([t[0], t[0]], v), "같은 장소", id="duplicate_train_place"),
        pytest.param(lambda t, v: (t, [v[0], v[0]]), "같은 장소", id="duplicate_validation_place"),
        pytest.param(_ghost_label, "앵커", id="label_not_an_anchor"),
        pytest.param(_front_with_one_sample, "2개 이상", id="front_has_one_sample"),
        pytest.param(_constant_roles, "표준편차", id="zero_sigma"),
        pytest.param(lambda t, v: (t, []), "검증 회차가 없다", id="no_validation"),
        pytest.param(lambda t, v: (t, [PlaceRuns("place-north", [])]), "검증 회차가 없다", id="validation_without_runs"),
    ],
)
def test_FR16_common_fit_rejects_thin_roles(build, message):
    """역할 표본이 모자라거나 입력이 잘못되면 `FitError`다. 라벨이 장소의 앵커가 아니거나 같은 장소가 둘이어도 마찬가지다."""
    train, validation = two_places()
    train, validation = build(train, validation)

    with pytest.raises(FitError, match=message):
        fit_common_model(train, validation, make_config(), bin_sec=BIN_SEC)


def test_FR16_common_fit_rejects_all_infinite_losses(monkeypatch):
    """모든 조합의 손실이 무한대이면 고를 근거가 없어 `FitError`다."""
    train, validation = two_places()
    monkeypatch.setattr(common_fit, "validation_log_loss", lambda model, places, zeros, *, bin_sec: math.inf)

    with pytest.raises(FitError, match="무한대"):
        fit_common_model(train, validation, make_config(), bin_sec=BIN_SEC)


@pytest.mark.parametrize(
    ("overrides", "bin_sec"),
    [
        pytest.param({"model_version": ""}, BIN_SEC, id="version_empty"),
        pytest.param({"model_version": ".."}, BIN_SEC, id="version_dotdot"),
        pytest.param({"model_version": "a/b"}, BIN_SEC, id="version_slash"),
        pytest.param({"model_version": "a\\b"}, BIN_SEC, id="version_backslash"),
        pytest.param({"model_version": None}, BIN_SEC, id="version_none"),
        pytest.param({"taus": ()}, BIN_SEC, id="taus_empty"),
        pytest.param({"taus": (0.0,)}, BIN_SEC, id="tau_zero"),
        pytest.param({"taus": (1.0, -1.0)}, BIN_SEC, id="tau_negative"),
        pytest.param({"taus": (math.nan,)}, BIN_SEC, id="tau_nan"),
        pytest.param({"taus": (math.inf,)}, BIN_SEC, id="tau_inf"),
        pytest.param({"taus": (True,)}, BIN_SEC, id="tau_bool"),
        pytest.param({"stays": ()}, BIN_SEC, id="stays_empty"),
        pytest.param({"stays": (0.0,)}, BIN_SEC, id="stay_zero"),
        pytest.param({"stays": (1.0,)}, BIN_SEC, id="stay_one"),
        pytest.param({"stays": (1.5,)}, BIN_SEC, id="stay_over_one"),
        pytest.param({"window_secs": ()}, BIN_SEC, id="windows_empty"),
        pytest.param({"window_secs": (0.0,)}, BIN_SEC, id="window_zero"),
        pytest.param({"auto_zero_min_bins": 0}, BIN_SEC, id="min_bins_zero"),
        pytest.param({"auto_zero_min_bins": -3}, BIN_SEC, id="min_bins_negative"),
        pytest.param({"auto_zero_min_bins": 12.0}, BIN_SEC, id="min_bins_float"),
        pytest.param({"auto_zero_min_bins": True}, BIN_SEC, id="min_bins_bool"),
        pytest.param({}, 0.0, id="bin_sec_zero"),
        pytest.param({}, -1.0, id="bin_sec_negative"),
        pytest.param({}, math.nan, id="bin_sec_nan"),
        pytest.param({}, math.inf, id="bin_sec_inf"),
        pytest.param({}, 1e-9, id="bin_sec_below_microsecond"),
    ],
)
def test_FR16_common_fit_rejects_bad_config(overrides, bin_sec):
    """설정이 범위 밖이면 데이터를 보기 전에 `FitError`다."""
    train, validation = two_places()

    with pytest.raises(FitError):
        fit_common_model(train, validation, make_config(**overrides), bin_sec=bin_sec)
