"""FR-09·FR-11 SmartThings 보고 상태 조회 (specs/iot-device-reported-state)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from nearpilot.iot.smartthings.http import HttpSmartThingsTransport
from nearpilot.iot.smartthings.reported import (
    ReportedStateRouter, SmartThingsReportedState, reported_values,
)
from nearpilot.iot.smartthings.transport import SmartThingsNotSent, SmartThingsTransportError

DEVICE = "dev-0f1e2d3c-4b5a"
NOW = datetime(2026, 11, 14, 6, 30, tzinfo=timezone.utc)

# 2026-11-14 display-1(M5)에서 읽은 응답의 모양. 음량은 전날 값으로 남아 있었다.
STATUS = {
    "switch": {"switch": {"value": "off", "timestamp": "2026-11-14T06:30:02.700Z"}},
    "audioVolume": {"volume": {"value": 2, "unit": "%", "timestamp": "2026-11-13T12:31:14.999Z"}},
    "audioMute": {"mute": {"value": "unmuted", "timestamp": "2026-11-13T12:16:14.619Z"}},
    "tvChannel": {"tvChannel": {"value": "501", "timestamp": "2026-11-13T12:31:22.000Z"},
                  "tvChannelName": {"value": "", "timestamp": "2026-11-13T12:31:22.000Z"}},
    "custom.launchapp": {},
    "healthCheck": {"DeviceWatch-DeviceStatus": {"value": "online"}},
}


class FakeSource:
    def __init__(self, status=None, error: Exception | None = None):
        self.status = STATUS if status is None else status
        self.error = error
        self.calls: list[str] = []

    def get_component_status(self, device_id):
        self.calls.append(device_id)
        if self.error is not None:
            raise self.error
        return self.status


def reader(source, cache_sec=2.0):
    clock = [0.0]
    r = SmartThingsReportedState(source, DEVICE, "display-1", cache_sec=lambda: cache_sec,
                                 clock=lambda: NOW, monotonic=lambda: clock[0])
    return r, clock


def as_dict(state):
    return {v.key: (v.value, v.observed_at) for v in state.values}


def test_FR09_reported_values_with_observed_at():
    state, _ = reader(FakeSource())
    result = state.read()
    assert result.node_id == "display-1" and result.read_at == NOW
    values = as_dict(result)
    assert values["power"] == ("off", datetime(2026, 11, 14, 6, 30, 2, 700000, tzinfo=timezone.utc))
    assert values["volume"] == (2, datetime(2026, 11, 13, 12, 31, 14, 999000, tzinfo=timezone.utc))
    assert values["mute"][0] == "unmuted" and values["channel"][0] == "501" and values["app"][0] == ""
    assert [v.key for v in result.values].count("power") == 1  # on·off 는 한 키다


def test_FR09_only_declared_actions_reported():
    """동작의 requires capability가 응답에 모두 있는 동작만 담는다. 선언 합성과 같은 규칙이다."""
    status = {key: value for key, value in STATUS.items() if key != "custom.launchapp"}
    keys = {v.key for v in reported_values(status)}
    assert "app" not in keys  # launch_app 은 custom.launchapp 이 있어야 선언한다
    assert "picture_mode" not in keys and "sound_mode" not in keys  # 응답에 없는 capability
    assert "DeviceWatch-DeviceStatus" not in keys and "healthCheck" not in keys  # 동작 표 밖


def test_FR09_missing_attribute_or_timestamp():
    status = {"switch": {"switch": {"value": "on"}}, "audioVolume": {},
              "audioMute": {"mute": {"value": None, "timestamp": "not-a-time"}}}
    values = {v.key: (v.value, v.observed_at) for v in reported_values(status)}
    assert values == {"power": ("on", None), "mute": (None, None)}


def test_FR09_reported_cache_within_setting():
    source = FakeSource()
    state, clock = reader(source, cache_sec=2.0)
    first = state.read()
    clock[0] = 1.9
    assert state.read() is first and len(source.calls) == 1
    clock[0] = 2.0
    state.read()
    assert len(source.calls) == 2


def test_FR09_zero_cache_reads_every_time():
    source = FakeSource()
    state, _ = reader(source, cache_sec=0.0)
    state.read()
    state.read()
    assert len(source.calls) == 2


def test_FR09_transport_error_returns_none(caplog):
    source = FakeSource(error=SmartThingsNotSent("SmartThings component status: 요청 거부(429)"))
    state, _ = reader(source)
    assert state.read() is None
    assert state.read() is None
    assert len(source.calls) == 2  # 실패는 캐시하지 않는다
    assert DEVICE not in caplog.text


def test_FR09_router_returns_none_for_unbound_node():
    state, _ = reader(FakeSource())
    router = ReportedStateRouter({"display-1": state})
    assert router.read("light-1") is None
    assert router.read("display-1").node_id == "display-1"


def test_FR11_reported_has_no_device_id():
    state, _ = reader(FakeSource())
    assert DEVICE not in repr(state.read())


# ── HTTP 구성요소 상태 조회 ───────────────────────────────────────


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _transport(tmp_path, body):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"access_token": "secret-token"}), encoding="utf-8")
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return _Response(body)
    return HttpSmartThingsTransport(token_file=token, request_timeout_sec=4.0, opener=opener), requests


def test_FR09_http_component_status_path(tmp_path):
    transport, requests = _transport(tmp_path, json.dumps(STATUS).encode("utf-8"))
    assert transport.get_component_status(DEVICE) == STATUS
    assert requests[0].get_method() == "GET"
    assert requests[0].full_url.endswith(f"/devices/{DEVICE}/components/main/status")


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_FR09_http_component_status_invalid_body(tmp_path, body):
    transport, _ = _transport(tmp_path, body)
    with pytest.raises(SmartThingsTransportError) as raised:
        transport.get_component_status(DEVICE)
    assert DEVICE not in str(raised.value)
