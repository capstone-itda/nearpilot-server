"""FR-09 request_access 도구. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from _mcp_client import TOKEN_A, TOKEN_B, Client, run
from nearpilot.shared.enums import Action, ReasonCode, Verdict

ARGS = {"node_id": "locker-1", "action": "open", "request_id": "r1"}


def access(c, args=ARGS, token=TOKEN_A):
    return c.call(args, token=token, tool="request_access")


def test_FR09_도구_목록에_request_access가_있다():
    names = [t["name"] for t in run(Client().post("tools/list")).json()["result"]["tools"]]
    assert {"find_available_devices", "get_device_state", "request_access", "request_use",
            "release_use", "get_use_status"} == set(names)


def test_FR09_request_access는_토큰_계정과_인자로_서비스를_부른다():
    c = Client()
    access(c)
    access(c, ARGS, token=TOKEN_B)
    assert c.core.accesses == [("user-a", "locker-1", Action.OPEN, "r1"),
                               ("user-b", "locker-1", Action.OPEN, "r1")]


def test_FR09_승인_대기_판정을_반환한다():
    resp = access(Client())
    assert resp == {"verdict": "NEED_APPROVAL", "reason": "APPROVAL_PENDING", "node_id": "locker-1",
                    "use_id": None, "request_id": "r1", "message": "소유자의 승인을 기다립니다."}


def test_FR09_승인_요청_응답에_use_id가_없다():
    c = Client()
    c.core.access_decision = replace(
        c.core.access_decision, verdict=Verdict.OK, reason=ReasonCode.OK, use_id="use-1")
    resp = access(c)
    assert (resp["verdict"], resp["use_id"]) == ("OK", None)
    assert "use-1" not in json.dumps(resp)


@pytest.mark.parametrize("args", [
    {**ARGS, "node_id": ""},
    {**ARGS, "request_id": ""},
])
def test_FR09_인자가_비면_INVALID_INPUT이고_서비스를_부르지_않는다(args):
    c = Client()
    resp = access(c, args)
    assert (resp["verdict"], resp["reason"], resp["use_id"]) == ("DENY", "INVALID_INPUT", None)
    assert c.core.accesses == []


@pytest.mark.parametrize("action", ["", "lock", "CLOSE", "drop table"])
def test_FR09_action이_잘못되면_INVALID_INPUT(action):
    c = Client()
    resp = access(c, {**ARGS, "action": action})
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INVALID_INPUT")
    assert resp["request_id"] == "r1" and c.core.accesses == []


def test_FR09_서비스_예외는_INTERNAL_ERROR():
    c = Client()
    c.core.error = RuntimeError("secret internal detail")
    resp = access(c)
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INTERNAL_ERROR")
    assert "secret" not in json.dumps(resp)


def test_FR11_허용_목록_밖의_값은_응답에_없다():
    c = Client()
    resp = access(c)
    assert set(resp) == {"verdict", "reason", "node_id", "use_id", "request_id", "message"}
    text = json.dumps(resp)
    assert "internal-2" not in text and "user-a" not in text


def test_FR08_인자의_사용자_ID는_계정으로_쓰지_않는다():
    c = Client()
    access(c, {**ARGS, "user_id": "user-b"})
    assert c.core.accesses[0][0] == "user-a"


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_FR08_토큰이_없으면_서비스를_부르지_않는다(token):
    c = Client()
    r = run(c.post("tools/call", {"name": "request_access", "arguments": ARGS}, token=token))
    assert r.status_code == 401 and c.core.accesses == []
