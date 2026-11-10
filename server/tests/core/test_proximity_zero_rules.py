"""FR-16 FR-17 FR-18 FR-23 판정의 자동 영점 규칙 (근접 지정, 전역 할당, 실행 직전 재검사). 다른 계층은 fake 로 대신한다.

근접 추정이 알려 주는 `ProximityResult.unzeroed`(자동 영점이 없는 근접 참여 기기)와 `conservative`(그 기기마다 보수적 사후확률)를
판정이 어떻게 쓰는지 본다. 임계값 비교는 `threshold_probability`다. 자동 영점이 있으면 사후확률, 없으면 보수적 사후확률이다.
거리 선택자는 자동 영점이 없는 기기도 이전처럼 사후확률로 비교한다 (FR-05).
판정 구성은 `test_decision_pipeline.py` 와 같은 방식이다. 다른 시험 파일을 import 하지 않는다.
거리 선택자 시험은 공유 fake 모듈 `_selector_fakes` 를 쓴다.
Δ를 모르는 모델의 시험(`unknown_bound`)만 fake 대신 실제 `compute_posteriors` 의 결과를 판정에 넣는다.
사후확률의 합은 1 이하여야 해서 두 기기가 모두 0.5 를 넘는 시험 값은 만들 수 없다. 임계값을 낮춰서 쓴다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _selector_fakes as selector_fakes
import pytest

from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.core.decision.recheck import Rechecker
from nearpilot.core.proximity.common_model import load_common_model
from nearpilot.core.proximity.posterior import compute_posteriors
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import (
    DeviceState,
    ExecutionKind,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    SessionState,
    Step,
    StoreOutcome,
    TrustState,
    Verdict,
)
from nearpilot.shared.frozen import thaw
from nearpilot.shared.models import (
    BeaconKey,
    DeviceOutcome,
    ExecutionContext,
    NodeInfo,
    PolicyCheckResult,
    ProximityResult,
    RequestLink,
    ReserveResult,
    RssiFrame,
    StoreResult,
    TargetSpec,
    UseRequest,
    UseSession,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("u", 1, 1)
NEAREST = "nearest:locker"
ANY = "any:locker"
SELECTOR = "near:locker:1:1"


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
    """정해 둔 사후확률과 보수적 사후확률을 돌려준다.

    `conservative` 의 기기가 자동 영점이 없는 기기(`unzeroed`)다. 키는 `posteriors` 키의 부분집합이어야 하고
    값은 같은 기기의 사후확률 이하여야 한다 (FR-16).
    """

    def __init__(self, posteriors, conservative=None):
        self.posteriors = posteriors
        self.conservative = dict(conservative or {})
        self.calls = []

    def posterior(self, beacons, candidates, at, *, place_nodes):
        self.calls.append([n.node_id for n in candidates])
        return ProximityResult(
            self.posteriors, at, at, "m", "c", frozenset(self.conservative), self.conservative)


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


def build(posteriors, conservative=None, nodes=None, threshold=0.4):
    repo = FakeRepo(nodes or [node("locker-1"), node("locker-2")])
    prox = FakeProximity(posteriors, conservative)
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


class RecheckRepo:
    """실행 직전 재검사가 부르는 저장소 접점. 대상(locker-1)은 자기 예약 상태다. 저장소를 바꾸면 실패한다."""

    def __init__(self, nodes):
        self.nodes_list = nodes
        self.frames = [RssiFrame(n.node_id, BEACON, -60.0, NOW, "s1", 1, NOW, NOW) for n in nodes]

    def beacons_of(self, user_id):
        return [BEACON]

    def rssi_since(self, beacons, since):
        return self.frames

    def nodes(self, capability=None):
        return self.nodes_list

    def session(self, use_id):
        return UseSession(use_id, "i1", "locker-1", SessionState.RESERVED)

    def session_owner_matches(self, use_id, user_id):
        return True

    def save_decision(self, *args, **kwargs):
        raise AssertionError("재검사가 저장소를 바꿨다")

    def reserve_if_free(self, *args, **kwargs):
        raise AssertionError("재검사가 ⑤⑥을 다시 실행했다")


def recheck(target, posteriors, conservative=None, threshold=0.4):
    """locker-1 대여형 대상의 재검사. (판정, 근접 fake, 정책 fake, 감사 fake)를 돌려준다."""
    repo = RecheckRepo([node("locker-1", device_state=DeviceState.RESERVED), node("locker-2")])
    prox, pol, audit = FakeProximity(posteriors, conservative), FakePolicy(), FakeAudit()
    ids = iter(range(10_000))
    rechecker = Rechecker(repo, prox, pol, audit, clock=lambda: NOW, new_id=lambda: f"e{next(ids)}")
    ctx = ExecutionContext(
        "i1", request(target), "locker-1", "use-1", None, Settings(version="v1", threshold_locker=threshold),
        execution_kind=ExecutionKind.RENTAL_INITIAL)
    return rechecker.recheck(ctx), prox, pol, audit


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
    assert detail["conservative"] == {}

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
    # 1등(0.55)에 자동 영점이 없고 보수적 사후확률(0.30)이 임계값(0.4)에 못 미친다.
    # 2등(0.44)은 자동 영점이 있고 임계값을 넘는다. 2등을 고르지 않는다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55, "locker-2": 0.44}, conservative={"locker-1": 0.30})
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.30)
    assert repo.reserved == [] and pol.calls == [] and repo.saved == [d]
    assert proximity_detail(audit)["unzeroed"] == ["locker-1"]


def test_FR17_unzeroed_top_passes_with_conservative_probability():
    # 1등(0.55)에 자동 영점이 없어도 보수적 사후확률(0.45)이 임계값(0.4) 이상이면 그 기기로 진행한다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55, "locker-2": 0.30}, conservative={"locker-1": 0.45})
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.OK, ReasonCode.OK, "locker-1")
    assert d.p_max == 0.45  # 판정에 적는 값은 사후확률(0.55)이 아니라 보수적 사후확률이다
    assert [r.node_id for r in repo.reserved] == ["locker-1"] and pol.calls == ["locker-1"]

    # 임계값과 같아도 진행한다 (이상)
    pipe, repo, *_ = build({"locker-1": 0.55, "locker-2": 0.30}, conservative={"locker-1": 0.4})
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-1", 0.4)

    # 1등은 명목 사후확률로 고른다. 2등(0.44, 자동 영점 있음)이 1등의 보수적 값(0.42)보다 커도 1등을 고른다
    pipe, repo, *_ = build({"locker-1": 0.55, "locker-2": 0.44}, conservative={"locker-1": 0.42})
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-1", 0.42)

    # 1등이 아닌 기기가 자동 영점이 없는 것은 1등의 값에 영향이 없다
    pipe, repo, *_ = build({"locker-1": 0.55, "locker-2": 0.44}, conservative={"locker-2": 0.10})
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-1", 0.55)


def test_FR17_zeroed_top_uses_threshold():
    posteriors = {"locker-1": 0.55, "locker-2": 0.44}

    # 2등에만 자동 영점이 없다. 1등이 임계값을 넘으면 이전처럼 OK 다
    pipe, repo, prox, pol, audit, _ = build(posteriors, conservative={"locker-2": 0.20}, threshold=0.5)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.OK, ReasonCode.OK, "locker-1", 0.55)
    assert proximity_detail(audit)["unzeroed"] == ["locker-2"]

    # 1등이 임계값을 넘지 못하면 이전처럼 ASK 다
    pipe, repo, prox, pol, audit, _ = build(posteriors, conservative={"locker-2": 0.20}, threshold=0.6)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.55)
    assert repo.reserved == []


def test_FR17_explicit_target_ignores_unzeroed():
    # 명시 지정은 근접 추정을 부르지 않는다. 대상이 자동 영점이 없는 기기여도 이전처럼 판정한다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55}, conservative={"locker-1": 0.10})
    d = pipe.decide(request("locker-1"))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.OK, ReasonCode.OK, "locker-1")
    assert prox.calls == [] and [r.node_id for r in repo.reserved] == ["locker-1"]
    (skipped,) = [e for e in audit.events if e.step is Step.PROXIMITY]
    assert skipped.payload == {"skipped": True, "reason": "explicit_target", "node_id": "locker-1"}


def test_FR17_proximity_audit_records_conservative():
    # ③ 감사 기록에 unzeroed 옆으로 conservative 를 남긴다. 키는 정렬하고, 입력 순서는 상관없다
    pipe, repo, prox, pol, audit, _ = build(
        {"locker-1": 0.55, "locker-2": 0.40}, conservative={"locker-2": 0.15, "locker-1": 0.45})
    pipe.decide(request(NEAREST))
    detail = proximity_detail(audit)
    assert detail["unzeroed"] == ["locker-1", "locker-2"]
    assert detail["conservative"] == {"locker-1": 0.45, "locker-2": 0.15}
    assert list(detail["conservative"]) == ["locker-1", "locker-2"]

    # 임계값에 못 미쳐 ASK 로 멈춘 판정에도 남는다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55, "locker-2": 0.40}, conservative={"locker-1": 0.20})
    assert pipe.decide(request(NEAREST)).verdict is Verdict.ASK
    assert proximity_detail(audit)["conservative"] == {"locker-1": 0.20}

    # any: 도 같다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55, "locker-2": 0.40}, conservative={"locker-2": 0.15})
    pipe.decide(request(ANY))
    detail = proximity_detail(audit)
    assert (detail["unzeroed"], detail["conservative"]) == (["locker-2"], {"locker-2": 0.15})

    # 자동 영점이 없는 기기가 없으면 빈 매핑이다
    pipe, repo, prox, pol, audit, _ = build({"locker-1": 0.55, "locker-2": 0.40})
    pipe.decide(request(NEAREST))
    assert proximity_detail(audit)["conservative"] == {}


# ── FR-23 전역 할당의 자동 영점 규칙 ──────────────────────────────────


def test_FR23_any_uses_conservative_probability_for_unzeroed():
    # 1등(locker-1)은 자동 영점이 있고 임계값을 넘고 정책도 OK 지만 점유 중이다.
    # 2등(locker-2)은 사후확률(0.44)이 임계값(0.4)을 넘지만 자동 영점이 없고 보수적 사후확률(0.30)은 못 넘는다.
    # 점유되지 않았어도 허용쌍이 아니다
    nodes = [node("locker-1", device_state=DeviceState.ACTIVE), node("locker-2")]
    pipe, repo, prox, pol, audit, window = build(
        {"locker-1": 0.55, "locker-2": 0.44}, conservative={"locker-2": 0.30}, nodes=nodes)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY, None, 0.55)
    assert window.requests == [] and repo.reserved == []
    assert pol.calls == ["locker-1"]  # 보수적 사후확률이 임계값에 못 미치는 기기는 정책도 검사하지 않는다
    assert all("locker-2" not in a["allowed"] for a in allocation_details(audit))

    # 보수적 사후확률이 임계값 이상이면 자동 영점이 없는 기기도 허용쌍이다. 배정 비용도 그 값이다.
    # 사후확률은 2등(0.30)이 3등(0.25)보다 크고 보수적 사후확률(0.28)도 3등보다 크다
    nodes = [node("locker-1", device_state=DeviceState.ACTIVE), node("locker-2"), node("locker-3")]
    posteriors = {"locker-1": 0.40, "locker-2": 0.30, "locker-3": 0.25}
    pipe, repo, prox, pol, audit, window = build(
        posteriors, conservative={"locker-2": 0.28}, nodes=nodes, threshold=0.2)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-2", 0.28)
    (sent,) = window.requests
    assert dict(sent.candidates) == {"locker-2": 0.28, "locker-3": 0.25}  # 사후확률 0.30 이 아니다
    assert [a["allowed"] for a in allocation_details(audit)] == [["locker-2", "locker-3"]]
    assert [r.node_id for r in repo.reserved] == ["locker-2"]
    assert pol.calls == ["locker-1", "locker-2", "locker-3"]  # 확률이 큰 순서

    # 보수적 사후확률이 임계값(0.2)에 못 미치면 그 기기는 허용쌍이 아니다. 자동 영점이 있는 3등이 배정된다
    pipe, repo, prox, pol, audit, window = build(
        posteriors, conservative={"locker-2": 0.15}, nodes=nodes, threshold=0.2)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-3", 0.25)
    (sent,) = window.requests
    assert dict(sent.candidates) == {"locker-3": 0.25}
    assert [a["allowed"] for a in allocation_details(audit)] == [["locker-3"]]
    assert [r.node_id for r in repo.reserved] == ["locker-3"]
    assert pol.calls == ["locker-1", "locker-3"]

    # 1등에 자동 영점이 없어도 보수적 사후확률이 임계값 이상이면 배정될 수 있다
    pipe, repo, prox, pol, audit, window = build({"locker-1": 0.55, "locker-2": 0.30}, conservative={"locker-1": 0.45})
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-1", 0.45)
    (sent,) = window.requests
    assert dict(sent.candidates) == {"locker-1": 0.45}


def test_FR23_any_top_unzeroed_asks():
    # 1등(locker-1)에 자동 영점이 없고 보수적 사후확률(0.25)이 임계값(0.3)에 못 미친다.
    # 2등(locker-2)은 자동 영점이 있고 임계값을 넘지만 다른 기기를 배정하지 않는다
    nodes = [node("locker-1"), node("locker-2"), node("locker-3")]
    pipe, repo, prox, pol, audit, window = build(
        {"locker-1": 0.45, "locker-2": 0.35, "locker-3": 0.15}, conservative={"locker-3": 0.10, "locker-1": 0.25},
        nodes=nodes, threshold=0.3)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.25)
    assert window.requests == [] and repo.reserved == [] and pol.calls == []  # 대기 구간에 내지 않는다
    assert proximity_detail(audit)["unzeroed"] == ["locker-1", "locker-3"]


# ── FR-18 실행 직전 재검사 ────────────────────────────────────────────


@pytest.mark.parametrize("target", [NEAREST, ANY])
def test_FR18_recheck_uses_conservative_probability(target):
    posteriors = {"locker-1": 0.55, "locker-2": 0.20}

    # 사후확률(0.55)은 임계값(0.4)을 넘지만 보수적 사후확률(0.30)은 못 넘는다. 재검사가 실패한다
    d, prox, pol, audit = recheck(target, posteriors, {"locker-1": 0.30})
    assert (d.verdict, d.reason, d.node_id, d.use_id, d.p_max) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, "locker-1", None, 0.30)
    assert pol.calls == []  # 확률에서 막으면 ④ 에 가지 않는다
    payload = thaw(audit.events[-1].payload)
    assert (payload["p"], payload["threshold"]) == (0.30, 0.4)

    # 보수적 사후확률도 임계값 이상이면 통과한다. 판정의 p_max 와 감사의 p 는 보수적 값이다
    d, prox, pol, audit = recheck(target, posteriors, {"locker-1": 0.45})
    assert (d.verdict, d.reason, d.use_id, d.p_max) == (Verdict.OK, ReasonCode.OK, "use-1", 0.45)
    assert pol.calls == ["locker-1"]
    assert thaw(audit.events[-1].payload)["p"] == 0.45

    # 대상에 자동 영점이 있으면(다른 기기만 없다) 사후확률로 비교한다
    d, prox, pol, audit = recheck(target, posteriors, {"locker-2": 0.10})
    assert (d.verdict, d.p_max) == (Verdict.OK, 0.55)
    d, prox, pol, audit = recheck(target, posteriors, {"locker-2": 0.10}, threshold=0.6)
    assert (d.verdict, d.reason, d.p_max) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, 0.55)


# ── FR-17 거리 선택자는 사후확률로 비교한다 ───────────────────────────


class SelectorProximity(selector_fakes.FakeProximity):
    """`_selector_fakes.FakeProximity` 에 보수적 사후확률을 더한다. `conservative` 의 기기가 자동 영점이 없는 기기다."""

    def __init__(self, values, posteriors, conservative):
        super().__init__(values, posteriors)
        self.conservative = conservative

    def posterior(self, beacons, candidates, at, *, place_nodes):
        result = super().posterior(beacons, candidates, at, place_nodes=place_nodes)
        conservative = {n: p for n, p in self.conservative.items() if n in result.posteriors}
        return ProximityResult(result.posteriors, at, at, "m", "c", frozenset(conservative), conservative)


def test_FR17_selector_rental_keeps_nominal_probability_for_unzeroed():
    # 거리 선택자는 자동 영점이 없는 기기도 쓸 수 있다 (FR-05). 대여형 하위 요청은 이전처럼 사후확률로 임계값을 본다.
    # locker-1 은 사후확률(0.6)이 임계값(0.5)을 넘고 보수적 사후확률(0.2)은 못 넘는다
    settings = Settings(version="t", threshold_locker=0.5)
    repo = selector_fakes.FakeRepo([selector_fakes.node("locker-1", "locker", Occupancy.RENTAL)])
    prox = SelectorProximity({"locker-1": -50.0}, {"locker-1": 0.6}, {"locker-1": 0.2})
    ids = iter(range(100_000))
    pipe = DecisionPipeline(
        repo, prox, selector_fakes.FakePolicy(), selector_fakes.FakeAudit(), settings, selector_fakes.FakeReadiness(),
        new_id=lambda: f"e{next(ids)}", store=selector_fakes.FakeStore())

    # 최초 판정: 통과한다. 하위 요청의 p_max 는 사후확률이다
    sd = pipe.decide_selector(selector_fakes.request(SELECTOR, action="open"))
    assert (sd.decision.verdict, sd.decision.reason) == (Verdict.OK, ReasonCode.OK)
    assert [(c.verdict, c.node_id, c.p_max) for c in sd.children] == [(Verdict.OK, "locker-1", 0.6)]
    assert [c.node_id for c in sd.contexts] == ["locker-1"]

    # 재검사: 같은 요청이 통과한다. 판정의 p_max 와 감사의 p 는 사후확률이다
    d, prox, pol, audit = recheck(SELECTOR, {"locker-1": 0.6}, {"locker-1": 0.2}, threshold=0.5)
    assert (d.verdict, d.reason, d.use_id, d.p_max) == (Verdict.OK, ReasonCode.OK, "use-1", 0.6)
    assert thaw(audit.events[-1].payload)["p"] == 0.6
    # 같은 값의 근접 지정은 보수적 사후확률로 막힌다. 선택자만 사후확률로 비교한다
    d, *_ = recheck(NEAREST, {"locker-1": 0.6}, {"locker-1": 0.2}, threshold=0.5)
    assert (d.verdict, d.reason, d.p_max) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, 0.2)

    # 사후확률이 임계값에 못 미치면 선택자도 막힌다 (최초와 재검사). 하위 판정은 사후확률(0.4)을 담는다
    low_repo = selector_fakes.FakeRepo([selector_fakes.node("locker-1", "locker", Occupancy.RENTAL)])
    pipe_low = DecisionPipeline(
        low_repo, SelectorProximity({"locker-1": -50.0}, {"locker-1": 0.4}, {"locker-1": 0.2}),
        selector_fakes.FakePolicy(), selector_fakes.FakeAudit(), settings, selector_fakes.FakeReadiness(),
        new_id=lambda: f"f{next(ids)}", store=selector_fakes.FakeStore())
    low = pipe_low.decide_selector(selector_fakes.request(SELECTOR, action="open", request_id="r2")).decision
    assert (low.verdict, low.reason) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE)
    assert low.devices == (DeviceOutcome("locker-1", Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None),)
    assert low_repo.decisions[low_repo.child_links[0].child].p_max == 0.4
    d, *_ = recheck(SELECTOR, {"locker-1": 0.4}, {"locker-1": 0.2}, threshold=0.5)
    assert (d.verdict, d.reason, d.p_max) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, 0.4)


# ── FR-16 Δ를 모르는 모델 ─────────────────────────────────────────────

MODEL_FILE = Path(__file__).resolve().parent / "proximity" / "fixtures" / "common" / "syn-common-1.json"  # Δ = 6.0


def test_FR16_conservative_posterior_unknown_bound_is_zero():
    # 기기 한 대 앞에서 모델의 front 평균으로 3초 동안 들린 강한 신호다. Δ를 아는 모델(6.0)은 이 신호의 보수적 사후확률이 임계값을 넘는다
    model = load_common_model(MODEL_FILE)
    unknown = replace(model, auto_zero_bound_db=None)
    nodes = [node("locker-1")]
    frames = [
        RssiFrame("locker-1", BEACON, model.front.mean, at, "s1", seq, at, at)
        for seq, at in enumerate((NOW - timedelta(seconds=2), NOW - timedelta(seconds=1), NOW), start=1)
    ]

    def posterior(of_model):
        return compute_posteriors(
            of_model, frames, BEACON, nodes, nodes, NOW, bin_sec=1.0, participants=["locker-1"], zeros={}, calib_version="0")

    known_result, unknown_result = posterior(model), posterior(unknown)
    assert known_result.conservative["locker-1"] >= 0.95
    # Δ를 모르면 자동 영점이 없는 기기의 보수적 사후확률은 0이다. 사후확률은 Δ와 상관없다. 이 결과는 `ProximityResult` 검증을 통과한다
    assert unknown_result.unzeroed == frozenset({"locker-1"}) and dict(unknown_result.conservative) == {"locker-1": 0.0}
    assert unknown_result.posteriors == known_result.posteriors
    assert unknown_result.posteriors["locker-1"] > 0.95
    assert unknown_result.threshold_probability("locker-1") == 0.0

    # 판정: 같은 신호에서 Δ를 알면 진행하고, 모르면 보수적 사후확률 도입 전처럼 ASK 다 (근접 지정, 전역 할당, 재검사)
    posteriors, known, unknown_conservative = (dict(known_result.posteriors), dict(known_result.conservative),
                                                dict(unknown_result.conservative))
    pipe, repo, *_ = build(posteriors, known, nodes=nodes, threshold=0.95)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-1", known["locker-1"])
    assert recheck(NEAREST, posteriors, known, threshold=0.95)[0].verdict is Verdict.OK

    pipe, repo, prox, pol, audit, window = build(posteriors, unknown_conservative, nodes=nodes, threshold=0.95)
    d = pipe.decide(request(NEAREST))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.0)
    assert repo.reserved == [] and pol.calls == []
    assert proximity_detail(audit)["conservative"] == {"locker-1": 0.0}

    pipe, repo, prox, pol, audit, window = build(posteriors, unknown_conservative, nodes=nodes, threshold=0.95)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.0)
    assert window.requests == [] and repo.reserved == [] and pol.calls == []

    d, prox, pol, audit = recheck(NEAREST, posteriors, unknown_conservative, threshold=0.95)
    assert (d.verdict, d.reason, d.p_max) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, 0.0)
    assert pol.calls == []
