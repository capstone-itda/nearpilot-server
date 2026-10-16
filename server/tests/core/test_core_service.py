"""FR-09 MCP 기능 여섯 개의 CORE 위임 객체. 기능 서비스는 fake 로 대신한다."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from nearpilot.core.decision.core_service import DelegatingCoreService
from nearpilot.shared.enums import Action, ReasonCode, Verdict
from nearpilot.shared.interfaces import CoreService
from nearpilot.shared.models import Decision, TargetSpec, UseRequest

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def req(user_id="u1", request_id="r1") -> UseRequest:
    return UseRequest(user_id, request_id, TargetSpec.parse("nearest:locker"), Action.OPEN, "claude", NOW)


class Recorder:
    """메서드마다 받은 인자를 기록하고 정해진 값을 돌려준다."""

    def __init__(self, **returns):
        self.calls: list[tuple] = []
        self.returns = returns
        self.error: Exception | None = None

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.error:
            raise self.error
        return self.returns[name]

    def request_use(self, req):
        return self._call("request_use", req)

    def request_access(self, user_id, node_id, action, request_id):
        return self._call("request_access", user_id, node_id, action, request_id)

    def find_available_devices(self, user_id):
        return self._call("find_available_devices", user_id)

    def get_device_state(self, user_id, node_id):
        return self._call("get_device_state", user_id, node_id)

    def release_use(self, user_id, use_id):
        return self._call("release_use", user_id, use_id)

    def get_use_status(self, user_id, use_id):
        return self._call("get_use_status", user_id, use_id)


def make(**returns):
    use, access, query, release = Recorder(**returns), Recorder(**returns), Recorder(**returns), Recorder(**returns)
    return DelegatingCoreService(use=use, access=access, query=query, release=release), use, access, query, release


def test_FR09_core_service_satisfies_protocol():
    svc, *_ = make()
    assert isinstance(svc, CoreService)


def test_FR09_core_service_delegates_six_methods():
    use_d = Decision("i1", "r1", Verdict.OK, ReasonCode.OK, node_id="locker-1", use_id="use-1")
    access_d = Decision("i2", "r2", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, node_id="locker-2")
    release_d = Decision("i3", "", Verdict.OK, ReasonCode.OK, node_id="locker-1", use_id="use-1")
    devices = [{"node_id": "locker-1"}]
    state = {"node_id": "locker-1", "state": "available"}
    status = {"use_id": "use-1", "state": "ACTIVE"}
    svc, use, access, query, release = make(
        request_use=use_d, request_access=access_d, release_use=release_d,
        find_available_devices=devices, get_device_state=state, get_use_status=status)
    r = req()

    assert svc.request_use(r) is use_d
    assert svc.request_access("u1", "locker-2", Action.OPEN, "r2") is access_d
    assert svc.find_available_devices("u1") is devices
    assert svc.get_device_state("u1", "locker-1") is state
    assert svc.release_use("u1", "use-1") is release_d
    assert svc.get_use_status("u1", "use-1") is status

    assert use.calls == [("request_use", r)]
    assert access.calls == [("request_access", "u1", "locker-2", Action.OPEN, "r2")]
    assert query.calls == [("find_available_devices", "u1"), ("get_device_state", "u1", "locker-1")]
    assert release.calls == [("release_use", "u1", "use-1"), ("get_use_status", "u1", "use-1")]


@pytest.mark.parametrize("decision", [
    Decision("i1", "r1", Verdict.DENY, ReasonCode.EXEC_UNKNOWN, node_id="locker-1"),
    Decision("i1", "r1", Verdict.DENY, ReasonCode.POLICY_DENIED),
    Decision("i1", "r1", Verdict.BUSY, ReasonCode.DEVICE_BUSY, node_id="locker-1"),
])
def test_FR18_core_service_keeps_decision(decision):
    svc, *_ = make(request_use=decision)
    assert svc.request_use(req()) is decision


def test_FR21_core_service_keeps_ownership_check():
    not_found = Decision("", "", Verdict.DENY, ReasonCode.USE_NOT_FOUND)
    status = {"reason_code": "USE_NOT_FOUND"}
    svc, _, _, _, release = make(release_use=not_found, get_use_status=status)

    assert svc.release_use("intruder", "use-of-owner") is not_found
    assert svc.get_use_status("intruder", "use-of-owner") is status
    # 소유권 판단에 쓰는 계정을 바꾸지 않고 넘긴다
    assert release.calls == [("release_use", "intruder", "use-of-owner"),
                             ("get_use_status", "intruder", "use-of-owner")]


def test_FR09_core_service_exception_propagates():
    svc, use, *_ = make()
    use.error = RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        svc.request_use(req())


def test_NFR03_core_service_concurrent_calls():
    """같은 객체로 동시에 부른다. 전역 잠금이 있으면 Barrier 가 풀리지 않는다."""
    n = 4
    barrier = threading.Barrier(n, timeout=5)

    class ConcurrentUse:
        def request_use(self, r):
            barrier.wait()
            return Decision(f"i-{r.request_id}", r.request_id, Verdict.OK, ReasonCode.OK, use_id=r.user_id)

    svc = DelegatingCoreService(use=ConcurrentUse(), access=Recorder(), query=Recorder(), release=Recorder())
    reqs = [req(user_id=f"u{i}", request_id=f"r{i}") for i in range(n)]
    with ThreadPoolExecutor(n) as pool:
        results = list(pool.map(svc.request_use, reqs))
    assert [(d.request_id, d.use_id) for d in results] == [(r.request_id, r.user_id) for r in reqs]
