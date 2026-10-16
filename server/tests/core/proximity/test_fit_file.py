"""적합한 모델의 파일 쓰기 (FR-16, FR-22).

`model_to_dict`가 모델 파일의 형식으로 바꾸는지, `write_model_file`이 쓴 파일을 `load_model`이 같은 모델로 읽는지,
같은 입력에서 같은 바이트가 나오는지, 있는 파일을 덮어쓰지 않는지 확인한다. 파일은 `tmp_path`에만 쓴다.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from nearpilot.core.proximity import (
    NONE_LABEL,
    FitResult,
    LabeledRun,
    NodeModel,
    ProximityModel,
    load_model,
    model_to_dict,
    write_model_file,
)

from .conftest import T0
from .runs import make_run
from .test_filter import MODEL_A, MODEL_B
from .test_fit import ANCHORS, BASE_CONFIG, TRAIN_RUNS, VALIDATION_RUNS, make_runs, run_fit, with_foreign_frames

SMALL_CONFIG = replace(BASE_CONFIG, taus=(1.0, 2.0), stays=(0.8, 0.9))  # 조합 4개: τ 2개 × stay 2개 × window_sec 1개

# 손으로 쓴 모델. 필드마다 값이 달라서 필드가 뒤바뀌면 드러난다. 기기는 b, a 순서로 담았고 파일에는 a, b 순서로 써야 한다.
# validation의 한글 회차 ID는 UTF-8로 쓰는지, null 손실은 무한대를 바꾼 값인지 본다.
HAND_VALIDATION = {
    "criterion": "log_loss",
    "loss": 0.25,
    "grid": [
        {"tau": 1.0, "stay": 0.8, "window_sec": 5.0, "loss": None},
        {"tau": 2.0, "stay": 0.8, "window_sec": 5.0, "loss": 0.25},
    ],
    "train_runs": ["학습-1"],
    "validation_runs": ["검증-1"],
}
HAND_MODEL = ProximityModel(
    place_id="place-x",
    model_version="v-1",
    calib_version="temp-1",
    window_sec=5.0,
    stay=0.8,
    min_distance_m=0.3,
    calibration_method="temperature",
    calibration_tau=2.0,
    nodes={
        "b": NodeModel("b", x=11.0, y=0.5, front_x=10.0, front_y=0.25, A=-58.0, n=2.1, sigma=3.0, none_rssi=-89.0, none_sigma=5.0),
        "a": NodeModel("a", x=0.0, y=1.5, front_x=1.0, front_y=2.5, A=-59.0, n=1.9, sigma=4.0, none_rssi=-90.0, none_sigma=6.0),
    },
    validation=HAND_VALIDATION,
    none_points=((1.5, 2.5), (3.0, -1.0)),
)
# HAND_MODEL의 모델 파일 내용. 키 순서는 파일에 쓰는 순서다 (spec 6절).
HAND_DICT = {
    "place_id": "place-x",
    "model_version": "v-1",
    "calib_version": "temp-1",
    "window_sec": 5.0,
    "transition": {"stay": 0.8},
    "min_distance_m": 0.3,
    "calibration": {"method": "temperature", "tau": 2.0},
    "nodes": {
        "a": {"x": 0.0, "y": 1.5, "front_x": 1.0, "front_y": 2.5, "A": -59.0, "n": 1.9, "sigma": 4.0, "none_rssi": -90.0, "none_sigma": 6.0},
        "b": {"x": 11.0, "y": 0.5, "front_x": 10.0, "front_y": 0.25, "A": -58.0, "n": 2.1, "sigma": 3.0, "none_rssi": -89.0, "none_sigma": 5.0},
    },
    "none_points": [{"x": 1.5, "y": 2.5}, {"x": 3.0, "y": -1.0}],
    "validation": HAND_VALIDATION,
}
# HAND_MODEL을 쓴 파일의 글자 그대로다. 들여쓰기 2칸, 한글은 그대로, 끝에 줄바꿈 하나다.
HAND_TEXT = """\
{
  "place_id": "place-x",
  "model_version": "v-1",
  "calib_version": "temp-1",
  "window_sec": 5.0,
  "transition": {
    "stay": 0.8
  },
  "min_distance_m": 0.3,
  "calibration": {
    "method": "temperature",
    "tau": 2.0
  },
  "nodes": {
    "a": {
      "x": 0.0,
      "y": 1.5,
      "front_x": 1.0,
      "front_y": 2.5,
      "A": -59.0,
      "n": 1.9,
      "sigma": 4.0,
      "none_rssi": -90.0,
      "none_sigma": 6.0
    },
    "b": {
      "x": 11.0,
      "y": 0.5,
      "front_x": 10.0,
      "front_y": 0.25,
      "A": -58.0,
      "n": 2.1,
      "sigma": 3.0,
      "none_rssi": -89.0,
      "none_sigma": 5.0
    }
  },
  "none_points": [
    {
      "x": 1.5,
      "y": 2.5
    },
    {
      "x": 3.0,
      "y": -1.0
    }
  ],
  "validation": {
    "criterion": "log_loss",
    "loss": 0.25,
    "grid": [
      {
        "tau": 1.0,
        "stay": 0.8,
        "window_sec": 5.0,
        "loss": null
      },
      {
        "tau": 2.0,
        "stay": 0.8,
        "window_sec": 5.0,
        "loss": 0.25
      }
    ],
    "train_runs": [
      "학습-1"
    ],
    "validation_runs": [
      "검증-1"
    ]
  }
}
"""


# ── 도우미 ───────────────────────────────────────────────────────────


def small_fit(
    *, train: Sequence[LabeledRun] = TRAIN_RUNS, validation: Sequence[LabeledRun] = VALIDATION_RUNS[:1]
) -> FitResult:
    """조합 4개의 작은 입력으로 적합한다. 학습 회차 14개와 검증 회차 1개이고, 손실은 모두 유한하다."""
    return run_fit(train=train, validation=validation, config=SMALL_CONFIG)


def infinite_loss_fit() -> FitResult:
    """τ = 1의 검증 손실이 무한대인 적합. 3단계 `test_FR16_zero_probability_loses`와 같은 입력이다."""
    # 학습 잡음이 0.5 dB라서 sigma가 약 0.5다. 잡음 2 dB로 적합하면 τ = 1에서도 손실이 무한대가 되지 않는다.
    train = [
        *make_runs(MODEL_A, ANCHORS, noise_sigma=0.5),
        *make_runs(MODEL_A, [NONE_LABEL], noise_sigma=0.5, first_seed=1000),
    ]
    # 라벨은 locker-1 앞인데 프레임은 speaker-1 앞의 것이다 (잡음 0). τ = 1이면 locker-1의 확률이 0으로 내려간다.
    standing = make_run(MODEL_A, "speaker-1", run_id="va-speaker", seconds=20, start=T0, seed=77, noise_sigma=0.0)
    mislabeled = replace(standing, run_id="va-mislabeled", label="locker-1")
    return run_fit(train=train, validation=[mislabeled], config=replace(BASE_CONFIG, taus=(1.0, 1000.0)))


def shuffled(run: LabeledRun, rng: random.Random) -> LabeledRun:
    """회차의 프레임 순서를 뒤섞는다."""
    frames = list(run.frames)
    rng.shuffle(frames)
    return replace(run, frames=tuple(frames))


def container_types(value: Any) -> set[type]:
    """value 안에 있는 컨테이너(매핑, list, tuple)의 종류를 모은다."""
    kinds: set[type] = set()
    if isinstance(value, (dict, list, tuple)):
        kinds.add(type(value))
        for child in value.values() if isinstance(value, dict) else value:
            kinds |= container_types(child)
    return kinds


def ordered(value: Any) -> Any:
    """dict를 (키, 값) 쌍의 list로 바꿔서 `==`가 키 순서까지 비교하게 한다."""
    if isinstance(value, dict):
        return [(key, ordered(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [ordered(item) for item in value]
    return value


# ── 모델 파일 내용 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "tau", "calibration"),
    [
        pytest.param("none", 1.0, {"method": "none"}, id="none"),
        pytest.param("temperature", 2.0, {"method": "temperature", "tau": 2.0}, id="temperature"),
    ],
)
def test_FR22_model_to_dict_follows_file_format(method, tau, calibration):
    model = replace(HAND_MODEL, calibration_method=method, calibration_tau=tau)
    # 전제: 모델의 컨테이너는 읽기 전용 매핑과 tuple이라서 dict와 list가 아니다
    assert container_types(model.validation).isdisjoint({dict, list})
    assert list(model.nodes) == ["b", "a"]

    result = model_to_dict(model)

    # 키 순서까지 같다. 기기는 node_id 정렬 순이고, none_points는 모델의 순서다
    assert ordered(result) == ordered({**HAND_DICT, "calibration": calibration})
    # 파일에 쓸 값은 dict와 list뿐이다. 매핑은 dict로, tuple은 list로 되돌렸다
    assert container_types(result) == {dict, list}


def test_FR22_model_file_text_is_fixed(tmp_path):
    path = write_model_file(tmp_path, HAND_MODEL)

    # 바이트로 비교한다. UTF-8 한글, 줄바꿈 \n, 끝의 줄바꿈 하나까지 고정이다
    assert path == tmp_path / "place-x" / "v-1.json"
    assert path.read_bytes() == HAND_TEXT.encode("utf-8")
    assert load_model(path) == HAND_MODEL


@pytest.mark.parametrize(
    ("method", "tau", "expected"),
    [
        pytest.param("platt", 1.0, "calibration_method", id="unknown_method"),
        pytest.param("", 1.0, "calibration_method", id="empty_method"),
        pytest.param("TEMPERATURE", 2.0, "calibration_method", id="method_case"),
        pytest.param("none", 2.0, "calibration_tau", id="none_with_tau"),
        pytest.param("temperature", 0.0, "calibration_tau", id="tau_zero"),
        pytest.param("temperature", -1.0, "calibration_tau", id="tau_negative"),
        pytest.param("temperature", math.inf, "calibration_tau", id="tau_inf"),
        pytest.param("temperature", math.nan, "calibration_tau", id="tau_nan"),
    ],
)
def test_FR16_model_rejects_inconsistent_calibration(method, tau, expected):
    # 계산은 τ만 읽고 모델 파일은 방법을 쓴다. 어긋난 모델은 만들 수 없어서 쓴 파일과 계산이 다를 수 없다
    with pytest.raises(ValueError, match=expected):
        replace(MODEL_A, calibration_method=method, calibration_tau=tau)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"model_version": name}, id=f"model_version_{i}") for i, name in enumerate(["", ".", "..", "sub/v1", "../../escaped", "a\\b"])
    ]
    + [pytest.param({"place_id": name}, id=f"place_id_{i}") for i, name in enumerate(["", ".", "..", "x/y", "../escaped", "a\\b"])],
)
def test_FR16_write_rejects_non_component_names(tmp_path, overrides):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match=next(iter(overrides))):
        write_model_file(root, replace(MODEL_A, **overrides))

    assert sorted(tmp_path.rglob("*")) == [root]  # 루트 안에도 밖에도 아무것도 만들지 않는다


# ── 쓴 파일 읽기 ─────────────────────────────────────────────────────


def finite_loss_model() -> ProximityModel:
    return small_fit().model


def infinite_loss_model() -> ProximityModel:
    return infinite_loss_fit().model


def fixture_model() -> ProximityModel:
    return replace(MODEL_A, model_version="syn-a-2")  # 합성 픽스처 모델. 보정은 none이다


@pytest.mark.parametrize(
    ("build", "method", "null_losses"),
    [
        pytest.param(finite_loss_model, "temperature", 0, id="fit_finite_losses"),
        pytest.param(infinite_loss_model, "temperature", 1, id="fit_infinite_loss"),
        pytest.param(fixture_model, "none", 0, id="fixture_calibration_none"),
    ],
)
def test_FR22_written_model_file_reloads_same(tmp_path, build, method, null_losses):
    model = build()
    # 전제: 보정 방법과 null 손실(무한대)의 수
    assert model.calibration_method == method
    assert sum(entry["loss"] is None for entry in model.validation.get("grid", ())) == null_losses

    path = write_model_file(tmp_path, model)

    assert path == tmp_path / "place-a" / f"{model.model_version}.json"
    assert load_model(path) == model
    # 읽은 모델을 다시 쓰면 같은 바이트가 나온다. 정수와 실수, 순서가 읽는 동안 바뀌지 않았다는 뜻이다
    assert write_model_file(tmp_path / "again", load_model(path)).read_bytes() == path.read_bytes()


def test_FR22_fit_is_deterministic(tmp_path):
    first = small_fit()
    second = small_fit()

    assert first == second
    first_path = write_model_file(tmp_path / "first", first.model)
    second_path = write_model_file(tmp_path / "second", second.model)
    assert first_path.read_bytes() == second_path.read_bytes()

    # 회차 안의 프레임 순서를 뒤섞어도 같은 결과와 같은 파일이다. 쓰지 않는 프레임과 같은 프레임이 두 번 온 회차를 뒤섞는다.
    # 중복은 정렬 순서로 처음 하나만 쓰므로, 순서를 정하는 규칙이 입력 순서에 기대면 어느 쪽이 남는지가 흔들린다.
    rng = random.Random(0)
    train = [with_foreign_frames(run) for run in TRAIN_RUNS]
    validation = [with_foreign_frames(run) for run in VALIDATION_RUNS[:1]]
    mixed_train = [shuffled(run, rng) for run in train]
    mixed_validation = [shuffled(run, rng) for run in validation]
    assert all(mixed.frames != run.frames for mixed, run in zip(mixed_train + mixed_validation, train + validation))
    reordered = small_fit(train=mixed_train, validation=mixed_validation)

    assert reordered == first  # 깨끗한 입력의 결과와도 같다
    reordered_path = write_model_file(tmp_path / "reordered", reordered.model)
    assert reordered_path.read_bytes() == first_path.read_bytes()


# ── 쓰기 ─────────────────────────────────────────────────────────────


def test_FR16_write_refuses_overwrite(tmp_path):
    model = small_fit().model
    path = write_model_file(tmp_path, model)
    original = path.read_bytes()

    with pytest.raises(FileExistsError):
        write_model_file(tmp_path, model)  # 같은 모델을 한 번 더
    assert path.read_bytes() == original

    other = replace(model, calibration_tau=8.0)  # 같은 버전, 다른 τ
    assert other.calibration_tau != model.calibration_tau
    assert model_to_dict(other) != model_to_dict(model)
    with pytest.raises(FileExistsError):
        write_model_file(tmp_path, other)
    assert path.read_bytes() == original
    assert load_model(path) == model
    assert list(path.parent.iterdir()) == [path]  # 임시 파일이나 백업을 남기지 않는다


def test_FR16_write_places_file_by_place_and_version(tmp_path):
    root = tmp_path / "models" / "current"  # 없는 디렉터리 둘 아래
    models = [
        replace(MODEL_A, model_version="syn-a-2"),
        replace(MODEL_A, model_version="syn-a-3"),  # 같은 장소의 다른 버전은 이미 있는 디렉터리에 쓴다
        replace(MODEL_B, model_version="syn-b-2"),
    ]

    paths = [write_model_file(root, model) for model in models]

    assert paths == [
        root / "place-a" / "syn-a-2.json",
        root / "place-a" / "syn-a-3.json",
        root / "place-b" / "syn-b-2.json",
    ]
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
        "place-a/syn-a-2.json",
        "place-a/syn-a-3.json",
        "place-b/syn-b-2.json",
    ]
    assert [load_model(path) for path in paths] == models


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"validation": {"loss": math.inf}}, id="validation_inf"),
        pytest.param({"validation": {"loss": math.nan}}, id="validation_nan"),
        pytest.param({"validation": {"grid": ({"tau": 1.0, "loss": -math.inf},)}}, id="validation_nested_negative_inf"),
        pytest.param({"window_sec": math.inf}, id="window_sec_inf"),
        pytest.param({"stay": math.nan}, id="stay_nan"),
        pytest.param({"none_points": ((math.nan, 0.0),)}, id="none_point_nan"),
        pytest.param(
            {"nodes": {**MODEL_A.nodes, "locker-1": replace(MODEL_A.nodes["locker-1"], sigma=math.inf)}},
            id="node_sigma_inf",
        ),
    ],
)
def test_FR16_write_rejects_non_finite_values(tmp_path, overrides):
    model = replace(MODEL_A, model_version="syn-a-2", **overrides)

    with pytest.raises(ValueError):
        write_model_file(tmp_path, model)

    assert list(tmp_path.iterdir()) == []  # 직렬화가 먼저라서 디렉터리도 파일도 만들지 않는다
