"""거리 선택자의 shared 계약 검사 (FR-09, FR-16, FR-22, #253)."""

import inspect
import json
import typing
from dataclasses import fields
from datetime import datetime, timezone

import pytest

from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings, settings_from_data, settings_to_data
from nearpilot.shared.enums import ReasonCode, TargetKind, Verdict
from nearpilot.shared.interfaces import NodeEventSink, Repository
from nearpilot.shared.models import (
    Decision,
    DecisionRecord,
    DeviceOutcome,
    DistanceOrder,
    RssiReport,
    SelectorChild,
    SelectorChildren,
    SelectorRecord,
    TargetSpec,
    UseRequest,
)

RECEIVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

# 거리 선택자가 아닌 요청의 중복 판단 요약. 거리 선택자를 더하기 전 코드에서 얻은 값이다.
EXISTING_DIGESTS = (
    ("nearest:locker", "open", None, None, "5d73e1bd902ce876748572037396a505b6f50738e4927e229d17de84f73cbcb5"),
    ("locker-1", "open", None, None, "c582b00f84ac0e0ced1041e97bea48099961eff487dae055b6f0828f0669b998"),
    ("any:locker", "open", None, None, "cd51fa8b67ae2dde90b65f4a6dd0eb4f6658bb7a1b5621d4cb962406f3f5b510"),
    ("nearest:light", "on", {"brightness": 10}, None,
     "aa1ff7f98ffd329206a249f57bd874d5063abcbcea60018c8bc15405d5f3b095"),
    ("light-1", "off", None, "p-1", "35d6624712f3b88d89ae81743450f3d31273a8095da57141bb6e256796894382"),
)


def _request(target, action, arguments=None, parent=None):
    return UseRequest("u", "r", TargetSpec.parse(target), action, None, RECEIVED_AT, parent, arguments)


# ── 대상 지정 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "kind", "start", "count"),
    [
        ("near:light:1:2", TargetKind.NEAR, 1, 2),
        ("far:light:1:1", TargetKind.FAR, 1, 1),
        ("near:light:2:all", TargetKind.NEAR, 2, None),
    ],
)
def test_FR09_selector_parse_near_far_all(raw, kind, start, count):
    spec = TargetSpec.parse(raw)
    assert (spec.kind, spec.device_kind, spec.node_id, spec.start, spec.count) == (kind, "light", None, start, count)
    assert spec.is_selector
    assert spec.serialize() == raw


@pytest.mark.parametrize(
    ("raw", "kind", "device_kind", "node_id"),
    [
        ("nearest:locker", TargetKind.NEAREST, "locker", None),
        ("any:locker", TargetKind.ANY, "locker", None),
        ("locker-1", TargetKind.EXPLICIT, None, "locker-1"),
    ],
)
def test_FR09_selector_parse_keeps_existing_forms(raw, kind, device_kind, node_id):
    spec = TargetSpec.parse(raw)
    assert (spec.kind, spec.device_kind, spec.node_id) == (kind, device_kind, node_id)
    assert (spec.start, spec.count) == (None, None)
    assert not spec.is_selector
    assert spec.serialize() == raw


@pytest.mark.parametrize(
    "raw",
    [
        # 거리 선택자의 시작·개수·종류 규칙
        "near:light:0:1", "near:light:1:0", "near:light:x:2", "near:light:1", "near:light:01:2",
        "near:light:1:ALL", "near:Light:1:2", "far:locker",
        # 마디 수와 문자
        "near:light:1:2:3", "near:light:1:", "near::1:2", "near:light::2",
        "near:light:-1:2", "near:light:1:+2", "near:light:1.0:2", "near:light:1:2.5",
        "near:light:1:2\n", "near:light:1:all ", "near:light: 1:2", "near:light:１:2",
        # 파이썬 `int()` 는 받아들이지만 규칙 `[1-9][0-9]*` 에는 어긋나는 문자열
        "near:light:1_0:2", "near:light:1:1_0", "near:light:1 :2", "near:light:1:2 ", "near:light:1\n:2",
        # 거리 선택자가 아닌 지정은 지금처럼 나머지 전체가 종류 이름이다
        "nearest:a:b", "any:locker:1:2",
    ],
)
def test_FR09_selector_parse_malformed_value_error(raw):
    with pytest.raises(ValueError):
        TargetSpec.parse(raw)


@pytest.mark.parametrize(
    ("kind", "start", "count"),
    [
        (TargetKind.NEAREST, 1, None),  # 거리 선택자가 아닌 지정은 start·count 를 담지 않는다
        (TargetKind.NEAREST, None, 1),
        (TargetKind.ANY, 1, 1),
        (TargetKind.NEAR, 0, 1),
        (TargetKind.NEAR, True, 1),  # bool 은 정수로 보지 않는다
        (TargetKind.NEAR, None, 1),  # 시작은 꼭 있어야 한다
        (TargetKind.NEAR, 1.0, 1),
        (TargetKind.FAR, 1, 0),
        (TargetKind.FAR, 1, True),
        (TargetKind.FAR, 1, False),
        (TargetKind.FAR, 1, 1.5),
    ],
)
def test_FR09_selector_direct_construction_validated(kind, start, count):
    with pytest.raises(ValueError):
        TargetSpec(kind, "light", start=start, count=count)


def test_FR09_selector_direct_construction_explicit_rejects_start_count():
    with pytest.raises(ValueError):
        TargetSpec(TargetKind.EXPLICIT, None, "light-1", start=1)
    with pytest.raises(ValueError):
        TargetSpec(TargetKind.EXPLICIT, None, "light-1", count=1)


def test_FR09_selector_direct_construction_all_is_none_count():
    spec = TargetSpec(TargetKind.FAR, "light", start=3)
    assert spec.count is None
    assert spec.serialize() == "far:light:3:all"
    assert TargetSpec.parse(spec.serialize()) == spec


# ── 중복 판단 요약 ─────────────────────────────────────────────────


def test_FR22_existing_digest_unchanged():
    for target, action, arguments, parent, expected in EXISTING_DIGESTS:
        got = input_digest(_request(target, action, arguments, parent))
        assert got == expected, target

    base = input_digest(_request("near:light:1:2", "off"))
    assert base == input_digest(_request("near:light:1:2", "off"))
    for other in ("near:light:2:2", "near:light:1:all", "far:light:1:2", "nearest:light"):
        assert base != input_digest(_request(other, "off")), other


# ── 설정 ───────────────────────────────────────────────────────────


def test_FR09_settings_selector_values_validated():
    default = Settings()
    assert (default.anchor_recent_sec, default.order_similar_db) == (3.0, 3.0)

    assert default.rssi_frame_sec == 1.0
    with pytest.raises(ValueError):
        Settings(anchor_recent_sec=0.5)  # rssi_frame_sec 보다 작다
    assert Settings(anchor_recent_sec=1.0).anchor_recent_sec == 1.0  # 같으면 된다
    with pytest.raises(ValueError):
        Settings(order_similar_db=0)
    with pytest.raises(ValueError):
        Settings(order_similar_db=-1)
    with pytest.raises(ValueError):
        Settings(anchor_recent_sec=True)
    for name in ("anchor_recent_sec", "order_similar_db"):
        for value in (float("nan"), float("inf"), True, "3"):
            with pytest.raises(ValueError):
                Settings(**{name: value})

    changed = Settings(anchor_recent_sec=4.5, order_similar_db=2.5)
    restored = settings_from_data(settings_to_data(changed))
    assert (restored.anchor_recent_sec, restored.order_similar_db) == (4.5, 2.5)
    assert restored == changed

    old_snapshot = settings_to_data(Settings())
    del old_snapshot["anchor_recent_sec"], old_snapshot["order_similar_db"]
    old = settings_from_data(old_snapshot)
    assert (old.anchor_recent_sec, old.order_similar_db) == (3.0, 3.0)


BOUNDARY_SETTINGS = ("order_frame_sigma_db", "order_static_sigma_db", "order_boundary_min_prob")
BOUNDARY_DEFAULTS = (4.0, 3.5, 0.9)


def _boundary_cases():
    """(바꾼 설정, 허용 여부). 허용·거부 경계 값과, 세 설정 모두에서 숫자가 아니거나 유한하지 않은 값."""
    allowed = [
        {"order_static_sigma_db": 0},  # 고정 오차는 0 이상이다
        {"order_boundary_min_prob": 0.5},  # 기준 확률은 0.5 이상 1 미만이다
        {"order_boundary_min_prob": 0.99},
    ]
    rejected = [
        {"order_frame_sigma_db": 0},  # 프레임 잡음은 0 초과다
        {"order_frame_sigma_db": -1},
        {"order_static_sigma_db": -0.1},
        {"order_boundary_min_prob": 0.49},
        {"order_boundary_min_prob": 1.0},
        {"order_boundary_min_prob": 1},  # TOML 의 정수 1
    ]
    rejected += [
        {name: value}
        for name in BOUNDARY_SETTINGS
        for value in (float("nan"), float("inf"), True, "1")
    ]
    cases = [(overrides, True) for overrides in allowed] + [(overrides, False) for overrides in rejected]
    return [
        pytest.param(overrides, ok, id=f"{'허용' if ok else '거부'} {overrides}") for overrides, ok in cases
    ]


@pytest.mark.parametrize(("overrides", "allowed"), _boundary_cases())
def test_FR09_settings_boundary_values_validated(overrides, allowed):
    default = Settings()
    assert tuple(getattr(default, name) for name in BOUNDARY_SETTINGS) == BOUNDARY_DEFAULTS

    if not allowed:
        with pytest.raises(ValueError):
            Settings(**overrides)
        return

    changed = Settings(**overrides)
    assert all(getattr(changed, name) == value for name, value in overrides.items())
    snapshot = json.loads(json.dumps(settings_to_data(changed)))  # 저장한 JSON 스냅샷
    assert settings_from_data(snapshot) == changed
    # 새 키가 없는 옛 스냅샷은 바꾼 값이 아니라 기본값으로 읽는다
    for name in BOUNDARY_SETTINGS:
        del snapshot[name]
    old = settings_from_data(snapshot)
    assert tuple(getattr(old, name) for name in BOUNDARY_SETTINGS) == BOUNDARY_DEFAULTS


# ── 거리 순서와 판정 기록 ──────────────────────────────────────────


def test_FR09_distance_order_validates_nodes_and_values():
    order = DistanceOrder(("light-2", "light-1", "light-3"), {"light-2": -58.0, "light-1": -61, "light-3": None})
    assert order.nodes == ("light-2", "light-1", "light-3")
    assert order.values == {"light-2": -58.0, "light-1": -61, "light-3": None}
    with pytest.raises(TypeError):
        order.values["light-2"] = 0.0  # 값은 읽기 전용이다
    assert DistanceOrder((), {}).nodes == ()

    with pytest.raises(ValueError):
        DistanceOrder(("light-1", "light-1"), {"light-1": -60.0})  # 같은 기기가 두 번
    with pytest.raises(ValueError):
        DistanceOrder(("light-1", "light-2"), {"light-1": -60.0})  # 정렬 값이 모자라다
    with pytest.raises(ValueError):
        DistanceOrder(("light-1",), {"light-1": -60.0, "light-2": -70.0})  # 순서에 없는 기기의 정렬 값
    for bad in (True, float("nan"), float("inf"), float("-inf"), "-60"):
        with pytest.raises(ValueError):
            DistanceOrder(("light-1",), {"light-1": bad})


def test_FR16_distance_order_counts_validated():
    nodes = ("light-2", "light-1", "light-3")
    values = {"light-2": -58.0, "light-1": -61.0, "light-3": None}
    order = DistanceOrder(nodes, values, {"light-2": 3, "light-1": 1, "light-3": 0})
    assert order.counts == {"light-2": 3, "light-1": 1, "light-3": 0}
    with pytest.raises(TypeError):
        order.counts["light-2"] = 0  # 값은 읽기 전용이다

    # 기본값은 빈 사전이고, 비어 있으면 기기와 맞추지 않는다 (프레임 수를 계산하지 않은 순서)
    assert DistanceOrder(nodes, values).counts == {}
    assert DistanceOrder(nodes, values, {}).counts == {}
    assert DistanceOrder((), {}, {}).counts == {}


@pytest.mark.parametrize(
    "counts",
    [
        pytest.param({"light-2": 3, "light-1": 1}, id="기기가 모자라다"),
        pytest.param({"light-2": 3, "light-1": 1, "light-3": 0, "light-9": 1}, id="순서에 없는 기기"),
        pytest.param({"light-2": -1, "light-1": 1, "light-3": 0}, id="음수"),
        pytest.param({"light-2": 3.0, "light-1": 1, "light-3": 0}, id="정수가 아닌 수"),
        pytest.param({"light-2": True, "light-1": 1, "light-3": 0}, id="불리언"),
        pytest.param({"light-2": "3", "light-1": 1, "light-3": 0}, id="문자열"),
        pytest.param({"light-2": 3, "light-1": 0, "light-3": 0}, id="정렬 값이 있는데 프레임 수 0"),
        pytest.param({"light-2": 3, "light-1": 1, "light-3": 2}, id="정렬 값이 없는데 프레임 수 1 이상"),
    ],
)
def test_FR16_distance_order_counts_rejects_invalid(counts):
    values = {"light-2": -58.0, "light-1": -61.0, "light-3": None}
    with pytest.raises(ValueError):
        DistanceOrder(("light-2", "light-1", "light-3"), values, counts)


def test_FR22_selector_record_validates_selected_and_children():
    record = SelectorRecord(
        "near:light:1:2", ("light-2", "light-1", "light-3"),
        {"light-2": -58.0, "light-1": -61.0, "light-3": None},
        ("light-2", "light-1"), False, ("child-1",),  # 하위 요청이 고른 기기보다 적어도 된다
    )
    assert record.selected == ("light-2", "light-1")
    assert record.children == ("child-1",)
    with pytest.raises(TypeError):
        record.values["light-2"] = 0.0
    assert SelectorRecord("near:light:1:2", (), {}, (), True).children == ()  # 기본값

    base = ("near:light:1:2", ("light-2", "light-1", "light-3"), {})
    with pytest.raises(ValueError):
        SelectorRecord(*base, ("light-1", "light-2"), False)  # order 의 순서와 다르다
    with pytest.raises(ValueError):
        SelectorRecord(*base, ("light-9",), False)  # order 에 없는 기기
    with pytest.raises(ValueError):
        SelectorRecord(*base, ("light-2", "light-2"), False)  # 같은 기기를 두 번 골랐다
    with pytest.raises(ValueError):
        SelectorRecord(*base, ("light-2",), False, ("child-1", "child-2"))  # 하위 요청이 더 많다


def test_FR22_selector_record_validates_counts_and_boundary_probability():
    values = {"light-2": -58.0, "light-1": -61.0, "light-3": None}
    base = ("near:light:1:2", ("light-2", "light-1", "light-3"), values)
    selected = ("light-2", "light-1")

    # 기본값은 far: 요청과 옛 기록의 모양이다
    plain = SelectorRecord(*base, selected, False)
    assert (plain.counts, plain.boundary_probability) == ({}, None)

    counts = {"light-2": 4, "light-1": 3, "light-3": 0}
    record = SelectorRecord(*base, selected, False, (), counts, 0.87)
    assert (record.counts, record.boundary_probability) == (counts, 0.87)
    with pytest.raises(TypeError):
        record.counts["light-2"] = 0  # 값은 읽기 전용이다
    for edge in (0, 0.0, 1, 1.0):  # [0, 1] 의 양 끝도 확률이다
        assert SelectorRecord(*base, selected, False, (), counts, edge).boundary_probability == edge

    # 프레임 수의 기기는 거리 순서의 기기와 같아야 한다
    with pytest.raises(ValueError):
        SelectorRecord(*base, selected, False, (), {"light-2": 4, "light-1": 3})  # 모자라다
    with pytest.raises(ValueError):
        SelectorRecord(*base, selected, False, (), {**counts, "light-9": 1})  # 순서에 없는 기기
    for bad in (-0.01, 1.01, float("nan"), float("inf"), float("-inf"), True, "0.9"):
        with pytest.raises(ValueError):
            SelectorRecord(*base, selected, False, (), counts, bad)


def test_FR22_decision_record_selector_defaults_to_none():
    record = DecisionRecord(Settings())
    assert record.selector is None
    selector = SelectorRecord("far:light:1:1", ("light-1",), {"light-1": None}, ("light-1",), False)
    assert DecisionRecord(Settings(), selector=selector).selector is selector


def test_FR22_selector_children_carry_record_and_pending_decision():
    record = SelectorRecord("near:light:1:2", ("a", "b"), {"a": -50.0, "b": -60.0}, ("a", "b"), False, ("c1", "c2"))
    decision = Decision("c1", "", Verdict.OK, ReasonCode.OK, "a")
    stored = SelectorChildren(record, (SelectorChild("c1", "a", decision), SelectorChild("c2", "b", None)))
    assert [child.internal_request_id for child in stored.children] == list(record.children)
    assert stored.children[1].decision is None


# ── 응답 값과 IOT·DB 접점 ──────────────────────────────────────────


def test_FR09_decision_selector_fields_default_for_other_targets():
    plain = Decision("ir-1", "r-1", Verdict.OK, ReasonCode.OK)
    assert (plain.devices, plain.selected_count, plain.similar) == ((), None, None)
    # 새 필드는 끝에 있다. 기존 위치 인자 호출이 그대로 같은 필드에 들어간다.
    positional = Decision("ir-1", "r-1", Verdict.OK, ReasonCode.OK, "locker-1", "use-1", 0.97, "안내")
    assert (positional.node_id, positional.use_id, positional.p_max, positional.message) == (
        "locker-1", "use-1", 0.97, "안내")
    assert (positional.devices, positional.selected_count, positional.similar) == ((), None, None)
    assert positional == Decision("ir-1", "r-1", Verdict.OK, ReasonCode.OK, "locker-1", "use-1", 0.97, "안내")

    outcome = DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK)
    assert outcome.use_id is None
    selector = Decision("ir-2", "r-2", Verdict.OK, ReasonCode.OK, devices=(outcome,), selected_count=1, similar=False)
    assert selector.devices == (outcome,)
    assert selector != plain


def test_FR11_selector_device_outcome_has_no_user_or_child_ids():
    names = {f.name for f in fields(DeviceOutcome)}
    assert names == {"node_id", "verdict", "reason", "use_id", "item"}
    assert not names & {"user_id", "occupant", "occupant_user_id", "internal_request_id", "request_id"}


def test_FR09_rssi_report_has_common_report_fields_and_frame_ts():
    names = {f.name for f in fields(RssiReport)}
    assert names == {"node_id", "session_id", "seq", "frame_ts", "sent_at", "received_at"}


def test_FR09_selector_interfaces_add_repository_methods():
    assert list(inspect.signature(Repository.link_selector_child).parameters) == [
        "self", "parent_internal_request_id", "req", "node_id", "input_digest",
    ]
    assert list(inspect.signature(Repository.selector_children).parameters) == [
        "self", "parent_internal_request_id",
    ]


def test_FR09_selector_interfaces_add_sink_method():
    assert list(inspect.signature(NodeEventSink.on_rssi_report).parameters) == ["self", "report"]
    hints = typing.get_type_hints(NodeEventSink.on_rssi_report)
    assert hints == {"report": RssiReport, "return": type(None)}
