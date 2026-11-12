"""FR-04·FR-09 SmartThings capability 선언 합성.

specs/iot-smartthings-descriptor/spec.md, 동작 표는 specs/iot-smartthings-companion/spec.md 결정 16
"""

from __future__ import annotations

from nearpilot.iot.smartthings.descriptor import UnsupportedDeviceError, smartthings_descriptor
from nearpilot.shared.descriptor import descriptor_hash, parse_descriptor
from nearpilot.shared.enums import ActionRole, Occupancy

VENDOR = {"vendor_name": "samsung", "model": "m5", "firmware": "1.0.0"}


def test_FR04_missing_switch_capability_rejected():
    try:
        smartthings_descriptor([], **VENDOR)
    except UnsupportedDeviceError:
        pass
    else:
        raise AssertionError("switch 없이 선언을 만들면 안 된다")


def test_FR04_minimal_capabilities_declares_switch_only():
    descriptor = smartthings_descriptor(["switch"], **VENDOR)
    assert [a.name for a in descriptor.actions] == ["on", "off"]
    assert descriptor.kind == "display"
    assert descriptor.occupancy is Occupancy.SHARED
    assert descriptor.action("off").role is ActionRole.DEACTIVATE
    assert descriptor.action("on").role is None


def test_FR04_audio_volume_adds_set_volume():
    descriptor = smartthings_descriptor(["switch", "audioVolume"], **VENDOR)
    assert [a.name for a in descriptor.actions] == ["on", "off", "set_volume"]
    spec = descriptor.action("set_volume")
    assert spec.role is None
    assert spec.properties["volume"]["minimum"] == 0
    assert spec.properties["volume"]["maximum"] == 100


FULL = ["switch", "audioVolume", "audioMute", "custom.picturemode", "custom.soundmode", "tvChannel",
        "custom.launchapp", "refresh"]


def test_FR04_full_capabilities_declares_all_actions():
    descriptor = smartthings_descriptor(FULL, **VENDOR)
    assert [a.name for a in descriptor.actions] == [
        "on", "off", "set_volume", "set_mute", "set_picture_mode", "set_sound_mode", "set_channel", "launch_app",
    ]
    assert descriptor.action("on").properties == {}
    assert list(descriptor.action("set_mute").properties["state"]["enum"]) == ["muted", "unmuted"]
    assert descriptor.action("set_channel").properties["channel"] == {"type": "string", "maxLength": 4}
    assert list(descriptor.action("launch_app").properties["app"]["enum"]) == ["youtube"]
    assert all(a.role is None for a in descriptor.actions if a.name != "off")


def test_FR04_removed_actions_not_declared():
    descriptor = smartthings_descriptor(FULL + ["mediaInputSource"], **VENDOR)
    names = [a.name for a in descriptor.actions]
    assert not {"mute", "unmute", "set_input_source"} & set(names)


def test_FR04_launch_app_needs_tv_channel_for_confirmation():
    descriptor = smartthings_descriptor(["switch", "custom.launchapp"], **VENDOR)
    assert [a.name for a in descriptor.actions] == ["on", "off"]
    descriptor = smartthings_descriptor(["switch", "custom.launchapp", "tvChannel"], **VENDOR)
    assert [a.name for a in descriptor.actions] == ["on", "off", "set_channel", "launch_app"]


def test_FR04_unknown_capability_ignored():
    descriptor = smartthings_descriptor(["switch", "samsungvd.remoteControl"], **VENDOR)
    assert [a.name for a in descriptor.actions] == ["on", "off"]


def test_FR04_duplicate_capability_id_deduplicated():
    descriptor = smartthings_descriptor(["switch", "audioVolume", "audioVolume"], **VENDOR)
    assert [a.name for a in descriptor.actions] == ["on", "off", "set_volume"]


def test_FR04_missing_firmware_defaults_to_unknown():
    descriptor = smartthings_descriptor(["switch"], vendor_name="samsung", model="m5", firmware="")
    assert descriptor.vendor.firmware == "unknown"
    descriptor_none = smartthings_descriptor(["switch"], vendor_name="samsung", model="m5", firmware=None)
    assert descriptor_none.vendor.firmware == "unknown"


def test_FR04_result_passes_parse_descriptor_contract():
    descriptor = smartthings_descriptor(FULL, **VENDOR)
    reparsed = parse_descriptor(descriptor.to_json())
    assert reparsed == descriptor
    assert descriptor_hash(descriptor) == descriptor_hash(reparsed)
