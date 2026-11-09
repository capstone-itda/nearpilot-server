"""FR-13 SmartThings 온라인 하트비트 (specs/iot-smartthings-adapter/spec.md)."""

from __future__ import annotations

from datetime import datetime, timezone

from nearpilot.iot.smartthings.heartbeat import SmartThingsHeartbeat
from nearpilot.iot.smartthings.transport import SmartThingsTransportError
from nearpilot.shared.enums import CommandState

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


class FakeSink:
    def __init__(self):
        self.statuses = []

    def on_status(self, status):
        self.statuses.append(status)


class FakeTransport:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def get_attribute(self, device_id, capability, attribute):
        self.calls.append((device_id, capability, attribute))
        if self.error is not None:
            raise self.error
        return "on"

    def post_command(self, *args, **kwargs):  # pragma: no cover - not used by heartbeat
        raise AssertionError("heartbeat must not send commands")


def make_heartbeat(transport: FakeTransport, sink: FakeSink) -> SmartThingsHeartbeat:
    return SmartThingsHeartbeat(transport, "device-123", "display-1", "session-abc", sink, clock=lambda: NOW)


def test_FR13_heartbeat_success_calls_on_status():
    transport = FakeTransport()
    sink = FakeSink()
    heartbeat = make_heartbeat(transport, sink)

    status = heartbeat.poll_once()

    assert status is not None
    assert sink.statuses == [status]
    assert status.node_id == "display-1"
    assert status.session_id == "session-abc"
    assert status.command_state is CommandState.IDLE
    assert status.observed_at == NOW
    assert transport.calls == [("device-123", "switch", "switch")]


def test_FR13_heartbeat_failure_skips_on_status():
    transport = FakeTransport(error=SmartThingsTransportError("offline"))
    sink = FakeSink()
    heartbeat = make_heartbeat(transport, sink)

    status = heartbeat.poll_once()

    assert status is None
    assert sink.statuses == []


def test_FR13_heartbeat_seq_increments_across_calls():
    transport = FakeTransport()
    sink = FakeSink()
    heartbeat = make_heartbeat(transport, sink)

    first = heartbeat.poll_once()
    second = heartbeat.poll_once()

    assert second.seq == first.seq + 1
