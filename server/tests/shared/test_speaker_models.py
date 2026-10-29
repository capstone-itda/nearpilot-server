"""FR-09 스피커 음량의 shared 모델, 설정, Protocol 인자. 다른 계층은 쓰지 않는다."""

from __future__ import annotations

from dataclasses import asdict, astuple, fields
from inspect import signature

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.interfaces import ExecutionStore, Repository
from nearpilot.shared.models import (
    CommandRecord,
    ExecutionContext,
    NodeAnnounce,
    NodeCommand,
    NodeInfo,
    SpeakerControls,
    SpeakerSetting,
    UseRequest,
)


@pytest.mark.parametrize(
    ("overrides", "ok"),
    [
        pytest.param({}, True, id="defaults_20_20"),
        pytest.param({"speaker_volume_max": 10, "speaker_default_volume": 10}, True, id="cap_10_default_10"),
        pytest.param({"speaker_volume_max": 1, "speaker_default_volume": 0}, True, id="cap_1_default_0"),
        pytest.param({"speaker_volume_max": 30, "speaker_default_volume": 30}, True, id="cap_30_default_30"),
        # 상한만 틀리게 하려고 기본 음량은 0 으로 둔다. 기본 음량이 상한보다 커서 거부되는 경우와 섞지 않는다.
        pytest.param({"speaker_volume_max": 0, "speaker_default_volume": 0}, False, id="cap_0"),
        pytest.param({"speaker_volume_max": 31, "speaker_default_volume": 0}, False, id="cap_31"),
        pytest.param({"speaker_volume_max": True, "speaker_default_volume": 0}, False, id="cap_bool"),
        pytest.param({"speaker_volume_max": "20", "speaker_default_volume": 0}, False, id="cap_str"),
        pytest.param({"speaker_volume_max": 20.0, "speaker_default_volume": 0}, False, id="cap_float"),
        pytest.param({"speaker_default_volume": -1}, False, id="default_negative"),
        pytest.param({"speaker_default_volume": 21}, False, id="default_above_cap_20"),
        pytest.param({"speaker_default_volume": True}, False, id="default_bool"),
    ],
)
def test_FR09_speaker_settings_validate(overrides, ok):
    if not ok:
        with pytest.raises(ValueError):
            Settings(**overrides)
        return
    settings = Settings(**overrides)
    assert settings.speaker_volume_max == overrides.get("speaker_volume_max", 20)
    assert settings.speaker_default_volume == overrides.get("speaker_default_volume", 20)


def test_FR09_speaker_settings_old_snapshot_loads():
    # DB 의 설정 스냅샷은 Settings(**json) 으로 복원한다. 스피커 설정이 없던 때 저장한 스냅샷도 읽어야 한다.
    snapshot = asdict(Settings(version="old"))
    for name in ("speaker_volume_max", "speaker_default_volume"):
        del snapshot[name]
    assert Settings(**snapshot) == Settings(version="old")


@pytest.mark.parametrize(
    ("model", "value", "ok"),
    [
        pytest.param(SpeakerControls, 0, False, id="controls_0"),
        pytest.param(SpeakerControls, 31, False, id="controls_31"),
        pytest.param(SpeakerControls, True, False, id="controls_bool"),
        pytest.param(SpeakerControls, "20", False, id="controls_str"),
        pytest.param(SpeakerControls, 1, True, id="controls_1"),
        pytest.param(SpeakerControls, 30, True, id="controls_30"),
        pytest.param(SpeakerSetting, -1, False, id="setting_negative"),
        pytest.param(SpeakerSetting, 31, False, id="setting_31"),
        pytest.param(SpeakerSetting, True, False, id="setting_bool"),
        pytest.param(SpeakerSetting, 1.0, False, id="setting_float"),
        pytest.param(SpeakerSetting, "20", False, id="setting_str"),
        pytest.param(SpeakerSetting, None, True, id="setting_none"),
        pytest.param(SpeakerSetting, 0, True, id="setting_0"),
        pytest.param(SpeakerSetting, 30, True, id="setting_30"),
    ],
)
def test_FR09_speaker_models_validate(model, value, ok):
    if not ok:
        with pytest.raises(ValueError):
            model(value)
        return
    assert astuple(model(value)) == (value,)


def test_FR09_speaker_setting_complete():
    assert SpeakerSetting().complete is False
    assert SpeakerSetting(20).complete is True
    assert SpeakerSetting(0).complete is True  # 음량 0 도 정해진 값이다. 없는 값이 아니다.


def test_FR09_speaker_setting_json_round_trip():
    for setting, data in (
        (SpeakerSetting(), {}),
        (SpeakerSetting(0), {"volume": 0}),
        (SpeakerSetting(20), {"volume": 20}),
        (SpeakerSetting(30), {"volume": 30}),
    ):
        assert setting.to_json() == data
        assert SpeakerSetting.from_json(data) == setting
    assert SpeakerSetting.from_json(None) is None
    for bad in ({"volume": 1, "brightness": 2}, {"brightness": 2}, ["volume"], 5):
        with pytest.raises(ValueError):
            SpeakerSetting.from_json(bad)


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (NodeInfo, "speaker_controls"),
        (UseRequest, "speaker"),
        (ExecutionContext, "speaker"),
        (CommandRecord, "speaker"),
        (NodeAnnounce, "speaker_controls"),
        (NodeCommand, "speaker"),
    ],
)
def test_FR09_speaker_fields_default_none(model, name):
    # 새 필드는 모두 기본값 None 이어야 기존 호출과 저장된 값이 바뀌지 않는다.
    by_name = {f.name: f for f in fields(model)}
    assert by_name[name].default is None


def test_FR09_speaker_protocol_arguments():
    params = list(signature(ExecutionStore.link_command).parameters.values())
    assert [p.name for p in params[-2:]] == ["light", "speaker"]
    assert params[-1].default is None
    assert list(signature(Repository.last_speaker_setting).parameters) == ["self", "node_id"]
