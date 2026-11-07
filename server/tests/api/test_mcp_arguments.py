"""FR-09 request_use 의 동작 인자 `arguments` (specs/core-device-actions/spec.md).

도구 입력 스키마는 기기 종류와 관계없이 같다. API 는 `arguments` 가 객체인지만 검사하고 안쪽은 판정이 기기 선언으로
검사한다. 서비스와 계정 조회는 fake 로 대신한다.
"""

from __future__ import annotations

import pytest
from _mcp_client import Client, run

BASE = {"target_spec": "nearest:light", "action": "on", "request_id": "r1"}


def test_FR09_request_use_schema_is_kind_neutral():
    tools = run(Client().post("tools/list")).json()["result"]["tools"]
    tool = next(t for t in tools if t["name"] == "request_use")
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"target_spec", "action", "request_id", "arguments"}
    assert schema["properties"]["arguments"]["type"] == "object"
    assert set(schema["required"]) == {"target_spec", "action", "request_id"}  # 인자는 선택이다
    assert "input_schema" in tool["description"]
    for word in ("brightness", "volume", "controls"):  # 종류별 인자를 도구 계약에 두지 않는다
        assert word not in repr(tool)


def test_FR09_arguments_reach_core_unchanged():
    c = Client()
    c.call({**BASE, "arguments": {"brightness": 30, "color": "#0000ff"}})
    assert c.core.requests[-1].arguments == {"brightness": 30, "color": "#0000ff"}  # 정규화는 판정이 한다
    c.call({**BASE, "request_id": "r2", "arguments": {"brightness": 0}})
    assert c.core.requests[-1].arguments == {"brightness": 0}  # 0 은 값이다. 생략과 다르다
    c.call({**BASE, "request_id": "r3", "arguments": {}})
    assert c.core.requests[-1].arguments == {}


def test_FR09_arguments_omitted_keeps_old_request_shape():
    c = Client()
    c.call(BASE)
    c.call({"target_spec": "locker-1", "action": "open", "request_id": "r2"})
    assert [r.arguments for r in c.core.requests] == [None, None]


@pytest.mark.parametrize("arguments", ["brightness=30", 30, 30.5, True, [30], ["brightness"]])
def test_FR09_arguments_not_object_rejected(arguments):
    c = Client()
    resp = c.call({**BASE, "arguments": arguments})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert c.core.requests == []  # 판정까지 가지 않는다


@pytest.mark.parametrize("inner", [{"brightness": "30"}, {"brightness": 30.5}, {"volume": -1}, {"unknown": True}])
def test_FR09_api_does_not_check_argument_values(inner):
    # 안쪽 값은 판정이 대상 기기의 선언으로 검사한다. API 는 바꾸지도 거르지도 않는다 (FR-09).
    c = Client()
    c.call({**BASE, "arguments": inner})
    assert c.core.requests[-1].arguments == inner


@pytest.mark.parametrize("action", ["OPEN", "", "open!", "1on", "a" * 33])
def test_FR09_action_name_rule_checked(action):
    c = Client()
    resp = c.call({**BASE, "action": action})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT") and c.core.requests == []


def test_FR09_declared_action_names_reach_core():
    # 동작은 기기가 선언한 이름이다. API 는 열거형으로 제한하지 않는다 (FR-09, NFR-07).
    c = Client()
    c.call({"target_spec": "fan-room", "action": "spin_up", "request_id": "r1", "arguments": {"speed": 2}})
    req = c.core.requests[-1]
    assert (req.target.node_id, req.target.device_kind, req.action, req.arguments) == (
        "fan-room", None, "spin_up", {"speed": 2})
    c.call({"target_spec": "nearest:fan", "action": "spin_up", "request_id": "r2"})
    assert c.core.requests[-1].target.device_kind == "fan"


def test_FR09_access_request_takes_declared_action_names():
    c = Client()
    c.call({"node_id": "fan-room", "action": "spin_up", "request_id": "r1"}, tool="request_access")
    assert c.core.accesses[-1][1:3] == ("fan-room", "spin_up")
    resp = c.call({"node_id": "fan-room", "action": "Spin", "request_id": "r2"}, tool="request_access")
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
