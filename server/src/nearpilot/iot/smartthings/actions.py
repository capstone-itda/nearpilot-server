"""SmartThings 동작 매핑: capability.command와 확인 속성 (FR-06·FR-07, specs/iot-smartthings-adapter).

NearPilot 동작 이름 하나에 SmartThings capability, command, 확인할 속성, 기대값 계산을 묶는다.
선언 합성(`iot/smartthings/descriptor.py`)과 명령 전송(`iot/smartthings/gateway.py`)이 이 표 하나를 쓴다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

_SIGNAL = {"kind": "signal_output"}
_NO_ARGS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


@dataclass(frozen=True, slots=True)
class SmartThingsAction:
    """NearPilot 동작 이름 하나에 대응하는 SmartThings 쪽 정보."""

    capability: str
    command: str
    confirm_attribute: str
    expected_value: Callable[[Mapping[str, Any] | None], Any]
    declaration: Mapping[str, Any]  # device-protocol.md 형식의 동작 선언(이름 포함)


def _const(value: Any) -> Callable[[Mapping[str, Any] | None], Any]:
    return lambda arguments: value


def _arg(key: str) -> Callable[[Mapping[str, Any] | None], Any]:
    return lambda arguments: (arguments or {})[key]


# 순서가 선언 순서다. `on`·`off`는 모든 display 기기에 항상 들어간다(switch 필수, specs/iot-smartthings-descriptor).
ACTIONS: dict[str, SmartThingsAction] = {
    "on": SmartThingsAction(
        capability="switch", command="on", confirm_attribute="switch", expected_value=_const("on"),
        declaration={"name": "on", "description": "디스플레이를 켠다", "input_schema": _NO_ARGS, "result": _SIGNAL},
    ),
    "off": SmartThingsAction(
        capability="switch", command="off", confirm_attribute="switch", expected_value=_const("off"),
        declaration={"name": "off", "description": "디스플레이를 끈다", "role": "deactivate",
                     "input_schema": _NO_ARGS, "result": _SIGNAL},
    ),
    "set_volume": SmartThingsAction(
        capability="audioVolume", command="setVolume", confirm_attribute="volume", expected_value=_arg("volume"),
        declaration={
            "name": "set_volume", "description": "음량을 정한다",
            "input_schema": {
                "type": "object",
                "properties": {"volume": {"type": "integer", "minimum": 0, "maximum": 100}},
                "required": ["volume"], "additionalProperties": False,
            },
            "result": _SIGNAL,
        },
    ),
    "mute": SmartThingsAction(
        capability="audioMute", command="mute", confirm_attribute="mute", expected_value=_const("muted"),
        declaration={"name": "mute", "description": "소리를 끈다", "input_schema": _NO_ARGS, "result": _SIGNAL},
    ),
    "unmute": SmartThingsAction(
        capability="audioMute", command="unmute", confirm_attribute="mute", expected_value=_const("unmuted"),
        declaration={"name": "unmute", "description": "소리를 켠다", "input_schema": _NO_ARGS, "result": _SIGNAL},
    ),
    "set_input_source": SmartThingsAction(
        capability="mediaInputSource", command="setInputSource", confirm_attribute="inputSource",
        expected_value=_arg("inputSource"),
        declaration={
            "name": "set_input_source", "description": "입력 소스를 바꾼다",
            "input_schema": {
                "type": "object", "properties": {"inputSource": {"type": "string"}},
                "required": ["inputSource"], "additionalProperties": False,
            },
            "result": _SIGNAL,
        },
    ),
}

ALWAYS_DECLARED = ("on", "off")  # switch capability 하나로 둘 다 나온다 (deactivate 필수)
