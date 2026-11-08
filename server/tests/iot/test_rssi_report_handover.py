"""빈 보고를 포함한 유효한 rssi 메시지의 인계 (FR-09, #252, specs/core-distance-selector/spec.md).

IOT는 비콘이 없는 빈 rssi 보고도 버리지 않고 `NodeEventSink.on_rssi_report` 로 한 번 넘긴다.
메시지 해석(`decode_rssi_report`)과 인계 순서, 재시도, 거부를 이 파일 안의 fake sink 로 확인한다.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging

import pytest

from nearpilot.iot import messages
from nearpilot.iot.messages import MessageValidationError, decode_message, decode_rssi_report
from nearpilot.iot.mqtt.connection import IncomingReport
from nearpilot.iot.mqtt.delivery import DeliveryConfig, ReportDelivery
from nearpilot.shared.config import Settings
from nearpilot.shared.interfaces import NodeEventSink
from nearpilot.shared.models import BeaconKey, RssiFrame, RssiReport

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
BEACON = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NODE = "light-1"
TOPIC = f"nearpilot/node/{NODE}/rssi"
T0 = datetime(2026, 11, 8, 3, 0, 0, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def sec(value):
    return timedelta(seconds=value)


def observation(minor=1, rssi=-60):
    return dict(uuid=BEACON, major=1, minor=minor, rssi=rssi)


def status(*, session=S1, seq=1, at=T0):
    data = dict(schema_version=2, node_id=NODE, session_id=session, seq=seq, sent_at=iso(at),
                observed_at=iso(at), command_state="idle")
    return IncomingReport(f"nearpilot/node/{NODE}/status", json.dumps(data).encode(),
                          1, False, False, at + sec(0.1))


def rssi_data(observations=(), *, session=S1, seq=1, frame_ts=T0 + sec(0.2), sent_at=None):
    return dict(schema_version=2, node_id=NODE, session_id=session, seq=seq,
                sent_at=iso(frame_ts if sent_at is None else sent_at), frame_ts=iso(frame_ts),
                observations=list(observations))


def rssi(observations=(), *, received_at=None, **fields):
    """rssi 메시지. 관측을 안 주면 빈 보고다. 수신 시각을 안 주면 관측 시각 0.1초 뒤에 받는다."""
    data = rssi_data(observations, **fields)
    if received_at is None:
        received_at = fields.get("frame_ts", T0 + sec(0.2)) + sec(0.1)
    return IncomingReport(TOPIC, json.dumps(data).encode(), 0, False, False, received_at)


class Sink:
    """NodeEventSink fake. 성공한 호출은 `calls`, 실패한 시도까지는 `attempts` 에 부른 순서대로 남긴다."""

    def __init__(self):
        self.calls = []
        self.attempts = []
        self.fail = lambda method, item: False

    def _call(self, method, item):
        self.attempts.append((method, item))
        if self.fail(method, item):
            raise RuntimeError("CORE unavailable")
        self.calls.append((method, item))

    def on_announce(self, item):
        self._call("on_announce", item)

    def on_rssi(self, item):
        self._call("on_rssi", item)

    def on_rssi_report(self, item):
        self._call("on_rssi_report", item)

    def on_status(self, item):
        self._call("on_status", item)

    def on_result(self, item):
        self._call("on_result", item)

    def of(self, method):
        return [item for name, item in self.calls if name == method]

    @property
    def names(self):
        return [name for name, _ in self.calls]


class Harness:
    def __init__(self, *, skew=0.0, limit=100):
        self.sink = Sink()
        self.now = T0 + sec(1)
        self.delivery = ReportDelivery(
            self.sink, settings=Settings,
            config=DeliveryConfig(skew, 120, 60, limit, 1.0, 2.0), clock=lambda: self.now)

    def feed(self, *reports):
        for report in reports:
            self.delivery.process(report)
        return self.delivery.deliver_due()

    def advance(self, seconds):
        self.now += sec(seconds)


@pytest.fixture
def h():
    return Harness()


# ── 인계 ────────────────────────────────────────────────────────────


def test_FR09_iot_empty_rssi_report_handed_over(h):
    h.feed(status())

    # 빈 보고는 on_rssi 없이 on_rssi_report 한 번으로 넘어간다. 값은 보고의 머리 그대로다.
    frame_ts, sent_at, received_at = T0 + sec(0.2), T0 + sec(0.25), T0 + sec(0.3)
    assert h.feed(rssi(seq=1, frame_ts=frame_ts, sent_at=sent_at, received_at=received_at)) == 1
    assert h.sink.of("on_rssi") == []
    assert h.sink.of("on_rssi_report") == [RssiReport(NODE, S1, 1, frame_ts, sent_at, received_at)]

    # 관측이 둘이면 on_rssi 두 번 뒤에 on_rssi_report 한 번이다. 같은 차선이라 순서가 바뀌지 않는다.
    h.sink.calls.clear()
    assert h.feed(rssi([observation(1), observation(2)], seq=2, frame_ts=T0 + sec(0.5))) == 3
    assert h.sink.names == ["on_rssi", "on_rssi", "on_rssi_report"]
    frames, (report,) = h.sink.of("on_rssi"), h.sink.of("on_rssi_report")
    assert [frame.beacon.minor for frame in frames] == [1, 2]
    assert report == RssiReport(NODE, S1, 2, T0 + sec(0.5), T0 + sec(0.5), T0 + sec(0.6))
    assert {(f.session_id, f.seq, f.frame_ts, f.sent_at, f.received_at) for f in frames} == {
        (report.session_id, report.seq, report.frame_ts, report.sent_at, report.received_at)}

    # 상태 보고 전에 온 빈 보고와 beacon_recent_sec 밖의 오래된 빈 보고는 인계하지 않는다.
    early = Harness()
    early.feed(rssi(seq=1))
    assert early.sink.calls == []
    old = Harness()
    old.feed(status())
    old.feed(rssi(seq=1, frame_ts=T0 - sec(3), received_at=T0 + sec(0.1)))
    assert old.sink.names == ["on_status"]

    # 같은 순번의 같은 빈 보고를 다시 받으면 한 번만 인계한다.
    again = Harness()
    again.feed(status())
    first = rssi(seq=1)
    again.feed(first)
    assert again.feed(replace(first, received_at=first.received_at + sec(1), dup=True)) == 0
    assert len(again.sink.of("on_rssi_report")) == 1


def test_FR09_iot_reports_keep_lane_order_across_messages(h):
    h.feed(status())
    h.feed(rssi(seq=1, frame_ts=T0 + sec(0.2)),
           rssi([observation(1), observation(2)], seq=2, frame_ts=T0 + sec(0.4)),
           rssi(seq=3, frame_ts=T0 + sec(0.6)))
    assert h.sink.names == ["on_status", "on_rssi_report", "on_rssi", "on_rssi", "on_rssi_report",
                            "on_rssi_report"]
    assert [report.seq for report in h.sink.of("on_rssi_report")] == [1, 2, 3]


def test_FR09_iot_failed_frame_holds_back_report(h):
    h.feed(status())
    h.sink.fail = lambda method, item: method == "on_rssi" and item.beacon.minor == 2
    h.feed(rssi([observation(1), observation(2)], seq=1))
    # 두 번째 프레임이 실패한 동안 같은 보고의 on_rssi_report 는 그 뒤에서 기다린다.
    assert h.sink.names == ["on_status", "on_rssi"]
    assert h.delivery.pending_count == 2
    h.sink.fail = lambda method, item: False
    h.advance(1)
    h.delivery.deliver_due()
    assert h.sink.names == ["on_status", "on_rssi", "on_rssi", "on_rssi_report"]
    assert h.delivery.pending_count == 0


def test_FR09_iot_failed_report_retried_without_resending_frames(h):
    h.feed(status())
    failures = iter([True, True])
    h.sink.fail = lambda method, item: method == "on_rssi_report" and next(failures, False)
    h.feed(rssi([observation(1)], seq=1, received_at=T0 + sec(0.7)))
    assert h.sink.names == ["on_status", "on_rssi"]
    for _ in range(2):
        h.advance(1)
        h.delivery.deliver_due()
    assert h.sink.names == ["on_status", "on_rssi", "on_rssi_report"]
    attempts = [item for name, item in h.sink.attempts if name == "on_rssi_report"]
    assert len(attempts) == 3 and all(item == attempts[0] for item in attempts)
    assert attempts[0].received_at == T0 + sec(0.7)  # 다시 인계해도 처음 받은 시각이다
    assert h.delivery.pending_count == 0


# ── 거부 ────────────────────────────────────────────────────────────


def test_FR09_iot_empty_rssi_report_needs_live_session(caplog):
    h = Harness()
    with caplog.at_level(logging.WARNING):
        h.feed(rssi(seq=1))  # 상태 보고 전
        h.feed(status(session=S1))
        h.feed(rssi(session=S2, seq=1))  # 확인한 연결과 다른 연결
    assert h.sink.of("on_rssi_report") == []
    assert caplog.text.count("connection not confirmed by live status") == 2
    h.feed(rssi(session=S1, seq=1))
    assert [report.session_id for report in h.sink.of("on_rssi_report")] == [S1]


@pytest.mark.parametrize("observations", [[], [observation(1)]], ids=["empty", "frames"])
@pytest.mark.parametrize("frame_ts,sent_at,accepted", [
    (T0 - sec(2.4), T0 - sec(2.4), True),    # 나이 2.5 + 어긋남 0.4 < beacon_recent_sec 3
    (T0 - sec(2.6), T0 - sec(2.6), False),   # 나이 2.7 + 어긋남 0.4 > beacon_recent_sec 3
    (T0 - sec(2.6), T0, False),              # 발행이 최근이어도 나이는 관측 시각으로 잰다
    (T0 + sec(0.5), T0 + sec(0.5), True),    # 수신 시각 + 어긋남 안
    (T0 + sec(0.6), T0, False),              # 미래의 관측 시각
    (T0, T0 + sec(0.6), False),              # 미래의 발행 시각
])
def test_FR09_iot_stale_or_future_rssi_report(observations, frame_ts, sent_at, accepted):
    """빈 보고도 프레임이 있는 보고와 같은 시각 검사를 거친다."""
    h = Harness(skew=0.4)
    h.feed(status())
    h.feed(rssi(observations, frame_ts=frame_ts, sent_at=sent_at, received_at=T0 + sec(0.1)))
    assert len(h.sink.of("on_rssi_report")) == (1 if accepted else 0)
    assert len(h.sink.of("on_rssi")) == (len(observations) if accepted else 0)


@pytest.mark.parametrize("observations", [[], [observation(1)]], ids=["empty", "frames"])
def test_FR09_iot_duplicate_rssi_report_handed_over_once(h, caplog, observations):
    h.feed(status())
    first = rssi(observations, seq=3)
    h.feed(first)
    with caplog.at_level(logging.WARNING):
        assert h.feed(replace(first, received_at=first.received_at + sec(1), dup=True)) == 0
    assert len(h.sink.of("on_rssi_report")) == 1
    assert len(h.sink.of("on_rssi")) == len(observations)
    assert "different content" not in caplog.text  # 같은 내용의 재수신은 거부가 아니다


FRAMES = dict(observations=[observation(1)])


@pytest.mark.parametrize("first,second", [
    (dict(), FRAMES),                                            # 빈 보고 뒤, 같은 순번의 프레임 보고
    (FRAMES, dict()),                                            # 프레임 보고 뒤, 같은 순번의 빈 보고
    (FRAMES, dict(observations=[observation(1, rssi=-70)])),     # 프레임 값이 다르다
    (dict(), dict(frame_ts=T0 + sec(0.25))),                     # 관측 시각이 다르다
    (dict(), dict(sent_at=T0 + sec(0.15))),                      # 발행 시각이 다르다
])
def test_FR09_iot_reused_sequence_with_different_content_rejected(h, caplog, first, second):
    h.feed(status())
    h.feed(rssi(seq=4, **first))
    delivered = list(h.sink.calls)
    with caplog.at_level(logging.WARNING):
        assert h.feed(rssi(seq=4, **second)) == 0
    assert "sequence reused with different content" in caplog.text
    assert h.sink.calls == delivered


def test_FR09_iot_empty_rssi_report_checks_sequence_and_time_order(h, caplog):
    h.feed(status())
    h.feed(rssi(seq=5, frame_ts=T0 + sec(0.5)))
    with caplog.at_level(logging.WARNING):
        h.feed(rssi(seq=4, frame_ts=T0 + sec(0.6)))                      # 낮은 순번
        h.feed(rssi(seq=6, frame_ts=T0 + sec(0.4)))                      # 순번은 크지만 관측이 이르다
        h.feed(rssi([observation(1)], seq=3, frame_ts=T0 + sec(0.7)))    # 빈 보고가 정한 순번보다 낮다
    assert caplog.text.count("out-of-order sequence") == 2
    assert caplog.text.count("observation older than current") == 1
    assert [report.seq for report in h.sink.of("on_rssi_report")] == [5]
    assert h.sink.of("on_rssi") == []
    h.feed(rssi([observation(1)], seq=6, frame_ts=T0 + sec(0.8)))
    assert [report.seq for report in h.sink.of("on_rssi_report")] == [5, 6]
    assert [frame.seq for frame in h.sink.of("on_rssi")] == [6]


def test_FR09_iot_saturated_handover_rejects_rssi_report(caplog):
    h = Harness(limit=1)
    h.sink.fail = lambda method, item: True
    h.feed(status())
    assert h.delivery.saturated
    with caplog.at_level(logging.WARNING):
        h.feed(rssi(seq=1))
    assert "kind=rssi reason=handover saturated" in caplog.text
    assert h.delivery.pending_count == 1  # 거부한 보고는 대기열에 넣지 않았다
    h.sink.fail = lambda method, item: False
    h.advance(1)
    h.delivery.deliver_due()
    assert h.sink.names == ["on_status"]
    h.feed(rssi(seq=1))  # 거부한 보고는 순번을 옮기지 않았다
    assert [report.seq for report in h.sink.of("on_rssi_report")] == [1]


@pytest.mark.parametrize("payload", [
    b"not json",
    json.dumps(rssi_data() | dict(observations="x")).encode(),
    json.dumps(rssi_data([observation(1), observation(1, -70)])).encode(),
    json.dumps(rssi_data() | dict(schema_version=3)).encode(),
    json.dumps(rssi_data() | dict(frame_ts="yesterday", password="device-password-123")).encode(),
], ids=["not-json", "observations-not-list", "duplicate-beacon", "schema-version", "frame-ts"])
def test_FR09_iot_invalid_rssi_message_rejected(h, caplog, payload):
    h.feed(status())
    with caplog.at_level(logging.DEBUG):
        h.feed(IncomingReport(TOPIC, payload, 0, False, False, T0 + sec(0.3)))
    assert h.sink.names == ["on_status"]
    assert f"node={NODE} kind=rssi" in caplog.text
    assert "device-password-123" not in caplog.text  # 거부 기록에 페이로드를 남기지 않는다 (NFR-08)


@pytest.mark.parametrize("topic", ["other/node/light-1/rssi", "nearpilot/node/+/rssi", "nearpilot/node//rssi"])
def test_FR09_iot_rssi_topic_checked(h, caplog, topic):
    h.feed(status())
    with caplog.at_level(logging.WARNING):
        h.feed(IncomingReport(topic, json.dumps(rssi_data()).encode(), 0, False, False, T0 + sec(0.3)))
    assert h.sink.names == ["on_status"]
    assert "kind=rssi" in caplog.text


def test_FR09_iot_empty_rssi_report_does_not_refresh_online_state(h):
    """빈 보고는 기기의 온라인 확인을 늘리지 않는다. 연결 확인은 상태 보고로만 늘어난다."""
    h.feed(status())  # 수신 T0 + 0.1
    ttl = sec(Settings().online_ttl_sec)
    h.feed(rssi(seq=1, frame_ts=T0 + ttl, received_at=T0 + ttl + sec(0.1)))
    assert len(h.sink.of("on_rssi_report")) == 1
    h.now = T0 + sec(0.1) + ttl + sec(0.001)
    assert h.delivery.current_session(NODE) is None


def test_FR09_iot_fake_sink_satisfies_protocol():
    assert isinstance(Sink(), NodeEventSink)


# ── 해석 ────────────────────────────────────────────────────────────

FRAME_TS, SENT_AT, RECEIVED_AT = T0 + sec(0.2), T0 + sec(0.25), T0 + sec(0.3)


def payload_of(data):
    return json.dumps(data).encode()


def decode_pair(payload):
    return (decode_rssi_report(TOPIC, payload, received_at=RECEIVED_AT),
            decode_message(TOPIC, payload, received_at=RECEIVED_AT))


def test_FR09_decode_rssi_report_header_and_frames():
    data = rssi_data([observation(1, -60), observation(2, -70.5)], seq=7, frame_ts=FRAME_TS, sent_at=SENT_AT)
    (header, frames), by_message = decode_pair(payload_of(data))
    assert header == RssiReport(NODE, S1, 7, FRAME_TS, SENT_AT, RECEIVED_AT)
    assert frames == (
        RssiFrame(NODE, BeaconKey(BEACON, 1, 1), -60, FRAME_TS, S1, 7, SENT_AT, RECEIVED_AT),
        RssiFrame(NODE, BeaconKey(BEACON, 1, 2), -70.5, FRAME_TS, S1, 7, SENT_AT, RECEIVED_AT),
    )
    assert by_message == frames  # decode_message 의 반환은 그대로다 (프레임 튜플)


def test_FR09_decode_rssi_report_empty_scan():
    data = rssi_data(seq=3, frame_ts=FRAME_TS, sent_at=SENT_AT)
    (header, frames), by_message = decode_pair(payload_of(data))
    assert header == RssiReport(NODE, S1, 3, FRAME_TS, SENT_AT, RECEIVED_AT)
    assert frames == () and by_message == ()


def test_FR09_decode_rssi_report_exported():
    assert "decode_rssi_report" in messages.__all__


@pytest.mark.parametrize("kind", ["announce", "status", "result", "time_request", "cmd", "unknown"])
def test_FR09_decode_rssi_report_rejects_other_topics(kind):
    # 페이로드는 rssi 로 올바르다. 거부 이유는 토픽뿐이다.
    with pytest.raises(MessageValidationError, match="topic"):
        decode_rssi_report(f"nearpilot/node/{NODE}/{kind}", payload_of(rssi_data()), received_at=RECEIVED_AT)


@pytest.mark.parametrize("topic", ["other/node/light-1/rssi", "nearpilot/node//rssi", "nearpilot/node/+/rssi",
                                   "nearpilot/node/light-1/rssi/extra", "nearpilot/node/light-1"])
def test_FR09_decode_rssi_report_rejects_malformed_topic(topic):
    with pytest.raises(MessageValidationError, match="topic"):
        decode_rssi_report(topic, payload_of(rssi_data()), received_at=RECEIVED_AT)


CHANGES = {
    "observations-none": dict(observations=None),
    "observations-text": dict(observations="x"),
    "observation-none": dict(observations=[None]),
    "duplicate-beacon": dict(observations=[observation(1), observation(1, -90)]),
    "uuid-bad": dict(observations=[observation(1) | dict(uuid="bad")]),
    "uuid-upper": dict(observations=[observation(1) | dict(uuid=BEACON.upper())]),
    "major-range": dict(observations=[observation(1) | dict(major=65536)]),
    "rssi-bool": dict(observations=[observation(1) | dict(rssi=True)]),
    "rssi-text": dict(observations=[observation(1) | dict(rssi="-60")]),
    "frame-ts-text": dict(frame_ts="yesterday"),
    "frame-ts-none": dict(frame_ts=None),
    "seq-zero": dict(seq=0),
    "session-bad": dict(session_id="bad"),
    "sent-at-naive": dict(sent_at="2026-11-08T03:00:00"),
    "schema-3": dict(schema_version=3),
    "schema-1": dict(schema_version=1),
    "node-other": dict(node_id="other-node"),
}


def assert_same_rejection(payload, received_at=RECEIVED_AT):
    """decode_rssi_report 와 decode_message 가 같은 이유로 거부한다."""
    with pytest.raises(MessageValidationError) as report_error:
        decode_rssi_report(TOPIC, payload, received_at=received_at)
    with pytest.raises(MessageValidationError) as message_error:
        decode_message(TOPIC, payload, received_at=received_at)
    assert str(report_error.value) == str(message_error.value)


@pytest.mark.parametrize("change", list(CHANGES.values()), ids=list(CHANGES))
def test_FR09_decode_rssi_report_validates_like_decode_message(change):
    assert_same_rejection(payload_of(rssi_data([observation(1)]) | change))


@pytest.mark.parametrize("key", list(rssi_data()))
def test_FR09_decode_rssi_report_requires_every_field(key):
    data = rssi_data([observation(1)])
    del data[key]
    assert_same_rejection(payload_of(data))


@pytest.mark.parametrize("payload", [b"\xff", b"[]", b"{", b'{"schema_version":2,"schema_version":2}'])
def test_FR09_decode_rssi_report_rejects_invalid_json(payload):
    assert_same_rejection(payload)


@pytest.mark.parametrize("received_at", [
    datetime(2026, 11, 8, 3, 0, 0),                                       # 시간대 없음
    datetime(2026, 11, 8, 3, 0, 0, tzinfo=timezone(timedelta(hours=1))),  # UTC 가 아니다
    "2026-11-08T03:00:00Z",
])
def test_FR09_decode_rssi_report_requires_utc_received_at(received_at):
    assert_same_rejection(payload_of(rssi_data()), received_at)
