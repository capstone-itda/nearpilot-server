"""FR-04 스피커 v2 announce.controls 해석과 FR-06·FR-09·FR-22 스피커 cmd 인자 인코딩."""

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


def announce(capability="speaker", **extra):
    data = dict(schema_version=2, node_id="speaker-1", session_id=SESSION, seq=1, sent_at=STAMP,
                capability=capability, occupancy="shared") | extra
    return decode_message("nearpilot/node/speaker-1/announce", json.dumps(data).encode(), received_at=RECEIVED)


def test_FR04_announce_speaker_controls_to_shared_model():
    report = announce(controls={"volume_max": 20})
    assert report.descriptor == legacy_descriptor("speaker", Occupancy.SHARED, volume_max=20)
    def volume_max(descriptor):
        volume = descriptor.action("on").properties.get("volume")
        return None if volume is None else volume["maximum"]

    assert volume_max(report.descriptor) == 20
    for value in (1, 30):  # 범위의 양 끝도 받는다
        assert volume_max(announce(controls={"volume_max": value}).descriptor) == value
    assert volume_max(announce().descriptor) is None  # 이전 펌웨어의 발표는 그대로 받는다
    light = announce("light", controls={"brightness_max": 64, "color": True})
    assert volume_max(light.descriptor) is None


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


def command(action="on", arguments=None):
    return NodeCommand("command-1", "speaker-1", action, SESSION, SENT, RECEIVED, arguments=arguments)


def test_FR06_encode_command_speaker_parameters():
    data = json.loads(encode_command(command(arguments={"volume": 12}), schema_version=2))
    assert data["parameters"] == {"volume": 12}
    assert data["action"] == "on"
    zero = json.loads(encode_command(command(arguments={"volume": 0}), schema_version=2))
    assert zero["parameters"] == {"volume": 0} and zero["action"] == "on"


def test_FR22_same_command_same_speaker_parameters():
    a = encode_command(command(arguments={"volume": 12}), schema_version=2)
    assert a == encode_command(replace(command(), arguments={"volume": 12}), schema_version=2)
    # 내용이 다르면 바이트도 다르다. parameters 가 없는 명령과도 다르다.
    assert a != encode_command(command(arguments={"volume": 13}), schema_version=2)
    assert a != encode_command(command(), schema_version=2)
