"""Simulator ↔ IOT gateway over a real localhost Mosquitto, with a CORE fake.

CORE·DB assembly is #88's work; here NodeEventSink is a recording fake.
"""

from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from nearpilot.iot.mqtt import (
    DeliveryConfig, MqttCommandGateway, MqttConfig, MqttConnection, ReportDelivery,
)
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, CommandOutcome, NodeResultState
from nearpilot.shared.models import NodeCommand
from tools.node_sim import (
    Beacon, BrokerConfig, CommandStore, Credentials, Scenario, SimDevice, SimNode,
)

BEACON = "11111111-2222-3333-4444-555555555555"
SIM_SETTINGS = Settings(status_report_sec=0.2, rssi_frame_sec=0.2)


class Sink:
    def __init__(self):
        self.calls = []
        self.condition = threading.Condition()

    def _call(self, method, report):
        with self.condition:
            self.calls.append((method, report))
            self.condition.notify_all()

    def on_announce(self, report):
        self._call("on_announce", report)

    def on_rssi(self, report):
        self._call("on_rssi", report)

    def on_status(self, report):
        self._call("on_status", report)

    def on_result(self, report):
        self._call("on_result", report)

    def of(self, method, node="node-a"):
        with self.condition:
            return [r for m, r in self.calls if m == method and r.node_id == node]

    def wait(self, method, predicate=lambda r: True, node="node-a", timeout=5.0):
        def matches():
            return [r for m, r in self.calls
                    if m == method and r.node_id == node and predicate(r)]
        with self.condition:
            self.condition.wait_for(matches, timeout)
            found = matches()
        assert found, f"no matching {method} from {node}"
        return found[-1]


@pytest.fixture
def iot(broker):
    sink = Sink()
    delivery = ReportDelivery(sink, settings=Settings,
                              config=DeliveryConfig({"node-a": 0.0, "node-b": 0.0}, 1000, 0.1, 3.0))
    holder = {}
    connection = MqttConnection(
        MqttConfig("127.0.0.1", broker.port, "gateway-sim-test", "nearpilot-server",
                   broker.secret_file, 5, 1, 3, 3, 1, 1),
        on_report=delivery.submit,
        on_connection_lost=lambda: holder["gateway"].on_connection_lost())
    gateway = holder["gateway"] = MqttCommandGateway(connection, delivery)
    delivery.start()
    connection.start()
    try:
        yield sink, gateway, connection
    finally:
        connection.stop()
        delivery.stop()


@pytest.fixture
def sims(broker, tmp_path):
    created = []

    def create(node_id="node-a", *, account=None, capability="locker", store=None, **scenario):
        account = account or node_id
        secret = tmp_path / f".env.node-{account}"
        secret.write_text(broker.passwords[account], encoding="utf-8")
        node = SimNode(node_id, capability, "rental" if capability == "locker" else "shared",
                       CommandStore(tmp_path / f"{store or node_id}.sqlite3"),
                       Scenario(**({"actuation_sec": 0.1} | scenario)))
        device = SimDevice(node, BrokerConfig("127.0.0.1", broker.port, 5, 1, 2),
                           Credentials(account, secret), SIM_SETTINGS)
        created.append(device)
        device.start()
        return device

    yield create
    for device in created:
        device.stop()


def command(session, command_id="command-1", node="node-a", action=Action.OPEN):
    now = datetime.now(timezone.utc)
    return NodeCommand(command_id, node, action, session, now, now + timedelta(seconds=3))


def live_session(sink, device):
    # The device creates its session on CONNACK; read it when each status arrives.
    return sink.wait("on_status", lambda r: r.session_id == device.node.session_id).session_id


def test_NFR12_simulator_local_ap(iot, sims):
    sink, gateway, _ = iot
    device = sims(beacons=[Beacon(BEACON, 1, 1, -60)])
    session = live_session(sink, device)
    sink.wait("on_rssi", lambda r: r.session_id == session)
    sent = command(session)
    first = gateway.send(sent, 3)
    assert first.outcome == CommandOutcome.SUCCEEDED
    # FR-06: the same command over MQTT re-reports; it does not actuate again.
    again = gateway.send(sent, 3)
    assert again.outcome == CommandOutcome.SUCCEEDED
    assert again.report.event_id == first.report.event_id
    assert device.node.actuations == 1


def test_FR07_delayed_or_lost_result(iot, sims, broker):
    sink, gateway, connection = iot
    lost = sims(drop_final_result=True)
    session = live_session(sink, lost)
    outcome = gateway.send(command(session), 0.8)
    assert (outcome.outcome, outcome.report) == (CommandOutcome.UNKNOWN, None)
    assert {r.state for r in sink.of("on_result")} <= {
        NodeResultState.ACCEPTED, NodeResultState.RUNNING}
    lost.stop()

    delayed = sims(store="delayed", result_delay_sec=1.0)
    session = live_session(sink, delayed)
    outcome = gateway.send(command(session, "command-2"), 0.4)
    assert outcome.outcome == CommandOutcome.UNKNOWN
    late = sink.wait("on_result", lambda r: r.command_id == "command-2"
                     and r.state == NodeResultState.SUCCEEDED)
    assert late.session_id == session
    delayed.stop()

    slow = sims(store="slow", actuation_sec=5.0)
    session = live_session(sink, slow)
    holder = {}
    waiting = threading.Thread(target=lambda: holder.update(
        result=gateway.send(command(session, "command-3"), 10)))
    waiting.start()
    sink.wait("on_result", lambda r: r.command_id == "command-3"
              and r.state == NodeResultState.RUNNING)
    started = time.monotonic()
    broker.stop()  # Server connection lost after publishing.
    waiting.join(5)
    assert time.monotonic() - started < 3
    assert (holder["result"].outcome, holder["result"].report) == (CommandOutcome.UNKNOWN, None)
    broker.start()
    deadline = time.monotonic() + 5
    while not connection.ready and time.monotonic() < deadline:
        time.sleep(0.02)
    assert connection.ready


def test_FR07_restart_reports_unknown_over_broker(iot, sims):
    sink, gateway, _ = iot
    device = sims(store="power", actuation_sec=5.0)
    session = live_session(sink, device)
    sent = command(session)
    assert gateway.send(sent, 0.5).outcome == CommandOutcome.UNKNOWN
    device.stop()  # Power loss while running; the finish timer never fires.
    device.node.store.close()
    restarted = sims(store="power", actuation_sec=5.0)
    recovered = sink.wait("on_result", lambda r: r.state == NodeResultState.UNKNOWN)
    assert recovered.session_id == restarted.node.session_id != session
    assert restarted.node.actuations == 0


def test_FR04_reconnect_over_broker(iot, sims):
    sink, _, _ = iot
    first = sims(store="reconnect")
    old = live_session(sink, first)
    first.stop()
    second = sims(store="reconnect", beacons=[Beacon(BEACON, 1, 1, -60)])
    new = live_session(sink, second)
    assert new != old
    announce = sink.wait("on_announce", lambda r: r.session_id == new)
    assert announce.seq == 1
    assert sink.wait("on_rssi", lambda r: r.session_id == new).seq >= 1


def test_NFR08_misconfigured_node_blocked(iot, sims):
    sink, _, _ = iot
    sims("node-b", account="node-a", store="impostor")  # node-a's account, node-b's topics.
    good = sims("node-a")
    live_session(sink, good)
    time.sleep(1.0)
    assert [m for m, r in sink.calls if r.node_id == "node-b"] == []
