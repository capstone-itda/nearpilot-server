"""FR-09 묶음 요청의 MCP 도구 계약: 입력 검사, 응답 키, 입력 스키마와 도구 설명 (specs/core-request-batch/spec.md).

판정은 fake 로 대신한다. API 는 `requests` 와 항목의 형식만 검사한다. 항목 수의 상한과 같은 기기 중복은 판정이 검사한다.
"""

from __future__ import annotations

import json

import pytest
from _mcp_client import NOW, Client, run

from nearpilot.shared.enums import ReasonCode, TargetKind, Verdict
from nearpilot.shared.models import BATCH_ACTION, Decision, DeviceOutcome, TargetSpec, UseItem, UseRequest

# 단일 요청과 거리 선택자가 아닌 판정 전 거부의 응답 키 (FR-11)
BASE_KEYS = {"verdict", "reason", "node_id", "use_id", "request_id", "message"}
BATCH_KEYS = BASE_KEYS | {"devices", "selected_count", "similar"}
DEVICE_KEYS = {"node_id", "verdict", "reason", "use_id"}

LIGHT_ON = {"target_spec": "near:light:1:all", "action": "on"}
SPEAKER_OFF = {"target_spec": "speaker-1", "action": "off"}
REQUESTS = [LIGHT_ON, SPEAKER_OFF]


@pytest.mark.parametrize("args", [
    pytest.param({"requests": REQUESTS, "target_spec": "near:light:1:2"}, id="requests_and_target_spec"),
    pytest.param({"requests": REQUESTS, "action": "on"}, id="requests_and_action"),
    pytest.param({"requests": REQUESTS, "arguments": {}}, id="requests_and_empty_arguments"),
    pytest.param({"requests": REQUESTS, "target_spec": "light-1", "action": "on"}, id="requests_and_single_form"),
    pytest.param({}, id="neither"),
    pytest.param({"target_spec": "light-1"}, id="target_spec_without_action"),
])
def test_FR09_batch_mixed_or_missing_form_invalid_input(args):
    c = Client()
    resp = c.call({**args, "request_id": "b1"})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert (resp["node_id"], resp["use_id"], resp["request_id"]) == (None, None, "b1")
    # 두 형식을 섞었거나 둘 다 없으면 묶음인지 거리 선택자인지 알 수 없다. 형식이 맞는 거리 선택자를 섞어도 기존 여섯 키만 담는다.
    assert set(resp) == BASE_KEYS
    assert c.core.requests == []


@pytest.mark.parametrize("requests", [
    pytest.param([], id="empty_list"),
    pytest.param("x", id="not_a_list"),
    pytest.param({**LIGHT_ON}, id="object_not_list"),
    pytest.param([1], id="item_not_object"),
    pytest.param([{"action": "on"}], id="item_without_target_spec"),
    pytest.param([{"target_spec": "light-1"}], id="item_without_action"),
    pytest.param([{"target_spec": 5, "action": "on"}], id="item_target_spec_not_string"),
    pytest.param([{"target_spec": "nearest:", "action": "on"}], id="item_target_spec_malformed"),
    pytest.param([{"target_spec": "batch:", "action": "on"}], id="item_target_spec_batch"),
    pytest.param([{"target_spec": "light-1", "action": "Turn-On"}], id="item_action_name_rule"),
    pytest.param([{"target_spec": "light-1", "action": "on", "arguments": ["a"]}], id="item_arguments_array"),
    pytest.param([{"target_spec": "light-1", "action": "on", "extra": 1}], id="item_unknown_key"),
    pytest.param([LIGHT_ON, {"target_spec": "light-1"}], id="second_item_malformed"),
])
def test_FR09_batch_malformed_items_invalid_input(requests):
    c = Client()
    resp = c.call({"requests": requests, "request_id": "b1"})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert c.core.requests == []


@pytest.mark.parametrize(("args", "error", "reason"), [
    pytest.param({"requests": REQUESTS, "request_id": ""}, None, "INVALID_INPUT", id="empty_request_id"),
    pytest.param({"requests": [{"target_spec": "light-1"}], "request_id": "b1"}, None, "INVALID_INPUT",
                 id="malformed_item"),
    pytest.param({"requests": REQUESTS, "request_id": "b1"}, RuntimeError("secret internal detail"),
                 "INTERNAL_ERROR", id="tool_error"),
])
def test_FR09_batch_api_rejection_keeps_batch_keys(args, error, reason):
    # 묶음 형식(단일 요청 인자가 하나도 없다)은 판정 전에 거부해도 고르기 전에 끝난 판정과 같은 키를 담는다 (spec 5절)
    c = Client()
    c.core.error = error
    resp = c.call(args)
    assert (resp["verdict"], resp["reason"]) == ("DENY", reason)
    assert set(resp) == BATCH_KEYS
    assert (resp["devices"], resp["selected_count"], resp["similar"]) == ([], 0, False)
    assert "secret" not in json.dumps(resp, ensure_ascii=False)  # 예외 내용은 응답에 넣지 않는다
    assert len(c.core.requests) == (1 if error else 0)  # 도구 처리 중 예외만 판정까지 갔다


def test_FR09_batch_tool_schema_and_description():
    c = Client()
    tools = run(c.post("tools/list")).json()["result"]["tools"]
    tool = next(t for t in tools if t["name"] == "request_use")
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"request_id", "target_spec", "action", "arguments", "requests"}
    # 단일 요청 인자와 묶음 인자는 모두 선택이다. 둘 중 하나만 왔는지는 서버가 검사한다
    assert schema["required"] == ["request_id"]
    properties = schema["properties"]
    assert (properties["target_spec"]["type"], properties["action"]["type"]) == ("string", "string")
    assert properties["arguments"]["type"] == "object"
    requests = properties["requests"]
    assert (requests["type"], requests["minItems"]) == ("array", 1)
    item = requests["items"]
    assert item["type"] == "object"
    assert item["required"] == ["target_spec", "action"]
    assert {key: value["type"] for key, value in item["properties"].items()} == {
        "target_spec": "string", "action": "string", "arguments": "object"}
    assert item["additionalProperties"] is False
    for text in ("requests", "near:light:1:all", "묶음 요청이 NEED_APPROVAL 이면 devices 의 node_id 로 request_access", "'조명 다 켜 줘' 는 near:light:1:all 과 on 이다",
                 "'조명 다 켜고 스피커 꺼 줘' 는 "
                 "requests=[{target_spec: near:light:1:all, action: on}, {target_spec: near:speaker:1:all, action: off}]",
                 "묶음 요청은 하나라도 걸리면 아무것도 실행하지 않는다", "item(1부터 센 항목 순서)",
                 "같은 기기를 두 항목에 넣지 않는다"):
        assert text in tool["description"], text

    # 기존 단일 호출은 스키마가 바뀌어도 같은 UseRequest 를 코어에 넘긴다
    c.call({"target_spec": "nearest:locker", "action": "open", "request_id": "r1"})
    c.call({"target_spec": "near:light:1:2", "action": "on", "request_id": "r2", "arguments": {"brightness": 30}})
    assert c.core.requests == [
        UseRequest("user-a", "r1", TargetSpec(TargetKind.NEAREST, "locker"), "open", None, NOW),
        UseRequest("user-a", "r2", TargetSpec(TargetKind.NEAR, "light", start=1, count=2), "on", None, NOW,
                   arguments={"brightness": 30}),
    ]
    assert [req.items for req in c.core.requests] == [(), ()]


def test_FR09_request_use_keeps_json_like_strings():
    # SDK 는 주석이 str 이 아닌 인자의 문자열을 JSON 으로 먼저 읽는다. 단일 요청의 target_spec 과 action 은
    # 문자열 그대로 코어에 가야 한다: "null" 은 기기 ID 가 null 인 명시 지정이고 "[1]" 은 기기 ID 가 [1] 인 명시 지정이다.
    c = Client()
    tools = run(c.post("tools/list")).json()["result"]["tools"]
    schema = next(t for t in tools if t["name"] == "request_use")["inputSchema"]
    assert schema["required"] == ["request_id"]  # 문자열 주석이어도 target_spec 과 action 은 선택 인자다
    sent = [("null", "on"), ("[1]", "on"), ("light-1", "null")]
    for index, (target_spec, action) in enumerate(sent):
        resp = c.call({"target_spec": target_spec, "action": action, "request_id": f"r{index}"})
        assert (resp["verdict"], resp["reason"]) == ("OK", "OK")  # INVALID_INPUT 으로 거부하지 않는다
    assert [(req.target, req.action, req.items) for req in c.core.requests] == [
        (TargetSpec(TargetKind.EXPLICIT, None, "null"), "on", ()),
        (TargetSpec(TargetKind.EXPLICIT, None, "[1]"), "on", ()),
        (TargetSpec(TargetKind.EXPLICIT, None, "light-1"), "null", ())]


def test_FR09_batch_response_devices_have_item():
    c = Client()
    c.core.decision = Decision(
        "internal-p", "b1", Verdict.DENY, ReasonCode.EXEC_FAILED,
        devices=(DeviceOutcome("light-2", Verdict.OK, ReasonCode.OK, item=1),
                 DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK, item=1),
                 DeviceOutcome("locker-1", Verdict.OK, ReasonCode.OK, use_id="use-1", item=2),
                 DeviceOutcome("speaker-1", Verdict.DENY, ReasonCode.EXEC_FAILED, item=3)),
        selected_count=4, similar=True)
    resp = c.call({"requests": [
        {"target_spec": "near:light:1:all", "action": "on", "arguments": {"brightness": 30}},
        {"target_spec": "locker-1", "action": "open"},
        {"target_spec": "speaker-1", "action": "off"}], "request_id": "b1"})
    assert set(resp) == BATCH_KEYS
    assert [set(device) for device in resp["devices"]] == [DEVICE_KEYS | {"item"}] * 4
    # 순서는 판정이 정한 그대로다: 항목 순서이고, 거리 선택자 항목 안에서는 거리 순서다
    assert resp["devices"] == [
        {"node_id": "light-2", "verdict": "OK", "reason": "OK", "use_id": None, "item": 1},
        {"node_id": "light-1", "verdict": "OK", "reason": "OK", "use_id": None, "item": 1},
        {"node_id": "locker-1", "verdict": "OK", "reason": "OK", "use_id": "use-1", "item": 2},
        {"node_id": "speaker-1", "verdict": "DENY", "reason": "EXEC_FAILED", "use_id": None, "item": 3}]
    assert (resp["selected_count"], resp["similar"]) == (4, True)
    # 묶음의 상위 node_id 와 use_id 는 비어 있다. 점유 핸들은 기기별로만 낸다
    assert (resp["verdict"], resp["reason"], resp["node_id"], resp["use_id"], resp["request_id"]) == \
        ("DENY", "EXEC_FAILED", None, None, "b1")
    text = json.dumps(resp)
    assert "internal-p" not in text and "user-a" not in text

    # 코어는 묶음 UseRequest 하나를 받는다: 대상 batch:, 동작 batch, 항목은 보낸 순서와 값 그대로다
    (req,) = c.core.requests
    assert req == UseRequest(
        "user-a", "b1", TargetSpec(TargetKind.BATCH, None), BATCH_ACTION, None, NOW,
        items=(UseItem(TargetSpec(TargetKind.NEAR, "light", start=1, count=None), "on", {"brightness": 30}),
               UseItem(TargetSpec(TargetKind.EXPLICIT, None, "locker-1"), "open"),
               UseItem(TargetSpec(TargetKind.EXPLICIT, None, "speaker-1"), "off")))
    assert (req.target.serialize(), req.arguments) == ("batch:", None)

    # 거리 선택자 응답은 그대로다: 기기에 item 키가 없다
    c.core.decision = Decision(
        "internal-p", "r2", Verdict.OK, ReasonCode.OK,
        devices=(DeviceOutcome("light-2", Verdict.OK, ReasonCode.OK),
                 DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK)),
        selected_count=2, similar=False)
    resp = c.call({"target_spec": "near:light:1:2", "action": "on", "request_id": "r2"})
    assert [set(device) for device in resp["devices"]] == [DEVICE_KEYS] * 2


def test_FR09_batch_api_leaves_item_count_and_duplicates_to_decision():
    # 항목 수의 상한(batch_max_items)과 같은 기기 중복은 판정이 검사한다. API 는 형식이 맞으면 그대로 넘긴다
    c = Client()
    c.call({"requests": [{"target_spec": "light-1", "action": "on"}] * 100, "request_id": "b1"})
    (req,) = c.core.requests
    assert len(req.items) == 100
