"""Report validation, current connection and in-memory CORE handover."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
import threading

import pytest

from nearpilot.iot.mqtt.connection import IncomingReport
from nearpilot.iot.mqtt.delivery import DeliveryConfig, ReportDelivery
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import NodeResultState, SafetyCheckOutcome
from nearpilot.shared.models import CheckCriteria, SafetyCheckRequest

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
EVENT = "87654321-4321-4321-4321-cba987654321"
EVENT2 = "87654321-4321-4321-4321-cba987654322"
BEACON = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
T0 = datetime(2026, 10, 16, 3, 0, 0, tzinfo=timezone.utc)
QOS = {"announce": 1, "rssi": 0, "status": 1, "result": 1, "safety_check_result": 1}


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def sec(value):
    return timedelta(seconds=value)


def body(kind, node, session, seq, at):
    data = dict(schema_version=1, node_id=node, session_id=session, seq=seq, sent_at=iso(at))
    data.update({
        "announce": dict(capability="locker", occupancy="rental"),
        "status": dict(observed_at=iso(at), physical_state=dict(
            door_closed=True, light_on=None, actuator_state="idle", sensor_fault=False)),
        "rssi": dict(frame_ts=iso(at), observations=[dict(uuid=BEACON, major=1, minor=1, rssi=-60)]),
        "result": dict(command_id="command-1", event_id=EVENT, state="succeeded",
                       occurred_at=iso(at)),
        "safety_check_result": dict(
            check_id="check-1", event_id=EVENT, criteria_id="locker-return",
            criteria_version=1, checked_at=iso(at), outcome="passed",
            evidence={name: dict(value=True, observed_at=iso(at))
                      for name in ("door_closed", "locked")}),
    }[kind])
    return data


def incoming(kind, node="locker-1", *, session=S1, seq=1, at=T0, received=None,
             retain=False, **fields):
    data = body(kind, node, session, seq, at) | fields
    return IncomingReport(f"nearpilot/node/{node}/{kind}", json.dumps(data).encode(),
                          QOS[kind], retain, False, received or at + sec(0.1))


def check_request(**fields):
    return SafetyCheckRequest(**(dict(
        check_id="check-1", node_id="locker-1", target_session_id=S1,
        criteria=CheckCriteria("locker-return", 1),
        issued_at=T0 - sec(1), expires_at=T0 + sec(2)) | fields))


class Sink:
    """NodeEventSink fake. fail(method, report) decides whether a call raises."""

    def __init__(self):
        self.calls = []
        self.attempts = []
        self.fail = lambda method, report: False

    def _call(self, method, report):
        self.attempts.append((method, report))
        if self.fail(method, report):
            raise RuntimeError("CORE unavailable password=core-secret")
        self.calls.append((method, report))

    def on_announce(self, report):
        self._call("on_announce", report)

    def on_rssi(self, report):
        self._call("on_rssi", report)

    def on_status(self, report):
        self._call("on_status", report)

    def on_result(self, report):
        self._call("on_result", report)

    def on_safety_check_result(self, report):
        self._call("on_safety_check_result", report)

    def of(self, method):
        return [report for name, report in self.calls if name == method]


class Harness:
    def __init__(self, *, skew=None, limit=100, sink=None):
        self.sink = sink or Sink()
        self.now = T0 + sec(1)
        self.delivery = ReportDelivery(
            self.sink, settings=Settings,
            config=DeliveryConfig({"locker-1": 0.0} if skew is None else skew, limit, 1.0, 2.0),
            clock=lambda: self.now)

    def feed(self, *reports):
        for report in reports:
            self.delivery.process(report)
        return self.delivery.deliver_due()

    def advance(self, seconds):
        self.now += sec(seconds)


@pytest.fixture
def h():
    return Harness()


def test_FR04_retained_announce_not_live(h):
    retained = incoming("announce", retain=True)
    h.feed(retained, replace(retained, received_at=retained.received_at + sec(5)))
    assert [a.session_id for a in h.sink.of("on_announce")] == [S1]
    # Discovery alone does not confirm the connection for observations.
    h.feed(incoming("rssi", seq=1))
    assert h.sink.of("on_rssi") == []
    assert h.sink.of("on_status") == []


def test_FR05_requires_live_session(h):
    h.feed(incoming("rssi", seq=1))
    assert h.sink.of("on_rssi") == []
    h.feed(incoming("status", seq=1), incoming("rssi", seq=2, at=T0 + sec(0.5)))
    assert [(f.seq, f.session_id) for f in h.sink.of("on_rssi")] == [(2, S1)]


def test_FR05_partial_handover_retries_missing(h):
    beacons = [dict(uuid=BEACON, major=1, minor=minor, rssi=-60 - minor) for minor in (1, 2)]
    h.feed(incoming("status"))
    h.sink.fail = lambda method, frame: method == "on_rssi" and frame.beacon.minor == 2
    report = incoming("rssi", seq=1, observations=beacons)
    assert h.feed(report) == 1
    assert h.delivery.pending_count == 1
    h.sink.fail = lambda method, frame: False
    assert h.delivery.deliver_due() == 0  # Not before the retry interval.
    h.advance(1)
    assert h.delivery.deliver_due() == 1
    frames = h.sink.of("on_rssi")
    assert [f.beacon.minor for f in frames] == [1, 2]
    assert {f.received_at for f in frames} == {report.received_at}
    assert {(f.session_id, f.seq) for f in frames} == {(S1, 1)}


def test_FR05_dedup_after_successful_delivery(h, caplog):
    status = incoming("status")
    h.feed(status)
    assert h.feed(replace(status, received_at=status.received_at + sec(1), dup=True)) == 0
    changed = incoming("status", physical_state=dict(
        door_closed=False, light_on=None, actuator_state="idle", sensor_fault=False))
    with caplog.at_level(logging.WARNING):
        assert h.feed(changed) == 0
    assert "different content" in caplog.text
    assert len(h.sink.of("on_status")) == 1


def test_FR13_session_cannot_move_backwards(h):
    h.feed(incoming("status", session=S1, at=T0))
    h.feed(incoming("status", session=S2, at=T0 - sec(1), received=T0 + sec(0.2)))
    h.feed(incoming("rssi", session=S2, seq=1, at=T0 + sec(0.3)),
           incoming("rssi", session=S1, seq=1, at=T0 + sec(0.3)))
    assert [s.session_id for s in h.sink.of("on_status")] == [S1]
    assert [f.session_id for f in h.sink.of("on_rssi")] == [S1]


def test_FR13_session_tie_keeps_current(h):
    h.feed(incoming("status", session=S1, at=T0))
    h.feed(incoming("status", session=S2, at=T0))
    assert [s.session_id for s in h.sink.of("on_status")] == [S1]
    h.feed(incoming("status", session=S2, at=T0 + sec(0.5)))
    h.feed(incoming("rssi", session=S1, seq=1, at=T0 + sec(0.6)),
           incoming("rssi", session=S2, seq=1, at=T0 + sec(0.6)))
    assert [s.session_id for s in h.sink.of("on_status")] == [S1, S2]
    assert [f.session_id for f in h.sink.of("on_rssi")] == [S2]


@pytest.mark.parametrize("observed,sent,accepted", [
    (T0 - sec(14.4), T0 - sec(14.4), True),     # age 14.5 + skew 0.4 < ttl 15
    (T0 - sec(14.6), T0 - sec(14.6), False),    # age 14.7 + skew 0.4 > ttl 15
    (T0 + sec(0.5), T0 + sec(0.5), True),       # within skew of reception
    (T0 + sec(0.6), T0, False),                 # future observation
    (T0, T0 + sec(0.6), False),                 # future send time
])
def test_FR13_stale_or_future_status_rejected(observed, sent, accepted):
    h = Harness(skew={"locker-1": 0.4})
    report = incoming("status", at=sent, received=T0 + sec(0.1), observed_at=iso(observed))
    h.feed(report)
    assert len(h.sink.of("on_status")) == (1 if accepted else 0)


def test_FR13_unverified_freshness_not_live():
    h = Harness(skew={})
    h.delivery.expect_check(check_request())
    h.feed(incoming("announce"), incoming("status"), incoming("rssi"),
           incoming("result"), incoming("safety_check_result"))
    assert [name for name, _ in h.sink.calls] == ["on_announce", "on_result"]
    with pytest.raises(ValueError):
        DeliveryConfig({"locker-1": -1.0}, 1, 1.0, 1.0)
    for limit, retry in ((0, 1.0), (1, 0), (True, 1.0), (1, float("nan"))):
        with pytest.raises(ValueError):
            DeliveryConfig({}, limit, retry, 1.0)


def test_FR07_old_session_result_forwarded(h):
    h.feed(incoming("status", session=S1, at=T0 - sec(2)),
           incoming("status", session=S2, at=T0))
    late = incoming("result", session=S1, seq=9, at=T0 - sec(60), received=T0 + sec(0.5))
    h.feed(late)
    assert [r.session_id for r in h.sink.of("on_result")] == [S1]
    h.feed(incoming("rssi", session=S2, seq=1, at=T0 + sec(0.6)))
    assert [f.session_id for f in h.sink.of("on_rssi")] == [S2]


def test_FR07_conflicting_event_not_hidden(h, caplog):
    first = incoming("result", seq=1)
    again = incoming("result", seq=2, at=T0 + sec(1), occurred_at=iso(T0))
    conflict = incoming("result", seq=3, at=T0 + sec(2), occurred_at=iso(T0), state="failed")
    with caplog.at_level(logging.WARNING):
        h.feed(first, again, conflict, replace(conflict, received_at=T0 + sec(3)))
    assert [r.state for r in h.sink.of("on_result")] == [
        NodeResultState.SUCCEEDED, NodeResultState.FAILED]
    assert {r.event_id for r in h.sink.of("on_result")} == {EVENT}
    assert "Conflicting report" in caplog.text


def test_FR07_retry_preserves_original_report(h):
    failures = iter([True, True])
    h.sink.fail = lambda method, report: next(failures, False)
    report = incoming("result", received=T0 + sec(0.7))
    h.feed(report)
    attempted = h.sink.attempts[0][1]
    for _ in range(2):
        h.advance(1)
        h.delivery.deliver_due()
    assert h.sink.of("on_result") == [attempted]
    assert len(h.sink.attempts) == 3
    assert all(r == attempted for _, r in h.sink.attempts)
    assert attempted.received_at == T0 + sec(0.7)
    assert attempted.event_id == EVENT
    assert h.delivery.pending_count == 0


def test_FR07_waiter_not_blocked_by_sink():
    release, entered, notified = threading.Event(), threading.Event(), []

    class BlockingSink(Sink):
        def on_result(self, report):
            entered.set()
            release.wait(5)
            super().on_result(report)

    sink = BlockingSink()
    delivery = ReportDelivery(sink, settings=Settings,
                              config=DeliveryConfig({"locker-1": 0.0}, 100, 0.05, 2.0))
    waiter = threading.Event()

    def listener(result):
        notified.append(result.command_id)
        if result.command_id == "command-2":
            waiter.set()

    delivery.add_result_listener(listener)
    delivery.start()
    try:
        delivery.submit(incoming("result"))
        assert entered.wait(2)
        delivery.submit(incoming("result", seq=2, command_id="command-2", event_id=EVENT2))
        assert waiter.wait(2), "command waiter blocked behind CORE handover"
        assert sink.of("on_result") == []
    finally:
        release.set()
        delivery.stop()
    assert notified == ["command-1", "command-2"]


@pytest.mark.parametrize("request_fields,result_fields,received", [
    (dict(check_id="check-2"), {}, None),
    (dict(node_id="light-1"), {}, None),
    ({}, dict(session=S2), None),
    (dict(criteria=CheckCriteria("locker-return", 2)), {}, None),
    (dict(issued_at=T0 + sec(0.5), expires_at=T0 + sec(3)), {}, None),
    (dict(issued_at=T0 - sec(3), expires_at=T0 - sec(0.5)), {}, None),
    ({}, {}, T0 + sec(3.5)),
    ({}, dict(evidence={name: dict(value=True, observed_at=iso(T0 - sec(2)))
                        for name in ("door_closed", "locked")}), None),
], ids=["unknown-id", "other-node", "other-session", "other-criteria", "before-request",
        "after-expiry", "stale", "old-evidence"])
def test_FR20_invalid_check_not_close_evidence(h, request_fields, result_fields, received):
    h.delivery.expect_check(check_request(**request_fields))
    h.feed(incoming("safety_check_result", received=received, **result_fields))
    assert h.sink.of("on_safety_check_result") == []
    h2 = Harness()
    h2.delivery.expect_check(check_request())
    h2.feed(incoming("safety_check_result"))
    assert len(h2.sink.of("on_safety_check_result")) == 1


def evidence(door, locked, *, at=T0):
    return {name: dict(value=value, observed_at=iso(at))
            for name, value in (("door_closed", door), ("locked", locked))}


@pytest.mark.parametrize("fields,criteria,delivered", [
    (dict(evidence={"door_closed": dict(value=True, observed_at=iso(T0))}), None, False),
    (dict(evidence=evidence(True, None)), None, False),
    (dict(evidence=evidence(True, False)), None, False),
    (dict(outcome="not_ready", evidence=evidence(True, True)), None, False),
    (dict(criteria_version=2), CheckCriteria("locker-return", 2), False),
    (dict(outcome="not_ready", evidence=evidence(True, False)), None, True),
    (dict(outcome="unknown", evidence=evidence(True, True)), None, True),
    (dict(outcome="fault", evidence=evidence(False, False)), None, True),
], ids=["missing-item", "null-value", "passed-with-false", "not-ready-all-true",
        "unsupported-version", "valid-not-ready", "unknown", "fault"])
def test_FR20_incomplete_evidence_unconfirmed(h, fields, criteria, delivered):
    h.delivery.expect_check(check_request(**({"criteria": criteria} if criteria else {})))
    h.feed(incoming("safety_check_result", **fields))
    results = h.sink.of("on_safety_check_result")
    assert len(results) == (1 if delivered else 0)
    if delivered:
        assert results[0].outcome == SafetyCheckOutcome(fields["outcome"])


def test_FR20_expect_check_conflict_rejected(h):
    h.delivery.expect_check(check_request())
    h.delivery.expect_check(check_request())
    with pytest.raises(ValueError):
        h.delivery.expect_check(check_request(target_session_id=S2))


def test_FR07_restart_does_not_claim_delivery():
    sink = Sink()
    sink.fail = lambda method, report: True
    attempted = threading.Event()
    original = sink._call

    def call(method, report):
        attempted.set()
        original(method, report)

    sink._call = call
    delivery = ReportDelivery(sink, settings=Settings,
                              config=DeliveryConfig({"locker-1": 0.0}, 100, 0.05, 2.0))
    delivery.start()
    report = incoming("result")
    delivery.submit(report)
    assert attempted.wait(2)
    delivery.stop()
    assert delivery.pending_count == 1
    assert sink.calls == []
    # A new server instance has no memory of the report: it is neither
    # marked delivered nor deduplicated, so a re-report is handed over again.
    restarted = Harness(sink=Sink())
    restarted.feed(report)
    assert [r.event_id for r in restarted.sink.of("on_result")] == [EVENT]


def test_FR07_saturation_keeps_results(caplog):
    h = Harness(limit=2)
    h.sink.fail = lambda method, report: True
    with caplog.at_level(logging.WARNING):
        h.feed(incoming("status", seq=1), incoming("rssi", seq=1, at=T0 + sec(0.1)))
        assert h.delivery.saturated
        h.feed(incoming("status", seq=2, at=T0 + sec(0.5)),
               incoming("announce", seq=1),
               incoming("result"), incoming("result", seq=2, command_id="command-2",
                                            event_id=EVENT2))
        assert h.delivery.pending_count == 4
        h.sink.fail = lambda method, report: False
        h.advance(1)
        h.delivery.deliver_due()
    assert not h.delivery.saturated
    assert [s.seq for s in h.sink.of("on_status")] == [1]
    assert h.sink.of("on_announce") == []
    assert len(h.sink.of("on_result")) == 2
    assert "saturated" in caplog.text and "handover saturated" in caplog.text
    # A status rejected while saturated did not move the tracked sequence.
    h.feed(incoming("status", seq=2, at=T0 + sec(0.5)))
    assert [s.seq for s in h.sink.of("on_status")] == [1, 2]


def test_FR07_failed_lane_does_not_block_others(h):
    h.sink.fail = lambda method, report: (method == "on_result"
                                          and report.command_id == "command-1")
    h.feed(incoming("result"),
           incoming("result", seq=2, command_id="command-2", event_id=EVENT2),
           incoming("status"))
    assert [r.command_id for r in h.sink.of("on_result")] == ["command-2"]
    assert len(h.sink.of("on_status")) == 1
    assert h.delivery.pending_count == 1


def test_NFR08_rejection_logs_omit_secrets(h, caplog):
    secret = "device-password-123"
    bad = IncomingReport("nearpilot/node/locker-1/status",
                         ('{"password": "%s", "schema_version": 2}' % secret).encode(),
                         1, False, False, T0)
    h.sink.fail = lambda method, report: True
    with caplog.at_level(logging.DEBUG):
        h.feed(bad, incoming("status", password=secret))
    assert "node=locker-1" in caplog.text and "kind=status" in caplog.text
    assert secret not in caplog.text
    assert "core-secret" not in caplog.text
