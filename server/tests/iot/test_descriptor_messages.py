"""FR-04 announce v3·v2 해석과 FR-06 버전별 cmd 인코딩 (specs/iot-device-descriptor/spec.md)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from nearpilot.iot.messages import MessageValidationError, decode_message, encode_command
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Action, Capability, Occupancy
from nearpilot.shared.models import LightSetting, NodeAnnounce, NodeCommand, SpeakerSetting

SESSION = "12345678-1234-1234-1234-123456789abc"
STAMP = "2026-11-06T03:00:00.000Z"
SENT = datetime.fromisoformat(STAMP)
RECEIVED = SENT + timedelta(seconds=1)


def ring() -> dict:
    """조명 v2 템플릿과 같은 v3 선언. 참조 펌웨어가 내장할 선언이다 (spec 결정 3)."""
    data = legacy_descriptor(Capability.LIGHT, Occupancy.SHARED, brightness_max=64, color=True).to_json()
    data["vendor"] = {"name": "nearpilot", "model": "light-ring", "firmware": "3.0.0"}
    return data


def message(version: int, **fields) -> dict:
    return dict(schema_version=version, node_id="light-1", session_id=SESSION, seq=1, sent_at=STAMP) | fields


def decode(data: dict | bytes):
    payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    return decode_message("nearpilot/node/light-1/announce", payload, received_at=RECEIVED)


def test_FR04_v3_announce_to_shared_model():
    report = decode(message(3, descriptor=ring()))
    assert isinstance(report, NodeAnnounce) and report.schema_version == 3
    assert report.descriptor.vendor.firmware == "3.0.0"
    assert report.descriptor.to_json() == ring()


def test_FR04_v2_announce_becomes_template():
    """v2 발표는 같은 종류의 템플릿 선언이 되고, 해시가 v3 참조 선언과 같다 (FR-04)."""
    v2 = decode(message(2, capability="light", occupancy="shared",
                        controls={"brightness_max": 64, "color": True}))
    v3 = decode(message(3, descriptor=ring()))
    assert v2.schema_version == 2
    assert v2.descriptor == legacy_descriptor(Capability.LIGHT, Occupancy.SHARED, brightness_max=64, color=True)
    assert v2.descriptor.hash == v3.descriptor.hash  # 펌웨어를 v3로 올려도 다시 신뢰하지 않는다


def test_FR04_announce_over_2048_bytes_rejected():
    base = message(3, descriptor=ring())
    size = len(json.dumps(base, ensure_ascii=False).encode())
    fitted = json.loads(json.dumps(base))
    fitted["pad"] = "x" * (2048 - size - len(', "pad": ""'))
    raw = json.dumps(fitted, ensure_ascii=False).encode()
    assert len(raw) == 2048
    decode(raw)
    fitted["pad"] += "x"
    with pytest.raises(MessageValidationError):
        decode(json.dumps(fitted, ensure_ascii=False).encode())


def test_FR04_invalid_descriptor_rejected_with_session():
    data = ring()
    data["actions"][0]["input_schema"]["properties"]["brightness"]["multipleOf"] = 2
    with pytest.raises(MessageValidationError) as caught:
        decode(message(3, descriptor=data))
    assert caught.value.session_id == SESSION  # 연결마다 한 번 감사하려고 연결 ID를 붙인다
    with pytest.raises(MessageValidationError):
        decode(message(3, capability="light", occupancy="shared"))  # v3에 선언이 없다


def test_FR04_unknown_kind_rejected_until_generalized():
    """PR ② 전까지는 Capability 열거형의 종류만 받는다 (spec 결정 6)."""
    data = ring()
    data["kind"] = "fan"
    with pytest.raises(MessageValidationError, match="kind not supported yet"):
        decode(message(3, descriptor=data))


@pytest.mark.parametrize("version", [1, 4])
def test_FR04_announce_versions_outside_transition_rejected(version):
    with pytest.raises(MessageValidationError):
        decode(message(version, descriptor=ring()))


def test_FR04_other_messages_stay_v2():
    status = message(3, observed_at=STAMP, command_state="idle")
    with pytest.raises(MessageValidationError):
        decode_message("nearpilot/node/light-1/status", json.dumps(status).encode(), received_at=RECEIVED)


def command(**fields) -> NodeCommand:
    return NodeCommand("command-1", "light-1", Action.ON, SESSION, SENT, RECEIVED, **fields)


def test_FR06_v3_node_gets_v3_command():
    data = json.loads(encode_command(command(light=LightSetting(30, "#0000ff")), schema_version=3))
    assert data["schema_version"] == 3
    assert data["arguments"] == {"brightness": 30, "color": "#0000FF"}
    assert "parameters" not in data
    speaker = NodeCommand("command-2", "speaker-1", Action.ON, SESSION, SENT, RECEIVED,
                          speaker=SpeakerSetting(12))
    assert json.loads(encode_command(speaker, schema_version=3))["arguments"] == {"volume": 12}
    assert "arguments" not in json.loads(encode_command(command(), schema_version=3))


def test_FR06_v2_node_gets_v2_command():
    """v2 기기에는 같은 값을 parameters 로 보낸다 (#245 담당자 결정)."""
    v2 = json.loads(encode_command(command(light=LightSetting(30, "#0000FF")), schema_version=2))
    v3 = json.loads(encode_command(command(light=LightSetting(30, "#0000FF")), schema_version=3))
    assert v2["schema_version"] == 2 and "arguments" not in v2
    assert v2["parameters"] == v3["arguments"]


@pytest.mark.parametrize("version", [1, 4])
def test_FR06_encode_rejects_unknown_command_version(version):
    with pytest.raises(MessageValidationError):
        encode_command(command(), schema_version=version)
