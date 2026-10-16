"""모델 파일 읽기와 검증 (FR-16). 합성 모델 파일과 임시 파일로 확인한다."""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from nearpilot.core.proximity import (
    ModelFileError,
    ModelMismatchError,
    NodeModel,
    ProximityModel,
    load_model,
    load_models,
)
from nearpilot.shared.enums import Capability, DeviceState, Occupancy, TrustState

from .conftest import (
    BEACON,
    MODEL_ROOT,
    OTHER_BEACON,
    PLACE_A_NODES,
    T0,
    make_frame,
    make_node,
    place_a_nodes,
)

PLACE_A_FILE = MODEL_ROOT / "place-a" / "syn-a-1.json"
PLACE_B_FILE = MODEL_ROOT / "place-b" / "syn-b-1.json"
PLACE_A_TEXT = PLACE_A_FILE.read_text(encoding="utf-8")
SYNTHETIC_VALUES = (-59.0, 2.0, 4.0, -90.0, 6.0)  # A, n, sigma, none_rssi, none_sigma

_DROP = object()


def _write(tmp_path: Path, *, content: str | bytes, place: str = "place-a", version: str = "syn-a-1") -> Path:
    """`tmp_path/<place>/<version>.json`을 쓴다."""
    path = tmp_path / place / f"{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _edit(dotted: str, value: Any) -> Callable[[Path], Path]:
    """합성 place-a 모델의 `dotted` 위치(예: "nodes.locker-1.sigma")를 value로 바꾼 파일을 만드는 함수.

    value가 `_DROP`이면 그 키를 지운다. 없는 키를 가리키면 키를 더한다.
    """
    raw = json.loads(PLACE_A_TEXT)
    *parents, last = dotted.split(".")
    target = raw
    for key in parents:
        target = target[key]
    if value is _DROP:
        del target[last]
    else:
        target[last] = value
    return partial(_write, content=json.dumps(raw))


def _temperature(tau: Any) -> Callable[[Path], Path]:
    """합성 place-a 모델의 calibration을 `{"method": "temperature", "tau": tau}`로 바꾼 파일을 만드는 함수."""
    return _edit("calibration", {"method": "temperature", "tau": tau})


def test_FR16_load_fixture_models():
    model_a = load_model(PLACE_A_FILE)
    model_b = load_model(PLACE_B_FILE)

    assert (model_a.place_id, model_a.model_version, model_a.calib_version) == ("place-a", "syn-a-1", "none-1")
    assert (model_a.window_sec, model_a.stay, model_a.min_distance_m) == (10.0, 0.9, 0.3)
    assert model_a.calibration_method == "none"
    assert tuple(model_a.nodes) == PLACE_A_NODES
    for x, node_id in enumerate(PLACE_A_NODES):
        assert model_a.nodes[node_id] == NodeModel(node_id, float(x), 0.0, float(x), 0.5, *SYNTHETIC_VALUES)
    assert model_a.validation == {}

    assert (model_b.place_id, model_b.model_version, model_b.calib_version) == ("place-b", "syn-b-1", "none-1")
    assert (model_b.window_sec, model_b.stay, model_b.min_distance_m) == (10.0, 0.9, 0.3)
    assert model_b.calibration_method == "none"
    assert dict(model_b.nodes) == {"mood-1": NodeModel("mood-1", 0.0, 0.0, 0.0, 0.5, *SYNTHETIC_VALUES)}
    assert model_b.validation == {}

    for model in (model_a, model_b):
        with pytest.raises(FrozenInstanceError):
            model.window_sec = 1.0
        with pytest.raises(FrozenInstanceError):
            next(iter(model.nodes.values())).sigma = 1.0
        with pytest.raises(TypeError):
            operator.setitem(model.nodes, "extra", None)
        with pytest.raises(TypeError):
            operator.setitem(model.validation, "extra", None)


def test_FR16_load_models_by_version(model_root):
    models = load_models(model_root, {"place-a": "syn-a-1", "place-b": "syn-b-1"})

    assert set(models) == {"place-a", "place-b"}
    assert models["place-a"] == load_model(PLACE_A_FILE)
    assert models["place-b"] == load_model(PLACE_B_FILE)
    assert (models["place-b"].place_id, models["place-b"].model_version) == ("place-b", "syn-b-1")
    assert set(load_models(model_root, {"place-b": "syn-b-1"})) == {"place-b"}  # 요청한 장소만 읽는다


@pytest.mark.parametrize(
    ("versions", "missing_place"),
    [
        pytest.param({"place-a": "syn-a-9"}, "place-a", id="missing_version"),
        pytest.param({"place-z": "syn-a-1"}, "place-z", id="missing_place"),
        pytest.param({"place-a": "syn-a-1", "place-b": "syn-b-9"}, "place-b", id="one_of_two_missing"),
    ],
)
def test_FR16_missing_place_model_raises(model_root, versions, missing_place):
    with pytest.raises(ModelMismatchError, match=re.escape(missing_place)):
        load_models(model_root, versions)


def test_FR16_model_errors_keep_interface_bases():
    assert issubclass(ModelFileError, ValueError)
    assert issubclass(ModelMismatchError, LookupError)
    assert not issubclass(ModelFileError, LookupError)
    assert not issubclass(ModelMismatchError, ValueError)


# (파일을 만드는 함수, 오류 메시지에 들어 있어야 하는 말). 말이 같이 맞아야 엉뚱한 이유로 통과하지 않는다.
INVALID_FILES = [
    # 형식
    pytest.param(partial(_write, content="{not json"), "JSON", id="not_json"),
    pytest.param(partial(_write, content=b"\xff\xfe{}"), "JSON", id="not_utf8"),
    pytest.param(partial(_write, content="[]"), "JSON 객체", id="top_level_not_object"),
    pytest.param(_edit("window_sec", _DROP), "window_sec", id="missing_key"),
    pytest.param(_edit("nodes", _DROP), "nodes", id="missing_nodes"),
    pytest.param(_edit("transition.stay", _DROP), "stay", id="missing_transition_key"),
    pytest.param(_edit("extra", 1), "extra", id="unknown_key"),
    pytest.param(_edit("window_secs", 10.0), "window_secs", id="typo_key_added"),
    pytest.param(_edit("nodes.locker-1.sigmaa", 4.0), "sigmaa", id="unknown_node_key"),
    pytest.param(_edit("nodes.locker-1.node_id", "locker-1"), "node_id", id="node_id_in_node_entry"),
    pytest.param(_edit("nodes.locker-1.sigma", _DROP), "locker-1", id="missing_node_key"),
    pytest.param(_edit("transition.leave", 0.1), "leave", id="unknown_transition_key"),
    pytest.param(_edit("calibration.params", {}), "params", id="unknown_calibration_key"),
    pytest.param(_edit("calibration", {"method": "none", "tau": 2.0}), "모르는 키 ['tau']", id="none_with_tau"),
    pytest.param(_edit("calibration", {"method": "temperature"}), "빠진 키 ['tau']", id="temperature_missing_tau"),
    pytest.param(
        _edit("calibration", {"method": "temperature", "tau": 2.0, "params": {}}),
        "params",
        id="temperature_unknown_key",
    ),
    pytest.param(_edit("nodes", []), "nodes", id="nodes_not_object"),
    pytest.param(_edit("nodes.locker-1", 1), "locker-1", id="node_entry_not_object"),
    pytest.param(_edit("validation", []), "validation", id="validation_not_object"),
    pytest.param(_edit("none_points", _DROP), "none_points", id="missing_none_points"),
    pytest.param(_edit("none_points", {}), "none_points", id="none_points_not_array"),
    pytest.param(_edit("none_points", [1]), "none_points[0]", id="none_point_not_object"),
    pytest.param(_edit("none_points", [{"x": 1.0}]), "none_points[0]", id="none_point_missing_y"),
    pytest.param(_edit("none_points", [{"x": 1.0, "y": 2.0, "z": 0.0}]), "none_points[0]", id="none_point_unknown_key"),
    pytest.param(_edit("none_points", [{"x": float("nan"), "y": 2.0}]), "none_points[0].x", id="none_point_nan"),
    pytest.param(_edit("none_points", [{"x": 1.0, "y": True}]), "none_points[0].y", id="none_point_bool"),
    # 경로
    pytest.param(partial(_write, content=PLACE_A_TEXT, version="syn-a-2"), "model_version", id="path_version_mismatch"),
    pytest.param(partial(_write, content=PLACE_A_TEXT, place="place-b"), "place_id", id="path_place_mismatch"),
    # 값
    pytest.param(_edit("transition.stay", 0), "transition.stay", id="stay_zero"),
    pytest.param(_edit("transition.stay", 1), "transition.stay", id="stay_one"),
    pytest.param(_edit("window_sec", 0), "window_sec", id="window_sec_zero"),
    pytest.param(_edit("min_distance_m", 0), "min_distance_m", id="min_distance_zero"),
    pytest.param(_edit("nodes.locker-1.sigma", 0), "locker-1.sigma", id="sigma_zero"),
    pytest.param(_edit("nodes.locker-1.none_sigma", -1.0), "locker-1.none_sigma", id="none_sigma_negative"),
    pytest.param(_edit("nodes.locker-1.n", 0), "locker-1.n", id="n_zero"),
    pytest.param(_edit("nodes.locker-1.A", float("nan")), "locker-1.A", id="nan"),
    pytest.param(_edit("nodes.locker-1.none_rssi", float("inf")), "locker-1.none_rssi", id="infinity"),
    pytest.param(_edit("window_sec", 10**400), "window_sec", id="int_too_large_for_float"),
    pytest.param(_edit("nodes.locker-1.sigma", True), "locker-1.sigma", id="bool_value"),
    pytest.param(_edit("window_sec", True), "window_sec", id="bool_window_sec"),
    pytest.param(_edit("nodes.locker-1.sigma", "4.0"), "locker-1.sigma", id="string_number"),
    pytest.param(_edit("nodes", {}), "nodes", id="empty_nodes"),
    pytest.param(_edit("calibration.method", "linear"), "calibration.method", id="unsupported_calibration"),
    pytest.param(_edit("calibration.method", "linear"), "temperature", id="unsupported_calibration_lists_accepted"),
    pytest.param(_temperature(0), "calibration.tau", id="tau_zero"),
    pytest.param(_temperature(-1.0), "calibration.tau", id="tau_negative"),
    pytest.param(_temperature(float("nan")), "calibration.tau", id="tau_nan"),
    pytest.param(_temperature(True), "calibration.tau", id="tau_bool"),
    pytest.param(_temperature("2.0"), "calibration.tau", id="tau_string"),
    pytest.param(_edit("calib_version", ""), "calib_version", id="empty_string"),
    pytest.param(_edit("model_version", 1), "model_version", id="string_field_not_string"),
    pytest.param(_edit("nodes.", json.loads(PLACE_A_TEXT)["nodes"]["locker-1"]), "nodes.", id="empty_node_id"),
]


@pytest.mark.parametrize(("make_file", "expected"), INVALID_FILES)
def test_FR16_invalid_model_file_raises(tmp_path, make_file, expected):
    path = make_file(tmp_path)

    with pytest.raises(ModelFileError, match=re.escape(expected)):
        load_model(path)


def test_FR16_integer_values_become_float_and_validation_is_optional(tmp_path):
    raw = json.loads(PLACE_B_FILE.read_text(encoding="utf-8"))
    raw["window_sec"] = 10
    raw["min_distance_m"] = 1
    raw["nodes"]["mood-1"].update(x=0, y=0, front_x=0, front_y=1, A=-59, n=2, sigma=4, none_rssi=-90, none_sigma=6)
    del raw["validation"]
    path = _write(tmp_path, content=json.dumps(raw), place="place-b", version="syn-b-1")

    model = load_model(path)

    node = model.nodes["mood-1"]
    numbers = [
        model.window_sec, model.stay, model.min_distance_m,
        node.x, node.y, node.front_x, node.front_y, node.A, node.n, node.sigma, node.none_rssi, node.none_sigma,
    ]
    assert all(type(number) is float for number in numbers)
    assert (model.window_sec, model.min_distance_m) == (10.0, 1.0)
    assert (node.front_y, node.A, node.none_sigma) == (1.0, -59.0, 6.0)
    assert model.validation == {}
    with pytest.raises(TypeError):
        operator.setitem(model.validation, "extra", None)


_MUTATIONS = {
    "setitem": lambda mapping: operator.setitem(mapping, "k", 1),
    "delitem": lambda mapping: operator.delitem(mapping, "k"),
    "clear": lambda mapping: mapping.clear(),
    "pop": lambda mapping: mapping.pop("k"),
    "popitem": lambda mapping: mapping.popitem(),
    "setdefault": lambda mapping: mapping.setdefault("k", 1),
    "update": lambda mapping: mapping.update({"k": 1}),
    "ior": lambda mapping: operator.ior(mapping, {"k": 1}),
}


@pytest.mark.parametrize("mutate", [pytest.param(fn, id=name) for name, fn in _MUTATIONS.items()])
def test_FR16_model_mappings_are_deeply_read_only(tmp_path, mutate):
    raw = json.loads(PLACE_B_FILE.read_text(encoding="utf-8"))
    raw["validation"] = {"thresholds": {"light": 0.5}, "runs": [{"id": 1}]}
    model = load_model(_write(tmp_path, content=json.dumps(raw), place="place-b", version="syn-b-1"))

    for mapping in (model.nodes, model.validation, model.validation["thresholds"], model.validation["runs"][0]):
        with pytest.raises(TypeError):
            mutate(mapping)

    assert isinstance(model.validation["runs"], tuple)
    assert model.validation == {"thresholds": {"light": 0.5}, "runs": ({"id": 1},)}
    assert tuple(model.nodes) == ("mood-1",)


def test_FR16_model_copies_input_mappings():
    node = NodeModel("mood-1", 0.0, 0.0, 0.0, 0.5, *SYNTHETIC_VALUES)
    nodes = {"mood-1": node}
    validation = {"thresholds": {"light": 0.5}}

    model = ProximityModel("place-b", "syn-b-1", "none-1", 10.0, 0.9, 0.3, "none", nodes, validation)
    nodes["extra"] = node
    validation["thresholds"]["light"] = 0.9

    assert tuple(model.nodes) == ("mood-1",)
    assert model.validation == {"thresholds": {"light": 0.5}}
    with pytest.raises(TypeError):
        operator.setitem(model.validation["thresholds"], "light", 0.1)


def test_FR16_model_supports_deepcopy_and_json():
    model = load_model(PLACE_A_FILE)

    copied = deepcopy(model)
    assert copied == model
    with pytest.raises(TypeError):
        operator.setitem(copied.nodes, "extra", None)

    dumped = json.loads(json.dumps(asdict(model)))
    assert dumped["calibration_method"] == "none"
    assert dumped["nodes"]["speaker-1"]["x"] == 5.0
    assert dumped["validation"] == {}


def test_FR16_helpers_match_fixture_models():
    assert tuple(load_model(PLACE_A_FILE).nodes) == PLACE_A_NODES
    assert MODEL_ROOT.is_absolute() and (MODEL_ROOT / "place-a" / "syn-a-1.json").is_file()
    assert (T0.year, T0.month, T0.day, T0.hour, T0.tzinfo.utcoffset(T0)) == (2026, 10, 15, 12, timedelta(0))
    assert (BEACON.uuid, BEACON.major, BEACON.minor) == ("11111111-2222-3333-4444-555555555555", 1, 1)
    assert (OTHER_BEACON.uuid, OTHER_BEACON.major, OTHER_BEACON.minor) == (BEACON.uuid, 1, 2)

    nodes = place_a_nodes()
    assert [node.node_id for node in nodes] == list(PLACE_A_NODES)
    assert [node.capability for node in nodes] == (
        [Capability.LOCKER] * 3 + [Capability.LIGHT] * 2 + [Capability.SPEAKER]
    )
    assert [node.occupancy for node in nodes] == [Occupancy.RENTAL] * 3 + [Occupancy.SHARED] * 3
    assert {node.place_id for node in nodes} == {"place-a"}
    assert {node.trust_state for node in nodes} == {TrustState.TRUSTED}
    assert {node.device_state for node in nodes} == {DeviceState.AVAILABLE}
    assert {node.exclusive_group for node in nodes} == {None}
    assert {node.last_seen_at for node in nodes} == {T0}

    revoked = place_a_nodes(**{"locker-2": TrustState.REVOKED})
    assert [node.trust_state for node in revoked] == [
        TrustState.TRUSTED, TrustState.REVOKED, TrustState.TRUSTED,
        TrustState.TRUSTED, TrustState.TRUSTED, TrustState.TRUSTED,
    ]
    with pytest.raises(ValueError):
        place_a_nodes(**{"locker-9": TrustState.REVOKED})

    mood = make_node("mood-1", "place-b")
    assert (mood.place_id, mood.capability, mood.occupancy) == ("place-b", Capability.LIGHT, Occupancy.SHARED)
    custom = make_node(
        "locker-1", trust=TrustState.DISCOVERED, capability=Capability.LIGHT, device_state=DeviceState.FAULT,
    )
    assert (custom.trust_state, custom.capability, custom.occupancy, custom.device_state) == (
        TrustState.DISCOVERED, Capability.LIGHT, Occupancy.SHARED, DeviceState.FAULT,
    )

    frame = make_frame("locker-1", -60.0, T0)
    assert (frame.node_id, frame.rssi, frame.frame_ts, frame.beacon, frame.seq) == ("locker-1", -60.0, T0, BEACON, 1)
    assert (frame.session_id, frame.sent_at, frame.received_at) == ("s-locker-1", T0, T0 + timedelta(milliseconds=50))
    late = T0 + timedelta(seconds=1)
    custom_frame = make_frame("light-1", -70.0, T0, beacon=OTHER_BEACON, seq=7, session_id="sess", received_at=late)
    assert (custom_frame.beacon, custom_frame.seq, custom_frame.session_id) == (OTHER_BEACON, 7, "sess")
    assert (custom_frame.sent_at, custom_frame.received_at) == (T0, late)


def test_FR16_none_points_are_read_as_float_pairs(tmp_path):
    model_a = load_model(PLACE_A_FILE)
    assert len(model_a.none_points) == 20
    assert (2.0, 2.5) in model_a.none_points
    assert all(isinstance(v, float) for point in model_a.none_points for v in point)
    raw = json.loads(PLACE_A_TEXT)
    raw["none_points"] = [{"x": 1, "y": 2}]
    assert load_model(_write(tmp_path, content=json.dumps(raw))).none_points == ((1.0, 2.0),)
    raw["none_points"] = []
    assert load_model(_write(tmp_path, content=json.dumps(raw))).none_points == ()


def test_FR16_temperature_model_file_is_read(tmp_path):
    fixture = load_model(PLACE_A_FILE)
    assert (fixture.calibration_method, fixture.calibration_tau) == ("none", 1.0)  # none의 τ는 1.0이다
    assert type(fixture.calibration_tau) is float

    for written, expected in ((2.5, 2.5), (2, 2.0)):  # 정수 tau도 float로 읽는다
        model = load_model(_temperature(written)(tmp_path))

        assert model.calibration_method == "temperature"
        assert model.calibration_tau == expected
        assert type(model.calibration_tau) is float
