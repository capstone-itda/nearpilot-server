"""FR-09 request_use 의 조명 설정 인자. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import pytest
from _mcp_client import NOW, Client, run

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.mcp.server import build_app
from nearpilot.shared.enums import Action
from nearpilot.shared.models import LightSetting

BASE = {"target_spec": "nearest:light", "action": "on", "request_id": "r1"}


class CappedClient(Client):
    """서버 설정의 밝기 상한 64 로 앱을 만든다."""

    def new_app(self):
        return build_app(self.core, Authenticator(self.accounts), lambda: NOW, light_brightness_max=64)


def test_FR09_request_use_schema_tells_light_format_and_range():
    tools = run(CappedClient().post("tools/list")).json()["result"]["tools"]
    schema = next(t for t in tools if t["name"] == "request_use")["inputSchema"]
    assert schema["properties"]["brightness"]["type"] == "integer"
    assert (schema["properties"]["brightness"]["minimum"], schema["properties"]["brightness"]["maximum"]) == (0, 64)
    assert schema["properties"]["color"]["pattern"] == "^#[0-9A-Fa-f]{6}$"
    assert set(schema["required"]) == {"target_spec", "action", "request_id"}  # 인자는 선택이다
    description = next(t for t in tools if t["name"] == "request_use")["description"]
    assert "controls.brightness_max" in description and "off" in description


def test_FR09_light_input_reaches_core_normalized():
    c = Client()
    c.call({**BASE, "brightness": 30, "color": "#0000ff"})
    assert c.core.requests[-1].light == LightSetting(30, "#0000FF")
    c.call({**BASE, "request_id": "r2", "brightness": 0})
    assert c.core.requests[-1].light == LightSetting(0, None)


def test_FR09_light_input_omitted_keeps_old_request_shape():
    c = Client()
    c.call(BASE)
    c.call({"target_spec": "locker-1", "action": "open", "request_id": "r2"})
    assert [r.light for r in c.core.requests] == [None, None]


@pytest.mark.parametrize("brightness", ["30", 30.0, 30.5, True, False, [30], {"v": 30}])
def test_FR09_light_input_rejects_wrong_types(brightness):
    c = Client()
    resp = c.call({**BASE, "brightness": brightness})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert c.core.requests == []  # 판정까지 가지 않는다


@pytest.mark.parametrize("brightness", [-1, 256, 10_000])
def test_FR09_light_input_rejects_out_of_wire_range(brightness):
    c = Client()
    resp = c.call({**BASE, "brightness": brightness})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT") and c.core.requests == []


@pytest.mark.parametrize("color", ["0000FF", "#00F", "#0000FG", "#0000FF0", "blue", "", 255, True])
def test_FR09_light_input_rejects_bad_color(color):
    c = Client()
    resp = c.call({**BASE, "color": color})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT") and c.core.requests == []


@pytest.mark.parametrize("target_spec, action", [
    ("nearest:light", "off"), ("light-1", "off"), ("locker-1", "open"), ("nearest:locker", "close"),
    ("nearest:speaker", "on"),
])
def test_FR09_light_input_only_for_light_on(target_spec, action):
    c = Client()
    resp = c.call({"target_spec": target_spec, "action": action, "request_id": "r1", "brightness": 30})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT") and c.core.requests == []


def test_FR09_explicit_light_target_takes_light_input():
    c = Client()
    c.call({"target_spec": "light-1", "action": "on", "request_id": "r1", "color": "#FF0000"})
    req = c.core.requests[-1]
    assert (req.target.node_id, req.action, req.light) == ("light-1", Action.ON, LightSetting(None, "#FF0000"))
