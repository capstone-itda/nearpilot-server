"""FR-23 대기 구간. 실제 시간을 기다리지 않도록 wait 을 가짜로 바꾼다."""

from __future__ import annotations

import threading
import time

import pytest

from nearpilot.core.allocation import AllocationRequest, AllocationWindow, allocate


def req(request_id, **candidates) -> AllocationRequest:
    return AllocationRequest(request_id, candidates)


class Harness:
    """구간을 연 스레드는 gate 가 열릴 때까지 기다린다. 요청은 스레드로 보낸다."""

    def __init__(self, window_ms=500, allocate_fn=None, wait_error=None):
        self.gate = threading.Event()
        self.waits: list[float] = []
        self.batches: list[list[str]] = []
        self.results: dict[str, object] = {}
        self._threads: list[threading.Thread] = []
        self._allocate_fn = allocate_fn
        self._wait_error = wait_error

        def wait(seconds):
            self.waits.append(seconds)
            assert self.gate.wait(5), "gate 가 열리지 않았다"
            if self._wait_error:
                raise self._wait_error

        def allocate_fn_(requests):
            self.batches.append(sorted(r.request_id for r in requests))
            return (self._allocate_fn or allocate)(requests)

        self.window = AllocationWindow(window_ms, wait=wait, allocate_fn=allocate_fn_)

    def send(self, request: AllocationRequest, pending: int | None = None) -> None:
        def run():
            try:
                self.results[request.request_id] = self.window.submit(request)
            except Exception as exc:  # noqa: BLE001
                self.results[request.request_id] = exc

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._threads.append(t)
        if pending is not None:  # 구간에 들어올 때까지 기다린다
            deadline = time.monotonic() + 2
            while self.window.pending() < pending:
                assert time.monotonic() < deadline, "요청이 구간에 들어오지 않았다"
                time.sleep(0.002)

    def close_and_join(self) -> None:
        self.gate.set()
        for t in self._threads:
            t.join(5)
            assert not t.is_alive(), "요청이 깨어나지 않았다"


def test_FR23_구간_안의_요청은_한_묶음으로_배정한다():
    h = Harness()
    h.send(req("r1", n1=0.9), pending=1)
    h.send(req("r2", n1=0.8, n2=0.5), pending=2)
    h.close_and_join()
    assert h.batches == [["r1", "r2"]]  # 배정은 한 번이다
    assert h.results == {"r1": "n1", "r2": "n2"}


def test_FR23_구간이_닫힌_뒤의_요청은_새_구간을_연다():
    h = Harness()
    h.send(req("r1", n1=0.9), pending=1)
    h.close_and_join()
    assert h.window.pending() == 0
    h.send(req("r2", n1=0.8))
    h.close_and_join()
    assert h.batches == [["r1"], ["r2"]] and len(h.waits) == 2
    assert h.results == {"r1": "n1", "r2": "n1"}  # 새 구간이라 서로 경합하지 않는다


def test_FR23_설정한_대기_시간만큼_기다린다():
    h = Harness(window_ms=500)
    h.send(req("r1", n1=0.9))
    h.close_and_join()
    assert h.waits == [0.5]


def test_FR23_배정받지_못한_요청은_None이다():
    h = Harness()
    h.send(req("r1", n1=0.9), pending=1)
    h.send(req("r2", n1=0.8), pending=2)
    h.close_and_join()
    assert h.results == {"r1": "n1", "r2": None}


def test_FR23_배정_예외는_모든_요청에_전달한다():
    def boom(requests):
        raise ValueError("boom")

    h = Harness(allocate_fn=boom)
    h.send(req("r1", n1=0.9), pending=1)
    h.send(req("r2", n1=0.8), pending=2)
    h.close_and_join()
    assert all(isinstance(v, ValueError) for v in h.results.values()) and len(h.results) == 2


def test_FR23_대기_예외여도_모든_요청을_깨운다():
    h = Harness(wait_error=RuntimeError("wait failed"))
    h.send(req("r1", n1=0.9), pending=1)
    h.send(req("r2", n1=0.8), pending=2)
    h.close_and_join()  # 깨어나지 않으면 여기서 실패한다
    assert all(isinstance(v, RuntimeError) for v in h.results.values()) and len(h.results) == 2
    assert h.window.pending() == 0


def test_FR23_구간이_없으면_진단_값이_0이다():
    assert AllocationWindow(500).pending() == 0


def test_FR23_실제_시간으로도_한_묶음이다():
    window = AllocationWindow(50)
    results: dict[str, object] = {}

    def run(request):
        results[request.request_id] = window.submit(request)

    threads = [threading.Thread(target=run, args=(req("r1", n1=0.9, n2=0.2),)),
               threading.Thread(target=run, args=(req("r2", n1=0.8),))]
    threads[0].start()
    deadline = time.monotonic() + 2
    while window.pending() < 1:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    threads[1].start()
    for t in threads:
        t.join(5)
    # r1 이 n1 을 가져가면 r2 는 못 받는다. 배정 수 최대 기준이라 r1 이 n2 를 받는다
    assert results == {"r1": "n2", "r2": "n1"}
