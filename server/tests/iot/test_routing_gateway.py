"""FR-06 동반 기기 명령 라우팅 (specs/iot-smartthings-companion/spec.md 결정 1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.iot.routing import RoutingCommandGateway
from nearpilot.shared.enums import CommandOutcome
from nearpilot.shared.models import CommandResult, NodeCommand

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


class RecordingGateway:
    def __init__(self, name: str):
        self.name = name
        self.calls: list[tuple[NodeCommand, float]] = []

    def send(self, command: NodeCommand, timeout_sec: float) -> CommandResult:
        self.calls.append((command, timeout_sec))
        return CommandResult(command.command_id, CommandOutcome.UNKNOWN, reported_at=NOW)


def command(node_id: str) -> NodeCommand:
    return NodeCommand("cmd-1", node_id, "on", "sess-1", NOW, NOW + timedelta(seconds=10))


def test_FR06_routing_bound_node_goes_to_smartthings():
    mqtt, smartthings = RecordingGateway("mqtt"), RecordingGateway("smartthings")
    gateway = RoutingCommandGateway(mqtt, {"display-1": smartthings})
    result = gateway.send(command("display-1"), 10.0)
    assert result.command_id == "cmd-1"
    assert [(c.node_id, t) for c, t in smartthings.calls] == [("display-1", 10.0)]
    assert mqtt.calls == []


def test_FR06_routing_unbound_node_goes_to_mqtt():
    mqtt, smartthings = RecordingGateway("mqtt"), RecordingGateway("smartthings")
    gateway = RoutingCommandGateway(mqtt, {"display-1": smartthings})
    gateway.send(command("light-1"), 3.0)
    assert [(c.node_id, t) for c, t in mqtt.calls] == [("light-1", 3.0)]
    assert smartthings.calls == []
