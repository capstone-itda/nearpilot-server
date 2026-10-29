"""FR-09 request_use 의 스피커 음량 인자. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import pytest
from _mcp_client import NOW, Client, run

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.mcp.server import build_app
from nearpilot.shared.models import SpeakerSetting

BASE = {"target_spec": "speaker-1", "action": "on", "request_id": "r1"}


class CappedClient(Client):
    """서버 설정의 음량 상한 20 으로 앱을 만든다."""

    def new_app(self):
        return build_app(self.core, Authenticator(self.accounts), lambda: NOW, speaker_volume_max=20)


def test_FR09_request_use_schema_tells_volume_range():
    tools = run(CappedClient().post("tools/list")).json()["result"]["tools"]
    tool = next(t for t in tools if t["name"] == "request_use")
    volume = tool["inputSchema"]["properties"]["volume"]
    assert volume["type"] == "integer"
    assert (volume["minimum"], volume["maximum"]) == (0, 20)
    assert "controls.volume_max" in volume["description"]
    assert set(tool["inputSchema"]["required"]) == {"target_spec", "action", "request_id"}  # 인자는 선택이다
    assert "controls.volume_max" in tool["description"] and "off" in tool["description"]


def test_FR09_speaker_input_reaches_core():
    c = Client()
    c.call({**BASE, "volume": 12})
    assert c.core.requests[-1].speaker == SpeakerSetting(12)
    assert c.core.requests[-1].light is None
    c.call({**BASE, "request_id": "r2", "volume": 0})
    assert c.core.requests[-1].speaker == SpeakerSetting(0)  # 0 은 값이다. 생략과 다르다
    c.call({**BASE, "request_id": "r3"})
    assert c.core.requests[-1].speaker is None


@pytest.mark.parametrize("volume", ["12", 12.0, 12.5, True, False, [12], {"v": 12}])
def test_FR09_speaker_input_rejects_wrong_types(volume):
    c = Client()
    resp = c.call({**BASE, "volume": volume})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert c.core.requests == []  # 판정까지 가지 않는다


@pytest.mark.parametrize("volume", [-1, 31])
def test_FR09_speaker_input_rejects_out_of_wire_range(volume):
    c = Client()
    resp = c.call({**BASE, "volume": volume})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT") and c.core.requests == []


@pytest.mark.parametrize("target_spec, action, extra", [
    ("speaker-1", "off", {"volume": 12}), ("nearest:speaker", "off", {"volume": 12}),
    ("light-1", "on", {"volume": 12}), ("locker-1", "open", {"volume": 12}),
    ("speaker-1", "on", {"brightness": 30}), ("speaker-1", "on", {"color": "#0000FF"}),
])
def test_FR09_speaker_input_only_for_speaker_on(target_spec, action, extra):
    c = Client()
    resp = c.call({"target_spec": target_spec, "action": action, "request_id": "r1", **extra})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT") and c.core.requests == []
