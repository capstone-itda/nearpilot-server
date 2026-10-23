"""대시보드 테스트의 공통 도구. ASGI 앱을 직접 부른다 (CI 의 dev 의존성에는 httpx 와 pytest-asyncio 가 없다)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.shared.enums import (
    Action,
    ApprovalMode,
    Capability,
    Occupancy,
    ReasonCode,
    Role,
    SessionState,
    Step,
    TargetKind,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import Account, MonitorEvent, Subject
from nearpilot.shared.tokens import hash_token

NOW = datetime(2026, 10, 23, 14, 3, 5, 120000, tzinfo=timezone(timedelta(hours=9)))
OWNER_TOKEN, VISITOR_TOKEN = "tok-owner", "tok-visitor"
OWNER = Subject("user-owner", Role.OWNER)
VISITOR = Subject("user-visitor", Role.VISITOR)

_WAIT_SEC = 1.0  # 메시지를 기다리는 기본 시간. accept 와 앱 태스크가 끝나기를 기다리는 시간도 같다
_APP_DONE = object()  # 앱 태스크가 끝났다는 표시. 앱이 보낸 메시지 뒤에 온다


def settings(**overrides) -> DashboardSettings:
    """기본: queue_max_events=100, batch_max_events=50, auth_timeout_sec=1.0, audit_limit_max=200. 바꿀 값만 넘긴다."""
    values = {"queue_max_events": 100, "batch_max_events": 50, "auth_timeout_sec": 1.0, "audit_limit_max": 200}
    return DashboardSettings(**{**values, **overrides})


class FakeAccounts:
    """AccountLookup fake. OWNER_TOKEN 은 소유자 계정, VISITOR_TOKEN 은 방문자 계정이다.

    error 속성에 예외를 넣으면 조회가 그 예외를 던진다. calls 에 받은 해시를 쌓는다.
    """

    def __init__(self) -> None:
        self.known = {hash_token(OWNER_TOKEN): Account(OWNER.user_id, OWNER.role),
                      hash_token(VISITOR_TOKEN): Account(VISITOR.user_id, VISITOR.role)}
        self.error: Exception | None = None
        self.calls: list[str] = []

    def account_by_token_hash(self, token_hash):
        self.calls.append(token_hash)
        if self.error:
            raise self.error
        return self.known.get(token_hash)


def authenticator(accounts: FakeAccounts | None = None) -> Authenticator:
    return Authenticator(FakeAccounts() if accounts is None else accounts)


class FakeSnapshot:
    """MonitorSnapshot fake. events 속성(list[MonitorEvent])을 돌려준다. error 가 있으면 던진다.

    calls 로 부른 횟수를 센다.
    """

    def __init__(self, events: list[MonitorEvent] | None = None) -> None:
        self.events: list[MonitorEvent] = [] if events is None else events
        self.error: Exception | None = None
        self.calls = 0

    def current_events(self) -> list[MonitorEvent]:
        self.calls += 1
        if self.error:
            raise self.error
        return self.events


class FakeQueries:
    """DashboardQueries fake. 메서드마다 표본 결과 속성(places_data, uses_data, approvals_data, audit_data, history_data,
    request_data, trust_data, rental_data, health_data)을 돌려준다.

    표본은 조회 형식의 모든 키(선택 키 포함)를 채우고, 사람은 OWNER·VISITOR Subject 를 쓴다. 사람이 있는 목록은
    OWNER 행이 먼저다. calls 에 (메서드 이름, 인자 튜플)을 쌓는다. error 속성에 예외를 넣으면 모든 메서드가 던진다.
    request_data / rental_data 를 None 으로 두면 None 을 돌려준다 (없는 요청, 없는 대여).
    """

    def __init__(self) -> None:
        early, later = NOW - timedelta(minutes=30), NOW + timedelta(minutes=45)
        self.error: Exception | None = None
        self.calls: list[tuple[str, tuple]] = []
        lounge = (  # 사물함 3, 조명 3, 스피커 1
            [{"nodeId": f"locker-{n}", "name": f"사물함 {n}", "capability": Capability.LOCKER,
              "occupancy": Occupancy.RENTAL} for n in (1, 2, 3)]
            + [{"nodeId": f"light-{n}", "name": f"조명 {n}", "capability": Capability.LIGHT,
                "occupancy": Occupancy.SHARED} for n in (1, 2, 3)]
            + [{"nodeId": "speaker-1", "name": "스피커 1", "capability": Capability.SPEAKER,
                "occupancy": Occupancy.SHARED}]
        )
        self.places_data: Any = {  # 한 소유자가 두 장소를 갖는다. weakRssiDbm 은 응답 전체에 하나다
            "weakRssiDbm": -80,
            "places": [
                {"placeId": "lounge", "placeName": "라운지", "modelVersion": "model-1",
                 "calibrationVersion": "cal-1", "devices": lounge},
                {"placeId": "side-room", "placeName": "옆방", "modelVersion": "model-2",
                 "calibrationVersion": "cal-2",
                 "devices": [{"nodeId": "mood-1", "name": "무드등", "capability": Capability.LIGHT,
                              "occupancy": Occupancy.SHARED}]},
            ],
        }
        self.uses_data: Any = [
            {"subject": OWNER, "nodeId": "locker-2", "occupancy": Occupancy.RENTAL, "session": SessionState.RESERVED,
             "sessionNote": "예약됨", "startedAt": NOW, "overdueMinutes": 0},
            {"subject": VISITOR, "nodeId": "locker-1", "occupancy": Occupancy.RENTAL, "session": SessionState.ACTIVE,
             "sessionNote": "반납 확인 중", "startedAt": early, "overdueMinutes": 12.5},
        ]
        self.approvals_data: Any = {
            "pending": [{"subject": VISITOR, "nodeId": "locker-1", "mode": ApprovalMode.ONE_TIME, "requestedAt": NOW}],
            "active": [{"subject": VISITOR, "nodeId": "light-1", "mode": ApprovalMode.TIME_WINDOW,
                        "requestedAt": early, "remainingMinutes": 45, "validUntil": later, "uses": 1}],
            "ended": [{"subject": VISITOR, "nodeId": "locker-2", "mode": ApprovalMode.ONE_TIME, "requestedAt": early,
                       "uses": 1, "endReason": ReasonCode.APPROVAL_EXHAUSTED}],
        }
        self.audit_data: Any = [  # 최근 것부터
            {"id": "a-2", "at": NOW, "step": Step.EXEC, "nodeId": "locker-1", "text": "명령을 보냈다",
             "code": ReasonCode.OK},
            {"id": "a-1", "at": early, "step": Step.PROXIMITY, "nodeId": "locker-1", "text": "후보 확률이 임계값보다 낮다",
             "code": ReasonCode.LOW_CONFIDENCE},
        ]
        self.history_data: Any = [
            {"subject": OWNER, "requestId": "req-2", "at": NOW,
             "target": {"kind": TargetKind.EXPLICIT, "nodeId": "light-1"}, "action": Action.ON,
             "verdict": Verdict.OK, "nodeId": "light-1", "outcome": "succeeded", "code": ReasonCode.OK},
            {"subject": VISITOR, "requestId": "req-1", "at": early,
             "target": {"kind": TargetKind.NEAREST, "capability": Capability.LOCKER}, "action": Action.OPEN,
             "verdict": Verdict.ASK, "nodeId": "locker-1", "outcome": "더 가까이 와야 한다",
             "code": ReasonCode.LOW_CONFIDENCE},
        ]
        self.request_data: Any = {
            "subject": VISITOR, "id": "req-1", "tool": "request_use",
            "target": {"kind": TargetKind.NEAREST, "capability": Capability.LOCKER}, "action": Action.OPEN, "at": NOW,
            "verdict": Verdict.OK, "reason": ReasonCode.OK,
            "steps": [{"result": "pass", "detail": f"단계 {n}"} for n in range(7)],
            "nodeId": "locker-1", "posteriors": {"locker-1": 0.97, "light-1": 0.03}, "threshold": 0.9,
            "execution": "succeeded", "accessRequestedAt": early, "accessExpiredAt": later, "respondedAt": NOW,
        }
        self.trust_data: Any = {
            "live": {
                "since": early, "decisions": 40, "doubleOccupancy": 0, "latencyMedianSec": 0.84, "latencyP95Sec": 1.9,
                "counts": {Verdict.OK: 30, Verdict.BUSY: 4, Verdict.ASK: 3, Verdict.DENY: 2, Verdict.NEED_APPROVAL: 1},
            },
            "evaluation": {
                "date": "2026-10-20",
                "metrics": [
                    {"id": 1, "name": "올바른 기기 선택", "pass": True, "label": "정확도", "sample": "n=50",
                     "sampleNote": "시연 장소 기준"},
                    {"id": 2, "name": "이중 점유", "pass": None, "label": "건수", "sample": "n=0"},
                ],
                "confusion": {"labels": ["locker-1", "light-1", "none"],
                              "values": [[98, 1, 1], [2, 97, 1], [0, 0, 100]]},
            },
        }
        self.rental_data: Any = {
            "subject": VISITOR, "nodeId": "locker-1", "startedAt": early, "overdueMinutes": 12.5,
            "timeline": [{"at": early, "text": "대여를 시작했다"}, {"at": NOW, "text": "대여 시간이 지났다", "now": True}],
        }
        self.health_data: Any = {
            "devices": [
                {"nodeId": "locker-1", "connected": True, "trust": TrustState.TRUSTED},
                {"nodeId": "light-1", "connected": False, "trust": TrustState.DISCOVERED},
            ],
            "beacons": [
                {"subject": OWNER, "lastSeenSec": 1.5, "note": "신호가 강하다"},
                {"subject": VISITOR, "lastSeenSec": 2.5, "note": "신호가 약하다"},
            ],
            "server": {"database": True, "mqtt": True, "model": "model-1"},
        }

    def _answer(self, name: str, args: tuple, data: Any) -> Any:
        self.calls.append((name, args))
        if self.error:
            raise self.error
        return data

    def places(self):
        return self._answer("places", (), self.places_data)

    def uses(self):
        return self._answer("uses", (), self.uses_data)

    def approvals(self):
        return self._answer("approvals", (), self.approvals_data)

    def audit(self, limit):
        return self._answer("audit", (limit,), self.audit_data)

    def history(self, period, verdict, node_id):
        return self._answer("history", (period, verdict, node_id), self.history_data)

    def request(self, request_id):
        return self._answer("request", (request_id,), self.request_data)

    def trust(self):
        return self._answer("trust", (), self.trust_data)

    def rental(self, node_id):
        return self._answer("rental", (node_id,), self.rental_data)

    def health(self):
        return self._answer("health", (), self.health_data)


def run(coro, timeout: float = 10.0):
    """asyncio.run 감싸기. pytest-asyncio 가 없어서 동기 테스트 함수가 비동기 코드를 이렇게 돌린다.

    고장 난 코드가 테스트를 멈추지 않게 기한을 건다. 넘으면 TimeoutError 다.
    """
    async def bounded():
        async with asyncio.timeout(timeout):
            return await coro

    return asyncio.run(bounded())


async def http_request(
    app, method: str, path: str, *, query: str = "", headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    """ASGI http 요청을 직접 보낸다. (상태 코드, JSON 본문 또는 본문이 JSON 이 아니면 bytes) 를 돌려준다.

    query 는 "?" 없이 쓴다 (예: "limit=10"). 앱이 예외를 올리면 그 예외가 그대로 올라온다.
    """
    host = {"host": "127.0.0.1:8000", "content-length": "0",
            **{k.lower(): v for k, v in (headers or {}).items()}}
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in host.items()],
    }
    sent, status, chunks = False, None, []

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    assert status is not None, "앱이 응답을 시작하지 않았다"
    body = b"".join(chunks)
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


class WsClosed(Exception):
    """앱이 websocket 을 닫았다. code 는 websocket.close 의 코드다."""

    def __init__(self, code: int) -> None:
        super().__init__(f"websocket 이 닫혔다 (code={code})")
        self.code = code


class WsClient:
    """ASGI websocket 을 직접 부르는 클라이언트. async with 로 쓴다.

    async with WsClient(app, "/dashboard/stream", blocking=False) as ws:
        await ws.send_json({...})
        msg = await ws.receive_json(timeout=1.0)   # 다음 websocket.send 의 JSON. close 가 오면 WsClosed(code) 를 던진다
        code = await ws.receive_close(timeout=1.0)  # 다음 메시지가 close 여야 하고 그 코드를 돌려준다
    blocking=True 면 앱의 send 가 테스트가 그 메시지를 받을 때까지 기다린다 (늦은 클라이언트 흉내).
    진입 시 websocket.connect 를 보내고 websocket.accept 를 기다린다 (앱이 accept 대신 close 하면 WsClosed).
    나갈 때 websocket.disconnect 를 보내고 앱 태스크가 끝나기를 기다린다 (최대 1초, 넘으면 취소).

    기한 안에 메시지가 오지 않으면 TimeoutError 다. 앱이 예외로 끝나면 받는 쪽이 그 예외를 올린다.
    나갈 때도 테스트가 이미 실패하는 중이 아니면 올린다 (취소한 앱은 올리지 않는다).
    """

    def __init__(self, app, path: str, blocking: bool = False) -> None:
        self._app, self._path, self._blocking = app, path, blocking
        self._inbox: asyncio.Queue = asyncio.Queue()  # 클라이언트 → 앱
        self._outbox: asyncio.Queue = asyncio.Queue()  # 앱 → 클라이언트. (메시지, 받았다는 표시) 또는 _APP_DONE
        self._left = False
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> WsClient:
        self._inbox.put_nowait({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(self._scope(), self._receive, self._send))
        self._task.add_done_callback(lambda _: self._outbox.put_nowait(_APP_DONE))
        try:
            message = await self._next(_WAIT_SEC)
            if message["type"] != "websocket.accept":
                raise AssertionError(f"accept 가 와야 하는데 {message['type']} 가 왔다")
        except BaseException:
            await self._leave()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._leave()
        error = None if self._task.cancelled() else self._task.exception()
        if exc_type is None and error is not None:
            raise error

    async def send_json(self, data: Any) -> None:
        await self.send_text(json.dumps(data))

    async def send_text(self, text: str) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "text": text})

    async def receive_json(self, timeout: float = _WAIT_SEC) -> Any:
        message = await self._next(timeout)
        if message["type"] != "websocket.send":
            raise AssertionError(f"send 가 와야 하는데 {message['type']} 가 왔다")
        text = message.get("text")
        return json.loads(message["bytes"] if text is None else text)

    async def receive_close(self, timeout: float = _WAIT_SEC) -> int:
        try:
            message = await self._next(timeout)
        except WsClosed as closed:
            return closed.code
        raise AssertionError(f"close 가 와야 하는데 {message['type']} 가 왔다: {message.get('text')}")

    async def _next(self, timeout: float) -> dict:
        """앱이 보낸 다음 메시지. close 면 WsClosed 를, 앱이 끝났으면 앱의 예외를 올린다."""
        async with asyncio.timeout(timeout):  # wait_for 는 timeout=0 이면 쌓인 메시지가 있어도 TimeoutError 다
            item = await self._outbox.get()
        if item is _APP_DONE:
            self._outbox.put_nowait(_APP_DONE)  # 이어지는 호출도 같은 결과를 본다
            error = None if self._task.cancelled() else self._task.exception()
            if error is not None:
                raise error
            raise AssertionError("앱이 받을 메시지를 남기지 않고 끝났다")
        message, received = item
        received.set()
        if message["type"] == "websocket.close":
            raise WsClosed(message.get("code", 1000))
        return message

    async def _leave(self) -> None:
        """disconnect 를 보내고 앱 태스크가 끝나기를 기다린다. 기한을 넘으면 취소한다."""
        self._left = True
        self._inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
        while not self._outbox.empty():  # 받는 쪽이 없어진 send 를 풀어 준다
            item = self._outbox.get_nowait()
            if item is not _APP_DONE:
                item[1].set()
        done, _ = await asyncio.wait({self._task}, timeout=_WAIT_SEC)
        if not done:
            self._task.cancel()
            await asyncio.wait({self._task})

    async def _receive(self) -> dict:
        message = await self._inbox.get()
        if message["type"] == "websocket.disconnect":
            self._inbox.put_nowait(message)  # 나간 뒤에는 receive 가 계속 disconnect 를 돌려준다
        return message

    async def _send(self, message: dict) -> None:
        if self._left:  # 클라이언트가 나간 뒤에 앱이 보낸 메시지는 버린다
            return
        received = asyncio.Event()
        self._outbox.put_nowait((message, received))
        if self._blocking:
            await received.wait()

    def _scope(self) -> dict:
        return {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "scheme": "ws", "path": self._path, "raw_path": self._path.encode(), "query_string": b"",
            "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
            "headers": [(b"host", b"127.0.0.1:8000")], "subprotocols": [],
        }
