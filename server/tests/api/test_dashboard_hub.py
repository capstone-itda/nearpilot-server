"""FR-12B 모니터링 허브. 어느 스레드에서 발행해도 모든 연결에 같은 순서로 가고, 발행자는 막히지도 예외를 받지도 않는다 (NFR-07)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from typing import Any

import pytest

from _dashboard import NOW, VISITOR, settings
from _dashboard import run as run_loop
from nearpilot.api.ws.hub import MonitorHub, Subscription
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import render_event
from nearpilot.shared.enums import Role
from nearpilot.shared.interfaces import MonitorFeed
from nearpilot.shared.models import MonitorEvent, Subject

HUB_LOGGER = "nearpilot.api.ws.hub"
ISO = "2026-10-23T14:03:05.120+09:00"  # NOW
THREADS, PER_THREAD = 3, 50


def run(coro, timeout: float = 10.0):
    """_dashboard.run 에 기한을 건다. 허브가 이벤트를 주지 않아도 테스트가 멈추지 않고 실패한다."""

    async def bounded():
        async with asyncio.timeout(timeout):
            return await coro

    return run_loop(bounded())


def result(request_id: str) -> MonitorEvent:
    """사람이 필요 없는 유효한 이벤트."""
    return MonitorEvent("mcp_result", NOW, {"requestId": request_id})


def beacon() -> MonitorEvent:
    """사람이 필요한 유효한 이벤트."""
    return MonitorEvent("beacon", NOW, {"seen": True}, VISITOR)


def ids(messages: list[dict[str, Any]]) -> list[str]:
    return [message["requestId"] for message in messages]


class SpyNames(PersonNames):
    """이름표를 달라고 한 Subject 를 순서대로 쌓는다."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[Subject] = []

    def person(self, subject):
        self.asked.append(subject)
        return super().person(subject)


class BrokenNames(PersonNames):
    """이름표가 예외를 던진다. 예외 메시지에 사용자 ID 가 있다."""

    def person(self, subject):
        raise RuntimeError(f"이름표가 고장났다: {subject.user_id}")


async def settle() -> None:
    """call_soon_threadsafe 로 넘어온 _offer 가 루프에서 돌도록 한 번 양보한다. 먼저 넘어온 것이 먼저 돈다."""
    await asyncio.sleep(0)


async def is_waiting(sub: Subscription) -> bool:
    """next_batch 가 기다리는지 본다 (대기열이 비었고 넘치지 않았다). 쌓인 것이 있으면 꺼내 버리므로 끝에서 쓴다."""
    task = asyncio.create_task(sub.next_batch())
    await settle()
    waiting = not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return waiting


async def collect(sub: Subscription, count: int, timeout: float = 5.0) -> list[dict[str, Any]]:
    """count 개를 받을 때까지 묶음을 모은다. 기한 안에 못 받거나 넘치면 실패한다."""
    got: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        while len(got) < count:
            batch = await sub.next_batch()
            assert batch is not None, "대기열이 넘쳤다"
            got.extend(batch)
    return got


def closed_subscription(hub: MonitorHub) -> Subscription:
    """루프가 닫힌 구독. 다른 스레드에서 asyncio.run 으로 구독하면 그 루프는 끝나면서 닫힌다."""

    async def subscribe() -> Subscription:
        return hub.subscribe()

    return asyncio.run(subscribe())


def test_FR12B_연결이_없으면_발행은_아무것도_하지_않는다(caplog):
    names = SpyNames()
    hub = MonitorHub(names, settings())

    hub.publish(beacon())  # 사람이 필요한 이벤트
    hub.publish(result("r1"))
    hub.publish(MonitorEvent("not_a_type", NOW, {}))  # 틀린 이벤트도 검사하지 않는다
    assert names.asked == []
    assert caplog.records == []

    async def main():
        sub = hub.subscribe()
        hub.publish(beacon())
        assert names.asked == [VISITOR]  # 연결이 있으면 이름표를 부른다
        hub.unsubscribe(sub)
        hub.publish(beacon())
        assert names.asked == [VISITOR]  # 마지막 연결이 나가면 다시 하지 않는다

    run(main())


def test_FR12B_다른_스레드의_발행이_모든_연결에_순서대로_간다():
    hub = MonitorHub(PersonNames(), settings(queue_max_events=1000))
    start = threading.Barrier(THREADS, timeout=5)
    errors: list[BaseException] = []

    def work(thread: int) -> None:
        try:
            start.wait()
            for n in range(PER_THREAD):
                hub.publish(result(f"t{thread}-{n}"))
        except BaseException as error:  # noqa: BLE001 - 발행자가 예외를 받으면 안 된다
            errors.append(error)

    async def main():
        subs = [hub.subscribe(), hub.subscribe()]
        threads = [threading.Thread(target=work, args=(t,)) for t in range(THREADS)]
        for thread in threads:
            thread.start()
        try:
            return await asyncio.gather(*(collect(sub, THREADS * PER_THREAD) for sub in subs))
        finally:
            for thread in threads:
                thread.join()

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 스레드가 자주 바뀌게 해서 잠금이 없으면 연결마다 순서가 달라지게 한다
    try:
        first, second = (ids(received) for received in run(main()))
    finally:
        sys.setswitchinterval(interval)
    assert errors == []
    assert first == second  # 두 연결이 같은 순서로 받았다
    assert sorted(first) == sorted(f"t{t}-{n}" for t in range(THREADS) for n in range(PER_THREAD))
    for t in range(THREADS):  # 스레드마다 번호 순서가 지켜진다
        assert [i for i in first if i.startswith(f"t{t}-")] == [f"t{t}-{n}" for n in range(PER_THREAD)]


def test_FR12B_형식이_틀린_이벤트는_버리고_발행자에게_예외를_올리지_않는다(caplog):
    caplog.set_level(logging.WARNING, logger=HUB_LOGGER)
    hub = MonitorHub(PersonNames(), settings())
    uid = VISITOR.user_id
    events = [
        result("ok-0"),
        MonitorEvent(f"secret-type-{uid}", NOW, {"requestId": "x"}),  # 알 수 없는 type
        result("ok-1"),
        MonitorEvent("mcp_call", NOW, {"tool": "request_use", "note": uid}, VISITOR),  # 필수 키 requestId 가 없다
        result("ok-2"),
        MonitorEvent("beacon", NOW, {"seen": True, "note": uid}),  # 사람이 필요한데 subject 가 없다
        result("ok-3"),
        MonitorEvent("step", NOW, {"requestId": "r1", "step": uid, "result": "pass"}),  # 값이 틀렸다
        result("ok-4"),
    ]

    async def main():
        sub = hub.subscribe()
        for event in events:
            hub.publish(event)  # 예외가 나오면 테스트가 실패한다
        await settle()
        received = await collect(sub, 5)
        assert await is_waiting(sub)  # 틀린 이벤트는 대기열에 없다
        return received

    assert ids(run(main())) == [f"ok-{n}" for n in range(5)]
    messages = [r.getMessage() for r in caplog.records if r.name == HUB_LOGGER and r.levelno == logging.WARNING]
    assert len(messages) == 4  # 틀린 이벤트마다 경고 하나
    assert "mcp_call" in messages[1] and "mcp_call.requestId" in messages[1]  # 알려진 type 과 오류 경로
    assert "beacon.subject" in messages[2]
    assert "step.step" in messages[3]
    assert uid not in caplog.text
    assert "secret-type" not in caplog.text  # 알 수 없는 type 값은 쓰지 않는다


def test_NFR07_발행자가_예외를_받지_않는다(caplog):
    caplog.set_level(logging.WARNING, logger=HUB_LOGGER)

    # (a) 이름표가 예외를 던진다
    hub = MonitorHub(BrokenNames(), settings())

    async def names_fail():
        sub = hub.subscribe()
        hub.publish(beacon())  # 이름표가 필요한 이벤트는 버린다
        hub.publish(result("after"))
        await settle()
        return await collect(sub, 1)

    assert ids(run(names_fail())) == ["after"]
    assert "RuntimeError" in caplog.text  # 예외 종류만 남는다
    assert VISITOR.user_id not in caplog.text

    # (b) 루프가 닫힌 구독이 살아 있는 구독 사이에 있다
    hub = MonitorHub(PersonNames(), settings())

    async def loop_closed():
        before = hub.subscribe()
        await asyncio.to_thread(closed_subscription, hub)
        after = hub.subscribe()
        hub.publish(result("r1"))
        hub.publish(result("r2"))
        await settle()
        return await collect(before, 2), await collect(after, 2)

    got_before, got_after = run(loop_closed())
    assert ids(got_before) == ids(got_after) == ["r1", "r2"]


def test_NFR07_허브_안의_예상하지_못한_예외도_로그만_남기고_돌아간다(caplog, monkeypatch):
    hub = MonitorHub(PersonNames(), settings())

    def broken(event):
        raise ValueError(f"고장: {VISITOR.user_id}")

    monkeypatch.setattr(hub, "render", broken)

    async def main():
        sub = hub.subscribe()
        hub.publish(result("r1"))  # 예외가 나오면 테스트가 실패한다
        await settle()
        assert await is_waiting(sub)

    run(main())
    [record] = [r for r in caplog.records if r.name == HUB_LOGGER]
    assert record.levelno == logging.ERROR
    assert "ValueError" in record.getMessage() and "mcp_result" in record.getMessage()
    assert record.exc_info is None  # 트레이스백에는 예외 메시지가 있다
    assert VISITOR.user_id not in caplog.text


def test_FR12B_묶음은_한도까지만_담고_나머지는_다음_묶음에_순서대로_담는다():
    hub = MonitorHub(PersonNames(), settings(batch_max_events=3))

    async def main():
        sub = hub.subscribe()
        for n in range(7):
            hub.publish(result(f"r{n}"))
        await settle()
        return [ids(await sub.next_batch()) for _ in range(3)]

    assert run(main()) == [["r0", "r1", "r2"], ["r3", "r4", "r5"], ["r6"]]


def test_FR12B_이벤트가_올_때까지_기다리고_다른_스레드의_발행이_깨운다():
    hub = MonitorHub(PersonNames(), settings(batch_max_events=3))

    def late_publish() -> None:
        time.sleep(0.05)  # 루프가 이벤트를 기다리며 쉬는 중에 발행한다
        hub.publish(result("late"))

    async def main():
        sub = hub.subscribe()
        waiter = asyncio.create_task(sub.next_batch())
        await settle()
        assert not waiter.done()  # 올 때까지 기다린다
        thread = threading.Thread(target=late_publish)
        thread.start()
        try:
            return ids(await asyncio.wait_for(waiter, 5.0))
        finally:
            thread.join()

    assert run(main()) == ["late"]  # 묶음을 채우려고 더 기다리지 않는다


def test_FR12B_대기열은_한도까지_쌓고_하나_더_오면_넘친다():
    hub = MonitorHub(PersonNames(), settings(queue_max_events=3, batch_max_events=10))

    async def main():
        sub = hub.subscribe()
        for n in range(3):
            hub.publish(result(f"r{n}"))
        await settle()
        assert not sub.overflowed  # 한도까지는 쌓는다
        assert ids(await sub.next_batch()) == ["r0", "r1", "r2"]
        for n in range(3, 6):  # 꺼낸 만큼 다시 쌓을 수 있다
            hub.publish(result(f"r{n}"))
        await settle()
        assert not sub.overflowed
        hub.publish(result("r6"))  # 한도까지 쌓인 대기열에 또 온다
        await settle()
        assert sub.overflowed
        assert await sub.next_batch() is None  # 쌓였던 것도 버렸다
        hub.publish(result("r7"))
        await settle()
        assert await sub.next_batch() is None  # 넘친 뒤에는 계속 None
        assert sub.overflowed

    run(main())


def test_FR12B_넘친_연결은_None을_받고_다른_연결은_계속_받는다():
    hub = MonitorHub(PersonNames(), settings(queue_max_events=3, batch_max_events=10))

    async def main():
        slow, fast = hub.subscribe(), hub.subscribe()
        received: list[str] = []
        for n in range(6):  # slow 는 한 번도 비우지 않는다
            hub.publish(result(f"r{n}"))
            await settle()
            received += ids(await fast.next_batch())
        assert slow.overflowed and not fast.overflowed
        assert await slow.next_batch() is None
        return received

    assert run(main()) == [f"r{n}" for n in range(6)]


def test_FR12B_unsubscribe_뒤에는_받지_않는다():
    hub = MonitorHub(PersonNames(), settings())
    other = MonitorHub(PersonNames(), settings())

    async def main():
        gone, kept, foreign = hub.subscribe(), hub.subscribe(), other.subscribe()
        hub.publish(result("r1"))
        await settle()
        hub.unsubscribe(gone)
        hub.publish(result("r2"))
        await settle()
        assert ids(await gone.next_batch()) == ["r1"]  # 빼기 전에 쌓인 것만 있다
        assert await is_waiting(gone)
        assert ids(await kept.next_batch()) == ["r1", "r2"]
        hub.unsubscribe(gone)  # 이미 뺀 구독은 무시한다
        hub.unsubscribe(foreign)  # 이 허브의 구독이 아니면 무시한다
        other.publish(result("r3"))
        await settle()
        assert ids(await foreign.next_batch()) == ["r3"]

    run(main())


def test_FR11_허브가_내보내는_이벤트에는_사용자_ID와_허용_목록_밖의_키가_없다():
    names = PersonNames()
    hub = MonitorHub(names, settings())
    event = MonitorEvent(
        "approval", NOW,
        {"requestId": "r1", "nodeId": "locker-1", "kind": "requested", "userId": VISITOR.user_id, "subject": VISITOR},
        VISITOR)

    async def main():
        sub = hub.subscribe()
        hub.publish(event)
        await settle()
        return await sub.next_batch()

    [message] = run(main())
    assert message == {"type": "approval", "at": ISO, "requestId": "r1", "nodeId": "locker-1", "kind": "requested",
                       "personId": "p1"}
    assert message == render_event(event, names)  # 이름표가 같아 조회와 같은 사람이다
    assert VISITOR.user_id not in json.dumps(message)


def test_FR12B_render는_틀린_이벤트에_None을_돌려주고_예외를_올리지_않는다(caplog):
    caplog.set_level(logging.WARNING, logger=HUB_LOGGER)
    names = PersonNames()
    names.person(Subject("user-first", Role.VISITOR))  # 조회가 먼저 이름을 붙였다
    hub = MonitorHub(names, settings())
    good = MonitorEvent("approval", NOW, {"requestId": "r1", "nodeId": "locker-1", "kind": "requested"}, VISITOR)

    message = hub.render(good)  # 연결이 없어도 바꾼다
    assert message == render_event(good, names)
    assert message["personId"] == "p2" == names.person(VISITOR).person_id  # 넘겨받은 이름표를 쓴다
    bad = [MonitorEvent("not_a_type", NOW, {}), MonitorEvent(123, NOW, {}), MonitorEvent("beacon", NOW, {"seen": True}),
           None, "text"]
    assert [hub.render(event) for event in bad] == [None] * 5
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 5


def test_FR12B_허브는_MonitorFeed이고_받은_이름표를_돌려준다():
    names = PersonNames()
    hub = MonitorHub(names, settings())
    assert isinstance(hub, MonitorFeed)
    assert hub.names is names


def test_FR12B_실행_중인_루프_밖에서는_구독할_수_없다():
    with pytest.raises(RuntimeError):
        MonitorHub(PersonNames(), settings()).subscribe()
