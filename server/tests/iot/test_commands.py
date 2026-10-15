"""Command sending, result waits and safety-check requests with a transport fake."""

from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest

from nearpilot.iot.mqtt.commands import MqttCommandGateway
from nearpilot.iot.mqtt.connection import IncomingReport, NotReady, PublishUncertain
from nearpilot.iot.mqtt.delivery import DeliveryConfig, ReportDelivery
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, CommandOutcome, NodeResultState
from nearpilot.shared.interfaces import NodeCommandGateway, SafetyCheckGateway
from nearpilot.shared.models import CheckCriteria, NodeCommand, SafetyCheckRequest

S1 = "11111111-1111-1111-1111-111111111111"
EVENT = "87654321-4321-4321-4321-cba987654321"
T0 = datetime(2026, 10, 16, 3, 0, 0, tzinfo=timezone.utc)
COMMAND = NodeCommand("command-1", "locker-1", Action.OPEN, S1, T0, T0 + timedelta(seconds=3))
CHECK = SafetyCheckRequest("check-1", "locker-1", S1, CheckCriteria("locker-return", 1),
                           T0, T0 + timedelta(seconds=3))


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def result(state="succeeded", *, node="locker-1", command_id="command-1", event=EVENT, seq=1,
           error_code=None):
    data = dict(schema_version=1, node_id=node, session_id=S1, seq=seq, sent_at=iso(T0),
                command_id=command_id, event_id=event, state=state, occurred_at=iso(T0))
    if error_code:
        data["error_code"] = error_code
    return IncomingReport(f"nearpilot/node/{node}/result", json.dumps(data).encode(),
                          1, False, False, T0 + timedelta(seconds=0.1))


def event(n):
    return f"87654321-4321-4321-4321-{n:012d}"


class Sink:
    def __init__(self):
        self.calls = []
        self.fail = False

    def _call(self, method, report):
        if self.fail:
            raise RuntimeError("CORE unavailable")
        self.calls.append((method, report))

    def on_announce(self, report):
        self._call("on_announce", report)

    def on_rssi(self, report):
        self._call("on_rssi", report)

    def on_status(self, report):
        self._call("on_status", report)

    def on_result(self, report):
        self._call("on_result", report)

    def on_safety_check_result(self, report):
        self._call("on_safety_check_result", report)


class Connection:
    """publish_request fake. on_publish runs inside the publish call."""

    def __init__(self):
        self.publications = []
        self.error = None
        self.on_publish = None

    def publish_request(self, node_id, kind, payload):
        self.publications.append((node_id, kind, payload))
        if self.error:
            raise self.error
        if self.on_publish:
            self.on_publish(node_id, kind, payload)
        return object()


@pytest.fixture
def env():
    sink, connection = Sink(), Connection()
    delivery = ReportDelivery(sink, settings=Settings,
                              config=DeliveryConfig({"locker-1": 0.0}, 100, 0.05, 2.0),
                              clock=lambda: T0 + timedelta(seconds=1))
    gateway = MqttCommandGateway(connection, delivery)
    return sink, connection, delivery, gateway


def in_thread(fn, *args):
    box = {}

    def run():
        try:
            box["value"] = fn(*args)
        except Exception as error:
            box["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_FR06_gateway_implements_shared_contracts(env):
    gateway = env[3]
    assert isinstance(gateway, NodeCommandGateway)
    assert isinstance(gateway, SafetyCheckGateway)


def test_FR06_no_publish_without_core_call(env):
    sink, connection, delivery, gateway = env
    delivery.process(result())
    delivery.process(result("unknown", event=event(2), seq=2))
    gateway.on_connection_lost()
    delivery.deliver_due()
    assert connection.publications == []
    assert len(sink.calls) == 2


def test_FR06_concurrent_same_command(env):
    _, connection, delivery, gateway = env
    first, a = in_thread(gateway.send, COMMAND, 5)
    assert wait_for(lambda: len(connection.publications) == 1)
    second, b = in_thread(gateway.send, COMMAND, 5)
    time.sleep(0.05)
    delivery.process(result())
    first.join(2)
    second.join(2)
    assert len(connection.publications) == 1
    assert a["value"].outcome == b["value"].outcome == CommandOutcome.SUCCEEDED
    assert a["value"].report == b["value"].report


def test_FR06_changed_command_not_republished(env):
    _, connection, delivery, gateway = env
    changed = NodeCommand("command-1", "locker-1", Action.OPEN, S1, T0, T0 + timedelta(seconds=9))
    waiting, box = in_thread(gateway.send, COMMAND, 5)
    assert wait_for(lambda: len(connection.publications) == 1)
    with pytest.raises(ValueError):
        gateway.send(changed, 5)
    delivery.process(result())
    waiting.join(2)
    assert box["value"].outcome == CommandOutcome.SUCCEEDED
    with pytest.raises(ValueError):
        gateway.send(changed, 5)
    assert len(connection.publications) == 1
    # A later retransmission of the same content keeps ID, content and expiry.
    connection.on_publish = lambda *_: delivery.process(result(seq=2))
    assert gateway.send(COMMAND, 5).outcome == CommandOutcome.SUCCEEDED
    assert len(connection.publications) == 2
    assert connection.publications[0] == connection.publications[1]


def test_FR06_definite_not_sent_before_publish(env):
    _, connection, _, gateway = env
    connection.error = NotReady("not ready")
    outcome = gateway.send(COMMAND, 5)
    assert (outcome.outcome, outcome.report) == (CommandOutcome.NOT_SENT, None)
    connection.error = None
    invalid = NodeCommand("command-2", "locker/1", Action.OPEN, S1, T0, T0 + timedelta(seconds=3))
    assert gateway.send(invalid, 5).outcome == CommandOutcome.NOT_SENT
    assert [kind for _, kind, _ in connection.publications] == ["cmd"]


def test_FR06_saturation_not_sent(env):
    sink, connection, _, _ = env
    sink.fail = True
    delivery = ReportDelivery(sink, settings=Settings,
                              config=DeliveryConfig({"locker-1": 0.0}, 1, 0.05, 2.0),
                              clock=lambda: T0 + timedelta(seconds=1))
    gateway = MqttCommandGateway(connection, delivery)
    delivery.process(result())
    assert delivery.saturated
    assert gateway.send(COMMAND, 5).outcome == CommandOutcome.NOT_SENT
    assert connection.publications == []
    gateway.request_check(CHECK)
    assert [kind for _, kind, _ in connection.publications] == ["safety_check"]


def test_FR07_disconnect_timeout_unknown(env):
    _, connection, _, gateway = env
    connection.error = PublishUncertain("unknown")
    started = time.monotonic()
    uncertain = gateway.send(COMMAND, 5)
    assert (uncertain.outcome, uncertain.report) == (CommandOutcome.UNKNOWN, None)
    assert time.monotonic() - started < 1
    connection.error = None
    assert gateway.send(COMMAND, 0.05).outcome == CommandOutcome.UNKNOWN
    waiting, box = in_thread(gateway.send, COMMAND, 5)
    assert wait_for(lambda: len(connection.publications) == 3)
    started = time.monotonic()
    gateway.on_connection_lost()
    waiting.join(2)
    assert time.monotonic() - started < 1
    assert (box["value"].outcome, box["value"].report) == (CommandOutcome.UNKNOWN, None)


def test_FR07_progress_not_success(env):
    _, connection, delivery, gateway = env

    def progress(*_):
        delivery.process(result("accepted", event=event(1)))
        delivery.process(result("running", event=event(2), seq=2))

    connection.on_publish = progress
    started = time.monotonic()
    outcome = gateway.send(COMMAND, 0.2)
    assert outcome.outcome == CommandOutcome.UNKNOWN
    assert outcome.report is None
    assert time.monotonic() - started < 1  # Progress did not extend the deadline.


def test_FR07_fast_result_not_lost(env):
    _, connection, delivery, gateway = env
    connection.on_publish = lambda *_: delivery.process(result())
    outcome = gateway.send(COMMAND, 5)
    assert outcome.outcome == CommandOutcome.SUCCEEDED
    assert outcome.report.event_id == EVENT


def test_FR07_result_correlation(env):
    _, connection, delivery, gateway = env
    other = NodeCommand("command-2", "locker-1", Action.OPEN, S1, T0, T0 + timedelta(seconds=3))
    first, a = in_thread(gateway.send, COMMAND, 0.5)
    second, b = in_thread(gateway.send, other, 5)
    assert wait_for(lambda: len(connection.publications) == 2)
    delivery.process(result(node="light-1"))  # Same command ID from another node.
    delivery.process(result(command_id="command-2", event=event(2)))
    second.join(2)
    first.join(2)
    assert b["value"].outcome == CommandOutcome.SUCCEEDED
    assert b["value"].report.command_id == "command-2"
    assert (a["value"].outcome, a["value"].report) == (CommandOutcome.UNKNOWN, None)


@pytest.mark.parametrize("state,error_code,expected", [
    ("rejected", "DEVICE_BUSY", CommandOutcome.REJECTED),
    ("rejected", "SESSION_MISMATCH", CommandOutcome.FAILED),
    ("rejected", "COMMAND_ID_CONFLICT", CommandOutcome.FAILED),
    ("failed", None, CommandOutcome.FAILED),
    ("unknown", None, CommandOutcome.UNKNOWN),
    ("succeeded", None, CommandOutcome.SUCCEEDED),
])
def test_FR07_rejection_mapping_preserves_report(env, state, error_code, expected):
    sink, connection, delivery, gateway = env
    connection.on_publish = lambda *_: delivery.process(result(state, error_code=error_code))
    outcome = gateway.send(COMMAND, 5)
    assert outcome.outcome == expected
    assert outcome.report.state == NodeResultState(state)
    assert outcome.report.error_code == error_code
    delivery.deliver_due()
    assert sink.calls == [("on_result", outcome.report)]


def test_FR07_wait_and_sink_no_deadlock():
    release, entered = threading.Event(), threading.Event()

    class BlockingSink(Sink):
        def on_result(self, report):
            entered.set()
            release.wait(5)
            super().on_result(report)

    sink, connection = BlockingSink(), Connection()
    delivery = ReportDelivery(sink, settings=Settings,
                              config=DeliveryConfig({"locker-1": 0.0}, 100, 0.05, 2.0))
    gateway = MqttCommandGateway(connection, delivery)
    delivery.start()
    try:
        delivery.submit(result(command_id="command-0", event=event(9)))
        assert entered.wait(2)  # CORE handover is now blocked on a device lock.
        connection.on_publish = lambda *_: delivery.submit(result())
        outcome = gateway.send(COMMAND, 2)
        assert outcome.outcome == CommandOutcome.SUCCEEDED
        assert sink.calls == []
    finally:
        release.set()
        delivery.stop()


def test_FR07_late_result_subscription_remains(env):
    sink, _, delivery, gateway = env
    assert gateway.send(COMMAND, 0.05).outcome == CommandOutcome.UNKNOWN
    delivery.process(result())
    delivery.deliver_due()
    assert [(m, r.command_id) for m, r in sink.calls] == [("on_result", "command-1")]


@pytest.mark.parametrize("error", [NotReady("not ready"), PublishUncertain("unknown")])
def test_FR20_check_send_failure_not_passed(env, error):
    _, connection, _, gateway = env
    connection.error = error
    with pytest.raises(type(error)):
        gateway.request_check(CHECK)
    assert len(connection.publications) == 1  # No self-made retry.
    connection.error = None
    changed = SafetyCheckRequest("check-1", "locker-1", S1, CheckCriteria("locker-return", 1),
                                 T0, T0 + timedelta(seconds=9))
    with pytest.raises(ValueError):
        gateway.request_check(changed)
    assert len(connection.publications) == 1


def test_FR20_request_check_does_not_wait(env):
    sink, connection, delivery, gateway = env
    started = time.monotonic()
    assert gateway.request_check(CHECK) is None
    assert time.monotonic() - started < 0.5
    assert [(node, kind) for node, kind, _ in connection.publications] == [
        ("locker-1", "safety_check")]
    data = dict(schema_version=1, node_id="locker-1", session_id=S1, seq=1,
                sent_at=iso(T0 + timedelta(seconds=0.5)), check_id="check-1", event_id=EVENT,
                criteria_id="locker-return", criteria_version=1,
                checked_at=iso(T0 + timedelta(seconds=0.5)), outcome="passed",
                evidence={name: dict(value=True, observed_at=iso(T0 + timedelta(seconds=0.5)))
                          for name in ("door_closed", "locked")})
    delivery.process(IncomingReport("nearpilot/node/locker-1/safety_check_result",
                                    json.dumps(data).encode(), 1, False, False,
                                    T0 + timedelta(seconds=0.6)))
    delivery.deliver_due()
    assert [m for m, _ in sink.calls] == ["on_safety_check_result"]


def test_NFR08_command_and_check_payload_minimal(env):
    _, connection, delivery, gateway = env
    connection.on_publish = lambda node, kind, payload: (
        delivery.process(result()) if kind == "cmd" else None)
    gateway.send(COMMAND, 5)
    gateway.request_check(CHECK)
    command, check = (json.loads(payload) for _, _, payload in connection.publications)
    common = {"schema_version", "node_id", "target_session_id", "issued_at", "expires_at"}
    assert set(command) == common | {"command_id", "action"}
    assert set(check) == common | {"check_id", "criteria_id", "criteria_version"}
    assert command["expires_at"] == iso(COMMAND.expires_at)
