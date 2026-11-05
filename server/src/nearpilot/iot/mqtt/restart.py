"""Remote node restart over MQTT (#235, specs/iot-node-restart).

`MqttNodeRestarter` implements `NodeRestarter`. It addresses the connection last confirmed
by a fresh status and publishes once; it never retries or waits for the device.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from nearpilot.iot.messages import MessageValidationError, encode_restart
from nearpilot.iot.mqtt.connection import NotReady
from nearpilot.iot.mqtt.delivery import ReportDelivery
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import RestartOutcome

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MqttNodeRestarter:
    """NodeRestarter over one MqttConnection. Validity is `exec_timeout_sec`, like a command."""

    def __init__(self, connection, delivery: ReportDelivery, *, settings: Callable[[], Settings],
                 clock: Callable[[], datetime] = _utc_now):
        self._connection = connection
        self._delivery = delivery
        self._settings = settings
        self._clock = clock

    def restart(self, node_id: str, restart_id: str) -> RestartOutcome:
        session_id = self._delivery.current_session(node_id)
        if session_id is None:
            return RestartOutcome.NODE_OFFLINE
        issued_at = self._clock()
        expires_at = issued_at + timedelta(seconds=self._settings().exec_timeout_sec)
        try:
            payload = encode_restart(node_id, restart_id, session_id, issued_at, expires_at)
        except MessageValidationError:
            log.error("Restart not sent: invalid MQTT restart for node=%s", node_id)
            return RestartOutcome.NOT_SENT
        try:
            self._connection.publish_request(node_id, "restart", payload)
        except NotReady:
            log.warning("Restart not sent: MQTT not ready, node=%s", node_id)
            return RestartOutcome.NOT_SENT
        except Exception:  # noqa: BLE001 - PublishUncertain included; never retried (spec 6절)
            log.warning("Restart publish outcome unknown: node=%s", node_id)
            return RestartOutcome.NOT_SENT
        return RestartOutcome.SENT
