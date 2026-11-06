"""FR-04 기기 선언 검증·해시·템플릿과 FR-06 인자 검증 (specs/iot-device-descriptor/spec.md)."""

from __future__ import annotations

import copy

import pytest

from nearpilot.shared.descriptor import (
    ArgumentProblem, DescriptorError, descriptor_hash, legacy_controls, legacy_descriptor,
    parse_descriptor, validate_arguments,
)
from nearpilot.shared.enums import ActionRole, Capability, Occupancy


def light() -> dict:
    """device-protocol.md 3절의 예시 선언."""
    return {
        "format": "nearpilot.device/1",
        "kind": "light",
        "occupancy": "shared",
        "vendor": {"name": "acme", "model": "ring-light-a", "firmware": "1.4.0"},
        "actions": [
            {
                "name": "on",
                "description": "조명을 켜고 밝기와 색을 정한다",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "brightness": {"type": "integer", "minimum": 0, "maximum": 64},
                        "color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "result": {"kind": "signal_output"},
            },
            {
                "name": "off",
                "description": "조명을 끈다",
                "role": "deactivate",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "result": {"kind": "signal_output"},
            },
        ],
    }


def locker() -> dict:
    data = light()
    data.update(kind="locker", occupancy="rental")
    data["actions"] = [
        {"name": "open", "description": "연다", "input_schema": {"type": "object"},
         "result": {"kind": "signal_output"}},
        {"name": "close", "description": "잠근다", "role": "reoperate", "input_schema": {"type": "object"},
         "result": {"kind": "signal_output"}},
    ]
    return data


def with_change(base: dict, path: tuple, value) -> dict:
    data = copy.deepcopy(base)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def test_FR04_descriptor_accepts_protocol_example():
    descriptor = parse_descriptor(light())
    assert (descriptor.format, descriptor.kind, descriptor.occupancy) == (1, "light", Occupancy.SHARED)
    assert [a.name for a in descriptor.actions] == ["on", "off"]
    assert descriptor.action("off").role is ActionRole.DEACTIVATE
    assert descriptor.to_json() == light()
    assert parse_descriptor(locker()).action("close").role is ActionRole.REOPERATE


@pytest.mark.parametrize("path,value", [
    (("extra",), 1),
    (("vendor", "serial"), "x"),
    (("actions", 0, "icon"), "bulb"),
    (("actions", 0, "input_schema", "format"), "x"),
    (("actions", 0, "input_schema", "properties", "brightness", "multipleOf"), 2),
    (("actions", 0, "result", "detail"), "x"),
])
def test_FR04_descriptor_rejects_unknown_keywords(path, value):
    with pytest.raises(DescriptorError):
        parse_descriptor(with_change(light(), path, value))


@pytest.mark.parametrize("path,value", [
    (("occupancy",), "exclusive"),
    (("actions", 1, "role"), "toggle"),
    (("actions", 0, "result", "kind"), "physical_state"),
])
def test_FR04_descriptor_rejects_unknown_occupancy_and_role(path, value):
    with pytest.raises(DescriptorError):
        parse_descriptor(with_change(light(), path, value))


@pytest.mark.parametrize("value", [
    "nearpilot.device/2", "nearpilot.device/0", "nearpilot.device/01", "nearpilot.device/1.1",
    "nearpilot.device/", "device/1", "", 1, None,
])
def test_FR04_descriptor_rejects_unsupported_format(value):
    with pytest.raises(DescriptorError):
        parse_descriptor(with_change(light(), ("format",), value))


def _actions(count: int) -> list[dict]:
    plain = [{"name": f"act_{i}", "description": "동작", "input_schema": {"type": "object"},
              "result": {"kind": "signal_output"}} for i in range(count - 1)]
    return plain + [light()["actions"][1]]


def _arguments(count: int) -> dict:
    return {f"arg_{i}": {"type": "integer"} for i in range(count)}


def test_FR04_descriptor_size_limits():
    def ok(data):
        parse_descriptor(data)

    def bad(data):
        with pytest.raises(DescriptorError):
            parse_descriptor(data)

    ok(with_change(light(), ("actions",), _actions(8)))
    bad(with_change(light(), ("actions",), _actions(9)))
    ok(with_change(light(), ("kind",), "k" * 32))
    bad(with_change(light(), ("kind",), "k" * 33))
    ok(with_change(light(), ("actions", 0, "name"), "a" * 32))
    bad(with_change(light(), ("actions", 0, "name"), "a" * 33))
    ok(with_change(light(), ("actions", 0, "description"), "가" * 40))  # 120바이트
    bad(with_change(light(), ("actions", 0, "description"), "가" * 40 + "a"))
    ok(with_change(light(), ("actions", 0, "input_schema", "properties"), _arguments(8)))
    bad(with_change(light(), ("actions", 0, "input_schema", "properties"), _arguments(9)))
    enum = {"type": "integer", "enum": list(range(16))}
    ok(with_change(light(), ("actions", 0, "input_schema", "properties", "brightness"), enum))
    enum = {"type": "integer", "enum": list(range(17))}
    bad(with_change(light(), ("actions", 0, "input_schema", "properties", "brightness"), enum))
    pattern = {"type": "string", "pattern": "a" * 64}
    ok(with_change(light(), ("actions", 0, "input_schema", "properties", "color"), pattern))
    pattern = {"type": "string", "pattern": "a" * 65}
    bad(with_change(light(), ("actions", 0, "input_schema", "properties", "color"), pattern))
    bad(with_change(light(), ("actions",), []))


@pytest.mark.parametrize("path,value", [
    (("kind",), "Light"), (("kind",), "1light"), (("kind",), "light:ring"), (("kind",), "light-2"),
    (("kind",), ""), (("actions", 0, "name"), "On"), (("actions", 0, "name"), "turn on"),
    (("actions", 1, "name"), "on"),  # 중복 이름
])
def test_FR04_descriptor_rejects_bad_names(path, value):
    with pytest.raises(DescriptorError):
        parse_descriptor(with_change(light(), path, value))


@pytest.mark.parametrize("path,value", [
    (("actions", 0, "input_schema", "properties", "brightness"), {"type": "object"}),
    (("actions", 0, "input_schema", "properties", "brightness"), {"type": "array"}),
    (("actions", 0, "input_schema", "properties", "brightness"),
     {"type": "integer", "properties": {}}),
    (("actions", 0, "input_schema", "type"), "array"),
    (("actions", 0, "input_schema", "minimum"), 0),
    (("actions", 0, "input_schema", "required"), ["missing"]),
    (("actions", 0, "input_schema", "additionalProperties"), {"type": "string"}),
    (("actions", 0, "input_schema", "properties", "brightness"), {"type": "integer", "pattern": "x"}),
    (("actions", 0, "input_schema", "properties", "color"), {"type": "string", "minimum": 1}),
    (("actions", 0, "input_schema", "properties", "color"), {"type": "string", "pattern": "("}),
    (("actions", 0, "input_schema", "properties", "brightness"),
     {"type": "integer", "minimum": 5, "maximum": 1}),
    (("actions", 0, "input_schema", "properties", "brightness"), {"type": "integer", "enum": ["a"]}),
])
def test_FR04_descriptor_rejects_nested_schema(path, value):
    with pytest.raises(DescriptorError):
        parse_descriptor(with_change(light(), path, value))


def test_FR04_shared_requires_one_plain_deactivate():
    without = light()
    del without["actions"][1]["role"]
    two = with_change(light(), ("actions", 0, "role"), "deactivate")
    with_arguments = with_change(light(), ("actions", 1, "input_schema", "properties"),
                                 {"level": {"type": "integer"}})
    open_schema = with_change(light(), ("actions", 1, "input_schema"), {"type": "object"})
    for data in (without, two, with_arguments, open_schema):
        with pytest.raises(DescriptorError):
            parse_descriptor(data)


def test_FR04_role_occupancy_mismatch_rejected():
    rental_deactivate = with_change(light(), ("occupancy",), "rental")
    shared_reoperate = copy.deepcopy(light())
    shared_reoperate["actions"].append({"name": "close", "description": "닫는다", "role": "reoperate",
                                        "input_schema": {"type": "object"}, "result": {"kind": "signal_output"}})
    for data in (rental_deactivate, shared_reoperate):
        with pytest.raises(DescriptorError):
            parse_descriptor(data)


def test_FR04_descriptor_hash_rules():
    """해시는 kind·occupancy·actions 전체다. vendor 와 format 은 넣지 않는다 (spec 결정 2)."""
    base = parse_descriptor(light())
    assert base.hash == descriptor_hash(base) and len(base.hash) == 64
    reordered = copy.deepcopy(light())
    reordered["actions"][0]["input_schema"] = dict(reversed(list(reordered["actions"][0]["input_schema"].items())))
    assert parse_descriptor(reordered).hash == base.hash  # 키 순서는 해시를 바꾸지 않는다
    assert parse_descriptor(with_change(light(), ("vendor", "firmware"), "2.0.0")).hash == base.hash
    for path, value in [(("actions", 0, "description"), "조명을 켠다"), (("kind",), "lamp"),
                        (("actions", 0, "input_schema", "properties", "brightness", "maximum"), 65)]:
        assert parse_descriptor(with_change(light(), path, value)).hash != base.hash
    swapped = copy.deepcopy(light())
    swapped["actions"].reverse()
    assert parse_descriptor(swapped).hash != base.hash  # 동작 순서는 선언의 일부다


def test_FR04_v2_templates():
    """v2 템플릿은 spec 결정 3의 선언이다. 제어 정보가 다르면 해시도 다르다."""
    ring = legacy_descriptor(Capability.LIGHT, Occupancy.SHARED, brightness_max=64, color=True)
    assert ring.action("on").description == "조명을 켜고 밝기와 색을 정한다"
    assert ring.action("on").properties["brightness"]["maximum"] == 64
    assert ring.action("on").properties["color"]["pattern"] == "^#[0-9A-Fa-f]{6}$"
    assert ring.action("off").role is ActionRole.DEACTIVATE
    assert ring.vendor.to_json() == {"name": "nearpilot", "model": "light", "firmware": "v2"}
    plain = legacy_descriptor(Capability.LIGHT, Occupancy.SHARED)
    assert plain.action("on").properties == {} and plain.hash != ring.hash
    box = legacy_descriptor(Capability.LOCKER, Occupancy.RENTAL)
    assert [(a.name, a.role) for a in box.actions] == [("open", None), ("close", ActionRole.REOPERATE)]
    speaker = legacy_descriptor(Capability.SPEAKER, Occupancy.SHARED, volume_max=20)
    assert speaker.action("on").properties["volume"] == {"type": "integer", "minimum": 0, "maximum": 20}
    for capability, occupancy in [(Capability.LOCKER, Occupancy.SHARED), (Capability.LIGHT, Occupancy.RENTAL)]:
        with pytest.raises(DescriptorError):
            legacy_descriptor(capability, occupancy)


def test_FR04_legacy_controls_from_descriptor():
    """PR ② 전까지 판정이 쓰는 조명·스피커 제어 정보 (spec 결정 7)."""
    assert legacy_controls(None) == (None, None)
    assert legacy_controls(parse_descriptor(light())) == ((64, True), None)
    assert legacy_controls(legacy_descriptor(Capability.LIGHT, Occupancy.SHARED)) == (None, None)
    assert legacy_controls(legacy_descriptor(Capability.SPEAKER, Occupancy.SHARED, volume_max=30)) == (None, 30)
    huge = with_change(light(), ("actions", 0, "input_schema", "properties", "brightness", "maximum"), 1000)
    assert legacy_controls(parse_descriptor(huge)) == (None, None)


def test_FR06_validate_arguments():
    on = parse_descriptor(light()).action("on")
    assert validate_arguments(on, None) is None
    assert validate_arguments(on, {"brightness": 64, "color": "#00ff00"}) is None
    assert validate_arguments(on, {"brightness": 65}) is ArgumentProblem.OUT_OF_RANGE
    assert validate_arguments(on, {"brightness": -1}) is ArgumentProblem.OUT_OF_RANGE
    assert validate_arguments(on, {"brightness": 30}, caps={"brightness": 20}) is ArgumentProblem.OUT_OF_RANGE
    assert validate_arguments(on, {"brightness": 20}, caps={"brightness": 20}) is None
    for bad in ({"brightness": 1.0}, {"brightness": True}, {"brightness": "1"}, {"color": "blue"},
                {"color": 1}, {"volume": 3}, [1], "x"):
        assert validate_arguments(on, bad) is ArgumentProblem.INVALID, bad
    # 형식 오류와 범위 오류가 함께 있으면 INVALID 다 (spec 결정 5).
    assert validate_arguments(on, {"brightness": 65, "color": "blue"}) is ArgumentProblem.INVALID


def test_FR06_validate_arguments_schema_keywords():
    data = with_change(light(), ("actions", 0, "input_schema"), {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["warm", "cold"]},
            "label": {"type": "string", "maxLength": 3},
            "ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "blink": {"type": "boolean"},
        },
        "required": ["mode"],
    })
    on = parse_descriptor(data).action("on")
    assert validate_arguments(on, {"mode": "warm", "label": "abc", "ratio": 0.5, "blink": False}) is None
    assert validate_arguments(on, {"label": "abc"}) is ArgumentProblem.INVALID  # required
    assert validate_arguments(on, {"label": "abc"}, partial=True) is None
    assert validate_arguments(on, {"mode": "hot"}) is ArgumentProblem.INVALID
    assert validate_arguments(on, {"mode": "warm", "label": "abcd"}) is ArgumentProblem.INVALID
    assert validate_arguments(on, {"mode": "warm", "ratio": 1.5}) is ArgumentProblem.OUT_OF_RANGE
    # additionalProperties 가 없으면 선언 밖 평탄한 인자도 받는다 (spec 결정 4).
    assert validate_arguments(on, {"mode": "warm", "extra": 1}) is None
    assert validate_arguments(on, {"mode": "warm", "extra": {"x": 1}}) is ArgumentProblem.INVALID
