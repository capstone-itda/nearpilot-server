"""FR-12B 모니터링 허브. 어느 스레드에서 발행해도 형식으로 바꿔 연결마다 대기열에 넣는다 (NFR-07).

대기열은 연결이 쓰는 이벤트 루프 안에서만 만진다. 다른 스레드는 `call_soon_threadsafe` 로 넘기기만 한다.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Any

from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import EVENT_TYPES, SchemaError, render_event
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.shared.models import MonitorEvent

log = logging.getLogger(__name__)


def _known_type(event: object) -> str:
    """로그에 쓸 이벤트 type. 알려진 type 만 쓴다 — 아닌 값은 임의 문자열이라 사용자 ID 가 들어 있을 수 있다."""
    type_ = getattr(event, "type", None)
    return type_ if isinstance(type_, str) and type_ in EVENT_TYPES else "(알 수 없음)"


class Subscription:
    """연결 하나의 대기열. 허브만 만든다.

    `next_batch` 와 `_offer` 는 만든 이벤트 루프 안에서만 부른다. 메시지 dict 는 모든 구독이 나눠 쓰므로 바꾸지 않는다.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, settings: DashboardSettings) -> None:
        self._loop = loop
        self._queue_max = settings.queue_max_events
        self._batch_max = settings.batch_max_events
        self._pending: deque[dict[str, Any]] = deque()
        self._overflowed = False
        self._wake = asyncio.Event()  # 이벤트가 쌓이거나 넘치면 기다리는 next_batch 를 깨운다

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    async def next_batch(self) -> list[dict[str, Any]] | None:
        """이벤트가 올 때까지 기다렸다가 batch_max_events 개까지 묶어 돌려준다. 넘쳤으면 None."""
        while not self._pending and not self._overflowed:
            self._wake.clear()
            await self._wake.wait()
        if self._overflowed:
            return None
        return [self._pending.popleft() for _ in range(min(self._batch_max, len(self._pending)))]

    def _offer(self, msg: dict[str, Any]) -> None:
        """허브가 `call_soon_threadsafe` 로 넘긴다 (루프 스레드). 한도까지 쌓인 대기열에 또 오면 넘친 것으로 하고 버린다."""
        if self._overflowed:
            return
        if len(self._pending) >= self._queue_max:
            self._overflowed = True
            self._pending.clear()
        else:
            self._pending.append(msg)
        self._wake.set()


class MonitorHub:  # MonitorFeed 를 구현한다
    """이벤트를 만드는 영역이 `publish` 로 넘기면 형식으로 바꿔 모든 구독의 대기열에 넣는다 (FR-12B)."""

    def __init__(self, names: PersonNames, settings: DashboardSettings) -> None:
        self._names = names
        self._settings = settings
        self._subs: list[Subscription] = []  # append 와 remove 는 원자적이라 잠금 없이 바꾼다
        self._lock = threading.Lock()  # 발행 순서를 지킨다. 모든 구독에 넣는 동안 잡는다

    @property
    def names(self) -> PersonNames:
        return self._names

    def publish(self, event: MonitorEvent) -> None:
        """어느 스레드에서 불러도 막지 않고 예외를 올리지 않는다 (NFR-07). 형식이 틀린 이벤트는 버린다."""
        try:
            if not self._subs:  # 연결이 없으면 이름표도 붙이지 않는다
                return
            msg = self.render(event)
            if msg is None:
                return
            with self._lock:  # 잠근 채 모든 구독에 넣어야 구독마다 순서가 같다
                for sub in list(self._subs):
                    try:
                        sub._loop.call_soon_threadsafe(sub._offer, msg)
                    except RuntimeError:  # 루프가 닫혔다. 그 구독만 건너뛴다
                        continue
        except Exception as error:  # noqa: BLE001 - 모니터링의 실패는 판정과 실행을 바꾸지 않는다. 예외 종류만 남긴다
            log.error("모니터링 이벤트 발행 중 예외: type=%s 예외=%s", _known_type(event), type(error).__name__)

    def render(self, event: MonitorEvent) -> dict[str, Any] | None:
        """형식으로 바꾼다. 틀리면 로그를 남기고 None. 예외를 올리지 않는다 (지금 상태를 보낼 때도 쓴다)."""
        try:
            return render_event(event, self._names)
        except SchemaError as error:  # 메시지는 경로뿐이다
            log.warning("모니터링 이벤트를 버렸다: type=%s 경로=%s", _known_type(event), error)
        except Exception as error:  # noqa: BLE001 - 값과 사용자 ID 가 새지 않게 예외 종류만 남긴다
            log.warning("모니터링 이벤트를 버렸다: type=%s 예외=%s", _known_type(event), type(error).__name__)
        return None

    def subscribe(self) -> Subscription:
        """실행 중인 이벤트 루프 안에서 부른다. 그 루프에 대기열을 만든다."""
        subscription = Subscription(asyncio.get_running_loop(), self._settings)
        self._subs.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """목록에서 뺀다. 없는 구독이면 무시한다."""
        try:
            self._subs.remove(subscription)
        except ValueError:
            pass
