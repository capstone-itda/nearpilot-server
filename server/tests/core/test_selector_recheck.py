"""FR-18 거리 선택자 하위 요청의 실행 직전 재검사 (specs/core-distance-selector/spec.md).

`tests/core/test_recheck.py` 와 같은 구성이다. 하위 요청의 `ExecutionContext.request` 는 상위 `UseRequest` 이고
`internal_request_id` 는 하위 요청의 ID(`c1`)다. 재검사는 대상을 다시 고르지 않고 저장소를 바꾸지 않는다.
공용형 하위 요청은 비콘 수신만, 대여형 하위 요청은 `nearest:` 와 같이 근접 임계값을 다시 확인한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.decision.recheck import Rechecker
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    DeviceState,
    ExecutionKind,
    Occupancy,
    ReasonCode,
    SessionState,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import (
    BeaconKey,
    ExecutionContext,
    NodeInfo,
    PolicyCheckResult,
    ProximityResult,
    RssiFrame,
    TargetSpec,
    UseRequest,
    UseSession,
)

NOW = datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)
BEACON = BeaconKey("u", 1, 1)
CHILD = "c1"  # 하위 요청의 내부 ID. 외부 request_id 는 상위 요청의 r1 이다


def node(node_id, kind, occupancy, **kw) -> NodeInfo:
    """후보 조건을 모두 채운 기기. 대여형은 자기 예약(RESERVED) 상태다."""
    base = dict(
        node_id=node_id, place_id="p", kind=kind, occupancy=occupancy, trust_state=TrustState.TRUSTED,
        device_state=DeviceState.RESERVED if occupancy is Occupancy.RENTAL else DeviceState.AVAILABLE,
        exclusive_group="g", last_seen_at=NOW - timedelta(seconds=1))
    base.update(kw)
    return NodeInfo(**base)


def lamps() -> list[NodeInfo]:
    return [node("lamp-1", "lamp", Occupancy.SHARED), node("lamp-2", "lamp", Occupancy.SHARED)]


def lockers() -> list[NodeInfo]:
    """locker-1 이 하위 요청의 대상이고 자기 예약 상태다."""
    return [node("locker-1", "locker", Occupancy.RENTAL),
            node("locker-2", "locker", Occupancy.RENTAL, device_state=DeviceState.AVAILABLE)]


def frame(node_id) -> RssiFrame:
    return RssiFrame(node_id, BEACON, -60.0, NOW, "s1", 1, NOW, NOW)


def ctx(target, node_id, action, use_id, kind) -> ExecutionContext:
    req = UseRequest(user_id="u1", request_id="r1", target=TargetSpec.parse(target), action=action,
                     host_kind="claude", received_at=NOW - timedelta(seconds=4))
    return ExecutionContext(CHILD, req, node_id, use_id, None, Settings(version="v1"), execution_kind=kind)


def shared_ctx(target="near:lamp:1:2", node_id="lamp-1") -> ExecutionContext:
    return ctx(target, node_id, "on", None, ExecutionKind.SHARED)


def rental_ctx(target="near:locker:1:2", node_id="locker-1") -> ExecutionContext:
    return ctx(target, node_id, "open", "use-1", ExecutionKind.RENTAL_INITIAL)


class FakeRepo:
    def __init__(self, nodes):
        self.nodes_list = list(nodes)
        self.frames = [frame(n.node_id) for n in nodes]  # 모든 기기가 비콘을 봤다
        self.beacons = [BEACON]
        self.session_obj = UseSession("use-1", CHILD, "locker-1", SessionState.RESERVED)
        self.owner_ok = True
        self.calls: list[str] = []

    def beacons_of(self, user_id):
        self.calls.append("beacons_of")
        return self.beacons

    def rssi_since(self, beacons, since):
        self.calls.append("rssi_since")
        return self.frames

    def nodes(self, kind=None):
        self.calls.append("nodes")
        return self.nodes_list

    def session(self, use_id):
        self.calls.append("session")
        return self.session_obj

    def session_owner_matches(self, use_id, user_id):
        self.calls.append("session_owner_matches")
        return self.owner_ok

    def save_decision(self, *a, **k):  # 재검사는 저장하지 않는다
        raise AssertionError("재검사가 저장소를 바꿨다")

    def reserve_if_free(self, *a, **k):
        raise AssertionError("재검사가 ⑤⑥을 다시 실행했다")

    def link_selector_child(self, *a, **k):
        raise AssertionError("재검사가 하위 요청을 만들었다")


class FakeProximity:
    """`posteriors` 가 None 이면 `posterior` 를 부르는 것이 오류다."""

    def __init__(self, posteriors=None):
        self.posteriors = posteriors
        self.calls: list[list[str]] = []

    def posterior(self, beacons, candidates, at, *, place_nodes):
        self.calls.append([n.node_id for n in candidates])
        if self.posteriors is None:
            raise AssertionError("사후확률을 계산했다")
        return ProximityResult(self.posteriors, at, at, "m", "c")


class FakePolicy:
    def __init__(self, result=None):
        self.result = result or PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)
        self.calls: list[str] = []
        self.request_ids: list[str | None] = []

    def check(self, user_id, node_id, action, now, *, internal_request_id=None, reoperate=False):
        self.calls.append(node_id)
        self.request_ids.append(internal_request_id)
        return self.result


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def build(nodes, posteriors=None, policy=None):
    repo, prox, audit = FakeRepo(nodes), FakeProximity(posteriors), FakeAudit()
    pol = policy or FakePolicy()
    ids = iter(range(10_000))
    rc = Rechecker(repo, prox, pol, audit, clock=lambda: NOW, new_id=lambda: f"e{next(ids)}")
    return rc, repo, prox, pol, audit


# ── 공용형 하위 요청: 비콘 수신만 본다 ────────────────────────────────


@pytest.mark.parametrize("kind,target", [
    pytest.param("lamp", "near:lamp:1:2", id="임계값 설정이 없는 종류"),
    pytest.param("light", "far:light:1:1", id="임계값 설정이 있는 종류"),
])
@pytest.mark.parametrize("case,verdict,reason", [
    ("beacon_seen", Verdict.OK, ReasonCode.OK),
    ("beacon_gone", Verdict.DENY, ReasonCode.BEACON_NOT_SEEN),
    ("target_unavailable", Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
])
def test_FR18_selector_recheck_shared_beacon_only(kind, target, case, verdict, reason):
    first, other = f"{kind}-1", f"{kind}-2"
    rc, repo, prox, pol, audit = build(  # posteriors 가 없어서 posterior 를 부르면 fake 가 실패한다
        [node(first, kind, Occupancy.SHARED), node(other, kind, Occupancy.SHARED)])
    if case == "beacon_gone":
        repo.frames = []
    elif case == "target_unavailable":  # 대상이 ② 후보에서 빠진다. 다른 기기가 비콘을 봐서 ① 은 통과한다
        repo.nodes_list[0] = node(first, kind, Occupancy.SHARED, device_state=DeviceState.FAULT)
        repo.frames = [frame(other)]
    d = rc.recheck(shared_ctx(target, first))
    assert (d.verdict, d.reason, d.node_id) == (verdict, reason, first)
    assert prox.calls == []
    assert pol.calls == ([first] if case == "beacon_seen" else [])
    if case == "beacon_seen":
        payload = audit.events[-1].payload
        assert (payload["skipped"], payload["reason_detail"]) == (True, "selector_shared")
        assert "p" not in payload and "threshold" not in payload


@pytest.mark.parametrize("target,verdict,reason", [
    pytest.param("nearest:light", Verdict.ASK, ReasonCode.MOVE_CLOSER, id="근접 지정은 공용형 기기도 확률을 확인한다"),
    pytest.param("near:light:1:1", Verdict.OK, ReasonCode.OK, id="거리 선택자의 공용형 하위 요청은 확인하지 않는다"),
])
def test_FR18_selector_recheck_shared_skip_applies_only_to_selector(target, verdict, reason):
    rc, repo, prox, pol, audit = build([node("light-1", "light", Occupancy.SHARED)], {"light-1": 0.4})
    d = rc.recheck(shared_ctx(target, "light-1"))
    assert (d.verdict, d.reason) == (verdict, reason)
    assert len(prox.calls) == (1 if verdict is Verdict.ASK else 0)


def test_FR18_selector_recheck_shared_still_checks_policy():
    policy = FakePolicy(PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, 2))
    rc, repo, prox, pol, audit = build(lamps(), policy=policy)
    d = rc.recheck(shared_ctx())
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (
        Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, "lamp-1", None)
    assert pol.calls == ["lamp-1"] and pol.request_ids == [CHILD]  # 하위 요청의 ID 로 자기 사용분을 뺀다
    assert audit.events[-1].payload["reason_detail"] == "selector_shared"  # 막혀도 근접 생략 근거를 남긴다
    assert prox.calls == []


# ── 대여형 하위 요청: nearest: 와 같이 임계값을 다시 확인한다 ─────────────


@pytest.mark.parametrize("p,verdict,reason", [
    (0.5, Verdict.ASK, ReasonCode.MOVE_CLOSER),
    (0.97, Verdict.OK, ReasonCode.OK),
])
def test_FR18_selector_recheck_rental_move_closer(p, verdict, reason):
    rc, repo, prox, pol, audit = build(lockers(), {"locker-1": p})
    d = rc.recheck(rental_ctx())
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (verdict, reason, "locker-1", p)
    assert d.use_id == ("use-1" if verdict is Verdict.OK else None)
    assert prox.calls == [["locker-1", "locker-2"]]  # 후보는 ② 의 전체 후보다
    payload = audit.events[-1].payload
    assert (payload["p"], payload["threshold"]) == (p, 0.95)
    assert pol.calls == (["locker-1"] if verdict is Verdict.OK else [])  # 확률에서 막으면 ④ 에 가지 않는다


def test_FR18_selector_recheck_rental_without_threshold_fails():
    rc, repo, prox, pol, audit = build([node("box-1", "box", Occupancy.RENTAL)], {"box-1": 0.99})
    d = rc.recheck(ctx("near:box:1:1", "box-1", "open", "use-1", ExecutionKind.RENTAL_INITIAL))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.RECHECK_FAILED, "box-1")
    assert prox.calls == [] and pol.calls == []


def test_FR18_selector_recheck_does_not_pick_another_device():
    rc, repo, prox, pol, audit = build(lockers(), {"locker-1": 0.4, "locker-2": 0.59})
    d = rc.recheck(rental_ctx())  # 다른 기기가 더 가깝게 들려도 하위 요청의 기기만 본다
    assert (d.verdict, d.reason, d.node_id) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, "locker-1")
    rc, repo, prox, pol, audit = build(lockers(), {"locker-1": 0.96, "locker-2": 0.03})
    d = rc.recheck(rental_ctx())
    assert (d.verdict, d.node_id) == (Verdict.OK, "locker-1") and pol.calls == ["locker-1"]


def test_FR18_selector_recheck_checks_own_reservation_for_rental_only():
    rc, repo, prox, pol, audit = build(lockers(), {"locker-1": 0.97})
    repo.session_obj = UseSession("use-1", CHILD, "locker-1", SessionState.CLOSED)
    d = rc.recheck(rental_ctx())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.RECHECK_FAILED)
    rc, repo, prox, pol, audit = build(lamps())  # 공용형 하위 요청에는 세션이 없다
    assert rc.recheck(shared_ctx()).verdict is Verdict.OK
    assert "session" not in repo.calls


# ── 판정의 요청 ID 는 상위 외부 ID 와 하위 내부 ID 다 ────────────────────


def test_FR18_selector_recheck_decision_keeps_parent_and_child_ids():
    # 상태 관리가 재검사 판정의 요청 ID 를 대조한다 (불일치하면 저장 오류다)
    cases = [
        (build(lamps())[0], shared_ctx()),  # 통과
        (build(lamps())[0], shared_ctx("near:lamp:1:2", "lamp-9")),  # 대상이 없다
        (build(lockers(), {"locker-1": 0.5})[0], rental_ctx()),  # 확률 부족
        (build(lockers())[0], rental_ctx()),  # 검사 오류 (posterior 를 부르면 fake 가 실패한다)
    ]
    for rc, c in cases:
        d = rc.recheck(c)
        assert (d.internal_request_id, d.request_id) == (CHILD, "r1")
