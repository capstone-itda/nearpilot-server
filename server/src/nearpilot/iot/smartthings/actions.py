"""SmartThings 동작 표: 선언, 명령 선택, 확인 방식 (FR-04·FR-06·FR-07, specs/iot-smartthings-companion).

동작 8개와 SmartThings 명령, 위치 인자, 성공 확인은 `requirements.md` FR-07
"SmartThings 클라우드 기기의 결과 확인" 표를 따른다 (#271, #270 실기 결과).
선언 합성(`descriptor.py`), 명령 전송(`gateway.py`), 펌웨어 동반 기기 선언 시험이 이 표 하나를 읽는다.

한 선언 동작이 인자 값에 따라 SmartThings 명령을 고르는 규칙은 동작마다 `plan(arguments)` 하나에 둔다.
고를 수 없는 인자면 `None`이고 게이트웨이는 보내지 않는다 (spec 결정 2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

EQUALS = "equals"  # 같은 capability 속성이 기대값과 같다
CHANGED = "changed"  # 전송 전 값에서 바뀐다
EQUALS_OTHER = "equals_other"  # 다른 capability 속성이 기대값과 같다
METHODS = (EQUALS, CHANGED, EQUALS_OTHER)

REFRESH_CAPABILITY = "refresh"
REFRESH_COMMAND = "refresh"


@dataclass(frozen=True, slots=True)
class Confirm:
    """상태 조회로 성공을 확인하는 방식 (spec 결정 3)."""

    method: str
    capability: str
    attribute: str
    expected: tuple[Any, ...] = ()  # equals·equals_other: 이 중 하나와 같으면 성공. changed 는 비운다

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"unknown confirm method: {self.method}")
        if (self.method == CHANGED) != (not self.expected):
            raise ValueError("changed 는 기대값이 없고, 나머지는 기대값이 있어야 한다")


@dataclass(frozen=True, slots=True)
class ModeName:
    """보낼 인자를 기기가 반환한 ID·이름 대응에서 고른다 (spec 결정 8)."""

    attribute: str  # 같은 capability 의 대응표 속성 (예: supportedPictureModesMap)
    mode_id: str


@dataclass(frozen=True, slots=True)
class Plan:
    """인자 하나로 정한 SmartThings 명령과 확인 방식."""

    capability: str
    command: str
    arguments: tuple[Any, ...]  # SmartThings 위치 인자. mode_name 이 있으면 게이트웨이가 고른 이름 하나로 채운다
    confirm: Confirm
    refresh: bool = False  # 확인 조회마다 refresh.refresh 를 먼저 보낸다 (spec 결정 4)
    mode_name: ModeName | None = None


@dataclass(frozen=True, slots=True)
class Reported:
    """보고 상태의 키 하나와 읽을 속성 (FR-09, #289, specs/iot-device-reported-state 결정 1)."""

    key: str
    capability: str
    attribute: str


@dataclass(frozen=True, slots=True)
class SmartThingsAction:
    """NearPilot 동작 이름 하나에 대응하는 SmartThings 쪽 정보."""

    requires: tuple[str, ...]  # 선언 합성에서 이 capability 가 모두 있어야 동작을 선언한다
    declaration: Mapping[str, Any]  # device-protocol.md 형식의 동작 선언(이름 포함)
    plan: Callable[[Mapping[str, Any] | None], Plan | None]
    reported: Reported | None = None  # 이 동작이 바꾸는 값. 확인 속성과 같다


_SIGNAL = {"kind": "signal_output"}
# 인자 없는 동작. 펌웨어 동반 기기 발표가 2048바이트 안에 들도록 빈 properties·required 를 쓰지 않는다.
_NO_ARGS = {"type": "object", "additionalProperties": False}


def _schema(name: str, prop: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {name: dict(prop)}, "required": [name], "additionalProperties": False}


def _arg(arguments: Mapping[str, Any] | None, name: str) -> Any:
    return (arguments or {}).get(name)


def _switch(command: str) -> Callable[[Mapping[str, Any] | None], Plan | None]:
    def plan(arguments: Mapping[str, Any] | None) -> Plan:
        return Plan("switch", command, (), Confirm(EQUALS, "switch", "switch", (command,)))
    return plan


def _volume(arguments: Mapping[str, Any] | None) -> Plan | None:
    volume = _arg(arguments, "volume")
    if type(volume) is not int:
        return None
    return Plan("audioVolume", "setVolume", (volume,), Confirm(EQUALS, "audioVolume", "volume", (volume,)))


_MUTE_COMMANDS = {"muted": "mute", "unmuted": "unmute"}


def _mute(arguments: Mapping[str, Any] | None) -> Plan | None:
    state = _arg(arguments, "state")
    command = _MUTE_COMMANDS.get(state) if isinstance(state, str) else None
    if command is None:
        return None
    return Plan("audioMute", command, (), Confirm(EQUALS, "audioMute", "mute", (state,)))


def _mode(capability: str, command: str, attribute: str, map_attribute: str
          ) -> Callable[[Mapping[str, Any] | None], Plan | None]:
    def plan(arguments: Mapping[str, Any] | None) -> Plan | None:
        mode_id = _arg(arguments, "mode")
        if not isinstance(mode_id, str) or not mode_id:
            return None
        # 기대값의 이름은 게이트웨이가 대응표에서 고른 뒤 더한다. 현재 값이 ID 나 이름이면 성공이다 (spec 가정).
        return Plan(capability, command, (), Confirm(EQUALS, capability, attribute, (mode_id,)),
                    refresh=True, mode_name=ModeName(map_attribute, mode_id))
    return plan


_CHANNEL_NUMBER = re.compile(r"[0-9]{1,4}")
_CHANNEL_STEPS = {"up": "channelUp", "down": "channelDown"}


def _channel(arguments: Mapping[str, Any] | None) -> Plan | None:
    channel = _arg(arguments, "channel")
    if not isinstance(channel, str):
        return None
    if _CHANNEL_NUMBER.fullmatch(channel):
        return Plan("tvChannel", "setTvChannel", (channel,),
                    Confirm(EQUALS, "tvChannel", "tvChannel", (channel, int(channel))), refresh=True)
    step = _CHANNEL_STEPS.get(channel)
    if step is None:
        return None
    # 상대 명령이다. 같은 command_id 로 다시 보내지 않는다 (FR-07, spec 결정 6).
    return Plan("tvChannel", step, (), Confirm(CHANGED, "tvChannel", "tvChannel"), refresh=True)


@dataclass(frozen=True, slots=True)
class App:
    arguments: tuple[str, ...]  # custom.launchapp.launchApp 위치 인자
    identifier: str  # 실행 뒤 tvChannel.tvChannelName 값


# #270 실기 값 (spec 결정 9)
APPS: dict[str, App] = {
    "youtube": App(("", "YouTube"), "9Ur5IzDKqV.TizenYouTube"),
}


def _launch(arguments: Mapping[str, Any] | None) -> Plan | None:
    app = APPS.get(_arg(arguments, "app")) if isinstance(_arg(arguments, "app"), str) else None
    if app is None:
        return None
    return Plan("custom.launchapp", "launchApp", app.arguments,
                Confirm(EQUALS_OTHER, "tvChannel", "tvChannelName", (app.identifier,)), refresh=True)


# 화면·소리 모드 ID 의 최대 길이. 펌웨어 패턴 부분집합에 맞춰 길이만 제한한다 (spec 가정).
MODE_ID_MAX_LENGTH = 32

# 순서가 선언 순서다. 설명은 펌웨어 발표 상한(2048바이트) 때문에 짧게 쓴다. `on`·`off`는 모든 display 기기에 항상 들어간다(switch 필수, specs/iot-smartthings-descriptor).
ACTIONS: dict[str, SmartThingsAction] = {
    "on": SmartThingsAction(
        requires=("switch",), plan=_switch("on"),
        reported=Reported("power", "switch", "switch"),
        declaration={"name": "on", "description": "켜기", "input_schema": _NO_ARGS, "result": _SIGNAL},
    ),
    "off": SmartThingsAction(
        requires=("switch",), plan=_switch("off"),
        reported=Reported("power", "switch", "switch"),
        declaration={"name": "off", "description": "끄기", "role": "deactivate",
                     "input_schema": _NO_ARGS, "result": _SIGNAL},
    ),
    "set_volume": SmartThingsAction(
        requires=("audioVolume",), plan=_volume,
        reported=Reported("volume", "audioVolume", "volume"),
        declaration={"name": "set_volume", "description": "음량",
                     "input_schema": _schema("volume", {"type": "integer", "minimum": 0, "maximum": 100}),
                     "result": _SIGNAL},
    ),
    "set_mute": SmartThingsAction(
        requires=("audioMute",), plan=_mute,
        reported=Reported("mute", "audioMute", "mute"),
        declaration={"name": "set_mute", "description": "음소거",
                     "input_schema": _schema("state", {"type": "string", "enum": ["muted", "unmuted"]}),
                     "result": _SIGNAL},
    ),
    "set_picture_mode": SmartThingsAction(
        requires=("custom.picturemode",),
        reported=Reported("picture_mode", "custom.picturemode", "pictureMode"),
        plan=_mode("custom.picturemode", "setPictureMode", "pictureMode", "supportedPictureModesMap"),
        declaration={"name": "set_picture_mode", "description": "화면 모드 ID",
                     "input_schema": _schema("mode", {"type": "string", "maxLength": MODE_ID_MAX_LENGTH}),
                     "result": _SIGNAL},
    ),
    "set_sound_mode": SmartThingsAction(
        requires=("custom.soundmode",),
        reported=Reported("sound_mode", "custom.soundmode", "soundMode"),
        plan=_mode("custom.soundmode", "setSoundMode", "soundMode", "supportedSoundModesMap"),
        declaration={"name": "set_sound_mode", "description": "소리 모드 ID",
                     "input_schema": _schema("mode", {"type": "string", "maxLength": MODE_ID_MAX_LENGTH}),
                     "result": _SIGNAL},
    ),
    "set_channel": SmartThingsAction(
        requires=("tvChannel",), plan=_channel,
        reported=Reported("channel", "tvChannel", "tvChannel"),
        declaration={"name": "set_channel", "description": "채널 번호나 up·down",
                     "input_schema": _schema("channel", {"type": "string", "maxLength": 4}),
                     "result": _SIGNAL},
    ),
    "launch_app": SmartThingsAction(
        requires=("custom.launchapp", "tvChannel"), plan=_launch,
        reported=Reported("app", "tvChannel", "tvChannelName"),
        declaration={"name": "launch_app", "description": "앱 실행",
                     "input_schema": _schema("app", {"type": "string", "enum": sorted(APPS)}),
                     "result": _SIGNAL},
    ),
}

ALWAYS_DECLARED = ("on", "off")  # switch capability 하나로 둘 다 나온다 (deactivate 필수)
