"""CORE-facing command sending and safety-check requests (iot-command-gateway spec).

The gateway publishes only what CORE hands it. It never decides permission,
issues new command IDs or retries on its own. PUBACK, `accepted` and `running`
are not success; only a device result or a server-made outcome ends a wait.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import threading
import time

from nearpilot.iot.messages import MessageValidationError, encode_command, encode_safety_check
from nearpilot.iot.mqtt.connection import NotReady
from nearpilot.iot.mqtt.delivery import ReportDelivery
from nearpilot.shared.enums import CommandOutcome, NodeResultState, RejectCode
from nearpilot.shared.models import CommandResult, NodeCommand, NodeResult, SafetyCheckRequest

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def outcome_of(report: NodeResult) -> CommandOutcome | None:
    """Map a device result; None for progress that must not end a wait."""
    if report.state in (NodeResultState.ACCEPTED, NodeResultState.RUNNING):
        return None
    if report.state == NodeResultState.REJECTED:
        # Only a verified DEVICE_BUSY proves this command was not started.
        return (CommandOutcome.REJECTED if report.error_code == RejectCode.DEVICE_BUSY
                else CommandOutcome.FAILED)
    return {NodeResultState.SUCCEEDED: CommandOutcome.SUCCEEDED,
            NodeResultState.FAILED: CommandOutcome.FAILED,
            NodeResultState.UNKNOWN: CommandOutcome.UNKNOWN}[report.state]


@dataclass
class _Waiter:
    command: NodeCommand
    callers: int = 0
    published: bool = False
    lost: bool = False
    report: NodeResult | None = None
    final: CommandOutcome | None = None  # Server-made outcome of the publish attempt.


class MqttCommandGateway:
    """NodeCommandGateway and SafetyCheckGateway over one MqttConnection.

    Wire `on_connection_lost` to MqttConnection. Results arrive from
    ReportDelivery's processing worker, never through CORE handover.
    """

    def __init__(self, connection, delivery: ReportDelivery, *,
                 clock: Callable[[], datetime] = _utc_now,
                 monotonic: Callable[[], float] = time.monotonic):
        self._connection = connection
        self._delivery = delivery
        self._clock = clock
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._waiters: dict[str, _Waiter] = {}
        self._commands: dict[str, NodeCommand] = {}
        delivery.add_result_listener(self._on_result)

    # ── NodeCommandGateway ─────────────────────────────────────────

    def send(self, command: NodeCommand, timeout_sec: float) -> CommandResult:
        if (type(timeout_sec) not in (int, float) or not math.isfinite(timeout_sec)
                or timeout_sec <= 0):
            raise ValueError("Invalid command timeout")
        try:
            payload = encode_command(command)
        except MessageValidationError:
            log.error("Command not sent: invalid MQTT command for node=%s", command.node_id)
            return self._server_result(command, CommandOutcome.NOT_SENT)
        with self._condition:
            known = self._commands.get(command.command_id)
            if known is not None and known != command:
                # The ID may already be on the wire; never report it NOT_SENT.
                raise ValueError("Command ID reused with different content")
            waiter = self._waiters.get(command.command_id)
            owner = waiter is None
            if owner:
                if self._delivery.saturated:
                    log.error("Command not sent: CORE handover saturated, node=%s",
                              command.node_id)
                    return self._server_result(command, CommandOutcome.NOT_SENT)
                waiter = self._waiters[command.command_id] = _Waiter(command)
                self._commands[command.command_id] = command
            waiter.callers += 1
        try:
            # The wait is registered before publishing so a fast result is kept.
            deadline = self._monotonic() + timeout_sec
            if owner:
                self._publish(waiter, payload)
            with self._condition:
                while (waiter.report is None and waiter.final is None and not waiter.lost):
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                if waiter.report is not None:
                    return CommandResult(command.command_id, outcome_of(waiter.report),
                                         waiter.report, self._clock())
                # Disconnect after publishing and timeouts are UNKNOWN, not NOT_SENT.
                return self._server_result(command, waiter.final or CommandOutcome.UNKNOWN)
        finally:
            with self._condition:
                waiter.callers -= 1
                if waiter.callers == 0 and self._waiters.get(command.command_id) is waiter:
                    del self._waiters[command.command_id]

    def _publish(self, waiter: _Waiter, payload: bytes) -> None:
        command = waiter.command
        with self._condition:
            waiter.published = True
        try:
            self._connection.publish_request(command.node_id, "cmd", payload)
        except NotReady:
            final = CommandOutcome.NOT_SENT
            log.warning("Command not sent: MQTT not ready, node=%s", command.node_id)
        except Exception:
            final = CommandOutcome.UNKNOWN
            log.warning("Command publish outcome unknown: node=%s", command.node_id)
        else:
            return
        with self._condition:
            waiter.final = final
            self._condition.notify_all()

    def _server_result(self, command: NodeCommand, outcome: CommandOutcome) -> CommandResult:
        # Server-made results carry no device report and no event ID.
        return CommandResult(command.command_id, outcome, None, self._clock())

    def _on_result(self, report: NodeResult) -> None:
        if outcome_of(report) is None:
            return  # Progress does not end or extend the wait.
        with self._condition:
            waiter = self._waiters.get(report.command_id)
            if (waiter is None or waiter.command.node_id != report.node_id
                    or waiter.report is not None):
                return  # Late or conflicting results still reach CORE via delivery.
            waiter.report = report
            self._condition.notify_all()

    def on_connection_lost(self) -> None:
        """MqttConnection.on_connection_lost: published waits become UNKNOWN."""
        with self._condition:
            for waiter in self._waiters.values():
                if waiter.published:
                    waiter.lost = True
            self._condition.notify_all()

    # ── SafetyCheckGateway ─────────────────────────────────────────

    def request_check(self, request: SafetyCheckRequest) -> None:
        """Publish without waiting. Failures raise; CORE owns deadlines and retries."""
        payload = encode_safety_check(request)
        self._delivery.expect_check(request)
        self._connection.publish_request(request.node_id, "safety_check", payload)
