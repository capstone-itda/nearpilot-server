"""MQTT 시각 요청·응답 (FR-07, #236, specs/iot-mqtt-time-sync/spec.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from nearpilot.iot.messages import MessageValidationError, decode_time_request, encode_time
from nearpilot.iot.mqtt import DeliveryConfig, NotReady, PublishUncertain, TimeResponder

SESSION = "12345678-1234-4234-8234-123456789abc"
TOPIC = "nearpilot/node/light-1/time_request"
T0 = datetime(2026, 11, 5, 3, 0, 0, 250000, tzinfo=timezone.utc)
CONFIG = DeliveryConfig(0.5, 120, 60, 10, 0.5, 1.0)


def request(**changes) -> bytes:
    body = dict(schema_version=2, node_id="light-1", session_id=SESSION, request_id="r-1")
    body.update(changes)
    return json.dumps({k: v for k, v in body.items() if v is not None}).encode("utf-8")


class Connection:
    def __init__(self, error=None):
        self.error = error
        self.published = []

    def publish_request(self, node_id, kind, payload):
        if self.error is not None:
            raise self.error
        self.published.append((node_id, kind, json.loads(payload)))


def responder(connection, clock=lambda: T0):
    result = TimeResponder(CONFIG, clock=clock)
    result.connection = connection
    return result


def test_FR07_time_request_decoded():
    assert decode_time_request(TOPIC, request()) == ("light-1", "r-1")


@pytest.mark.parametrize("topic,payload", [
    (TOPIC, request(schema_version=1)),
    (TOPIC, request(node_id="light-2")),
    (TOPIC, request(request_id=None)),
    (TOPIC, request(request_id="")),
    (TOPIC, request(request_id="x" * 65)),
    (TOPIC, request(request_id="a\nb")),
    (TOPIC, request(session_id="not-a-uuid")),
    (TOPIC, b"not json"),
    ("nearpilot/node/light-1/status", request()),
    ("nearpilot/node/+/time_request", request()),
])
def test_FR07_time_request_rejects_invalid(topic, payload, caplog):
    with pytest.raises(MessageValidationError):
        decode_time_request(topic, payload)
    connection = Connection()
    responder(connection).handle(topic, payload)
    assert connection.published == []
    assert "r-1" not in caplog.text and SESSION not in caplog.text  # Payloads are never logged (NFR-08).


def test_FR07_time_reply_carries_server_time_and_settings():
    stamps = iter([T0, T0 + timedelta(seconds=9)])
    connection = Connection()
    responder(connection, clock=lambda: next(stamps)).handle(TOPIC, request(request_id="r-7"))
    assert connection.published == [("light-1", "time", dict(
        schema_version=2, node_id="light-1", request_id="r-7", server_time="2026-11-05T03:00:00.250000Z",
        max_error_sec=0.5, trust_sec=120, resync_sec=60))]


def test_FR07_time_reply_takes_clock_at_publish():
    """The server time is read while answering, not when the request was queued."""
    calls = []
    connection = Connection()
    responder(connection, clock=lambda: calls.append(1) or T0).handle(TOPIC, request())
    assert len(calls) == 1 and len(connection.published) == 1


def test_FR07_time_responder_skips_when_not_ready():
    connection = Connection(NotReady("not ready"))
    responder(connection).handle(TOPIC, request())
    assert connection.published == []
    TimeResponder(CONFIG, clock=lambda: T0).handle(TOPIC, request())  # No connection bound yet.


@pytest.mark.parametrize("error", [PublishUncertain("uncertain"), ValueError("bad")])
def test_FR07_time_responder_survives_publish_failure(error, caplog):
    responder(Connection(error)).handle(TOPIC, request())
    assert "time reply not sent" in caplog.text


def test_FR07_time_encode_requires_utc():
    with pytest.raises(MessageValidationError):
        encode_time("light-1", "r-1", datetime(2026, 11, 5), max_error_sec=0.5, trust_sec=120, resync_sec=60)
    with pytest.raises(MessageValidationError):
        encode_time("light/1", "r-1", T0, max_error_sec=0.5, trust_sec=120, resync_sec=60)
