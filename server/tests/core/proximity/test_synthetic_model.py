"""합성 생성기와 합성 공통 모델 `synthetic-2.json` (FR-16).

생성기가 시드 하나로 같은 회차를 내고 명세(방, 기기, 라벨, 시각, RSSI 식)대로 만드는지, 같은 시드로 다시 적합한 모델이
커밋한 파일과 같은지, 파일에 합성 표시가 있고 배치가 없는지, 그 모델이 다른 시드로 만든 방에서 기기 앞 방문자를 맞히는지 본다.
합성 값은 시험용이 아니라 모델 파일의 원천이다. 실측이 아니고, 이 시험은 실제 정확도를 말하지 않는다.
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.core.proximity import synthetic as generator_module
from nearpilot.core.proximity.common_fit import (
    NONE_LABEL,
    CommonFitConfig,
    LabeledRun,
    PlaceRuns,
    fit_common_model,
    place_anchors,
    place_zeros,
)
from nearpilot.core.proximity.common_model import CommonModel, common_model_to_dict, load_common_model
from nearpilot.core.proximity.posterior import compute_posteriors
from nearpilot.core.proximity.synthetic import SYNTHETIC_SOURCE, SyntheticSpec, generate_places, make_synthetic_model
from nearpilot.shared.models import BeaconKey

from .conftest import make_node

SERVER_ROOT = Path(__file__).resolve().parents[3]  # tests/core/proximity/<이 파일> → server/
MODEL_FILE = SERVER_ROOT / "config" / "final-demo" / "models" / "synthetic-2.json"
BIN_SEC = 1.0  # Settings().rssi_frame_sec의 기본값
FORBIDDEN_KEYS = {"x", "y", "front_x", "nodes", "none_points", "place_id"}
# 모델을 적합한 시드(252)와 다른 시드로 만든 평가용 방 둘. 이 시드는 기기 3대 방과 7대 방을 만든다.
EVAL_SPEC = SyntheticSpec(seed=7, rooms=2)


# ── 도우미 ───────────────────────────────────────────────────────────


def assert_same_content(actual: object, expected: object, where: str = "model") -> None:
    """JSON 내용이 같은지 본다. 실수는 상대 오차 1e-9 이내, 나머지(정수, 문자열, bool)는 형까지 그대로 같아야 한다.

    `math.log`, `math.exp`는 운영체제마다 마지막 자리가 다를 수 있어서 실수를 정확히 비교하지 않는다.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys(), f"{where}: 키가 다르다"
        for key in expected:
            assert_same_content(actual[key], expected[key], f"{where}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), f"{where}: 길이가 다르다"
        for i, (a, e) in enumerate(zip(actual, expected)):
            assert_same_content(a, e, f"{where}[{i}]")
    elif isinstance(expected, float):
        assert isinstance(actual, float), f"{where}: 실수가 아니다 ({actual!r})"
        assert math.isclose(actual, expected, rel_tol=1e-9), f"{where}: {actual!r} != {expected!r}"
    else:
        assert type(actual) is type(expected) and actual == expected, f"{where}: {actual!r} != {expected!r}"


def keys_in(value: object) -> set[str]:
    """JSON 내용 안의 모든 객체 키."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in keys_in(item)}
    if isinstance(value, list):
        return {key for item in value for key in keys_in(item)}
    return set()


def generator_fields(spec: SyntheticSpec) -> dict[str, object]:
    """`SyntheticSpec`의 필드 전체. 모델 파일의 JSON은 tuple을 list로 쓴다."""
    return {name: list(value) if isinstance(value, tuple) else value for name, value in asdict(spec).items()}


def last_evaluation_time(run: LabeledRun, window_sec: float) -> datetime:
    """`validation_log_loss`와 같은 평가 시각 중 마지막: `started_at + window_sec`에서 `BIN_SEC` 간격으로 `ended_at` 이하까지."""
    first = run.started_at + timedelta(seconds=window_sec)
    step = timedelta(seconds=BIN_SEC)
    return first + ((run.ended_at - first) // step) * step


def top_device(model: CommonModel, run: LabeledRun, anchors: frozenset[str], zeros: Mapping[str, float]) -> str:
    """회차의 마지막 평가 시각에서 사후확률이 가장 큰 기기."""
    nodes = [make_node(node_id, "eval-place", capability="light") for node_id in sorted(anchors)]
    posteriors = compute_posteriors(
        model, run.frames, run.beacon, nodes, nodes, last_evaluation_time(run, model.window_sec),
        bin_sec=BIN_SEC, participants=anchors, zeros=zeros, calib_version="test",
    ).posteriors
    return max(posteriors, key=lambda node_id: posteriors[node_id])


def front_accuracy(
    model: CommonModel, runs: Sequence[LabeledRun], anchors: frozenset[str], zeros: Mapping[str, float],
) -> float:
    """기기 앞 회차 중 1등이 라벨 기기인 비율."""
    assert runs
    return sum(top_device(model, run, anchors, zeros) == run.label for run in runs) / len(runs)


def lowered(group: PlaceRuns, node_id: str, db: float) -> PlaceRuns:
    """묶음의 모든 회차에서 `node_id` 기기의 프레임 RSSI를 db만큼 낮춘다."""
    return PlaceRuns(group.place_id, [
        replace(run, frames=tuple(
            replace(frame, rssi=frame.rssi - db) if frame.node_id == node_id else frame for frame in run.frames
        ))
        for run in group.runs
    ])


class RecordingRandom(random.Random):
    """`uniform`과 `randint`를 부를 때마다 호출(이름, 인수)과 결과를 순서대로 적는 `random.Random`."""

    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.calls: list[tuple] = []
        self.values: list[float] = []

    def uniform(self, a, b):
        value = super().uniform(a, b)
        self.calls.append(("uniform", a, b))
        self.values.append(value)
        return value

    def randint(self, a, b):
        value = super().randint(a, b)
        self.calls.append(("randint", a, b))
        self.values.append(value)
        return value


@pytest.fixture(scope="module")
def synthetic_model() -> CommonModel:
    return load_common_model(MODEL_FILE)


@pytest.fixture(scope="module")
def eval_places() -> tuple[list[PlaceRuns], list[PlaceRuns]]:
    return generate_places(EVAL_SPEC, bin_sec=BIN_SEC)


# ── 합성 모델 파일 ──────────────────────────────────────────────────


def test_FR16_synthetic_model_reproducible():
    """시드 252로 다시 만든 모델이 커밋한 `synthetic-2.json`과 같다 (숫자는 상대 오차 1e-9)."""
    fresh = make_synthetic_model("synthetic-2", SyntheticSpec(), bin_sec=BIN_SEC)

    stored = json.loads(MODEL_FILE.read_text(encoding="utf-8"))

    assert_same_content(common_model_to_dict(fresh.model), stored)
    assert [(g.tau, g.stay, g.window_sec) for g in fresh.losses] == [(1.0, 0.9, 3.5)]  # HMM 값은 고정
    assert sorted(fresh.zeros) == [f"syn-room-{r}" for r in range(1, 7)]


def test_FR16_content_comparison_tolerates_only_float_noise():
    """재현 시험의 비교 도우미가 마지막 자리 오차만 봐준다: 값, 키, 길이, 형이 다르면 실패한다."""
    base = {"a": 1.0, "b": [1, 2.5, {"c": "text", "d": True}], "e": 7}

    assert_same_content({**base, "a": 1.0 * (1 + 1e-12)}, base)
    for changed in (
        {**base, "a": 1.0 * (1 + 1e-6)},
        {**base, "b": [1, 2.5, {"c": "other", "d": True}]},
        {**base, "b": [1, 2.5, {"c": "text", "d": False}]},
        {**base, "b": [1, 2.5]},
        {**base, "f": 1},
        {**base, "e": 7.0},
        {**base, "e": True},
    ):
        with pytest.raises(AssertionError):
            assert_same_content(changed, base)


def test_FR16_synthetic_model_marked():
    """파일이 합성임을 밝히고 (`source`, `quality_verified`, 생성기 설정 전체) 배치가 없다. 읽을 수 있는 공통 모델 파일이다."""
    content = json.loads(MODEL_FILE.read_text(encoding="utf-8"))

    validation = content["validation"]
    assert validation["source"] == SYNTHETIC_SOURCE == "SYNTHETIC_NOT_MEASURED"
    assert validation["quality_verified"] is False
    assert validation["generator"] == generator_fields(SyntheticSpec())
    assert validation["generator"]["seed"] == 252
    assert isinstance(validation["validation_loss"], float) and 0.0 < validation["validation_loss"] < 1.0
    assert keys_in(content) & FORBIDDEN_KEYS == set()

    model = load_common_model(MODEL_FILE)
    assert model.model_version == "synthetic-2"
    assert model.auto_zero_min_bins == SyntheticSpec().min_bins == 120
    assert common_model_to_dict(model) == content


@pytest.mark.parametrize("bin_sec", [1.0, 0.5])
def test_FR16_make_synthetic_model_uses_the_shared_fit(bin_sec):
    """합성 모델도 실측과 같은 `fit_common_model`로 만든다: 같은 칸 길이로 같은 회차에 같은 설정을 넣으면 같은 결과이고, 후보는 명세의 격자다."""
    spec = replace(
        SyntheticSpec(), rooms=2, devices=(3, 3), train_runs=2, validation_runs=1, run_bins=10,
        taus=(1.0, 2.0), stays=(0.85,), window_secs=(3.0,), min_bins=5,
    )

    result = make_synthetic_model("tiny-1", spec, bin_sec=bin_sec)

    train, validation = generate_places(spec, bin_sec=bin_sec)
    config = CommonFitConfig(
        "tiny-1", spec.taus, spec.stays, spec.window_secs, spec.min_bins,
        validation={"source": SYNTHETIC_SOURCE, "quality_verified": False, "generator": generator_fields(spec)},
    )
    direct = fit_common_model(train, validation, config, bin_sec=bin_sec)
    assert common_model_to_dict(result.model) == common_model_to_dict(direct.model)
    assert [(grid.tau, grid.stay, grid.window_sec) for grid in result.losses] == [(1.0, 0.85, 3.0), (2.0, 0.85, 3.0)]
    assert result.model.model_version == "tiny-1"
    assert result.model.auto_zero_min_bins == 5
    assert result.model.validation["generator"]["taus"] == (1.0, 2.0)
    assert result.zeros.keys() == {"syn-room-1", "syn-room-2"}


# ── 합성 모델의 동작 ────────────────────────────────────────────────


def test_FR16_front_device_highest_posterior(synthetic_model, eval_places):
    """`synthetic-2.json` 모델이 다른 시드의 방에서 기기 앞 회차의 마지막 평가 시각에 사후확률 1등을 라벨 기기에게 준다 (90% 이상).

    장소의 자동 영점은 그 장소의 모든 회차 프레임에서 `place_zeros`로 찾는다.
    """
    train, validation = eval_places
    correct = 0
    total = 0
    for train_group, validation_group in zip(train, validation):
        anchors = place_anchors(train_group, validation_group)
        zeros = place_zeros([train_group, validation_group], bin_sec=BIN_SEC, min_bins=synthetic_model.auto_zero_min_bins)
        assert zeros.keys() == anchors  # 기기가 셋 이상인 방은 모든 기기의 자동 영점을 찾는다
        for run in validation_group.runs:
            if run.label != NONE_LABEL:
                total += 1
                correct += top_device(synthetic_model, run, anchors, zeros) == run.label

    assert total == EVAL_SPEC.validation_runs * sum(len(place_anchors(t, v)) for t, v in zip(train, validation))
    assert correct / total >= 0.9, f"{correct}/{total}"


def test_FR16_auto_zero_corrects_offset(synthetic_model, eval_places):
    """기기 하나의 모든 프레임을 8 dB 낮추면 자동 영점이 4 dB 이상 내려가고, 그 보정으로 그 기기 앞 회차의 정답률이 80% 이상이 된다.

    자동 영점이 없으면 (`zeros={}`) 정답률은 그보다 낮거나 같다. 자동 영점은 1등이 아닌 앵커들 안에서 자기를 포함한 평균과의 차이라서
    기기 m대가 비교에 들면 8·(m−1)/m만큼 움직인다. 그래서 기기가 가장 많은 방을 고른다.
    기기는 8 dB를 낮추기 전 정답률이 80% 이상인 첫 기기다 — 낮추기 전에도 틀리는 기기는 틀린 이유가 오프셋인지 알 수 없다.
    정답률은 그 기기 앞 회차 전부(학습과 검증)로 잰다. 모델은 다른 시드의 데이터로 적합해서 둘 다 처음 보는 회차다.
    """
    train, validation = eval_places
    index = max(range(len(train)), key=lambda i: len(place_anchors(train[i], validation[i])))  # 기기가 가장 많은 방. 같으면 앞의 방
    groups = [train[index], validation[index]]
    anchors = place_anchors(*groups)
    min_bins = synthetic_model.auto_zero_min_bins
    before = place_zeros(groups, bin_sec=BIN_SEC, min_bins=min_bins)

    def front_runs(node_id: str, place_groups: Sequence[PlaceRuns]) -> list[LabeledRun]:
        return [run for group in place_groups for run in group.runs if run.label == node_id]

    node_id = next(
        node_id for node_id in sorted(anchors)
        if front_accuracy(synthetic_model, front_runs(node_id, groups), anchors, before) >= 0.8
    )
    lowered_groups = [lowered(group, node_id, 8.0) for group in groups]
    after = place_zeros(lowered_groups, bin_sec=BIN_SEC, min_bins=min_bins)

    runs = front_runs(node_id, lowered_groups)
    with_zeros = front_accuracy(synthetic_model, runs, anchors, after)
    without_zeros = front_accuracy(synthetic_model, runs, anchors, {})
    assert len(anchors) >= 4
    assert len(runs) == EVAL_SPEC.train_runs + EVAL_SPEC.validation_runs
    assert after[node_id] <= before[node_id] - 4.0, (before[node_id], after[node_id])
    assert with_zeros >= without_zeros, (with_zeros, without_zeros)
    assert with_zeros >= 0.8, with_zeros


# ── 생성기 ──────────────────────────────────────────────────────────


def test_FR16_synthetic_generator_is_seeded_and_uses_no_global_random():
    """같은 시드에는 같은 프레임이 나오고, 다른 시드에는 다른 프레임이 나온다. 전역 `random`을 읽지도 바꾸지도 않는다."""
    spec = replace(SyntheticSpec(), rooms=2, devices=(3, 4), run_bins=10)

    random.seed(1)
    global_state = random.getstate()
    first = generate_places(spec, bin_sec=BIN_SEC)
    assert random.getstate() == global_state  # 전역 난수를 쓰지 않았다
    random.seed(2)  # 전역 시드가 달라도 결과가 같아야 한다
    again = generate_places(spec, bin_sec=BIN_SEC)
    other = generate_places(replace(spec, seed=spec.seed + 1), bin_sec=BIN_SEC)

    def rssis(places: tuple[list[PlaceRuns], list[PlaceRuns]]) -> list[float]:
        return [frame.rssi for group in places[0] + places[1] for run in group.runs for frame in run.frames]

    assert first == again
    assert rssis(first) == rssis(again) and len(rssis(first)) > 100
    assert rssis(first) != rssis(other)
    assert first != other


def test_FR16_synthetic_generator_structure():
    """방 6곳, 방마다 기기 3~7대, 라벨(기기마다 하나와 none)마다 학습 5개와 검증 2개, 회차는 30칸이고 칸마다 앵커마다 프레임 하나다."""
    train, validation = generate_places(SyntheticSpec(), bin_sec=BIN_SEC)

    assert [group.place_id for group in train] == [f"syn-room-{r}" for r in range(1, 7)]
    assert [group.place_id for group in validation] == [group.place_id for group in train]
    expected_order: list[str] = []
    for room, (train_group, validation_group) in enumerate(zip(train, validation), start=1):
        anchors = place_anchors(train_group, validation_group)
        devices = [f"syn{room}-{i}" for i in range(1, len(anchors) + 1)]
        assert 3 <= len(devices) <= 7
        assert anchors == frozenset(devices)
        labels = [*devices, NONE_LABEL]
        expected_order += [
            f"syn-room-{room}-{label}-{kind}-{number}"
            for label in labels
            for kind, count in (("train", 5), ("validation", 2))
            for number in range(1, count + 1)
        ]
        for group, kind, count in ((train_group, "train", 5), (validation_group, "validation", 2)):
            assert [run.label for run in group.runs] == [label for label in labels for _ in range(count)]
            assert [run.run_id for run in group.runs] == [
                f"syn-room-{room}-{label}-{kind}-{number}" for label in labels for number in range(1, count + 1)
            ]
            for run in group.runs:
                assert len(run.frames) == 30 * len(devices)
                assert {frame.node_id for frame in run.frames} == anchors
                assert run.ended_at - run.started_at == timedelta(seconds=30)
    # 시간은 방 → 라벨(기기 순서, 마지막이 none) → 학습 → 검증 순으로 흐른다.
    in_time_order = sorted((run for group in train + validation for run in group.runs), key=lambda run: run.started_at)
    assert [run.run_id for run in in_time_order] == expected_order


def test_FR16_synthetic_generator_frame_fields():
    """프레임의 비콘, 연결 ID, 순번, 시각: 칸 k의 프레임은 회차 시작 + (k + 0.5)·bin_sec이고, 회차는 모든 방에 걸쳐 (30·bin_sec + 10초)씩 늦어진다."""
    spec = SyntheticSpec()
    train, validation = generate_places(spec, bin_sec=BIN_SEC)
    runs = sorted((run for group in train + validation for run in group.runs), key=lambda run: run.started_at)

    first_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    span = timedelta(seconds=spec.run_bins * BIN_SEC + 10)
    assert [run.started_at for run in runs] == [first_start + i * span for i in range(len(runs))]
    beacon = BeaconKey("00000000-0000-4000-8000-000000000252", 1, 1)
    seqs: dict[str, list[int]] = {}
    for run in runs:
        assert run.beacon == beacon
        for frame in run.frames:
            k = round((frame.frame_ts - run.started_at) / timedelta(seconds=BIN_SEC) - 0.5)
            assert frame.frame_ts == run.started_at + timedelta(seconds=(k + 0.5) * BIN_SEC)
            assert 0 <= k < spec.run_bins
            assert frame.beacon == beacon
            assert frame.sent_at == frame.frame_ts
            assert frame.received_at == frame.frame_ts + timedelta(milliseconds=50)
            assert frame.session_id == f"{frame.node_id}-s1"
            seqs.setdefault(frame.node_id, []).append(frame.seq)
    # 기기마다 seq는 1부터 모든 회차(학습과 검증)에 걸쳐 하나씩 늘어난다. 키가 겹치는 프레임이 없다.
    for node_id, values in seqs.items():
        assert values == list(range(1, len(values) + 1)), node_id


@pytest.mark.parametrize("bin_sec", [0.5, 2.0])
def test_FR16_synthetic_generator_follows_bin_sec(bin_sec):
    """칸 길이를 따른다: 회차는 run_bins·bin_sec초이고, 칸 k의 프레임은 시작 + (k + 0.5)·bin_sec에 있고, 회차 사이는 10초다."""
    spec = replace(SyntheticSpec(), rooms=1, devices=(3, 3), train_runs=2, validation_runs=1, run_bins=6)

    train, validation = generate_places(spec, bin_sec=bin_sec)

    runs = sorted((run for group in train + validation for run in group.runs), key=lambda run: run.started_at)
    length = timedelta(seconds=6 * bin_sec)
    first_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert len(runs) == 4 * 3
    assert [run.started_at for run in runs] == [first_start + i * (length + timedelta(seconds=10)) for i in range(len(runs))]
    for run in runs:
        assert run.ended_at - run.started_at == length
        assert sorted({frame.frame_ts for frame in run.frames}) == [
            run.started_at + timedelta(seconds=(k + 0.5) * bin_sec) for k in range(6)
        ]


def test_FR16_synthetic_generator_follows_run_gap_and_receive_delay():
    """회차 사이의 빈 시간과 수신 지연은 설정(`run_gap_sec`, `receive_delay_sec`)을 따른다. 코드 상수가 아니다.

    값은 기본값(10초, 0.05초)과 다르게 둔다. 회차는 `run_bins·bin_sec + run_gap_sec`초씩 늦어지고, 프레임의 `received_at`은
    `frame_ts`보다 `receive_delay_sec`초 뒤다.
    """
    spec = replace(
        SyntheticSpec(), rooms=1, devices=(3, 3), train_runs=2, validation_runs=1, run_bins=6,
        run_gap_sec=7.5, receive_delay_sec=0.2,
    )

    train, validation = generate_places(spec, bin_sec=BIN_SEC)

    runs = sorted((run for group in train + validation for run in group.runs), key=lambda run: run.started_at)
    first_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    span = timedelta(seconds=6 * BIN_SEC + 7.5)
    assert len(runs) == 4 * 3
    assert [run.started_at for run in runs] == [first_start + i * span for i in range(len(runs))]
    assert {frame.received_at - frame.frame_ts for run in runs for frame in run.frames} == {timedelta(milliseconds=200)}


@pytest.mark.parametrize(
    ("front_m", "distance"),
    [pytest.param(0.2, 0.7, id="clamped_to_min_distance"), pytest.param(0.9, 0.9, id="free_distance")],
)
def test_FR16_synthetic_rssi_follows_the_path_loss_formula(front_m, distance):
    """`rssi = A − 10·n·log10(max(거리, min_distance_m)) + 영점 + 잡음`: 잡음 0이면 기기 앞의 그 기기 프레임은 식 그대로다.

    값은 모두 기본값과 다르게 둔다. 기기 앞 거리는 front_m이고 min_distance_m(0.7)보다 가까우면 0.7로 센다.
    """
    spec = SyntheticSpec(
        rooms=1, devices=(3, 3), sigma=0.0, zero_db=(2.5, 2.5), front_m=front_m, min_distance_m=0.7, A=-55.0, n=2.5,
        train_runs=2, validation_runs=1, run_bins=4,
    )
    train, validation = generate_places(spec, bin_sec=BIN_SEC)

    expected = spec.A - 10 * spec.n * math.log10(distance) + 2.5
    own_frames = [
        frame.rssi
        for group in (train[0], validation[0])
        for run in group.runs
        if run.label != NONE_LABEL
        for frame in run.frames
        if frame.node_id == run.label
    ]
    assert len(own_frames) == 3 * 3 * 4  # 기기 셋 × 회차 셋 × 4칸
    assert own_frames == pytest.approx([expected] * len(own_frames), abs=1e-9)


def test_FR16_synthetic_noise_has_the_configured_sigma():
    """잡음은 N(0, sigma)이다: 기기 앞 방문자의 그 기기 프레임이 평균 −52.98, 표준편차 sigma 근처다."""
    spec = SyntheticSpec(
        rooms=1, devices=(3, 3), zero_db=(0.0, 0.0), train_runs=1, validation_runs=0, run_bins=3000, sigma=3.0,
    )
    train, _ = generate_places(spec, bin_sec=BIN_SEC)

    run = next(run for run in train[0].runs if run.label != NONE_LABEL)
    own = [frame.rssi for frame in run.frames if frame.node_id == run.label]
    assert len(own) == 3000
    assert statistics.fmean(own) == pytest.approx(-59.0 - 20 * math.log10(0.5), abs=0.3)
    assert statistics.pstdev(own) == pytest.approx(3.0, abs=0.2)


def test_FR16_synthetic_room_geometry():
    """방은 가로·세로가 범위 안이고, 기기 3~7대가 벽 위에 서로 min_gap_m 이상 떨어져 있다.

    기기 앞은 벽에서 방 안쪽으로 front_m이다. 영점 차이는 zero_db 범위 안이다. none 위치는 방 안에서 모든 기기 앞과 none_clearance_m 이상 떨어진다.
    배치는 반환값에 없어서 방 하나를 만드는 비공개 함수로 본다.
    """
    # 방이 넉넉해야 none 자리가 늘 있다. 거리와 범위는 기본값과 다르게 둔다.
    spec = SyntheticSpec(width_m=(6.0, 8.0), depth_m=(5.0, 6.0), front_m=0.8, min_gap_m=1.2, zero_db=(-3.0, 5.0))
    counts = set()
    walls = set()
    for seed in range(40):
        rng = random.Random(seed)
        room = generator_module._make_room(rng, spec, 3)
        counts.add(len(room.anchors))
        assert room.place_id == "syn-room-3"
        assert spec.width_m[0] <= room.width <= spec.width_m[1] and spec.depth_m[0] <= room.depth <= spec.depth_m[1]
        assert [anchor.node_id for anchor in room.anchors] == [f"syn3-{i}" for i in range(1, len(room.anchors) + 1)]
        for anchor in room.anchors:
            x, y = anchor.position
            assert 0.0 <= x <= room.width and 0.0 <= y <= room.depth
            assert x in (0.0, room.width) or y in (0.0, room.depth)  # 벽 위다
            fx, fy = anchor.front
            assert 0.0 < fx < room.width and 0.0 < fy < room.depth  # 방 안이다
            # 기기가 선 벽에서 방 안쪽으로 수직으로 front_m이다 (모서리 근처에서는 다른 벽이 더 가까울 수 있다)
            if y == 0.0:
                wall, expected_front = "bottom", (x, spec.front_m)
            elif x == room.width:
                wall, expected_front = "right", (room.width - spec.front_m, y)
            elif y == room.depth:
                wall, expected_front = "top", (x, room.depth - spec.front_m)
            else:
                wall, expected_front = "left", (spec.front_m, y)
            walls.add(wall)
            assert anchor.front == pytest.approx(expected_front, abs=1e-9)
            assert spec.zero_db[0] <= anchor.zero_db <= spec.zero_db[1]
        for i, a in enumerate(room.anchors):
            for b in room.anchors[i + 1:]:
                assert math.dist(a.position, b.position) >= spec.min_gap_m
        for _ in range(5):
            x, y = generator_module._none_position(rng, spec, room)
            assert 0.0 <= x <= room.width and 0.0 <= y <= room.depth
            assert all(math.dist((x, y), anchor.front) >= spec.none_clearance_m for anchor in room.anchors)
    assert counts == {3, 4, 5, 6, 7}  # 기기 수는 범위 양 끝을 포함해 고르게 나온다
    assert walls == {"bottom", "right", "top", "left"}  # 기기는 네 벽 모두에 선다


def test_FR16_synthetic_room_draws_in_the_documented_order():
    """방 하나의 난수는 가로, 세로, 기기 수, (기기마다: 벽 위 위치를 조건이 맞을 때까지, 영점 차이) 순서로 뽑는다."""
    spec = SyntheticSpec(width_m=(6.0, 8.0), depth_m=(5.0, 6.0), devices=(4, 4), zero_db=(-3.0, 5.0))
    rng = RecordingRandom(3)

    room = generator_module._make_room(rng, spec, 1)

    names = {
        ("uniform", *spec.width_m): "W",
        ("uniform", *spec.depth_m): "D",
        ("randint", *spec.devices): "N",
        ("uniform", 0.0, 2.0 * (room.width + room.depth)): "P",  # 둘레 위의 위치
        ("uniform", *spec.zero_db): "Z",
    }
    sequence = "".join(names.get(call, "?") for call in rng.calls)
    assert re.fullmatch(r"WDN(?:P+Z){4}", sequence), sequence


def test_FR16_synthetic_redraw_limit_is_1000_redraws():
    """자리를 못 찾으면 처음 한 번에 다시 뽑기 1000번을 더해 1001번 뽑고 `ValueError`다."""
    rng = RecordingRandom(1)
    with pytest.raises(ValueError, match="min_gap_m"):
        generator_module._make_room(rng, SyntheticSpec(devices=(2, 2), min_gap_m=100.0), 1)
    perimeter = ("uniform", 0.0, 2.0 * (rng.values[0] + rng.values[1]))  # 앞의 두 값이 가로와 세로다
    assert rng.calls.count(perimeter) == 1 + 1001  # 첫째 기기는 바로 놓이고, 둘째 기기는 1001번 뽑고 포기한다

    spec = SyntheticSpec(none_clearance_m=100.0)
    rng = RecordingRandom(1)
    room = generator_module._make_room(rng, spec, 1)
    before = len(rng.calls)
    with pytest.raises(ValueError, match="none_clearance_m"):
        generator_module._none_position(rng, spec, room)
    assert len(rng.calls) - before == 2 * 1001  # 가로와 세로 좌표를 한 쌍씩 1001번


def test_FR16_synthetic_none_visitor_keeps_clear_of_every_device():
    """none 회차의 방문자는 모든 기기 앞에서 none_clearance_m 이상 떨어져서, 삼각 부등식으로 어느 기기까지도 (none_clearance_m − front_m) 이상이다.

    배치는 반환값에 없어서 RSSI에서 거리를 되짚는다 (잡음 0, 영점 0, 가까운 거리를 자르지 않는다).
    """
    spec = SyntheticSpec(
        seed=5, rooms=8, devices=(7, 7), width_m=(5.0, 5.0), depth_m=(4.0, 4.0), sigma=0.0, zero_db=(0.0, 0.0),
        min_distance_m=0.01, train_runs=3, validation_runs=0, run_bins=1,
    )
    train, _ = generate_places(spec, bin_sec=BIN_SEC)

    checked = 0
    for group in train:
        # 잡음이 없어서 RSSI 묶음이 방문자 자리를 정한다. none 회차는 회차마다 다른 자리에 선다.
        positions = {tuple(frame.rssi for frame in run.frames) for run in group.runs if run.label == NONE_LABEL}
        assert len(positions) == spec.train_runs
        for run in group.runs:
            if run.label == NONE_LABEL:
                for frame in run.frames:
                    distance = 10 ** ((spec.A - frame.rssi) / (10 * spec.n))
                    assert distance >= spec.none_clearance_m - spec.front_m - 1e-9, (run.run_id, frame.node_id)
                    checked += 1
    assert checked == 8 * 3 * 7


def test_FR16_synthetic_generator_gives_up_when_no_position_fits():
    """조건에 맞는 자리를 1000번 넘게 다시 뽑아도 찾지 못하면 `ValueError`다."""
    with pytest.raises(ValueError, match="min_gap_m"):
        generate_places(SyntheticSpec(devices=(3, 3), min_gap_m=100.0), bin_sec=BIN_SEC)
    with pytest.raises(ValueError, match="none_clearance_m"):
        generate_places(SyntheticSpec(none_clearance_m=100.0), bin_sec=BIN_SEC)
