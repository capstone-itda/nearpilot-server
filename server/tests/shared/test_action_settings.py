"""FR-09·FR-17·FR-23 종류별 설정과 동작 인자의 shared 모델 (specs/core-device-actions/spec.md).

다른 계층은 쓰지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import fields
from inspect import signature

import pytest

from nearpilot.shared.config import Settings, settings_from_data, settings_to_data
from nearpilot.shared.enums import TargetKind
from nearpilot.shared.interfaces import ExecutionStore, PolicyChecker, Repository
from nearpilot.shared.models import (
    CommandRecord,
    ExecutionContext,
    NodeCommand,
    ReserveRequest,
    TargetSpec,
    UseRequest,
)

OLD_KEYS = {"light_brightness_max": 64, "light_default_brightness": 64, "light_default_color": "#FFFFFF",
            "speaker_volume_max": 20, "speaker_default_volume": 20}


def test_FR09_param_settings_defaults_match_old_baseline():
    s = Settings()
    assert s.param_caps("light", "on") == {"brightness": 64}
    assert s.param_caps("speaker", "on") == {"volume": 20}
    assert s.param_defaults("light", "on") == {"brightness": 64, "color": "#FFFFFF"}
    assert s.param_defaults("speaker", "on") == {"volume": 20}
    assert s.param_caps("fan", "spin") == {} and s.param_defaults("locker", "open") == {}
    assert s.any_allowed_kinds == ("locker",)


@pytest.mark.parametrize("overrides", [
    pytest.param({"param_max": {"Light": {"on": {"brightness": 1}}}}, id="kind_name"),
    pytest.param({"param_max": {"light": {"on": {"brightness": "64"}}}}, id="max_str"),
    pytest.param({"param_max": {"light": {"on": {"brightness": True}}}}, id="max_bool"),
    pytest.param({"param_max": {"light": {"on": {"brightness": float("inf")}}}}, id="max_inf"),
    pytest.param({"param_max": {"light": {"on": 64}}}, id="max_shape"),
    pytest.param({"param_default": {"light": {"on": {"color": ["#FFFFFF"]}}}}, id="default_list"),
    pytest.param({"param_default": {"light": {"on": {"brightness": 65}}}}, id="default_above_max"),
    pytest.param({"any_allowed_kinds": ("locker", "locker")}, id="any_duplicate"),
    pytest.param({"any_allowed_kinds": ("Locker",)}, id="any_name"),
    pytest.param({"thresholds": {"fan": 1.5}}, id="threshold_range"),
    pytest.param({"thresholds": {"fan": True}}, id="threshold_bool"),
    pytest.param({"thresholds": {"locker": 0.5}}, id="threshold_field_kind"),
    pytest.param({"thresholds": {"explicit_locker": 0.5}}, id="threshold_not_a_field"),
])
def test_FR09_param_settings_validate(overrides):
    if overrides == {"thresholds": {"explicit_locker": 0.5}}:
        # 필드가 아닌 종류 이름이므로 유효하다. 다른 필드를 종류로 읽지 않는다 (spec 결정 4).
        s = Settings(**overrides)
        assert s.threshold_for("explicit_locker") == 0.5 and s.threshold_explicit_locker is None
        return
    with pytest.raises(ValueError):
        Settings(**overrides)


def test_FR17_threshold_for_kind():
    s = Settings(thresholds={"fan": 0.8, "heater": None})
    assert (s.threshold_for("locker"), s.threshold_for("light"), s.threshold_for("speaker")) == (0.95, 0.75, None)
    assert (s.threshold_for("fan"), s.threshold_for("heater"), s.threshold_for("gate")) == (0.8, None, None)
    assert s.threshold_for(None) is None


def test_FR09_settings_are_frozen():
    s = Settings()
    with pytest.raises(TypeError):
        s.param_max["light"]["on"]["brightness"] = 255
    with pytest.raises(TypeError):
        s.thresholds["fan"] = 0.5


def test_FR22_settings_snapshot_round_trip():
    s = Settings(version="v9", thresholds={"fan": 0.8}, any_allowed_kinds=("locker", "fan"),
                 param_max={"fan": {"spin": {"speed": 3}}}, param_default={"fan": {"spin": {"speed": 1}}})
    data = json.loads(json.dumps(settings_to_data(s)))
    assert settings_from_data(data) == s


def test_FR22_legacy_settings_snapshot_restored():
    # 조명·스피커 필드가 있던 때의 스냅샷 (`asdict(Settings)`). 새 형식으로 옮겨 같은 값을 복원한다.
    data = settings_to_data(Settings(version="old"))
    for name in ("thresholds", "param_max", "param_default", "any_allowed_kinds"):
        del data[name]
    data.update(OLD_KEYS, light_brightness_max=40, light_default_brightness=40, speaker_default_volume=5)
    restored = settings_from_data(data)
    assert restored.param_caps("light", "on") == {"brightness": 40}
    assert restored.param_defaults("speaker", "on") == {"volume": 5}
    assert restored.any_allowed_kinds == ("locker",) and restored.version == "old"
    # 스피커 설정이 생기기 전의 스냅샷도 읽는다. 없는 값은 초기 기준이다.
    for name in ("speaker_volume_max", "speaker_default_volume"):
        del data[name]
    assert settings_from_data(data).param_caps("speaker", "on") == {"volume": 20}


def test_FR09_legacy_keys_and_new_tables_not_mixed():
    with pytest.raises(ValueError):
        settings_from_data({"version": "x", "light_brightness_max": 30, "param_max": {}})


def test_FR09_target_spec_names():
    assert TargetSpec.parse("nearest:fan") == TargetSpec(TargetKind.NEAREST, "fan")
    assert TargetSpec.parse("any:locker") == TargetSpec(TargetKind.ANY, "locker")
    # 콜론이 없으면 기기 ID 그대로다. `{종류}-{번호}` 형식이 아니어도 된다 (spec 결정 2).
    assert TargetSpec.parse("fan_room") == TargetSpec(TargetKind.EXPLICIT, None, "fan_room")
    assert TargetSpec.parse("locker-1").serialize() == "locker-1"
    assert TargetSpec.parse("nearest:light").serialize() == "nearest:light"
    for bad in ("nearest:", "nearest:Fan", "explicit:locker-1", "far:locker", "", " a", 3):
        with pytest.raises(ValueError):
            TargetSpec.parse(bad)


@pytest.mark.parametrize("model", [UseRequest, ExecutionContext, CommandRecord, NodeCommand])
def test_FR09_arguments_fields_default_none(model):
    # 새 필드는 기본값 None 이어야 기존 호출과 저장된 값이 바뀌지 않는다.
    by_name = {f.name: f for f in fields(model)}
    assert by_name["arguments"].default is None
    assert not {"light", "speaker"} & set(by_name)


def test_FR09_arguments_are_frozen_objects():
    command = NodeCommand("c1", "fan-1", "spin", "s1", None, None, arguments={"speed": 2})
    with pytest.raises(TypeError):
        command.arguments["speed"] = 5
    with pytest.raises(ValueError):
        NodeCommand("c1", "fan-1", "spin", "s1", None, None, arguments=[2])
    with pytest.raises(ValueError):
        UseRequest("u1", "r1", TargetSpec.parse("fan-1"), "Spin", None, None)


def test_FR09_protocol_arguments():
    params = list(signature(ExecutionStore.link_command).parameters.values())
    assert [p.name for p in params[-1:]] == ["arguments"] and params[-1].default is None
    assert list(signature(Repository.last_success_arguments).parameters) == ["self", "node_id", "action"]
    assert signature(PolicyChecker.check).parameters["reoperate"].default is False
    assert {f.name: f.default for f in fields(ReserveRequest)}["reoperate_only"] is False
