"""Retained occupancy display for rental nodes (architecture.md §5 MQTT 3절).

The publisher mirrors the stored device state only. It never decides, changes or
infers occupancy, and a delivered display is not evidence of the current state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import logging
import threading

from nearpilot.iot.messages import MessageValidationError, encode_occupancy
from nearpilot.iot.mqtt.connection import NotReady, PublishUncertain
from nearpilot.shared.enums import DeviceState, Occupancy, TrustState
from nearpilot.shared.models import NodeInfo

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OccupancyPublisher:
    """Publishes a trusted rental node's state when it changes or after the link is lost.

    `tick` runs on a periodic task; wire `on_connection_lost` to MqttConnection.
    """

    def __init__(self, connection, nodes: Callable[[], Sequence[NodeInfo]], *,
                 clock: Callable[[], datetime] = _utc_now):
        self._connection = connection
        self._nodes = nodes
        self._clock = clock
        self._lock = threading.Lock()
        self._published: dict[str, DeviceState] = {}
        self._generation = 0  # Bumped on link loss so an in-flight publish is not remembered.

    def on_connection_lost(self) -> None:
        with self._lock:
            self._published.clear()
            self._generation += 1

    def tick(self) -> None:
        if not self._connection.ready:
            self.on_connection_lost()
            return
        for node in self._nodes():
            if node.occupancy != Occupancy.RENTAL or node.trust_state != TrustState.TRUSTED:
                continue
            with self._lock:
                if self._published.get(node.node_id) == node.device_state:
                    continue
                generation = self._generation
            try:
                payload = encode_occupancy(node.node_id, node.device_state, self._clock())
                self._connection.publish_request(node.node_id, "occupancy", payload)
            except NotReady:
                return  # Retried after the link is ready again.
            except (PublishUncertain, MessageValidationError, ValueError):
                log.warning("Occupancy display not confirmed: node=%s", node.node_id)
                continue  # Not remembered, so the next tick publishes again.
            with self._lock:
                if generation == self._generation:
                    self._published[node.node_id] = node.device_state
