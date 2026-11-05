"""MQTT clock replies for real nodes (#236, architecture.md §5 MQTT 6절).

The node sends `time_request` and keeps its monotonic send time t0. This module answers
on the Paho callback thread with the server UTC time taken right before publishing, so the
round trip the node measures bounds the error. It never touches CORE or report delivery.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging

from nearpilot.iot.messages import MessageValidationError, decode_time_request, encode_time
from nearpilot.iot.mqtt.connection import NotReady, PublishUncertain
from nearpilot.iot.mqtt.delivery import DeliveryConfig

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimeResponder:
    """Wire `handle` to `MqttConnection(on_time_request=...)` and set `connection` before start."""

    def __init__(self, config: DeliveryConfig, *, clock: Callable[[], datetime] = _utc_now):
        self._config = config
        self._clock = clock
        self.connection = None

    def handle(self, topic: str, payload: bytes) -> None:
        """Answer one request. Invalid requests and failed publishes only log; the node asks again."""
        try:
            node_id, request_id = decode_time_request(topic, payload)
        except MessageValidationError as error:
            log.warning("MQTT time request rejected: reason=%s", error)  # No payload (NFR-08).
            return
        connection = self.connection
        if connection is None:
            return
        try:
            body = encode_time(node_id, request_id, self._clock(),
                               max_error_sec=self._config.clock_max_error_sec,
                               trust_sec=self._config.clock_trust_sec,
                               resync_sec=self._config.clock_resync_sec)
            connection.publish_request(node_id, "time", body)
        except NotReady:
            return
        except (PublishUncertain, MessageValidationError, ValueError):
            log.warning("MQTT time reply not sent: node=%s", node_id)
