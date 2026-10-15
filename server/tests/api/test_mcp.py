"""FR-08·FR-09 MCP 엔드포인트와 request_use 도구. 서비스와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.mcp.server import build_app
from nearpilot.shared.enums import Action, ReasonCode, Role, TargetKind, Verdict
from nearpilot.shared.models import Account, Decision
from nearpilot.shared.tokens import hash_token

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TOKEN_A, TOKEN_B = "tok-A", "tok-B"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class FakeAccounts:
    def __init__(self):
        self.known = {hash_token(TOKEN_A): Account("user-a", Role.VISITOR),
                      hash_token(TOKEN_B): Account("user-b", Role.OWNER)}
        self.error: Exception | None = None

    def account_by_token_hash(self, token_hash):
        if self.error:
            raise self.error
        return self.known.get(token_hash)


class FakeCore:
    def __init__(self):
        self.requests = []
        self.error: Exception | None = None
        self.decision = Decision("internal-1", "r1", Verdict.OK, ReasonCode.OK, node_id="locker-1",
                                 use_id="use-1", p_max=0.97, message=None)

    def request_use(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        return self.decision


def run(coro):
    return asyncio.run(coro)


class Response:
    def __init__(self, status_code: int, headers: dict[str, str], content: bytes):
        self.status_code, self.headers, self.text = status_code, headers, content.decode()

    def json(self):
        return json.loads(self.text)


async def asgi_post(app, path: str, headers: dict[str, str], body: bytes) -> Response:
    """ASGI 앱에 HTTP POST 를 직접 보낸다. CI 의 dev 의존성에는 httpx 가 없다."""
    host = {"host": "127.0.0.1:8000", "content-length": str(len(body)), **headers}
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in host.items()],
    }
    sent, start, chunks = False, {}, []

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(status=message["status"], headers=message["headers"])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return Response(start["status"],
                    {k.decode().lower(): v.decode() for k, v in start["headers"]}, b"".join(chunks))


class Client:
    """ASGI 앱에 직접 JSON-RPC 요청을 보낸다. 서버는 stateless + JSON 응답이다."""

    def __init__(self):
        self.accounts, self.core = FakeAccounts(), FakeCore()

    def new_app(self):
        # MCP 세션 매니저는 앱 하나에서 한 번만 시작할 수 있다. 요청마다 새 앱을 만든다.
        return build_app(self.core, Authenticator(self.accounts), lambda: NOW)

    async def post(self, method, params=None, token=TOKEN_A, headers=None, id_=1):
        h = {**HEADERS, **(headers or {})}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
        app = self.new_app()
        async with app.router.lifespan_context(app):
            return await asgi_post(app, "/mcp", h, json.dumps(body).encode())

    def call(self, args, token=TOKEN_A):
        r = run(self.post("tools/call", {"name": "request_use", "arguments": args}, token=token))
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        return json.loads(result["content"][0]["text"])


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


# ── FR-09 request_use ───────────────────────────────────────────────


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


def test_FR10_호스트_이름과_상관없이_같은_응답():
    responses = []
    for name in ("claude", "chatgpt", "gemini"):
        c = Client()
        init = {**INIT, "clientInfo": {"name": name, "version": "1"}}
        assert run(c.post("initialize", init)).status_code == 200
        responses.append(c.call(ARGS))
    assert responses[0] == responses[1] == responses[2]


def test_FR11_응답에_내부_값이_없다():
    c = Client()
    resp = c.call(ARGS)
    text = json.dumps(resp)
    assert set(resp) == {"verdict", "reason", "node_id", "use_id", "request_id", "message"}
    assert "user-a" not in text and "internal-1" not in text and "0.97" not in text
