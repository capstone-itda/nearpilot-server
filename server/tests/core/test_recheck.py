"""FR-18 실행 직전 재검사. 다른 계층은 fake 로 대신한다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.decision.recheck import Rechecker
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action,
    Capability,
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


def node(node_id="locker-1", **kw) -> NodeInfo:
    base = dict(
        node_id=node_id, place_id="p", capability=Capability.LOCKER, occupancy=Occupancy.RENTAL,
        trust_state=TrustState.TRUSTED, device_state=DeviceState.RESERVED,  # 자기 예약
        exclusive_group="g", last_seen_at=NOW - timedelta(seconds=1),
    )
    base.update(kw)
    return NodeInfo(**base)


def anchor() -> NodeInfo:
    """비콘을 본 기기. 후보가 아닌 종류(조명)로 두어서 후보 조건과 분리한다."""
    return node("anchor-1", occupancy=Occupancy.SHARED, capability=Capability.LIGHT,
                device_state=DeviceState.AVAILABLE)


def frame(node_id="locker-1") -> RssiFrame:
    return RssiFrame(node_id, BEACON, -60.0, NOW, "s1", 1, NOW, NOW)


def ctx(use_id="use-1", node_id="locker-1", target="nearest:locker",
        kind=ExecutionKind.RENTAL_INITIAL, settings=None, usage_id=None) -> ExecutionContext:
    req = UseRequest(user_id="u1", request_id="r1", target=TargetSpec.parse(target),
                     action=Action.OPEN, host_kind="claude", received_at=NOW - timedelta(seconds=4))
    return ExecutionContext("i1", req, node_id, use_id, usage_id, settings or Settings(version="v1"),
                            execution_kind=kind)


class FakeRepo:
    def __init__(self):
        self.nodes_list = [node()]
        self.frames = [frame()]
        self.beacons = [BEACON]
        self.session_obj = UseSession("use-1", "i1", "locker-1", SessionState.RESERVED)
        self.owner_ok = True
        self.calls: list[str] = []
        self.raise_on: str | None = None

    def _hit(self, name):
        self.calls.append(name)
        if self.raise_on == name:
            raise RuntimeError("secret internal detail")

    def beacons_of(self, user_id):
        self._hit("beacons_of")
        return self.beacons

    def rssi_since(self, beacons, since):
        self._hit("rssi_since")
        return self.frames

    def nodes(self, capability=None):
        self._hit("nodes")
        return self.nodes_list

    def session(self, use_id):
        self._hit("session")
        return self.session_obj

    def session_owner_matches(self, use_id, user_id):
        self._hit("session_owner_matches")
        return self.owner_ok

    def save_decision(self, *a, **k):  # 재검사는 저장하지 않는다
        raise AssertionError("재검사가 저장소를 바꿨다")

    def reserve_if_free(self, *a, **k):
        raise AssertionError("재검사가 ⑤⑥을 다시 실행했다")


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
        super().__init__(PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, 2))
        self.own = own

    def check(self, user_id, node_id, action, now, *, internal_request_id=None):
        exhausted = super().check(user_id, node_id, action, now, internal_request_id=internal_request_id)
        if internal_request_id == self.own:
            return PolicyCheckResult(Verdict.OK, ReasonCode.OK, 2, approval_id="a1")
        return exhausted


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def build(posteriors=None, policy=None):
    repo = FakeRepo()
    prox = FakeProximity({"locker-1": 0.99} if posteriors is None else posteriors)
    pol = policy or FakePolicy()
    audit = FakeAudit()
    n = iter(range(10_000))
    rc = Rechecker(repo, prox, pol, audit, clock=lambda: NOW, new_id=lambda: f"e{next(n)}")
    return rc, repo, prox, pol, audit


def test_FR18_재검사는_모두_통과하면_OK이고_저장하지_않는다():
    rc, repo, *_ = build()
    d = rc.recheck(ctx())  # 저장소를 바꾸면 fake 가 AssertionError 를 낸다
    assert (d.verdict, d.reason, d.node_id, d.use_id) == (Verdict.OK, ReasonCode.OK, "locker-1", "use-1")


def test_FR18_재검사_비콘이_없으면_거부한다():
    rc, repo, prox, pol, audit = build()
    repo.frames = []
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert "nodes" not in repo.calls and prox.calls == [] and pol.calls == []


@pytest.mark.parametrize("kw", [
    {"device_state": DeviceState.FAULT},
    {"trust_state": TrustState.REVOKED},
    {"last_seen_at": NOW - timedelta(seconds=999)},
])
def test_FR18_재검사_대상이_후보_밖이면_거부한다(kw):
    rc, repo, prox, pol, audit = build()
    repo.nodes_list = [node(**kw), node("locker-2"), anchor()]
    repo.frames = [frame("anchor-1")]
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, "locker-1")
    assert prox.calls == []


def test_FR18_재검사_확률이_낮으면_MOVE_CLOSER이고_대상을_바꾸지_않는다():
    rc, repo, prox, pol, audit = build({"locker-1": 0.4, "locker-2": 0.59})
    repo.nodes_list = [node(), node("locker-2", device_state=DeviceState.AVAILABLE)]
    d = rc.recheck(ctx())
    assert prox.calls == [["locker-1", "locker-2"]]
    assert (d.verdict, d.reason, d.node_id) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, "locker-1")
    assert pol.calls == []


def test_FR18_재검사는_다른_기기를_고르지_않는다():
    rc, repo, prox, pol, audit = build({"locker-1": 0.96, "locker-2": 0.03})
    repo.nodes_list = [node(), node("locker-2", device_state=DeviceState.AVAILABLE)]
    d = rc.recheck(ctx())
    assert d.node_id == "locker-1" and pol.calls == ["locker-1"]


@pytest.mark.parametrize("verdict,reason", [
    (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING),
    (Verdict.NEED_APPROVAL, ReasonCode.ACTION_NOT_ALLOWED),
    (Verdict.DENY, ReasonCode.POLICY_DENIED),
])
def test_FR18_재검사_정책_결과를_그대로_반영한다(verdict, reason):
    rc, *_ = build(policy=FakePolicy(PolicyCheckResult(verdict, reason, 2)))
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason) == (verdict, reason)


def test_FR19_recheck_ignores_own_reserved_usage():
    # ④는 이 요청의 ID 를 넘겨 ⑤가 예약한 자기 사용분을 남은 횟수에서 뺀다. 다른 요청의 사용분으로 소진됐으면 거부한다
    rc, repo, prox, pol, audit = build(policy=OwnUsagePolicy(own="i1"))
    d = rc.recheck(ctx(usage_id="usage-1"))
    assert (d.verdict, d.reason) == (Verdict.OK, ReasonCode.OK)
    assert pol.request_ids == ["i1"]
    rc, *_ = build(policy=OwnUsagePolicy(own="other"))
    d = rc.recheck(ctx(usage_id="usage-1"))
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED)


@pytest.mark.parametrize("verdict,reason", [
    (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED),
    (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING),
    (Verdict.NEED_APPROVAL, ReasonCode.ACTION_NOT_ALLOWED),
    (Verdict.DENY, ReasonCode.POLICY_DENIED),
    (Verdict.DENY, ReasonCode.APPROVAL_EXHAUSTED),
])
def test_FR19_재검사_자기_사용분은_승인_소진만_통과시킨다(verdict, reason):
    # 자기 사용분을 뺀 뒤에도 정책이 막으면 usage_id 가 있어도 그 판정으로 멈춘다
    rc, repo, prox, pol, audit = build(policy=FakePolicy(PolicyCheckResult(verdict, reason, 2)))
    d = rc.recheck(ctx(usage_id="usage-1"))
    assert (d.verdict, d.reason) == (verdict, reason)
    assert pol.request_ids == ["i1"]


def test_FR18_재검사는_자기_예약을_타인_점유로_보지_않는다():
    rc, repo, *_ = build()
    assert repo.nodes_list[0].device_state is DeviceState.RESERVED
    assert rc.recheck(ctx()).verdict is Verdict.OK


def test_FR18_재검사_공용형은_예약_검사를_건너뛴다():
    rc, repo, *_ = build()
    d = rc.recheck(ctx(use_id=None, kind=ExecutionKind.SHARED))
    assert d.verdict is Verdict.OK and "session" not in repo.calls


@pytest.mark.parametrize("mutate", [
    lambda r: setattr(r, "session_obj", None),
    lambda r: setattr(r, "session_obj", UseSession("use-1", "i1", "locker-1", SessionState.CLOSED)),
    lambda r: setattr(r, "session_obj", UseSession("use-1", "i1", "locker-2", SessionState.RESERVED)),
    lambda r: setattr(r, "owner_ok", False),
])
def test_FR18_재검사_예약이_유효하지_않으면_RECHECK_FAILED(mutate):
    rc, repo, *_ = build()
    mutate(repo)
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.RECHECK_FAILED)


@pytest.mark.parametrize("mutate", [
    lambda r: setattr(r, "session_obj", None),
    lambda r: setattr(r, "session_obj", UseSession("use-1", "i1", "locker-1", SessionState.CLOSED)),
    lambda r: setattr(r, "owner_ok", False),
])
def test_FR19_자기_사용분으로_정책을_통과해도_예약을_검사한다(mutate):
    rc, repo, *_ = build(policy=OwnUsagePolicy(own="i1"))
    mutate(repo)
    d = rc.recheck(ctx(usage_id="usage-1"))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.RECHECK_FAILED)


@pytest.mark.parametrize("name", ["beacons_of", "rssi_since", "nodes", "session", "session_owner_matches"])
def test_FR18_재검사_예외는_RECHECK_FAILED로_거부한다(name):
    rc, repo, prox, pol, audit = build()
    repo.raise_on = name
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.RECHECK_FAILED)
    assert "secret" not in repr(d)
    assert any("secret" in str(e.payload) for e in audit.events)  # 예외 내용은 감사에만


def test_FR18_재검사_근접_정책_예외도_RECHECK_FAILED():
    rc, repo, prox, pol, audit = build()
    prox.posterior = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    assert rc.recheck(ctx()).reason is ReasonCode.RECHECK_FAILED
    rc, repo, prox, pol, audit = build()
    pol.check = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    assert rc.recheck(ctx()).reason is ReasonCode.RECHECK_FAILED


def test_FR18_재검사_정책의_알_수_없는_판정은_OK가_되지_않는다():
    rc, *_ = build(policy=FakePolicy(PolicyCheckResult(Verdict.BUSY, ReasonCode.OK, 1)))
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.RECHECK_FAILED)


def test_FR18_재검사_감사_기록이_실패하면_OK가_되지_않는다():
    rc, repo, prox, pol, audit = build()
    audit.append = lambda e: (_ for _ in ()).throw(OSError("disk"))
    assert rc.recheck(ctx()).verdict is Verdict.DENY


def test_FR18_재검사는_같은_입력에_같은_판정을_반환한다():
    assert build()[0].recheck(ctx()) == build()[0].recheck(ctx())


def test_FR18_재검사는_감사_기록에_설정_버전을_남긴다():
    rc, repo, prox, pol, audit = build()
    rc.recheck(ctx())
    assert audit.events[-1].payload["settings_version"] == "v1"
    assert audit.events[-1].internal_request_id == "i1"


def test_FR11_재검사_Decision에는_사용자_ID가_없다():
    rc, *_ = build()
    assert "u1" not in repr(rc.recheck(ctx()))


def test_FR18_재검사_재조작은_ACTIVE_세션이어야_통과한다():
    rc, repo, *_ = build()
    c = ctx(kind=ExecutionKind.RENTAL_REOPERATION)
    repo.session_obj = UseSession("use-1", "i0", "locker-1", SessionState.ACTIVE)
    assert rc.recheck(c).verdict is Verdict.OK
    repo.session_obj = UseSession("use-1", "i0", "locker-1", SessionState.RESERVED)
    assert rc.recheck(c).reason is ReasonCode.RECHECK_FAILED


def test_FR18_재검사_최초_대여는_ACTIVE_세션을_통과시키지_않는다():
    rc, repo, *_ = build()
    repo.session_obj = UseSession("use-1", "i1", "locker-1", SessionState.ACTIVE)
    assert rc.recheck(ctx()).reason is ReasonCode.RECHECK_FAILED


def test_FR18_재검사는_대상_장소의_전체_기기를_근접에_넘긴다():
    rc, repo, prox, pol, audit = build()
    repo.nodes_list = [node(), node("other-1", place_id="q"), anchor()]
    assert rc.recheck(ctx()).verdict is Verdict.OK
    assert sorted(prox.place_nodes[0]) == ["anchor-1", "locker-1"] and prox.calls == [["locker-1"]]


def test_FR18_재검사_비콘을_다른_장소만_봤으면_거부한다():
    rc, repo, prox, pol, audit = build()
    repo.nodes_list = [node(), node("other-1", place_id="q")]
    repo.frames = [frame("other-1")]
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
    assert prox.calls == []


def test_FR18_재검사_활성_비콘이_둘이면_RECHECK_FAILED():
    rc, repo, *_ = build()
    repo.beacons = [BEACON, BeaconKey("u", 1, 2)]
    assert rc.recheck(ctx()).reason is ReasonCode.RECHECK_FAILED


def test_FR18_재검사_명시_지정은_별도_임계값을_쓰고_없으면_RECHECK_FAILED():
    settings = Settings(version="v1", threshold_locker=0.95, threshold_explicit_locker=0.6)
    rc, *_ = build({"locker-1": 0.7})
    assert rc.recheck(ctx(target="locker-1", settings=settings)).verdict is Verdict.OK
    assert rc.recheck(ctx()).reason is ReasonCode.MOVE_CLOSER  # 일반 임계값 0.95
    assert rc.recheck(ctx(target="locker-1")).reason is ReasonCode.RECHECK_FAILED


# ── ③ 이후에 막히면 확률을 감사에 남긴다 (NFR-13) ──────────────────────────


def _low_probability(repo, prox, pol):
    prox.posteriors = {"locker-1": 0.4}


def _policy_denies(repo, prox, pol):
    pol.result = PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, 2)


def _reservation_invalid(repo, prox, pol):
    repo.owner_ok = False


def _beacon_gone(repo, prox, pol):
    repo.frames = []


def _target_gone(repo, prox, pol):
    repo.nodes_list = [node(device_state=DeviceState.FAULT), anchor()]
    repo.frames = [frame("anchor-1")]


def _check_fails(repo, prox, pol):
    repo.raise_on = "nodes"


@pytest.mark.parametrize("mutate,reason,p", [
    pytest.param(_low_probability, ReasonCode.MOVE_CLOSER, 0.4, id="③ 확률 부족"),
    pytest.param(_policy_denies, ReasonCode.POLICY_DENIED, 0.99, id="④ 정책 거부"),
    pytest.param(_reservation_invalid, ReasonCode.RECHECK_FAILED, 0.99, id="자기 예약이 유효하지 않음"),
    pytest.param(_beacon_gone, ReasonCode.BEACON_NOT_SEEN, None, id="① 비콘 없음"),
    pytest.param(_target_gone, ReasonCode.TARGET_UNAVAILABLE, None, id="② 대상이 후보 밖"),
    pytest.param(_check_fails, ReasonCode.RECHECK_FAILED, None, id="검사 오류"),
])
def test_NFR13_재검사가_막으면_확률을_감사에_남긴다(mutate, reason, p):
    rc, repo, prox, pol, audit = build()
    mutate(repo, prox, pol)
    c = ctx()
    d = rc.recheck(c)
    assert d.reason is reason
    payload = audit.events[-1].payload
    if p is None:  # 확률을 계산하지 않았다
        assert "p" not in payload and "threshold" not in payload
    else:
        assert (payload["p"], payload["threshold"]) == (p, c.settings.threshold_locker)
        assert d.p_max == p  # 판정 값은 그대로다
