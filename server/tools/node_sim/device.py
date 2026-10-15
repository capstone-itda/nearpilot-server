"""MQTT client for one simulated device, with its own account (NFR-08, NFR-11).

Every connection gets a new session ID and announces first. Status and RSSI
periods come from Settings; nothing here is a code constant (FR-05).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import threading
import time
from uuid import uuid4

import paho.mqtt.client as mqtt

from nearpilot.shared.config import Settings
from tools.node_sim.node import Message, SimNode

log = logging.getLogger(__name__)

# architecture.md §5 MQTT 1절: (QoS, retain) of device → server topics.
PUBLISH = {"announce": (1, True), "rssi": (0, False), "status": (1, False),
           "result": (1, False), "safety_check_result": (1, False)}


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    keepalive_sec: int
    reconnect_min_sec: int
    reconnect_max_sec: int

    def __post_init__(self):
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("Invalid broker setting: host")
        for name, low, high in (("port", 1, 65535), ("keepalive_sec", 1, 65535),
                                ("reconnect_min_sec", 1, None), ("reconnect_max_sec", 1, None)):
            value = getattr(self, name)
            if type(value) is not int or value < low or (high and value > high):
                raise ValueError(f"Invalid broker setting: {name}")
        if self.reconnect_max_sec < self.reconnect_min_sec:
            raise ValueError("Invalid broker reconnect interval")


@dataclass(frozen=True)
class Credentials:
    username: str
    password_file: Path = field(repr=False)


class SimDevice:
    """Runs a SimNode over a real MQTT connection. One start/stop lifetime."""

    def __init__(self, node: SimNode, broker: BrokerConfig, credentials: Credentials,
                 settings: Settings, *, client_factory=mqtt.Client):
        self.node = node
        self._broker = broker
        self._credentials = credentials
        self._settings = settings
        self._stopping = threading.Event()
        self.connected = threading.Event()
        self._kick = threading.Event()
        self._timers: list[threading.Timer] = []
        self._ticker = threading.Thread(target=self._tick, daemon=True,
                                        name=f"node-sim-{node.node_id}")
        self._client = client_factory(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=f"sim-{node.node_id}-{uuid4().hex[:8]}",
                                      clean_session=True, protocol=mqtt.MQTTv311,
                                      transport="tcp")
        self._client.reconnect_delay_set(broker.reconnect_min_sec, broker.reconnect_max_sec)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def topic(self, kind: str) -> str:
        return f"nearpilot/node/{self.node.node_id}/{kind}"

    def start(self) -> None:
        password = Path(self._credentials.password_file).read_text(encoding="utf-8").strip()
        if not password:
            raise ValueError("Empty device password file")
        self._client.username_pw_set(self._credentials.username, password)
        self._client.connect_async(self._broker.host, self._broker.port, self._broker.keepalive_sec)
        if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("Simulator network loop failed")
        self._ticker.start()

    def stop(self) -> None:
        self._stopping.set()
        self._kick.set()
        for timer in list(self._timers):
            timer.cancel()
        self.connected.clear()
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
        if self._ticker.is_alive():
            self._ticker.join(5)

    # ── MQTT callbacks ──────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.warning("Simulator connection rejected: node=%s", self.node.node_id)
            return
        messages = self.node.connect()
        client.subscribe([(self.topic("cmd"), 1), (self.topic("safety_check"), 1)])
        self._publish(messages)
        self.connected.set()
        self._kick.set()  # Report status right after the announce.

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()

    def _on_message(self, client, userdata, message):
        kind = message.topic.rsplit("/", 1)[-1]
        payload = bytes(message.payload)
        if kind == "cmd":
            messages, started = self.node.handle_command(payload)
            self._publish(messages)
            if started:
                self._later(self.node.scenario.actuation_sec, self._finish)
        elif kind == "safety_check":
            self._publish(self.node.handle_check(payload))

    def _finish(self) -> None:
        messages = self.node.finish()
        delay = self.node.scenario.result_delay_sec
        if delay:
            self._later(delay, lambda: self._publish(messages))
        else:
            self._publish(messages)

    def _later(self, seconds: float, action) -> None:
        if self._stopping.is_set():
            return
        timer = threading.Timer(seconds, action)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def _publish(self, messages: list[Message]) -> None:
        for kind, body in messages:
            qos, retain = PUBLISH[kind]
            self._client.publish(self.topic(kind), json.dumps(body), qos=qos, retain=retain)

    # ── periodic reports ────────────────────────────────────────────

    def _tick(self) -> None:
        next_status = next_rssi = 0.0
        while not self._stopping.is_set():
            now = time.monotonic()
            if self._kick.is_set():
                self._kick.clear()
                next_status = next_rssi = now
            if not self.connected.is_set():
                self._kick.wait()  # A new connection or stop sets the kick.
                continue
            if now >= next_status:
                self._publish([self.node.status()])
                next_status = now + self._settings.status_report_sec
            if now >= next_rssi:
                self._publish([self.node.rssi()])
                next_rssi = now + self._settings.rssi_frame_sec
            self._kick.wait(max(0.0, min(next_status, next_rssi) - time.monotonic()))
