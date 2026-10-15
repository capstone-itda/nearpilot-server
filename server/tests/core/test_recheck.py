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


def ctx(use_id="use-1", node_id="locker-1") -> ExecutionContext:
    req = UseRequest(user_id="u1", request_id="r1", target=TargetSpec.parse("nearest:locker"),
                     action=Action.OPEN, host_kind="claude", received_at=NOW - timedelta(seconds=4))
    return ExecutionContext("i1", req, node_id, use_id, None, Settings(version="v1"))


class FakeRepo:
    def __init__(self):
        self.nodes_list = [node()]
        self.frames = [RssiFrame("locker-1", BEACON, -60.0, NOW)]
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
        return [BEACON]

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

    def posterior(self, beacons, candidates, at):
        self.calls.append([n.node_id for n in candidates])
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
    repo.nodes_list = [node(**kw), node("locker-2")]
    d = rc.recheck(ctx())
    assert (d.verdict, d.reason, d.node_id) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, "locker-1")
    assert prox.calls == []


def test_FR18_재검사_확률이_낮으면_MOVE_CLOSER이고_대상을_바꾸지_않는다():
    rc, repo, prox, pol, audit = build({"locker-1": 0.4, "locker-2": 0.99})
    repo.nodes_list = [node(), node("locker-2", device_state=DeviceState.AVAILABLE)]
    d = rc.recheck(ctx())
    assert prox.calls == [["locker-1", "locker-2"]]
    assert (d.verdict, d.reason, d.node_id) == (Verdict.ASK, ReasonCode.MOVE_CLOSER, "locker-1")
    assert pol.calls == []


def test_FR18_재검사는_다른_기기를_고르지_않는다():
    rc, repo, prox, pol, audit = build({"locker-1": 0.99, "locker-2": 0.999})
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


def test_FR18_재검사는_자기_예약을_타인_점유로_보지_않는다():
    rc, repo, *_ = build()
    assert repo.nodes_list[0].device_state is DeviceState.RESERVED
    assert rc.recheck(ctx()).verdict is Verdict.OK


def test_FR18_재검사_공용형은_예약_검사를_건너뛴다():
    rc, repo, *_ = build()
    d = rc.recheck(ctx(use_id=None))
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
