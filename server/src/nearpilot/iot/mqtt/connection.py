"""MQTT transport lifecycle. Callbacks hand off to IOT workers, never CORE.

This module does not validate report bodies, infer node liveness or wait for
physical execution. A successful publish call/PUBACK is not execution success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import threading
from typing import Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

REPORT_QOS = {"announce": 1, "rssi": 0, "status": 1, "result": 1}
SUBSCRIPTIONS = tuple((f"nearpilot/node/+/{kind}", qos)
                      for kind, qos in REPORT_QOS.items())


class ConnectionFailure(RuntimeError):
    """Connection, subscription or shutdown failed; no secret details attached."""


class NotReady(ConnectionFailure):
    """No publish was attempted by this call."""


class PublishUncertain(ConnectionFailure):
    """A publish was attempted; do not infer NOT_SENT or physical success."""


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    client_id: str
    username: str
    password_file: Path = field(repr=False)
    keepalive_sec: int
    connect_timeout_sec: float
    start_timeout_sec: float
    stop_timeout_sec: float
    reconnect_min_sec: int
    reconnect_max_sec: int

    def __post_init__(self):
        for name in ("host", "client_id", "username"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"Invalid MQTT setting: {name}")
        for name, minimum, maximum in (("port", 1, 65535), ("keepalive_sec", 1, 65535),
                                       ("reconnect_min_sec", 1, None),
                                       ("reconnect_max_sec", 1, None)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum or (maximum and value > maximum):
                raise ValueError(f"Invalid MQTT setting: {name}")
        for name in ("connect_timeout_sec", "start_timeout_sec", "stop_timeout_sec"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid MQTT setting: {name}")
        if self.reconnect_max_sec < self.reconnect_min_sec:
            raise ValueError("Invalid MQTT reconnect interval")


@dataclass(frozen=True)
class IncomingReport:
    topic: str
    payload: bytes = field(repr=False)
    qos: int
    retain: bool
    dup: bool
    received_at: datetime


class MqttConnection:
    """One start/stop lifetime; reconnects are managed by Paho's network thread.

    on_report and on_connection_lost must be short, nonblocking IOT handoffs.
    They must not call NodeEventSink directly. Delivery/retry policy belongs to
    iot-event-delivery; command settlement belongs to iot-command-gateway.
    """

    def __init__(self, config: MqttConfig, *,
                 on_report: Callable[[IncomingReport], None],
                 on_connection_lost: Callable[[], None],
                 client_factory=mqtt.Client,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.config = config
        self._on_report = on_report
        self._on_connection_lost = on_connection_lost
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._started = False
        self._running = False
        self._ready = False
        self._error = None
        self._subscription_mid = None
        self._stop_done = threading.Event()
        self._stop_error = False
        self._client = client_factory(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=config.client_id, clean_session=True,
                                      protocol=mqtt.MQTTv311, transport="tcp")
        self._client.connect_timeout = config.connect_timeout_sec
        self._client.reconnect_delay_set(config.reconnect_min_sec, config.reconnect_max_sec)
        self._client.on_connect = self._connected
        self._client.on_connect_fail = self._connect_failed
        self._client.on_subscribe = self._subscribed
        self._client.on_disconnect = self._disconnected
        self._client.on_message = self._message

    @property
    def ready(self) -> bool:
        with self._condition:
            return self._running and self._ready

    def start(self) -> None:
        with self._condition:
            if self._started:
                raise ConnectionFailure("MQTT instance already started")
            self._started = True
            self._running = True
        try:
            try:
                with self._condition:
                    # A concurrent stop must not be followed by a new network
                    # loop after its shutdown worker has already completed.
                    if not self._running:
                        raise ConnectionFailure("MQTT startup cancelled")
                    password = Path(self.config.password_file).read_text(encoding="utf-8").rstrip("\r\n")
                    if not password or "\n" in password or "\r" in password or "\x00" in password:
                        raise ValueError("Invalid password file")
                    self._client.username_pw_set(self.config.username, password)
                    self._client.connect_async(self.config.host, self.config.port,
                                               self.config.keepalive_sec)
                    if self._client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                        raise ValueError("Network loop failed")
            except Exception:
                raise ConnectionFailure("MQTT startup failed") from None
            with self._condition:
                completed = self._condition.wait_for(
                    lambda: self._ready or self._error or not self._running,
                    timeout=self.config.start_timeout_sec)
                if not completed or not self._running or not self._ready:
                    raise ConnectionFailure(self._error or "MQTT subscription readiness timed out")
        except ConnectionFailure:
            self.stop()
            raise

    def stop(self) -> None:
        with self._condition:
            if not self._started:
                return
            stopping = self._running
            self._running = False
            self._ready = False
            self._subscription_mid = None
            self._condition.notify_all()
        if stopping:
            self._notify_loss()
            # loop_stop blocks until Paho exits. Bound the caller's wait without
            # claiming the network thread terminated when it has not.
            threading.Thread(target=self._close_network, daemon=True,
                             name="mqtt-shutdown").start()
        if not self._stop_done.wait(self.config.stop_timeout_sec):
            raise ConnectionFailure("MQTT shutdown timed out")
        if self._stop_error:
            raise ConnectionFailure("MQTT shutdown failed")

    def _close_network(self):
        try:
            try:
                self._client.disconnect()
            finally:
                self._client.loop_stop()
        except Exception:
            self._stop_error = True
        finally:
            self._stop_done.set()

    def publish_request(self, node_id: str, kind: str, payload: bytes):
        """Transport only; called by the CORE-facing command module.

        Returns Paho's publish receipt, never a physical command result.
        """
        if (not isinstance(node_id, str) or not node_id
                or any(char in node_id for char in "/+#\x00")):
            raise ValueError("Invalid MQTT node ID")
        if kind != "cmd" or not isinstance(payload, bytes):
            raise ValueError("Invalid MQTT request")
        with self._condition:
            if not self._running or not self._ready:
                raise NotReady("MQTT subscriptions not ready; publish not attempted")
            try:
                receipt = self._client.publish(f"nearpilot/node/{node_id}/{kind}", payload,
                                               qos=1, retain=False)
                if receipt.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise ValueError("Publish rejected")
                return receipt
            except Exception:
                raise PublishUncertain("MQTT publish outcome unknown") from None

    def _connected(self, client, userdata, flags, reason_code, properties):
        with self._condition:
            self._ready = False
            self._subscription_mid = None
            if not self._running:
                return
            if reason_code != 0:
                self._fail("MQTT authentication or connection rejected")
                return
            self._error = None
            try:
                rc, mid = client.subscribe(list(SUBSCRIPTIONS))
                if rc != mqtt.MQTT_ERR_SUCCESS:
                    raise ValueError("Subscribe rejected")
                self._subscription_mid = mid
            except Exception:
                self._fail("MQTT subscription request failed")

    def _connect_failed(self, client, userdata):
        # Initial outages retry until start_timeout; running connections retry
        # under Paho with the configured backoff.
        log.warning("MQTT connection attempt failed")

    def _subscribed(self, client, userdata, mid, reason_codes, properties):
        with self._condition:
            if not self._running or mid != self._subscription_mid:
                return
            self._subscription_mid = None
            granted = [getattr(code, "value", code) for code in reason_codes]
            if granted != [qos for _, qos in SUBSCRIPTIONS]:
                self._fail("MQTT required subscription QoS not granted")
                return
            self._ready = True
            self._condition.notify_all()

    def _fail(self, reason):
        self._error = reason
        self._ready = False
        log.warning(reason)
        self._condition.notify_all()

    def _disconnected(self, client, userdata, flags, reason_code, properties):
        with self._condition:
            notify = self._running
            self._ready = False
            self._subscription_mid = None
            self._condition.notify_all()
        if notify:
            self._notify_loss()

    def _notify_loss(self):
        try:
            self._on_connection_lost()
        except Exception:
            log.error("MQTT connection-loss handoff failed")

    def _message(self, client, userdata, message):
        with self._condition:
            if not self._running:
                return
        # Retained reports may arrive before SUBACK: readiness must not discard
        # discovery. Body/freshness validation belongs to the delivery worker.
        parts = message.topic.split("/")
        if (len(parts) != 4 or parts[:2] != ["nearpilot", "node"] or not parts[2]
                or any(char in parts[2] for char in "+#\x00") or parts[3] not in REPORT_QOS):
            log.warning("MQTT unexpected report topic")
            return
        try:
            received_at = self._clock()
            if received_at.tzinfo is None or received_at.utcoffset() is None:
                raise ValueError("UTC reception clock required")
            self._on_report(IncomingReport(message.topic, bytes(message.payload), message.qos,
                                           bool(message.retain), bool(message.dup),
                                           received_at.astimezone(timezone.utc)))
        except Exception:
            # Do not log payloads, callback exceptions or credentials.
            log.error("MQTT report handoff failed")
