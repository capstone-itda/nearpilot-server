"""FR-09 find_available_devices·get_device_state 도구. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import json

from _mcp_client import TOKEN_A, TOKEN_B, Client, run

UNAVAILABLE = {"node_id": None, "capability": None, "occupancy": None, "state": None, "online": None,
               "trusted": None, "access_mode": None, "controls": None,
               "reason": "TARGET_UNAVAILABLE"}


def devices(c, token=TOKEN_A):
    return c.call({}, token=token, tool="find_available_devices")


def state(c, args, token=TOKEN_A):
    return c.call(args, token=token, tool="get_device_state")


def test_FR09_도구_목록에_조회_도구가_있다():
    names = [t["name"] for t in run(Client().post("tools/list")).json()["result"]["tools"]]
    assert {"find_available_devices", "get_device_state"} <= set(names)


def test_FR09_find_available_devices는_토큰_계정으로_서비스를_부른다():
    c = Client()
    assert devices(c) == {"devices": c.core.devices, "reason": None}
    devices(c, TOKEN_B)
    assert c.core.device_calls == ["user-a", "user-b"]


def test_FR09_후보가_없으면_빈_목록():
    c = Client()
    c.core.devices = []
    assert devices(c) == {"devices": [], "reason": None}


def test_FR11_목록_항목에서_허용_목록_밖의_키를_버린다():
    c = Client()
    c.core.devices = [{**c.core.devices[0], "user_id": "user-b", "p": 0.97, "place_id": "p"}]
    resp = devices(c)
    assert set(resp["devices"][0]) == {"node_id", "capability", "occupancy", "access", "controls"}
    assert "user-b" not in json.dumps(resp) and "0.97" not in json.dumps(resp)


def test_FR09_목록_서비스_예외는_INTERNAL_ERROR():
    c = Client()
    c.core.error = RuntimeError("secret internal detail")
    resp = devices(c)
    assert resp == {"devices": [], "reason": "INTERNAL_ERROR"}
    assert "secret" not in json.dumps(resp)


def test_FR09_get_device_state는_토큰_계정과_node_id로_서비스를_부른다():
    c = Client()
    resp = state(c, {"node_id": "locker-1"})
    assert c.core.state_calls == [("user-a", "locker-1")]
    assert resp == {**c.core.device_state, "reason": None}


def test_FR14_조회할_수_없는_기기는_모두_같은_응답():
    c = Client()
    c.core.device_state = {"reason_code": "TARGET_UNAVAILABLE"}
    assert state(c, {"node_id": "private-1"}) == state(c, {"node_id": "no-such"}) == UNAVAILABLE


def test_FR11_상태에서_허용_목록_밖의_키를_버린다():
    c = Client()
    c.core.device_state = {**c.core.device_state, "user_id": "user-b", "owner": "user-b",
                           "place_id": "p"}
    resp = state(c, {"node_id": "locker-1"})
    assert set(resp) == {"node_id", "capability", "occupancy", "state", "online", "trusted",
                         "access_mode", "controls", "reason"}
    assert "user-b" not in json.dumps(resp)


def test_FR09_node_id가_비면_INVALID_INPUT():
    c = Client()
    resp = state(c, {"node_id": ""})
    assert resp == {**UNAVAILABLE, "reason": "INVALID_INPUT"} and c.core.state_calls == []


def test_FR09_상태_서비스_예외는_INTERNAL_ERROR():
    c = Client()
    c.core.error = RuntimeError("secret internal detail")
    resp = state(c, {"node_id": "locker-1"})
    assert resp == {**UNAVAILABLE, "reason": "INTERNAL_ERROR"}
    assert "secret" not in json.dumps(resp)


def test_FR08_토큰이_없으면_조회_도구를_처리하지_않는다():
    c = Client()
    r = run(c.post("tools/call", {"name": "find_available_devices", "arguments": {}}, token=None))
    assert r.status_code == 401
    assert c.core.device_calls == [] and c.core.state_calls == []
