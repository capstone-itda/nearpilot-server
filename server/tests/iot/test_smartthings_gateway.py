"""FR-06·FR-07 SmartThings 명령 전송과 상태 확인.

specs/iot-smartthings-adapter/spec.md, specs/iot-smartthings-companion/spec.md
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.iot.smartthings.gateway import SmartThingsCommandGateway
from nearpilot.iot.smartthings.transport import (
    SmartThingsMaybeSent, SmartThingsNotSent, SmartThingsTransportError,
)
from nearpilot.shared.enums import CommandOutcome
from nearpilot.shared.models import NodeCommand

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
DEVICE = "device-123"


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def now(self) -> datetime:
        return NOW


class FakeTransport:
    """속성마다 조회 값 순서를 둔다. 순서가 끝나면 마지막 값을 계속 돌려준다."""

    def __init__(self):
        self.posted: list[tuple[str, str, str, list]] = []
        self.post_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.values: dict[tuple[str, str], list[object]] = {}
        self.attribute_sequence: list[object] = []  # 기본 확인 속성(switch·volume 등)의 순서
        self.get_error: Exception | None = None
        self.reads: list[tuple[str, str]] = []

    def post_command(self, device_id, capability, command, arguments):
        assert isinstance(arguments, list)
        if capability == "refresh":
            self.posted.append((device_id, capability, command, arguments))
            if self.refresh_error is not None:
                raise self.refresh_error
            return
        if self.post_error is not None:
            raise self.post_error
        self.posted.append((device_id, capability, command, arguments))

    def get_attribute(self, device_id, capability, attribute):
        assert device_id == DEVICE
        self.reads.append((capability, attribute))
        if self.get_error is not None:
            raise self.get_error
        queue = self.values.get((capability, attribute))
        if queue is None:
            queue = self.attribute_sequence
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def commands(self) -> list[tuple[str, str, list]]:
        return [(cap, cmd, args) for _, cap, cmd, args in self.posted if cap != "refresh"]

    def refreshes(self) -> int:
        return sum(1 for _, cap, _, _ in self.posted if cap == "refresh")


def command(action: str, *, command_id: str = "cmd-1", arguments: dict | None = None) -> NodeCommand:
    return NodeCommand(
        command_id=command_id, node_id="display-1", action=action, target_session_id="sess-1",
        issued_at=NOW, expires_at=NOW + timedelta(seconds=10), arguments=arguments,
    )


def make_gateway(transport: FakeTransport, clock: FakeClock) -> SmartThingsCommandGateway:
    return SmartThingsCommandGateway(
        transport, DEVICE, poll_interval_sec=1.0,
        clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep,
    )


# ── 기존 규칙 (specs/iot-smartthings-adapter) ─────────────────────────


def test_FR06_unknown_action_not_sent():
    transport = FakeTransport()
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_input_source", arguments={"inputSource": "HDMI1"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.NOT_SENT
    assert result.report is None
    assert transport.posted == []


def test_FR06_post_failure_not_sent():
    transport = FakeTransport()
    transport.post_error = SmartThingsTransportError("auth failed")
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("on"), timeout_sec=5)
    assert result.outcome is CommandOutcome.NOT_SENT


def test_FR07_confirmed_immediately_succeeds():
    transport = FakeTransport()
    transport.attribute_sequence = ["on"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("on"), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert result.report is None
    assert transport.posted == [(DEVICE, "switch", "on", [])]
    assert result.confirmation == {"source": "smartthings_state", "capability": "switch",
                                   "attribute": "switch", "value": "on"}


def test_FR07_confirmed_after_polling_succeeds():
    transport = FakeTransport()
    transport.attribute_sequence = ["off", "off", "on"]
    clock = FakeClock()
    gateway = make_gateway(transport, clock)
    result = gateway.send(command("on"), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert clock.value == 2.0  # 두 번 더 기다린 뒤 세 번째 조회에서 확인했다


def test_FR07_timeout_without_confirmation_is_unknown():
    transport = FakeTransport()
    transport.attribute_sequence = []  # 항상 None, 기대값과 다름
    clock = FakeClock()
    gateway = make_gateway(transport, clock)
    result = gateway.send(command("on"), timeout_sec=2.5)
    assert result.outcome is CommandOutcome.UNKNOWN
    assert result.confirmation is None
    assert clock.value >= 2.5


def test_FR07_poll_errors_treated_as_unconfirmed():
    transport = FakeTransport()
    transport.get_error = SmartThingsTransportError("network blip")
    clock = FakeClock()
    gateway = make_gateway(transport, clock)
    result = gateway.send(command("on"), timeout_sec=1.5)
    assert result.outcome is CommandOutcome.UNKNOWN


def test_FR06_same_command_id_same_content_resends():
    # 절대 명령은 같은 command_id 로 다시 와도 다시 보내고 확인한다 — MqttCommandGateway와 같은 규칙(FR-06).
    transport = FakeTransport()
    transport.attribute_sequence = ["on", "on"]
    gateway = make_gateway(transport, FakeClock())
    cmd = command("on")
    first = gateway.send(cmd, timeout_sec=5)
    second = gateway.send(cmd, timeout_sec=5)
    assert first.outcome is CommandOutcome.SUCCEEDED
    assert second.outcome is CommandOutcome.SUCCEEDED
    assert len(transport.posted) == 2


def test_FR06_same_command_id_different_content_raises():
    transport = FakeTransport()
    transport.attribute_sequence = ["on"]
    gateway = make_gateway(transport, FakeClock())
    gateway.send(command("on", command_id="cmd-x"), timeout_sec=5)
    with pytest.raises(ValueError):
        gateway.send(command("off", command_id="cmd-x"), timeout_sec=5)


@pytest.mark.parametrize("timeout_sec", [0, -1, float("inf"), float("nan")])
def test_FR06_invalid_timeout_raises(timeout_sec):
    transport = FakeTransport()
    gateway = make_gateway(transport, FakeClock())
    with pytest.raises(ValueError):
        gateway.send(command("on"), timeout_sec=timeout_sec)


def test_FR07_set_volume_uses_argument_as_expected_value():
    transport = FakeTransport()
    transport.attribute_sequence = [20]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_volume", arguments={"volume": 20}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert transport.posted == [(DEVICE, "audioVolume", "setVolume", [20])]


def test_FR07_volume_reported_as_float_succeeds():
    # 상태 조회가 숫자를 15.0 으로 돌려줘도 요청한 15 와 같은 값이다 (#276).
    transport = FakeTransport()
    transport.attribute_sequence = [15.0]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_volume", arguments={"volume": 15}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert result.confirmation["value"] == 15.0


@pytest.mark.parametrize(("observed", "requested"), [("15", 15), (True, 1), (16.0, 15)])
def test_FR07_volume_other_value_is_unknown(observed, requested):
    # 문자열·bool·다른 숫자는 같은 값이 아니다.
    transport = FakeTransport()
    transport.attribute_sequence = [observed]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_volume", arguments={"volume": requested}), timeout_sec=2)
    assert result.outcome is CommandOutcome.UNKNOWN


def test_FR07_unconfirmed_logs_observed_value_without_device_id(caplog):
    transport = FakeTransport()
    transport.attribute_sequence = ["off"]
    gateway = make_gateway(transport, FakeClock())
    with caplog.at_level("WARNING", logger="nearpilot.iot.smartthings.gateway"):
        result = gateway.send(command("on"), timeout_sec=2)
    assert result.outcome is CommandOutcome.UNKNOWN
    text = caplog.text
    assert "node=display-1" in text and "command=cmd-1" in text
    assert "switch.switch" in text and "'off'" in text and "'on'" in text
    assert DEVICE not in text


# ── 동반 기기 동작 표 (specs/iot-smartthings-companion) ─────────────────


def test_FR06_http_connect_error_not_sent():
    transport = FakeTransport()
    transport.post_error = SmartThingsNotSent("SmartThings switch.on: 연결 실패")
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("on"), timeout_sec=5)
    assert result.outcome is CommandOutcome.NOT_SENT
    assert transport.reads == []  # 보내지 못했으면 확인하지 않는다


def test_FR07_ambiguous_post_keeps_confirming():
    transport = FakeTransport()
    transport.post_error = SmartThingsMaybeSent("SmartThings switch.on: 응답 시간 초과")
    transport.attribute_sequence = ["off", "on"]
    gateway = make_gateway(transport, FakeClock())
    assert gateway.send(command("on"), timeout_sec=5).outcome is CommandOutcome.SUCCEEDED

    unconfirmed = FakeTransport()
    unconfirmed.post_error = SmartThingsMaybeSent("SmartThings switch.on: 서버 오류(503)")
    unconfirmed.attribute_sequence = ["off"]
    gateway = make_gateway(unconfirmed, FakeClock())
    assert gateway.send(command("on"), timeout_sec=3).outcome is CommandOutcome.UNKNOWN


@pytest.mark.parametrize("channel", ["12345", "next", "", "1a"])
def test_FR06_invalid_channel_not_sent(channel):
    transport = FakeTransport()
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_channel", arguments={"channel": channel}), timeout_sec=5)
    assert result.outcome is CommandOutcome.NOT_SENT
    assert transport.posted == []


@pytest.mark.parametrize(("state", "smartthings_command"), [("muted", "mute"), ("unmuted", "unmute")])
def test_FR07_set_mute_selects_command(state, smartthings_command):
    transport = FakeTransport()
    transport.values[("audioMute", "mute")] = [state]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_mute", arguments={"state": state}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert transport.commands() == [("audioMute", smartthings_command, [])]
    assert result.confirmation["attribute"] == "mute"
    assert result.confirmation["value"] == state


MODES = [{"id": "modeStandard", "name": "표준"}, {"id": "modeMovie", "name": "영화"}]


def test_FR07_picture_mode_sends_name_from_map():
    transport = FakeTransport()
    transport.values[("custom.picturemode", "supportedPictureModesMap")] = [MODES]
    transport.values[("custom.picturemode", "pictureMode")] = ["modeStandard", "영화"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_picture_mode", arguments={"mode": "modeMovie"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert transport.commands() == [("custom.picturemode", "setPictureMode", ["영화"])]
    assert transport.refreshes() == 2  # 확인 조회마다 먼저 refresh
    assert result.confirmation == {"source": "smartthings_state", "capability": "custom.picturemode",
                                   "attribute": "pictureMode", "value": "영화"}


def test_FR07_sound_mode_confirmed_by_id():
    transport = FakeTransport()
    transport.values[("custom.soundmode", "supportedSoundModesMap")] = [[{"id": "modeVoice", "name": "음성 강조"}]]
    transport.values[("custom.soundmode", "soundMode")] = ["modeVoice"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_sound_mode", arguments={"mode": "modeVoice"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert transport.commands() == [("custom.soundmode", "setSoundMode", ["음성 강조"])]


def test_FR07_mode_map_missing_not_sent():
    transport = FakeTransport()
    transport.values[("custom.picturemode", "supportedPictureModesMap")] = [None]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_picture_mode", arguments={"mode": "modeMovie"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.NOT_SENT
    assert transport.commands() == []
    assert transport.refreshes() == 1  # null 이면 refresh 뒤 한 번 더 읽는다
    assert transport.reads.count(("custom.picturemode", "supportedPictureModesMap")) == 2

    later = FakeTransport()
    later.values[("custom.picturemode", "supportedPictureModesMap")] = [None, MODES]
    later.values[("custom.picturemode", "pictureMode")] = ["modeMovie"]
    gateway = make_gateway(later, FakeClock())
    result = gateway.send(command("set_picture_mode", arguments={"mode": "modeMovie"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert later.commands() == [("custom.picturemode", "setPictureMode", ["영화"])]

    unknown_id = FakeTransport()
    unknown_id.values[("custom.picturemode", "supportedPictureModesMap")] = [MODES]
    gateway = make_gateway(unknown_id, FakeClock())
    result = gateway.send(command("set_picture_mode", arguments={"mode": "modeGame"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.NOT_SENT


def test_FR07_channel_number_confirmed_after_refresh():
    transport = FakeTransport()
    transport.values[("tvChannel", "tvChannel")] = ["7", "11"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_channel", arguments={"channel": "11"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert transport.commands() == [("tvChannel", "setTvChannel", ["11"])]
    assert transport.refreshes() == 2


def test_FR07_channel_up_confirmed_by_change():
    transport = FakeTransport()
    # 전송 전 값 7, 첫 확인 조회 7(아직 그대로), 둘째 8
    transport.values[("tvChannel", "tvChannel")] = ["7", "7", "8"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_channel", arguments={"channel": "up"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert transport.commands() == [("tvChannel", "channelUp", [])]
    assert result.confirmation == {"source": "smartthings_state", "capability": "tvChannel",
                                   "attribute": "tvChannel", "value": "8", "before": "7"}


def test_FR07_channel_down_without_before_value_not_sent():
    transport = FakeTransport()
    transport.values[("tvChannel", "tvChannel")] = [None]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_channel", arguments={"channel": "down"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.NOT_SENT
    assert transport.commands() == []


def test_FR06_relative_command_not_resent():
    transport = FakeTransport()
    transport.values[("tvChannel", "tvChannel")] = ["7", "8"]
    gateway = make_gateway(transport, FakeClock())
    cmd = command("set_channel", command_id="cmd-up", arguments={"channel": "up"})
    first = gateway.send(cmd, timeout_sec=5)
    second = gateway.send(cmd, timeout_sec=5)
    assert first.outcome is CommandOutcome.SUCCEEDED
    assert second is first
    assert transport.commands() == [("tvChannel", "channelUp", [])]

    # 처음 결과가 불명이어도 다시 보내지 않는다
    stuck = FakeTransport()
    stuck.values[("tvChannel", "tvChannel")] = ["7"]
    gateway = make_gateway(stuck, FakeClock())
    first = gateway.send(cmd, timeout_sec=2)
    second = gateway.send(cmd, timeout_sec=2)
    assert first.outcome is second.outcome is CommandOutcome.UNKNOWN
    assert stuck.commands() == [("tvChannel", "channelUp", [])]


def test_FR07_launch_app_confirmed_by_other_attribute():
    transport = FakeTransport()
    transport.values[("tvChannel", "tvChannelName")] = ["", "9Ur5IzDKqV.TizenYouTube"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("launch_app", arguments={"app": "youtube"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert transport.commands() == [("custom.launchapp", "launchApp", ["", "YouTube"])]
    assert result.confirmation["capability"] == "tvChannel"
    assert result.confirmation["attribute"] == "tvChannelName"


def test_FR07_refresh_failure_keeps_confirming():
    transport = FakeTransport()
    transport.refresh_error = SmartThingsMaybeSent("SmartThings refresh.refresh: 서버 오류(500)")
    transport.values[("tvChannel", "tvChannel")] = ["11"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("set_channel", arguments={"channel": "11"}), timeout_sec=5)
    assert result.outcome is CommandOutcome.SUCCEEDED


def test_FR07_refresh_completion_alone_is_not_success():
    # refresh 가 성공해도 조회 값이 기대값이 아니면 성공이 아니다
    transport = FakeTransport()
    transport.values[("tvChannel", "tvChannelName")] = ["com.other.app"]
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("launch_app", arguments={"app": "youtube"}), timeout_sec=2)
    assert result.outcome is CommandOutcome.UNKNOWN
    assert transport.refreshes() >= 2
