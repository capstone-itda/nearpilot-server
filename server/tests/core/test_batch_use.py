"""FR-09·FR-18·FR-22 묶음 요청의 실행 인계, 결과 묶음, 재전송 (specs/core-request-batch/spec.md).

`_batch_fakes` 의 `build_batch` 로 판정을 만들고 `RequestUseService(pipeline, FakeExecution, store)` 로 감싼다.
`FakeExecution` 이 하위 요청마다 정해 둔 결과를 내고, 실행한 하위 요청의 기록을 `FakeStore.executions` 에 남긴다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace

import pytest
from _batch_fakes import batch_request, batch_settings, build_batch, locker, world
from _selector_fakes import LIGHT_VALUES, FakeExecution, FakePolicy, Rejected

from nearpilot.core.decision.service import RequestUseService, aggregate_batch, aggregate_selector
from nearpilot.shared.enums import ExecutionStatus, ReasonCode, ReserveOutcome, Verdict
from nearpilot.shared.models import Decision, DeviceOutcome, PolicyCheckResult

# 조명 셋의 가까운 순서는 light-2, light-1, light-3 이다. `near:light:1:2` 의 하위 요청은 light-2, light-1 이다.
# 사물함 둘은 둘 다 임계값(0.4)을 넘는다. locker-1 이 더 가깝다. 스피커의 근접 지정은 임계값(0.5)을 넘는다.
LOCKER_VALUES = {"locker-1": -50.0, "locker-2": -70.0}
POSTERIORS = {"speaker-1": 0.9, "locker-1": 0.5, "locker-2": 0.45}
SIMILAR_LIGHT_VALUES = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0}  # light-1 과 light-3 이 비슷하다
SERVICE_LOGGER = "nearpilot.core.decision.service"


class Tamper:
    """판정이 낸 결과를 `fix` 로 바꿔 서비스에 넘긴다. 판정과 인계가 어긋났을 때 서비스의 반응을 본다."""

    def __init__(self, inner, fix):
        self.inner = inner
        self.fix = fix

    def decide_batch(self, req):
        return self.fix(self.inner.decide_batch(req))


def serve(results=None, barrier=None, after=None, policy=None, light_values=None, tamper=None, **settings):
    """조명 셋, 스피커 하나, 사물함 둘로 만든 서비스."""
    pipe, repo, prox, store, audit = build_batch(
        [*world(), locker("locker-1"), locker("locker-2")], {**(light_values or LIGHT_VALUES), **LOCKER_VALUES},
        POSTERIORS, policy, batch_settings(threshold_speaker=0.5, threshold_locker=0.4, **settings))
    exe = FakeExecution(store, results, barrier, after)
    return RequestUseService(pipe if tamper is None else Tamper(pipe, tamper), exe, store), repo, exe, store


def device(node_id, item, verdict=Verdict.OK, reason=ReasonCode.OK, use_id=None):
    return DeviceOutcome(node_id, verdict, reason, use_id, item=item)


def mixed():
    """거리 선택자, 근접 지정, 대여형 명시 지정 항목이 섞인 요청. 하위 요청은 c1(light-2), c2(light-1), c3(speaker-1), c4(locker-1)."""
    return batch_request(("near:light:1:2", "on"), ("nearest:speaker", "off"), ("locker-1", "open"))


def state(repo, exe, store):
    """새 하위 요청, 예약, 저장, 취소, 인계가 생겼는지 보는 값."""
    return (len(exe.contexts), list(repo.child_links), list(repo.reserved), list(repo.saved),
            list(store.cancels), dict(store.executions))


# ── 실행과 결과 묶음 ──────────────────────────────────────────────────


@pytest.mark.parametrize("items,devices", [
    pytest.param(
        (("near:light:1:all", "on"), ("speaker-1", "off")),
        [("light-2", 1, None), ("light-1", 1, None), ("light-3", 1, None), ("speaker-1", 2, None)],
        id="거리 선택자와 명시 지정"),
    pytest.param(
        (("far:light:1:1", "on"), ("nearest:speaker", "off"), ("locker-1", "open")),
        [("light-3", 1, None), ("speaker-1", 2, None), ("locker-1", 3, "use-locker-1")],
        id="거리 선택자, 근접 지정, 대여형 명시 지정"),
])
def test_FR09_batch_mixed_kinds_runs_all(items, devices):
    svc, repo, exe, store = serve()
    d = svc.request_use(batch_request(*items))
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.devices == tuple(device(node_id, item, use_id=use_id) for node_id, item, use_id in devices)
    assert (d.selected_count, d.similar) == (len(devices), False)
    assert (d.node_id, d.use_id, d.p_max, d.message) == (None, None, None, None)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")  # 호스트가 받는 ID 는 상위 요청의 것이다
    # 인계는 하위 요청마다 한 번이고, context 는 하위 요청의 것이다. 동작은 항목의 것이다
    assert sorted((c.internal_request_id, c.node_id) for c in exe.contexts) == [
        (f"c{n}", node_id) for n, (node_id, _, _) in enumerate(devices, start=1)]
    assert {c.node_id: c.request.action for c in exe.contexts} == {
        node_id: items[item - 1][1] for node_id, item, _ in devices}
    assert [store.executions[f"c{n}"].status for n in range(1, len(devices) + 1)] == [
        ExecutionStatus.SUCCEEDED] * len(devices)
    assert store.cancels == []


@pytest.mark.parametrize("items,count", [
    pytest.param((("near:light:1:1", "on"), ("speaker-1", "off")), 2, id="항목마다 기기 하나"),
    pytest.param((("near:light:1:2", "on"), ("speaker-1", "off")), 3, id="거리 선택자 항목이 기기 둘"),
    pytest.param((("light-1", "on"), ("light-2", "on"), ("nearest:speaker", "off")), 3, id="명시 지정과 근접 지정"),
])
def test_FR18_batch_children_run_concurrently(items, count):
    # 모든 하위 요청의 execute 가 함께 시작해야 배리어를 넘는다. 차례로 실행하면 대기 시간이 지나 실패한다
    barrier = threading.Barrier(count, timeout=10)
    svc, repo, exe, store = serve(barrier=barrier)
    d = svc.request_use(batch_request(*items))
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert len(d.devices) == len(exe.contexts) == count
    assert all(o.verdict is Verdict.OK for o in d.devices)


def test_FR18_batch_partial_exec_failure_reported(caplog):
    # 대여형 셋째 하위 요청이 실패한다. 앞 기기의 OK 와 use_id 는 그대로고, 실패한 기기에는 use_id 가 없다
    svc, repo, exe, store = serve({"locker-2": ExecutionStatus.FAILED})
    d = svc.request_use(batch_request(("near:light:1:1", "on"), ("locker-1", "open"), ("near:locker:2:1", "open")))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.EXEC_FAILED)
    assert d.devices == (device("light-2", 1), device("locker-1", 2, use_id="use-locker-1"),
                         device("locker-2", 3, Verdict.DENY, ReasonCode.EXEC_FAILED))
    assert (d.selected_count, d.similar) == (3, False)
    assert (d.node_id, d.use_id) == (None, None)
    assert store.cancels == []  # 이미 실행한 기기를 되돌리지 않는다
    assert [store.executions[c].status for c in ("c1", "c2", "c3")] == [
        ExecutionStatus.SUCCEEDED, ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED]

    # 한 기기의 execute 가 예외를 던지면 그 기기만 INTERNAL_ERROR 이고 다른 기기는 그대로다
    svc, repo, exe, store = serve({"light-1": RuntimeError("secret internal detail")})
    with caplog.at_level(logging.ERROR, logger=SERVICE_LOGGER):
        d = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == (device("light-2", 1), device("light-1", 1, Verdict.DENY, ReasonCode.INTERNAL_ERROR),
                         device("speaker-1", 2))
    assert d.message and "secret" not in repr(d)
    assert store.cancels == [] and store.executions["c1"].status is ExecutionStatus.SUCCEEDED
    assert any("secret" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)  # 로그에는 남는다


def test_FR18_batch_failure_does_not_stop_other_children():
    svc, repo, exe, store = serve({"light-2": ExecutionStatus.FAILED})  # 첫 항목의 첫 기기가 실패한다
    d = svc.request_use(batch_request(("near:light:1:all", "on"), ("speaker-1", "off")))
    assert {c.node_id for c in exe.contexts} == {"light-2", "light-1", "light-3", "speaker-1"}  # 뒤 기기도 실행한다
    assert d.devices == (device("light-2", 1, Verdict.DENY, ReasonCode.EXEC_FAILED), device("light-1", 1),
                         device("light-3", 1), device("speaker-1", 2))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.EXEC_FAILED)
    assert store.cancels == []


def test_FR18_batch_first_failure_in_item_order_decides_parent():
    # 둘째 항목의 기기가 먼저 끝나도 상위 판정은 항목 순서에서 처음 실패한 기기의 것이다
    svc, repo, exe, store = serve(
        {"light-2": ExecutionStatus.FAILED, "speaker-1": ExecutionStatus.UNKNOWN}, after={"light-2": "speaker-1"})
    d = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.EXEC_FAILED)
    assert d.devices == (device("light-2", 1, Verdict.DENY, ReasonCode.EXEC_FAILED), device("light-1", 1),
                         device("speaker-1", 2, Verdict.DENY, ReasonCode.EXEC_UNKNOWN))


@pytest.mark.parametrize("failed", ["light-2", "speaker-1"])
def test_FR18_batch_recheck_rejection_blocks_only_that_child(failed):
    svc, repo, exe, store = serve({failed: Rejected(Verdict.ASK, ReasonCode.MOVE_CLOSER)})
    d = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    rejected = {"light-2": 1, "speaker-1": 2}[failed]
    assert d.devices == tuple(
        device(node_id, item, Verdict.ASK, ReasonCode.MOVE_CLOSER) if node_id == failed else device(node_id, item)
        for node_id, item in (("light-2", 1), ("light-1", 1), ("speaker-1", 2)))
    assert [o.item for o in d.devices if o.verdict is Verdict.ASK] == [rejected]
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.MOVE_CLOSER)
    assert len(exe.contexts) == 3  # 다른 하위 요청도 실행한다
    assert store.cancels == []  # 이미 인계한 하위 요청의 예약을 되돌리지 않는다
    assert sorted(r.status.value for r in store.executions.values()) == ["not_sent", "succeeded", "succeeded"]


@pytest.mark.parametrize("status,verdict,reason", [
    (ExecutionStatus.REJECTED, Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    (ExecutionStatus.UNKNOWN, Verdict.DENY, ReasonCode.EXEC_UNKNOWN),
    (ExecutionStatus.PENDING, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    (ExecutionStatus.IN_PROGRESS, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    (ExecutionStatus.NOT_SENT, Verdict.DENY, ReasonCode.INTERNAL_ERROR),  # 재검사 판정도 사유도 없는 미전송
])
def test_FR18_batch_child_result_maps_like_single_request(status, verdict, reason):
    svc, repo, exe, store = serve({"locker-2": status})
    d = svc.request_use(batch_request(("locker-1", "open"), ("near:locker:2:1", "open")))
    assert (d.verdict, d.reason) == (verdict, reason)
    assert d.devices == (device("locker-1", 1, use_id="use-locker-1"), device("locker-2", 2, verdict, reason))


@pytest.mark.parametrize("case", [
    "beacon_not_seen", "no_candidate", "device_busy", "duplicate_device", "over_max_items"])
def test_FR09_batch_non_ok_decision_returns_without_execution(case):
    items = (("near:light:1:2", "on"), ("speaker-1", "off"))
    settings = {}
    if case == "beacon_not_seen":
        expected = (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    elif case == "no_candidate":
        items = (("speaker-1", "off"), ("near:light:4:1", "on"))  # 둘째 항목이 고를 기기가 없다
        expected = (Verdict.DENY, ReasonCode.NO_CANDIDATE)
    elif case == "device_busy":
        expected = (Verdict.BUSY, ReasonCode.DEVICE_BUSY)
    elif case == "duplicate_device":
        items = (("light-2", "on"), ("near:light:1:1", "off"))  # 둘째 항목이 첫 항목의 기기를 다시 고른다
        expected = (Verdict.DENY, ReasonCode.INVALID_INPUT)
    else:
        settings = {"batch_max_items": 1}
        expected = (Verdict.DENY, ReasonCode.INVALID_INPUT)
    svc, repo, exe, store = serve(**settings)
    if case == "beacon_not_seen":
        repo.frames = []
    elif case == "device_busy":
        repo.reserve_results = {"speaker-1": ReserveOutcome.DEVICE_BUSY}
    d = svc.request_use(batch_request(*items))
    assert (d.verdict, d.reason) == expected
    assert exe.contexts == [] and store.executions == {}  # 인계하지 않는다
    assert (d.use_id, d.node_id) == (None, None)


def test_FR18_batch_unexpected_error_is_internal_error_without_devices(monkeypatch):
    # execute 밖에서 예상 못 한 예외가 나면 기기별 결과를 믿지 않고 상위 판정이 INTERNAL_ERROR 다
    svc, repo, exe, store = serve()

    def broken(child, result):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(svc, "_from_result", broken)
    d = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == () and d.selected_count == 3 and d.similar is False
    assert (d.node_id, d.use_id) == (None, None)
    assert d.message and "secret" not in repr(d)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")


@pytest.mark.parametrize("tamper", [
    pytest.param(lambda sd: replace(sd, contexts=sd.contexts[:-1]), id="인계 context 가 하나 적다"),
    pytest.param(lambda sd: replace(sd, decision=replace(sd.decision, devices=sd.decision.devices[:-1])),
                 id="상위 devices 가 하나 적다"),
    pytest.param(lambda sd: replace(sd, decision=replace(
        sd.decision, devices=tuple(replace(o, item=None) for o in sd.decision.devices))), id="devices 에 항목 순서가 없다"),
])
def test_FR18_batch_decision_mismatch_is_internal_error(tamper):
    # 하위 판정, 인계 context, 상위 devices 의 개수가 어긋나면 일부만 보고 OK 가 되지 않는다
    svc, repo, exe, store = serve(tamper=tamper)
    d = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == () and d.selected_count == 3 and d.similar is False
    assert (d.node_id, d.use_id) == (None, None)


# ── 재전송 (FR-22) ────────────────────────────────────────────────────


def test_FR22_batch_resend_replays_stored_execution_status():
    svc, repo, exe, store = serve()
    first = svc.request_use(mixed())
    before = state(repo, exe, store)
    again = svc.request_use(mixed())
    assert state(repo, exe, store) == before  # execute 를 더 부르지 않고, 새 하위 요청도 예약도 만들지 않는다
    assert again == first
    assert again.devices == (device("light-2", 1), device("light-1", 1), device("speaker-1", 2),
                             device("locker-1", 3, use_id="use-locker-1"))
    assert (again.verdict, again.selected_count, again.similar) == (Verdict.OK, 4, False)

    # 하위 하나의 저장된 실행 상태가 IN_PROGRESS 면 그 기기는 BUSY·COMMAND_IN_PROGRESS 이고 상위도 그 판정이다
    store.executions["c3"] = replace(store.executions["c3"], status=ExecutionStatus.IN_PROGRESS)
    busy = svc.request_use(mixed())
    assert (busy.verdict, busy.reason) == (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS)
    assert busy.devices == (device("light-2", 1), device("light-1", 1),
                            device("speaker-1", 2, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
                            device("locker-1", 3, use_id="use-locker-1"))
    assert (busy.selected_count, busy.similar) == (4, False)
    assert (busy.node_id, busy.use_id) == (None, None)
    assert len(exe.contexts) == 4  # 여전히 새 명령은 없다


def test_FR22_batch_resend_returns_flags():
    # 비슷함 표시는 첫 판정에서 온다
    svc, repo, exe, store = serve(light_values=SIMILAR_LIGHT_VALUES)
    first = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert (first.similar, first.selected_count) == (True, 3)
    assert svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off"))) == first


@pytest.mark.parametrize("results", [
    pytest.param({"locker-2": ExecutionStatus.FAILED}, id="둘째 실행 실패"),
    pytest.param({"locker-2": ExecutionStatus.UNKNOWN}, id="둘째 실행 불명"),
    pytest.param({"locker-2": ExecutionStatus.REJECTED}, id="둘째 기기 거부"),
    pytest.param({"locker-2": Rejected(Verdict.ASK, ReasonCode.MOVE_CLOSER)}, id="둘째 재검사 거부"),
    pytest.param({"locker-1": Rejected(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)}, id="첫째 재검사 거부"),
])
def test_FR22_batch_resend_matches_first_response_per_device(results):
    svc, repo, exe, store = serve(results)
    first = svc.request_use(batch_request(("locker-1", "open"), ("near:locker:2:1", "open")))
    again = svc.request_use(batch_request(("locker-1", "open"), ("near:locker:2:1", "open")))
    assert first.verdict is not Verdict.OK
    assert again == first  # 판정, 기기별 결과와 항목 순서, 개수, 표시가 첫 응답과 같다
    assert len(exe.contexts) == 2 and store.cancels == []


def test_FR22_batch_resend_after_check_failure_executes_nothing():
    policy = FakePolicy({"speaker-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    svc, repo, exe, store = serve(policy=policy)
    first = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))  # 둘째 항목이 승인이 필요하다
    assert (first.verdict, first.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)
    assert first.devices == (device("speaker-1", 2, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert exe.contexts == []  # 아무것도 실행하지 않았다
    before = state(repo, exe, store)
    again = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert again == first
    assert state(repo, exe, store) == before  # execute 를 부르지 않는다. 새 하위 요청도 예약도 없다


def test_FR22_batch_resend_lookup_error_is_internal_error(monkeypatch):
    svc, repo, exe, store = serve(light_values=SIMILAR_LIGHT_VALUES)
    assert svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off"))).similar is True

    def broken(internal_request_id):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(store, "execution", broken)
    d = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.devices == () and d.selected_count == 3 and d.similar is True
    assert (d.node_id, d.use_id) == (None, None)
    assert d.message and "secret" not in repr(d)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")
    assert len(exe.contexts) == 3  # 다시 실행하지 않는다


def test_FR22_batch_resend_without_execution_record_is_in_progress():
    svc, repo, exe, store = serve()
    svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    del store.executions["c3"]  # 스피커 하위 요청의 인계가 끝나지 않았다
    d = svc.request_use(batch_request(("near:light:1:2", "on"), ("speaker-1", "off")))
    assert (d.verdict, d.reason) == (Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS)
    assert d.devices == (device("light-2", 1), device("light-1", 1),
                         device("speaker-1", 2, Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS))
    assert len(exe.contexts) == 3  # context 를 다시 만들 수 없으므로 인계하지 않는다


# ── 응답에 내부 값이 없다 (FR-11) ─────────────────────────────────────


def test_FR11_batch_response_hides_user_and_child_ids():
    svc, repo, exe, store = serve()
    first = svc.request_use(mixed())
    again = svc.request_use(mixed())
    children = [link.child for link in repo.child_links]
    assert children == ["c1", "c2", "c3", "c4"]
    for d in (first, again):
        text = repr(d)
        assert "visitor" not in text
        assert all(f"'{child}'" not in text for child in children)


# ── 결과 묶음 ─────────────────────────────────────────────────────────

PARENT = Decision("i1", "r1", Verdict.OK, ReasonCode.OK, devices=(device("judged", 1, use_id="u0"),),
                  selected_count=3, similar=True)


def child(node_id, verdict=Verdict.OK, reason=ReasonCode.OK, **kw):
    return Decision(f"c-{node_id}", "r1", verdict, reason, node_id=node_id, **kw)


def test_FR09_aggregate_batch_all_ok():
    d = aggregate_batch(PARENT, [child("a", use_id="u-a", p_max=0.9), child("b"), child("c")], [1, 1, 2])
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.devices == (device("a", 1, use_id="u-a"), device("b", 1), device("c", 2))  # 판정 때의 devices 가 아니다
    assert (d.selected_count, d.similar) == (3, True)
    assert (d.node_id, d.use_id, d.p_max, d.message) == (None, None, None, None)
    assert (d.internal_request_id, d.request_id) == ("i1", "r1")


def test_FR09_aggregate_batch_first_failure_in_item_order_decides():
    outcomes = [
        child("a", use_id="u-a"),
        child("b", Verdict.BUSY, ReasonCode.DEVICE_BUSY, use_id="leaked", p_max=0.7),
        child("c", Verdict.DENY, ReasonCode.EXEC_FAILED),
        child("d"),
    ]
    d = aggregate_batch(PARENT, outcomes, [1, 1, 2, 3])
    assert (d.verdict, d.reason) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY)  # 처음 OK 가 아닌 결과
    assert d.devices == (
        device("a", 1, use_id="u-a"),
        device("b", 1, Verdict.BUSY, ReasonCode.DEVICE_BUSY),  # OK 가 아닌 결과에는 use_id 를 두지 않는다
        device("c", 2, Verdict.DENY, ReasonCode.EXEC_FAILED),
        device("d", 3),
    )
    assert (d.selected_count, d.similar) == (3, True)
    assert (d.node_id, d.use_id, d.p_max) == (None, None, None)


def test_FR09_aggregate_batch_internal_error_has_generic_message():
    d = aggregate_batch(PARENT, [child("a", Verdict.DENY, ReasonCode.INTERNAL_ERROR), child("b")], [1, 2])
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.message  # 내부 오류에는 일반 안내 문구가 붙는다
    assert aggregate_batch(PARENT, [child("a")], [1]).message is None


def test_FR09_aggregate_batch_matches_selector_except_item():
    outcomes = [child("a", use_id="u-a"), child("b", Verdict.BUSY, ReasonCode.DEVICE_BUSY, use_id="leaked"), child("c")]
    batch = aggregate_batch(PARENT, outcomes, [1, 1, 2])
    assert replace(batch, devices=tuple(replace(o, item=None) for o in batch.devices)) == aggregate_selector(
        PARENT, outcomes)


@pytest.mark.parametrize("outcomes,items", [
    pytest.param(["a", "b"], [1], id="항목 순서가 적다"),
    pytest.param(["a"], [1, 2], id="항목 순서가 많다"),
])
def test_FR09_aggregate_batch_length_mismatch_raises(outcomes, items):
    with pytest.raises(ValueError):
        aggregate_batch(PARENT, [child(node_id) for node_id in outcomes], items)
