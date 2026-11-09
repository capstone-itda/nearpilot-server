"""FR-06·FR-07 SmartThings 명령 전송과 상태 확인 (specs/iot-smartthings-adapter/spec.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.iot.smartthings.gateway import SmartThingsCommandGateway
from nearpilot.iot.smartthings.transport import SmartThingsTransportError
from nearpilot.shared.enums import CommandOutcome
from nearpilot.shared.models import NodeCommand

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


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
    def __init__(self):
        self.posted: list[tuple[str, str, str, dict | None]] = []
        self.post_error: Exception | None = None
        self.attribute_sequence: list[object] = []
        self.get_error: Exception | None = None

    def post_command(self, device_id, capability, command, arguments):
        if self.post_error is not None:
            raise self.post_error
        self.posted.append((device_id, capability, command, arguments))

    def get_attribute(self, device_id, capability, attribute):
        if self.get_error is not None:
            raise self.get_error
        if self.attribute_sequence:
            return self.attribute_sequence.pop(0)
        return None


def command(action: str, *, command_id: str = "cmd-1", arguments: dict | None = None) -> NodeCommand:
    return NodeCommand(
        command_id=command_id, node_id="display-1", action=action, target_session_id="sess-1",
        issued_at=NOW, expires_at=NOW + timedelta(seconds=10), arguments=arguments,
    )


def make_gateway(transport: FakeTransport, clock: FakeClock) -> SmartThingsCommandGateway:
    return SmartThingsCommandGateway(
        transport, "device-123", poll_interval_sec=1.0,
        clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep,
    )


def test_FR06_unknown_action_not_sent():
    transport = FakeTransport()
    gateway = make_gateway(transport, FakeClock())
    result = gateway.send(command("launch_app"), timeout_sec=5)
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
    assert transport.posted == [("device-123", "switch", "on", None)]


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
    assert clock.value >= 2.5


def test_FR07_poll_errors_treated_as_unconfirmed():
    transport = FakeTransport()
    transport.get_error = SmartThingsTransportError("network blip")
    clock = FakeClock()
    gateway = make_gateway(transport, clock)
    result = gateway.send(command("on"), timeout_sec=1.5)
    assert result.outcome is CommandOutcome.UNKNOWN


def test_FR06_same_command_id_same_content_resends():
    # 기기(SmartThings)가 같은 command_id의 재전송을 멱등하게 받는다고 가정한다(모든 동작이 절대값 지정이다).
    # 게이트웨이는 충돌로 보지 않고 다시 전송·확인한다 — MqttCommandGateway와 같은 규칙(FR-06).
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
    assert transport.posted == [("device-123", "audioVolume", "setVolume", {"volume": 20})]
