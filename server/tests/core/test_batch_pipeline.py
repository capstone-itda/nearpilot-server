"""FR-09·FR-18·FR-22 묶음 요청 판정 (specs/core-request-batch/spec.md).

묶음 요청을 상위 요청 하나와 항목마다 고른 기기의 하위 요청으로 판정한다. 다른 계층은 `_batch_fakes` 의 fake 로 대신한다.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from _batch_fakes import (
    FakeWindow,
    batch_request,
    batch_settings,
    build_batch,
    dimmable_lights,
    locker,
    speaker,
    world,
)
from _selector_fakes import LIGHT_VALUES, FakePolicy, request

from nearpilot.core.decision.digest import input_digest
from nearpilot.core.decision.pipeline import boundary_probability
from nearpilot.shared.enums import ExecutionKind, ExecutionStatus, ReasonCode, Step, Verdict
from nearpilot.shared.frozen import thaw
from nearpilot.shared.models import (
    BatchItemRecord,
    BatchRecord,
    Decision,
    DeviceOutcome,
    PolicyCheckResult,
    SelectorRecord,
)

PARENT = "i1"  # FakeRepo 가 처음 연결한 요청의 내부 ID
BATCH_SIBLING = "batch_sibling"

# 가까운 순서는 light-2, light-1, light-3 이다. 끝 경계(light-1, light-3)가 2 dB 차이라 `near:light:1:2` 는 "비슷함"이다
SIMILAR_LIGHT_VALUES = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0}


def device(node_id, item, verdict=Verdict.OK, reason=ReasonCode.OK, use_id=None):
    return DeviceOutcome(node_id, verdict, reason, use_id, item=item)


def child_ids(repo):
    return [link.child for link in repo.child_links]


def events(audit, internal_request_id, step=None):
    return [e for e in audit.events
            if e.internal_request_id == internal_request_id and (step is None or e.step is step)]


def sibling_cancel(child, reason, node_id):
    """앞 하위 요청의 예약을 걸린 판정 때문에 미전송으로 취소한 기록 하나."""
    return (child, ExecutionStatus.PENDING, reason,
            {"source": BATCH_SIBLING, "reason": reason.value, "node_id": node_id})


def snapshot(repo, store, audit):
    """새 하위 요청, 예약, 저장, 취소, 감사가 생겼는지 보는 값."""
    return (list(repo.child_links), list(repo.reserved), list(repo.saved), list(store.cancels),
            list(audit.events), repo.calls.count("link_selector_child"))


class ExplodingPolicy(FakePolicy):
    """정해 둔 기기의 정책 검사에서 예외를 던진다."""

    def __init__(self, node_id):
        super().__init__()
        self.node_id = node_id

    def check(self, user_id, node_id, action, now, **kw):
        if node_id == self.node_id:
            raise RuntimeError("secret internal detail")
        return super().check(user_id, node_id, action, now, **kw)


# ── 항목 수 (FR-09) ───────────────────────────────────────────────────


def test_FR09_batch_over_max_items_denied():
    items = (("light-1", "on"), ("light-2", "on"), ("speaker-1", "off"))
    pipe, repo, prox, store, audit = build_batch(settings=batch_settings(batch_max_items=2))
    sd = pipe.decide_batch(batch_request(*items))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert (d.node_id, d.use_id, d.p_max) == (None, None, None)
    assert sd.children == () and sd.contexts == ()
    # 하위 요청을 만들기 전에 거부한다. 비콘도 후보도 보지 않는다
    assert repo.child_links == [] and "link_selector_child" not in repo.calls
    assert "beacons_of" not in repo.calls and repo.reserved == [] and store.cancels == []
    # 기록은 요청의 항목을 모두 담는다. 하위 요청이 없어 항목마다 받은 요청뿐이다
    assert repo.decisions[PARENT] == d
    assert [(i.target_spec, i.action, i.children) for i in repo.records[PARENT].batch.items] == [
        ("light-1", "on", ()), ("light-2", "on", ()), ("speaker-1", "off", ())]
    last = events(audit, PARENT)[-1]
    assert last.step is Step.CANDIDATE
    assert thaw(last.payload) == {"verdict": "DENY", "reason": "INVALID_INPUT", "settings_version": "t",
                                  "children": 0, "items": 3, "batch_max_items": 2}

    # 상한은 설정에서 읽는다. 항목 수가 상한과 같으면 판정한다
    pipe, repo, *_ = build_batch(settings=batch_settings(batch_max_items=3))
    ok = pipe.decide_batch(batch_request(*items))
    assert (ok.decision.verdict, ok.decision.reason) == (Verdict.OK, ReasonCode.OK)
    assert len(repo.child_links) == 3


def test_FR22_batch_capacity_exceeded_denied_and_saved():
    pipe, repo, prox, store, audit = build_batch()
    repo.capacity_exceeded = True
    sd = pipe.decide_batch(batch_request(("light-1", "on"), ("speaker-1", "off")))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED)
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert sd.children == () and sd.contexts == ()
    assert repo.decisions[PARENT] == d
    assert [(i.target_spec, i.action, i.children) for i in repo.records[PARENT].batch.items] == [
        ("light-1", "on", ()), ("speaker-1", "off", ())]  # 기록은 요청의 항목을 모두 담는다
    assert "beacons_of" not in repo.calls and repo.child_links == []
    last = events(audit, PARENT)[-1]
    assert last.step is Step.DEDUP and last.payload["capacity_exceeded"] is True


# ── 같은 기기를 두 번 고른다 (FR-09) ───────────────────────────────────


@pytest.mark.parametrize("items,duplicate,earlier,p_max", [
    pytest.param((("light-1", "on"), ("light-1", "off")), "light-1", ["c1"], None, id="명시 지정 둘"),
    pytest.param((("near:light:1:all", "on"), ("light-2", "off")), "light-2", ["c1", "c2", "c3"], None,
                 id="거리 선택자와 명시 지정"),
    pytest.param((("light-1", "on"), ("light-1", "open")), "light-1", ["c1"], None,
                 id="동작 검사보다 먼저 거부한다"),  # 조명은 open 을 선언하지 않았다. 동작을 먼저 보면 UNSUPPORTED_ACTION 이다
    pytest.param((("light-2", "on"), ("nearest:light", "off")), "light-2", ["c1"], 0.8, id="명시 지정과 근접 지정"),
    pytest.param((("near:light:1:2", "on"), ("far:light:1:2", "off")), "light-1", ["c1", "c2", "c3"], None,
                 id="거리 선택자 둘"),
])
def test_FR09_batch_duplicate_device_denied(items, duplicate, earlier, p_max):
    pipe, repo, prox, store, audit = build_batch(posteriors={"light-2": 0.8})
    sd = pipe.decide_batch(batch_request(*items))
    d = sd.decision
    failed = f"c{len(earlier) + 1}"  # 같은 기기를 다시 고른 뒤 항목의 하위 요청
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert d.devices == (device(duplicate, 2, Verdict.DENY, ReasonCode.INVALID_INPUT),)
    assert d.selected_count == len(earlier) + 1 and d.node_id is None
    assert sd.children == () and sd.contexts == ()
    assert child_ids(repo) == [*earlier, failed]
    # 거부한 하위 요청은 예약하지 않는다. 앞 하위 요청의 예약은 모두 취소한다
    assert [r.internal_request_id for r in repo.reserved] == earlier
    assert store.cancels == [sibling_cancel(c, ReasonCode.INVALID_INPUT, duplicate) for c in earlier]
    for child in earlier:
        assert repo.decisions[child].verdict is Verdict.OK  # 앞 하위 요청의 판정 기록은 OK 로 남는다
    rejected = repo.decisions[failed]
    assert (rejected.verdict, rejected.reason, rejected.node_id, rejected.p_max) == (
        Verdict.DENY, ReasonCode.INVALID_INPUT, duplicate, p_max)
    # 감사는 ② 후보 단계에 같은 기기를 다시 골랐음을 남긴다
    assert {"duplicate": duplicate} in [thaw(e.payload) for e in events(audit, failed, Step.CANDIDATE)]
    last = events(audit, failed)[-1]
    assert last.step is Step.CANDIDATE
    assert last.payload == {"verdict": "DENY", "reason": "INVALID_INPUT", "settings_version": "t",
                            "batch_parent": PARENT}
    parent = events(audit, PARENT)[-1]
    assert parent.step is Step.CANDIDATE and parent.payload["children"] == len(earlier) + 1


def test_FR09_batch_any_item_duplicate_denied_after_assignment():
    window = FakeWindow("locker-2")  # 대기 구간이 앞 항목과 같은 기기를 배정한다
    pipe, repo, prox, store, audit = build_batch(
        [locker("locker-1"), locker("locker-2")], {}, {"locker-1": 0.5, "locker-2": 0.45},
        settings=batch_settings(threshold_locker=0.4), window=window)
    sd = pipe.decide_batch(batch_request(("locker-2", "open"), ("any:locker", "open")))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert d.devices == (device("locker-2", 2, Verdict.DENY, ReasonCode.INVALID_INPUT),)
    assert len(window.requests) == 1  # 전역 할당 항목은 배정 뒤에 검사한다
    assert [r.internal_request_id for r in repo.reserved] == ["c1"]
    assert store.cancels == [sibling_cancel("c1", ReasonCode.INVALID_INPUT, "locker-2")]
    assert repo.decisions["c2"].p_max == 0.45


def test_FR09_batch_any_item_policy_stop_reports_device():
    """전역 할당 항목이 ④ 에서 걸리면 걸린 기기의 하위 요청을 만들어 `devices` 에 낸다 (spec 결정 32)."""
    policy = FakePolicy({nid: PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)
                         for nid in ("locker-1", "locker-2")})
    pipe, repo, prox, store, audit = build_batch(
        [*dimmable_lights(), locker("locker-1"), locker("locker-2")], {}, {"locker-1": 0.5, "locker-2": 0.45},
        policy=policy, settings=batch_settings(threshold_locker=0.4), window=FakeWindow("locker-1"))
    sd = pipe.decide_batch(batch_request(("light-1", "on"), ("any:locker", "open")))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)
    assert d.devices == (device("locker-1", 2, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert d.selected_count == 2
    assert child_ids(repo) == ["c1", "c2"]
    saved = repo.decisions["c2"]
    assert (saved.internal_request_id, saved.node_id, saved.verdict) == ("c2", "locker-1", Verdict.NEED_APPROVAL)
    assert [r.internal_request_id for r in repo.reserved] == ["c1"]  # 걸린 하위 요청은 예약하지 않는다
    assert store.cancels == [sibling_cancel("c1", ReasonCode.APPROVAL_REQUIRED, "locker-1")]
    assert [item.nodes for item in repo.records[PARENT].batch.items] == [("light-1",), ("locker-1",)]


# ── 거리 선택자 항목이 하위 요청 전에 멈춘다 (FR-09) ────────────────────


@pytest.mark.parametrize("case", ["no_candidate", "order_uncertain", "no_candidate_of_kind"])
def test_FR09_batch_selector_item_stops_batch(case):
    settings, values = batch_settings(), LIGHT_VALUES
    if case == "no_candidate":  # 시작이 순서의 끝 뒤라 고를 기기가 없다. 거리 순서는 만들었다
        second = "near:light:4:1"
        reason, verdict, step, similar, selected = ReasonCode.NO_CANDIDATE, Verdict.DENY, Step.PROXIMITY, False, ()
    elif case == "order_uncertain":  # 끝 경계(light-1, light-2)가 1 dB 차이라 경계 확률이 기준(0.9)보다 낮다
        second = "near:light:1:1"
        settings = batch_settings(order_boundary_min_prob=0.9)
        values = {"light-1": -60.0, "light-2": -61.0, "light-3": -62.0}
        reason, verdict, step, similar, selected = (
            ReasonCode.ORDER_UNCERTAIN, Verdict.ASK, Step.PROXIMITY, True, ("light-1",))
    else:  # 요청 종류(램프)의 후보가 없다. 거리 순서를 만들기 전에 멈춘다
        second = "near:lamp:1:1"
        reason, verdict, step, similar, selected = ReasonCode.NO_CANDIDATE, Verdict.DENY, Step.CANDIDATE, False, None
    pipe, repo, prox, store, audit = build_batch(values=values, settings=settings)
    sd = pipe.decide_batch(batch_request(("speaker-1", "off"), (second, "on")))
    d = sd.decision
    assert (d.verdict, d.reason) == (verdict, reason)
    assert d.devices == () and d.node_id is None
    assert d.selected_count == 1 and d.similar is similar  # 고른 기기는 첫 항목의 하나다. 멈춘 항목은 0을 더하고, 비슷함은 그 항목의 값도 더한다
    assert sd.children == () and sd.contexts == ()
    assert child_ids(repo) == ["c1"]
    assert store.cancels == [sibling_cancel("c1", reason, None)]  # 첫 항목의 예약을 취소한다
    assert repo.decisions[PARENT] == d
    # 상위 기록은 두 항목을 담는다. 둘째는 하위 요청이 없고, 거리 순서를 만들었으면 그 기록이 있다
    first, second_item = repo.records[PARENT].batch.items
    assert (first.target_spec, first.children, first.selector) == ("speaker-1", ("c1",), None)
    assert (second_item.target_spec, second_item.action, second_item.children) == (second, "on", ())
    if selected is None:
        assert second_item.selector is None and prox.order_calls == []
    else:
        assert second_item.selector.selected == selected and second_item.selector.children == ()
        assert second_item.selector.similar is similar
    last = events(audit, PARENT)[-1]
    assert last.step is step
    assert last.payload["children"] == 1 and last.payload["reason"] == reason.value


@pytest.mark.parametrize("light_close,speaker_close,expected", [
    pytest.param(True, False, True, id="앞 항목만 비슷"),
    pytest.param(False, True, True, id="뒤 항목만 비슷"),
    pytest.param(False, False, False, id="둘 다 아님"),
])
def test_FR09_batch_similar_from_any_selector_item(light_close, speaker_close, expected):
    values = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0 if light_close else -80.0,
              "speaker-1": -50.0, "speaker-2": -52.0 if speaker_close else -80.0}
    pipe, repo, *_ = build_batch([*world(), speaker("speaker-2")], values)
    sd = pipe.decide_batch(batch_request(("near:light:1:2", "on"), ("near:speaker:1:1", "off")))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.similar is expected
    assert repo.decisions[PARENT].similar is expected
    # 항목마다 거리 선택자 기록이 자기 비슷함을 담는다
    lights_item, speakers_item = repo.records[PARENT].batch.items
    assert (lights_item.selector.similar, speakers_item.selector.similar) == (light_close, speaker_close)


def test_FR09_batch_selected_count_counts_selector_item_fully():
    """`selected_count` 는 항목마다 실제로 고른 기기 수의 합이다 (docs/requirements.md FR-09, spec 결정 11).

    거리 선택자 항목은 중간 기기에서 걸려도 고른 수 전체를 센다. 같은 대상을 거리 선택자 요청 하나로 보낸 값과 같고,
    만든 하위 요청 수가 아니다. 하위 요청 전에 멈춘 항목은 0을 더하고 판정하지 않은 뒤 항목은 세지 않는다. 재전송도 같다.
    """

    def alone(target, **kw):  # 같은 정책의 거리 선택자 요청 하나가 낸 고른 수
        pipe, *_ = build_batch(**kw)
        return pipe.decide_selector(request(target)).decision.selected_count

    # ④ 에서 거리 순서의 둘째 기기(light-1)가 걸린다. 하위 요청은 light-2, light-1 둘뿐이지만 고른 기기는 셋이다
    policy = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    assert alone("near:light:1:3", policy=policy) == 3
    pipe, repo, *_ = build_batch(policy=policy)
    req = batch_request(("near:light:1:3", "on"))
    d = pipe.decide_batch(req).decision
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)
    assert d.devices == (device("light-1", 1, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert child_ids(repo) == ["c1", "c2"]
    assert d.selected_count == 3
    assert repo.records[PARENT].batch.items[0].selector.selected == ("light-2", "light-1", "light-3")  # 기록한 고른 기기와 같다
    again = pipe.decide_batch(req)
    assert again.decision == d and again.decision.selected_count == 3  # 재전송도 같다

    # 항목마다 더한다. 앞 항목은 고른 수만큼, 걸린 항목은 고른 수 전체, 판정하지 않은 뒤 항목은 0이다
    pipe, repo, *_ = build_batch([*world(), locker("locker-1")], policy=policy)
    req = batch_request(("speaker-1", "off"), ("near:light:1:3", "on"), ("locker-1", "open"))
    d = pipe.decide_batch(req).decision
    assert d.devices == (device("light-1", 2, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert child_ids(repo) == ["c1", "c2", "c3"]  # speaker-1, light-2, light-1
    assert d.selected_count == 1 + 3 + 0
    assert pipe.decide_batch(req).decision.selected_count == 4

    # 같은 기기 검사에서 거리 선택자 항목의 첫 기기가 걸려도 그 항목이 고른 수 전체를 센다
    pipe, repo, *_ = build_batch()
    req = batch_request(("light-2", "on"), ("near:light:1:2", "off"))
    d = pipe.decide_batch(req).decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert child_ids(repo) == ["c1", "c2"]  # 둘째 항목은 하위 요청을 하나만 만들었다
    assert (d.selected_count, alone("near:light:1:2")) == (1 + 2, 2)
    assert pipe.decide_batch(req).decision.selected_count == 3

    # 경계 순서가 애매해 하위 요청 없이 멈춘 항목은 0을 더한다. 거리 선택자 요청 하나도 0이다
    uncertain = {"values": {"light-1": -60.0, "light-2": -61.0, "light-3": -62.0},
                 "settings": batch_settings(order_boundary_min_prob=0.9)}  # 끝 경계의 값 차이가 1 dB 라 경계 확률이 기준보다 낮다
    assert alone("near:light:1:2", **uncertain) == 0
    pipe, repo, *_ = build_batch(**uncertain)
    req = batch_request(("speaker-1", "off"), ("near:light:1:2", "on"))
    d = pipe.decide_batch(req).decision
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.ORDER_UNCERTAIN)
    assert child_ids(repo) == ["c1"] and d.selected_count == 1
    stopped = repo.records[PARENT].batch.items[1]
    assert stopped.selector.selected == ("light-1", "light-2") and stopped.children == ()  # 고르기만 했고 하위 요청은 없다
    assert pipe.decide_batch(req).decision.selected_count == 1


def test_FR09_batch_old_entries_reject_batch():
    pipe, repo, prox, store, audit = build_batch()
    req = batch_request(("light-1", "on"), ("speaker-1", "off"))
    decision, ctx = pipe.decide_for_execution(req)
    assert (decision.verdict, decision.reason, decision.internal_request_id) == (
        Verdict.DENY, ReasonCode.INVALID_INPUT, "")
    assert decision.request_id == "r1" and ctx is None
    assert pipe.decide(req) == decision
    with pytest.raises(ValueError):
        pipe.decide_selector(req)  # 묶음 요청은 거리 선택자가 아니다
    for target in ("light-1", "nearest:light", "any:light", "near:light:1:2"):
        with pytest.raises(ValueError):
            pipe.decide_batch(request(target))  # 묶음 요청이 아니다
    assert repo.calls == [] and audit.events == []  # 저장소를 부르지 않는다


# ── 하위 요청은 단일 요청과 같은 경로를 쓴다 (FR-18) ────────────────────


def test_FR18_batch_children_follow_single_path():
    policy = FakePolicy()
    pipe, repo, prox, store, audit = build_batch(
        posteriors={"speaker-1": 0.9}, policy=policy, settings=batch_settings(threshold_speaker=0.5))
    req = batch_request(("near:light:1:all", "on"), ("nearest:speaker", "off"))
    sd = pipe.decide_batch(req)
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.devices == (device("light-2", 1), device("light-1", 1), device("light-3", 1), device("speaker-1", 2))
    assert (d.selected_count, d.similar, d.node_id, d.use_id, d.p_max) == (4, False, None, None, None)
    lights_item, speaker_item = req.items
    expected = [("c1", "light-2", lights_item), ("c2", "light-1", lights_item), ("c3", "light-3", lights_item),
                ("c4", "speaker-1", speaker_item)]
    assert child_ids(repo) == [child for child, _, _ in expected]
    # 하위 요청 행은 항목의 요청과 고른 기기로 만든다. 근접 지정 항목도 고른 기기를 넘긴다
    digest = input_digest(req)
    assert [(link.parent, link.req.target, link.req.action, link.node_id, link.digest)
            for link in repo.child_links] == [
        (PARENT, lights_item.target, "on", "light-2", digest), (PARENT, lights_item.target, "on", "light-1", digest),
        (PARENT, lights_item.target, "on", "light-3", digest), (PARENT, speaker_item.target, "off", "speaker-1", digest)]
    assert all(link.req.request_id == "r1" and link.req.items == () for link in repo.child_links)
    # 예약과 정책 검사는 하위 요청마다 한다
    assert [(r.internal_request_id, r.node_id, r.action, r.reoperate_only) for r in repo.reserved] == [
        (child, node_id, item.action, False) for child, node_id, item in expected]
    assert policy.calls == [node_id for _, node_id, _ in expected]
    assert policy.request_ids == [child for child, _, _ in expected]
    # 판정 기록과 인계 context 도 하위 요청마다 있다
    assert len(sd.children) == len(sd.contexts) == 4
    for (child, node_id, item), decision, ctx in zip(expected, sd.children, sd.contexts, strict=True):
        saved = repo.decisions[child]
        assert saved == decision
        assert (saved.node_id, saved.request_id, saved.verdict) == (node_id, "r1", Verdict.OK)
        assert (ctx.internal_request_id, ctx.node_id, ctx.use_id) == (child, node_id, None)
        assert ctx.execution_kind is ExecutionKind.SHARED and ctx.settings.version == "t"
        # 재검사가 쓰는 요청은 항목의 대상·동작이고 request_id 는 상위 요청의 외부 ID 다
        assert (ctx.request.target, ctx.request.action, ctx.request.request_id) == (item.target, item.action, "r1")
        assert events(audit, child), "하위 요청의 감사 이벤트가 있다"
    assert repo.decisions[PARENT].node_id is None
    assert [saved.internal_request_id for saved in repo.saved] == [*child_ids(repo), PARENT]  # 상위 판정은 마지막에 저장한다
    assert set(repo.records) == {PARENT, *child_ids(repo)}
    # 근접 지정 항목은 단일 요청처럼 사후확률을 한 번 부르고, ④~⑥ 감사를 하위 요청 ID 로 남긴다. ①②③ 은 아래의 상위 요청 ID 다
    assert [e.step for e in events(audit, "c4")] == [Step.POLICY, Step.DEVICE_OCCUPANCY, Step.POLICY]
    assert events(audit, "c4")[-1].payload == {"verdict": "OK", "reason": "OK", "settings_version": "t",
                                               "batch_parent": PARENT}
    assert prox.posterior_calls == [(["speaker-1"], ["light-1", "light-2", "light-3", "speaker-1"])]
    # 항목의 고르기는 상위 요청 ID 로 남는다: 거리 선택자 항목의 ①②와 거리 순서, 근접 지정 항목의 ①②③
    assert [e.step for e in events(audit, PARENT)] == [
        Step.BEACON, Step.CANDIDATE, Step.PROXIMITY, Step.BEACON, Step.CANDIDATE, Step.PROXIMITY,
        Step.DEVICE_OCCUPANCY]
    assert thaw(events(audit, PARENT, Step.PROXIMITY)[1].payload)["posteriors"] == {"speaker-1": 0.9}
    assert events(audit, PARENT)[-1].payload == {"verdict": "OK", "reason": "OK", "settings_version": "t",
                                                 "children": 4}


def test_FR18_batch_posterior_called_once_per_selector_item():
    values = {"locker-1": -50.0, "locker-2": -70.0, "locker-3": -90.0}
    nodes = [*(locker(node_id) for node_id in values), speaker()]
    pipe, repo, prox, store, audit = build_batch(
        nodes, values, {"locker-1": 0.5, "locker-2": 0.3, "locker-3": 0.15, "speaker-1": 0.9},
        settings=batch_settings(threshold_locker=0.1, threshold_speaker=0.5))
    sd = pipe.decide_batch(batch_request(
        ("near:locker:1:2", "open"), ("far:locker:1:1", "open"), ("nearest:speaker", "off")))
    assert (sd.decision.verdict, sd.decision.reason) == (Verdict.OK, ReasonCode.OK)
    assert sd.decision.devices == (
        device("locker-1", 1, use_id="use-locker-1"), device("locker-2", 1, use_id="use-locker-2"),
        device("locker-3", 2, use_id="use-locker-3"), device("speaker-1", 3))
    everyone = ["locker-1", "locker-2", "locker-3", "speaker-1"]
    # 거리 선택자 항목은 항목마다 한 번이다(고른 기기가 둘이어도). 근접 지정 항목은 하위 요청마다 한 번이다
    assert prox.posterior_calls == [(["locker-1", "locker-2", "locker-3"], everyone),
                                    (["locker-1", "locker-2", "locker-3"], everyone), (["speaker-1"], everyone)]


@pytest.mark.parametrize("target,action,chosen,stop", [
    pytest.param("nearest:speaker", "off", "speaker-1", None, id="근접 지정"),
    pytest.param("any:locker", "open", "locker-2", None, id="전역 할당"),
    pytest.param("speaker-1", "off", "speaker-1", None, id="명시 지정"),
    pytest.param("speaker-9", "off", None, ReasonCode.TARGET_UNAVAILABLE, id="② 에서 멈춤"),
    pytest.param("nearest:speaker", "off", None, ReasonCode.LOW_CONFIDENCE, id="③ 에서 멈춤"),
])
def test_FR18_batch_item_child_created_after_choosing(target, action, chosen, stop):
    """근접 지정·전역 할당·명시 지정 항목은 ①②③ 으로 기기를 먼저 고르고, 고른 기기로 하위 요청 행을 만든다 (#284)."""
    window = FakeWindow("locker-2")
    policy = FakePolicy({"locker-2": PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1, "ap-2")})
    pipe, repo, prox, store, audit = build_batch(
        [*world(), locker("locker-1"), locker("locker-2")], {},
        {"speaker-1": 0.1 if stop is ReasonCode.LOW_CONFIDENCE else 0.9, "locker-1": 0.5, "locker-2": 0.45},
        policy=policy, settings=batch_settings(threshold_speaker=0.5, threshold_locker=0.4), window=window)
    linked = []
    link = repo.link_selector_child

    def spy(parent, item_req, node_id, digest):  # 하위 요청 행을 만드는 때에 이미 남은 감사와 예약을 적는다
        linked.append((node_id, [(e.internal_request_id, e.step) for e in audit.events], len(repo.reserved)))
        return link(parent, item_req, node_id, digest)

    repo.link_selector_child = spy
    sd = pipe.decide_batch(batch_request(("light-1", "on"), (target, action)))
    second_item = repo.records[PARENT].batch.items[1]
    checks = list(zip(policy.calls, policy.request_ids))  # ④ 정책 검사의 (기기, 감사 ID)
    if stop is not None:  # 기기를 고르기 전에 멈췄다. 그 항목의 하위 요청 행을 만들지 않는다
        d = sd.decision
        assert d.reason is stop and d.devices == () and d.selected_count == 1
        assert [node_id for node_id, *_ in linked] == ["light-1"] and child_ids(repo) == ["c1"]
        assert second_item.children == () and second_item.nodes == ()
        assert store.cancels == [sibling_cancel("c1", stop, None)]  # 앞 항목의 예약만 취소한다
        assert checks == [("light-1", "c1")]
        return
    assert sd.decision.verdict is Verdict.OK
    assert [node_id for node_id, *_ in linked] == ["light-1", chosen]  # 하위 요청은 늘 고른 기기로 만든다
    _, audited, reserved = linked[1]
    # 이 항목의 하위 요청 행을 만들기 전에 ①②③ 이 상위 요청 ID 로 끝났다. 그 하위 요청의 감사와 예약은 아직 없다
    assert [step for iid, step in audited if iid == PARENT][3:6] == [Step.BEACON, Step.CANDIDATE, Step.PROXIMITY]
    assert "c2" not in {iid for iid, _ in audited} and reserved == 1  # 예약은 앞 항목의 하나다
    assert len(window.requests) == (1 if target.startswith("any:") else 0)  # 전역 할당의 배정은 고르기 안이다
    # 나머지는 하위 요청 ID 로 감사한다. ①②③ 감사는 하위 요청에 없다
    steps = {e.step for e in events(audit, "c2")}
    assert Step.DEVICE_OCCUPANCY in steps and not steps & {Step.BEACON, Step.CANDIDATE, Step.PROXIMITY}
    assert (repo.child_links[1].node_id, repo.decisions["c2"].node_id) == (chosen, chosen)
    assert (second_item.children, second_item.nodes) == (("c2",), (chosen,))
    if target.startswith("any:"):  # 전역 할당은 ④ 를 고르기 안에서 후보마다 하고(상위 요청 ID), 그 검사 결과를 예약에 그대로 넘긴다
        assert checks == [("light-1", "c1"), ("locker-1", PARENT), ("locker-2", PARENT)]
        assert [r.approval_id for r in repo.reserved] == [None, "ap-2"]
    else:  # 다른 항목은 ④ 를 하위 요청 ID 로 한 번 한다
        assert checks == [("light-1", "c1"), (chosen, "c2")]
    # 하위 요청의 판정 기록이 고르기에서 정한 근접 추정 값을 담는다. 명시 지정은 확률을 계산하지 않았다
    posteriors = {"nearest:speaker": {"speaker-1": 0.9}, "any:locker": {"locker-1": 0.5, "locker-2": 0.45}}.get(target, {})
    record = repo.records["c2"]
    assert (dict(record.posteriors), record.model_version) == (posteriors, "m" if posteriors else None)


@pytest.mark.parametrize("case", ["need_approval", "low_confidence"])
def test_FR18_batch_one_fails_none_executes(case):
    if case == "need_approval":  # 둘째 항목의 기기가 승인이 필요하다. 기기를 고른 뒤라 그 하위 요청이 걸린다
        policy = FakePolicy({"speaker-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
        pipe, repo, prox, store, audit = build_batch(policy=policy)
        second = ("speaker-1", "off")
        reason, verdict, node_id = ReasonCode.APPROVAL_REQUIRED, Verdict.NEED_APPROVAL, "speaker-1"
        children = ["c1", "c2", "c3"]
    else:  # 근접 지정 항목의 사후확률이 임계값(0.5) 미만이다. 기기를 고르지 못해 그 항목의 하위 요청을 만들지 않는다
        pipe, repo, prox, store, audit = build_batch(
            posteriors={"speaker-1": 0.1}, settings=batch_settings(threshold_speaker=0.5))
        second = ("nearest:speaker", "off")
        reason, verdict, node_id = ReasonCode.LOW_CONFIDENCE, Verdict.ASK, None
        children = ["c1", "c2"]
    sd = pipe.decide_batch(batch_request(("near:light:1:2", "on"), second))
    d = sd.decision
    assert (d.verdict, d.reason) == (verdict, reason)
    # 걸린 하위 요청이 있으면 그 기기가 devices 의 하나이고, 고르기에서 걸렸으면 빈 목록이다
    assert d.devices == (() if node_id is None else (device(node_id, 2, verdict, reason),))
    # selected_count 는 항목마다 고른 기기 수의 합이다. 하위 요청을 모두 만든 항목뿐이라 만든 수와 같고, 고르기에서 걸린 항목은 0이다
    assert d.selected_count == len(children) and (d.node_id, d.use_id, d.p_max) == (None, None, None)
    assert sd.contexts == () and sd.children == ()  # 아무것도 인계하지 않는다
    assert child_ids(repo) == children
    assert [r.internal_request_id for r in repo.reserved] == ["c1", "c2"]  # 걸린 하위 요청은 예약하지 않는다
    assert store.cancels == [sibling_cancel(c, reason, node_id) for c in ("c1", "c2")]
    assert [repo.decisions[c].verdict for c in ("c1", "c2")] == [Verdict.OK, Verdict.OK]
    second_item = repo.records[PARENT].batch.items[1]
    assert second_item.children == tuple(children[2:])
    if node_id is None:  # 고르기에서 걸렸다. 그 항목의 하위 요청도 기록도 없다
        assert "c3" not in repo.decisions and second_item.nodes == ()
    else:
        failed = repo.decisions["c3"]
        assert (failed.verdict, failed.reason, failed.node_id, failed.p_max) == (verdict, reason, node_id, None)


@pytest.mark.parametrize("case", ["child_fails", "selector_stops"])
def test_FR18_batch_sibling_cancel_precedes_saving_parent(case):
    if case == "child_fails":
        policy = FakePolicy({"speaker-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
        pipe, repo, prox, store, audit = build_batch(policy=policy)
        req = batch_request(("light-1", "on"), ("speaker-1", "off"))
    else:
        pipe, repo, prox, store, audit = build_batch()
        req = batch_request(("light-1", "on"), ("near:light:4:1", "on"))
    saved_at_cancel = []
    cancel = store.cancel_not_sent

    def spy(*args):
        saved_at_cancel.append(PARENT in {d.internal_request_id for d in repo.saved})
        return cancel(*args)

    store.cancel_not_sent = spy
    pipe.decide_batch(req)
    assert saved_at_cancel == [False]  # 상위 판정을 저장하기 전에 취소한다


@pytest.mark.parametrize("case", ["reserve_of_single_item", "choose_of_single_item", "policy_of_selector_child"])
def test_FR18_batch_internal_error_cancels_children(case):
    if case == "reserve_of_single_item":  # 둘째 항목(명시 지정)의 reserve_if_free 가 예외를 던진다. 예약을 부른 뒤라 취소한다
        pipe, repo, prox, store, audit = build_batch()
        real = repo.reserve_if_free

        def reserve(reserve_request):
            if reserve_request.node_id == "speaker-1":
                raise RuntimeError("secret internal detail")
            return real(reserve_request)

        repo.reserve_if_free = reserve
        req = batch_request(("near:light:1:1", "on"), ("speaker-1", "off"))
        cancelled, unsaved, unsaved_node = ["c1", "c2"], "c2", "speaker-1"  # 기기를 고른 뒤에 만든 하위 요청이라 기기가 있다
    elif case == "choose_of_single_item":  # 둘째 항목(근접 지정)의 고르기 ① 에서 예외가 난다. 그 항목은 하위 요청을 만들기 전이다
        pipe, repo, prox, store, audit = build_batch(
            posteriors={"speaker-1": 0.9}, settings=batch_settings(threshold_speaker=0.5))
        real, seen = repo.beacons_of, []

        def beacons_of(user_id):
            seen.append(user_id)
            if len(seen) == 2:  # 첫 항목의 ① 은 지나고 둘째 항목의 ① 에서 던진다
                raise RuntimeError("secret internal detail")
            return real(user_id)

        repo.beacons_of = beacons_of
        req = batch_request(("near:light:1:1", "on"), ("nearest:speaker", "off"))
        cancelled, unsaved, unsaved_node = ["c1"], None, None
    else:  # 둘째 항목(거리 선택자)의 둘째 하위 요청의 정책 검사가 예외를 던진다. 예약 전이라 취소하지 않는다
        pipe, repo, prox, store, audit = build_batch(policy=ExplodingPolicy("light-1"))
        req = batch_request(("speaker-1", "off"), ("near:light:1:2", "on"))
        cancelled, unsaved, unsaved_node = ["c1", "c2"], "c3", "light-1"
    sd = pipe.decide_batch(req)
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert "secret" not in repr(d) and d.message == "요청을 처리하지 못했습니다."
    assert sd.contexts == () and sd.children == ()
    # 예약을 부른 하위 요청을 모두 취소한다. 근거는 판정 오류다
    assert store.cancels == [(c, ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR, {
        "source": "decision_error", "reason": "INTERNAL_ERROR"}) for c in cancelled]
    # 판정을 저장한 하위 요청은 그대로고, 저장하지 못한 하위 요청에는 INTERNAL_ERROR 를 저장한다
    assert all(repo.decisions[c].verdict is Verdict.OK for c in cancelled if c != unsaved)
    assert [repo.save_attempts.count(c) for c in (*cancelled, PARENT)] == [1] * (len(cancelled) + 1)  # 다시 저장하려 하지 않는다
    if unsaved is None:  # 기기를 고르기 전에 멈춘 항목은 하위 요청 행을 만들지 않았다
        assert child_ids(repo) == cancelled
    else:
        rejected = repo.decisions[unsaved]
        assert (rejected.verdict, rejected.reason, rejected.node_id) == (
            Verdict.DENY, ReasonCode.INTERNAL_ERROR, unsaved_node)
    assert (repo.decisions[PARENT].verdict, repo.decisions[PARENT].reason) == (
        Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert repo.records[PARENT].batch is None  # 오류 판정은 묶음 기록을 담지 않는다
    assert any("secret" in str(e.payload) for e in audit.events)  # 예외 내용은 감사에만 남는다


# ── 상위 판정 기록 (FR-22) ─────────────────────────────────────────────


def test_FR22_batch_parent_record_has_items():
    settings = batch_settings(threshold_speaker=0.5)
    pipe, repo, prox, store, audit = build_batch(
        [*dimmable_lights(), speaker()], posteriors={"speaker-1": 0.9}, settings=settings)
    sd = pipe.decide_batch(batch_request(
        ("near:light:1:2", "on", {"brightness": 10}), ("nearest:speaker", "off")))
    assert sd.decision.verdict is Verdict.OK
    # 인자는 받은 그대로 기록하고, 판정이 정한 인자는 인계 context 가 가진다
    assert [thaw(ctx.arguments) for ctx in sd.contexts] == [{"brightness": 10}, {"brightness": 10}, None]
    record = repo.records[PARENT]
    lights_item, speaker_item = record.batch.items
    # 항목은 받은 순서대로, 받은 요청(대상·동작·인자)과 만든 하위 요청을 담는다
    assert (lights_item.target_spec, lights_item.action, thaw(lights_item.arguments)) == (
        "near:light:1:2", "on", {"brightness": 10})
    assert lights_item.children == ("c1", "c2")
    assert (speaker_item.target_spec, speaker_item.action, speaker_item.arguments) == (
        "nearest:speaker", "off", None)
    assert speaker_item.children == ("c3",) and speaker_item.selector is None
    # 거리 선택자 항목은 거리 선택자 기록도 담는다. 그 안의 하위 요청은 항목의 것과 같다
    selector = lights_item.selector
    assert selector.target_spec == "near:light:1:2"
    assert selector.order == ("light-2", "light-1", "light-3") and dict(selector.values) == LIGHT_VALUES
    assert selector.selected == ("light-2", "light-1") and selector.similar is False
    assert selector.children == lights_item.children
    assert dict(selector.counts) == {"light-2": 30, "light-1": 30, "light-3": 30}
    assert selector.boundary_probability == pytest.approx(boundary_probability(
        -60.0, 30, -75.0, 30, settings.order_frame_sigma_db, settings.order_static_sigma_db))
    # 상위 판정은 기기가 없고 근접 추정 값을 담지 않는다. 그 값은 하위 요청의 기록이 담는다
    assert repo.decisions[PARENT].node_id is None and record.selector is None
    assert (record.model_version, record.calib_version, record.policy_version) == (None, None, None)
    assert (record.window_start, record.window_end, dict(record.posteriors)) == (None, None, {})
    assert record.settings.version == "t"
    assert (repo.records["c3"].model_version, dict(repo.records["c3"].posteriors)) == ("m", {"speaker-1": 0.9})


def test_FR22_batch_record_has_all_requested_items():
    """상위 판정 기록은 걸려도, 항목 수나 용량을 넘어도 요청의 항목을 모두 항목 순서대로 담는다 (FR-22, spec 결정 15·27).

    판정하지 않은 항목은 받은 대상·동작·인자만 있고 하위 요청·기기·거리 선택자 기록이 없다.
    """
    # 첫 항목이 걸린다. 뒤 둘은 판정하지 않았다
    policy = FakePolicy({"speaker-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, *_ = build_batch([*dimmable_lights(), speaker()], policy=policy)
    req = batch_request(("speaker-1", "off"), ("near:light:1:2", "on", {"brightness": 10}), ("light-3", "on"))
    first = pipe.decide_batch(req)
    assert (first.decision.verdict, first.decision.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)
    assert child_ids(repo) == ["c1"]  # 첫 항목의 하위 요청뿐이다
    judged, second, third = repo.records[PARENT].batch.items
    assert (judged.target_spec, judged.children, judged.nodes) == ("speaker-1", ("c1",), ("speaker-1",))
    assert second == BatchItemRecord("near:light:1:2", "on", {"brightness": 10})
    assert (second.children, second.nodes, second.selector) == ((), (), None)
    assert third == BatchItemRecord("light-3", "on", None)
    # 재전송은 같은 판정과 걸린 기기를 낸다. 판정하지 않은 항목은 하위 요청이 없어 응답에 나오지 않는다
    again = pipe.decide_batch(req)
    assert again.decision == first.decision
    assert again.decision.devices == (device("speaker-1", 1, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert again.decision.selected_count == 1

    # 항목 수 초과와 용량 초과는 하위 요청 없이 거부하지만 기록은 요청의 항목을 모두 담는다
    items = (("light-1", "on"), ("near:light:1:2", "on"), ("speaker-1", "off"))
    requested = [BatchItemRecord(target, action, None) for target, action in items]
    pipe, repo, *_ = build_batch(settings=batch_settings(batch_max_items=2))
    assert pipe.decide_batch(batch_request(*items)).decision.reason is ReasonCode.INVALID_INPUT
    assert list(repo.records[PARENT].batch.items) == requested
    assert repo.child_links == []
    pipe, repo, *_ = build_batch()
    repo.capacity_exceeded = True
    assert pipe.decide_batch(batch_request(*items)).decision.reason is ReasonCode.CAPACITY_EXCEEDED
    assert list(repo.records[PARENT].batch.items) == requested
    assert repo.child_links == []


@pytest.mark.parametrize("case", ["all_ok", "selector_child_fails"])
def test_FR22_batch_record_has_item_nodes(case):
    """항목 기록은 만든 하위 요청마다 고른 기기를 같은 순서로 담는다. 거리 선택자 항목은 고른 기기의 앞쪽이다 (#284)."""
    if case == "all_ok":
        pipe, repo, prox, store, audit = build_batch(
            [*world(), locker("locker-1"), locker("locker-2")],
            posteriors={"speaker-1": 0.9, "locker-1": 0.5, "locker-2": 0.45},
            settings=batch_settings(threshold_speaker=0.5, threshold_locker=0.4), window=FakeWindow("locker-2"))
        req = batch_request(("near:light:1:2", "on"), ("nearest:speaker", "off"), ("locker-1", "open"),
                            ("any:locker", "open"))
        nodes = [("light-2", "light-1"), ("speaker-1",), ("locker-1",), ("locker-2",)]
    else:  # 거리 선택자 항목의 둘째 하위 요청이 걸려 멈췄다. 셋째 기기의 하위 요청은 만들지 않았고, 둘째 항목은 판정하지 않았다
        policy = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
        pipe, repo, prox, store, audit = build_batch(policy=policy)
        req = batch_request(("near:light:1:all", "on"), ("speaker-1", "off"))
        nodes = [("light-2", "light-1"), ()]
    pipe.decide_batch(req)
    items = repo.records[PARENT].batch.items
    assert [item.nodes for item in items] == nodes
    for item in items:  # 하위 요청과 같은 순서·같은 수고, 하위 요청 판정이 가진 기기와 같다
        assert tuple(repo.decisions[child].node_id for child in item.children) == item.nodes
        assert item.selector is None or item.nodes == item.selector.selected[:len(item.nodes)]
    if case == "selector_child_fails":
        assert items[0].selector.selected == ("light-2", "light-1", "light-3")


# ── ⓪ 중복과 재전송 (FR-22) ───────────────────────────────────────────


def test_FR22_batch_same_request_id_other_input_conflicts():
    pipe, repo, prox, store, audit = build_batch()
    original = batch_request(("light-1", "on"), ("speaker-1", "off"))
    assert pipe.decide_batch(original).decision.verdict is Verdict.OK
    single = request("light-1", request_id="r2")
    assert pipe.decide(single).verdict is Verdict.OK  # 단일 요청이 쓴 키다
    before = snapshot(repo, store, audit)[:5]
    others = [
        batch_request(("speaker-1", "off"), ("light-1", "on")),  # 항목 순서가 다르다
        batch_request(("light-1", "on")),  # 항목이 다르다
        batch_request(("light-1", "on"), request_id="r2"),  # 단일 요청을 보냈던 키다
    ]
    for other in others:
        sd = pipe.decide_batch(other)
        d = sd.decision
        assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
        assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
        assert sd.children == () and sd.contexts == ()
    # 아무것도 새로 만들지 않았고, 같은 요청을 다시 보내면 충돌이 아니라 재전송이다
    assert [e.payload for e in events(audit, PARENT, Step.DEDUP)] == [{"result": "REQUEST_ID_CONFLICT"}] * 2
    again = pipe.decide_batch(original)
    assert (again.decision.verdict, again.decision.reason) == (Verdict.OK, ReasonCode.OK) and again.contexts == ()
    assert snapshot(repo, store, audit)[:4] == before[:4]


def test_FR22_batch_in_progress_resend_is_busy_and_not_saved():
    pipe, repo, prox, store, audit = build_batch()
    req = batch_request(("light-1", "on"), ("speaker-1", "off"))
    repo.keys[(req.user_id, req.request_id)] = ("i9", input_digest(req))  # 같은 키가 있는데 판정이 아직 없다
    sd = pipe.decide_batch(req)
    d = sd.decision
    assert (d.verdict, d.reason, d.internal_request_id) == (Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS, "i9")
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert repo.calls == ["link_request"] and repo.saved == [] and store.cancels == []
    assert [(e.step, e.payload) for e in audit.events] == [(Step.DEDUP, {"result": "IN_PROGRESS"})]


def test_FR22_batch_resend_returns_children_results():
    pipe, repo, prox, store, audit = build_batch(
        [*world(), locker("locker-1")], SIMILAR_LIGHT_VALUES, {"speaker-1": 0.9},
        settings=batch_settings(threshold_speaker=0.5))
    req = batch_request(("near:light:1:2", "on"), ("nearest:speaker", "off"), ("locker-1", "open"))
    first = pipe.decide_batch(req)
    assert first.decision.similar is True and len(first.contexts) == 4
    before = snapshot(repo, store, audit)
    again = pipe.decide_batch(req)
    assert again.decision == first.decision  # devices·selected_count·similar 도 같다
    assert again.decision.devices == (
        device("light-2", 1), device("light-1", 1), device("speaker-1", 2),
        device("locker-1", 3, use_id="use-locker-1"))
    assert (again.decision.selected_count, again.decision.similar) == (4, True)
    assert again.children == first.children and again.contexts == ()
    assert snapshot(repo, store, audit) == before  # 새 하위 요청도 예약도 저장도 감사도 없다


@pytest.mark.parametrize("case", ["need_approval", "low_confidence", "order_uncertain"])
def test_FR22_batch_resend_after_check_failure(case):
    if case == "need_approval":
        policy = FakePolicy({"speaker-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
        pipe, repo, prox, store, audit = build_batch(policy=policy)
        req = batch_request(("near:light:1:1", "on"), ("speaker-1", "off"))
        devices, selected_count, similar = (
            device("speaker-1", 2, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),), 2, False
    elif case == "low_confidence":  # 둘째 항목이 기기를 고르지 못해 하위 요청 없이 멈췄다. 재전송도 기기를 내지 않는다
        pipe, repo, prox, store, audit = build_batch(
            posteriors={"speaker-1": 0.1}, settings=batch_settings(threshold_speaker=0.5))
        req = batch_request(("near:light:1:1", "on"), ("nearest:speaker", "off"))
        devices, selected_count, similar = (), 1, False
    else:  # 하위 요청 전에 멈췄다. 비슷함은 멈춘 항목의 값이다
        pipe, repo, prox, store, audit = build_batch(
            values={"light-1": -60.0, "light-2": -61.0, "light-3": -62.0},
            settings=batch_settings(order_boundary_min_prob=0.9))
        req = batch_request(("speaker-1", "off"), ("near:light:1:1", "on"))
        devices, selected_count, similar = (), 1, True
    first = pipe.decide_batch(req)
    assert first.decision.verdict is not Verdict.OK
    assert (first.decision.devices, first.decision.selected_count, first.decision.similar) == (
        devices, selected_count, similar)
    before = snapshot(repo, store, audit)
    again = pipe.decide_batch(req)
    assert again.decision == first.decision  # 같은 판정과 걸린 기기·항목 순서다
    assert again.children == () and again.contexts == ()
    assert snapshot(repo, store, audit) == before  # 새 하위 요청도 예약도 취소도 없다


@pytest.mark.parametrize("damage", [
    "missing_child_decision", "child_not_ok", "missing_batch_record",
    "empty_items", "second_item_missing", "second_item_child_missing", "selector_fewer_children",
    "selector_record_missing", "single_item_has_selector",
    "item_target_differs", "item_action_differs", "item_arguments_differ",
])
def test_FR22_batch_resend_inconsistent_ok_is_internal_error(damage):
    pipe, repo, prox, store, audit = build_batch()
    req = batch_request(("near:light:1:2", "on"), ("speaker-1", "off"))
    pipe.decide_batch(req)
    last = child_ids(repo)[-1]
    selector_item, single_item = repo.records[PARENT].batch.items

    def rewrite(*items):  # 저장된 OK 상위 판정의 묶음 기록을 바꾼다
        repo.records[PARENT] = replace(repo.records[PARENT], batch=BatchRecord(items))

    if damage == "missing_child_decision":  # 하위 판정이 저장되지 않았다
        del repo.decisions[last]
    elif damage == "missing_batch_record":  # OK 상위 판정에 묶음 기록이 없다
        repo.records[PARENT] = replace(repo.records[PARENT], batch=None)
    elif damage == "child_not_ok":  # 하위 판정이 OK 가 아니다
        repo.decisions[last] = Decision(last, "r1", Verdict.BUSY, ReasonCode.DEVICE_BUSY, node_id="speaker-1")
    elif damage == "empty_items":  # 항목이 하나도 없다
        rewrite()
    elif damage == "second_item_missing":  # 둘째 항목의 기록이 없다
        rewrite(selector_item)
    elif damage == "second_item_child_missing":  # 둘째 항목에 하위 요청이 없다
        rewrite(selector_item, replace(single_item, children=(), nodes=()))
    elif damage == "selector_fewer_children":  # 거리 선택자 항목의 하위 요청이 고른 기기(둘)보다 적다
        one = selector_item.children[:1]
        rewrite(replace(selector_item, children=one, nodes=selector_item.nodes[:1],
                        selector=replace(selector_item.selector, children=one)), single_item)
    elif damage == "selector_record_missing":  # 거리 선택자 항목에 거리 선택자 기록이 없다
        rewrite(replace(selector_item, selector=None), single_item)
    elif damage == "single_item_has_selector":  # 거리 선택자가 아닌 항목에 거리 선택자 기록이 있다
        lone = SelectorRecord("speaker-1", ("speaker-1",), {"speaker-1": -50.0}, ("speaker-1",), False,
                              single_item.children)
        rewrite(selector_item, replace(single_item, selector=lone))
    elif damage == "item_target_differs":  # 기록한 항목의 대상이 요청과 다르다
        rewrite(replace(selector_item, target_spec="near:light:1:3"), single_item)
    elif damage == "item_action_differs":  # 기록한 항목의 동작이 요청과 다르다
        rewrite(selector_item, replace(single_item, action="on"))
    else:  # item_arguments_differ: 기록한 항목의 인자가 요청과 다르다
        rewrite(replace(selector_item, arguments={"brightness": 10}), single_item)
    saved, attempts = list(repo.saved), list(repo.save_attempts)
    again = pipe.decide_batch(req)
    d = again.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)  # OK 가 되지 않는다
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert again.children == () and again.contexts == ()
    assert repo.saved == saved and repo.save_attempts == attempts  # 저장하지 않는다. 시도도 하지 않는다
    assert events(audit, PARENT)[-1].payload["result"] == "INTERNAL_ERROR"
    assert events(audit, PARENT)[-1].payload["error"].startswith("ValueError(")  # 일관성 검사가 ValueError 로 거부했다
