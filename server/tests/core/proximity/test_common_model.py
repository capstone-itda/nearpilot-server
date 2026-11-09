"""공통 근접 모델 파일 읽기와 쓰기 (FR-16). 합성 모델 파일과 임시 파일로 확인한다."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from nearpilot.core.proximity.common_model import (
    CommonModel,
    ModelFileError,
    ModelMismatchError,
    RoleParams,
    common_model_to_dict,
    load_common_model,
    load_model_version,
    write_common_model_file,
)

COMMON_ROOT = Path(__file__).resolve().parent / "fixtures" / "common"
COMMON_FILE = COMMON_ROOT / "syn-common-1.json"
COMMON_TEXT = COMMON_FILE.read_text(encoding="utf-8")
ROLES = ("front", "other", "none")

_DROP = object()


def _write(tmp_path: Path, *, content: str | bytes, version: str = "syn-common-1") -> Path:
    """`tmp_path/<version>.json`을 쓴다."""
    path = tmp_path / f"{version}.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _edit(dotted: str, value: Any) -> Callable[[Path], Path]:
    """합성 공통 모델의 `dotted` 위치(예: "observation.front.sigma")를 value로 바꾼 파일을 만드는 함수.

    value가 `_DROP`이면 그 키를 지운다. 없는 키를 가리키면 키를 더한다.
    """
    raw = json.loads(COMMON_TEXT)
    *parents, last = dotted.split(".")
    target = raw
    for key in parents:
        target = target[key]
    if value is _DROP:
        del target[last]
    else:
        target[last] = value
    return partial(_write, content=json.dumps(raw))


def _model(**changes: Any) -> CommonModel:
    return replace(load_common_model(COMMON_FILE), **changes)


def _assert_plain(value: Any) -> None:
    """JSON으로 쓸 수 있는 plain 값(dict, list, 문자열, 수, bool, None)만 있다. 읽기 전용 매핑과 tuple이 남아 있지 않다."""
    if isinstance(value, dict):
        assert type(value) is dict
        for item in value.values():
            _assert_plain(item)
    elif isinstance(value, list):
        assert type(value) is list
        for item in value:
            _assert_plain(item)
    else:
        assert type(value) in (str, int, float, bool, type(None)), type(value)


def test_FR16_common_model_loads(tmp_path):
    model = load_common_model(COMMON_FILE)

    assert model.model_version == "syn-common-1"
    assert (model.window_sec, model.stay) == (3.0, 0.9)
    assert (model.calibration_method, model.calibration_tau) == ("none", 1.0)
    assert model.front == RoleParams(mean=-55.0, sigma=4.0)
    assert model.other == RoleParams(mean=-72.0, sigma=6.0)
    assert model.none == RoleParams(mean=-80.0, sigma=6.0)
    assert model.auto_zero_min_bins == 20
    assert model.validation == {"source": "SYNTHETIC_NOT_MEASURED", "quality_verified": False}

    # 읽은 모델은 바꿀 수 없다
    with pytest.raises(FrozenInstanceError):
        model.window_sec = 1.0
    with pytest.raises(FrozenInstanceError):
        model.front.sigma = 1.0
    with pytest.raises(TypeError):
        model.validation["quality_verified"] = True
    with pytest.raises(TypeError):
        model.validation.update({"extra": 1})

    # 모델 → dict → 파일 → 모델이 같은 모델을 낸다. 루트가 없으면 만든다.
    written = write_common_model_file(tmp_path / "models" / "common", model)
    assert written == tmp_path / "models" / "common" / "syn-common-1.json"
    assert load_common_model(written) == model
    assert common_model_to_dict(load_common_model(written)) == common_model_to_dict(model)


def test_FR16_common_model_numbers_become_floats(tmp_path):
    raw = json.loads(COMMON_TEXT)
    raw["window_sec"] = 3
    raw["observation"]["front"] = {"mean": -55, "sigma": 4}
    model = load_common_model(_write(tmp_path, content=json.dumps(raw)))

    assert isinstance(model.window_sec, float)
    assert (type(model.front.mean), type(model.front.sigma)) == (float, float)
    assert isinstance(model.auto_zero_min_bins, int)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("place_id", "place-a", id="place_id"),
        pytest.param("nodes", {"locker-1": {"x": 0.0, "y": 0.0}}, id="nodes"),
        pytest.param("none_points", [{"x": 0.0, "y": 1.5}], id="none_points"),
        pytest.param("calib_version", "none-1", id="calib_version"),
        pytest.param("min_distance_m", 0.3, id="min_distance_m"),
    ],
)
def test_FR16_common_model_rejects_layout_keys(tmp_path, key, value):
    path = _edit(key, value)(tmp_path)

    with pytest.raises(ModelFileError, match=rf"모르는 키 .*{key}"):
        load_common_model(path)


@pytest.mark.parametrize(
    "dotted",
    [
        pytest.param("observation.locker-1", id="per_device_entry"),
        pytest.param("observation.front.x", id="coordinate_in_role"),
        pytest.param("observation.front.node_id", id="node_id_in_role"),
        pytest.param("observation.far", id="extra_role"),
        pytest.param("transition.leave", id="extra_transition_key"),
        pytest.param("auto_zero.max_bins", id="extra_auto_zero_key"),
        pytest.param("extra", id="extra_top_key"),
    ],
)
def test_FR16_common_model_rejects_per_anchor_entries(tmp_path, dotted):
    path = _edit(dotted, {"mean": -60.0, "sigma": 5.0})(tmp_path)

    with pytest.raises(ModelFileError, match="모르는 키"):
        load_common_model(path)


# (파일을 만드는 함수, 오류 메시지에 들어 있어야 하는 말). 말이 같이 맞아야 엉뚱한 이유로 통과하지 않는다.
INVALID_FILES = [
    # 형식
    pytest.param(partial(_write, content="{not json"), "JSON", id="not_json"),
    pytest.param(partial(_write, content=b"\xff\xfe{}"), "JSON", id="not_utf8"),
    pytest.param(partial(_write, content="[]"), "JSON 객체", id="top_level_not_object"),
    pytest.param(_edit("observation.front", [-55.0, 4.0]), "observation.front", id="role_not_object"),
    pytest.param(_edit("validation", [1]), "validation", id="validation_not_object"),
    # 빠진 키
    pytest.param(_edit("model_version", _DROP), "빠진 키 ['model_version']", id="missing_model_version"),
    pytest.param(_edit("window_sec", _DROP), "빠진 키 ['window_sec']", id="missing_window_sec"),
    pytest.param(_edit("transition", _DROP), "빠진 키 ['transition']", id="missing_transition"),
    pytest.param(_edit("transition.stay", _DROP), "빠진 키 ['stay']", id="missing_stay"),
    pytest.param(_edit("calibration", _DROP), "빠진 키 ['calibration']", id="missing_calibration"),
    pytest.param(_edit("observation", _DROP), "빠진 키 ['observation']", id="missing_observation"),
    pytest.param(_edit("observation.none", _DROP), "빠진 키 ['none']", id="missing_none_role"),
    pytest.param(_edit("observation.front.sigma", _DROP), "빠진 키 ['sigma']", id="missing_sigma"),
    pytest.param(_edit("observation.other.mean", _DROP), "빠진 키 ['mean']", id="missing_mean"),
    pytest.param(_edit("auto_zero", _DROP), "빠진 키 ['auto_zero']", id="missing_auto_zero"),
    pytest.param(_edit("auto_zero.min_bins", _DROP), "빠진 키 ['min_bins']", id="missing_min_bins"),
    # 이름
    pytest.param(_edit("model_version", "other-name"), "파일 이름", id="model_version_differs_from_file_name"),
    pytest.param(partial(_write, content=COMMON_TEXT, version="other-name"), "파일 이름", id="file_name_differs"),
    pytest.param(_edit("model_version", ""), "model_version", id="empty_model_version"),
    pytest.param(_edit("model_version", 1), "model_version", id="model_version_not_string"),
    # 값의 범위
    *[
        pytest.param(_edit(f"observation.{role}.sigma", value), f"observation.{role}.sigma", id=f"{role}_sigma_{name}")
        for role in ROLES
        for name, value in (("zero", 0), ("negative", -1.0))
    ],
    pytest.param(_edit("observation.front.mean", float("inf")), "observation.front.mean", id="mean_infinite"),
    pytest.param(_edit("observation.other.mean", float("-inf")), "observation.other.mean", id="mean_negative_infinite"),
    pytest.param(_edit("observation.none.mean", "-80"), "observation.none.mean", id="mean_string"),
    pytest.param(_edit("observation.front.mean", True), "observation.front.mean", id="mean_bool"),
    pytest.param(_edit("observation.front.sigma", float("nan")), "observation.front.sigma", id="sigma_nan"),
    pytest.param(_edit("transition.stay", 0), "transition.stay", id="stay_zero"),
    pytest.param(_edit("transition.stay", 1), "transition.stay", id="stay_one"),
    pytest.param(_edit("transition.stay", -0.1), "transition.stay", id="stay_negative"),
    pytest.param(_edit("transition.stay", 1.5), "transition.stay", id="stay_above_one"),
    pytest.param(_edit("window_sec", 0), "window_sec", id="window_zero"),
    pytest.param(_edit("window_sec", -3.0), "window_sec", id="window_negative"),
    pytest.param(_edit("window_sec", float("nan")), "window_sec", id="window_nan"),
    pytest.param(_edit("window_sec", 10**400), "window_sec", id="window_too_large_int"),
    pytest.param(_edit("auto_zero.min_bins", 0), "auto_zero.min_bins", id="min_bins_zero"),
    pytest.param(_edit("auto_zero.min_bins", -5), "auto_zero.min_bins", id="min_bins_negative"),
    pytest.param(_edit("auto_zero.min_bins", 1.5), "auto_zero.min_bins", id="min_bins_fraction"),
    pytest.param(_edit("auto_zero.min_bins", 20.0), "auto_zero.min_bins", id="min_bins_float"),
    pytest.param(_edit("auto_zero.min_bins", True), "auto_zero.min_bins", id="min_bins_true"),
    pytest.param(_edit("auto_zero.min_bins", "20"), "auto_zero.min_bins", id="min_bins_string"),
    # 보정
    pytest.param(_edit("calibration.method", "isotonic"), "isotonic", id="unknown_calibration_method"),
    pytest.param(_edit("calibration", {}), "calibration.method", id="calibration_without_method"),
    pytest.param(_edit("calibration", {"method": "none", "tau": 2.0}), "모르는 키 ['tau']", id="none_with_tau"),
    pytest.param(_edit("calibration", {"method": "temperature"}), "빠진 키 ['tau']", id="temperature_missing_tau"),
    pytest.param(_edit("calibration", {"method": "temperature", "tau": 0}), "calibration.tau", id="temperature_tau_zero"),
    pytest.param(_edit("calibration", {"method": "temperature", "tau": -1.0}), "calibration.tau", id="temperature_tau_negative"),
]


@pytest.mark.parametrize(("make_file", "message"), INVALID_FILES)
def test_FR16_common_model_rejects_bad_values(tmp_path, make_file, message):
    path = make_file(tmp_path)

    with pytest.raises(ModelFileError) as caught:
        load_common_model(path)

    assert message in str(caught.value)


def test_FR16_common_model_missing_file_raises_os_error(tmp_path):
    with pytest.raises(OSError) as caught:
        load_common_model(tmp_path / "syn-common-1.json")

    assert not isinstance(caught.value, ModelFileError)  # 읽기 실패는 형식 오류와 따로 올라간다


def test_FR16_common_model_validation_is_optional(tmp_path):
    raw = json.loads(COMMON_TEXT)
    del raw["validation"]
    model = load_common_model(_write(tmp_path, content=json.dumps(raw)))

    assert model.validation == {}
    assert "validation" not in common_model_to_dict(model)  # 비어 있으면 키를 넣지 않는다
    written = write_common_model_file(tmp_path / "out", model)
    assert "validation" not in json.loads(written.read_text(encoding="utf-8"))
    assert load_common_model(written) == model


def test_FR16_common_model_temperature_round_trip(tmp_path):
    path = _edit("calibration", {"method": "temperature", "tau": 2.5})(tmp_path)
    model = load_common_model(path)

    assert (model.calibration_method, model.calibration_tau) == ("temperature", 2.5)
    assert common_model_to_dict(model)["calibration"] == {"method": "temperature", "tau": 2.5}
    assert load_common_model(write_common_model_file(tmp_path / "out", model)) == model


def test_FR16_common_model_to_dict_format():
    model = _model(validation={"source": "SYNTHETIC_NOT_MEASURED", "grid": ({"tau": 1.0, "loss": None},), "note": "합성"})
    content = common_model_to_dict(model)

    assert list(content) == [
        "model_version", "window_sec", "transition", "calibration", "observation", "auto_zero", "validation",
    ]
    assert content["transition"] == {"stay": 0.9}
    assert content["calibration"] == {"method": "none"}
    assert content["observation"] == {
        "front": {"mean": -55.0, "sigma": 4.0},
        "other": {"mean": -72.0, "sigma": 6.0},
        "none": {"mean": -80.0, "sigma": 6.0},
    }
    assert content["auto_zero"] == {"min_bins": 20}
    assert content["validation"]["grid"] == [{"tau": 1.0, "loss": None}]  # tuple은 list로 되돌린다
    _assert_plain(content)
    json.dumps(content, allow_nan=False)  # JSON으로 쓸 수 있다


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"calibration_tau": 2.0}, id="none_with_tau"),
        pytest.param({"calibration_method": "temperature", "calibration_tau": 0.0}, id="temperature_tau_zero"),
        pytest.param({"calibration_method": "temperature", "calibration_tau": float("inf")}, id="temperature_tau_infinite"),
        pytest.param({"calibration_method": "temperature", "calibration_tau": float("nan")}, id="temperature_tau_nan"),
        pytest.param({"calibration_method": "isotonic"}, id="unknown_method"),
    ],
)
def test_FR16_common_model_rejects_inconsistent_calibration(changes):
    with pytest.raises(ValueError):
        _model(**changes)


def test_FR16_common_model_freezes_nested_validation():
    model = _model(validation={"grid": [{"tau": 1.0}], "source": "SYNTHETIC_NOT_MEASURED"})

    assert model.validation["grid"] == ({"tau": 1.0},)  # list는 tuple로 굳는다
    with pytest.raises(TypeError):
        model.validation["grid"][0]["tau"] = 2.0  # 안쪽 매핑도 바꿀 수 없다
    with pytest.raises(TypeError):
        model.validation.clear()


def test_FR16_load_model_version(tmp_path):
    assert load_model_version(COMMON_ROOT, "syn-common-1") == load_common_model(COMMON_FILE)

    with pytest.raises(ModelMismatchError, match="syn-common-9"):
        load_model_version(COMMON_ROOT, "syn-common-9")
    with pytest.raises(ModelMismatchError):  # 루트가 없어도 오류는 같다
        load_model_version(tmp_path / "missing-root", "syn-common-1")
    (tmp_path / "folder.json").mkdir()
    with pytest.raises(ModelMismatchError):  # 파일이 아니면 없는 것과 같다
        load_model_version(tmp_path, "folder")

    # 파일은 있지만 내용의 model_version이 파일 이름과 다르면 읽기 오류다
    _write(tmp_path, content=COMMON_TEXT, version="other-name")
    with pytest.raises(ModelFileError, match="파일 이름"):
        load_model_version(tmp_path, "other-name")


def test_FR16_write_common_model_file_never_overwrites(tmp_path):
    model = load_common_model(COMMON_FILE)
    path = write_common_model_file(tmp_path, model)
    before = path.read_bytes()

    with pytest.raises(FileExistsError):
        write_common_model_file(tmp_path, replace(model, stay=0.5))

    assert path.read_bytes() == before
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["syn-common-1.json"]  # 임시 파일도 남기지 않는다


def test_FR16_write_common_model_file_format(tmp_path):
    model = _model(validation={"source": "SYNTHETIC_NOT_MEASURED", "note": "합성 값"})
    path = write_common_model_file(tmp_path, model)

    raw = path.read_bytes()
    expected = json.dumps(common_model_to_dict(model), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    assert raw.decode("utf-8") == expected
    assert b"\r" not in raw  # 줄바꿈은 LF다
    assert "합성 값".encode("utf-8") in raw  # 한글을 이스케이프하지 않는다


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "../escape"])
def test_FR16_write_common_model_file_rejects_path_names(tmp_path, name):
    root = tmp_path / "root"

    with pytest.raises(ValueError, match="model_version"):
        write_common_model_file(root, _model(model_version=name))

    assert not root.exists()  # 디렉터리도 파일도 만들지 않는다
    assert not (tmp_path / "escape.json").exists()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"window_sec": float("nan")}, id="window_nan"),
        pytest.param({"stay": float("inf")}, id="stay_infinite"),
        pytest.param({"front": RoleParams(mean=float("-inf"), sigma=4.0)}, id="mean_infinite"),
        pytest.param({"validation": {"loss": float("nan")}}, id="validation_nan"),
    ],
)
def test_FR16_write_common_model_file_rejects_non_finite(tmp_path, changes):
    root = tmp_path / "root"

    with pytest.raises(ValueError):
        write_common_model_file(root, _model(**changes))

    assert not root.exists()  # 직렬화가 먼저라서 디렉터리도 파일도 만들지 않는다


def test_FR16_common_model_error_class_hierarchy():
    # 두 오류는 서로 다른 계통이다. 호출자는 파일 내용 오류(ValueError)와 없는 모델(LookupError)을 따로 잡는다.
    assert issubclass(ModelFileError, ValueError)
    assert issubclass(ModelMismatchError, LookupError)
    assert not issubclass(ModelFileError, LookupError)
    assert not issubclass(ModelMismatchError, ValueError)
