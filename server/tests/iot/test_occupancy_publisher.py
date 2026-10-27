"""Retained occupancy display: mirrors stored rental states, never decides them."""

import json
from dataclasses import replace
from datetime import datetime, timezone

from nearpilot.iot.mqtt.connection import NotReady, PublishUncertain
from nearpilot.iot.mqtt.occupancy import OccupancyPublisher
from nearpilot.shared.enums import Capability, DeviceState, Occupancy, TrustState
from nearpilot.shared.models import NodeInfo

NOW = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)


def node(node_id="locker-1", state=DeviceState.AVAILABLE, **changes):
    info = NodeInfo(node_id, "lab", Capability.LOCKER, Occupancy.RENTAL, TrustState.TRUSTED, state,
                    None, NOW)
    return replace(info, **changes)


class FakeConnection:
    def __init__(self):
        self.ready = True
        self.sent = []
        self.error = None

    def publish_request(self, node_id, kind, payload):
        if self.error:
            raise self.error
        self.sent.append((node_id, kind, json.loads(payload)["device_state"]))


def publisher(nodes):
    connection = FakeConnection()
    return OccupancyPublisher(connection, lambda: nodes, clock=lambda: NOW), connection


def test_FR19_occupancy_published_once_per_state_change():
    nodes = [node()]
    occupancy, connection = publisher(nodes)
    occupancy.tick()
    occupancy.tick()
    nodes[0] = node(state=DeviceState.ACTIVE)
    occupancy.tick()
    assert connection.sent == [("locker-1", "occupancy", "AVAILABLE"),
                               ("locker-1", "occupancy", "ACTIVE")]


def test_FR19_occupancy_only_for_trusted_rental_nodes():
    occupancy, connection = publisher([
        node("light-1", occupancy=Occupancy.SHARED, capability=Capability.LIGHT),
        node("locker-2", trust_state=TrustState.DISCOVERED),
        node("locker-3", trust_state=TrustState.REVOKED),
        node("locker-4", state=DeviceState.RESERVED)])
    occupancy.tick()
    assert connection.sent == [("locker-4", "occupancy", "RESERVED")]


def test_FR19_occupancy_republished_after_connection_loss():
    occupancy, connection = publisher([node(state=DeviceState.ACTIVE)])
    occupancy.tick()
    occupancy.on_connection_lost()
    occupancy.tick()
    connection.ready = False
    occupancy.tick()
    connection.ready = True
    occupancy.tick()
    assert [state for *_, state in connection.sent] == ["ACTIVE"] * 3


def test_FR19_unconfirmed_occupancy_publish_retried():
    occupancy, connection = publisher([node(), node("locker-2")])
    connection.error = PublishUncertain("unknown")
    occupancy.tick()
    connection.error = None
    occupancy.tick()
    assert [node_id for node_id, *_ in connection.sent] == ["locker-1", "locker-2"]


def test_FR19_not_ready_stops_occupancy_round():
    occupancy, connection = publisher([node(), node("locker-2")])
    connection.error = NotReady("not ready")
    occupancy.tick()
    connection.error = None
    occupancy.tick()
    assert len(connection.sent) == 2
