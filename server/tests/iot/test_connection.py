"""Connection failures and races with a controlled Paho fake."""

from dataclasses import replace
from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest

from nearpilot.iot.mqtt.connection import (
    ConnectionFailure, IncomingReport, MqttConfig, MqttConnection, NotReady,
    PublishUncertain, SUBSCRIPTIONS,
)


class FakeClient:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.subscriptions = []
        self.publications = []
        self.mid = 0
        self.auth_reason = 0
        self.granted = [1, 0, 1, 1, 1]
        self.subscribe_rc = 0
        self.publish_rc = 0
        self.publish_exception = None
        self.subscribe_exception = None
        self.loop_rc = 0
        self.auto_ack = True
        self.subscribed = threading.Event()
        self.close_gate = threading.Event()
        self.close_gate.set()
        self.disconnected = False
        self.stopped = False

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def reconnect_delay_set(self, minimum, maximum):
        self.backoff = (minimum, maximum)

    def connect_async(self, host, port, keepalive):
        self.address = (host, port, keepalive)

    def loop_start(self):
        if self.loop_rc:
            return self.loop_rc
        def connect_and_ack():
            self.on_connect(self, None, None, self.auth_reason, None)
            if self.auto_ack and self.subscriptions:
                self.ack()
        threading.Thread(target=connect_and_ack).start()
        return 0

    def subscribe(self, topics):
        if self.subscribe_exception:
            raise self.subscribe_exception
        self.mid += 1
        self.subscriptions.append(topics)
        self.subscribed.set()
        return self.subscribe_rc, self.mid

    def ack(self, mid=None, grants=None):
        self.on_subscribe(self, None, self.mid if mid is None else mid,
                          self.granted if grants is None else grants, None)

    def publish(self, topic, payload, qos, retain):
        self.publications.append((topic, payload, qos, retain))
        if self.publish_exception:
            raise self.publish_exception
        return SimpleNamespace(rc=self.publish_rc, mid=len(self.publications))

    def disconnect(self):
        self.disconnected = True
        self.on_disconnect(self, None, None, 0, None)
        return 0

    def loop_stop(self):
        self.close_gate.wait()
        self.stopped = True
        return 0


@pytest.fixture
def setup_connection(tmp_path):
    secret = tmp_path / ".env.mqtt-password"
    secret.write_text("test-private-password\n", encoding="utf-8")
    config = MqttConfig("localhost", 1883, "test-server", "nearpilot-server", secret,
                        30, 1, 1, 1, 1, 4)
    reports, losses, clients = [], [], []
    def factory(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client
    stamp = datetime(2026, 10, 16, tzinfo=timezone.utc)
    connection = MqttConnection(config, on_report=reports.append,
                                on_connection_lost=lambda: losses.append("lost"),
                                client_factory=factory, clock=lambda: stamp)
    yield connection, clients[0], reports, losses, stamp
    clients[0].close_gate.set()
    connection.stop()


def test_NFR11_mqtt_version_qos_retain(setup_connection):
    connection, client, *_ = setup_connection
    connection.start()
    assert connection.ready
    assert client.options == dict(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="test-server", clean_session=True,
                                  protocol=mqtt.MQTTv311, transport="tcp")
    assert client.subscriptions == [list(SUBSCRIPTIONS)]
    assert client.address == ("localhost", 1883, 30)
    assert client.connect_timeout == 1
    assert client.backoff == (1, 4)
    assert client.credentials == ("nearpilot-server", "test-private-password")
    assert not client.publications
    for kind in ("cmd", "safety_check"):
        receipt = connection.publish_request("node-a", kind, b"{}")
        assert receipt.rc == 0
    assert client.publications == [(f"nearpilot/node/node-a/{kind}", b"{}", 1, False)
                                    for kind in ("cmd", "safety_check")]


def test_FR13_readiness_waits_for_suback(setup_connection):
    connection, client, *_ = setup_connection
    client.auto_ack = False
    completed = threading.Event()
    worker = threading.Thread(target=lambda: (connection.start(), completed.set()))
    worker.start()
    assert client.subscribed.wait(1)
    assert not connection.ready
    assert not completed.is_set()
    with pytest.raises(NotReady):
        connection.publish_request("node-a", "cmd", b"{}")
    client.ack(mid=999)
    assert not connection.ready
    client.ack()
    worker.join(1)
    assert completed.is_set() and connection.ready


def test_FR13_subscription_timeout_cleans_up(setup_connection):
    connection, client, *_ = setup_connection
    connection.config = replace(connection.config, start_timeout_sec=0.01)
    client.auto_ack = False
    with pytest.raises(ConnectionFailure, match="readiness timed out"):
        connection.start()
    assert not connection.ready and client.disconnected and client.stopped
    client.ack()
    assert not connection.ready


def test_FR07_stop_during_start_cancels_readiness(setup_connection):
    connection, client, *_ = setup_connection
    client.auto_ack = False
    failures = []
    def start():
        try:
            connection.start()
        except ConnectionFailure as error:
            failures.append(error)
    worker = threading.Thread(target=start)
    worker.start()
    assert client.subscribed.wait(1)
    connection.stop()
    client.ack()
    worker.join(1)
    assert not worker.is_alive() and len(failures) == 1
    assert not connection.ready and client.stopped


@pytest.mark.parametrize("failure", ["auth", "subscribe", "exception", "grants", "missing", "downgrade", "loop"])
def test_NFR08_failed_auth_or_subscribe_not_ready(setup_connection, failure):
    connection, client, *_ = setup_connection
    if failure == "auth":
        client.auth_reason = 135
    elif failure == "subscribe":
        client.subscribe_rc = 4
    elif failure == "exception":
        client.subscribe_exception = RuntimeError("private information")
    elif failure == "grants":
        client.granted[-1] = 128
    elif failure == "missing":
        client.granted = [1]
    elif failure == "downgrade":
        client.granted = [0] * 5
    else:
        client.loop_rc = 1
    with pytest.raises(ConnectionFailure):
        connection.start()
    assert not connection.ready
    assert client.disconnected and client.stopped
    assert not client.publications


def test_FR13_reconnect_resubscribes(setup_connection):
    connection, client, reports, losses, _ = setup_connection
    connection.start()
    previous_mid = client.mid
    client.on_disconnect(client, None, None, 7, None)
    assert not connection.ready and losses == ["lost"]
    client.on_connect(client, None, None, 0, None)
    client.ack(mid=previous_mid)
    assert not connection.ready
    assert client.subscriptions == [list(SUBSCRIPTIONS)] * 2
    client.ack()
    assert connection.ready and reports == []


def test_FR13_retained_announce_not_online(setup_connection):
    connection, client, reports, _, stamp = setup_connection
    connection.start()
    client.on_message(client, None, SimpleNamespace(topic="nearpilot/node/node-a/announce",
        payload=b'{"node_id":"node-a"}', qos=1, retain=True, dup=True))
    assert reports == [IncomingReport("nearpilot/node/node-a/announce",
                       b'{"node_id":"node-a"}', 1, True, True, stamp)]
    # Only raw transport metadata: no online or safety state is constructed.
    assert not hasattr(connection, "nodes")


def test_FR13_retained_report_before_suback_preserved(setup_connection):
    connection, client, reports, *_ = setup_connection
    connection.start()
    client.on_disconnect(client, None, None, 7, None)
    client.on_connect(client, None, None, 0, None)
    client.on_message(client, None, SimpleNamespace(topic="nearpilot/node/node-a/announce",
        payload=b"{}", qos=1, retain=True, dup=False))
    assert not connection.ready and len(reports) == 1


@pytest.mark.parametrize("topic", ["nearpilot/node/node-a/cmd", "nearpilot/node/+/status",
                                    "other/node/node-a/status", "nearpilot/node//status"])
def test_NFR08_unexpected_topic_not_handed_off(setup_connection, topic):
    connection, client, reports, *_ = setup_connection
    connection.start()
    client.on_message(client, None, SimpleNamespace(topic=topic, payload=b"secret", qos=1,
                                                   retain=False, dup=False))
    assert reports == []


@pytest.mark.parametrize("failure", ["exception", "rc"])
def test_FR07_disconnect_keeps_uncertainty(setup_connection, failure):
    connection, client, _, losses, _ = setup_connection
    connection.start()
    if failure == "exception":
        client.publish_exception = OSError("credential leak")
    else:
        client.publish_rc = 4
    with pytest.raises(PublishUncertain):
        connection.publish_request("node-a", "cmd", b"{}")
    assert len(client.publications) == 1
    client.on_disconnect(client, None, None, 7, None)
    assert losses == ["lost"]
    with pytest.raises(NotReady):
        connection.publish_request("node-a", "cmd", b"{}")
    assert len(client.publications) == 1


def test_FR07_shutdown_does_not_claim_success(setup_connection):
    connection, client, reports, losses, _ = setup_connection
    connection.start()
    connection.publish_request("node-a", "cmd", b"{}")
    connection.stop()
    connection.stop()
    assert not connection.ready and losses == ["lost"] and reports == []
    assert client.disconnected and client.stopped
    with pytest.raises(NotReady):
        connection.publish_request("node-a", "cmd", b"{}")
    with pytest.raises(ConnectionFailure):
        connection.start()


def test_FR07_shutdown_wait_is_bounded(setup_connection):
    connection, client, *_ = setup_connection
    connection.config = replace(connection.config, stop_timeout_sec=0.01)
    connection.start()
    client.close_gate.clear()
    with pytest.raises(ConnectionFailure, match="shutdown timed out"):
        connection.stop()
    assert not connection.ready
    client.close_gate.set()
    assert connection._stop_done.wait(1)
    connection.stop()


def test_NFR08_failures_omit_secrets(setup_connection, caplog):
    connection, client, _, _, stamp = setup_connection
    connection._on_report = lambda report: (_ for _ in ()).throw(RuntimeError("secret-body"))
    connection._on_connection_lost = lambda: (_ for _ in ()).throw(RuntimeError("secret-loss"))
    connection.start()
    client.on_message(client, None, SimpleNamespace(topic="nearpilot/node/node-a/status",
        payload=b"secret-payload", qos=1, retain=False, dup=False))
    connection.stop()
    for secret in ("secret-body", "secret-loss", "secret-payload", "test-private-password"):
        assert secret not in caplog.text
    assert "report handoff failed" in caplog.text
    assert "connection-loss handoff failed" in caplog.text


def test_NFR08_invalid_password_file_stops_safely(setup_connection):
    connection, client, *_ = setup_connection
    connection.config.password_file.write_text("private\ninvalid", encoding="utf-8")
    with pytest.raises(ConnectionFailure, match="startup failed") as error:
        connection.start()
    assert "private" not in str(error.value)
    assert not connection.ready and client.stopped


def test_NFR08_missing_password_file_omits_path(setup_connection):
    connection, client, *_ = setup_connection
    connection.config = replace(connection.config, password_file=connection.config.password_file / "missing")
    with pytest.raises(ConnectionFailure, match="startup failed") as error:
        connection.start()
    assert str(connection.config.password_file) not in str(error.value)
    assert client.stopped


@pytest.mark.parametrize("field,value", [
    ("port", True), ("port", 0), ("port", 65536), ("host", ""), ("username", "\x00"),
    ("keepalive_sec", 0), ("keepalive_sec", 65536), ("start_timeout_sec", float("nan")),
    ("stop_timeout_sec", float("inf")), ("connect_timeout_sec", False),
    ("reconnect_min_sec", 0), ("reconnect_max_sec", 0), ("reconnect_min_sec", 5),
])
def test_NFR11_invalid_runtime_settings(setup_connection, field, value):
    connection, *_ = setup_connection
    with pytest.raises(ValueError):
        replace(connection.config, **{field: value})


@pytest.mark.parametrize("node,kind", [("node/+", "cmd"), ("#", "cmd"), ("", "cmd"),
                                      ("node-a", "status"), ("node-a", "announce")])
def test_FR06_transport_restricts_outgoing_topics(setup_connection, node, kind):
    connection, client, *_ = setup_connection
    connection.start()
    with pytest.raises(ValueError):
        connection.publish_request(node, kind, b"{}")
    assert client.publications == []
