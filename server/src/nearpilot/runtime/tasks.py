"""조립의 실행 도구: 요청 접수 문, 주기 작업, 기기별 잠금, 늦은 콜백 연결."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

log = logging.getLogger(__name__)


class NotAccepting(RuntimeError):
    """요청 접수 전이거나 종료 중이다. MCP 도구는 이 예외를 내부 오류 거부로 바꾼다."""


class CallGate:
    """새 요청 접수를 열고 닫는다. 진행 중인 호출 수를 센다 (spec 6절 "종료와 부분 실패")."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._accepting = False
        self._active = 0

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    def open(self) -> None:
        with self._condition:
            self._accepting = True

    def close(self) -> None:
        with self._condition:
            self._accepting = False

    @contextmanager
    def enter(self) -> Iterator[None]:
        with self._condition:
            if not self._accepting:
                raise NotAccepting("서버가 요청을 받지 않는다")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._active == 0, timeout=max(0.0, timeout))


class PeriodicTask:
    """작업 하나를 자기 스레드에서 반복한다. 같은 작업의 호출은 겹치지 않는다.

    실패는 경고로 남기고 다음 호출을 예약한다. 실패를 성공이나 안전 확인으로 바꾸지 않는다.
    """

    def __init__(
        self, name: str, interval_sec: float, fn: Callable[[], Any], *,
        scope: Callable[[], AbstractContextManager[Any]] = nullcontext,
    ) -> None:
        self.name = name
        self.interval_sec = interval_sec
        self._fn = fn
        self._scope = scope
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"주기 작업을 이미 시작했다: {self.name}")
        self._thread = threading.Thread(target=self._loop, name=f"runtime-{self.name}", daemon=True)
        self._thread.start()

    @property
    def started(self) -> bool:
        return self._thread is not None

    def signal_stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float) -> bool:
        """멈췄으면 True 다. 제한 안에 끝나지 않은 호출을 강제로 끊지 않는다."""
        if self._thread is None:
            return True
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive()

    def run_once(self) -> bool:
        try:
            with self._scope():
                self._fn()
        except Exception as error:  # noqa: BLE001 - 다음 주기에 재시도한다
            log.warning("주기 작업 실패, 다음 주기에 재시도한다: %s (%s)", self.name, type(error).__name__)
            return False
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self.run_once()


class NodeLocks:
    """기기별 공통 재진입 잠금. 모든 상태 관리 서비스가 같은 객체를 쓴다 (NFR-04)."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def __call__(self, node_id: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(node_id)
            if lock is None:
                lock = self._locks[node_id] = threading.RLock()
            return lock


class Late:
    """순환 참조를 끊는 콜백. 작업 시작 전에 연결하고, 미연결 호출은 거부한다."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._target: Callable[..., Any] | None = None

    @property
    def bound(self) -> bool:
        return self._target is not None

    def bind(self, target: Callable[..., Any]) -> None:
        if self._target is not None:
            raise RuntimeError(f"콜백을 이미 연결했다: {self.name}")
        self._target = target

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._target is None:
            raise RuntimeError(f"연결하지 않은 콜백이다: {self.name}")
        return self._target(*args, **kwargs)
