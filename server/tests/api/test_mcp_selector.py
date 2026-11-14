"""FR-09 거리 선택자의 MCP 도구 계약: 형식 오류, 도구 설명, 응답 키 (specs/core-distance-selector/spec.md).

판정은 fake 로 대신한다. API 는 형식만 검사하고 응답에 기기별 결과를 키로 바꿔 담는다.
"""

from __future__ import annotations

import json

import pytest
from _mcp_client import Client, run

from nearpilot.shared.enums import ReasonCode, TargetKind, Verdict
from nearpilot.shared.models import Decision, DeviceOutcome, TargetSpec

# 거리 선택자가 아닌 요청의 응답 키 (FR-11)
BASE_KEYS = {"verdict", "reason", "node_id", "use_id", "request_id", "message"}
SELECTOR_KEYS = BASE_KEYS | {"devices", "selected_count", "similar"}
DEVICE_KEYS = {"node_id", "verdict", "reason", "use_id"}


def selector_args(target_spec="near:light:1:2", action="off", request_id="r1"):
    return {"target_spec": target_spec, "action": action, "request_id": request_id}


@pytest.mark.parametrize("target_spec", [
    "near:light:0:1", "near:light:1:0", "near:light:x:2", "near:light:1", "far:light:1:2:3"])
def test_FR09_selector_malformed_invalid_input(target_spec):
    c = Client()
    resp = c.call(selector_args(target_spec))
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert (resp["node_id"], resp["use_id"], resp["request_id"]) == (None, None, "r1")
    # 판정까지 가지 못한 거부는 거리 선택자인지 알 수 없다. 기존 여섯 키만 담는다.
    assert set(resp) == BASE_KEYS
    assert c.core.requests == []


@pytest.mark.parametrize("args", [
    selector_args(action=""), selector_args(request_id=""),
    {**selector_args(), "arguments": ["not", "object"]}])
def test_FR09_selector_api_rejection_keeps_selector_keys(args):
    c = Client()
    resp = c.call(args)
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    # 형식이 맞는 거리 선택자는 판정 전에 거부해도 고르기 전에 끝난 판정과 같은 키를 담는다 (spec 결정 12)
    assert set(resp) == SELECTOR_KEYS
    assert (resp["devices"], resp["selected_count"], resp["similar"]) == ([], 0, False)
    assert c.core.requests == []


def test_FR09_tool_description_mentions_selector():
    tools = run(Client().post("tools/list")).json()["result"]["tools"]
    tool = next(t for t in tools if t["name"] == "request_use")
    for text in ("near:{kind}:{start}:{count}", "far:{kind}:{start}:{count}", "near:light:1:2",
                 "far:light:1:1", "near:light:2:all", "되묻는다",
                 "NEED_APPROVAL 이면 devices 의 node_id 로 request_access"):
        assert text in tool["description"], text
    # 거리 선택자는 입력 스키마를 바꾸지 않는다. 묶음 요청이 requests 를 더했다 (FR-09, #284)
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"target_spec", "action", "request_id", "arguments", "requests"}
    assert set(schema["required"]) == {"request_id"}


def test_FR09_tool_description_mentions_order_uncertain():
    tools = run(Client().post("tools/list")).json()["result"]["tools"]
    tool = next(t for t in tools if t["name"] == "request_use")
    # near 거리 선택자의 순서가 애매하면 아무것도 실행하지 않고 ASK 와 ORDER_UNCERTAIN 이 오며, 호스트는 사용자에게 되묻는다
    for text in ("near 거리 선택자는 경계의 기기들의 순서가 애매하면 아무것도 실행하지 않고",
                 "ASK 와 ORDER_UNCERTAIN 을 반환", "사용자에게 어느 기기인지 더 구체적으로 묻는다"):
        assert text in tool["description"], text


def test_FR09_selector_response_devices_keys():
    c = Client()
    c.core.decision = Decision(
        "internal-p", "r1", Verdict.OK, ReasonCode.OK,
        devices=(DeviceOutcome("light-2", Verdict.OK, ReasonCode.OK),
                 DeviceOutcome("light-1", Verdict.DENY, ReasonCode.EXEC_FAILED)),
        selected_count=2, similar=True)
    resp = c.call(selector_args("near:light:1:2"))
    assert set(resp) == SELECTOR_KEYS
    assert [set(device) for device in resp["devices"]] == [DEVICE_KEYS, DEVICE_KEYS]
    # 순서는 판정이 정한 거리 순서 그대로다 (기기 ID 순서가 아니다)
    assert resp["devices"] == [
        {"node_id": "light-2", "verdict": "OK", "reason": "OK", "use_id": None},
        {"node_id": "light-1", "verdict": "DENY", "reason": "EXEC_FAILED", "use_id": None}]
    assert (resp["selected_count"], resp["similar"]) == (2, True)
    # 거리 선택자의 상위 node_id 와 use_id 는 비어 있다
    assert (resp["verdict"], resp["reason"], resp["node_id"], resp["use_id"], resp["request_id"]) == \
        ("OK", "OK", None, None, "r1")
    text = json.dumps(resp)
    assert "internal-p" not in text and "user-a" not in text
    (req,) = c.core.requests
    assert req.target == TargetSpec(TargetKind.NEAR, "light", start=1, count=2)


def test_FR09_selector_response_device_use_id():
    c = Client()
    c.core.decision = Decision(
        "internal-p", "r1", Verdict.OK, ReasonCode.OK,
        devices=(DeviceOutcome("locker-2", Verdict.OK, ReasonCode.OK, use_id="use-2"),
                 DeviceOutcome("locker-1", Verdict.OK, ReasonCode.OK, use_id="use-1")),
        selected_count=2, similar=False)
    resp = c.call(selector_args("far:locker:1:all", "open"))
    assert [(d["node_id"], d["use_id"]) for d in resp["devices"]] == [("locker-2", "use-2"), ("locker-1", "use-1")]
    assert (resp["node_id"], resp["use_id"]) == (None, None)  # 점유 핸들은 기기별로만 낸다
    assert resp["similar"] is False
    (req,) = c.core.requests
    assert req.target == TargetSpec(TargetKind.FAR, "locker", start=1, count=None)


def test_FR09_selector_response_keys_without_devices():
    # 고르기 전에 끝난 판정도 거리 선택자의 응답이다: devices 가 비고 selected_count 0, similar false
    c = Client()
    c.core.decision = Decision(
        "internal-p", "r1", Verdict.DENY, ReasonCode.NO_CANDIDATE,
        devices=(), selected_count=0, similar=False)
    resp = c.call(selector_args("near:light:1:2"))
    assert set(resp) == SELECTOR_KEYS
    assert (resp["verdict"], resp["reason"]) == ("DENY", "NO_CANDIDATE")
    assert (resp["devices"], resp["selected_count"], resp["similar"]) == ([], 0, False)


def test_FR09_selector_response_blocked_keeps_selected_count():
    # 검사에서 걸리면 devices 는 처음 걸린 기기 하나이고 selected_count 는 실제로 고른 개수 그대로다
    c = Client()
    c.core.decision = Decision(
        "internal-p", "r1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED,
        devices=(DeviceOutcome("locker-1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),),
        selected_count=2, similar=True)
    resp = c.call(selector_args("near:locker:1:2", "open"))
    assert (resp["verdict"], resp["reason"], resp["node_id"]) == ("NEED_APPROVAL", "APPROVAL_REQUIRED", None)
    assert resp["devices"] == [
        {"node_id": "locker-1", "verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED", "use_id": None}]
    assert (resp["selected_count"], resp["similar"]) == (2, True)


@pytest.mark.parametrize("target_spec", ["nearest:locker", "locker-1", "any:locker"])
def test_FR09_existing_response_keys_unchanged(target_spec):
    c = Client()
    resp = c.call({"target_spec": target_spec, "action": "open", "request_id": "r1"})
    assert set(resp) == BASE_KEYS
    assert resp == {"verdict": "OK", "reason": "OK", "node_id": "locker-1", "use_id": "use-1",
                    "request_id": "r1", "message": "1번 사물함을 열었습니다."}


@pytest.mark.parametrize("target_spec", ["nearest:locker", "locker-1", "any:locker"])
def test_FR09_existing_rejection_keys_unchanged(target_spec):
    # 다른 대상 지정의 판정 전 거부는 거리 선택자 키를 담지 않는다 (spec 2절)
    c = Client()
    resp = c.call({"target_spec": target_spec, "action": "", "request_id": "r1"})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert set(resp) == BASE_KEYS
    c.core.error = RuntimeError("secret internal detail")
    resp = c.call({"target_spec": target_spec, "action": "open", "request_id": "r2"})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INTERNAL_ERROR")
    assert set(resp) == BASE_KEYS


def test_FR09_selector_internal_error_rejection_keeps_selector_keys():
    # 도구 처리 중 예외(서버가 요청을 받지 않을 때 포함)도 거리 선택자 키를 담는다. 예외 내용은 넣지 않는다
    c = Client()
    c.core.error = RuntimeError("secret internal detail")
    resp = c.call(selector_args())
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INTERNAL_ERROR")
    assert set(resp) == SELECTOR_KEYS
    assert (resp["devices"], resp["selected_count"], resp["similar"]) == ([], 0, False)
    assert "secret" not in json.dumps(resp, ensure_ascii=False)
