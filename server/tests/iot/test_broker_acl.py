"""Real localhost Mosquitto tests; no public broker or production credentials."""

from queue import Queue
import secrets
import threading
import time

import paho.mqtt.client as mqtt
import pytest

from nearpilot.iot.mqtt.connection import MqttConfig, MqttConnection, NotReady

# The `broker` fixture lives in tests/iot/conftest.py.


class Peer:
    def __init__(self, broker, user):
        self.messages = Queue()
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.reason = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"test-{secrets.token_hex(8)}", clean_session=True, protocol=mqtt.MQTTv311)
        self.client.on_connect = self._connected
        self.client.on_subscribe = lambda *args: self.subscribed.set()
        self.client.on_message = lambda client, userdata, message: self.messages.put(
            (message.topic, bytes(message.payload)))
        if user is not None:
            self.client.username_pw_set(user, broker.passwords[user])
        self.client.connect("127.0.0.1", broker.port, keepalive=5)
        self.client.loop_start()
        assert self.connected.wait(3), "Test peer did not receive CONNACK"

    def _connected(self, client, userdata, flags, reason, properties):
        self.reason = reason
        self.connected.set()

    def subscribe(self, topics):
        self.subscribed.clear()
        rc, _ = self.client.subscribe([(topic, 1) for topic in topics])
        assert rc == 0 and self.subscribed.wait(3)

    def publish(self, topic, payload=b"marker", retain=False):
        receipt = self.client.publish(topic, payload, qos=1, retain=retain)
        receipt.wait_for_publish(timeout=3)
        assert receipt.is_published()

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


@pytest.fixture
def peers(broker):
    created = []
    def create(user):
        peer = Peer(broker, user)
        created.append(peer)
        return peer
    yield create
    for peer in created:
        peer.close()


def until_marker(peer, marker_topic):
    received = []
    while True:
        message = peer.messages.get(timeout=3)
        received.append(message)
        if message[0] == marker_topic:
            return received


def test_NFR08_anonymous_connection_denied(peers):
    assert peers(None).reason.is_failure


@pytest.mark.parametrize("kind", ["announce", "rssi", "status", "result"])
def test_NFR08_cross_node_report_write_denied(peers, kind):
    server, node = peers("nearpilot-server"), peers("node-a")
    server.subscribe([f"nearpilot/node/+/{kind}"])
    own = f"nearpilot/node/node-a/{kind}"
    node.publish(f"nearpilot/node/node-b/{kind}", b"forged")
    node.publish(own, b"own-marker")
    assert until_marker(server, own) == [(own, b"own-marker")]


@pytest.mark.parametrize("kind", ["cmd"])
def test_NFR08_cross_node_command_read_denied(peers, kind):
    server, node = peers("nearpilot-server"), peers("node-a")
    own = f"nearpilot/node/node-a/{kind}"
    other = f"nearpilot/node/node-b/{kind}"
    node.subscribe([own, other, f"nearpilot/node/+/{kind}"])
    server.publish(other, b"other-private-command")
    server.publish(own, b"own-marker")
    received = until_marker(node, own)
    assert all(topic == own and payload == b"own-marker" for topic, payload in received)
    # MQTT 3.1.1 may accept a subscription filter while filtering each message.


@pytest.mark.parametrize("kind", ["cmd"])
def test_FR06_only_server_publishes_commands(peers, kind):
    server, node = peers("nearpilot-server"), peers("node-a")
    topic = f"nearpilot/node/node-a/{kind}"
    node.subscribe([topic])
    node.publish(topic, b"unapproved-command")
    server.publish(topic, b"core-command-marker")
    # Same destination stream: the server marker proves the read path works.
    assert until_marker(node, topic) == [(topic, b"core-command-marker")]


def test_NFR08_server_cannot_forge_reports(peers):
    server, node = peers("nearpilot-server"), peers("node-a")
    topic = "nearpilot/node/node-a/status"
    server.subscribe([topic])
    server.publish(topic, b"forged-report")
    node.publish(topic, b"node-marker")
    assert until_marker(server, topic) == [(topic, b"node-marker")]


def test_NFR12_local_broker_without_internet(broker, peers):
    reports, losses = Queue(), Queue()
    node = peers("node-a")
    node.publish("nearpilot/node/node-a/announce", b"retained-discovery", retain=True)
    config = MqttConfig("127.0.0.1", broker.port, "gateway-test", "nearpilot-server",
                        broker.secret_file, 5, 1, 3, 3, 1, 1)
    connection = MqttConnection(config, on_report=reports.put,
                                on_connection_lost=lambda: losses.put("lost"))
    try:
        connection.start()
        assert connection.ready
        discovery = reports.get(timeout=3)
        assert discovery.retain and discovery.payload == b"retained-discovery"
        assert discovery.received_at.utcoffset().total_seconds() == 0
        node.subscribe(["nearpilot/node/node-a/cmd"])
        receipt = connection.publish_request("node-a", "cmd", b"encoded-core-command")
        receipt.wait_for_publish(timeout=3)
        assert node.messages.get(timeout=3) == ("nearpilot/node/node-a/cmd", b"encoded-core-command")
        broker.stop()
        assert losses.get(timeout=3) == "lost"
        assert not connection.ready
        with pytest.raises(NotReady):
            connection.publish_request("node-a", "cmd", b"must-not-send")
        broker.start()
        deadline = time.monotonic() + 5
        while (not connection.ready or not node.client.is_connected()) and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        assert connection.ready and node.client.is_connected()
        node.publish("nearpilot/node/node-a/status", b"after-reconnect")
        assert reports.get(timeout=3).payload == b"after-reconnect"
    finally:
        connection.stop()
