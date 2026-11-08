"""FR-09·FR-17·FR-18·FR-22 거리 선택자 판정 (specs/core-distance-selector/spec.md).

상위 요청 하나와 고른 기기마다의 하위 요청으로 판정한다. 다른 계층은 `_selector_fakes` 의 fake 로 대신한다.
"""

from __future__ import annotations

import pytest
from _selector_fakes import (
    LIGHT_VALUES,
    FakePolicy,
    FakeReadiness,
    build,
    frame,
    lights,
    node,
    request,
)

from nearpilot.core.decision.digest import input_digest
from nearpilot.core.decision.pipeline import SelectorDecision, selector_cut
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    ExecutionKind,
    ExecutionStatus,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    Step,
    StoreOutcome,
    Verdict,
)
from nearpilot.shared.frozen import thaw
from nearpilot.shared.models import Decision, DeviceOutcome, PolicyCheckResult

PARENT = "i1"  # FakeRepo 가 처음 연결한 요청의 내부 ID
SIBLING_EVIDENCE = "selector_sibling"

# 사물함 두 대. locker-1 이 더 가깝다.
LOCKER_VALUES = {"locker-1": -50.0, "locker-2": -70.0}


def lockers():
    return [node(node_id, "locker", Occupancy.RENTAL) for node_id in LOCKER_VALUES]


def device(node_id, verdict=Verdict.OK, reason=ReasonCode.OK, use_id=None):
    return DeviceOutcome(node_id, verdict, reason, use_id)


def child_ids(repo):
    return [link.child for link in repo.child_links]


def events(audit, internal_request_id, step=None):
    return [e for e in audit.events
            if e.internal_request_id == internal_request_id and (step is None or e.step is step)]


class ExplodingPolicy(FakePolicy):
    """정해 둔 기기의 정책 검사에서 예외를 던진다."""

    def __init__(self, node_id):
        super().__init__()
        self.node_id = node_id

    def check(self, user_id, node_id, action, now, **kw):
        if node_id == self.node_id:
            raise RuntimeError("secret internal detail")
        return super().check(user_id, node_id, action, now, **kw)


# ── 고르기: 거리 순서에서 시작·개수 ───────────────────────────────────


def test_FR09_selector_near_picks_closest_two():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    sd = pipe.decide_selector(request("near:light:1:2"))
    d = sd.decision
    assert isinstance(sd, SelectorDecision)
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert [link.node_id for link in repo.child_links] == ["light-2", "light-1"]
    assert [c.node_id for c in sd.contexts] == ["light-2", "light-1"]
    assert [c.node_id for c in sd.children] == ["light-2", "light-1"]
    assert d.devices == (device("light-2"), device("light-1"))
    assert d.selected_count == 2 and d.similar is False
    assert d.node_id is None and d.use_id is None and d.p_max is None
    # 인계 context 는 하위 판정과 같은 순서이고 같은 하위 요청이다
    assert [c.internal_request_id for c in sd.contexts] == child_ids(repo)
    assert [c.internal_request_id for c in sd.children] == child_ids(repo)


def test_FR09_selector_far_picks_farthest():
    pipe, repo, *_ = build(lights(), LIGHT_VALUES)
    sd = pipe.decide_selector(request("far:light:1:1"))
    assert sd.decision.verdict is Verdict.OK
    assert [link.node_id for link in repo.child_links] == ["light-3"]
    assert sd.decision.devices == (device("light-3"),)
    assert sd.decision.selected_count == 1


def test_FR09_selector_start_two_until_end():
    pipe, repo, *_ = build(lights(), LIGHT_VALUES)
    sd = pipe.decide_selector(request("near:light:2:all"))
    assert sd.decision.verdict is Verdict.OK
    assert [link.node_id for link in repo.child_links] == ["light-1", "light-3"]
    assert sd.decision.selected_count == 2


def test_FR09_selector_count_shortage_runs_available():
    pipe, repo, *_ = build(lights(), LIGHT_VALUES)
    sd = pipe.decide_selector(request("near:light:2:5"))  # 둘째부터 다섯 개를 원하지만 둘뿐이다
    assert sd.decision.verdict is Verdict.OK
    assert [link.node_id for link in repo.child_links] == ["light-1", "light-3"]
    assert sd.decision.selected_count == 2  # 실제로 고른 개수다
    assert len(sd.contexts) == 2


@pytest.mark.parametrize("case", ["empty_order", "start_beyond_end", "no_candidate"])
def test_FR09_selector_no_participant_no_candidate(case):
    if case == "empty_order":  # 근접 참여 기기가 하나도 없다
        pipe, repo, prox, store, audit = build(lights(), {})
        target = "near:light:1:2"
    elif case == "start_beyond_end":  # 시작이 순서의 끝 뒤다
        pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
        target = "near:light:4:1"
    else:  # 비콘은 봤지만 요청 종류(조명)의 후보가 없다
        pipe, repo, prox, store, audit = build([node("speaker-1", "speaker", Occupancy.SHARED)],
                                               {"speaker-1": -50.0})
        target = "near:light:1:2"
    sd = pipe.decide_selector(request(target))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.NO_CANDIDATE)
    assert d.devices == () and d.selected_count == 0 and d.similar is False
    assert d.node_id is None
    assert sd.children == () and sd.contexts == ()
    assert repo.child_links == [] and repo.reserved == [] and store.cancels == []
    selector = repo.records[PARENT].selector
    if case == "no_candidate":  # 거리 순서를 만들기 전에 멈췄다
        assert selector is None and prox.order_calls == []
    else:  # 거리 순서는 만들었으므로 기록에 남긴다
        assert selector is not None
        assert selector.selected == () and selector.children == () and selector.similar is False
        assert selector.target_spec == target


# ── "비슷함" 표시 ───────────────────────────────────────────────────


def test_FR09_selector_similar_boundary_flag():
    order = ("a", "b", "c")

    def cut(values, start, count, db=3.0):
        return selector_cut(order, dict(zip(order, values)), start, count, db)

    # 끝 경계(b, c)의 차이가 기준(3 dB)보다 작으면 참, 크면 거짓이다
    assert cut((-50.0, -60.0, -62.0), 1, 2) == (("a", "b"), True)
    assert cut((-50.0, -60.0, -70.0), 1, 2) == (("a", "b"), False)
    # 시작 경계(a, b)도 본다
    assert cut((-58.0, -60.0, -75.0), 2, 2) == (("b", "c"), True)
    assert cut((-50.0, -60.0, -75.0), 2, 2) == (("b", "c"), False)
    # 차이가 기준과 같으면 비슷하지 않다. 기준은 인자로 받는다
    assert cut((-50.0, -60.0, -63.0), 1, 2) == (("a", "b"), False)
    assert cut((-50.0, -60.0, -63.0), 1, 2, db=3.5) == (("a", "b"), True)
    # 두 값이 모두 None 이면 비슷하고, 하나만 None 이면 비슷하지 않다
    assert cut((-50.0, None, None), 1, 2) == (("a", "b"), True)
    assert cut((-50.0, -60.0, None), 1, 2) == (("a", "b"), False)
    # 경계가 둘이면 한쪽만 비슷해도 참이다
    assert cut((-58.0, -60.0, -75.0), 2, 1) == (("b",), True)  # 시작 경계만 비슷
    assert cut((-50.0, -60.0, -62.0), 2, 1) == (("b",), True)  # 끝 경계만 비슷
    assert cut((-50.0, -60.0, -75.0), 2, 1) == (("b",), False)
    # 끝까지 고르면 경계가 없다
    assert cut((-50.0, -60.0, -62.0), 1, None) == (("a", "b", "c"), False)
    assert cut((-50.0, -60.0, -62.0), 1, 5) == (("a", "b", "c"), False)
    # 고를 기기가 없으면 ((), False) 다
    assert cut((-50.0, -60.0, -62.0), 4, 1) == ((), False)
    assert cut((-50.0, -60.0, -62.0), 4, None) == ((), False)


def test_FR09_selector_similar_flag_in_decision_and_record():
    values = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0}  # 끝 경계(light-1, light-3)가 2 dB 차이다
    pipe, repo, prox, store, audit = build(lights(), values)
    sd = pipe.decide_selector(request("near:light:1:2"))
    assert sd.decision.verdict is Verdict.OK and sd.decision.similar is True
    assert repo.decisions[PARENT].similar is True
    assert repo.records[PARENT].selector.similar is True
    order_event = events(audit, PARENT, Step.PROXIMITY)[0]
    assert order_event.payload["similar"] is True and order_event.payload["order_similar_db"] == 3.0


def test_FR09_selector_similar_threshold_comes_from_settings():
    values = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0}
    pipe, *_ = build(lights(), values, settings=Settings(version="t", order_similar_db=1.5))
    assert pipe.decide_selector(request("near:light:1:2")).decision.similar is False


# ── 근접 검사: 점유 방식별 (FR-17) ────────────────────────────────────


def test_FR17_selector_shared_skips_posterior():
    lamps = [node("lamp-1", "lamp", Occupancy.SHARED), node("lamp-2", "lamp", Occupancy.SHARED)]
    pipe, repo, prox, store, audit = build(lamps, {"lamp-1": -50.0, "lamp-2": -60.0})
    assert Settings(version="t").threshold_for("lamp") is None  # 임계값 설정이 없는 종류다
    sd = pipe.decide_selector(request("near:lamp:1:2"))
    assert (sd.decision.verdict, sd.decision.reason) == (Verdict.OK, ReasonCode.OK)
    assert len(sd.contexts) == 2
    assert prox.posterior_calls == []
    first = events(audit, repo.child_links[0].child, Step.PROXIMITY)[0]
    assert first.payload == {"skipped": True, "reason": "selector_shared", "node_id": "lamp-1"}


def test_FR17_selector_rental_threshold_ask():
    pipe, repo, prox, store, audit = build(lockers(), LOCKER_VALUES, {"locker-1": 0.97, "locker-2": 0.02})
    sd = pipe.decide_selector(request("near:locker:1:2", action="open"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE)
    assert d.devices == (device("locker-2", Verdict.ASK, ReasonCode.LOW_CONFIDENCE),)
    assert d.selected_count == 2 and d.node_id is None and d.p_max is None
    assert sd.contexts == () and sd.children == ()
    first, second = child_ids(repo)
    assert [c[0] for c in store.cancels] == [first]  # 앞 하위 요청의 예약만 한 번 취소한다
    assert store.cancels[0][1:3] == (ExecutionStatus.PENDING, ReasonCode.LOW_CONFIDENCE)
    assert store.cancels[0][3]["source"] == SIBLING_EVIDENCE
    assert len(prox.posterior_calls) == 1
    assert repo.decisions[second].p_max == 0.02  # 하위 판정은 자기 기기의 사후확률을 담는다

    # 가장 먼 쪽이 locker-2 가 되는 방향도 같다
    pipe, repo, prox, store, audit = build(lockers(), LOCKER_VALUES, {"locker-1": 0.97, "locker-2": 0.02})
    d = pipe.decide_selector(request("far:locker:1:1", action="open")).decision
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE)
    assert d.devices == (device("locker-2", Verdict.ASK, ReasonCode.LOW_CONFIDENCE),)
    assert d.selected_count == 1
    assert store.cancels == [] and len(prox.posterior_calls) == 1


def test_FR17_selector_rental_without_posterior_entry_is_low_confidence():
    pipe, repo, prox, store, audit = build(lockers(), LOCKER_VALUES, {"locker-1": 0.97})  # locker-2 는 표에 없다
    d = pipe.decide_selector(request("far:locker:1:1", action="open")).decision
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE)
    assert repo.decisions[repo.child_links[0].child].p_max == 0.0


def test_FR17_selector_posterior_gets_all_candidates():
    lamp = node("lamp-1", "lamp", Occupancy.SHARED)  # 같은 장소의 다른 종류다. 후보는 아니지만 장소의 기기다
    other_place = node("locker-9", "locker", Occupancy.RENTAL, place_id="q")  # 다른 장소는 장소의 기기가 아니다
    pipe, repo, prox, store, audit = build(
        [*lockers(), lamp, other_place], LOCKER_VALUES, {"locker-1": 0.97, "locker-2": 0.02})
    repo.frames = [frame(node_id) for node_id in ("locker-1", "locker-2", "lamp-1")]  # 비콘은 이 장소에서만 들린다
    sd = pipe.decide_selector(request("near:locker:1:1", action="open"))  # locker-1 만 고른다
    assert sd.decision.verdict is Verdict.OK
    assert prox.posterior_calls == [(["locker-1", "locker-2"], ["locker-1", "locker-2", "lamp-1"])]


def test_FR17_selector_rental_probability_equal_to_threshold_passes():
    settings = Settings(version="t", threshold_locker=0.5)
    pipe, repo, prox, store, audit = build(
        lockers(), LOCKER_VALUES, {"locker-1": 0.5, "locker-2": 0.5}, settings=settings)
    d = pipe.decide_selector(request("near:locker:1:2", action="open")).decision
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)  # 임계값 미만일 때만 묻는다


def test_FR17_selector_rental_all_above_threshold_ok():
    settings = Settings(version="t", threshold_locker=0.4)
    pipe, repo, prox, store, audit = build(
        lockers(), LOCKER_VALUES, {"locker-1": 0.5, "locker-2": 0.45}, settings=settings)
    sd = pipe.decide_selector(request("near:locker:1:2", action="open"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert d.devices == (device("locker-1", use_id="use-locker-1"), device("locker-2", use_id="use-locker-2"))
    assert d.use_id is None  # 상위 판정에는 use_id 가 없다. 기기마다 devices 에 있다
    assert [c.use_id for c in sd.contexts] == ["use-locker-1", "use-locker-2"]
    assert [c.execution_kind for c in sd.contexts] == [ExecutionKind.RENTAL_INITIAL] * 2
    assert [c.p_max for c in sd.children] == [0.5, 0.45]
    assert len(prox.posterior_calls) == 1  # 요청 하나에 한 번이다
    assert store.cancels == []


def test_FR22_selector_records_posterior_for_parent_and_rental_children():
    settings = Settings(version="t", threshold_locker=0.4)
    pipe, repo, *_ = build(lockers(), LOCKER_VALUES, {"locker-1": 0.5, "locker-2": 0.45}, settings=settings)
    pipe.decide_selector(request("near:locker:1:2", action="open"))
    for iid in (PARENT, *child_ids(repo)):
        record = repo.records[iid]
        assert (record.model_version, record.calib_version) == ("m", "c")
        assert dict(record.posteriors) == {"locker-1": 0.5, "locker-2": 0.45}
        assert record.window_start is not None and record.window_end is not None
        assert record.settings.version == "t"
    assert repo.records[PARENT].policy_version is None
    assert [repo.records[c].policy_version for c in child_ids(repo)] == [1, 1]
    # 사후확률을 부르지 않은 요청(공용형만)의 기록에는 근접 값이 없다
    pipe, repo, *_ = build(lights(), LIGHT_VALUES)
    pipe.decide_selector(request("near:light:1:2"))
    for iid in (PARENT, *child_ids(repo)):
        record = repo.records[iid]
        assert (record.model_version, record.calib_version, record.window_start) == (None, None, None)
        assert dict(record.posteriors) == {}


def test_FR17_selector_rental_without_threshold_denied():
    pipe, repo, prox, store, audit = build([node("box-1", "box", Occupancy.RENTAL)], {"box-1": -50.0})
    assert Settings(version="t").threshold_for("box") is None
    sd = pipe.decide_selector(request("near:box:1:1", action="open"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert d.devices == (device("box-1", Verdict.DENY, ReasonCode.INVALID_INPUT),)
    assert d.selected_count == 1
    assert prox.posterior_calls == []
    assert repo.reserved == [] and sd.contexts == ()
    assert repo.decisions[repo.child_links[0].child].node_id == "box-1"
    detail = events(audit, repo.child_links[0].child, Step.PROXIMITY)[0].payload
    assert detail == {"node_id": "box-1", "threshold": None}


# ── 하위 요청은 단일 기기 요청과 같은 경로를 쓴다 (FR-18) ───────────────


def test_FR18_selector_children_follow_single_path():
    pol = FakePolicy()
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    req = request("near:light:1:2")
    sd = pipe.decide_selector(req)
    first, second = child_ids(repo)
    digest = input_digest(req)
    assert [(link.parent, link.req, link.node_id, link.digest) for link in repo.child_links] == [
        (PARENT, req, "light-2", digest), (PARENT, req, "light-1", digest)]
    assert [(r.internal_request_id, r.node_id, r.user_id, r.action, r.approval_id, r.reoperate_only)
            for r in repo.reserved] == [
        (first, "light-2", "visitor", "on", None, False), (second, "light-1", "visitor", "on", None, False)]
    assert pol.calls == ["light-2", "light-1"] and pol.request_ids == [first, second]
    for child, node_id, decision, ctx in zip((first, second), ("light-2", "light-1"), sd.children, sd.contexts):
        saved = repo.decisions[child]
        assert saved == decision
        assert (saved.node_id, saved.request_id, saved.verdict) == (node_id, "r1", Verdict.OK)
        assert (ctx.internal_request_id, ctx.node_id, ctx.request.request_id) == (child, node_id, "r1")
        assert ctx.request == req and ctx.request.target == req.target and ctx.request.arguments is None
        assert ctx.execution_kind is ExecutionKind.SHARED and ctx.use_id is None
        assert ctx.settings.version == "t"
        # 하위 감사 이벤트는 하위 요청 ID 로 남는다
        assert events(audit, child), "하위 요청의 감사 이벤트가 있다"
    assert repo.decisions[PARENT].node_id is None
    assert [d.internal_request_id for d in repo.saved] == [first, second, PARENT]  # 상위 판정은 마지막에 저장한다
    assert set(repo.records) == {PARENT, first, second}


def test_FR18_selector_one_fails_none_executes():
    pol = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    sd = pipe.decide_selector(request("near:light:1:3"))  # 가까운 순서: light-2, light-1, light-3
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)
    assert d.devices == (device("light-1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert d.selected_count == 3 and d.node_id is None
    assert sd.contexts == () and sd.children == ()
    # 걸린 기기 뒤의 light-3 은 하위 요청을 만들지 않고, 걸린 기기는 예약하지 않는다
    assert [link.node_id for link in repo.child_links] == ["light-2", "light-1"]
    assert [r.node_id for r in repo.reserved] == ["light-2"]
    # 앞 하위 요청(light-2)의 예약만 한 번 미전송으로 취소한다
    assert store.cancels == [(child_ids(repo)[0], ExecutionStatus.PENDING, ReasonCode.APPROVAL_REQUIRED, {
        "source": SIBLING_EVIDENCE, "reason": "APPROVAL_REQUIRED", "node_id": "light-1"})]
    # 하위 판정 기록은 OK 와 걸린 판정으로 남는다
    first, second = child_ids(repo)
    assert repo.decisions[first].verdict is Verdict.OK
    assert (repo.decisions[second].verdict, repo.decisions[second].node_id) == (Verdict.NEED_APPROVAL, "light-1")


def test_FR18_selector_second_device_busy_none_executes():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    repo.reserve_results["light-1"] = ReserveOutcome.DEVICE_BUSY
    sd = pipe.decide_selector(request("near:light:1:3"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY)
    assert d.devices == (device("light-1", Verdict.BUSY, ReasonCode.DEVICE_BUSY),)
    assert sd.contexts == ()
    assert [link.node_id for link in repo.child_links] == ["light-2", "light-1"]
    assert [c[0] for c in store.cancels] == [child_ids(repo)[0]]
    assert store.cancels[0][3]["source"] == SIBLING_EVIDENCE and store.cancels[0][3]["node_id"] == "light-1"


def test_FR18_selector_first_device_fails_cancels_nothing():
    pol = FakePolicy({"light-2": PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, 2)})
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    sd = pipe.decide_selector(request("near:light:1:3"))
    assert (sd.decision.verdict, sd.decision.reason) == (Verdict.DENY, ReasonCode.POLICY_DENIED)
    assert sd.decision.devices == (device("light-2", Verdict.DENY, ReasonCode.POLICY_DENIED),)
    assert [link.node_id for link in repo.child_links] == ["light-2"]  # 뒤 기기는 하위 요청을 만들지 않는다
    assert repo.reserved == [] and store.cancels == []


def test_FR09_selector_unsupported_action_denied_per_device():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    sd = pipe.decide_selector(request("near:light:1:2", action="open"))  # 조명은 open 을 선언하지 않았다
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.UNSUPPORTED_ACTION)
    assert d.devices == (device("light-2", Verdict.DENY, ReasonCode.UNSUPPORTED_ACTION),)
    assert d.selected_count == 2
    assert [link.node_id for link in repo.child_links] == ["light-2"]
    assert repo.reserved == []


def test_FR18_selector_reoperate_role_reaches_reservation():
    pipe, repo, *_ = build(lockers()[:1], {"locker-1": -50.0}, {"locker-1": 0.97})
    repo.reserve_results["locker-1"] = ReserveOutcome.REOPERATING
    sd = pipe.decide_selector(request("near:locker:1:1", action="close"))  # close 는 재조작 역할이다
    assert sd.decision.verdict is Verdict.OK
    assert repo.reserved[0].reoperate_only is True
    assert sd.contexts[0].execution_kind is ExecutionKind.RENTAL_REOPERATION
    assert sd.decision.devices == (device("locker-1", use_id="use-locker-1"),)


def test_FR18_selector_candidates_exclude_unready_devices():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    pipe._readiness = FakeReadiness(("light-2",))  # 재시작 복원 중이라 새 실행을 할 수 없다
    sd = pipe.decide_selector(request("near:light:1:2"))
    assert prox.order_calls == [["light-1", "light-3"]]
    assert [link.node_id for link in repo.child_links] == ["light-1", "light-3"]
    assert sd.decision.verdict is Verdict.OK


# ── ①②에서 멈추는 경우와 ⓪의 결과 ────────────────────────────────────


def test_FR18_selector_beacon_not_seen_stops_before_candidates():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    repo.frames = []
    sd = pipe.decide_selector(request("near:light:1:2"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert d.devices == () and d.selected_count == 0 and d.similar is False
    assert prox.order_calls == [] and repo.child_links == []
    assert repo.records[PARENT].selector is None
    last = events(audit, PARENT)[-1]
    assert last.step is Step.BEACON and last.payload["children"] == 0


def test_FR22_selector_capacity_exceeded_denied_and_saved():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    repo.capacity_exceeded = True
    sd = pipe.decide_selector(request("near:light:1:2"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED)
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert repo.decisions[PARENT] == d and repo.records[PARENT].selector is None
    assert "beacons_of" not in repo.calls and repo.child_links == []
    last = events(audit, PARENT)[-1]
    assert last.step is Step.DEDUP and last.payload["capacity_exceeded"] is True


def test_FR22_selector_in_progress_resend_is_busy_and_not_saved():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    req = request("near:light:1:2")
    repo.keys[(req.user_id, req.request_id)] = ("i9", input_digest(req))  # 같은 키가 있는데 판정이 아직 없다
    sd = pipe.decide_selector(req)
    d = sd.decision
    assert (d.verdict, d.reason, d.internal_request_id) == (
        Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS, "i9")
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert repo.calls == ["link_request"] and repo.saved == [] and store.cancels == []
    assert [(e.step, e.payload) for e in audit.events] == [(Step.DEDUP, {"result": "IN_PROGRESS"})]


def test_FR22_selector_digest_includes_start_count():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    assert pipe.decide_selector(request("near:light:1:2")).decision.verdict is Verdict.OK
    links, reserved, saved = list(repo.child_links), list(repo.reserved), list(repo.saved)
    base = input_digest(request("near:light:1:2"))
    for target in ("near:light:2:2", "near:light:1:all", "near:light:1:3", "far:light:1:2"):
        assert input_digest(request(target)) != base
        other = pipe.decide_selector(request(target))  # 같은 request_id 에 다른 입력이다
        assert (other.decision.verdict, other.decision.reason) == (
            Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
        assert (other.decision.devices, other.decision.selected_count, other.decision.similar) == ((), 0, False)
        assert other.children == () and other.contexts == ()
    assert repo.child_links == links and repo.reserved == reserved and repo.saved == saved  # 아무것도 새로 만들지 않았다
    assert events(audit, PARENT, Step.DEDUP)[-1].payload == {"result": "REQUEST_ID_CONFLICT"}

    # 같은 요청을 다시 보내면 충돌이 아니라 재전송이다. 새 하위 요청도 예약도 없다
    again = pipe.decide_selector(request("near:light:1:2"))
    assert (again.decision.verdict, again.decision.reason) == (Verdict.OK, ReasonCode.OK)
    assert again.contexts == ()
    assert repo.child_links == links and repo.reserved == reserved and repo.saved == saved


def test_FR09_selector_old_entry_rejects_selector():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    for target in ("near:light:1:2", "far:light:1:all"):
        decision, ctx = pipe.decide_for_execution(request(target))
        assert (decision.verdict, decision.reason, decision.internal_request_id) == (
            Verdict.DENY, ReasonCode.INVALID_INPUT, "")
        assert decision.request_id == "r1" and ctx is None
        assert pipe.decide(request(target)) == decision
    assert repo.calls == [] and audit.events == []  # link_request 를 비롯해 저장소를 부르지 않는다


def test_FR09_selector_decide_selector_rejects_other_targets():
    pipe, repo, *_ = build(lights(), LIGHT_VALUES)
    for target in ("nearest:light", "any:light", "light-1"):
        with pytest.raises(ValueError):
            pipe.decide_selector(request(target))
    assert repo.calls == []


# ── 재전송 (FR-22) ────────────────────────────────────────────────────


def test_FR22_selector_resend_returns_stored_decisions():
    values = {"light-1": -60.0, "light-2": -50.0, "light-3": -62.0}
    pipe, repo, prox, store, audit = build(lights(), values)
    first = pipe.decide_selector(request("near:light:1:2"))
    assert first.decision.similar is True
    before = (list(repo.child_links), list(repo.reserved), list(repo.saved), list(audit.events))
    again = pipe.decide_selector(request("near:light:1:2"))
    assert again.decision == first.decision  # devices·selected_count·similar 도 같다
    assert again.decision.devices == (device("light-2"), device("light-1"))
    assert (again.decision.selected_count, again.decision.similar) == (2, True)
    assert again.children == first.children and again.contexts == ()
    assert (list(repo.child_links), list(repo.reserved), list(repo.saved), list(audit.events)) == before


def test_FR22_selector_resend_decision_after_check_failure():
    pol = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    first = pipe.decide_selector(request("near:light:1:3"))
    before = (list(repo.child_links), list(repo.reserved), list(repo.saved), len(store.cancels))
    again = pipe.decide_selector(request("near:light:1:3"))
    assert again.decision == first.decision
    assert again.decision.devices == (device("light-1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert again.decision.selected_count == 3
    assert again.children == () and again.contexts == ()
    assert (list(repo.child_links), list(repo.reserved), list(repo.saved), len(store.cancels)) == before


def test_FR22_selector_resend_before_pick_returns_plain_decision():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    repo.frames = []  # ① 에서 멈춘 판정이라 거리 선택자 기록이 없다
    first = pipe.decide_selector(request("near:light:1:2"))
    again = pipe.decide_selector(request("near:light:1:2"))
    assert again.decision == first.decision
    assert (again.decision.verdict, again.decision.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert (again.decision.devices, again.decision.selected_count, again.decision.similar) == ((), 0, False)


def test_FR22_selector_resend_no_candidate_keeps_empty_selection():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    first = pipe.decide_selector(request("near:light:4:1"))
    again = pipe.decide_selector(request("near:light:4:1"))
    assert again.decision == first.decision
    assert (again.decision.verdict, again.decision.reason) == (Verdict.DENY, ReasonCode.NO_CANDIDATE)
    assert (again.decision.devices, again.decision.selected_count, again.decision.similar) == ((), 0, False)


@pytest.mark.parametrize("damage", ["missing_child_decision", "child_not_ok"])
def test_FR22_selector_resend_inconsistent_ok_is_internal_error(damage):
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    pipe.decide_selector(request("near:light:1:2"))
    first, second = child_ids(repo)
    if damage == "missing_child_decision":  # 하위 판정이 저장되지 않았다
        del repo.decisions[second]
    else:  # 하위 판정이 OK 가 아니다
        repo.decisions[second] = Decision(second, "r1", Verdict.BUSY, ReasonCode.DEVICE_BUSY, node_id="light-1")
    saved, attempts = list(repo.saved), list(repo.save_attempts)
    again = pipe.decide_selector(request("near:light:1:2"))
    d = again.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)  # OK 가 되지 않는다
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert again.children == () and again.contexts == ()
    assert repo.saved == saved and repo.save_attempts == attempts  # 저장하지 않는다. 시도도 하지 않는다
    assert events(audit, PARENT)[-1].payload["result"] == "INTERNAL_ERROR"


# ── 상위 판정 기록 (FR-22) ─────────────────────────────────────────────


def test_FR22_selector_parent_record_has_order():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    pipe.decide_selector(request("near:light:1:2"))
    selector = repo.records[PARENT].selector
    assert selector.target_spec == "near:light:1:2"
    assert selector.order == ("light-2", "light-1", "light-3")
    assert dict(selector.values) == LIGHT_VALUES
    assert selector.selected == ("light-2", "light-1")
    assert selector.similar is False
    assert selector.children == tuple(child_ids(repo))  # 만든 하위 요청의 순서다

    # far 는 요청 방향의 순서, 곧 near 순서를 뒤집은 것이다
    pipe, repo, *_ = build(lights(), LIGHT_VALUES)
    pipe.decide_selector(request("far:light:2:2"))
    selector = repo.records[PARENT].selector
    assert selector.target_spec == "far:light:2:2"
    assert selector.order == ("light-3", "light-1", "light-2")
    assert selector.selected == ("light-1", "light-2")
    assert selector.children == tuple(child_ids(repo))


def test_FR22_selector_parent_record_keeps_only_created_children():
    pol = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, *_ = build(lights(), LIGHT_VALUES, policy=pol)
    pipe.decide_selector(request("near:light:1:3"))
    selector = repo.records[PARENT].selector
    assert selector.selected == ("light-2", "light-1", "light-3")  # 고른 기기는 모두 남기고
    assert selector.children == tuple(child_ids(repo)) and len(selector.children) == 2  # 하위 요청은 만든 만큼이다


def test_FR22_selector_far_unheard_devices_come_first():
    values = {"light-1": -60.0, "light-2": None, "light-3": None}  # 비콘을 못 본 기기는 near 순서의 끝이다
    pipe, repo, *_ = build(lights(), values)
    pipe.decide_selector(request("far:light:1:2"))
    selector = repo.records[PARENT].selector
    assert selector.order == ("light-3", "light-2", "light-1")
    assert selector.selected == ("light-3", "light-2")
    assert selector.similar is False  # (light-2 None, light-1 -60) 한쪽만 None


def test_FR22_selector_audit_records_order_and_children():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    pipe.decide_selector(request("near:light:1:2"))
    first, second = child_ids(repo)
    parent = events(audit, PARENT)
    assert [e.step for e in parent] == [Step.BEACON, Step.CANDIDATE, Step.PROXIMITY, Step.DEVICE_OCCUPANCY]
    beacon, candidate, order, final = (thaw(e.payload) for e in parent)  # 감사 payload는 동결돼 있다
    assert beacon["anchors"] == ["light-1", "light-2", "light-3"]
    assert candidate == {"place_id": "p", "candidates": ["light-1", "light-2", "light-3"], "online_ttl_sec": 15.0}
    assert order == {
        "selector": "near:light:1:2", "order": ["light-2", "light-1", "light-3"],
        "values": LIGHT_VALUES, "selected": ["light-2", "light-1"], "similar": False,
        "anchor_recent_sec": 3.0, "order_similar_db": 3.0}
    assert final == {"verdict": "OK", "reason": "OK", "settings_version": "t", "children": 2}
    for child, node_id in ((first, "light-2"), (second, "light-1")):
        steps = [e.step for e in events(audit, child)]
        assert steps == [Step.PROXIMITY, Step.POLICY, Step.DEVICE_OCCUPANCY, Step.POLICY]
        reserve = events(audit, child, Step.DEVICE_OCCUPANCY)[0].payload
        assert reserve == {"node_id": node_id, "outcome": "reserved"}
        assert events(audit, child)[-1].payload == {
            "verdict": "OK", "reason": "OK", "settings_version": "t", "selector_parent": PARENT}
    # 하위 요청은 거리 순서대로 차례로 판정하고 상위 감사가 마지막이다
    assert audit.events[-1] is parent[-1]
    assert audit.events.index(events(audit, first)[-1]) < audit.events.index(events(audit, second)[0])


def test_FR22_selector_parent_audit_records_stopped_step():
    pol = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    pipe.decide_selector(request("near:light:1:3"))
    last = events(audit, PARENT)[-1]
    assert last.step is Step.POLICY
    assert last.payload == {"verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED",
                            "settings_version": "t", "children": 2}
    last_child = events(audit, child_ids(repo)[1])[-1]
    assert last_child.step is Step.POLICY and last_child.payload["selector_parent"] == PARENT

    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    repo.reserve_results["light-2"] = ReserveOutcome.USER_BUSY
    pipe.decide_selector(request("near:light:1:2"))
    assert events(audit, PARENT)[-1].step is Step.USER_OCCUPANCY


# ── 오류와 취소 (FR-18, #80) ──────────────────────────────────────────


def test_FR18_selector_internal_error_cancels_children():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=ExplodingPolicy("light-1"))
    sd = pipe.decide_selector(request("near:light:1:3"))  # 가까운 순서: light-2, light-1, light-3
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert (d.devices, d.selected_count, d.similar) == ((), 0, False)
    assert "secret" not in repr(d) and d.message == "요청을 처리하지 못했습니다."
    assert sd.contexts == () and sd.children == ()
    first, second = child_ids(repo)
    assert len(repo.child_links) == 2  # light-3 의 하위 요청은 만들지 않았다
    # 예약을 부른 첫 하위 요청만 취소한다. 근거는 판정 오류다
    assert store.cancels == [(first, ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR, {
        "source": "decision_error", "reason": "INTERNAL_ERROR"})]
    # 첫 하위 요청은 OK 로 저장했고, 둘째에는 그 기기의 INTERNAL_ERROR 를 저장한다
    assert repo.decisions[first].verdict is Verdict.OK
    assert (repo.decisions[second].verdict, repo.decisions[second].reason, repo.decisions[second].node_id) == (
        Verdict.DENY, ReasonCode.INTERNAL_ERROR, "light-1")
    assert (repo.records[second].model_version, repo.records[second].selector) == (None, None)
    assert (repo.decisions[PARENT].verdict, repo.decisions[PARENT].reason) == (
        Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert repo.records[PARENT].selector is None
    assert any("secret" in str(e.payload) for e in audit.events)  # 예외 내용은 감사에만 남는다


@pytest.mark.parametrize("failure", ["parent_save", "parent_audit", "child_save", "child_audit"])
def test_FR18_selector_error_after_reservation_cancels_every_reservation(failure):
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    real_save, real_append = repo.save_decision, audit.append
    boom = {"count": 0}

    def save(decision, record):
        if failure == "parent_save" and decision.internal_request_id == PARENT and not boom["count"]:
            boom["count"] += 1
            raise OSError("disk")
        if failure == "child_save" and decision.internal_request_id == "c2" and not boom["count"]:
            boom["count"] += 1
            raise OSError("disk")
        return real_save(decision, record)

    def append(event):
        last = event.step is Step.DEVICE_OCCUPANCY and event.internal_request_id == PARENT
        if failure == "parent_audit" and last:
            raise OSError("disk")
        if failure == "child_audit" and event.step is Step.DEVICE_OCCUPANCY and event.internal_request_id == "c2":
            raise OSError("disk")
        real_append(event)

    repo.save_decision, audit.append = save, append
    sd = pipe.decide_selector(request("near:light:1:2"))
    assert (sd.decision.verdict, sd.decision.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert sd.contexts == () and sd.children == ()
    cancelled = {c[0] for c in store.cancels}
    # 예약을 부른 하위 요청은 모두 취소하고, 근거는 판정 오류다
    assert cancelled == set(child_ids(repo)) and len(store.cancels) == len(cancelled)
    assert all(c[3]["source"] == "decision_error" and c[2] is ReasonCode.INTERNAL_ERROR for c in store.cancels)
    assert (repo.decisions[PARENT].verdict, repo.decisions[PARENT].reason) == (
        Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    for child in child_ids(repo):  # 모든 하위 요청에 판정이 하나씩 있다
        assert child in repo.decisions


def test_FR18_selector_error_cancels_before_saving_error_decision():
    """오류 판정이 먼저 저장되면 그 사이에 온 같은 키의 재판정(#76)이 아직 취소되지 않은 예약을 받는다."""
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=ExplodingPolicy("light-1"))
    saved_at_cancel = []
    cancel = store.cancel_not_sent

    def spy(*args):
        saved_at_cancel.append([d.internal_request_id for d in repo.saved])
        return cancel(*args)

    store.cancel_not_sent = spy
    pipe.decide_selector(request("near:light:1:2"))
    first = child_ids(repo)[0]
    assert saved_at_cancel == [[first]]  # 취소할 때 오류 판정(둘째 하위 요청과 상위)은 아직 저장 전이다
    assert [d.internal_request_id for d in repo.saved] == [first, child_ids(repo)[1], PARENT]


def test_FR18_selector_sibling_cancel_precedes_saving_parent():
    pol = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    saved_at_cancel = []
    cancel = store.cancel_not_sent

    def spy(*args):
        saved_at_cancel.append(PARENT in {d.internal_request_id for d in repo.saved})
        return cancel(*args)

    store.cancel_not_sent = spy
    pipe.decide_selector(request("near:light:1:2"))
    assert saved_at_cancel == [False]  # 상위 판정을 저장하기 전에 취소한다


@pytest.mark.parametrize("failure", ["exception", "conflict", "no_store"])
def test_FR18_selector_sibling_cancel_failure_keeps_response(failure, caplog):
    pol = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    if failure == "exception":
        store.error = RuntimeError("secret internal detail")
    elif failure == "conflict":
        store.outcome = StoreOutcome.CONFLICT
    else:
        pipe._store = None  # 취소할 저장소가 없다
    sd = pipe.decide_selector(request("near:light:1:2"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED)  # 응답은 바뀌지 않는다
    assert d.devices == (device("light-1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED),)
    assert "secret" not in repr(d)
    assert repo.decisions[PARENT] == d
    assert len(store.cancels) == (0 if failure == "no_store" else 1)
    if failure != "no_store":
        assert any("예약 취소" in r.getMessage() for r in caplog.records)


def test_FR18_selector_error_after_sibling_cancel_does_not_cancel_again():
    pol = FakePolicy({"light-1": PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 2)})
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES, policy=pol)
    real_save, calls = repo.save_decision, []

    def save(decision, record):
        calls.append(decision.internal_request_id)
        if decision.internal_request_id == PARENT and calls.count(PARENT) == 1:
            raise OSError("disk")
        return real_save(decision, record)

    repo.save_decision = save
    d = pipe.decide_selector(request("near:light:1:2")).decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert len(store.cancels) == 1 and store.cancels[0][3]["source"] == SIBLING_EVIDENCE  # 한 번만 취소한다
    assert repo.decisions[PARENT].reason is ReasonCode.INTERNAL_ERROR


def test_FR18_selector_parent_save_conflict_does_not_overwrite():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    other = pipe.decide_selector(request("near:light:1:2", request_id="r0")).decision  # i1 은 r0 가 쓴다
    assert other.verdict is Verdict.OK
    # i2 가 될 요청의 상위 판정을 미리 저장해 두어 충돌시킨다
    repo.decisions["i2"] = Decision("i2", "r1", Verdict.DENY, ReasonCode.POLICY_DENIED)
    saved = len(repo.saved)
    sd = pipe.decide_selector(request("near:light:1:2", request_id="r1"))
    assert (sd.decision.verdict, sd.decision.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert sd.contexts == ()
    new_children = [link.child for link in repo.child_links if link.parent == "i2"]
    assert {c[0] for c in store.cancels} == set(new_children)  # 새로 예약한 하위 요청을 모두 취소한다
    assert repo.decisions["i2"].reason is ReasonCode.POLICY_DENIED  # 기존 판정을 덮어쓰지 않는다
    # 새로 저장한 것은 하위 요청의 OK 판정뿐이다. 상위 오류 판정은 저장하지 않는다
    assert [d.internal_request_id for d in repo.saved[saved:]] == new_children
    assert repo.save_attempts.count("i2") == 1  # 충돌한 저장 한 번뿐이다. 오류 판정을 다시 저장하려 하지 않는다


def test_FR18_selector_child_save_conflict_saves_parent_error():
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)
    repo.decisions["c1"] = Decision("c1", "r1", Verdict.DENY, ReasonCode.POLICY_DENIED)  # 첫 하위 요청의 저장이 충돌한다
    sd = pipe.decide_selector(request("near:light:1:2"))
    assert (sd.decision.verdict, sd.decision.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert [c[0] for c in store.cancels] == ["c1"]  # 충돌한 하위 요청의 예약도 취소한다
    assert repo.decisions["c1"].reason is ReasonCode.POLICY_DENIED  # 기존 판정을 덮어쓰지 않는다
    assert (repo.decisions[PARENT].verdict, repo.decisions[PARENT].reason) == (
        Verdict.DENY, ReasonCode.INTERNAL_ERROR)  # 상위 판정 저장은 충돌이 아니므로 오류 판정을 남긴다
    assert len(repo.child_links) == 1  # 뒤 기기의 하위 요청은 만들지 않았다


@pytest.mark.parametrize("name", ["link_request", "link_selector_child", "beacons_of", "nodes", "reserve_if_free"])
def test_FR18_selector_repository_error_is_internal_error(name):
    pipe, repo, prox, store, audit = build(lights(), LIGHT_VALUES)

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    setattr(repo, name, boom)
    sd = pipe.decide_selector(request("near:light:1:2"))
    d = sd.decision
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert "secret" not in repr(d) and sd.contexts == ()
    # ⓪ 의 오류는 요청 행이 없으니 저장하지 않고, 그 뒤의 오류는 상위 판정에 오류를 남긴다
    assert (PARENT in repo.decisions) == (name != "link_request")
    if name == "link_request":
        assert repo.save_attempts == []  # 요청 행이 없으니 저장을 시도하지도 않는다
    if name == "reserve_if_free":
        assert [c[0] for c in store.cancels] == [repo.child_links[0].child]
    else:
        assert store.cancels == []
