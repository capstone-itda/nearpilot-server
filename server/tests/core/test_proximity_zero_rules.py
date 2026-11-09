"""FR-17 FR-23 판정의 자동 영점 규칙 (근접 지정, 전역 할당). 다른 계층은 fake 로 대신한다.

근접 추정이 알려 주는 `ProximityResult.unzeroed`(자동 영점이 없는 근접 참여 기기)를 판정이 어떻게 쓰는지 본다.
판정 구성은 `test_decision_pipeline.py` 와 같은 방식이다. 다른 시험 파일을 import 하지 않는다.
사후확률의 합은 1 이하여야 해서 두 기기가 모두 0.5 를 넘는 시험 값은 만들 수 없다. 임계값을 낮춰서 쓴다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import (
    DeviceState,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    Step,
    StoreOutcome,
    TrustState,
    Verdict,
)
from nearpilot.shared.frozen import thaw
from nearpilot.shared.models import (
    BeaconKey,
    NodeInfo,
    PolicyCheckResult,
    ProximityResult,
    RequestLink,
    ReserveResult,
    RssiFrame,
    StoreResult,
    TargetSpec,
    UseRequest,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("u", 1, 1)
NEAREST = "nearest:locker"
ANY = "any:locker"


def node(node_id, **kw) -> NodeInfo:
    """후보 조건을 모두 채운 대여형 사물함."""
    base = dict(
        node_id=node_id, place_id="p", kind="locker", occupancy=Occupancy.RENTAL,
        trust_state=TrustState.TRUSTED, device_state=DeviceState.AVAILABLE,
        exclusive_group="g", last_seen_at=NOW - timedelta(seconds=1),
        descriptor=legacy_descriptor("locker", Occupancy.RENTAL),
    )
    base.update(kw)
    return NodeInfo(**base)


def request(target, request_id="r1") -> UseRequest:
    return UseRequest(
        user_id="u1", request_id=request_id, target=TargetSpec.parse(target),
        action="open", host_kind="claude", received_at=NOW,
    )


class FakeRepo:
    """판정이 부르는 저장소 접점. 모든 기기가 비콘을 봤다."""

    def __init__(self, nodes):
        self.nodes_list = nodes
        self.frames = [RssiFrame(n.node_id, BEACON, -60.0, NOW, "s1", 1, NOW, NOW) for n in nodes]
        self.reserved = []
        self.saved = []

    def link_request(self, req, input_digest):
        return RequestLink("i1", True, False)

    def beacons_of(self, user_id):
        return [BEACON]

    def rssi_since(self, beacons, since):
        return self.frames

    def nodes(self, capability=None):
        return self.nodes_list

    def reserve_if_free(self, req):
        self.reserved.append(req)
        return ReserveResult(ReserveOutcome.RESERVED, use_id=f"use-{req.node_id}")

    def save_decision(self, decision, record):
        self.saved.append(decision)
        return StoreResult(StoreOutcome.APPLIED)


class FakeProximity:
    """정해 둔 사후확률과 `unzeroed` 를 돌려준다. `unzeroed` 는 `posteriors` 키의 부분집합이어야 한다 (FR-16)."""

    def __init__(self, posteriors, unzeroed=()):
        self.posteriors = posteriors
        self.unzeroed = frozenset(unzeroed)
        self.calls = []

    def posterior(self, beacons, candidates, at, *, place_nodes):
        self.calls.append([n.node_id for n in candidates])
        return ProximityResult(self.posteriors, at, at, "m", "c", self.unzeroed)


class FakePolicy:
    """정책 검사는 모두 통과한다. 검사한 기기만 기록한다."""

    def __init__(self):
        self.calls = []

    def check(self, user_id, node_id, action, now, *, internal_request_id=None, reoperate=False):
        self.calls.append(node_id)
        return PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)


class FakeReadiness:
    def can_execute(self, node_id):
        return True


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


class FakeWindow:
    """대기 구간 대신 허용쌍 중 확률이 가장 큰 기기를 돌려준다."""

    def __init__(self):
        self.requests = []

    def submit(self, request, group=""):
        self.requests.append(request)
        return max(request.candidates, key=lambda n: (request.candidates[n], n))


def build(posteriors, unzeroed=(), nodes=None, threshold=0.4):
    repo = FakeRepo(nodes or [node("locker-1"), node("locker-2")])
    prox = FakeProximity(posteriors, unzeroed)
    policy = FakePolicy()
    audit = FakeAudit()
    window = FakeWindow()
    ids = iter(range(10_000))
    pipe = DecisionPipeline(
        repo, prox, policy, audit, Settings(version="v1", threshold_locker=threshold), FakeReadiness(),
        new_id=lambda: f"e{next(ids)}", window=window)
    return pipe, repo, prox, policy, audit, window


def proximity_detail(audit):
    """③ 근접 감사 기록 중 사후확률을 기록한 사건의 detail. 감사 payload 는 동결돼 있어서 되돌려 읽는다."""
    (event,) = [e for e in audit.events if e.step is Step.PROXIMITY and "posteriors" in e.payload]
    return thaw(event.payload)


def allocation_details(audit):
    """전역 할당 감사 기록의 detail 들."""
    return [thaw(e.payload["allocation"]) for e in audit.events
            if e.step is Step.DEVICE_OCCUPANCY and "allocation" in e.payload]


# ── FR-05 근접 참여 기기가 아닌 후보 ──────────────────────────────────


def test_FR05_non_participant_not_nearest_candidate():
    # 후보는 셋인데 posteriors 에는 둘만 있다. locker-1 은 근접 참여 기기가 아니고 기기 ID 순서로는 가장 앞이다
    nodes = [node("locker-1"), node("locker-2"), node("locker-3")]

    # 근접 지정: 판정은 후보를 모두 넘기고, 결과에 있는 기기 중 1등을 고른다
    pipe, repo, prox, pol, audit, _ = build({"locker-2": 0.55, "locker-3": 0.30}, nodes=nodes)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-2", 0.55)
    assert prox.calls == [["locker-1", "locker-2", "locker-3"]]
    detail = proximity_detail(audit)
    assert set(detail["posteriors"]) == {"locker-2", "locker-3"}
    assert detail["unzeroed"] == []  # 자동 영점이 없는 기기가 없어도 빈 목록을 남긴다

    # 결과에 있는 기기가 임계값에 못 미치면 ASK 다. 결과에 없는 기기를 대신 고르지 않는다
    pipe, repo, prox, pol, audit, _ = build({"locker-2": 0.30, "locker-3": 0.20}, nodes=nodes)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.30)
    assert repo.reserved == [] and pol.calls == []

    # any: 도 결과에 없는 기기를 허용쌍에 넣지 않는다. 임계값이 아주 낮아도 확률 0 은 넘지 못한다
    pipe, repo, prox, pol, audit, window = build(
        {"locker-2": 0.30, "locker-3": 0.10}, nodes=nodes, threshold=0.05)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-2", 0.30)
    (sent,) = window.requests
    assert dict(sent.candidates) == {"locker-2": 0.30, "locker-3": 0.10}
    assert pol.calls == ["locker-2", "locker-3"]  # 결과에 없는 기기는 정책도 검사하지 않는다
    assert proximity_detail(audit)["unzeroed"] == []


# ── FR-17 근접 지정의 자동 영점 규칙 ──────────────────────────────────


def test_FR17_top_unzeroed_asks():
    # 1등(0.55)에 자동 영점이 없다. 2등(0.44)은 자동 영점이 있고 임계값(0.4)을 넘는다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55, "locker-2": 0.44}, unzeroed={"locker-1"})
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.55)
    assert repo.reserved == [] and pol.calls == [] and repo.saved == [d]  # 2등을 고르지 않는다
    assert proximity_detail(audit)["unzeroed"] == ["locker-1"]


def test_FR17_zeroed_top_uses_threshold():
    posteriors = {"locker-1": 0.55, "locker-2": 0.44}

    # 2등에만 자동 영점이 없다. 1등이 임계값을 넘으면 이전처럼 OK 다
    pipe, repo, prox, pol, audit, _ = build(posteriors, unzeroed={"locker-2"}, threshold=0.5)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.OK, ReasonCode.OK, "locker-1", 0.55)
    assert proximity_detail(audit)["unzeroed"] == ["locker-2"]

    # 1등이 임계값을 넘지 못하면 이전처럼 ASK 다
    pipe, repo, prox, pol, audit, _ = build(posteriors, unzeroed={"locker-2"}, threshold=0.6)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.55)
    assert repo.reserved == []


def test_FR17_explicit_target_ignores_unzeroed():
    # 명시 지정은 근접 추정을 부르지 않는다. 대상이 자동 영점이 없는 기기여도 이전처럼 판정한다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55}, unzeroed={"locker-1"})
    d = pipe.decide(request("locker-1"))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.OK, ReasonCode.OK, "locker-1")
    assert prox.calls == [] and [r.node_id for r in repo.reserved] == ["locker-1"]
    (skipped,) = [e for e in audit.events if e.step is Step.PROXIMITY]
    assert skipped.payload == {"skipped": True, "reason": "explicit_target", "node_id": "locker-1"}


# ── FR-23 전역 할당의 자동 영점 규칙 ──────────────────────────────────


def test_FR23_any_excludes_unzeroed():
    # 1등(locker-1)은 자동 영점이 있고 임계값을 넘고 정책도 OK 지만 점유 중이다.
    # 2등(locker-2)은 임계값을 넘지만 자동 영점이 없다. 점유되지 않았어도 허용쌍이 아니다
    nodes = [node("locker-1", device_state=DeviceState.ACTIVE), node("locker-2")]
    pipe, repo, prox, pol, audit, window = build(
        {"locker-1": 0.55, "locker-2": 0.44}, unzeroed={"locker-2"}, nodes=nodes)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY, None, 0.55)
    assert window.requests == [] and repo.reserved == []
    assert pol.calls == ["locker-1"]  # 자동 영점이 없는 기기는 정책도 검사하지 않는다
    assert all("locker-2" not in a["allowed"] for a in allocation_details(audit))

    # 자동 영점이 있는 다른 기기가 허용쌍에 있으면 그 기기만 배정 후보다. 2등(0.30)이 3등(0.25)보다 가깝다
    nodes = [node("locker-1", device_state=DeviceState.ACTIVE), node("locker-2"), node("locker-3")]
    pipe, repo, prox, pol, audit, window = build(
        {"locker-1": 0.40, "locker-2": 0.30, "locker-3": 0.25}, unzeroed={"locker-2"},
        nodes=nodes, threshold=0.2)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-3", 0.25)
    (sent,) = window.requests
    assert dict(sent.candidates) == {"locker-3": 0.25}
    assert [a["allowed"] for a in allocation_details(audit)] == [["locker-3"]]
    assert [r.node_id for r in repo.reserved] == ["locker-3"]
    assert pol.calls == ["locker-1", "locker-3"]


def test_FR23_any_top_unzeroed_asks():
    # 1등(locker-1)에 자동 영점이 없다. 2등(locker-2)은 자동 영점이 있고 임계값(0.3)을 넘는다
    nodes = [node("locker-1"), node("locker-2"), node("locker-3")]
    pipe, repo, prox, pol, audit, window = build(
        {"locker-1": 0.45, "locker-2": 0.35, "locker-3": 0.15}, unzeroed={"locker-3", "locker-1"},
        nodes=nodes, threshold=0.3)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.45)
    assert window.requests == [] and repo.reserved == [] and pol.calls == []  # 대기 구간에 내지 않는다
    assert proximity_detail(audit)["unzeroed"] == ["locker-1", "locker-3"]
