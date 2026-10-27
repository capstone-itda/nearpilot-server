"""FR-04 announce.controls 해석과 FR-06·FR-09 cmd.parameters 인코딩."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from nearpilot.iot.messages import MessageValidationError, decode_message, encode_command
from nearpilot.shared.enums import Action, Capability
from nearpilot.shared.models import LightControls, LightSetting, NodeCommand

SESSION = "12345678-1234-1234-1234-123456789abc"
STAMP = "2026-10-27T03:00:00.123Z"
SENT = datetime.fromisoformat(STAMP)
RECEIVED = SENT + timedelta(seconds=1)


def announce(capability="light", **extra):
    data = dict(schema_version=2, node_id="light-1", session_id=SESSION, seq=1, sent_at=STAMP,
                capability=capability, occupancy="shared") | extra
    return decode_message("nearpilot/node/light-1/announce", json.dumps(data).encode(), received_at=RECEIVED)


def test_FR04_announce_controls_to_shared_model():
    report = announce(controls={"brightness_max": 64, "color": True})
    assert report.capability is Capability.LIGHT
    assert report.light_controls == LightControls(brightness_max=64, color=True)
    assert announce().light_controls is None  # 이전 펌웨어의 발표는 그대로 받는다


@pytest.mark.parametrize("controls", [
    {"brightness_max": 0, "color": True}, {"brightness_max": 256, "color": True},
    {"brightness_max": 64.0, "color": True}, {"brightness_max": True, "color": True},
    {"brightness_max": "64", "color": True}, {"brightness_max": 64, "color": 1},
    {"brightness_max": 64}, {"color": True}, {"brightness_max": 64, "color": True, "extra": 1},
    [64, True], None, 64,
])
def test_FR04_announce_rejects_bad_controls(controls):
    with pytest.raises(MessageValidationError):
        announce(controls=controls)


@pytest.mark.parametrize("capability", ["locker", "speaker"])
def test_FR04_announce_rejects_controls_on_non_light(capability):
    with pytest.raises(MessageValidationError):
        announce(capability, controls={"brightness_max": 64, "color": True})


def command(action=Action.ON, light=None):
    return NodeCommand("command-1", "light-1", action, SESSION, SENT, RECEIVED, light=light)


def test_FR06_encode_command_parameters():
    data = json.loads(encode_command(command(light=LightSetting(30, "#0000ff"))))
    assert data["parameters"] == {"brightness": 30, "color": "#0000FF"}
    assert data["action"] == "on"
    zero = json.loads(encode_command(command(light=LightSetting(0, "#FFFFFF"))))
    assert zero["parameters"]["brightness"] == 0 and zero["action"] == "on"


def test_FR06_encode_command_without_light_unchanged():
    assert "parameters" not in json.loads(encode_command(command()))


@pytest.mark.parametrize("cmd", [
    command(light=LightSetting(30, None)), command(light=LightSetting(None, "#0000FF")),
    command(Action.OFF, LightSetting(30, "#0000FF")), command(Action.OPEN, LightSetting(30, "#0000FF")),
])
def test_FR09_encode_command_rejects_partial_or_non_on(cmd):
    # 일부 인자만 보내거나 on 이 아닌 명령에 인자를 붙이지 않는다.
    with pytest.raises(MessageValidationError):
        encode_command(cmd)


def test_FR22_same_command_same_parameters():
    a = encode_command(command(light=LightSetting(30, "#0000FF")))
    assert a == encode_command(replace(command(), light=LightSetting(30, "#0000FF")))
