"""FR-23 대기 구간: `any:locker` 요청을 한 구간에 모아서 한 번에 배정한다.

첫 요청이 구간을 열고 `window_ms` 뒤에 닫는다 (고정 구간). 그 안에 들어온 요청이 한 묶음이다.
구간을 연 요청의 스레드가 기다리고 배정한다. 타이머 스레드는 없다. 나머지 요청은 이벤트를 기다린다.
어떤 예외가 나도 구간을 연 스레드는 묶음의 모든 요청을 깨운다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from nearpilot.core.allocation.allocator import AllocationRequest, allocate


class _Batch:
    def __init__(self) -> None:
        self.requests: list[AllocationRequest] = []
        self.done = threading.Event()
        self.result: dict[str, str] = {}
        self.error: BaseException | None = None


class AllocationWindow:
    def __init__(
        self,
        window_ms: int,
        wait: Callable[[float], None] = time.sleep,
        allocate_fn: Callable[[Sequence[AllocationRequest]], dict[str, str]] = allocate,
    ) -> None:
        self._window_sec = window_ms / 1000
        self._wait = wait
        self._allocate = allocate_fn
        self._lock = threading.Lock()
        self._open: _Batch | None = None

    def pending(self) -> int:
        """지금 열린 구간에서 기다리는 요청 수. 진단과 테스트용이다."""
        with self._lock:
            return len(self._open.requests) if self._open is not None else 0

    def submit(self, request: AllocationRequest) -> str | None:
        """구간이 닫힐 때까지 기다린 뒤 배정한 기기 ID를 반환한다. 배정받지 못하면 None 이다."""
        with self._lock:
            batch = self._open
            leader = batch is None
            if batch is None:
                batch = self._open = _Batch()
            batch.requests.append(request)
        if leader:
            self._run(batch)
        else:
            batch.done.wait()
        if batch.error is not None:
            raise batch.error
        return batch.result.get(request.request_id)

    def _run(self, batch: _Batch) -> None:
        requests: list[AllocationRequest] = []
        try:
            try:
                self._wait(self._window_sec)
            finally:
                with self._lock:  # 구간을 닫는다. 이후 요청은 새 구간을 연다
                    self._open = None
                    requests = list(batch.requests)
            batch.result = dict(self._allocate(requests))
        except BaseException as exc:
            batch.error = exc
        finally:
            batch.done.set()
