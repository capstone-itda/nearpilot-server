"""SmartThings capability 조회로 기기 선언을 합성한다 (FR-04·FR-09, specs/iot-smartthings-descriptor).

종류는 `display`, 점유 방식은 `shared`다. `switch` capability가 없으면 `off`를
`deactivate`로 선언할 수 없어 선언 자체를 만들지 않는다. 그 밖의 capability는
있을 때만 동작을 더한다. 이 모듈은 네트워크를 호출하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable

from nearpilot.shared.descriptor import DescriptorError, DeviceDescriptor, parse_descriptor

KIND = "display"
_FORMAT = "nearpilot.device/1"
_UNKNOWN_FIRMWARE = "unknown"

_NO_ARGS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
_SIGNAL = {"kind": "signal_output"}

# capability id -> (동작 이름, 동작 선언). SmartThings capability.command 매핑은
# iot-smartthings-adapter 의 명령 전송이 이 테이블을 그대로 읽는다.
_CAPABILITY_ACTIONS: dict[str, list[dict]] = {
    "audioVolume": [{
        "name": "set_volume",
        "description": "음량을 정한다",
        "input_schema": {
            "type": "object",
            "properties": {"volume": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["volume"],
            "additionalProperties": False,
        },
        "result": _SIGNAL,
    }],
    "audioMute": [
        {"name": "mute", "description": "소리를 끈다", "input_schema": _NO_ARGS, "result": _SIGNAL},
        {"name": "unmute", "description": "소리를 켠다", "input_schema": _NO_ARGS, "result": _SIGNAL},
    ],
    "mediaInputSource": [{
        "name": "set_input_source",
        "description": "입력 소스를 바꾼다",
        "input_schema": {
            "type": "object",
            "properties": {"inputSource": {"type": "string"}},
            "required": ["inputSource"],
            "additionalProperties": False,
        },
        "result": _SIGNAL,
    }],
}


class UnsupportedDeviceError(DescriptorError):
    """`switch` capability 가 없어서 `display` 선언을 만들 수 없다."""


def smartthings_descriptor(
    capability_ids: Iterable[str], *, vendor_name: str, model: str, firmware: str | None,
) -> DeviceDescriptor:
    """capability id 목록으로 `display` 종류의 `DeviceDescriptor`를 합성한다.

    `switch` 가 없으면 `UnsupportedDeviceError`다. 그 밖의 capability는 있을 때만
    동작을 더한다. `firmware`가 비어 있으면 `"unknown"`을 쓴다.
    """
    ids = set(capability_ids)
    if "switch" not in ids:
        raise UnsupportedDeviceError("switch capability 가 없어 display 선언을 만들 수 없다")

    actions = [
        {"name": "on", "description": "디스플레이를 켠다", "input_schema": _NO_ARGS, "result": _SIGNAL},
        {"name": "off", "description": "디스플레이를 끈다", "role": "deactivate",
         "input_schema": _NO_ARGS, "result": _SIGNAL},
    ]
    for capability, specs in _CAPABILITY_ACTIONS.items():
        if capability in ids:
            actions.extend(specs)

    return parse_descriptor({
        "format": _FORMAT,
        "kind": KIND,
        "occupancy": "shared",
        "vendor": {"name": vendor_name, "model": model, "firmware": firmware or _UNKNOWN_FIRMWARE},
        "actions": actions,
    })
