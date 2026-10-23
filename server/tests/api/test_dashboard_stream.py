"""FR-12B 모니터링 스트림 엔드포인트. 첫 메시지로 소유자를 인증하고, 지금 상태를 먼저 보낸 뒤 허브의 묶음을 이어서 보낸다."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI

from _dashboard import NOW, OWNER, OWNER_TOKEN, VISITOR, VISITOR_TOKEN, FakeAccounts, FakeSnapshot, WsClient
from _dashboard import authenticator, run, settings
from nearpilot.api.ws.hub import MonitorHub, Subscription
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.stream import add_stream_route
from nearpilot.shared.enums import DeviceState
from nearpilot.shared.models import MonitorEvent

STREAM_LOGGER = "nearpilot.api.ws.stream"
PATH = "/dashboard/stream"
ISO = "2026-10-23T14:03:05.120+09:00"  # NOW
AUTH = {"type": "auth", "token": OWNER_TOKEN}
EMPTY = {"v": 1, "events": []}  # 이벤트가 없는 메시지


def result(request_id: str) -> MonitorEvent:
    """사람이 필요 없는 유효한 이벤트."""
    return MonitorEvent("mcp_result", NOW, {"requestId": request_id})


def beacon() -> MonitorEvent:
    """사람이 있는 유효한 이벤트."""
    return MonitorEvent("beacon", NOW, {"seen": True}, VISITOR)


def ids(message: dict[str, Any]) -> list[str]:
    """메시지에 담긴 이벤트들의 requestId."""
    return [event["requestId"] for event in message["events"]]


async def until(condition) -> None:
    """조건이 참이 될 때까지 루프에 양보한다. 기한은 run 이 건다."""
    while not condition():
        await asyncio.sleep(0.001)


class SpyHub(MonitorHub):
    """구독과 구독 해지를 순서대로 쌓는다."""

    def __init__(self, names: PersonNames, config) -> None:
        super().__init__(names, config)
        self.subscribed: list[Subscription] = []
        self.unsubscribed: list[Subscription] = []

    def subscribe(self) -> Subscription:
        subscription = super().subscribe()
        self.subscribed.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self.unsubscribed.append(subscription)
        super().unsubscribe(subscription)


class BinaryWsClient(WsClient):
    """WsClient 에 binary 프레임 보내기를 더한다."""

    async def send_bytes(self, data: bytes) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "bytes": data})


class Rig:
    """스트림 엔드포인트를 붙인 테스트 앱과 그 fake 들. 바꿀 설정만 넘긴다.

    sent 에 앱이 보낸 ASGI 메시지를 쌓는다. ended 에는 앱 호출이 끝난 방식을 쌓는다:
    "returned" 는 스스로 끝났고, "cancelled" 는 WsClient 가 기한을 넘겨 취소했고, "raised" 는 예외로 끝났다.
    fail_close 가 True 면 앱이 닫기 메시지를 내보낼 때 연결이 끊긴 것처럼 OSError 를 올린다.
    """

    def __init__(self, **overrides) -> None:
        config = settings(**overrides)
        self.hub = SpyHub(PersonNames(), config)
        self.snapshot = FakeSnapshot()
        self.accounts = FakeAccounts()
        self.sent: list[dict[str, Any]] = []
        self.ended: list[str] = []
        self.fail_close = False
        router = APIRouter()
        add_stream_route(
            router, hub=self.hub, snapshot=self.snapshot, authenticator=authenticator(self.accounts), settings=config)
        self._app = FastAPI()
        self._app.include_router(router)

    async def app(self, scope, receive, send) -> None:
        async def spy(message) -> None:
            if self.fail_close and message["type"] == "websocket.close":
                raise OSError("연결이 끊겼다")
            self.sent.append(message)
            await send(message)

        try:
            await self._app(scope, receive, spy)
        except asyncio.CancelledError:
            self.ended.append("cancelled")
            raise
        except Exception:
            self.ended.append("raised")
            raise
        else:
            self.ended.append("returned")

    def connect(self, *, blocking: bool = False) -> BinaryWsClient:
        return BinaryWsClient(self.app, PATH, blocking)

    def assert_not_started(self) -> None:
        """인증하기 전에는 허브에 구독하지 않고 지금 상태를 읽지 않는다."""
        assert self.hub.subscribed == []
        assert self.snapshot.calls == 0


def test_FR12B_연결하면_지금_상태를_먼저_보낸다():
    rig = Rig()
    rig.snapshot.events = [
        beacon(), MonitorEvent("transition", NOW, {"nodeId": "locker-1", "to": DeviceState.AVAILABLE})]

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            first = await ws.receive_json()
            rig.hub.publish(beacon())  # 지금 상태에 나온 방문자가 다시 나타난다
            return first, await ws.receive_json()

    first, second = run(main())
    visitor = {"personId": "p1", "pseudonym": "방문자 A", "letter": "A", "role": "visitor"}
    assert first == {"v": 1, "events": [
        {"type": "beacon", "at": ISO, "seen": True, **visitor},
        {"type": "transition", "at": ISO, "nodeId": "locker-1", "to": "AVAILABLE"},
    ]}
    assert second == {"v": 1, "events": [{"type": "beacon", "at": ISO, "seen": True, **visitor}]}
    assert first["events"][0]["personId"] == second["events"][0]["personId"]  # 같은 방문자는 같은 사람이다
    assert VISITOR.user_id not in json.dumps([first, second])


def test_FR12B_지금_상태_조회가_실패해도_스트림을_이어_간다(caplog):
    rig = Rig()
    rig.snapshot.error = RuntimeError(f"조회 고장: {VISITOR.user_id}")

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            first = await ws.receive_json()
            rig.hub.publish(result("r1"))
            return first, await ws.receive_json()

    first, second = run(main())
    assert first == EMPTY  # 빈 묶음도 첫 메시지로 보낸다
    assert ids(second) == ["r1"]
    assert rig.snapshot.calls == 1
    [record] = [r for r in caplog.records if r.name == STREAM_LOGGER]
    assert record.levelno == logging.ERROR and "RuntimeError" in record.getMessage()  # 예외 종류만 남는다
    assert record.exc_info is None  # 트레이스백에는 예외 메시지가 있다
    assert VISITOR.user_id not in caplog.text


def test_FR12B_지금_상태가_비어도_첫_메시지를_보낸다():
    rig = Rig()

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            return await ws.receive_json()

    assert run(main()) == EMPTY


def test_FR12B_지금_상태의_틀린_이벤트는_빼고_묶음_크기로_나누지_않는다():
    rig = Rig(batch_max_events=2)
    rig.snapshot.events = [
        result("s0"), MonitorEvent("not_a_type", NOW, {}), result("s1"), result("s2"), result("s3")]

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            return await ws.receive_json()

    assert ids(run(main())) == ["s0", "s1", "s2", "s3"]  # 틀린 이벤트만 빠지고 한 메시지에 모두 담긴다


def test_FR12B_지금_상태를_읽는_사이의_이벤트도_받는다(monkeypatch):
    rig = Rig()
    rig.snapshot.events = [result("s0")]
    read = rig.snapshot.current_events

    def read_while_publishing():
        rig.hub.publish(result("during"))  # 지금 상태를 읽는 도중에 난 이벤트
        return read()

    monkeypatch.setattr(rig.snapshot, "current_events", read_while_publishing)

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            return await ws.receive_json(), await ws.receive_json()

    first, second = run(main())
    assert ids(first) == ["s0"]
    assert ids(second) == ["during"]  # 구독을 먼저 등록했으므로 잃지 않고, 지금 상태 뒤에 온다


def test_FR12B_동기_접점은_이벤트_루프_밖의_스레드에서_부른다(monkeypatch):
    rig = Rig()
    called_in: dict[str, int] = {}

    def spy_on(target, method: str) -> None:
        original = getattr(target, method)

        def spy(*args):
            called_in[method] = threading.get_ident()
            return original(*args)

        monkeypatch.setattr(target, method, spy)

    spy_on(rig.accounts, "account_by_token_hash")
    spy_on(rig.snapshot, "current_events")

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            await ws.receive_json()
        return threading.get_ident()  # 이벤트 루프를 돌리는 스레드

    loop_thread = run(main())
    assert set(called_in) == {"account_by_token_hash", "current_events"}
    assert loop_thread not in called_in.values()


def test_FR12B_메시지는_한글을_그대로_담은_텍스트_프레임이다():
    rig = Rig()
    rig.snapshot.events = [beacon()]

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            await ws.receive_json()

    run(main())
    [frame] = [m for m in rig.sent if m["type"] == "websocket.send"]
    assert frame.get("bytes") is None
    assert "방문자 A" in frame["text"]  # \uXXXX 로 쓰지 않는다
    assert json.loads(frame["text"])["v"] == 1


def test_FR12B_대기_중인_이벤트를_한도까지_한_메시지에_묶는다():
    rig = Rig(batch_max_events=3)

    async def main():
        async with rig.connect(blocking=True) as ws:
            await ws.send_json(AUTH)
            await until(lambda: rig.snapshot.calls == 1)  # 구독을 마쳤다. 지금 상태를 보내는 데서 붙잡혀 있다
            for n in range(5):
                rig.hub.publish(result(f"r{n}"))
            first = await ws.receive_json()  # 받으면 붙잡힌 보내기가 풀리고 쌓인 이벤트가 이어진다
            batches: list[dict[str, Any]] = []
            while sum(len(batch["events"]) for batch in batches) < 5:
                batches.append(await ws.receive_json())
            return first, batches

    first, batches = run(main())
    assert first == EMPTY
    assert [len(batch["events"]) for batch in batches] == [3, 2]  # 한도까지 담고 나머지는 다음 메시지에
    assert [i for batch in batches for i in ids(batch)] == [f"r{n}" for n in range(5)]


def test_FR12B_대기열이_찬_연결만_닫는다():
    rig = Rig(queue_max_events=2)

    async def main():
        async with rig.connect() as fast, rig.connect(blocking=True) as slow:
            await fast.send_json(AUTH)
            await slow.send_json(AUTH)
            assert await fast.receive_json() == EMPTY
            await until(lambda: rig.snapshot.calls == 2)  # slow 도 구독을 마쳤다. 지금 상태를 보내는 데서 붙잡혀 있다
            received: list[str] = []
            for n in range(6):  # slow 는 한 번도 받지 않는다
                rig.hub.publish(result(f"r{n}"))
                received += ids(await fast.receive_json())
            assert received == [f"r{n}" for n in range(6)]  # 다른 연결은 모두 받는다
            assert await slow.receive_json() == EMPTY  # 넘치기 전에 보내던 메시지는 받는다
            assert await slow.receive_close() == 1013
            await until(lambda: len(rig.hub.unsubscribed) == 1)  # 닫힌 연결은 구독도 뺀다
            rig.hub.publish(result("after"))
            assert ids(await fast.receive_json()) == ["after"]  # 닫힌 건 slow 뿐이다

    run(main())
    assert len(rig.hub.subscribed) == 2
    assert [sub.overflowed for sub in rig.hub.unsubscribed] == [True, False]  # slow 가 먼저 빠지고 fast 는 끊을 때 빠진다
    assert rig.ended == ["returned", "returned"]


def test_FR12B_인증_기한이_지나면_닫는다():
    rig = Rig(auth_timeout_sec=0.05)

    async def main():
        async with rig.connect() as ws:
            return await ws.receive_close()  # 아무것도 보내지 않는다

    assert run(main()) == 4408
    rig.assert_not_started()
    assert rig.accounts.calls == []


def test_FR12B_인증한_연결은_인증_기한이_지나도_이어진다():
    rig = Rig(auth_timeout_sec=0.2)

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            assert await ws.receive_json() == EMPTY
            await asyncio.sleep(0.5)  # 인증 기한의 두 배를 넘긴다
            rig.hub.publish(result("late"))
            return ids(await ws.receive_json())

    assert run(main()) == ["late"]


INVALID_AUTH = {
    "틀린_토큰": json.dumps({"type": "auth", "token": "tok-wrong"}),
    "빈_토큰": json.dumps({"type": "auth", "token": ""}),
    "토큰이_없다": json.dumps({"type": "auth"}),
    "토큰이_문자열이_아니다": json.dumps({"type": "auth", "token": 123}),
    "공백이_든_토큰": json.dumps({"type": "auth", "token": "tok owner"}),
    "Bearer를_붙인_토큰": json.dumps({"type": "auth", "token": f"Bearer {OWNER_TOKEN}"}),
    "type이_auth가_아니다": json.dumps({"type": "login", "token": OWNER_TOKEN}),
    "type이_없다": json.dumps({"token": OWNER_TOKEN}),
    "JSON이_아니다": "이건 JSON 이 아니다",
    "JSON이_객체가_아니다": json.dumps([OWNER_TOKEN]),
    "UTF8로_못_바꾸는_토큰": '{"type": "auth", "token": "tok-\\ud800"}',  # 짝 없는 서로게이트. 해시에서 실패한다
    "너무_깊이_중첩된_JSON": "[" * 1_000_000,  # json.loads 가 JSONDecodeError 가 아니라 RecursionError 를 올린다
}


@pytest.mark.parametrize("text", INVALID_AUTH.values(), ids=INVALID_AUTH.keys())
def test_FR12B_토큰이_틀리면_닫는다(text):
    rig = Rig()

    async def main():
        async with rig.connect() as ws:
            await ws.send_text(text)
            return await ws.receive_close()

    assert run(main()) == 4401
    rig.assert_not_started()


def test_FR12B_binary_프레임은_인증으로_받지_않는다():
    rig = Rig()

    async def main():
        async with rig.connect() as ws:
            await ws.send_bytes(json.dumps(AUTH).encode())  # 내용이 맞아도 text 프레임이 아니다
            return await ws.receive_close()

    assert run(main()) == 4401
    rig.assert_not_started()
    assert rig.accounts.calls == []


def test_FR12B_방문자_토큰은_닫는다():
    rig = Rig()

    async def main():
        async with rig.connect() as ws:
            await ws.send_json({"type": "auth", "token": VISITOR_TOKEN})
            return await ws.receive_close()

    assert run(main()) == 4403
    rig.assert_not_started()


def test_FR12B_계정_조회가_실패하면_닫는다(caplog):
    rig = Rig()
    rig.accounts.error = RuntimeError("계정 조회 고장")

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            return await ws.receive_close()

    assert run(main()) == 1011
    rig.assert_not_started()
    assert OWNER_TOKEN not in caplog.text


def test_FR12B_인증하기_전에_클라이언트가_끊으면_조용히_끝난다():
    rig = Rig(auth_timeout_sec=30)

    async def main():
        async with rig.connect():
            pass  # 아무것도 보내지 않고 나간다. 앱이 예외를 올리면 WsClient 가 올린다

    run(main())
    rig.assert_not_started()
    assert rig.ended == ["returned"]  # 인증 기한을 기다리지 않고 스스로 끝났다
    assert [m["type"] for m in rig.sent] == ["websocket.accept"]  # 끊긴 연결에는 닫기도 보내지 않는다


def test_FR12B_클라이언트가_끊으면_허브_구독이_빠진다():
    rig = Rig()

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            await ws.receive_json()
            assert len(rig.hub.subscribed) == 1
            assert rig.hub.unsubscribed == []  # 연결해 있는 동안은 구독 중이다

    run(main())
    assert rig.hub.unsubscribed == rig.hub.subscribed
    assert rig.ended == ["returned"]  # 보내기 루프가 끊김을 알아채고 스스로 끝났다


def test_FR12B_인증_뒤_클라이언트가_보낸_메시지는_무시한다():
    rig = Rig()
    ignored = [
        ("send_json", {"type": "auth", "token": VISITOR_TOKEN}),  # 다시 인증하지 않는다
        ("send_json", {"type": "subscribe", "nodeId": "locker-1"}),
        ("send_text", "JSON 이 아니다"),
        ("send_bytes", b"\x00\x01"),
    ]

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            assert await ws.receive_json() == EMPTY
            for n, (method, data) in enumerate(ignored):
                await getattr(ws, method)(data)
                rig.hub.publish(result(f"r{n}"))
                assert ids(await ws.receive_json()) == [f"r{n}"]  # 끊기지도 달라지지도 않고 이어진다
            rig.hub.publish(result("last"))
            assert ids(await ws.receive_json()) == ["last"]  # 마지막 메시지 뒤에도 연결이 살아 있다

    run(main())
    assert len(rig.accounts.calls) == 1  # 첫 메시지만 인증했다
    assert rig.snapshot.calls == 1


def test_FR12B_보내는_중_예외가_나면_1011로_닫는다(caplog, monkeypatch):
    async def broken(self):
        raise RuntimeError(f"보내기 고장: {OWNER.user_id}")

    monkeypatch.setattr(Subscription, "next_batch", broken)
    rig = Rig()

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            return await ws.receive_json(), await ws.receive_close()

    assert run(main()) == (EMPTY, 1011)
    [record] = [r for r in caplog.records if r.name == STREAM_LOGGER]
    assert record.levelno == logging.ERROR and "RuntimeError" in record.getMessage()  # 예외 종류만 남는다
    assert record.exc_info is None
    assert OWNER.user_id not in caplog.text
    assert rig.hub.unsubscribed == rig.hub.subscribed  # 구독도 뺀다
    assert rig.ended == ["returned"]


def test_FR12B_닫기가_실패해도_예외를_올리지_않는다(monkeypatch):
    async def broken(self):
        raise RuntimeError("보내기 고장")

    monkeypatch.setattr(Subscription, "next_batch", broken)
    rig = Rig()
    rig.fail_close = True  # 1011 로 닫으려 할 때 이미 끊겨 있다

    async def main():
        async with rig.connect() as ws:
            await ws.send_json(AUTH)
            assert await ws.receive_json() == EMPTY
            await until(lambda: rig.ended)

    run(main())  # 앱이 예외를 올리면 WsClient 가 올린다
    assert rig.ended == ["returned"]
    assert rig.hub.unsubscribed == rig.hub.subscribed
