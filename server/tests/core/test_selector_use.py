"""FR-09·FR-18·FR-22 거리 선택자의 실행 인계, 결과 묶음, 재전송 (specs/core-distance-selector/spec.md).

`_selector_fakes` 의 `build` 로 판정을 만들고 `RequestUseService(pipeline, FakeExecution, store)` 로 감싼다.
`FakeExecution` 이 하위 요청마다 정해 둔 결과를 내고, 실행한 하위 요청의 기록을 `FakeStore.executions` 에 남긴다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace

import pytest
from _selector_fakes import (
    LIGHT_VALUES,
    FakeExecution,
    FakePolicy,
    Rejected,
    build,
    lights,
    node,
    request,
)

from nearpilot.core.decision.service import RequestUseService, aggregate_selector
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import ExecutionStatus, Occupancy, ReasonCode, ReserveOutcome, Verdict
from nearpilot.shared.models import Decision, DeviceOutcome, PolicyCheckResult

# 조명 세 대의 가까운 순서는 light-2, light-1, light-3 이다. `near:light:1:2` 의 하위 요청은 c1(light-2), c2(light-1).
# 사물함 두 대는 둘 다 임계값을 넘는다. locker-1 이 더 가깝다.
LOCKER_VALUES = {"locker-1": -50.0, "locker-2": -70.0}
LOCKER_POSTERIORS = {"locker-1": 0.5, "locker-2": 0.45}
LOCKER_SETTINGS = Settings(version="t", threshold_locker=0.4)
SERVICE_LOGGER = "nearpilot.core.decision.service"


def lockers():
    return [node(node_id, "locker", Occupancy.RENTAL) for node_id in LOCKER_VALUES]


def serve(results=None, barrier=None, delays=None, policy=None, values=None):
    """조명 세 대로 만든 서비스."""
    pipe, repo, prox, store, audit = build(lights(), values or LIGHT_VALUES, policy=policy)
    exe = FakeExecution(store, results, barrier, delays)
    return RequestUseService(pipe, exe, store), repo, exe, store


def serve_lockers(results=None):
    """사물함 두 대로 만든 서비스. 요청은 `action="open"` 이다."""
    pipe, repo, prox, store, audit = build(
        lockers(), LOCKER_VALUES, LOCKER_POSTERIORS, settings=LOCKER_SETTINGS)
    exe = FakeExecution(store, results)
    return RequestUseService(pipe, exe, store), repo, exe, store


def device(node_id, verdict=Verdict.OK, reason=ReasonCode.OK, use_id=None):
    return DeviceOutcome(node_id, verdict, reason, use_id)


# ── 실행과 결과 묶음 ──────────────────────────────────────────────────


@pytest.mark.parametrize("target,picked", [
    pytest.param("near:light:1:2", [("c1", "light-2"), ("c2", "light-1")], id="near"),
    pytest.param("far:light:1:2", [("c1", "light-3"), ("c2", "light-1")], id="far"),
    pytest.param("near:light:1:1", [("c1", "light-2")], id="하위 요청 하나"),
])
def test_FR18_selector_all_children_succeed_returns_ok(target, picked):
    svc, repo, exe, store = serve()
    d = svc.request_use(request(target))
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.devices == tuple(device(node_id) for _, node_id in picked)
    assert (d.selected_count, d.similar) == (len(picked), False)
    assert (d.node_id, d.use_id, d.p_max, d.message) == (None, None, None, None)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")  # 호스트가 받는 ID 는 상위 요청의 것이다
    # 인계는 하위 요청마다 한 번이고, context 는 하위 요청의 것이다
    assert sorted((c.internal_request_id, c.node_id) for c in exe.contexts) == picked
    assert store.cancels == []


def test_FR18_selector_rental_use_id_only_on_ok_devices():
    svc, repo, exe, store = serve_lockers()
    d = svc.request_use(request("near:locker:1:2", action="open"))
    assert d.verdict is Verdict.OK and d.use_id is None  # 상위의 use_id 는 None 이다
    assert d.devices == (device("locker-1", use_id="use-locker-1"), device("locker-2", use_id="use-locker-2"))
    svc, repo, exe, store = serve_lockers({"locker-2": ExecutionStatus.FAILED})
    d = svc.request_use(request("near:locker:1:2", action="open"))
    assert d.use_id is None
    assert d.devices == (device("locker-1", use_id="use-locker-1"),
                         device("locker-2", Verdict.DENY, ReasonCode.EXEC_FAILED))  # 실패한 기기에는 use_id 가 없다


def test_FR18_selector_partial_exec_failure_reported(caplog):
    svc, repo, exe, store = serve({"light-1": ExecutionStatus.FAILED})  # 둘째 기기가 실패한다
    d = svc.request_use(request("near:light:1:2"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.EXEC_FAILED)
    assert d.devices == (device("light-2"), device("light-1", Verdict.DENY, ReasonCode.EXEC_FAILED))
    assert (d.selected_count, d.similar) == (2, False)
    assert (d.node_id, d.use_id) == (None, None)
    assert store.cancels == []  # 이미 실행한 첫 기기를 되돌리지 않는다
    assert store.executions["c1"].status is ExecutionStatus.SUCCEEDED

    # 둘째의 execute 가 예외를 던지면 둘째만 INTERNAL_ERROR 이고 첫째는 그대로다
    svc, repo, exe, store = serve({"light-1": RuntimeError("secret internal detail")})
    with caplog.at_level(logging.ERROR, logger=SERVICE_LOGGER):
        d = svc.request_use(request("near:light:1:2"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == (device("light-2"), device("light-1", Verdict.DENY, ReasonCode.INTERNAL_ERROR))
    assert d.message and "secret" not in repr(d)
    assert store.cancels == [] and store.executions["c1"].status is ExecutionStatus.SUCCEEDED
    assert any("secret" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)  # 로그에는 남는다


def test_FR18_selector_failure_does_not_stop_other_children():
    svc, repo, exe, store = serve({"light-2": ExecutionStatus.FAILED})  # 가장 가까운 기기가 실패한다
    d = svc.request_use(request("near:light:1:all"))
    assert {c.node_id for c in exe.contexts} == {"light-2", "light-1", "light-3"}  # 뒤 기기도 실행한다
    assert d.devices == (device("light-2", Verdict.DENY, ReasonCode.EXEC_FAILED), device("light-1"), device("light-3"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.EXEC_FAILED)
    assert store.cancels == []
    assert [store.executions[c].status for c in ("c1", "c2", "c3")] == [
        ExecutionStatus.FAILED, ExecutionStatus.SUCCEEDED, ExecutionStatus.SUCCEEDED]


def test_FR18_selector_first_failure_in_distance_order_decides_parent():
    svc, repo, exe, store = serve({"light-2": ExecutionStatus.UNKNOWN, "light-1": ExecutionStatus.FAILED})
    d = svc.request_use(request("near:light:1:2"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.EXEC_UNKNOWN)  # 거리 순서에서 처음 실패한 기기
    assert d.devices == (device("light-2", Verdict.DENY, ReasonCode.EXEC_UNKNOWN),
                         device("light-1", Verdict.DENY, ReasonCode.EXEC_FAILED))


@pytest.mark.parametrize("failed", ["light-2", "light-1"])
def test_FR18_selector_recheck_rejection_blocks_only_that_child(failed):
    svc, repo, exe, store = serve({failed: Rejected(Verdict.ASK, ReasonCode.MOVE_CLOSER)})
    d = svc.request_use(request("near:light:1:2"))
    rejected = device(failed, Verdict.ASK, ReasonCode.MOVE_CLOSER)
    assert d.devices == ((rejected, device("light-1")) if failed == "light-2" else (device("light-2"), rejected))
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.MOVE_CLOSER)
    assert len(exe.contexts) == 2  # 다른 하위 요청도 실행한다
    assert store.cancels == []  # 이미 인계한 하위 요청의 예약을 되돌리지 않는다
    assert sorted(r.status.value for r in store.executions.values()) == ["not_sent", "succeeded"]


@pytest.mark.parametrize("status,verdict,reason", [
    (ExecutionStatus.REJECTED, Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    (ExecutionStatus.UNKNOWN, Verdict.DENY, ReasonCode.EXEC_UNKNOWN),
    (ExecutionStatus.PENDING, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    (ExecutionStatus.IN_PROGRESS, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    (ExecutionStatus.NOT_SENT, Verdict.DENY, ReasonCode.INTERNAL_ERROR),  # 재검사 판정도 사유도 없는 미전송
])
def test_FR18_selector_child_result_maps_like_single_request(status, verdict, reason):
    svc, repo, exe, store = serve_lockers({"locker-2": status})
    d = svc.request_use(request("near:locker:1:2", action="open"))
    assert (d.verdict, d.reason) == (verdict, reason)
    assert d.devices == (device("locker-1", use_id="use-locker-1"), device("locker-2", verdict, reason))


def test_FR18_selector_results_keep_distance_order_when_completion_differs():
    # 첫 기기가 늦게 끝나도 기기별 결과는 완료 순서가 아니라 거리 순서에 맞는다
    svc, repo, exe, store = serve({"light-2": ExecutionStatus.FAILED}, delays={"light-2": 0.1})
    d = svc.request_use(request("near:light:1:2"))
    assert d.devices == (device("light-2", Verdict.DENY, ReasonCode.EXEC_FAILED), device("light-1"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.EXEC_FAILED)


@pytest.mark.parametrize("target,count", [("near:light:1:2", 2), ("near:light:1:all", 3)])
def test_FR18_selector_children_run_concurrently(target, count):
    # 모든 하위 요청의 execute 가 함께 시작해야 배리어를 넘는다. 차례로 실행하면 대기 시간이 지나 실패한다
    barrier = threading.Barrier(count, timeout=2)
    svc, repo, exe, store = serve(barrier=barrier)
    d = svc.request_use(request(target))
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert [o.node_id for o in d.devices] == ["light-2", "light-1", "light-3"][:count]
    assert all(o.verdict is Verdict.OK for o in d.devices)
    assert len(exe.contexts) == count


@pytest.mark.parametrize("case", ["beacon_not_seen", "no_candidate", "device_busy"])
def test_FR09_selector_non_ok_decision_returns_without_execution(case):
    svc, repo, exe, store = serve()
    target = "near:light:1:2"
    if case == "beacon_not_seen":
        repo.frames = []
        expected = (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    elif case == "no_candidate":
        target = "near:light:4:1"  # 시작이 순서의 끝 뒤다
        expected = (Verdict.DENY, ReasonCode.NO_CANDIDATE)
    else:
        repo.reserve_results = {"light-1": ReserveOutcome.DEVICE_BUSY}
        expected = (Verdict.BUSY, ReasonCode.DEVICE_BUSY)
    d = svc.request_use(request(target))
    assert (d.verdict, d.reason) == expected
    assert exe.contexts == [] and store.executions == {}  # 인계하지 않는다
    assert d.selected_count is not None and d.use_id is None and d.node_id is None


def test_FR18_selector_unexpected_error_is_internal_error_without_devices(monkeypatch):
    # execute 밖에서 예상 못 한 예외가 나면 기기별 결과를 믿지 않고 상위 판정이 INTERNAL_ERROR 다
    svc, repo, exe, store = serve()

    def broken(child, result):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(svc, "_from_result", broken)
    d = svc.request_use(request("near:light:1:2"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == () and d.selected_count == 2 and d.similar is False
    assert (d.node_id, d.use_id) == (None, None)
    assert d.message and "secret" not in repr(d)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")


def test_FR18_selector_context_count_mismatch_is_internal_error():
    # 하위 판정보다 인계 context 가 적으면 일부만 보고 OK 가 되지 않는다
    class DropLastContext:
        def __init__(self, inner):
            self.inner = inner

        def decide_selector(self, req):
            sd = self.inner.decide_selector(req)
            return replace(sd, contexts=sd.contexts[:-1])

    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    svc = RequestUseService(DropLastContext(pipe), FakeExecution(store), store)
    d = svc.request_use(request("near:light:1:2"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == () and d.selected_count == 2 and d.similar is False
    assert (d.node_id, d.use_id) == (None, None)


# ── 재전송 (FR-22) ────────────────────────────────────────────────────


def test_FR22_selector_resend_returns_children_results():
    svc, repo, exe, store = serve()
    first = svc.request_use(request("near:light:1:2"))
    before = (len(exe.contexts), list(repo.child_links), list(repo.reserved), list(repo.saved),
              list(store.cancels), dict(store.executions))
    again = svc.request_use(request("near:light:1:2"))
    after = (len(exe.contexts), list(repo.child_links), list(repo.reserved), list(repo.saved),
             list(store.cancels), dict(store.executions))
    assert after == before  # execute 를 더 부르지 않고, 새 하위 요청도 예약도 만들지 않는다
    assert again == first
    assert again.devices == (device("light-2"), device("light-1"))
    assert (again.verdict, again.selected_count, again.similar) == (Verdict.OK, 2, False)

    # 하위 하나의 저장된 실행 상태가 IN_PROGRESS 면 그 기기는 BUSY·COMMAND_IN_PROGRESS 이고 상위도 그 판정이다
    store.executions["c2"] = replace(store.executions["c2"], status=ExecutionStatus.IN_PROGRESS)
    busy = svc.request_use(request("near:light:1:2"))
    assert (busy.verdict, busy.reason) == (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS)
    assert busy.devices == (device("light-2"), device("light-1", Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS))
    assert (busy.selected_count, busy.similar) == (2, False)
    assert (busy.node_id, busy.use_id) == (None, None)
    assert len(exe.contexts) == 2  # 여전히 새 명령은 없다


def test_FR22_selector_resend_returns_use_ids_and_flags():
    # 재전송의 use_id 는 저장된 실행 기록에서, 비슷함 표시는 첫 판정에서 온다
    values = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0}  # light-1 과 light-3 이 비슷하다
    svc, repo, exe, store = serve(values=values)
    first = svc.request_use(request("near:light:1:2"))
    assert first.similar is True
    assert svc.request_use(request("near:light:1:2")) == first
    svc, repo, exe, store = serve_lockers()
    first = svc.request_use(request("near:locker:1:2", action="open"))
    again = svc.request_use(request("near:locker:1:2", action="open"))
    assert again == first
    assert again.devices == (device("locker-1", use_id="use-locker-1"), device("locker-2", use_id="use-locker-2"))


@pytest.mark.parametrize("results", [
    pytest.param({"locker-2": ExecutionStatus.FAILED}, id="둘째 실행 실패"),
    pytest.param({"locker-2": ExecutionStatus.UNKNOWN}, id="둘째 실행 불명"),
    pytest.param({"locker-2": ExecutionStatus.REJECTED}, id="둘째 기기 거부"),
    pytest.param({"locker-2": Rejected(Verdict.ASK, ReasonCode.MOVE_CLOSER)}, id="둘째 재검사 거부"),
    pytest.param({"locker-1": Rejected(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)},
                 id="첫째 재검사 거부"),
])
def test_FR22_selector_resend_matches_first_response_per_device(results):
    svc, repo, exe, store = serve_lockers(results)
    first = svc.request_use(request("near:locker:1:2", action="open"))
    again = svc.request_use(request("near:locker:1:2", action="open"))
    assert first.verdict is not Verdict.OK
    assert again == first  # 판정, 기기별 결과, 개수, 표시가 첫 응답과 같다
    assert len(exe.contexts) == 2 and store.cancels == []


def test_FR22_selector_resend_after_check_failure():
    policy = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    svc, repo, exe, store = serve(policy=policy)
    first = svc.request_use(request("near:light:1:2"))  # 둘째 기기가 승인이 필요하다
    assert (first.verdict, first.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)
    assert first.devices == (device("light-1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert exe.contexts == []  # 아무것도 실행하지 않았다
    before = (list(repo.child_links), list(repo.reserved), list(repo.saved), list(store.cancels))
    again = svc.request_use(request("near:light:1:2"))
    assert again == first
    assert again.devices == (device("light-1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert exe.contexts == []  # execute 를 부르지 않는다
    assert (list(repo.child_links), list(repo.reserved), list(repo.saved), list(store.cancels)) == before


def test_FR22_selector_resend_lookup_error_is_internal_error(monkeypatch):
    values = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0}  # light-1 과 light-3 이 비슷하다
    svc, repo, exe, store = serve(values=values)
    assert svc.request_use(request("near:light:1:2")).similar is True

    def broken(internal_request_id):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(store, "execution", broken)
    d = svc.request_use(request("near:light:1:2"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == () and d.selected_count == 2 and d.similar is True
    assert (d.node_id, d.use_id) == (None, None)
    assert d.message and "secret" not in repr(d)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")
    assert len(exe.contexts) == 2  # 다시 실행하지 않는다


def test_FR22_selector_resend_without_execution_record_is_in_progress():
    svc, repo, exe, store = serve()
    svc.request_use(request("near:light:1:2"))
    del store.executions["c2"]  # 둘째 하위 요청의 인계가 끝나지 않았다
    d = svc.request_use(request("near:light:1:2"))
    assert (d.verdict, d.reason) == (Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS)
    assert d.devices == (device("light-2"), device("light-1", Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS))
    assert len(exe.contexts) == 2  # context 를 다시 만들 수 없으므로 인계하지 않는다


# ── 응답에 내부 값이 없다 (FR-11) ─────────────────────────────────────


def test_FR11_selector_response_hides_user_and_child_ids():
    svc, repo, exe, store = serve()
    first = svc.request_use(request("near:light:1:2"))
    again = svc.request_use(request("near:light:1:2"))
    children = [link.child for link in repo.child_links]
    assert children == ["c1", "c2"]
    for d in (first, again):
        text = repr(d)
        assert "visitor" not in text
        assert all(f"'{child}'" not in text for child in children)


# ── 결과 묶음 ─────────────────────────────────────────────────────────

PARENT = Decision("i1", "r1", Verdict.OK, ReasonCode.OK, devices=(device("judged", use_id="u0"),),
                  selected_count=3, similar=True)


def child(node_id, verdict=Verdict.OK, reason=ReasonCode.OK, **kw):
    return Decision(f"c-{node_id}", "r1", verdict, reason, node_id=node_id, **kw)


def test_FR09_aggregate_selector_all_ok():
    d = aggregate_selector(PARENT, [child("a", use_id="u-a", p_max=0.9), child("b")])
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.devices == (device("a", use_id="u-a"), device("b"))  # 판정 때의 devices 가 아니라 결과의 devices 다
    assert (d.selected_count, d.similar) == (3, True)
    assert (d.node_id, d.use_id, d.p_max, d.message) == (None, None, None, None)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")


def test_FR09_aggregate_selector_first_failure_decides():
    outcomes = [
        child("a", use_id="u-a"),
        child("b", Verdict.BUSY, ReasonCode.DEVICE_BUSY, use_id="leaked", p_max=0.7),
        child("c", Verdict.DENY, ReasonCode.EXEC_FAILED),
        child("d"),
    ]
    d = aggregate_selector(PARENT, outcomes)
    assert (d.verdict, d.reason) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY)  # 처음 OK 가 아닌 결과
    assert d.devices == (
        device("a", use_id="u-a"),
        device("b", Verdict.BUSY, ReasonCode.DEVICE_BUSY),  # OK 가 아닌 결과에는 use_id 를 두지 않는다
        device("c", Verdict.DENY, ReasonCode.EXEC_FAILED),
        device("d"),
    )
    assert (d.selected_count, d.similar) == (3, True)
    assert (d.node_id, d.use_id, d.p_max) == (None, None, None)


def test_FR09_aggregate_selector_internal_error_has_generic_message():
    d = aggregate_selector(PARENT, [child("a", Verdict.DENY, ReasonCode.INTERNAL_ERROR), child("b")])
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.message  # 내부 오류에는 일반 안내 문구가 붙는다
    assert aggregate_selector(PARENT, [child("a")]).message is None
