"""FR-06·FR-07·NFR-08 SmartThings HTTP transport (specs/iot-smartthings-companion/spec.md 결정 14·15)."""

from __future__ import annotations

import io
import json
import logging
import socket
import traceback
import urllib.error

import pytest

from nearpilot.iot.smartthings.http import HttpSmartThingsTransport
from nearpilot.iot.smartthings.transport import (
    SmartThingsMaybeSent, SmartThingsNotSent, SmartThingsTransportError,
)

TOKEN = "secret-token-abc"
DEVICE = "dev-0f1e2d3c-4b5a"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    def __init__(self, *, body: bytes = b"{}", error: BaseException | None = None):
        self.body = body
        self.error = error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


def token_file(tmp_path, content=None):
    path = tmp_path / ".env.smartthings-token"
    path.write_text(json.dumps({"access_token": TOKEN}) if content is None else content, encoding="utf-8")
    return path


def http_error(code: int) -> urllib.error.HTTPError:
    url = f"https://api.smartthings.com/v1/devices/{DEVICE}/commands"
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(b'{"error":"x"}'))


def make(tmp_path, opener) -> HttpSmartThingsTransport:
    return HttpSmartThingsTransport(token_file=token_file(tmp_path), request_timeout_sec=4.0, opener=opener)


def assert_no_secret(error: BaseException) -> None:
    text = "".join(traceback.format_exception(error))
    assert TOKEN not in text
    assert DEVICE not in text


def test_FR06_http_post_command_request_shape(tmp_path):
    opener = FakeOpener(body=b'{"results":[{"status":"ACCEPTED"}]}')
    transport = make(tmp_path, opener)
    transport.post_command(DEVICE, "switch", "on", [])
    request, timeout = opener.requests[0]
    assert timeout == 4.0
    assert request.get_method() == "POST"
    assert request.full_url == f"https://api.smartthings.com/v1/devices/{DEVICE}/commands"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert json.loads(request.data.decode("utf-8")) == {
        "commands": [{"component": "main", "capability": "switch", "command": "on", "arguments": []}]}


def test_FR07_http_utf8_round_trip(tmp_path):
    opener = FakeOpener()
    transport = make(tmp_path, opener)
    transport.post_command(DEVICE, "custom.picturemode", "setPictureMode", ["영화"])
    request, _ = opener.requests[0]
    assert "영화".encode("utf-8") in request.data
    assert request.get_header("Content-type") == "application/json; charset=utf-8"

    opener.body = json.dumps({"pictureMode": {"value": "영화"},
                              "supportedPictureModesMap": {"value": [{"id": "modeMovie", "name": "영화"}]}},
                             ensure_ascii=False).encode("utf-8")
    assert transport.get_attribute(DEVICE, "custom.picturemode", "pictureMode") == "영화"
    assert transport.get_attribute(DEVICE, "custom.picturemode", "supportedPictureModesMap") == [
        {"id": "modeMovie", "name": "영화"}]
    request, _ = opener.requests[-1]
    assert request.get_method() == "GET"
    assert request.full_url.endswith(f"/devices/{DEVICE}/components/main/capabilities/custom.picturemode/status")


def test_FR07_http_missing_attribute_is_none(tmp_path):
    transport = make(tmp_path, FakeOpener(body=b'{"switch":{"value":"on"}}'))
    assert transport.get_attribute(DEVICE, "switch", "other") is None


def test_FR07_http_invalid_status_body_raises(tmp_path):
    transport = make(tmp_path, FakeOpener(body=b"not json"))
    with pytest.raises(SmartThingsTransportError):
        transport.get_attribute(DEVICE, "switch", "switch")


def test_FR06_http_connect_error_not_sent(tmp_path):
    transport = make(tmp_path, FakeOpener(error=urllib.error.URLError(ConnectionRefusedError(10061, "refused"))))
    with pytest.raises(SmartThingsNotSent) as caught:
        transport.post_command(DEVICE, "switch", "on", [])
    assert_no_secret(caught.value)


def test_FR06_http_unauthorized_not_sent(tmp_path):
    transport = make(tmp_path, FakeOpener(error=http_error(401)))
    with pytest.raises(SmartThingsNotSent) as caught:
        transport.post_command(DEVICE, "switch", "on", [])
    assert "401" in str(caught.value)
    assert_no_secret(caught.value)


@pytest.mark.parametrize("code", [400, 403, 404, 422, 429])
def test_FR06_http_client_error_not_sent(tmp_path, code):
    transport = make(tmp_path, FakeOpener(error=http_error(code)))
    with pytest.raises(SmartThingsNotSent) as caught:
        transport.post_command(DEVICE, "switch", "on", [])
    assert_no_secret(caught.value)


@pytest.mark.parametrize("error", [
    http_error(500), http_error(503), socket.timeout("timed out"), TimeoutError("timed out"),
    urllib.error.URLError(socket.timeout("timed out")),
])
def test_FR07_http_timeout_or_server_error_maybe_sent(tmp_path, error):
    transport = make(tmp_path, FakeOpener(error=error))
    with pytest.raises(SmartThingsMaybeSent) as caught:
        transport.post_command(DEVICE, "switch", "on", [])
    assert_no_secret(caught.value)


@pytest.mark.parametrize("content", [None, "", "{not json", '{"refresh_token": "r"}', '{"access_token": ""}',
                                     '["secret-token-abc"]'])
def test_NFR08_missing_token_not_sent_without_secret(tmp_path, caplog, content):
    path = tmp_path / "missing.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    opener = FakeOpener()
    transport = HttpSmartThingsTransport(token_file=path, request_timeout_sec=4.0, opener=opener)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SmartThingsNotSent) as caught:
            transport.post_command(DEVICE, "switch", "on", [])
    assert opener.requests == []
    assert_no_secret(caught.value)
    assert str(path) not in str(caught.value)
    assert TOKEN not in caplog.text and DEVICE not in caplog.text


def test_NFR08_token_file_reread_each_request(tmp_path):
    path = token_file(tmp_path)
    opener = FakeOpener()
    transport = HttpSmartThingsTransport(token_file=path, request_timeout_sec=4.0, opener=opener)
    transport.post_command(DEVICE, "switch", "on", [])
    path.write_text(json.dumps({"access_token": "rotated"}), encoding="utf-8")
    transport.post_command(DEVICE, "switch", "off", [])
    assert [r.get_header("Authorization") for r, _ in opener.requests] == [f"Bearer {TOKEN}", "Bearer rotated"]


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_FR06_http_request_timeout_validated(tmp_path, value):
    with pytest.raises(ValueError):
        HttpSmartThingsTransport(token_file=token_file(tmp_path), request_timeout_sec=value)
