"""FR-09 기기 조회. 저장소, 근접, 정책은 fake 로 대신한다."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.decision.query import DeviceQueryService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    AccessMode,
    Action,
    ApprovalMode,
    Capability,
    DeviceState,
    Occupancy,
    ReasonCode,
    TrustState,
    Verdict,
    Visibility,
)
from nearpilot.shared.models import (
    BeaconKey,
    NodeInfo,
    PolicyCheckResult,
    PolicyRevision,
    ProximityResult,
    RssiFrame,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("u", 1, 1)
OWNER = "owner-1"


def node(node_id="locker-1", cap=Capability.LOCKER, occ=Occupancy.RENTAL, **kw) -> NodeInfo:
    base = dict(
        node_id=node_id, place_id="p", capability=cap, occupancy=occ,
        trust_state=TrustState.TRUSTED, device_state=DeviceState.AVAILABLE,
        exclusive_group="g", last_seen_at=NOW - timedelta(seconds=1),
    )
    base.update(kw)
    return NodeInfo(**base)


def policy(node_id="locker-1", visibility=Visibility.PUBLIC, mode=AccessMode.OPEN) -> PolicyRevision:
    return PolicyRevision("pol", 1, node_id, visibility, mode, OWNER, ApprovalMode.ONE_TIME, 3600, 1)


def frame(node_id="locker-1") -> RssiFrame:
    return RssiFrame(node_id, BEACON, -60.0, NOW, "s1", 1, NOW, NOW)


class FakeRepo:
    def __init__(self):
        self.nodes_list = [node()]
        self.frames = [frame()]
        self.beacons = [BEACON]
        self.policies: dict[str, PolicyRevision] = {}
        self.policy_error: Exception | None = None
        self.calls: list[str] = []

    def beacons_of(self, user_id):
        self.calls.append("beacons_of")
        return self.beacons

    def rssi_since(self, beacons, since):
        return self.frames

    def nodes(self, capability=None):
        return self.nodes_list

    def current_policy(self, node_id):
        if self.policy_error:
            raise self.policy_error
        return self.policies.get(node_id) or policy(node_id)


class FakeProximity:
    def __init__(self, posteriors):
        self.posteriors, self.error = posteriors, None

    def posterior(self, beacons, candidates, at, *, place_nodes):
        if self.error:
            raise self.error
        return ProximityResult(self.posteriors, at, at, "m", "c")


class FakePolicy:
    def __init__(self, result=None):
        self.result = result or PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)
        self.calls: list[tuple[str, str, Action]] = []
        self.by_node: dict[str, PolicyCheckResult] = {}
        self.error_nodes: set[str] = set()

    def check(self, user_id, node_id, action, now):
        self.calls.append((user_id, node_id, action))
        if node_id in self.error_nodes:
            raise RuntimeError("secret internal detail")
        return self.by_node.get(node_id, self.result)


def build(posteriors=None, settings=None):
    repo, policy_check = FakeRepo(), FakePolicy()
    prox = FakeProximity({"locker-1": 0.97} if posteriors is None else posteriors)
    svc = DeviceQueryService(repo, prox, policy_check, settings or Settings(version="v1"), lambda: NOW)
    return svc, repo, prox, policy_check


def ids(listing):
    return [d["node_id"] for d in listing]


# ── find_available_devices ──────────────────────────────────────────


def test_FR09_조작_가능한_후보를_반환한다():
    svc, *_ = build()
    assert svc.find_available_devices("u1") == [
        {"node_id": "locker-1", "capability": "locker", "occupancy": "rental", "access": "operable"}]


def test_FR09_비콘이_없으면_빈_목록():
    svc, repo, *_ = build()
    repo.beacons = []
    assert svc.find_available_devices("u1") == []


def test_FR09_비콘_관측이_없으면_빈_목록():
    svc, repo, *_ = build()
    repo.frames = []
    assert svc.find_available_devices("u1") == []


def test_FR09_근접_확률이_임계값_미만이면_뺀다():
    svc, *_ = build({"locker-1": 0.9})
    assert svc.find_available_devices("u1") == []


def test_FR09_임계값이_없는_기능은_뺀다():
    svc, repo, *_ = build({"speaker-1": 0.99})
    repo.nodes_list = [node("speaker-1", Capability.SPEAKER, Occupancy.SHARED), node("light-1", Capability.LIGHT,
                                                                                     Occupancy.SHARED)]
    repo.frames = [frame("light-1")]
    assert ids(svc.find_available_devices("u1")) == []  # 스피커 임계값 None, 조명 확률 없음


@pytest.mark.parametrize("change", [
    {"trust_state": TrustState.DISCOVERED},
    {"device_state": DeviceState.FAULT},
    {"last_seen_at": NOW - timedelta(seconds=999)},
    {"last_seen_at": None},
    {"place_id": "other"},
])
def test_FR09_후보_조건을_만족하지_않으면_뺀다(change):
    svc, repo, *_ = build()
    anchor = node("anchor", Capability.LIGHT, Occupancy.SHARED)
    repo.nodes_list = [anchor, node("locker-1", **change)]
    repo.frames = [frame("anchor")]
    assert svc.find_available_devices("u1") == []


@pytest.mark.parametrize("state", [DeviceState.RESERVED, DeviceState.ACTIVE, DeviceState.CLOSING])
def test_FR09_점유_중인_기기는_뺀다(state):
    svc, repo, *_ = build()
    repo.nodes_list = [node(device_state=state)]
    assert svc.find_available_devices("u1") == []


def test_FR14_비공개_기기는_목록에서_숨긴다():
    svc, repo, *_ = build()
    repo.policies["locker-1"] = policy(visibility=Visibility.PRIVATE)
    assert svc.find_available_devices("u1") == []


def test_FR09_정책이_허용하면_조작_가능():
    svc, _, _, pol = build()
    assert svc.find_available_devices("u1")[0]["access"] == "operable"
    assert pol.calls == [("u1", "locker-1", Action.OPEN)]  # 사물함의 기본 동작


def test_FR09_승인이_필요하면_승인_필요():
    svc, _, _, pol = build()
    pol.result = PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 1)
    assert svc.find_available_devices("u1")[0]["access"] == "approval_required"


def test_FR09_정책이_거부하면_뺀다():
    svc, _, _, pol = build()
    pol.result = PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, 1)
    assert svc.find_available_devices("u1") == []


def test_FR09_정책_조회가_실패한_기기만_뺀다():
    low = replace(Settings(version="v1"), threshold_light=0.3)
    svc, repo, _, pol = build({"light-1": 0.5, "light-2": 0.4}, low)
    repo.nodes_list = [node("light-1", Capability.LIGHT, Occupancy.SHARED),
                       node("light-2", Capability.LIGHT, Occupancy.SHARED)]
    repo.frames = [frame("light-1")]
    pol.error_nodes = {"light-2"}
    assert ids(svc.find_available_devices("u1")) == ["light-1"]
    pol.error_nodes = {"light-1", "light-2"}
    assert svc.find_available_devices("u1") == []


def test_FR09_근접_계산_예외는_올린다():
    svc, _, prox, _ = build()
    prox.error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        svc.find_available_devices("u1")


def test_FR09_비콘_장소_불변_조건_위반은_예외():
    svc, repo, *_ = build()
    repo.beacons = [BEACON, BeaconKey("u", 1, 2)]
    with pytest.raises(ValueError):
        svc.find_available_devices("u1")
    repo.beacons = [BEACON]
    repo.nodes_list = [node("locker-1", place_id=None)]
    with pytest.raises(ValueError):
        svc.find_available_devices("u1")


def test_FR09_목록은_근접_확률_내림차순이다():
    low = replace(Settings(version="v1"), threshold_light=0.1)
    svc, repo, *_ = build({"light-1": 0.6, "light-2": 0.3}, low)
    repo.nodes_list = [node("light-2", Capability.LIGHT, Occupancy.SHARED),
                       node("light-1", Capability.LIGHT, Occupancy.SHARED)]
    repo.frames = [frame("light-1")]
    assert ids(svc.find_available_devices("u1")) == ["light-1", "light-2"]


def test_FR11_목록_항목에_내부_값이_없다():
    svc, *_ = build()
    (item,) = svc.find_available_devices("u1")
    assert set(item) == {"node_id", "capability", "occupancy", "access"}
    text = repr(item)
    assert "u1" not in text and "0.97" not in text and "'p'" not in text


# ── get_device_state ────────────────────────────────────────────────


def test_FR09_공개_기기_상태를_반환한다():
    svc, *_ = build()
    assert svc.get_device_state("anyone", "locker-1") == {
        "node_id": "locker-1", "capability": "locker", "occupancy": "rental", "state": "available",
        "online": True, "trusted": True, "access_mode": "open"}


def test_FR09_상태_조회는_근접_검사를_하지_않는다():
    svc, repo, prox, _ = build()
    repo.beacons = []
    prox.error = RuntimeError("boom")
    assert svc.get_device_state("anyone", "locker-1")["node_id"] == "locker-1"
    assert "beacons_of" not in repo.calls


@pytest.mark.parametrize("state,expected", [
    (DeviceState.AVAILABLE, "available"), (DeviceState.RESERVED, "occupied"),
    (DeviceState.ACTIVE, "occupied"), (DeviceState.CLOSING, "occupied"), (DeviceState.FAULT, "fault"),
])
def test_FR09_점유_상태는_주체_없이_반환한다(state, expected):
    svc, repo, *_ = build()
    repo.nodes_list = [node(device_state=state)]
    result = svc.get_device_state("anyone", "locker-1")
    assert result["state"] == expected
    assert set(result) == {"node_id", "capability", "occupancy", "state", "online", "trusted", "access_mode"}


def test_FR09_오프라인_미신뢰_기기도_상태를_보여_준다():
    svc, repo, *_ = build()
    repo.nodes_list = [node(last_seen_at=NOW - timedelta(seconds=999), trust_state=TrustState.DISCOVERED)]
    result = svc.get_device_state("anyone", "locker-1")
    assert (result["online"], result["trusted"]) == (False, False)
    repo.nodes_list = [node(last_seen_at=None)]
    assert svc.get_device_state("anyone", "locker-1")["online"] is False


def test_FR14_비공개_기기는_소유자만_조회한다():
    svc, repo, *_ = build()
    repo.policies["locker-1"] = policy(visibility=Visibility.PRIVATE, mode=AccessMode.RESTRICTED)
    result = svc.get_device_state(OWNER, "locker-1")
    assert (result["node_id"], result["access_mode"]) == ("locker-1", "restricted")


def test_FR14_비공개_기기는_타인에게_없는_기기와_같다():
    svc, repo, *_ = build()
    repo.policies["locker-1"] = policy(visibility=Visibility.PRIVATE)
    private = svc.get_device_state("someone", "locker-1")
    missing = svc.get_device_state("someone", "no-such-node")
    assert private == missing == {"reason_code": "TARGET_UNAVAILABLE"}


def test_FR09_상태_조회_예외는_올린다():
    svc, repo, *_ = build()
    repo.policy_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        svc.get_device_state("anyone", "locker-1")
