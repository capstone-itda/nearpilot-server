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
                                 usage_id=self.reserve_usage_id)
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

    def check(self, user_id, node_id, action, now):
        self.calls.append(node_id)
        return self.result


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def build(posteriors=None, policy=None, settings=None):
    repo = FakeRepo()
    prox = FakeProximity({"locker-1": 0.99} if posteriors is None else posteriors)
    pol = policy or FakePolicy()
    audit = FakeAudit()
    n = iter(range(10_000))
    pipe = DecisionPipeline(repo, prox, pol, audit, settings or Settings(
        version="v1", threshold_explicit_locker=0.5, threshold_explicit_light=0.5),
                            new_id=lambda: f"e{next(n)}")
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


def test_FR18_활성_비콘이_둘이면_INTERNAL_ERROR():
    pipe, repo, *_ = build()
    repo.beacons = [BEACON, BeaconKey("u", 1, 2)]
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert "rssi_since" not in repo.calls


def test_FR18_활성_비콘이_없으면_BEACON_NOT_SEEN():
    pipe, repo, *_ = build()
    repo.beacons = []
    d = pipe.decide(request())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert "rssi_since" not in repo.calls


def test_FR18_신뢰하지_않는_기기만_비콘을_봤으면_BEACON_NOT_SEEN():
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
