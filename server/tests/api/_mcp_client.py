"""MCP 도구 테스트의 공통 도구. ASGI 앱에 JSON-RPC 요청을 직접 보낸다 (CI 의 dev 의존성에는 httpx 가 없다)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.mcp.server import build_app
from nearpilot.shared.enums import ReasonCode, Role, Verdict
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
        self.releases, self.statuses = [], []
        self.accesses = []
        self.access_decision = Decision(
            "internal-2", "r1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, node_id="locker-1",
            message="소유자의 승인을 기다립니다.")
        self.release_decision = Decision("internal-1", "", Verdict.OK, ReasonCode.OK, node_id="locker-1",
                                         use_id="use-1", message="반납을 완료했다.")
        self.status = {"use_id": "use-1", "state": "ACTIVE", "rental_due_at": NOW,
                       "overtime_sec": 0.0, "closed_at": None}
        self.devices = [{"node_id": "locker-1", "capability": "locker", "occupancy": "rental",
                         "access": "operable"}]
        self.device_state = {"node_id": "locker-1", "capability": "locker", "occupancy": "rental",
                             "state": "available", "online": True, "trusted": True,
                             "access_mode": "open"}
        self.device_calls, self.state_calls = [], []
        self.error: Exception | None = None
        self.decision = Decision("internal-1", "r1", Verdict.OK, ReasonCode.OK, node_id="locker-1",
                                 use_id="use-1", p_max=0.97, message=None)

    def request_use(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        return self.decision

    def request_access(self, user_id, node_id, action, request_id):
        self.accesses.append((user_id, node_id, action, request_id))
        if self.error:
            raise self.error
        return self.access_decision

    def find_available_devices(self, user_id):
        self.device_calls.append(user_id)
        if self.error:
            raise self.error
        return self.devices

    def get_device_state(self, user_id, node_id):
        self.state_calls.append((user_id, node_id))
        if self.error:
            raise self.error
        return self.device_state

    def release_use(self, user_id, use_id):
        self.releases.append((user_id, use_id))
        if self.error:
            raise self.error
        return self.release_decision

    def get_use_status(self, user_id, use_id):
        self.statuses.append((user_id, use_id))
        if self.error:
            raise self.error
        return self.status


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

    def call(self, args, token=TOKEN_A, tool="request_use"):
        r = run(self.post("tools/call", {"name": tool, "arguments": args}, token=token))
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        return json.loads(result["content"][0]["text"])
