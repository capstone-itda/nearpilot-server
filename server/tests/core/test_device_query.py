"""FR-09 기기 조회. 저장소와 정책은 fake 로 대신한다."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.decision.query import DeviceQueryService
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import DescriptorError, legacy_descriptor
from nearpilot.shared.enums import (
    AccessMode,
    ApprovalMode,
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
    RssiFrame,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_NO_ARGUMENTS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
# 사물함 템플릿 선언의 동작 목록. 역할은 응답에 넣지 않는다 (FR-11).
LOCKER_ACTIONS = [
    {"name": "open", "description": "사물함을 연다", "input_schema": _NO_ARGUMENTS},
    {"name": "close", "description": "사물함을 잠근다. 반납이 아니다", "input_schema": _NO_ARGUMENTS},
]
BEACON = BeaconKey("u", 1, 1)
OWNER = "owner-1"


def node(node_id="locker-1", cap="locker", occ=Occupancy.RENTAL, **kw) -> NodeInfo:
    base = dict(
        node_id=node_id, place_id="p", kind=cap, occupancy=occ,
        trust_state=TrustState.TRUSTED, device_state=DeviceState.AVAILABLE,
        exclusive_group="g", last_seen_at=NOW - timedelta(seconds=1),
    )
    base.update(kw)
    if "capability" in base:
        base["kind"] = base.pop("capability")
    if "descriptor" not in base:
        base["descriptor"] = template(base["kind"], base["occupancy"])
    return NodeInfo(**base)

def template(kind, occupancy):
    """종류와 점유 방식의 v2 템플릿 선언. 역할 규칙에 맞지 않는 조합이면 None 이다."""
    try:
        return legacy_descriptor(kind, occupancy)
    except DescriptorError:
        return None



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
        self.open_sessions: set[tuple[str, str]] = set()  # (user_id, node_id) 열린 점유 세션
        self.session_error: Exception | None = None
        self.calls: list[str] = []

    def beacons_of(self, user_id):
        self.calls.append("beacons_of")
        return self.beacons

    def rssi_since(self, beacons, since):
        return [f for f in self.frames if f.beacon in beacons and f.received_at >= since]

    def nodes(self, capability=None):
        return self.nodes_list

    def current_policy(self, node_id):
        if self.policy_error:
            raise self.policy_error
        return self.policies.get(node_id) or policy(node_id)

    def has_open_session(self, user_id, node_id):
        self.calls.append("has_open_session")
        if self.session_error:
            raise self.session_error
        return (user_id, node_id) in self.open_sessions


class FakeReadiness:
    """상태 관리의 기기별 실행 가능 여부. 기본은 모두 가능하다."""

    def __init__(self, not_ready=()):
        self.not_ready = set(not_ready)
        self.error: Exception | None = None

    def can_execute(self, node_id):
        if self.error:
            raise self.error
        return node_id not in self.not_ready


class FakePolicy:
    def __init__(self, result=None):
        self.result = result or PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)
        self.calls: list[tuple[str, str, str]] = []
        self.by_node: dict[str, PolicyCheckResult] = {}
        self.error_nodes: set[str] = set()

    def check(self, user_id, node_id, action, now):
        self.calls.append((user_id, node_id, action))
        if node_id in self.error_nodes:
            raise RuntimeError("secret internal detail")
        return self.by_node.get(node_id, self.result)


def build(settings=None, readiness=None):
    repo, policy_check = FakeRepo(), FakePolicy()
    svc = DeviceQueryService(repo, policy_check, settings or Settings(version="v1"), lambda: NOW,
                             readiness or FakeReadiness())
    return svc, repo, policy_check


def ids(listing):
    return [d["node_id"] for d in listing]


# ── find_available_devices ──────────────────────────────────────────


def test_FR09_조작_가능한_후보를_반환한다():
    svc, *_ = build()
    assert svc.find_available_devices("u1") == [
        {"node_id": "locker-1", "kind": "locker", "occupancy": "rental", "access": "operable",
         "actions": LOCKER_ACTIONS}]


def test_FR09_비콘이_없으면_빈_목록():
    svc, repo, *_ = build()
    repo.beacons = []
    assert svc.find_available_devices("u1") == []


def test_FR09_비콘_관측이_없으면_빈_목록():
    svc, repo, *_ = build()
    repo.frames = []
    assert svc.find_available_devices("u1") == []


def test_FR09_같은_장소의_기기는_기기별_확률_없이_조회한다():
    svc, repo, *_ = build(settings=Settings(version="v1", threshold_locker=1.0, threshold_light=1.0))
    repo.nodes_list = [node(), node("locker-2"), node("light-1", "light", Occupancy.SHARED)]
    repo.frames = [frame("light-1")]  # 같은 장소의 기기 하나만 비콘을 봐도 된다.
    assert ids(svc.find_available_devices("u1")) == ["light-1", "locker-1", "locker-2"]


def test_FR09_임계값이_없는_기능도_조회한다():
    svc, repo, *_ = build(settings=Settings(version="v1", threshold_speaker=None))
    repo.nodes_list = [node("speaker-1", "speaker", Occupancy.SHARED), node("light-1", "light",
                                                                                     Occupancy.SHARED)]
    repo.frames = [frame("light-1")]
    assert ids(svc.find_available_devices("u1")) == ["light-1", "speaker-1"]


@pytest.mark.parametrize("change", [
    {"trust_state": TrustState.DISCOVERED},
    {"device_state": DeviceState.FAULT},
    {"last_seen_at": NOW - timedelta(seconds=999)},
    {"last_seen_at": None},
    {"place_id": "other"},
])
def test_FR09_후보_조건을_만족하지_않으면_뺀다(change):
    svc, repo, *_ = build()
    anchor = node("anchor", "light", Occupancy.SHARED)
    repo.nodes_list = [anchor, node("locker-1", **change)]
    repo.frames = [frame("anchor")]
    assert ids(svc.find_available_devices("u1")) == ["anchor"]


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
    svc, _, pol = build()
    assert svc.find_available_devices("u1")[0]["access"] == "operable"
    assert pol.calls == [("u1", "locker-1", "open")]  # 사물함의 기본 동작


def test_FR09_승인이_필요하면_승인_필요():
    svc, _, pol = build()
    pol.result = PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 1)
    assert svc.find_available_devices("u1")[0]["access"] == "approval_required"


def test_FR09_정책이_거부하면_뺀다():
    svc, _, pol = build()
    pol.result = PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, 1)
    assert svc.find_available_devices("u1") == []


def test_FR09_정책_조회가_실패한_기기만_뺀다():
    svc, repo, pol = build()
    repo.nodes_list = [node("light-1", "light", Occupancy.SHARED),
                       node("light-2", "light", Occupancy.SHARED)]
    repo.frames = [frame("light-1")]
    pol.error_nodes = {"light-2"}
    assert ids(svc.find_available_devices("u1")) == ["light-1"]
    pol.error_nodes = {"light-1", "light-2"}
    assert svc.find_available_devices("u1") == []


def test_FR09_최근_비콘_관측이_만료되면_빈_목록():
    svc, repo, *_ = build()
    assert ids(svc.find_available_devices("u1")) == ["locker-1"]
    old = NOW - timedelta(seconds=Settings().beacon_recent_sec + 1)
    repo.frames = [replace(frame(), frame_ts=old, sent_at=old, received_at=old)]
    assert svc.find_available_devices("u1") == []


@pytest.mark.parametrize("anchor", [
    node(trust_state=TrustState.DISCOVERED),
    node(trust_state=TrustState.REVOKED),
    node("other-node"),
])
def test_FR09_신뢰된_등록_기기의_비콘_수신이_필요하다(anchor):
    svc, repo, *_ = build()
    repo.nodes_list = [anchor]
    assert svc.find_available_devices("u1") == []


def test_FR09_다른_비콘_수신은_장소를_확인하지_못한다():
    svc, repo, *_ = build()
    repo.frames = [replace(frame(), beacon=BeaconKey("other", 1, 1))]
    assert svc.find_available_devices("u1") == []


def test_FR09_비콘_장소_불변_조건_위반은_예외():
    svc, repo, *_ = build()
    repo.beacons = [BEACON, BeaconKey("u", 1, 2)]
    with pytest.raises(ValueError):
        svc.find_available_devices("u1")
    repo.beacons = [BEACON]
    repo.nodes_list = [node("locker-1", place_id=None)]
    with pytest.raises(ValueError):
        svc.find_available_devices("u1")
    repo.nodes_list = [node(), node("light-1", "light", Occupancy.SHARED, place_id="other")]
    repo.frames = [frame(), frame("light-1")]
    with pytest.raises(ValueError):
        svc.find_available_devices("u1")


def test_FR09_목록은_기기_ID_순이다():
    svc, repo, *_ = build()
    repo.nodes_list = [node("light-2", "light", Occupancy.SHARED),
                       node("light-1", "light", Occupancy.SHARED)]
    repo.frames = [frame("light-1")]
    assert ids(svc.find_available_devices("u1")) == ["light-1", "light-2"]


def test_FR11_목록_항목에_내부_값이_없다():
    svc, *_ = build()
    (item,) = svc.find_available_devices("u1")
    assert set(item) == {"node_id", "kind", "occupancy", "access", "actions"}
    text = repr(item)
    assert "u1" not in text and "0.97" not in text and "'p'" not in text


# ── get_device_state ────────────────────────────────────────────────


def test_FR09_공개_기기_상태를_반환한다():
    svc, *_ = build()
    assert svc.get_device_state("anyone", "locker-1") == {
        "node_id": "locker-1", "kind": "locker", "occupancy": "rental", "state": "available",
        "online": True, "trusted": True, "access_mode": "open", "actions": LOCKER_ACTIONS,
        "reported": None}


def test_FR09_상태_조회는_근접_검사를_하지_않는다():
    svc, repo, *_ = build()
    repo.beacons = []
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
    assert set(result) == {"node_id", "kind", "occupancy", "state", "online", "trusted", "access_mode",
                           "actions", "reported"}


def test_FR09_device_query_speaker_controls():
    # 인자 스키마의 상한은 선언 값과 param_max 중 작은 값이다. 선언 값은 바꾸지 않는다 (FR-09).
    capped = Settings(version="v1", param_max={"speaker": {"on": {"volume": 10}}},
                      param_default={"speaker": {"on": {"volume": 10}}})
    for settings, controls, expected in [
        (None, {"volume_max": 30}, 20),  # 서버 상한이 더 작다
        (None, {"volume_max": 8}, 8),  # 선언 상한이 더 작다
        (capped, {"volume_max": 30}, 10),  # 서버 상한은 설정에서 읽는다
        (None, {}, None),  # 인자를 선언하지 않았다
    ]:
        svc, repo, *_ = build(settings=settings)
        descriptor = legacy_descriptor("speaker", Occupancy.SHARED, **controls)
        repo.nodes_list = [node("speaker-1", "speaker", Occupancy.SHARED, descriptor=descriptor)]
        on, off = svc.get_device_state("anyone", "speaker-1")["actions"]
        volume = on["input_schema"]["properties"].get("volume")
        assert (volume and volume["maximum"]) == expected
        assert set(on) == {"name", "description", "input_schema"} and off["name"] == "off"
        assert descriptor.action("on").properties.get("volume", {}).get("maximum") == controls.get("volume_max")


def test_FR09_device_query_new_kind_actions():
    # 서버가 모르는 종류도 선언 그대로 동작 목록을 보여 준다 (FR-09, NFR-07).
    from nearpilot.shared.descriptor import parse_descriptor
    fan = parse_descriptor({
        "format": "nearpilot.device/1", "kind": "fan", "occupancy": "shared",
        "vendor": {"name": "acme", "model": "f1", "firmware": "1.0"},
        "actions": [
            {"name": "spin", "description": "팬을 돌린다", "result": {"kind": "signal_output"},
             "input_schema": {"type": "object", "properties": {"speed": {"type": "integer", "minimum": 1,
                                                                          "maximum": 5}},
                              "required": [], "additionalProperties": False}},
            {"name": "stop", "description": "팬을 멈춘다", "role": "deactivate", "result": {"kind": "signal_output"},
             "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
        ]})
    svc, repo, *_ = build(settings=Settings(version="v1", param_max={"fan": {"spin": {"speed": 3}}}))
    repo.nodes_list = [node("fan-1", "fan", Occupancy.SHARED, descriptor=fan)]
    state = svc.get_device_state("anyone", "fan-1")
    assert state["kind"] == "fan"
    assert [a["name"] for a in state["actions"]] == ["spin", "stop"]
    assert state["actions"][0]["input_schema"]["properties"]["speed"]["maximum"] == 3
    assert "acme" not in repr(state) and fan.hash not in repr(state)  # 제작사와 해시는 넣지 않는다 (FR-11)


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


def test_FR09_비공개_기기를_점유_중인_본인은_조회한다():
    svc, repo, *_ = build()
    repo.nodes_list = [node(device_state=DeviceState.ACTIVE)]
    repo.policies["locker-1"] = policy(visibility=Visibility.PRIVATE, mode=AccessMode.RESTRICTED)
    repo.open_sessions = {("someone", "locker-1")}
    assert svc.get_device_state("someone", "locker-1") == {  # 점유 주체 키가 없다
        "node_id": "locker-1", "kind": "locker", "occupancy": "rental", "state": "occupied",
        "online": True, "trusted": True, "access_mode": "restricted", "actions": LOCKER_ACTIONS,
        "reported": None}
    repo.open_sessions = set()  # 세션이 닫혔다
    assert svc.get_device_state("someone", "locker-1") == {"reason_code": "TARGET_UNAVAILABLE"}


def test_FR09_공개_기기와_소유자_조회는_본인_세션을_읽지_않는다():
    svc, repo, *_ = build()
    assert svc.get_device_state("someone", "locker-1")["node_id"] == "locker-1"  # 공개 기기
    repo.policies["locker-1"] = policy(visibility=Visibility.PRIVATE)
    assert svc.get_device_state(OWNER, "locker-1")["node_id"] == "locker-1"  # 비공개 기기의 소유자
    assert "has_open_session" not in repo.calls


def test_FR09_상태_조회_예외는_올린다():
    svc, repo, *_ = build()
    repo.policy_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        svc.get_device_state("anyone", "locker-1")


def test_FR09_본인_세션_조회_예외는_올린다():
    svc, repo, *_ = build()
    repo.policies["locker-1"] = policy(visibility=Visibility.PRIVATE)
    repo.session_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        svc.get_device_state("someone", "locker-1")


def test_FR09_복원_중인_기기는_목록에서_뺀다():
    svc, repo, *_ = build(readiness=FakeReadiness({"locker-1"}))
    assert svc.find_available_devices("u1") == []


def test_FR11_복원_사실을_공개하지_않는다():
    svc, *_ = build(readiness=FakeReadiness({"locker-1"}))
    assert "복원" not in repr(svc.find_available_devices("u1"))


# ── 보고 상태 (FR-09, #289) ─────────────────────────────────────────


class FakeReported:
    def __init__(self, state=None, error: Exception | None = None):
        self.state, self.error = state, error
        self.calls: list[str] = []

    def read(self, node_id):
        self.calls.append(node_id)
        if self.error is not None:
            raise self.error
        return self.state


def build_reported(reported):
    repo, policy_check = FakeRepo(), FakePolicy()
    svc = DeviceQueryService(repo, policy_check, Settings(version="v1"), lambda: NOW, FakeReadiness(),
                             reported=reported)
    return svc, repo


def test_FR09_device_state_includes_reported():
    from nearpilot.shared.models import ReportedState, ReportedValue
    observed = datetime(2026, 11, 13, 12, 31, 14, tzinfo=timezone.utc)
    reported = FakeReported(ReportedState("locker-1", (
        ReportedValue("power", "on", NOW), ReportedValue("volume", 2, observed),
        ReportedValue("mute", None, None)), NOW))
    svc, _ = build_reported(reported)
    result = svc.get_device_state("anyone", "locker-1")
    assert result["reported"] == {
        "power": {"value": "on", "observed_at": NOW.isoformat()},
        "volume": {"value": 2, "observed_at": observed.isoformat()},
        "mute": {"value": None, "observed_at": None}}
    assert reported.calls == ["locker-1"]


def test_FR09_mqtt_node_reported_null():
    reported = FakeReported(None)
    svc, _ = build_reported(reported)
    assert svc.get_device_state("anyone", "locker-1")["reported"] is None
    svc, _ = build_reported(None)
    assert svc.get_device_state("anyone", "locker-1")["reported"] is None


def test_FR09_reported_for_other_node_is_ignored():
    from nearpilot.shared.models import ReportedState, ReportedValue
    svc, _ = build_reported(FakeReported(ReportedState("other", (ReportedValue("power", "on"),), NOW)))
    assert svc.get_device_state("anyone", "locker-1")["reported"] is None


def test_FR09_hidden_node_does_not_read_reported():
    reported = FakeReported(None)
    svc, repo = build_reported(reported)
    repo.policies["locker-1"] = policy(visibility=Visibility.PRIVATE)
    assert svc.get_device_state("stranger", "locker-1") == {"reason_code": "TARGET_UNAVAILABLE"}
    assert svc.get_device_state("anyone", "no-such") == {"reason_code": "TARGET_UNAVAILABLE"}
    assert reported.calls == []


def test_FR09_reader_error_keeps_state():
    svc, _ = build_reported(FakeReported(error=RuntimeError("secret internal detail")))
    result = svc.get_device_state("anyone", "locker-1")
    assert result["reported"] is None and result["state"] == "available"
    assert "secret" not in repr(result)
