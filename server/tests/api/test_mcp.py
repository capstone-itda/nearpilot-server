"""FR-08·FR-09 MCP 엔드포인트와 request_use 도구. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import json

import pytest

from _mcp_client import NOW, TOKEN_A, TOKEN_B, Client, run
from nearpilot.shared.enums import Action, ReasonCode, TargetKind, Verdict
from nearpilot.shared.models import Decision


ARGS = {"target_spec": "nearest:locker", "action": "open", "request_id": "r1"}
INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "x", "version": "1"}}


# ── FR-08 인증 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["initialize", "tools/list"])
def test_FR08_토큰이_없으면_401이고_MCP를_처리하지_않는다(method):
    c = Client()
    r = run(c.post(method, INIT if method == "initialize" else {}, token=None))
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert r.json() == {"error": "INVALID_TOKEN"}
    assert c.core.requests == []


def test_FR08_무효한_토큰은_401():
    c = Client()
    r = run(c.post("tools/list", token="not-registered"))
    assert r.status_code == 401 and r.json() == {"error": "INVALID_TOKEN"}
    assert "not-registered" not in r.text


def test_FR08_Bearer가_아닌_헤더는_401():
    c = Client()
    r = run(c.post("tools/list", token=None, headers={"Authorization": f"Basic {TOKEN_A}"}))
    assert r.status_code == 401


def test_FR08_계정_조회_실패는_500():
    c = Client()
    c.accounts.error = RuntimeError("secret internal detail")
    r = run(c.post("tools/list"))
    assert r.status_code == 500 and r.json() == {"error": "INTERNAL_ERROR"}
    assert "secret" not in r.text


def test_FR08_공개_Host를_설정하면_터널로_온_요청을_받는다():
    c = Client(public_hosts=("mcp.example.test",))
    r = run(c.post("tools/list", headers={"host": "mcp.example.test",
                                          "origin": "https://mcp.example.test"}))
    assert r.status_code == 200, r.text
    assert run(c.post("tools/list")).status_code == 200  # localhost 는 그대로 받는다


@pytest.mark.parametrize("public_hosts", [(), ("mcp.example.test",)])
def test_FR08_설정하지_않은_Host는_유효한_토큰이어도_421(public_hosts):
    c = Client(public_hosts=public_hosts)
    r = run(c.post("tools/list", headers={"host": "evil.example.test"}))
    assert r.status_code == 421
    assert c.core.requests == []


def test_FR08_설정하지_않은_Origin은_403():
    c = Client(public_hosts=("mcp.example.test",))
    r = run(c.post("tools/list", headers={"host": "mcp.example.test",
                                          "origin": "https://evil.example.test"}))
    assert r.status_code == 403


# ── FR-09 request_use ───────────────────────────────────────────────


@pytest.mark.parametrize("host", ["192.168.0.10:8000", "10.42.0.1:8000", "nearpilot.local:8000"])
def test_FR08_AP_주소로_붙어도_처리한다(host):
    c = Client(public_hosts=(host,))  # 서버의 AP 주소를 `[http] public_hosts`에 넣는다
    r = run(c.post("tools/list", headers={"host": host}))
    assert r.status_code == 200, r.text
    assert any(tool["name"] == "request_use" for tool in r.json()["result"]["tools"])


@pytest.mark.parametrize("host", ["192.168.0.10:8000", "10.42.0.1:8000"])
def test_FR08_AP_주소여도_토큰이_없으면_401(host):
    c = Client(public_hosts=(host,))
    r = run(c.post("tools/list", token=None, headers={"host": host}))
    assert r.status_code == 401


def test_FR09_도구_목록에_request_use가_있다():
    r = run(Client().post("tools/list"))
    assert r.status_code == 200
    assert "request_use" in [t["name"] for t in r.json()["result"]["tools"]]


def test_FR09_request_use는_토큰_계정으로_요청을_만든다():
    c = Client()
    resp = c.call(ARGS)
    (req,) = c.core.requests
    assert (req.user_id, req.request_id, req.action, req.received_at) == ("user-a", "r1", Action.OPEN, NOW)
    assert (req.target.kind, req.target.capability.value) == (TargetKind.NEAREST, "locker")
    assert req.host_kind is None and req.parent_internal_request_id is None
    assert resp == {"verdict": "OK", "reason": "OK", "node_id": "locker-1", "use_id": "use-1",
                    "request_id": "r1", "message": None}


def test_FR09_인자의_사용자_ID는_계정으로_쓰지_않는다():
    c = Client()
    c.call({**ARGS, "user_id": "user-b"})
    assert c.core.requests[0].user_id == "user-a"


def test_FR09_명시_지정_대상을_해석한다():
    c = Client()
    c.call({**ARGS, "target_spec": "locker-1", "action": "open"})
    assert (c.core.requests[0].target.kind, c.core.requests[0].target.node_id) == (TargetKind.EXPLICIT, "locker-1")


@pytest.mark.parametrize("bad", [
    {"target_spec": "locker"}, {"target_spec": "nearest:toaster"}, {"target_spec": ""},
    {"action": "explode"}, {"action": ""}, {"request_id": ""},
])
def test_FR09_인자가_틀리면_INVALID_INPUT이고_서비스를_부르지_않는다(bad):
    c = Client()
    resp = c.call({**ARGS, **bad})
    assert (resp["verdict"], resp["reason"], resp["use_id"], resp["node_id"]) == \
        ("DENY", "INVALID_INPUT", None, None)
    assert c.core.requests == []


def test_FR09_서비스_예외는_INTERNAL_ERROR():
    c = Client()
    c.core.error = RuntimeError("secret internal detail")
    resp = c.call(ARGS)
    assert (resp["verdict"], resp["reason"]) == ("DENY", "INTERNAL_ERROR")
    assert "secret" not in json.dumps(resp)
    assert resp["message"]  # 일반 안내만 있다


def test_FR09_재전송은_같은_request_id로_서비스를_부른다():
    c = Client()
    first, second = c.call(ARGS), c.call(ARGS)
    assert [r.request_id for r in c.core.requests] == ["r1", "r1"] and first == second


def test_FR09_다른_계정의_같은_request_id는_각자의_계정으로_전달한다():
    c = Client()
    c.call(ARGS, token=TOKEN_A)
    c.call(ARGS, token=TOKEN_B)
    assert [(r.user_id, r.request_id) for r in c.core.requests] == [("user-a", "r1"), ("user-b", "r1")]


def test_FR09_거부_판정도_그대로_응답한다():
    c = Client()
    c.core.decision = Decision("i", "r1", Verdict.ASK, ReasonCode.LOW_CONFIDENCE, p_max=0.5)
    resp = c.call(ARGS)
    assert (resp["verdict"], resp["reason"], resp["node_id"], resp["use_id"]) == \
        ("ASK", "LOW_CONFIDENCE", None, None)


# ── FR-10·FR-11 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["claude-code", "codex", "gemini-cli"])
def test_FR10_호스트_이름과_상관없이_같은_응답(host):
    # 3종 호스트는 CLI 3종으로 확인한다 (FR-10, 2026-10-27 팀 결정). 서버는 stateless 라 호스트 차이는
    # 도구 호출 요청 자체(User-Agent)에 실린다. 응답과 서비스가 받은 요청이 호스트와 상관없이 같다.
    baseline, c = Client(), Client()
    expected = baseline.call(ARGS)
    init = {**INIT, "clientInfo": {"name": host, "version": "1"}}
    assert run(c.post("initialize", init, headers={"user-agent": f"{host}/1.0"})).status_code == 200
    r = run(c.post("tools/call", {"name": "request_use", "arguments": ARGS}, headers={"user-agent": f"{host}/1.0"}))
    assert json.loads(r.json()["result"]["content"][0]["text"]) == expected
    assert c.core.requests == baseline.core.requests


def test_FR11_응답에_내부_값이_없다():
    c = Client()
    resp = c.call(ARGS)
    text = json.dumps(resp)
    assert set(resp) == {"verdict", "reason", "node_id", "use_id", "request_id", "message"}
    assert "user-a" not in text and "internal-1" not in text and "0.97" not in text
