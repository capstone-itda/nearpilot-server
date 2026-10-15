"""FR-09 release_use·get_use_status 도구. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import json

import pytest

from _mcp_client import NOW, TOKEN_A, TOKEN_B, Client, run
from nearpilot.shared.enums import ReasonCode, Verdict
from nearpilot.shared.models import Decision

NOT_FOUND = {"use_id": None, "state": None, "rental_due_at": None, "overtime_sec": None,
             "closed_at": None, "reason": "USE_NOT_FOUND"}


def release(c, args, token=TOKEN_A):
    return c.call(args, token=token, tool="release_use")


def status(c, args, token=TOKEN_A):
    return c.call(args, token=token, tool="get_use_status")


def test_FR09_도구_목록에_release_use와_get_use_status가_있다():
    names = [t["name"] for t in run(Client().post("tools/list")).json()["result"]["tools"]]
    assert {"release_use", "get_use_status"} <= set(names)


def test_FR09_release_use는_토큰_계정과_use_id로_서비스를_부른다():
    c = Client()
    resp = release(c, {"use_id": "use-1"})
    assert c.core.releases == [("user-a", "use-1")]
    assert resp == {"verdict": "OK", "reason": "OK", "node_id": "locker-1", "use_id": "use-1",
                    "request_id": None, "message": "반납을 완료했다."}


def test_FR09_get_use_status는_토큰_계정과_use_id로_서비스를_부른다():
    c = Client()
    resp = status(c, {"use_id": "use-1"})
    assert c.core.statuses == [("user-a", "use-1")]
    assert (resp["use_id"], resp["state"], resp["overtime_sec"], resp["reason"]) == \
        ("use-1", "ACTIVE", 0.0, None)


def test_FR09_get_use_status는_시각을_ISO_문자열로_반환한다():
    c = Client()
    c.core.status = {**c.core.status, "closed_at": NOW}
    resp = status(c, {"use_id": "use-1"})
    assert resp["rental_due_at"] == NOW.isoformat() and resp["closed_at"] == NOW.isoformat()


def test_FR21_없는_use_id는_USE_NOT_FOUND():
    c = Client()
    c.core.release_decision = Decision("", "", Verdict.DENY, ReasonCode.USE_NOT_FOUND)
    c.core.status = {"reason_code": "USE_NOT_FOUND"}
    r = release(c, {"use_id": "nope"})
    assert (r["verdict"], r["reason"], r["node_id"], r["use_id"]) == ("DENY", "USE_NOT_FOUND", None, None)
    assert status(c, {"use_id": "nope"}) == NOT_FOUND


def test_FR21_타인의_use_id는_없는_use_id와_같은_응답():
    """소유권 판단은 상태 관리가 한다. API 는 두 경우의 응답 모양을 바꾸지 않는다."""
    c = Client()
    c.core.release_decision = Decision("", "", Verdict.DENY, ReasonCode.USE_NOT_FOUND)
    c.core.status = {"reason_code": "USE_NOT_FOUND"}
    mine = release(c, {"use_id": "someone-elses"}, token=TOKEN_B)
    missing = release(c, {"use_id": "nope"}, token=TOKEN_B)
    assert mine == missing
    assert status(c, {"use_id": "someone-elses"}, token=TOKEN_B) == status(c, {"use_id": "nope"}, token=TOKEN_B) == NOT_FOUND
    assert c.core.releases[0][0] == "user-b"  # 요청한 계정은 토큰 계정이다


@pytest.mark.parametrize("tool,expected", [
    ("release_use", ("DENY", "INVALID_INPUT")),
    ("get_use_status", (None, "INVALID_INPUT")),
])
def test_FR09_use_id가_비면_INVALID_INPUT이고_서비스를_부르지_않는다(tool, expected):
    c = Client()
    resp = c.call({"use_id": ""}, tool=tool)
    assert (resp.get("verdict"), resp["reason"]) == expected
    assert c.core.releases == [] and c.core.statuses == []


@pytest.mark.parametrize("tool", ["release_use", "get_use_status"])
def test_FR09_서비스_예외는_INTERNAL_ERROR(tool):
    c = Client()
    c.core.error = RuntimeError("secret internal detail")
    resp = c.call({"use_id": "use-1"}, tool=tool)
    assert resp["reason"] == "INTERNAL_ERROR" and "secret" not in json.dumps(resp)


def test_FR11_허용_목록_밖의_값은_응답에_없다():
    c = Client()
    c.core.status = {**c.core.status, "user_id": "user-a", "owner": "user-z", "internal_request_id": "i-9"}
    resp = status(c, {"use_id": "use-1"})
    assert set(resp) == {"use_id", "state", "rental_due_at", "overtime_sec", "closed_at", "reason"}
    text = json.dumps(resp) + json.dumps(release(c, {"use_id": "use-1"}))
    assert "user-a" not in text and "user-z" not in text and "i-9" not in text and "internal-1" not in text


@pytest.mark.parametrize("tool", ["release_use", "get_use_status"])
def test_FR09_인자의_사용자_ID는_계정으로_쓰지_않는다(tool):
    c = Client()
    c.call({"use_id": "use-1", "user_id": "user-b"}, tool=tool)
    assert (c.core.releases + c.core.statuses)[0][0] == "user-a"


@pytest.mark.parametrize("tool", ["release_use", "get_use_status"])
def test_FR08_토큰이_없으면_두_도구도_401(tool):
    c = Client()
    r = run(c.post("tools/call", {"name": tool, "arguments": {"use_id": "use-1"}}, token=None))
    assert r.status_code == 401 and c.core.releases == [] and c.core.statuses == []
