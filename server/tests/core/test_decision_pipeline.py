"""FR-18 판정 파이프라인 ⓪~⑥. 다른 계층은 fake 로 대신한다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.decision.digest import input_digest
from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action,
    Capability,
    DeviceState,
    Occupancy,
    ExecutionKind,
    ReasonCode,
    ReserveOutcome,
    Step,
    StoreOutcome,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import (
    BeaconKey,
    Decision,
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


def node(node_id="locker-1", occ=Occupancy.RENTAL, cap=Capability.LOCKER, **kw) -> NodeInfo:
    base = dict(
        node_id=node_id, place_id="p", capability=cap, occupancy=occ,
        trust_state=TrustState.TRUSTED, device_state=DeviceState.AVAILABLE,
        exclusive_group="g", last_seen_at=NOW - timedelta(seconds=1),
    )
    base.update(kw)
    return NodeInfo(**base)


def frame(node_id="locker-1") -> RssiFrame:
    return RssiFrame(node_id, BEACON, -60.0, NOW, "s1", 1, NOW, NOW)


def anchor() -> NodeInfo:
    """비콘을 본 기기. 후보가 아닌 종류(조명)로 두어서 후보 조건과 분리한다."""
    return node("anchor-1", Occupancy.SHARED, Capability.LIGHT)


def request(target="nearest:locker", request_id="r1", action=Action.OPEN, **kw) -> UseRequest:
    return UseRequest(
        user_id="u1", request_id=request_id, target=TargetSpec.parse(target),
        action=action, host_kind="claude", received_at=NOW, **kw,
    )


class FakeRepo:
    def __init__(self):
        self.nodes_list = [node()]
        self.frames = [frame()]
        self.beacons = [BEACON]
        self.reserve_outcome = ReserveOutcome.RESERVED
        self.reserve_use_id: str | None = "use-1"
        self.reserve_usage_id: str | None = None
        self.reserved_at: datetime | None = None
        self.save_outcome = StoreOutcome.APPLIED
        self.link: RequestLink | None = None
        self.calls: list[str] = []
        self.saved: list[Decision] = []
        self.records = []
        self.reserved = []
        self.digests: list[str] = []
        self.raise_on: str | None = None

    def _hit(self, name):
        self.calls.append(name)
        if self.raise_on == name:
            raise RuntimeError("secret internal detail")

    def link_request(self, req, input_digest):
        self._hit("link_request")
        self.digests.append(input_digest)
        return self.link or RequestLink("i1", True, False)

    def beacons_of(self, user_id):
        self._hit("beacons_of")
        return self.beacons

    def rssi_since(self, beacons, since):
        self._hit("rssi_since")
        return self.frames

    def nodes(self, capability=None):
        self._hit("nodes")
        return self.nodes_list

    def reserve_if_free(self, req):
        self._hit("reserve_if_free")
        self.reserved.append(req)
        if self.reserve_outcome in (ReserveOutcome.RESERVED, ReserveOutcome.REOPERATING):
            return ReserveResult(self.reserve_outcome, use_id=self.reserve_use_id,
                                 usage_id=self.reserve_usage_id, reserved_at=self.reserved_at)
        return ReserveResult(self.reserve_outcome)

    def save_decision(self, decision, record):
        self._hit("save_decision")
        self.saved.append(decision)
        self.records.append(record)
        return StoreResult(self.save_outcome)


class FakeProximity:
    def __init__(self, posteriors):
        self.posteriors = posteriors
        self.calls = []
        self.place_nodes = []

    def posterior(self, beacons, candidates, at, *, place_nodes):
        self.calls.append([n.node_id for n in candidates])
        self.place_nodes.append([n.node_id for n in place_nodes])
        return ProximityResult(self.posteriors, at, at, "m", "c")


class FakePolicy:
    def __init__(self, result=None):
        self.result = result or PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)
        self.calls = []
        self.request_ids = []

    def check(self, user_id, node_id, action, now, *, internal_request_id=None):
        self.calls.append(node_id)
        self.request_ids.append(internal_request_id)
        return self.result


class OwnUsagePolicy(FakePolicy):
    """`own` 요청이 승인의 마지막 한 번을 확보해 뒀다. 그 요청의 ID를 받으면 사용분을 빼서 통과하고, 아니면 소진이다."""

    def __init__(self, own):
        super().__init__(PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, 1))
        self.own = own

    def check(self, user_id, node_id, action, now, *, internal_request_id=None):
        exhausted = super().check(user_id, node_id, action, now, internal_request_id=internal_request_id)
        if internal_request_id == self.own:
            return PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1, approval_id="a1")
        return exhausted


class FakeReadiness:
    """상태 관리의 기기별 실행 가능 여부. 기본은 모두 가능하다."""

    def __init__(self, not_ready=()):
        self.not_ready = set(not_ready)
        self.error: Exception | None = None

    def can_execute(self, node_id):
        if self.error:
            raise self.error
        return node_id not in self.not_ready


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def build(posteriors=None, policy=None, settings=None, readiness=None, window=None):
    repo = FakeRepo()
    prox = FakeProximity({"locker-1": 0.99} if posteriors is None else posteriors)
    pol = policy or FakePolicy()
    audit = FakeAudit()
    n = iter(range(10_000))
    pipe = DecisionPipeline(repo, prox, pol, audit, settings or Settings(
        version="v1", threshold_explicit_locker=0.5, threshold_explicit_light=0.5),
                            readiness or FakeReadiness(), new_id=lambda: f"e{next(n)}", window=window)
    return pipe, repo, prox, pol, audit


# ── 정상 경로와 단계 순서 ────────────────────────────────────────────


def test_FR18_통과한_대여형은_OK이고_RESERVED를_만든다():
    pipe, repo, prox, pol, audit = build()
    d = pipe.decide(request())
    assert (d.verdict, d.reason, d.node_id, d.use_id) == (Verdict.OK, ReasonCode.OK, "locker-1", "use-1")
    assert repo.saved == [d]
    assert repo.reserved[0].internal_request_id == "i1"


def test_FR18_단계를_0부터_순서대로_검사한다():
    pipe, repo, prox, pol, audit = build()
    pipe.decide(request())
    assert repo.calls[:3] == ["link_request", "beacons_of", "rssi_since"]
    assert repo.calls[-1] == "save_decision"
    assert repo.calls.index("nodes") < repo.calls.index("reserve_if_free")
    steps = [e.step for e in audit.events]
    assert steps.index(Step.BEACON) < steps.index(Step.CANDIDATE) < steps.index(Step.PROXIMITY) \
        < steps.index(Step.POLICY) < steps.index(Step.DEVICE_OCCUPANCY)


def test_FR18_판정에_적용한_설정_버전이_감사_기록에_남는다():
    pipe, repo, prox, pol, audit = build()
    pipe.decide(request())
    last = audit.events[-1]
    assert last.payload["settings_version"] == "v1"
    assert last.internal_request_id == "i1"


def test_FR18_같은_입력은_같은_판정을_반환한다():
    a = build()[0].decide(request())
    b = build()[0].decide(request())
    assert a == b


# ── 첫 실패에서 멈춘다 ──────────────────────────────────────────────


def test_FR18_비콘이_없으면_거부하고_뒤_단계를_실행하지_않는다():
    pipe, repo, prox, pol, audit = build()
    repo.frames = []
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert "nodes" not in repo.calls and prox.calls == [] and pol.calls == []


def test_FR18_후보가_없으면_NO_CANDIDATE():
    pipe, repo, prox, pol, audit = build()
    repo.nodes_list = [anchor()]  # 비콘을 본 기기는 있지만 요청 종류의 후보는 없다
    repo.frames = [frame("anchor-1")]
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.NO_CANDIDATE)
    assert prox.calls == []


@pytest.mark.parametrize("kw", [
    {"trust_state": TrustState.DISCOVERED},
    {"trust_state": TrustState.REVOKED},
    {"device_state": DeviceState.FAULT},
    {"last_seen_at": None},
    {"last_seen_at": NOW - timedelta(seconds=999)},
    {"capability": Capability.LIGHT},
])
def test_FR18_조건을_못_채운_기기는_후보가_아니다(kw):
    pipe, repo, prox, pol, audit = build()
    repo.nodes_list = [anchor(), node(**kw)]
    repo.frames = [frame("anchor-1")]
    d = pipe.decide(request())
    assert d.reason is ReasonCode.NO_CANDIDATE


def test_FR18_명시_지정_대상이_후보_밖이면_TARGET_UNAVAILABLE이고_근접을_부르지_않는다():
    pipe, repo, prox, pol, audit = build()
    repo.nodes_list = [node("locker-1"), node("locker-2", device_state=DeviceState.FAULT)]
    d = pipe.decide(request("locker-2"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)
    assert prox.calls == []


def test_FR18_any_지정은_이_작업에서_거부한다():
    pipe, *_ = build()
    d = pipe.decide(request("any:locker"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)


def test_FR18_사후확률이_임계값_미만이면_ASK_LOW_CONFIDENCE():
    pipe, repo, prox, pol, audit = build({"locker-1": 0.5, "locker-2": 0.5})
    d = pipe.decide(request())
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None, 0.5)
    assert pol.calls == [] and repo.reserved == []


def test_FR18_빈_사후확률은_LOW_CONFIDENCE():
    pipe, *_ = build({})
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE)


def test_FR18_임계값은_설정에서_받는다():
    pipe, *_ = build({"locker-1": 0.8}, settings=Settings(threshold_locker=0.7))
    assert pipe.decide(request()).verdict is Verdict.OK
    pipe, *_ = build({"locker-1": 0.8}, settings=Settings(threshold_locker=0.9))
    assert pipe.decide(request()).reason is ReasonCode.LOW_CONFIDENCE


def test_FR18_명시_지정도_전체_후보로_근접을_계산하고_확률_미만이면_MOVE_CLOSER():
    pipe, repo, prox, pol, audit = build({"locker-1": 0.4, "locker-2": 0.6})
    repo.nodes_list = [node("locker-1"), node("locker-2")]
    d = pipe.decide(request("locker-1"))
    assert prox.calls == [["locker-1", "locker-2"]]
    assert (d.verdict, d.reason, d.node_id) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, "locker-1")
    assert pol.calls == []


def test_FR18_정책은_확정한_대상만_검사한다():
    pipe, repo, prox, pol, audit = build({"locker-1": 0.01, "locker-2": 0.97})
    repo.nodes_list = [node("locker-1"), node("locker-2")]
    d = pipe.decide(request())
    assert pol.calls == ["locker-2"] and d.node_id == "locker-2"


@pytest.mark.parametrize("verdict,reason", [
    (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING),
    (Verdict.NEED_APPROVAL, ReasonCode.ACTION_NOT_ALLOWED),
    (Verdict.DENY, ReasonCode.POLICY_DENIED),
])
def test_FR18_정책_결과를_그대로_판정에_반영한다(verdict, reason):
    pipe, repo, prox, pol, audit = build(policy=FakePolicy(PolicyCheckResult(verdict, reason, 2)))
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (verdict, reason)
    assert repo.reserved == []


def test_FR18_정책이_OK_아닌_알_수_없는_판정을_내면_OK가_되지_않는다():
    pipe, *_ = build(policy=FakePolicy(PolicyCheckResult(Verdict.BUSY, ReasonCode.OK, 1)))
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)


def test_FR18_승인_ID를_점유_요청에_넘긴다():
    pol = FakePolicy(PolicyCheckResult(Verdict.OK, ReasonCode.OK, 3, approval_id="a1"))
    pipe, repo, *_ = build(policy=pol)
    pipe.decide(request())
    assert repo.reserved[0].approval_id == "a1"


def test_FR14_재판정은_자기_사용분으로_소진되지_않는다():
    # 오류로 거부한 요청을 같은 내부 요청으로 다시 판정한다. 이전 시도가 승인의 마지막 사용분을 확보해 뒀다 (#76)
    pipe, repo, prox, pol, audit = build(policy=OwnUsagePolicy(own="i1"))
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert pol.request_ids == ["i1"]
    assert repo.reserved[0].approval_id == "a1"  # ⑤는 같은 승인으로 기존 사용분을 쓴다

    # 다른 요청이 확보한 사용분은 그대로 세므로 소진으로 멈춘다
    pipe, repo, *_ = build(policy=OwnUsagePolicy(own="other"))
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED)
    assert repo.reserved == []


@pytest.mark.parametrize("outcome,verdict,reason", [
    (ReserveOutcome.DEVICE_BUSY, Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    (ReserveOutcome.USER_BUSY, Verdict.BUSY, ReasonCode.USER_BUSY),
    (ReserveOutcome.APPROVAL_EXHAUSTED, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED),
])
def test_FR18_FR19_점유_실패는_reserve_if_free_결과를_따른다(outcome, verdict, reason):
    pipe, repo, *_ = build()
    repo.reserve_outcome = outcome
    d = pipe.decide(request())
    assert (d.verdict, d.reason, d.use_id) == (verdict, reason, None)
    assert len(repo.reserved) == 1


def shared_build(policy=None):
    pipe, repo, prox, pol, audit = build({"light-1": 0.99}, policy=policy)
    repo.nodes_list = [node("light-1", Occupancy.SHARED, Capability.LIGHT)]
    repo.frames = [frame("light-1")]
    repo.reserve_use_id = None
    return pipe, repo, prox, pol, audit


def test_FR18_FR19_공용형도_reserve_if_free를_호출하고_세션은_없다():
    pipe, repo, *_ = shared_build()
    d, ctx = pipe.decide_for_execution(request("nearest:light", action=Action.ON))
    assert (d.verdict, d.node_id, d.use_id) == (Verdict.OK, "light-1", None)
    assert len(repo.reserved) == 1
    assert ctx.execution_kind is ExecutionKind.SHARED and ctx.use_id is None


def test_FR18_공용형_승인_사용분_ID를_점유_요청에_넘기고_인계에_담는다():
    pol = FakePolicy(PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1, approval_id="a1"))
    pipe, repo, *_ = shared_build(pol)
    repo.reserve_usage_id = "usage-1"
    d, ctx = pipe.decide_for_execution(request("nearest:light", action=Action.ON))
    assert d.verdict is Verdict.OK and repo.reserved[0].approval_id == "a1"
    assert ctx.usage_id == "usage-1"


def test_FR18_공용형에_세션이_돌아오면_OK가_되지_않는다():
    pipe, repo, *_ = shared_build()
    repo.reserve_use_id = "use-1"
    d = pipe.decide(request("nearest:light", action=Action.ON))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)


# ── ⓪ 중복 ──────────────────────────────────────────────────────────


def test_FR18_같은_키_다른_입력이면_REQUEST_ID_CONFLICT이고_저장하지_않는다():
    pipe, repo, prox, pol, audit = build()
    repo.link = RequestLink("i1", False, True)
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
    assert repo.saved == [] and "beacons_of" not in repo.calls


def test_FR18_같은_키_같은_입력이면_기존_판정을_반환한다():
    pipe, repo, prox, pol, audit = build()
    prev = Decision("i1", "r1", Verdict.OK, ReasonCode.OK, node_id="locker-1", use_id="use-1")
    repo.link = RequestLink("i1", False, False, previous=prev)
    assert pipe.decide(request()) is prev
    assert repo.reserved == [] and repo.saved == []


def test_FR18_진행_중인_같은_키는_다시_판정하지_않고_거부한다():
    pipe, repo, *_ = build()
    repo.link = RequestLink("i1", False, False)
    d = pipe.decide(request())
    assert d.verdict is Verdict.DENY and repo.reserved == []


def test_FR18_다른_계정의_같은_request_id는_서로_다른_요청으로_판정한다():
    pipe, repo, *_ = build()
    seen: dict[tuple[str, str], str] = {}

    def link(req, digest):
        key = (req.user_id, req.request_id)
        if key in seen:
            return RequestLink(seen[key], False, False)
        seen[key] = f"i{len(seen) + 1}"
        return RequestLink(seen[key], True, False)

    repo.link_request = link
    a = pipe.decide(request())
    other = UseRequest("u2", "r1", TargetSpec.parse("nearest:locker"), Action.OPEN, "claude", NOW)
    b = pipe.decide(other)
    assert a.internal_request_id != b.internal_request_id
    assert [r.internal_request_id for r in repo.reserved] == ["i1", "i2"]


def test_FR18_digest는_request_id_received_at_host_kind에_영향받지_않는다():
    a = input_digest(request(request_id="r1"))
    b = input_digest(UseRequest("u1", "r2", TargetSpec.parse("nearest:locker"), Action.OPEN,
                                "chatgpt", NOW + timedelta(hours=1)))
    assert a == b


def test_FR18_digest는_대상_동작_원요청이_다르면_달라진다():
    base = input_digest(request())
    assert input_digest(request("locker-1")) != base
    assert input_digest(request(action=Action.ON)) != base
    assert input_digest(request(parent_internal_request_id="p")) != base


def test_FR18_digest를_link_request에_넘긴다():
    pipe, repo, *_ = build()
    pipe.decide(request())
    assert repo.digests == [input_digest(request())]


# ── 예외는 OK가 되지 않는다 ──────────────────────────────────────────


@pytest.mark.parametrize("name", ["beacons_of", "rssi_since", "nodes", "reserve_if_free", "save_decision"])
def test_FR18_단계에서_예외가_나면_INTERNAL_ERROR로_거부한다(name):
    pipe, repo, prox, pol, audit = build()
    repo.raise_on = name
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert "secret" not in repr(d)
    assert any("secret" in str(e.payload) for e in audit.events)  # 예외 내용은 감사에만


def test_FR18_근접_정책_예외도_INTERNAL_ERROR():
    pipe, repo, prox, pol, audit = build()
    prox.posterior = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    assert pipe.decide(request()).reason is ReasonCode.INTERNAL_ERROR
    pipe, repo, prox, pol, audit = build()
    pol.check = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    assert pipe.decide(request()).reason is ReasonCode.INTERNAL_ERROR


def test_FR18_link_request가_실패하면_INTERNAL_ERROR이고_뒤_단계를_실행하지_않는다():
    pipe, repo, *_ = build()
    repo.raise_on = "link_request"
    d = pipe.decide(request())
    assert d.reason is ReasonCode.INTERNAL_ERROR and "beacons_of" not in repo.calls


def test_FR18_감사_기록이_실패하면_OK가_되지_않는다():
    pipe, repo, prox, pol, audit = build()
    audit.append = lambda e: (_ for _ in ()).throw(OSError("disk"))
    assert pipe.decide(request()).verdict is Verdict.DENY


def test_FR11_Decision에는_사용자_ID가_없다():
    pipe, *_ = build()
    d = pipe.decide(request())
    assert "u1" not in repr(d)


# ── 비콘과 장소 (#39, #42) ──────────────────────────────────────────


def test_FR15_활성_비콘이_둘이면_INTERNAL_ERROR():
    pipe, repo, *_ = build()
    repo.beacons = [BEACON, BeaconKey("u", 1, 2)]
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert "rssi_since" not in repo.calls


def test_FR15_활성_비콘이_없으면_BEACON_NOT_SEEN():
    pipe, repo, *_ = build()
    repo.beacons = []
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert "rssi_since" not in repo.calls


def test_FR15_신뢰하지_않는_기기만_비콘을_봤으면_BEACON_NOT_SEEN():
    pipe, repo, prox, pol, audit = build()
    repo.nodes_list = [node(trust_state=TrustState.DISCOVERED)]
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert prox.calls == []


def test_FR18_근접에_비콘을_본_장소의_전체_기기를_넘긴다():
    pipe, repo, prox, pol, audit = build({"locker-1": 0.99})
    repo.nodes_list = [
        node("locker-1"), anchor(),
        node("locker-9", place_id="other"),  # 다른 장소의 후보는 섞지 않는다
        node("fault-1", device_state=DeviceState.FAULT),  # 전체 상태에는 들어간다
    ]
    d = pipe.decide(request())
    assert d.verdict is Verdict.OK
    assert prox.calls == [["locker-1"]]
    assert sorted(prox.place_nodes[0]) == ["anchor-1", "fault-1", "locker-1"]


@pytest.mark.parametrize("place_ids", [{"locker-1": "p", "anchor-1": "q"}, {"locker-1": None}])
def test_FR18_비콘을_본_장소가_하나로_정해지지_않으면_INTERNAL_ERROR(place_ids):
    pipe, repo, prox, pol, audit = build()
    repo.nodes_list = [node(n, place_id=p) for n, p in place_ids.items()]
    repo.frames = [frame(n) for n in place_ids]
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert prox.calls == []


# ── 임계값 (#39, #61) ───────────────────────────────────────────────


def test_FR18_명시_지정은_별도_임계값을_쓴다():
    settings = Settings(threshold_locker=0.95, threshold_explicit_locker=0.6)
    pipe, *_ = build({"locker-1": 0.7}, settings=settings)
    assert pipe.decide(request("locker-1")).verdict is Verdict.OK
    pipe, *_ = build({"locker-1": 0.7}, settings=settings)
    assert pipe.decide(request("nearest:locker")).reason is ReasonCode.LOW_CONFIDENCE


def test_FR18_명시_지정_임계값이_없으면_INVALID_INPUT이고_근접을_부르지_않는다():
    pipe, repo, prox, pol, audit = build(settings=Settings())
    d = pipe.decide(request("locker-1"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert prox.calls == [] and pol.calls == [] and repo.reserved == []


def test_FR18_스피커_임계값이_없으면_INVALID_INPUT():
    pipe, repo, prox, pol, audit = build(settings=Settings())
    repo.nodes_list = [node("speaker-1", Occupancy.SHARED, Capability.SPEAKER)]
    repo.frames = [frame("speaker-1")]
    d = pipe.decide(request("nearest:speaker", action=Action.ON))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert prox.calls == []


# ── 재조작과 실행 인계 (#35, #16) ────────────────────────────────────


def test_FR18_FR19_본인_재조작은_기존_use_id로_OK이고_재조작으로_인계한다():
    pipe, repo, *_ = build()
    repo.reserve_outcome = ReserveOutcome.REOPERATING
    repo.reserve_use_id = "use-old"
    d, ctx = pipe.decide_for_execution(request())
    assert (d.verdict, d.use_id) == (Verdict.OK, "use-old")
    assert ctx.execution_kind is ExecutionKind.RENTAL_REOPERATION and ctx.use_id == "use-old"


def test_FR18_최초_대여_인계_context에_확정한_값이_담긴다():
    pipe, repo, *_ = build()
    repo.reserve_usage_id = "usage-1"
    req = request()
    d, ctx = pipe.decide_for_execution(req)
    assert (ctx.internal_request_id, ctx.request, ctx.node_id, ctx.use_id, ctx.usage_id) == \
        ("i1", req, "locker-1", "use-1", "usage-1")
    assert ctx.settings.version == "v1" and ctx.execution_kind is ExecutionKind.RENTAL_INITIAL


def test_FR18_OK가_아니거나_재전송이면_인계_context가_없다():
    pipe, repo, *_ = build()
    repo.frames = []
    assert pipe.decide_for_execution(request())[1] is None
    pipe, repo, *_ = build()
    prev = Decision("i1", "r1", Verdict.OK, ReasonCode.OK, node_id="locker-1", use_id="use-1")
    repo.link = RequestLink("i1", False, False, previous=prev)
    assert pipe.decide_for_execution(request()) == (prev, None)


def test_FR18_decide는_decide_for_execution의_판정과_같다():
    assert build()[0].decide(request()) == build()[0].decide_for_execution(request())[0]


# ── 판정 기록 (#33) ─────────────────────────────────────────────────


def test_FR22_판정_기록에_적용한_값이_담긴다():
    pol = FakePolicy(PolicyCheckResult(Verdict.OK, ReasonCode.OK, 7))
    pipe, repo, prox, *_ = build({"locker-1": 0.97}, policy=pol)
    pipe.decide(request())
    rec = repo.records[0]
    assert (rec.settings.version, rec.model_version, rec.calib_version, rec.policy_version) == \
        ("v1", "m", "c", 7)
    assert dict(rec.posteriors) == {"locker-1": 0.97}
    assert rec.window_start == NOW and rec.window_end == NOW


def test_FR22_근접_전에_끝난_판정은_버전과_구간이_None이다():
    pipe, repo, *_ = build()
    repo.frames = []
    pipe.decide(request())
    rec = repo.records[0]
    assert (rec.model_version, rec.calib_version, rec.policy_version, rec.window_start) == \
        (None, None, None, None)
    assert dict(rec.posteriors) == {}


@pytest.mark.parametrize("setup", [
    lambda repo: setattr(repo, "frames", []),
    lambda repo: setattr(repo, "reserve_outcome", ReserveOutcome.DEVICE_BUSY),
])
def test_FR22_OK가_아닌_판정도_한_번_저장한다(setup):
    pipe, repo, *_ = build()
    setup(repo)
    d = pipe.decide(request())
    assert d.verdict is not Verdict.OK and repo.saved == [d]


def test_FR22_판정_저장이_충돌하면_OK가_아니고_다시_저장하지_않는다():
    pipe, repo, *_ = build()
    repo.save_outcome = StoreOutcome.CONFLICT
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert len(repo.saved) == 1


# ── 머지된 결정의 반영 (#80, #82) ─────────────────────────────────────


def two_lockers(pipe_args=None):
    pipe, repo, prox, pol, audit = build({"locker-1": 0.01, "locker-2": 0.97},
                                         readiness=(pipe_args or FakeReadiness()))
    repo.nodes_list = [node("locker-1"), node("locker-2")]
    repo.frames = [frame("locker-1"), frame("locker-2")]
    return pipe, repo, prox, pol, audit


def test_FR18_복원_중인_기기는_후보에서_뺀다():
    pipe, repo, prox, *_ = two_lockers(FakeReadiness({"locker-1"}))
    d = pipe.decide(request())
    assert prox.calls == [["locker-2"]]  # ③ 에는 걸러낸 후보만 넘긴다
    assert prox.place_nodes == [["locker-1", "locker-2"]]  # 장소 전체 기기는 그대로다 (#39)
    assert (d.verdict, d.node_id) == (Verdict.OK, "locker-2")


def test_FR18_복원_중인_기기_대신_다른_기기를_고른다():
    pipe, repo, prox, *_ = two_lockers(FakeReadiness({"locker-1"}))
    d = pipe.decide(request())
    assert d.node_id == "locker-2" and d.p_max == 0.97
    pipe, repo, prox, *_ = two_lockers(FakeReadiness({"locker-2"}))  # 가장 가까운 기기를 뺀다
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE) and repo.reserved == []


def test_FR18_복원_중인_지정_대상은_거부한다():
    pipe, repo, prox, *_ = two_lockers(FakeReadiness({"locker-1"}))
    d = pipe.decide(request("locker-1"))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, None)
    assert prox.calls == [] and repo.reserved == []
    assert "복원" not in repr(d)


def test_FR18_후보가_모두_복원_중이면_거부한다():
    pipe, repo, prox, *_ = two_lockers(FakeReadiness({"locker-1", "locker-2"}))
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.NO_CANDIDATE)
    assert prox.calls == [] and repo.reserved == []


def test_FR18_복원_여부_조회_예외는_거부한다():
    ready = FakeReadiness()
    ready.error = RuntimeError("secret internal detail")
    pipe, repo, prox, *_ = build(readiness=ready)
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert "secret" not in repr(d) and repo.reserved == []


def test_FR19_예약_시각을_그대로_넘긴다():
    pipe, repo, *_ = build()
    repo.reserved_at = NOW - timedelta(seconds=5)
    d, ctx = pipe.decide_for_execution(request())
    assert d.verdict is Verdict.OK and ctx.reserved_at == NOW - timedelta(seconds=5)


def test_FR19_예약_시각이_없으면_None이다():
    pipe, repo, *_ = build()
    _, ctx = pipe.decide_for_execution(request())
    assert ctx.reserved_at is None


# ── 전역 할당: any:locker (FR-23) ─────────────────────────────────────


class FakeWindow:
    """대기 구간 대신 정해 둔 기기를 돌려준다. 기본은 확률이 가장 큰 허용 기기다."""

    def __init__(self, assign="best"):
        self.assign = assign
        self.requests = []
        self.error: Exception | None = None

    def submit(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.assign == "best":
            return max(request.candidates, key=lambda n: (request.candidates[n], n))
        return self.assign


class PerNodePolicy(FakePolicy):
    def __init__(self, by_node=None):
        super().__init__()
        self.by_node = by_node or {}

    def check(self, user_id, node_id, action, now, *, internal_request_id=None):
        self.calls.append(node_id)
        self.request_ids.append(internal_request_id)
        return self.by_node.get(node_id, self.result)


ANY_SETTINGS = Settings(version="v1", threshold_locker=0.4, threshold_explicit_locker=0.3,
                        threshold_explicit_light=0.3)
ANY = "any:locker"


def any_build(posteriors=None, policy=None, window="default", repo_nodes=None):
    window = FakeWindow() if window == "default" else window
    pipe, repo, prox, pol, audit = build(
        {"locker-1": 0.45, "locker-2": 0.5} if posteriors is None else posteriors,
        policy, ANY_SETTINGS, window=window)
    repo.nodes_list = repo_nodes or [node("locker-1"), node("locker-2")]
    repo.frames = [frame("locker-1"), frame("locker-2")]
    return pipe, repo, prox, pol, audit, window


def test_FR23_any_요청_하나는_가장_가까운_허용_기기를_받는다():
    pipe, repo, prox, pol, audit, window = any_build()
    d, ctx = pipe.decide_for_execution(request(ANY))
    assert (d.verdict, d.node_id, d.p_max) == (Verdict.OK, "locker-2", 0.5)
    assert repo.reserved[0].node_id == "locker-2" and ctx.node_id == "locker-2"
    assert prox.calls == [["locker-1", "locker-2"]]


def test_FR23_임계값을_넘은_후보가_없으면_ASK():
    pipe, repo, prox, pol, audit, window = any_build({"locker-1": 0.1, "locker-2": 0.2})
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.p_max, d.node_id) == (Verdict.ASK, ReasonCode.LOW_CONFIDENCE, 0.2, None)
    assert window.requests == [] and repo.reserved == [] and pol.calls == []


def test_FR23_임계값_미만_후보는_배정_입력에_없다():
    pipe, repo, prox, pol, audit, window = any_build({"locker-1": 0.45, "locker-2": 0.3})
    pipe.decide(request(ANY))
    (sent,) = window.requests
    assert dict(sent.candidates) == {"locker-1": 0.45} and sent.request_id == "i1"
    assert pol.calls == ["locker-1"]  # 임계값 미만 기기는 정책도 검사하지 않는다


def test_FR23_승인이_필요한_기기는_허용쌍이_아니다():
    need = PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 1)
    pipe, repo, prox, pol, audit, window = any_build(policy=PerNodePolicy({"locker-2": need}))
    d = pipe.decide(request(ANY))
    assert dict(window.requests[0].candidates) == {"locker-1": 0.45}
    assert (d.verdict, d.node_id) == (Verdict.OK, "locker-1")


def test_FR23_정책이_모두_거부하면_그_판정을_유지한다():
    need = PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 1)
    deny = PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, 1)
    pipe, repo, prox, pol, audit, window = any_build(
        policy=PerNodePolicy({"locker-2": need, "locker-1": deny}))
    d = pipe.decide(request(ANY))
    # 확률이 가장 큰 locker-2 의 판정을 그대로 쓴다
    assert (d.verdict, d.reason, d.node_id, d.p_max) == (
        Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, "locker-2", 0.5)
    assert window.requests == [] and repo.reserved == []


def test_FR23_점유_중인_기기는_허용쌍이_아니다():
    busy = node("locker-2", device_state=DeviceState.ACTIVE)
    pipe, repo, prox, pol, audit, window = any_build(repo_nodes=[node("locker-1"), busy])
    d = pipe.decide(request(ANY))
    assert dict(window.requests[0].candidates) == {"locker-1": 0.45}
    assert (d.verdict, d.node_id) == (Verdict.OK, "locker-1")

    both = [node("locker-1", device_state=DeviceState.RESERVED), busy]
    pipe, repo, prox, pol, audit, window = any_build(repo_nodes=both)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.node_id) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY, None)
    assert window.requests == [] and repo.reserved == []


def test_FR23_배정받지_못하면_BUSY():
    pipe, repo, prox, pol, audit, window = any_build(window=FakeWindow(assign=None))
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason, d.node_id, d.use_id) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY, None, None)
    assert repo.reserved == [] and repo.saved == [d]


def test_FR23_배정받은_기기를_예약하고_인계한다():
    pipe, repo, prox, pol, audit, window = any_build(window=FakeWindow(assign="locker-1"))
    d, ctx = pipe.decide_for_execution(request(ANY))
    assert (d.verdict, d.node_id, d.use_id, d.p_max) == (Verdict.OK, "locker-1", "use-1", 0.45)
    assert repo.reserved[0].node_id == "locker-1"
    assert (ctx.node_id, ctx.use_id, ctx.request.target.kind.value) == ("locker-1", "use-1", "any")
    allocation = [e.payload["allocation"] for e in audit.events if "allocation" in e.payload]
    assert allocation == [{"allowed": ("locker-2", "locker-1"), "assigned": "locker-1"}]


def test_FR23_any_배정의_정책_검사도_내부_요청_ID를_넘긴다():
    pipe, repo, prox, pol, audit, window = any_build(policy=PerNodePolicy())
    pipe.decide(request(ANY))
    assert pol.calls == ["locker-2", "locker-1"] and pol.request_ids == ["i1", "i1"]


def test_FR23_예약에_정책의_승인_ID를_넘긴다():
    approved = PolicyCheckResult(Verdict.OK, ReasonCode.OK, 7, approval_id="a1")
    pipe, repo, prox, pol, audit, window = any_build(policy=PerNodePolicy({"locker-2": approved}))
    pipe.decide(request(ANY))
    assert repo.reserved[0].node_id == "locker-2" and repo.reserved[0].approval_id == "a1"
    assert repo.records[0].policy_version == 7


def test_FR23_겹친_요청은_중복_예약되지_않는다():
    import threading

    from nearpilot.core.allocation import AllocationWindow

    window = AllocationWindow(40)
    out = {}

    def run(iid, posteriors):
        pipe, repo, *_ = any_build(posteriors, window=window)
        repo.link = RequestLink(iid, True, False)
        d = pipe.decide(request(ANY, request_id=f"r-{iid}"))
        out[iid] = (d, repo.reserved)

    threads = [threading.Thread(target=run, args=("i1", {"locker-1": 0.45, "locker-2": 0.5})),
               threading.Thread(target=run, args=("i2", {"locker-1": 0.45, "locker-2": 0.5}))]
    threads[0].start()
    import time
    deadline = time.monotonic() + 2
    while window.pending() < 1:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    threads[1].start()
    for t in threads:
        t.join(5)
    nodes = sorted(d.node_id for d, _ in out.values())
    assert nodes == ["locker-1", "locker-2"]  # 서로 다른 기기를 받는다
    assert all(d.verdict is Verdict.OK for d, _ in out.values())
    assert sum(len(reserved) for _, reserved in out.values()) == 2

    # 사물함 하나만 허용쌍이면 한쪽만 받고 한쪽은 BUSY 다
    out.clear()
    threads = [threading.Thread(target=run, args=("i1", {"locker-1": 0.45, "locker-2": 0.1})),
               threading.Thread(target=run, args=("i2", {"locker-1": 0.45, "locker-2": 0.1}))]
    threads[0].start()
    deadline = time.monotonic() + 2
    while window.pending() < 1:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    threads[1].start()
    for t in threads:
        t.join(5)
    verdicts = sorted(d.verdict.value for d, _ in out.values())
    assert verdicts == ["BUSY", "OK"]
    assert sum(len(reserved) for _, reserved in out.values()) == 1


def test_FR18_any_배정_예외는_내부_오류로_거부한다():
    window = FakeWindow()
    window.error = RuntimeError("secret internal detail")
    pipe, repo, prox, pol, audit, _ = any_build(window=window)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert "secret" not in repr(d) and repo.reserved == []


def test_FR18_any_지정은_구간이_없으면_거부한다():
    pipe, repo, prox, pol, audit, _ = any_build(window=None)
    d = pipe.decide(request(ANY))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert prox.calls == []


def test_FR23_locker_외_any_지정은_거부한다():
    pipe, repo, prox, pol, audit, window = any_build()
    d = pipe.decide(request("any:light"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert window.requests == [] and prox.calls == []


def test_FR11_배정_정보를_공개하지_않는다():
    pipe, repo, prox, pol, audit, window = any_build(window=FakeWindow(assign=None))
    busy = pipe.decide(request(ANY))
    assert busy.node_id is None and busy.message is None
    pipe, repo, prox, pol, audit, window = any_build()
    ok = pipe.decide(request(ANY, request_id="r9"))
    text = repr(ok)
    assert "allocation" not in text and "locker-1" not in text and "u1" not in text


# ── ⓪ 오류로 거부한 요청의 재판정 (#76) ─────────────────────────────────


def _retrying_link(repo):
    """DB 가 약속하는 ⓪ 동작: 같은 키는 같은 내부 ID 를 쓰고, INTERNAL_ERROR 로 끝난 판정만 한 번 새 시도로 넘긴다."""
    state = {"decision": None, "in_progress": False}

    def link(req, digest):
        if state["in_progress"]:
            return RequestLink("i1", False, False)
        prev = state["decision"]
        if prev is None or prev.reason is ReasonCode.INTERNAL_ERROR:
            state["decision"], state["in_progress"] = None, True
            return RequestLink("i1", True, False)
        return RequestLink("i1", False, False, previous=prev)

    def save(decision, record):
        state["decision"], state["in_progress"] = decision, False
        repo.saved.append(decision)
        return StoreResult(StoreOutcome.APPLIED)

    repo.link_request = link
    repo.save_decision = save
    return state


def test_FR18_오류로_거부한_요청을_같은_입력으로_다시_보내면_다시_판정한다():
    pipe, repo, *_ = build()
    _retrying_link(repo)
    repo.raise_on = "rssi_since"
    assert pipe.decide(request()).reason is ReasonCode.INTERNAL_ERROR
    repo.raise_on = None
    d = pipe.decide(request())
    assert (d.verdict, d.reason, d.internal_request_id) == (Verdict.OK, ReasonCode.OK, "i1")
    assert [x.reason for x in repo.saved] == [ReasonCode.INTERNAL_ERROR, ReasonCode.OK]


def test_FR18_재판정도_실패하면_다음_재전송이_다시_판정한다():
    pipe, repo, *_ = build()
    _retrying_link(repo)
    repo.raise_on = "rssi_since"
    assert pipe.decide(request()).reason is ReasonCode.INTERNAL_ERROR
    assert pipe.decide(request()).reason is ReasonCode.INTERNAL_ERROR
    repo.raise_on = None
    assert pipe.decide(request()).verdict is Verdict.OK


def test_FR18_확정한_판정은_재전송해도_다시_판정하지_않는다():
    pipe, repo, *_ = build()
    _retrying_link(repo)
    first = pipe.decide(request())
    assert first.verdict is Verdict.OK
    assert pipe.decide(request()) == first
    assert len(repo.reserved) == 1


def test_FR18_재판정이_진행_중이면_같은_키는_다시_판정하지_않고_거부한다():
    pipe, repo, *_ = build()
    state = _retrying_link(repo)
    repo.raise_on = "rssi_since"
    pipe.decide(request())
    repo.raise_on = None
    repo.link_request(request(), "d")  # 첫 번째 재전송이 시도를 가져갔다
    assert state["in_progress"]
    d = pipe.decide(request())  # 동시에 온 두 번째 재전송
    assert d.verdict is Verdict.DENY and len(repo.reserved) == 0

def test_FR22_capacity_stops_before_beacon_and_saves_record():
    pipe, repo, prox, policy, audit = build()
    repo.link = RequestLink("full", True, False, capacity_exceeded=True)
    decision, context = pipe.decide_for_execution(request())
    assert decision.reason is ReasonCode.CAPACITY_EXCEEDED
    assert decision.verdict is Verdict.DENY and context is None
    assert repo.calls == ["link_request", "save_decision"]
    assert repo.saved == [decision] and not repo.reserved
    assert repo.records[0].model_version is None
    assert [event.step for event in audit.events] == [Step.DEDUP]
