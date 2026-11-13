"""묶음 요청의 shared 계약 검사 (FR-09, FR-11, FR-22, #284)."""

import inspect
import json
import tomllib
import typing
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nearpilot.shared.config import Settings, settings_from_data, settings_to_data
from nearpilot.shared.enums import ReasonCode, TargetKind, Verdict
from nearpilot.shared.interfaces import Repository
from nearpilot.shared.models import (
    BATCH_ACTION,
    BatchChildren,
    BatchItemChildren,
    BatchItemRecord,
    BatchRecord,
    Decision,
    DecisionRecord,
    DeviceOutcome,
    SelectorChild,
    SelectorRecord,
    TargetSpec,
    UseItem,
    UseRequest,
)

SERVER = Path(__file__).resolve().parents[2]
RECEIVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _item(target="near:light:1:all", action="on", arguments=None):
    return UseItem(TargetSpec.parse(target), action, arguments)


def _batch(items=None, *, action=BATCH_ACTION, arguments=None, target="batch:"):
    """묶음 표기 대상의 `UseRequest`. `items` 를 생략하면 항목 두 개다."""
    if items is None:
        items = (_item(), _item("speaker-1", "off"))
    return UseRequest("u", "r", TargetSpec.parse(target), action, None, RECEIVED_AT, None, arguments, items)


# ── 대상 지정 ──────────────────────────────────────────────────────


def test_FR09_batch_target_spec_parse_serialize():
    assert TargetKind.BATCH.value == "batch"
    spec = TargetSpec.parse("batch:")
    assert spec == TargetSpec(TargetKind.BATCH, None)
    assert (spec.kind, spec.device_kind, spec.node_id, spec.start, spec.count) == (TargetKind.BATCH, None, None, None, None)
    assert spec.is_batch and not spec.is_selector
    assert spec.serialize() == "batch:"
    assert TargetSpec.parse(spec.serialize()) == spec  # 왕복

    # 콜론 뒤에 글자가 있으면 형식 오류다
    for raw in ("batch:x", "batch:light:1:1", "batch: ", "batch::", "batch:\n", "batch:all"):
        with pytest.raises(ValueError):
            TargetSpec.parse(raw)

    # 콜론이 없는 "batch" 는 지금처럼 기기 ID 가 batch 인 명시 지정이다
    explicit = TargetSpec.parse("batch")
    assert (explicit.kind, explicit.device_kind, explicit.node_id) == (TargetKind.EXPLICIT, None, "batch")
    assert not explicit.is_batch
    assert explicit.serialize() == "batch"

    # 묶음 표기는 종류 이름, 기기 ID, 시작, 개수를 담지 않는다
    with pytest.raises(ValueError):
        TargetSpec(TargetKind.BATCH, "light")
    for extra in ({"node_id": "light-1"}, {"start": 1}, {"count": 1}):
        with pytest.raises(ValueError):
            TargetSpec(TargetKind.BATCH, None, **extra)


@pytest.mark.parametrize(
    ("raw", "kind", "device_kind", "node_id", "start", "count"),
    [
        ("nearest:light", TargetKind.NEAREST, "light", None, None, None),
        ("any:locker", TargetKind.ANY, "locker", None, None, None),
        ("locker-1", TargetKind.EXPLICIT, None, "locker-1", None, None),
        ("near:light:1:all", TargetKind.NEAR, "light", None, 1, None),
        ("far:speaker:2:3", TargetKind.FAR, "speaker", None, 2, 3),
    ],
)
def test_FR09_batch_target_spec_keeps_existing_forms(raw, kind, device_kind, node_id, start, count):
    spec = TargetSpec.parse(raw)
    assert (spec.kind, spec.device_kind, spec.node_id, spec.start, spec.count) == (
        kind, device_kind, node_id, start, count)
    assert not spec.is_batch
    assert spec.serialize() == raw


def test_FR09_batch_target_spec_keeps_existing_validation_messages():
    # 묶음 표기를 더해도 기존 지정의 검사와 메시지는 그대로다
    with pytest.raises(ValueError, match="명시 지정만 기기 ID 를 담고 종류 이름을 담지 않는다"):
        TargetSpec(TargetKind.NEAREST, None)
    with pytest.raises(ValueError, match="명시 지정만 기기 ID 를 담고 종류 이름을 담지 않는다"):
        TargetSpec(TargetKind.EXPLICIT, "light", "light-1")
    with pytest.raises(ValueError, match="거리 선택자만 시작과 개수를 담는다"):
        TargetSpec(TargetKind.NEAREST, "light", start=1)
    with pytest.raises(ValueError, match="거리 선택자의 시작은 1 이상의 정수여야 한다"):
        TargetSpec(TargetKind.NEAR, "light", start=0, count=1)
    with pytest.raises(ValueError, match="target_spec 형식 오류"):
        TargetSpec.parse("explicit:light-1")


# ── 요청 ───────────────────────────────────────────────────────────


def test_FR09_batch_use_request_keeps_existing_fields_and_adds_items_last():
    names = [f.name for f in fields(UseRequest)]
    assert names == [
        "user_id", "request_id", "target", "action", "host_kind", "received_at",
        "parent_internal_request_id", "arguments", "items",
    ]
    # 기존 위치 인자 호출이 그대로 같은 필드에 들어가고 items 는 비어 있다
    plain = UseRequest("u", "r", TargetSpec.parse("nearest:light"), "on", "claude", RECEIVED_AT, "p-1", {"brightness": 1})
    assert (plain.parent_internal_request_id, plain.arguments, plain.items) == ("p-1", {"brightness": 1}, ())
    assert UseRequest("u", "r", TargetSpec.parse("near:light:1:all"), "on", None, RECEIVED_AT).items == ()


def test_FR09_batch_use_item_fields():
    assert [f.name for f in fields(UseItem)] == ["target", "action", "arguments"]
    assert _item("light-1", "off").arguments is None
    item = _item("near:light:2:all", "on", {"brightness": 10})
    assert (item.target.serialize(), item.action, item.arguments) == ("near:light:2:all", "on", {"brightness": 10})


_NOT_OBJECT = [[1, 2], "brightness", 3, {1: "x"}]  # 인자는 문자열 키의 객체여야 한다
_BATCH_CASES = [
    pytest.param(lambda: _batch(), True, id="항목이 있는 묶음 요청"),
    pytest.param(lambda: _batch([_item()]), True, id="항목 하나짜리 묶음 요청"),
    pytest.param(lambda: _batch([_item("near:light:1:all"), _item("any:locker", "open"), _item("locker-2", "open"),
                                 _item("nearest:speaker", "on")]), True, id="네 방식의 대상"),
    pytest.param(lambda: _batch([_item()] * 9 + [_item("speaker-1", "off")] * 91), True,
                 id="항목 수 상한은 모델이 검사하지 않는다"),
    pytest.param(lambda: _batch([]), False, id="항목이 비었다"),
    pytest.param(lambda: _batch(()), False, id="항목이 빈 튜플이다"),
    pytest.param(lambda: _batch(action="on"), False, id="action 이 batch 가 아니다"),
    pytest.param(lambda: _batch(action="Batch"), False, id="action 이 이름 규칙을 어긴다"),
    pytest.param(lambda: _batch(arguments={"brightness": 1}), False, id="묶음 요청이 arguments 를 담는다"),
    pytest.param(lambda: _batch(arguments={}), False, id="묶음 요청이 빈 arguments 를 담는다"),
    pytest.param(lambda: _batch(["near:light:1:all"]), False, id="항목이 UseItem 이 아니다(문자열)"),
    pytest.param(lambda: _batch([{"target_spec": "light-1", "action": "on"}]), False, id="항목이 UseItem 이 아니다(객체)"),
    pytest.param(lambda: _batch([_item(), None]), False, id="항목에 None 이 있다"),
    pytest.param(lambda: _batch(5), False, id="items 가 목록이 아니다"),
    pytest.param(lambda: _batch({"a": _item()}), False, id="items 가 객체다"),
    pytest.param(lambda: _batch(target="nearest:light"), False, id="근접 지정인데 items 가 있다"),
    pytest.param(lambda: _batch(target="locker-1", action="open"), False, id="명시 지정인데 items 가 있다"),
    pytest.param(lambda: _batch(target="near:light:1:all"), False, id="거리 선택자인데 items 가 있다"),
    pytest.param(lambda: _batch(target="any:locker", action="open"), False, id="전역 할당인데 items 가 있다"),
    pytest.param(lambda: _item("batch:"), False, id="항목의 대상이 묶음 표기다"),
    pytest.param(lambda: _item(action="Open"), False, id="항목의 동작이 이름 규칙을 어긴다"),
    pytest.param(lambda: _item(action=""), False, id="항목의 동작이 비었다"),
    pytest.param(lambda: _item(action=None), False, id="항목의 동작이 문자열이 아니다"),
    *(pytest.param(lambda bad=bad: _item(arguments=bad), False, id=f"항목의 arguments 가 객체가 아니다 {bad!r}")
      for bad in _NOT_OBJECT),
]


@pytest.mark.parametrize(("build", "allowed"), _BATCH_CASES)
def test_FR09_batch_use_request_validation(build, allowed):
    if not allowed:
        with pytest.raises(ValueError):
            build()
        return
    built = build()
    if isinstance(built, UseRequest):
        assert built.target.is_batch and built.action == BATCH_ACTION and built.arguments is None
        assert built.items and all(isinstance(item, UseItem) for item in built.items)


def test_FR09_batch_use_request_freezes_items_and_item_arguments():
    arguments = {"brightness": 10, "colors": ["#FFFFFF", "#000000"]}
    items = [_item("near:light:1:all", "on", arguments), _item("speaker-1", "off")]
    request = _batch(items)
    assert request.items == tuple(items) and isinstance(request.items, tuple)  # 목록은 튜플로 고정한다

    frozen = request.items[0].arguments
    assert frozen == {"brightness": 10, "colors": ("#FFFFFF", "#000000")}  # 안쪽 컨테이너도 불변이다
    with pytest.raises(TypeError):
        frozen["brightness"] = 99
    arguments["brightness"] = 99  # 원본을 바꿔도 항목은 그대로다
    assert request.items[0].arguments["brightness"] == 10
    with pytest.raises(FrozenInstanceError):
        request.items = ()


# ── 설정 ───────────────────────────────────────────────────────────


def test_FR09_settings_batch_max_items_validated():
    assert Settings().batch_max_items == 8
    assert Settings(batch_max_items=1).batch_max_items == 1  # 하한은 허용한다
    assert Settings(batch_max_items=100).batch_max_items == 100
    for bad in (0, -1, 1.5, 2.0, True, False, float("nan"), float("inf"), "8", None):
        with pytest.raises(ValueError):
            Settings(batch_max_items=bad)

    changed = Settings(batch_max_items=3)
    snapshot = json.loads(json.dumps(settings_to_data(changed)))  # 저장한 JSON 스냅샷
    assert snapshot["batch_max_items"] == 3
    assert settings_from_data(snapshot) == changed

    # 키가 없는 옛 스냅샷은 바꾼 값이 아니라 기본값으로 읽는다
    del snapshot["batch_max_items"]
    assert settings_from_data(snapshot).batch_max_items == 8


def test_FR09_example_config_batch_max_items_is_settings_default():
    example = tomllib.loads((SERVER / "config" / "server.example.toml").read_text(encoding="utf-8"))["settings"]
    assert example["batch_max_items"] == Settings().batch_max_items == 8
    # 예시 [settings] 의 키는 Settings 필드여야 한다 (tools/evaluation 이 Settings(**표) 로 읽는다)
    assert set(example) <= {f.name for f in fields(Settings)}
    assert Settings(**example).batch_max_items == 8


# ── 판정 기록 ──────────────────────────────────────────────────────


def _selector(children=()):
    return SelectorRecord(
        "near:light:1:2", ("light-2", "light-1"), {"light-2": -58.0, "light-1": -61.0},
        ("light-2", "light-1"), False, children,
    )


def test_FR22_batch_item_record_children_match_selector_children():
    selector = _selector(("c1", "c2"))
    item = BatchItemRecord("near:light:1:2", "on", {"brightness": 5, "colors": ["#FFFFFF"]}, ("c1", "c2"), selector)
    assert item.selector is selector and item.children == ("c1", "c2")
    assert item.arguments == {"brightness": 5, "colors": ("#FFFFFF",)}
    with pytest.raises(TypeError):
        item.arguments["brightness"] = 1  # 인자는 읽기 전용이다

    # 하위 요청을 만들기 전에 멈춘 거리 선택자 항목은 둘 다 비어 있다
    assert BatchItemRecord("near:light:1:2", "on", None, (), _selector()).children == ()
    # 거리 선택자가 아닌 항목은 하위 요청이 하나이고 거리 선택자 기록이 없다
    plain = BatchItemRecord("light-1", "off", None, ("c3",))
    assert (plain.arguments, plain.children, plain.selector) == (None, ("c3",), None)
    default = BatchItemRecord("light-1", "off", None)
    assert (default.children, default.selector) == ((), None)

    # 거리 선택자 기록의 하위 요청이 항목의 하위 요청과 다르면 거부한다
    for children in ((), ("c1",), ("c2", "c1"), ("c1", "c2", "c3")):
        with pytest.raises(ValueError):
            BatchItemRecord("near:light:1:2", "on", None, children, selector)


def test_FR22_batch_record_holds_items_and_decision_record_defaults_to_none():
    first = BatchItemRecord("light-1", "on", None, ("c1",))
    second = BatchItemRecord("speaker-1", "off", None, ("c2",))
    record = BatchRecord([first, second])  # 목록은 튜플로 고정한다
    assert record.items == (first, second) and isinstance(record.items, tuple)
    assert BatchRecord(()).items == ()

    assert DecisionRecord(Settings()).batch is None
    assert DecisionRecord(Settings(), batch=record).batch is record
    # 거리 선택자 기록과는 따로 있다
    selector = SelectorRecord("far:light:1:1", ("light-1",), {"light-1": None}, ("light-1",), False)
    both = DecisionRecord(Settings(), selector=selector, batch=record)
    assert (both.selector, both.batch) == (selector, record)


def test_FR22_batch_children_carry_record_and_stored_children():
    first = BatchItemRecord("light-1", "on", None, ("c1",))
    second = BatchItemRecord("speaker-1", "off", None, ("c2",))
    record = BatchRecord((first, second))
    decision = Decision("c1", "", Verdict.OK, ReasonCode.OK, "light-1")
    stored = BatchChildren(record, (
        BatchItemChildren(first, (SelectorChild("c1", "light-1", decision),)),
        BatchItemChildren(second, (SelectorChild("c2", "speaker-1", None),)),
    ))
    assert [item.record for item in stored.items] == list(record.items)
    for item in stored.items:
        assert [child.internal_request_id for child in item.children] == list(item.record.children)
    assert stored.items[1].children[0].decision is None  # 판정을 아직 저장하지 않은 하위 요청


# ── 응답 값과 DB 접점 ──────────────────────────────────────────────


def test_FR09_batch_device_outcome_item_defaults_to_none():
    plain = DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK)
    assert (plain.use_id, plain.item) == (None, None)
    # 기존 위치 인자 호출이 그대로 같은 필드에 들어간다
    positional = DeviceOutcome("locker-1", Verdict.OK, ReasonCode.OK, "use-1")
    assert (positional.use_id, positional.item) == ("use-1", None)
    assert DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK, None, 2).item == 2
    assert DeviceOutcome("light-1", Verdict.OK, ReasonCode.OK, item=1) != plain


def test_FR11_batch_device_outcome_has_no_user_or_child_ids():
    names = {f.name for f in fields(DeviceOutcome)}
    assert names == {"node_id", "verdict", "reason", "use_id", "item"}
    assert not names & {"user_id", "occupant", "occupant_user_id", "internal_request_id", "request_id"}


def test_FR09_batch_interfaces_change_repository_methods():
    assert list(inspect.signature(Repository.link_selector_child).parameters) == [
        "self", "parent_internal_request_id", "req", "node_id", "input_digest",
    ]
    assert typing.get_type_hints(Repository.link_selector_child)["node_id"] == (str | None)
    assert list(inspect.signature(Repository.batch_children).parameters) == ["self", "parent_internal_request_id"]
    assert typing.get_type_hints(Repository.batch_children)["return"] == (BatchChildren | None)
    # 거리 선택자의 조회는 그대로다
    assert list(inspect.signature(Repository.selector_children).parameters) == ["self", "parent_internal_request_id"]
