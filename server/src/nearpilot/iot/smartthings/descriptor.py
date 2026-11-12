"""SmartThings capability 조회로 기기 선언을 합성한다 (FR-04·FR-09, specs/iot-smartthings-descriptor).

종류는 `display`, 점유 방식은 `shared`다. `switch` capability가 없으면 `off`를
`deactivate`로 선언할 수 없어 선언 자체를 만들지 않는다. 그 밖의 동작은 동작 표가 요구하는
capability가 모두 있을 때만 더한다 (specs/iot-smartthings-companion 결정 16). 이 모듈은 네트워크를 호출하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable

from nearpilot.iot.smartthings.actions import ACTIONS, ALWAYS_DECLARED
from nearpilot.shared.descriptor import DescriptorError, DeviceDescriptor, parse_descriptor

KIND = "display"
_FORMAT = "nearpilot.device/1"
_UNKNOWN_FIRMWARE = "unknown"


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
        dict(action.declaration)
        for name, action in ACTIONS.items()
        if name in ALWAYS_DECLARED or all(capability in ids for capability in action.requires)
    ]

    return parse_descriptor({
        "format": _FORMAT,
        "kind": KIND,
        "occupancy": "shared",
        "vendor": {"name": vendor_name, "model": model, "firmware": firmware or _UNKNOWN_FIRMWARE},
        "actions": actions,
    })
