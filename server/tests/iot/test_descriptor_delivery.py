"""FR-04 거부한 발표의 감사와 FR-06 연결별 선언으로 하는 송신 전 검증 (specs/iot-device-descriptor/spec.md)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.iot.mqtt.commands import MqttCommandGateway
from nearpilot.iot.mqtt.connection import IncomingReport
from nearpilot.iot.mqtt.delivery import DeliveryConfig, ReportDelivery
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Action, Capability, CommandOutcome, Occupancy, Step
from nearpilot.shared.models import LightSetting, NodeCommand

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
T0 = datetime(2026, 11, 6, 3, 0, 0, tzinfo=timezone.utc)
RING = {"brightness_max": 64, "color": True}


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def report(kind, data, node="light-1"):
    return IncomingReport(f"nearpilot/node/{node}/{kind}", json.dumps(data, ensure_ascii=False).encode(),
                          1, kind == "announce", False, T0 + timedelta(seconds=0.1))


def v2_announce(*, session=S1, seq=1, controls=None):
    data = dict(schema_version=2, node_id="light-1", session_id=session, seq=seq, sent_at=iso(T0),
                capability="light", occupancy="shared")
    if controls is not None:
        data["controls"] = controls
    return report("announce", data)


def v3_announce(*, session=S1, seq=1, descriptor=None):
    if descriptor is None:
        descriptor = legacy_descriptor(Capability.LIGHT, Occupancy.SHARED, **RING).to_json()
    return report("announce", dict(schema_version=3, node_id="light-1", session_id=session, seq=seq,
                                   sent_at=iso(T0), descriptor=descriptor))


def result(command_id="command-1", session=S1):
    return report("result", dict(schema_version=2, node_id="light-1", session_id=session, seq=9,
                                 sent_at=iso(T0), command_id=command_id,
                                 event_id="87654321-4321-4321-4321-cba987654321", state="succeeded",
                                 occurred_at=iso(T0)))


class Sink:
    def __init__(self):
        self.calls = []

    def on_announce(self, item):
        self.calls.append(("on_announce", item))

    def on_rssi(self, item):
        self.calls.append(("on_rssi", item))

    def on_status(self, item):
        self.calls.append(("on_status", item))

    def on_result(self, item):
        self.calls.append(("on_result", item))


class Audit:
    def __init__(self):
        self.events = []
        self.fail = False

    def append(self, event):
        if self.fail:
            raise RuntimeError("audit down")
        self.events.append(event)


class Connection:
    def __init__(self, delivery=None):
        self.publications = []
        self.delivery = delivery

    def publish_request(self, node_id, kind, payload):
        self.publications.append(json.loads(payload))
        if self.delivery is not None:
            self.delivery.process(result(self.publications[-1]["command_id"]))
        return object()


@pytest.fixture
def world():
    sink, audit = Sink(), Audit()
    delivery = ReportDelivery(sink, settings=Settings, config=DeliveryConfig(0.0, 120, 60, 100, 0.05, 2.0),
                              clock=lambda: T0 + timedelta(seconds=1), audit=audit)
    connection = Connection(delivery)
    gateway = MqttCommandGateway(connection, delivery)
    return sink, audit, delivery, connection, gateway


def command(command_id="command-1", *, session=S1, action=Action.ON, light=None):
    return NodeCommand(command_id, "light-1", action, session, T0, T0 + timedelta(seconds=3), light=light)


def test_FR04_rejected_announce_audited_once(world):
    _, audit, delivery, _, _ = world
    bad = legacy_descriptor(Capability.LIGHT, Occupancy.SHARED).to_json()
    bad["actions"][1].pop("role")  # shared 기기에 deactivate 가 없다
    for _ in range(3):  # retained 발표가 다시 와도 감사는 한 번이다 (spec 결정 12)
        delivery.process(v3_announce(descriptor=bad))
    event, = audit.events
    assert event.step is Step.ANNOUNCE
    assert event.payload["node_id"] == "light-1" and event.payload["session_id"] == S1
    assert "deactivate" in event.payload["reason"]
    assert "descriptor" not in event.payload  # 원문은 남기지 않는다
    delivery.process(v3_announce(session=S2, descriptor=bad))  # 새 연결은 다시 남긴다
    assert len(audit.events) == 2
    delivery.process(report("announce", {"schema_version": 3}, node="light-1"))  # 연결 ID 없는 거부
    assert audit.events[-1].payload["session_id"] is None


def test_FR04_rejected_announce_audit_failure_does_not_stop_processing(world):
    sink, audit, delivery, _, _ = world
    audit.fail = True
    delivery.process(v3_announce(descriptor={"format": "nearpilot.device/1"}))
    audit.fail = False
    delivery.process(v3_announce(descriptor={"format": "nearpilot.device/1"}))  # 실패한 감사는 다시 남긴다
    assert len(audit.events) == 1
    delivery.process(v3_announce(seq=2))
    delivery.deliver_due()
    assert [method for method, _ in sink.calls] == ["on_announce"]


def test_FR06_announce_for_tracks_latest_per_connection(world):
    _, _, delivery, _, _ = world
    assert delivery.announce_for("light-1", S1) is None
    delivery.process(v2_announce(seq=2, controls=RING))
    delivery.process(v2_announce(seq=1))  # 늦게 온 작은 순번은 마지막 발표가 아니다
    assert delivery.announce_for("light-1", S1).seq == 2
    delivery.process(v3_announce(session=S2))
    assert delivery.announce_for("light-1", S1).schema_version == 2
    assert delivery.announce_for("light-1", S2).schema_version == 3


def test_FR06_command_outside_descriptor_not_sent(world):
    _, _, delivery, connection, gateway = world
    delivery.process(v3_announce())
    over = command(light=LightSetting(65, "#FFFFFF"))  # 선언 상한 64를 넘는다
    assert gateway.send(over, 1).outcome is CommandOutcome.NOT_SENT
    undeclared = command("command-2", action=Action.OPEN)
    assert gateway.send(undeclared, 1).outcome is CommandOutcome.NOT_SENT
    assert connection.publications == []
    ok = command("command-3", light=LightSetting(64, "#FFFFFF"))
    assert gateway.send(ok, 1).outcome is CommandOutcome.SUCCEEDED
    assert len(connection.publications) == 1


def test_FR06_command_without_announce_not_sent(world):
    _, _, delivery, connection, gateway = world
    delivery.process(v3_announce(session=S2))  # 다른 연결의 발표만 있다
    assert gateway.send(command(), 1).outcome is CommandOutcome.NOT_SENT
    assert connection.publications == []


def test_FR06_command_follows_announced_version(world):
    """기기가 마지막으로 발표한 버전으로 cmd 를 보낸다 (#245 담당자 결정)."""
    _, _, delivery, connection, gateway = world
    delivery.process(v2_announce(controls=RING))
    delivery.process(v3_announce(session=S2))
    setting = LightSetting(30, "#00FF00")
    assert gateway.send(command("command-1", light=setting), 1).outcome is CommandOutcome.SUCCEEDED
    assert gateway.send(command("command-2", session=S2, light=setting), 1).outcome is CommandOutcome.SUCCEEDED
    v2, v3 = connection.publications
    assert (v2["schema_version"], v2["parameters"]) == (2, {"brightness": 30, "color": "#00FF00"})
    assert (v3["schema_version"], v3["arguments"]) == (3, {"brightness": 30, "color": "#00FF00"})
