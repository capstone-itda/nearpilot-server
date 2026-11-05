"""MQTT 기기 원격 재시작 (NFR-04, #235, specs/iot-node-restart/spec.md)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.iot.messages import MessageValidationError, encode_restart
from nearpilot.iot.mqtt import MqttNodeRestarter, NotReady, PublishUncertain
from nearpilot.iot.mqtt.connection import REQUEST_DELIVERY
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import RestartOutcome
from nearpilot.shared.interfaces import NodeRestarter

SESSION = "12345678-1234-4234-8234-123456789abc"
T0 = datetime(2026, 11, 5, 3, 0, 0, 250000, tzinfo=timezone.utc)


class Connection:
    def __init__(self, error=None):
        self.error = error
        self.published = []

    def publish_request(self, node_id, kind, payload):
        self.published.append((node_id, kind, json.loads(payload)))
        if self.error is not None:
            raise self.error


class Delivery:
    def __init__(self, session=SESSION):
        self.session = session
        self.asked = []

    def current_session(self, node_id):
        self.asked.append(node_id)
        return self.session


def restarter(connection, delivery, *, timeout=3.0):
    return MqttNodeRestarter(connection, delivery, settings=lambda: Settings(exec_timeout_sec=timeout),
                             clock=lambda: T0)


def test_NFR04_restart_body():
    body = json.loads(encode_restart("locker-1", "r-1", SESSION, T0, T0 + timedelta(seconds=3)))
    assert body == dict(schema_version=2, node_id="locker-1", restart_id="r-1", target_session_id=SESSION,
                        issued_at="2026-11-05T03:00:00.250000Z", expires_at="2026-11-05T03:00:03.250000Z")


@pytest.mark.parametrize("node_id,restart_id,session,expires", [
    ("locker/1", "r-1", SESSION, T0 + timedelta(seconds=3)),
    ("locker-1", "", SESSION, T0 + timedelta(seconds=3)),
    ("locker-1", "x" * 65, SESSION, T0 + timedelta(seconds=3)),
    ("locker-1", "a\nb", SESSION, T0 + timedelta(seconds=3)),
    ("locker-1", "r-1", "not-a-uuid", T0 + timedelta(seconds=3)),
    ("locker-1", "r-1", SESSION, T0),
])
def test_NFR04_restart_body_rejects_invalid(node_id, restart_id, session, expires):
    with pytest.raises(MessageValidationError):
        encode_restart(node_id, restart_id, session, T0, expires)


def test_NFR04_restarter_targets_current_connection():
    connection, delivery = Connection(), Delivery()
    target = restarter(connection, delivery, timeout=4.5)
    assert isinstance(target, NodeRestarter)
    assert target.restart("locker-1", "r-1") is RestartOutcome.SENT
    [(node_id, kind, body)] = connection.published
    assert (node_id, kind) == ("locker-1", "restart")
    assert body["target_session_id"] == SESSION and body["restart_id"] == "r-1"
    assert body["expires_at"] == "2026-11-05T03:00:04.750000Z"  # issued_at + exec_timeout_sec
    assert "user_id" not in body and "token" not in body
    assert REQUEST_DELIVERY["restart"] == (1, False)  # QoS 1, not retained


def test_NFR04_restarter_node_offline():
    connection = Connection()
    assert restarter(connection, Delivery(session=None)).restart("locker-1", "r-1") is RestartOutcome.NODE_OFFLINE
    assert connection.published == []


@pytest.mark.parametrize("error", [NotReady("not ready"), PublishUncertain("unknown")])
def test_NFR04_restarter_not_sent(error, caplog):
    connection = Connection(error)
    assert restarter(connection, Delivery()).restart("locker-1", "r-1") is RestartOutcome.NOT_SENT
    assert len(connection.published) == 1  # 한 번만 시도하고 다시 보내지 않는다
    assert "locker-1" in caplog.text


def test_NFR04_restarter_invalid_id_not_sent():
    connection = Connection()
    assert restarter(connection, Delivery()).restart("locker-1", "x" * 65) is RestartOutcome.NOT_SENT
    assert connection.published == []
