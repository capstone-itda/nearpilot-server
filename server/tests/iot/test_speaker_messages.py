"""FR-04 스피커 announce.controls 해석과 FR-06·FR-09·FR-22 스피커 cmd.parameters 인코딩."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from nearpilot.iot.messages import MessageValidationError, decode_message, encode_command
from nearpilot.shared.descriptor import legacy_controls, legacy_descriptor
from nearpilot.shared.enums import Action, Capability, Occupancy
from nearpilot.shared.models import LightSetting, NodeCommand, SpeakerControls, SpeakerSetting

SESSION = "12345678-1234-1234-1234-123456789abc"
STAMP = "2026-10-27T03:00:00.123Z"
SENT = datetime.fromisoformat(STAMP)
RECEIVED = SENT + timedelta(seconds=1)


def announce(capability="speaker", **extra):
    data = dict(schema_version=2, node_id="speaker-1", session_id=SESSION, seq=1, sent_at=STAMP,
                capability=capability, occupancy="shared") | extra
    return decode_message("nearpilot/node/speaker-1/announce", json.dumps(data).encode(), received_at=RECEIVED)


def test_FR04_announce_speaker_controls_to_shared_model():
    report = announce(controls={"volume_max": 20})
    assert report.descriptor == legacy_descriptor(Capability.SPEAKER, Occupancy.SHARED, volume_max=20)
    assert legacy_controls(report.descriptor) == (None, 20)
    for volume_max in (1, 30):  # 범위의 양 끝도 받는다
        assert legacy_controls(announce(controls={"volume_max": volume_max}).descriptor) == (None, volume_max)
    assert legacy_controls(announce().descriptor) == (None, None)  # 이전 펌웨어의 발표는 그대로 받는다
    light = announce("light", controls={"brightness_max": 64, "color": True})
    assert legacy_controls(light.descriptor)[1] is None


@pytest.mark.parametrize("capability, controls", [
    ("speaker", {"volume_max": 0}), ("speaker", {"volume_max": 31}),
    ("speaker", {"volume_max": 20.0}), ("speaker", {"volume_max": True}),
    ("speaker", {"volume_max": "20"}), ("speaker", {}),
    ("speaker", {"volume_max": 20, "extra": 1}), ("speaker", {"brightness_max": 20, "color": True}),
    ("speaker", [20]), ("speaker", None), ("speaker", 20),
    ("light", {"volume_max": 20}), ("locker", {"volume_max": 20}),
])
def test_FR04_announce_rejects_bad_speaker_controls(capability, controls):
    with pytest.raises(MessageValidationError):
        announce(capability, controls=controls)


def command(action=Action.ON, light=None, speaker=None):
    return NodeCommand("command-1", "speaker-1", action, SESSION, SENT, RECEIVED, light=light, speaker=speaker)


def test_FR06_encode_command_speaker_parameters():
    data = json.loads(encode_command(command(speaker=SpeakerSetting(12)), schema_version=2))
    assert data["parameters"] == {"volume": 12}
    assert data["action"] == "on"
    zero = json.loads(encode_command(command(speaker=SpeakerSetting(0)), schema_version=2))
    assert zero["parameters"] == {"volume": 0} and zero["action"] == "on"


@pytest.mark.parametrize("cmd", [
    command(speaker=SpeakerSetting(None)),
    command(Action.OFF, speaker=SpeakerSetting(12)), command(Action.OPEN, speaker=SpeakerSetting(12)),
    command(light=LightSetting(30, "#0000FF"), speaker=SpeakerSetting(12)),
])
def test_FR09_encode_command_rejects_bad_speaker(cmd):
    # 값 없는 설정, on 이 아닌 명령의 인자, 두 기능의 인자를 함께 담은 명령은 보내지 않는다.
    with pytest.raises(MessageValidationError):
        encode_command(cmd, schema_version=2)


def test_FR22_same_command_same_speaker_parameters():
    a = encode_command(command(speaker=SpeakerSetting(12)), schema_version=2)
    assert a == encode_command(replace(command(), speaker=SpeakerSetting(12)), schema_version=2)
    # 내용이 다르면 바이트도 다르다. parameters 가 없는 명령과도 다르다.
    assert a != encode_command(command(speaker=SpeakerSetting(13)), schema_version=2)
    assert a != encode_command(command(), schema_version=2)
