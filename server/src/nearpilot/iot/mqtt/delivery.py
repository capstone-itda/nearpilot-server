"""Report validation, current-connection tracking and CORE handover.

Paho callbacks only enqueue (`submit`). A processing worker validates reports
and wakes command waiters; a separate handover worker calls NodeEventSink and
retries failures in memory. Queued or retried reports are never claimed as
delivered, and nothing here survives a server restart (iot-event-delivery spec).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import logging
import math
import threading
from types import MappingProxyType

from nearpilot.iot.messages import MessageValidationError, decode_message
from nearpilot.iot.mqtt.connection import IncomingReport
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import SafetyCheckOutcome
from nearpilot.shared.interfaces import NodeEventSink
from nearpilot.shared.models import (
    NodeAnnounce, NodeResult, NodeStatus, RssiFrame, SafetyCheckRequest, SafetyCheckResult,
)

log = logging.getLogger(__name__)

# architecture.md §4 "반납 검사와 종료"의 v1 필수 항목. 다른 기준은 근거를 확인할 수 없다.
V1_CHECK_ITEMS = {"locker-return": ("door_closed", "locked"),
                  "light-return": ("actuator_idle",), "speaker-return": ("actuator_idle",)}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.upper().replace("Z", "+00:00"))


@dataclass(frozen=True)
class DeliveryConfig:
    """IOT runtime settings with no defaults (S1·S4 A, #140).

    clock_skew_sec maps node_id to its verified maximum clock error. A node
    without an entry is unverified: its observations and checks are not used.
    """

    clock_skew_sec: Mapping[str, float]
    pending_limit: int
    retry_interval_sec: float
    stop_timeout_sec: float

    def __post_init__(self):
        if not isinstance(self.clock_skew_sec, Mapping):
            raise ValueError("Invalid delivery setting: clock_skew_sec")
        skews = dict(self.clock_skew_sec)
        for node_id, skew in skews.items():
            if (not isinstance(node_id, str) or not node_id or type(skew) not in (int, float)
                    or not math.isfinite(skew) or skew < 0):
                raise ValueError("Invalid delivery setting: clock_skew_sec")
        object.__setattr__(self, "clock_skew_sec", MappingProxyType(skews))
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
    rssi: tuple[RssiFrame, ...] | None = None


@dataclass
class _Pending:
    method: str
    report: object = field(repr=False)
    due: datetime
    attempts: int = 0


class ReportDelivery:
    """Validates reports and hands them to CORE in per-lane order.

    Lanes: (node, "observe") for announce/status/RSSI, (node, "result",
    command_id) and (node, "check", check_id). A failing lane does not block
    the others. Result listeners run on the processing worker before handover,
    so a sink waiting on a CORE lock cannot delay command waiters.
    """

    def __init__(self, sink: NodeEventSink, *, settings: Callable[[], Settings],
                 config: DeliveryConfig, clock: Callable[[], datetime] = _utc_now):
        self._sink = sink
        self._settings = settings
        self.config = config
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._inbox: deque[IncomingReport] = deque()
        self._lanes: dict[tuple, deque[_Pending]] = {}
        self._pending = 0
        self._saturated = False
        self._sessions: dict[str, _Session] = {}
        self._announces: dict[tuple[str, str, int], NodeAnnounce] = {}
        self._events: dict[tuple[str, str, str], list] = {}
        self._checks: dict[str, SafetyCheckRequest] = {}
        self._listeners: list[Callable[[NodeResult], None]] = []
        self._started = False
        self._running = False
        self._threads: list[threading.Thread] = []

    # ── wiring ──────────────────────────────────────────────────────

    def add_result_listener(self, listener: Callable[[NodeResult], None]) -> None:
        """Command waiters; called for every validated result, must not block."""
        with self._condition:
            self._listeners.append(listener)

    def expect_check(self, request: SafetyCheckRequest) -> None:
        """Register a check request before publishing it.

        A result is handed to CORE only when it matches a registered request.
        """
        with self._condition:
            existing = self._checks.get(request.check_id)
            if existing is not None and existing != request:
                raise ValueError("Safety check ID registered with different content")
            self._checks[request.check_id] = request

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
        try:
            decoded = decode_message(report.topic, report.payload, received_at=report.received_at)
        except MessageValidationError as error:
            self._reject(node_id, kind, str(error))
            return
        settings = self._settings()
        self._prune_checks(report.received_at, settings)
        if kind == "announce":
            self._accept_announce(decoded)
        elif kind == "status":
            self._accept_status(decoded, settings)
        elif kind == "rssi":
            self._accept_rssi(node_id, decoded, settings)
        elif kind == "result":
            self._accept_result(decoded)
        else:
            self._accept_check(decoded, settings)

    def _reject(self, node_id: str, kind: str, reason: str) -> None:
        # Never log payloads or credentials (NFR-08).
        log.warning("MQTT report rejected: node=%s kind=%s reason=%s", node_id, kind, reason)

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
            self._enqueue((announce.node_id, "observe"), "on_announce", announce)

    def _accept_status(self, status: NodeStatus, settings: Settings) -> None:
        node_id = status.node_id
        skew = self.config.clock_skew_sec.get(node_id)
        if skew is None:
            self._reject(node_id, "status", "clock skew unverified")
            return
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

    def _accept_rssi(self, node_id: str, frames: tuple[RssiFrame, ...], settings: Settings) -> None:
        if not frames:
            return  # A valid empty scan has no beacon observation to hand over.
        first = frames[0]
        skew = self.config.clock_skew_sec.get(node_id)
        if skew is None:
            self._reject(node_id, "rssi", "clock skew unverified")
            return
        reason = self._stale_or_future(first.received_at, first.frame_ts,
                                       (first.frame_ts, first.sent_at),
                                       settings.beacon_recent_sec, skew)
        if reason:
            self._reject(node_id, "rssi", reason)
            return
        with self._condition:
            current = self._sessions.get(node_id)
            if current is None or current.session_id != first.session_id:
                self._reject(node_id, "rssi", "connection not confirmed by live status")
                return
            last = current.rssi
            if last is not None:
                if first.seq <= last[0].seq:
                    if first.seq != last[0].seq:
                        self._reject(node_id, "rssi", "out-of-order sequence")
                    elif tuple(replace(f, received_at=last[0].received_at) for f in frames) != last:
                        self._reject(node_id, "rssi", "sequence reused with different content")
                    return
                if first.frame_ts < last[0].frame_ts:
                    self._reject(node_id, "rssi", "observation older than current")
                    return
            if self._saturated:
                self._reject(node_id, "rssi", "handover saturated")
                return
            current.rssi = frames
            for frame in frames:
                self._enqueue((node_id, "observe"), "on_rssi", frame)

    def _accept_result(self, result: NodeResult) -> None:
        # Old-session and late results are settlement evidence, not observations.
        for listener in tuple(self._listeners):
            try:
                listener(result)
            except Exception:
                log.error("Command result notification failed: node=%s", result.node_id)
        content = (result.command_id, result.state, result.occurred_at, result.error_code)
        self._accept_event("result", result, content, (result.node_id, "result", result.command_id))

    def _accept_check(self, result: SafetyCheckResult, settings: Settings) -> None:
        reason = self._check_mismatch(result, settings)
        if reason:
            self._reject(result.node_id, "safety_check_result", reason)
            return
        content = (result.check_id, result.criteria, result.checked_at, result.outcome,
                   result.evidence)
        self._accept_event("safety_check_result", result, content,
                           (result.node_id, "check", result.check_id))

    def _accept_event(self, kind: str, report, content: tuple, lane: tuple) -> None:
        method = "on_result" if kind == "result" else "on_safety_check_result"
        with self._condition:
            seen = self._events.setdefault((kind, report.node_id, report.event_id), [])
            if content in seen:
                return  # Re-report of a queued or delivered event.
            if seen:
                # Hand the conflicting report to CORE as well; do not hide it.
                log.warning("Conflicting report for one event: node=%s kind=%s",
                            report.node_id, kind)
            seen.append(content)
            self._enqueue(lane, method, report)

    def _check_mismatch(self, result: SafetyCheckResult, settings: Settings) -> str | None:
        with self._condition:
            request = self._checks.get(result.check_id)
        if request is None:
            return "unknown check request"
        if request.node_id != result.node_id:
            return "check node mismatch"
        if result.session_id != request.target_session_id:
            return "check connection mismatch"
        if result.criteria != request.criteria:
            return "check criteria mismatch"
        skew = self.config.clock_skew_sec.get(result.node_id)
        if skew is None:
            return "clock skew unverified"
        items = (V1_CHECK_ITEMS.get(result.criteria.criteria_id)
                 if result.criteria.criteria_version == 1 else None)
        if items is None:
            return "unsupported check criteria"
        earliest = request.issued_at - timedelta(seconds=skew)
        evidence_times = tuple(_parse_time(result.evidence[name]["observed_at"]) for name in items)
        if result.checked_at < earliest or any(value < earliest for value in evidence_times):
            return "check observed before request"
        if result.checked_at > request.expires_at + timedelta(seconds=skew):
            return "check after request expiry"
        reason = self._stale_or_future(result.received_at, result.checked_at,
                                       (result.checked_at, result.sent_at, *evidence_times),
                                       settings.safety_report_max_age_sec, skew)
        if reason:
            return reason
        values = [result.evidence[name]["value"] for name in items]
        if result.outcome == SafetyCheckOutcome.PASSED and not all(values):
            return "passed without all required evidence"
        if result.outcome == SafetyCheckOutcome.NOT_READY and all(values):
            return "not_ready without failing evidence"
        return None

    def _prune_checks(self, received_at: datetime, settings: Settings) -> None:
        # Reports are processed in reception order, so a request whose results
        # would all be stale at this reception time can no longer match.
        horizon = received_at - timedelta(seconds=settings.safety_report_max_age_sec)
        with self._condition:
            for check_id in [key for key, request in self._checks.items()
                             if request.expires_at < horizon]:
                del self._checks[check_id]

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
