"""FR-04 v2 announce.controls 해석과 FR-06·FR-09 cmd 인자 인코딩 (조명)."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from nearpilot.iot.messages import MessageValidationError, decode_message, encode_command
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Occupancy
from nearpilot.shared.models import NodeCommand

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
    assert report.descriptor == legacy_descriptor("light", Occupancy.SHARED, brightness_max=64, color=True)
    on = report.descriptor.action("on")
    assert (on.properties["brightness"]["maximum"], "color" in on.properties) == (64, True)
    assert announce().descriptor.action("on").properties == {}  # 이전 펌웨어의 발표는 그대로 받는다


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


def command(action="on", arguments=None):
    return NodeCommand("command-1", "light-1", action, SESSION, SENT, RECEIVED, arguments=arguments)


def test_FR06_encode_command_parameters():
    # 판정이 정한 인자를 그대로 보낸다. 대문자 정규화는 판정이 한다 (FR-09).
    data = json.loads(encode_command(command(arguments={"brightness": 30, "color": "#0000FF"}), schema_version=2))
    assert data["parameters"] == {"brightness": 30, "color": "#0000FF"}
    assert data["action"] == "on" and "arguments" not in data
    zero = json.loads(encode_command(command(arguments={"brightness": 0, "color": "#FFFFFF"}), schema_version=2))
    assert zero["parameters"]["brightness"] == 0 and zero["action"] == "on"
    v3 = json.loads(encode_command(command(arguments={"brightness": 30}), schema_version=3))
    assert v3["arguments"] == {"brightness": 30} and "parameters" not in v3


def test_FR06_encode_command_without_light_unchanged():
    assert "parameters" not in json.loads(encode_command(command(), schema_version=2))
    assert "arguments" not in json.loads(encode_command(command(), schema_version=3))


@pytest.mark.parametrize("action", ["", "On", "turn on", "on/1"])
def test_FR06_encode_command_rejects_bad_action_name(action):
    with pytest.raises(MessageValidationError):
        encode_command(command(action), schema_version=3)


def test_FR22_same_command_same_parameters():
    a = encode_command(command(arguments={"brightness": 30, "color": "#0000FF"}), schema_version=2)
    # 키 순서가 달라도 같은 명령은 같은 바이트다. 재시작 뒤 저장소에서 읽은 명령도 같다.
    assert a == encode_command(replace(command(), arguments={"color": "#0000FF", "brightness": 30}),
                               schema_version=2)
