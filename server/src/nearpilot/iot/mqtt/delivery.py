"""Report validation, current-connection tracking and CORE handover.

Paho callbacks only enqueue (`submit`). A processing worker validates reports
and wakes command waiters; a separate handover worker calls NodeEventSink and
retries failures in memory. Queued or retried reports are never claimed as
delivered, and nothing here survives a server restart (iot-event-delivery spec).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import logging
import math
import threading

from uuid import uuid4

from nearpilot.iot.messages import MessageValidationError, decode_message, decode_rssi_report
from nearpilot.iot.mqtt.connection import IncomingReport
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Step
from nearpilot.shared.interfaces import AuditLog, NodeEventSink
from nearpilot.shared.models import (
    AuditEvent, NodeAnnounce, NodeResult, NodeStatus, RssiFrame, RssiReport,
)

log = logging.getLogger(__name__)

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DeliveryConfig:
    """IOT runtime settings with no defaults (S1·S4 A, #236).

    Every node and simulator shares one clock bound. The `time` reply sends the
    same bound, trust and resync windows to the firmware (architecture.md §5 MQTT 6절).
    """

    clock_max_error_sec: float
    clock_trust_sec: int
    clock_resync_sec: int
    pending_limit: int
    retry_interval_sec: float
    stop_timeout_sec: float

    def __post_init__(self):
        error = self.clock_max_error_sec
        if type(error) not in (int, float) or not math.isfinite(error) or not 0 <= error <= 60:
            raise ValueError("Invalid delivery setting: clock_max_error_sec")
        if type(self.clock_trust_sec) is not int or not 15 <= self.clock_trust_sec <= 86400:
            raise ValueError("Invalid delivery setting: clock_trust_sec")
        if type(self.clock_resync_sec) is not int or not 5 <= self.clock_resync_sec <= self.clock_trust_sec:
            raise ValueError("Invalid delivery setting: clock_resync_sec")
        if type(self.pending_limit) is not int or self.pending_limit < 1:
            raise ValueError("Invalid delivery setting: pending_limit")
        for name in ("retry_interval_sec", "stop_timeout_sec"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid delivery setting: {name}")


@dataclass
class _Session:
    """The node connection last confirmed by a fresh live status."""

    session_id: str
    status: NodeStatus
    rssi: tuple[RssiReport, tuple[RssiFrame, ...]] | None = None  # last accepted report and frames


@dataclass
class _Pending:
    method: str
    report: object = field(repr=False)
    due: datetime
    attempts: int = 0


class ReportDelivery:
    """Validates reports and hands them to CORE in per-lane order.

    Lanes: (node, "observe") for announce/status/RSSI and (node, "result",
    command_id). A failing lane does not block the others. Result listeners run on the processing worker before handover,
    so a sink waiting on a CORE lock cannot delay command waiters.
    """

    def __init__(self, sink: NodeEventSink, *, settings: Callable[[], Settings],
                 config: DeliveryConfig, clock: Callable[[], datetime] = _utc_now,
                 audit: AuditLog | None = None):
        self._sink = sink
        self._settings = settings
        self.config = config
        self._clock = clock
        self._audit = audit
        self._audited: set[tuple[str, str | None, str]] = set()
        # Last accepted announce per connection: cmd version and pre-send check (spec 결정 11).
        self._latest_announce: dict[tuple[str, str], NodeAnnounce] = {}
        self._condition = threading.Condition(threading.RLock())
        self._inbox: deque[IncomingReport] = deque()
        self._lanes: dict[tuple, deque[_Pending]] = {}
        self._pending = 0
        self._saturated = False
        self._sessions: dict[str, _Session] = {}
        self._announces: dict[tuple[str, str, int], NodeAnnounce] = {}
        self._events: dict[tuple[str, str, str], list] = {}
        self._listeners: list[Callable[[NodeResult], None]] = []
        self._started = False
        self._running = False
        self._threads: list[threading.Thread] = []

    # ── wiring ──────────────────────────────────────────────────────

    def add_result_listener(self, listener: Callable[[NodeResult], None]) -> None:
        """Command waiters; called for every validated result, must not block."""
        with self._condition:
            self._listeners.append(listener)

    def current_session(self, node_id: str) -> str | None:
        """The connection confirmed by a status received within online_ttl_sec, else None (#235)."""
        ttl = self._settings().online_ttl_sec
        with self._condition:
            current = self._sessions.get(node_id)
            if current is None:
                return None
            received = current.status.received_at
            if (self._clock() - received).total_seconds() > ttl:
                return None
            return current.session_id

    def announce_for(self, node_id: str, session_id: str) -> NodeAnnounce | None:
        """The last validated announce (largest seq) of that connection, else None (FR-06)."""
        with self._condition:
            return self._latest_announce.get((node_id, session_id))

    @property
    def saturated(self) -> bool:
        with self._condition:
            return self._saturated

    @property
    def pending_count(self) -> int:
        with self._condition:
            return self._pending

    # ── receiving ───────────────────────────────────────────────────

    def submit(self, report: IncomingReport) -> None:
        """MqttConnection.on_report: enqueue only, never validate or call CORE."""
        with self._condition:
            self._inbox.append(report)
            self._condition.notify_all()

    def process(self, report: IncomingReport) -> None:
        """Validate one report in reception order and queue its handover."""
        parts = report.topic.split("/")
        node_id, kind = (parts[2], parts[3]) if len(parts) == 4 else ("?", "?")
        decode = decode_rssi_report if kind == "rssi" else decode_message
        try:
            decoded = decode(report.topic, report.payload, received_at=report.received_at)
        except MessageValidationError as error:
            self._reject(node_id, kind, str(error))
            if kind == "announce" and parts[:2] == ["nearpilot", "node"] and node_id:
                self._audit_announce(node_id, error.session_id, str(error))
            return
        settings = self._settings()
        if kind == "announce":
            self._accept_announce(decoded)
        elif kind == "status":
            self._accept_status(decoded, settings)
        elif kind == "rssi":
            rssi_report, frames = decoded
            self._accept_rssi(node_id, rssi_report, frames, settings)
        else:
            self._accept_result(decoded)

    def _reject(self, node_id: str, kind: str, reason: str) -> None:
        # Never log payloads or credentials (NFR-08).
        log.warning("MQTT report rejected: node=%s kind=%s reason=%s", node_id, kind, reason)

    def _audit_announce(self, node_id: str, session_id: str | None, reason: str) -> None:
        """Rejected announce → one `announce` audit row per (node, connection, reason) (FR-04).

        The payload is never included. A failing audit write only logs: it must not stop
        report processing.
        """
        key = (node_id, session_id, reason)
        with self._condition:
            if self._audit is None or key in self._audited:
                return
            self._audited.add(key)
        try:
            self._audit.append(AuditEvent(
                event_id=str(uuid4()), step=Step.ANNOUNCE,
                payload={"node_id": node_id, "session_id": session_id, "reason": reason}))
        except Exception as error:
            with self._condition:
                self._audited.discard(key)
            log.error("Rejected announce audit failed: node=%s error=%s", node_id, type(error).__name__)

    def _stale_or_future(self, received_at: datetime, observed_at: datetime,
                         times: tuple[datetime, ...], max_age: float, skew: float) -> str | None:
        limit = received_at + timedelta(seconds=skew)
        if any(value > limit for value in times):
            return "future observation"
        if (received_at - observed_at).total_seconds() + skew > max_age:
            return "stale observation"
        return None

    def _accept_announce(self, announce: NodeAnnounce) -> None:
        key = (announce.node_id, announce.session_id, announce.seq)
        with self._condition:
            seen = self._announces.get(key)
            if seen is not None:
                # Retained announces return on every broker reconnect.
                if replace(announce, received_at=seen.received_at) != seen:
                    self._reject(announce.node_id, "announce", "sequence reused with different content")
                return
            if self._saturated:
                self._reject(announce.node_id, "announce", "handover saturated")
                return
            self._announces[key] = announce
            connection = (announce.node_id, announce.session_id)
            latest = self._latest_announce.get(connection)
            if latest is None or latest.seq < announce.seq:
                self._latest_announce[connection] = announce
            self._enqueue((announce.node_id, "observe"), "on_announce", announce)

    def _accept_status(self, status: NodeStatus, settings: Settings) -> None:
        node_id = status.node_id
        skew = self.config.clock_max_error_sec
        reason = self._stale_or_future(status.received_at, status.observed_at,
                                       (status.observed_at, status.sent_at),
                                       settings.online_ttl_sec, skew)
        if reason:
            self._reject(node_id, "status", reason)
            return
        with self._condition:
            current = self._sessions.get(node_id)
            if current is not None and current.session_id == status.session_id:
                last = current.status
                if status.seq <= last.seq:
                    if status.seq != last.seq:
                        self._reject(node_id, "status", "out-of-order sequence")
                    elif replace(status, received_at=last.received_at) != last:
                        self._reject(node_id, "status", "sequence reused with different content")
                    return
                if status.observed_at < last.observed_at:
                    self._reject(node_id, "status", "observation older than current")
                    return
            elif current is not None and status.observed_at <= current.status.observed_at:
                # A new connection must be strictly newer; a tie keeps the current one.
                self._reject(node_id, "status", "not newer than current connection")
                return
            if self._saturated:
                self._reject(node_id, "status", "handover saturated")
                return
            if current is None or current.session_id != status.session_id:
                self._sessions[node_id] = _Session(status.session_id, status)
            else:
                current.status = status
            self._enqueue((node_id, "observe"), "on_status", status)

    def _accept_rssi(self, node_id: str, report: RssiReport, frames: tuple[RssiFrame, ...],
                     settings: Settings) -> None:
        # An empty scan (no beacon seen) goes through the same checks as a report with frames.
        skew = self.config.clock_max_error_sec
        reason = self._stale_or_future(report.received_at, report.frame_ts,
                                       (report.frame_ts, report.sent_at),
                                       settings.beacon_recent_sec, skew)
        if reason:
            self._reject(node_id, "rssi", reason)
            return
        with self._condition:
            current = self._sessions.get(node_id)
            if current is None or current.session_id != report.session_id:
                self._reject(node_id, "rssi", "connection not confirmed by live status")
                return
            last = current.rssi
            if last is not None:
                if report.seq <= last[0].seq:
                    if report.seq != last[0].seq:
                        self._reject(node_id, "rssi", "out-of-order sequence")
                    elif (replace(report, received_at=last[0].received_at),
                          tuple(replace(f, received_at=last[0].received_at)
                                for f in frames)) != last:
                        self._reject(node_id, "rssi", "sequence reused with different content")
                    return
                if report.frame_ts < last[0].frame_ts:
                    self._reject(node_id, "rssi", "observation older than current")
                    return
            if self._saturated:
                self._reject(node_id, "rssi", "handover saturated")
                return
            current.rssi = (report, frames)
            for frame in frames:
                self._enqueue((node_id, "observe"), "on_rssi", frame)
            # After every frame of this message, on the same lane (NodeEventSink.on_rssi_report).
            self._enqueue((node_id, "observe"), "on_rssi_report", report)

    def _accept_result(self, result: NodeResult) -> None:
        # Old-session and late results are settlement evidence, not observations.
        for listener in tuple(self._listeners):
            try:
                listener(result)
            except Exception:
                log.error("Command result notification failed: node=%s", result.node_id)
        content = (result.command_id, result.state, result.occurred_at, result.error_code)
        self._accept_event("result", result, content, (result.node_id, "result", result.command_id))

    def _accept_event(self, kind: str, report, content: tuple, lane: tuple) -> None:
        with self._condition:
            seen = self._events.setdefault((kind, report.node_id, report.event_id), [])
            if content in seen:
                return  # Re-report of a queued or delivered event.
            if seen:
                # Hand the conflicting report to CORE as well; do not hide it.
                log.warning("Conflicting report for one event: node=%s kind=%s",
                            report.node_id, kind)
            seen.append(content)
            self._enqueue(lane, "on_result", report)

    # ── handover ────────────────────────────────────────────────────

    def _enqueue(self, lane: tuple, method: str, report) -> None:
        self._lanes.setdefault(lane, deque()).append(_Pending(method, report, self._clock()))
        self._pending += 1
        self._update_saturation()
        self._condition.notify_all()

    def _update_saturation(self) -> None:
        saturated = self._pending >= self.config.pending_limit
        if saturated != self._saturated:
            self._saturated = saturated
            if saturated:
                log.error("CORE handover saturated: pending=%d limit=%d",
                          self._pending, self.config.pending_limit)
            else:
                log.warning("CORE handover saturation cleared: pending=%d", self._pending)

    def deliver_due(self) -> int:
        """Hand over every due report in lane order; returns the delivered count.

        Only one caller at a time: the handover worker, or a test without it.
        """
        delivered = 0
        now = self._clock()
        with self._condition:
            lanes = list(self._lanes)
        for lane in lanes:
            while True:
                with self._condition:
                    queue = self._lanes.get(lane)
                    if not queue or queue[0].due > now:
                        break
                    item = queue[0]
                try:
                    getattr(self._sink, item.method)(item.report)
                except Exception as error:
                    with self._condition:
                        item.attempts += 1
                        item.due = self._clock() + timedelta(seconds=self.config.retry_interval_sec)
                    log.warning("CORE handover failed: node=%s method=%s attempts=%d error=%s",
                                lane[0], item.method, item.attempts, type(error).__name__)
                    break
                with self._condition:
                    queue.popleft()
                    if not queue:
                        del self._lanes[lane]
                    self._pending -= 1
                    self._update_saturation()
                delivered += 1
        return delivered

    def _next_due(self) -> datetime | None:
        return min((queue[0].due for queue in self._lanes.values() if queue), default=None)

    # ── workers ─────────────────────────────────────────────────────

    def start(self) -> None:
        with self._condition:
            if self._started:
                raise RuntimeError("Report delivery already started")
            self._started = self._running = True
        for target, name in ((self._process_loop, "iot-report-process"),
                             (self._deliver_loop, "iot-core-handover")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        """Stop workers. Pending reports stay undelivered; none are claimed done."""
        with self._condition:
            self._running = False
            self._condition.notify_all()
        deadline = self._clock() + timedelta(seconds=self.config.stop_timeout_sec)
        for thread in self._threads:
            thread.join(max(0.0, (deadline - self._clock()).total_seconds()))
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("Report delivery shutdown timed out")

    def _process_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._inbox or not self._running)
                if not self._running:
                    return
                report = self._inbox.popleft()
            try:
                self.process(report)
            except Exception:
                log.error("MQTT report processing failed: topic kind=%s",
                          report.topic.rsplit("/", 1)[-1])

    def _deliver_loop(self) -> None:
        while True:
            with self._condition:
                if not self._running:
                    return
                due = self._next_due()
                now = self._clock()
                if due is None or due > now:
                    timeout = None if due is None else (due - now).total_seconds()
                    self._condition.wait(timeout)
                    continue
            self.deliver_due()
