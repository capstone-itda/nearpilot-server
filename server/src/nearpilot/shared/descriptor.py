"""기기 선언 `nearpilot.device/1` (#245, docs/device-protocol.md).

선언 검증, 인자 검증, 선언 해시, v2 템플릿을 순수 함수로 둔다. IOT, DB, 판정이 같은 규칙을 쓴다
(specs/iot-device-descriptor/spec.md 결정 1). 이 모듈은 다른 shared 모듈의 모델을 불러오지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, NoReturn

from nearpilot.shared.enums import ActionRole, Capability, Occupancy

__all__ = [
    "ANNOUNCE_MAX_BYTES", "CURRENT_FORMAT", "SUPPORTED_FORMATS", "ActionSpec", "ArgumentProblem",
    "DescriptorError", "DeviceDescriptor", "VendorInfo", "descriptor_hash", "legacy_controls",
    "legacy_descriptor", "parse_descriptor", "validate_arguments",
]

# ── 상한과 이름 규칙 (device-protocol.md 4절, 6절) ─────────────────────

FORMAT_PREFIX = "nearpilot.device/"
CURRENT_FORMAT = 1
SUPPORTED_FORMATS = frozenset({CURRENT_FORMAT})  # 현재 버전과 바로 앞 버전. 앞 버전은 아직 없다.
ANNOUNCE_MAX_BYTES = 2048
MAX_ACTIONS = 8
MAX_NAME_BYTES = 32
MAX_DESCRIPTION_BYTES = 120
MAX_ARGUMENTS = 8
MAX_ENUM_ITEMS = 16
MAX_PATTERN_BYTES = 64

SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "required", "minimum", "maximum", "enum", "pattern", "maxLength",
    "additionalProperties",
})
_OBJECT_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties"})
_ARGUMENT_TYPES = frozenset({"integer", "number", "string", "boolean"})
_NUMERIC_TYPES = frozenset({"integer", "number"})
_RESULT_KINDS = frozenset({"signal_output"})
_NAME = re.compile(r"[a-z][a-z0-9_]*")


class DescriptorError(ValueError):
    """선언이 공개 규칙을 따르지 않는다. 메시지에는 선언 내용을 넣지 않는다."""


def _fail(reason: str) -> NoReturn:
    raise DescriptorError(reason)


# ── 불변 JSON ───────────────────────────────────────────────────────


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ── 모델 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VendorInfo:
    """제작사 정보. 신뢰 판단과 감사에 쓰고 판정에는 쓰지 않는다 (NFR-07)."""

    name: str
    model: str
    firmware: str

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "model": self.model, "firmware": self.firmware}


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """동작 선언 하나. `input_schema`는 검증한 JSON Schema 부분집합이다."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    role: ActionRole | None = None
    result_kind: str = "signal_output"

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))

    @property
    def properties(self) -> Mapping[str, Mapping[str, Any]]:
        return self.input_schema.get("properties", MappingProxyType({}))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.role is not None:
            out["role"] = self.role.value
        out["input_schema"] = _thaw(self.input_schema)
        out["result"] = {"kind": self.result_kind}
        return out


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    """기기 선언. `parse_descriptor`가 만든 값만 공개 규칙을 만족한다."""

    format: int
    kind: str
    occupancy: Occupancy
    vendor: VendorInfo
    actions: tuple[ActionSpec, ...]

    def action(self, name: str) -> ActionSpec | None:
        return next((spec for spec in self.actions if spec.name == name), None)

    def to_json(self) -> dict[str, Any]:
        """`announce.descriptor`와 같은 모양. DB 저장과 감사에 쓴다."""
        return {
            "format": f"{FORMAT_PREFIX}{self.format}",
            "kind": self.kind,
            "occupancy": self.occupancy.value,
            "vendor": self.vendor.to_json(),
            "actions": [spec.to_json() for spec in self.actions],
        }

    @property
    def hash(self) -> str:
        return descriptor_hash(self)


def descriptor_hash(descriptor: DeviceDescriptor) -> str:
    """`kind`, `occupancy`, `actions` 전체의 정규 JSON SHA-256 hex (FR-04, spec 결정 2).

    `vendor`와 `format`은 넣지 않는다. 동작 순서는 선언 순서를 지킨다.
    """
    body = {
        "kind": descriptor.kind,
        "occupancy": descriptor.occupancy.value,
        "actions": [spec.to_json() for spec in descriptor.actions],
    }
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


# ── 선언 검증 (device-protocol.md 3·4·5절) ─────────────────────────


def _object(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _fail(f"{where} must be an object")
    return value


def _keys(value: Mapping[str, Any], where: str, required: frozenset[str] | set[str],
          optional: frozenset[str] | set[str] = frozenset()) -> None:
    keys = set(value)
    if keys - set(required) - set(optional):
        _fail(f"unknown keyword in {where}")
    if set(required) - keys:
        _fail(f"missing field in {where}")


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{where} must be a non-empty string")
    return value


def _name(value: object, where: str) -> str:
    value = _text(value, where)
    if len(value.encode("utf-8")) > MAX_NAME_BYTES or not _NAME.fullmatch(value):
        _fail(f"{where} breaks the name rule")
    return value


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _matches_type(kind: str, value: object) -> bool:
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return _number(value)
    if kind == "string":
        return isinstance(value, str)
    return type(value) is bool


def _format(value: object) -> int:
    value = _text(value, "format")
    if not value.startswith(FORMAT_PREFIX):
        _fail("format must be nearpilot.device/{N}")
    digits = value[len(FORMAT_PREFIX):]
    if not digits.isascii() or not digits.isdigit() or (len(digits) > 1 and digits[0] == "0"):
        _fail("format must be nearpilot.device/{N}")
    version = int(digits)
    if version not in SUPPORTED_FORMATS:
        _fail("unsupported descriptor format")
    return version


def _argument_schema(value: object, where: str) -> Mapping[str, Any]:
    prop = _object(value, where)
    if set(prop) - SCHEMA_KEYWORDS:
        _fail(f"unknown keyword in {where}")
    if set(prop) & {"properties", "required", "additionalProperties"}:
        _fail(f"nested schema in {where}")
    kind = prop.get("type")
    if kind in ("object", "array"):
        _fail(f"nested schema in {where}")
    if kind not in _ARGUMENT_TYPES:
        _fail(f"{where}.type must be integer, number, string or boolean")
    for bound in ("minimum", "maximum"):
        if bound in prop and (kind not in _NUMERIC_TYPES or not _number(prop[bound])):
            _fail(f"{where}.{bound} is invalid")
    if "minimum" in prop and "maximum" in prop and prop["minimum"] > prop["maximum"]:
        _fail(f"{where} minimum exceeds maximum")
    if "pattern" in prop:
        pattern = prop["pattern"]
        if kind != "string" or not isinstance(pattern, str):
            _fail(f"{where}.pattern is invalid")
        if len(pattern.encode("utf-8")) > MAX_PATTERN_BYTES:
            _fail(f"{where}.pattern exceeds {MAX_PATTERN_BYTES} bytes")
        try:
            re.compile(pattern)
        except re.error:
            _fail(f"{where}.pattern does not compile")
    if "maxLength" in prop:
        limit = prop["maxLength"]
        if kind != "string" or type(limit) is not int or limit < 0:
            _fail(f"{where}.maxLength is invalid")
    if "enum" in prop:
        items = prop["enum"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ENUM_ITEMS:
            _fail(f"{where}.enum must list 1 to {MAX_ENUM_ITEMS} items")
        if not all(_matches_type(kind, item) for item in items):
            _fail(f"{where}.enum item type mismatch")
        if len({_canonical(item) for item in items}) != len(items):
            _fail(f"{where}.enum has duplicates")
    return prop


def _input_schema(value: object, where: str) -> Mapping[str, Any]:
    schema = _object(value, where)
    if set(schema) - SCHEMA_KEYWORDS:
        _fail(f"unknown keyword in {where}")
    if set(schema) - _OBJECT_KEYWORDS:
        _fail(f"{where} allows only object keywords at the top level")
    if schema.get("type") != "object":
        _fail(f"{where}.type must be object")
    properties = _object(schema.get("properties", {}), f"{where}.properties")
    if len(properties) > MAX_ARGUMENTS:
        _fail(f"{where} declares more than {MAX_ARGUMENTS} arguments")
    for arg, prop in properties.items():
        if not arg:
            _fail(f"{where} has an empty argument name")
        _argument_schema(prop, f"{where}.properties")
    required = schema.get("required", [])
    if (not isinstance(required, list) or not all(isinstance(item, str) for item in required)
            or len(set(required)) != len(required) or set(required) - set(properties)):
        _fail(f"{where}.required is invalid")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        _fail(f"{where}.additionalProperties must be a boolean")
    return schema


def _action(value: object, index: int) -> ActionSpec:
    where = f"actions[{index}]"
    action = _object(value, where)
    _keys(action, where, {"name", "description", "input_schema", "result"}, {"role"})
    name = _name(action["name"], f"{where}.name")
    description = _text(action["description"], f"{where}.description")
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        _fail(f"{where}.description exceeds {MAX_DESCRIPTION_BYTES} bytes")
    role = None
    if "role" in action:
        raw = _text(action["role"], f"{where}.role")
        try:
            role = ActionRole(raw)
        except ValueError:
            _fail(f"{where}.role is unknown")
    schema = _input_schema(action["input_schema"], f"{where}.input_schema")
    result = _object(action["result"], f"{where}.result")
    _keys(result, f"{where}.result", {"kind"})
    if result["kind"] not in _RESULT_KINDS:
        _fail(f"{where}.result.kind is unknown")
    return ActionSpec(name=name, description=description, input_schema=schema, role=role,
                      result_kind=result["kind"])


def _check_roles(occupancy: Occupancy, actions: tuple[ActionSpec, ...]) -> None:
    deactivate = [spec for spec in actions if spec.role is ActionRole.DEACTIVATE]
    if occupancy is Occupancy.RENTAL and deactivate:
        _fail("rental device cannot declare deactivate")
    if occupancy is Occupancy.SHARED:
        if len(deactivate) != 1:
            _fail("shared device must declare exactly one deactivate")
        schema = deactivate[0].input_schema
        if schema.get("properties") or schema.get("additionalProperties", True) is not False:
            _fail("deactivate action must take no arguments")
        if any(spec.role is ActionRole.REOPERATE for spec in actions):
            _fail("only rental devices can declare reoperate")


def parse_descriptor(value: object) -> DeviceDescriptor:
    """공개 규칙의 형식·키워드·상한·이름·점유 방식·역할을 검증한다. 틀리면 DescriptorError."""
    data = _object(value, "descriptor")
    _keys(data, "descriptor", {"format", "kind", "occupancy", "vendor", "actions"})
    version = _format(data["format"])
    kind = _name(data["kind"], "kind")
    raw_occupancy = _text(data["occupancy"], "occupancy")
    try:
        occupancy = Occupancy(raw_occupancy)
    except ValueError:
        _fail("occupancy is unknown")
    vendor_data = _object(data["vendor"], "vendor")
    _keys(vendor_data, "vendor", {"name", "model", "firmware"})
    vendor = VendorInfo(*(_text(vendor_data[key], f"vendor.{key}") for key in ("name", "model", "firmware")))
    actions = data["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        _fail(f"actions must list 1 to {MAX_ACTIONS} actions")
    specs = tuple(_action(action, index) for index, action in enumerate(actions))
    if len({spec.name for spec in specs}) != len(specs):
        _fail("action names must be unique")
    _check_roles(occupancy, specs)
    return DeviceDescriptor(format=version, kind=kind, occupancy=occupancy, vendor=vendor, actions=specs)


# ── 인자 검증 (FR-06, FR-09) ────────────────────────────────────────


class ArgumentProblem(StrEnum):
    """인자 검증 결과. 판정은 PARAMETER_OUT_OF_RANGE 와 INVALID_INPUT 으로 옮긴다 (spec 결정 5)."""

    INVALID = "invalid"
    OUT_OF_RANGE = "out_of_range"


def _scalar(value: object) -> bool:
    return isinstance(value, (str, bool)) or _number(value)


def _check_value(prop: Mapping[str, Any], value: object, cap: int | float | None) -> ArgumentProblem | None:
    kind = prop["type"]
    if not _matches_type(kind, value):
        return ArgumentProblem.INVALID
    if "enum" in prop and value not in prop["enum"]:
        return ArgumentProblem.INVALID
    if kind == "string":
        if "maxLength" in prop and len(value) > prop["maxLength"]:
            return ArgumentProblem.INVALID
        if "pattern" in prop and re.search(prop["pattern"], value) is None:
            return ArgumentProblem.INVALID
        return None
    if kind in _NUMERIC_TYPES:
        maximum = prop.get("maximum")
        if cap is not None:
            maximum = cap if maximum is None else min(maximum, cap)
        if ("minimum" in prop and value < prop["minimum"]) or (maximum is not None and value > maximum):
            return ArgumentProblem.OUT_OF_RANGE
    return None


def validate_arguments(
    spec: ActionSpec, arguments: Mapping[str, Any] | None, *,
    caps: Mapping[str, int | float] | None = None, partial: bool = False,
) -> ArgumentProblem | None:
    """동작 선언의 인자 스키마로 인자를 검사한다. 문제가 없으면 None.

    `caps`는 인자별 서버 상한이다. 선언의 `maximum`과 작은 값을 쓴다. `partial`이면 `required`를
    검사하지 않는다 — 생략한 인자를 서버가 채우기 전의 방문자 입력이다.
    """
    args = {} if arguments is None else arguments
    if not isinstance(args, Mapping):
        return ArgumentProblem.INVALID
    properties = spec.properties
    out_of_range = False
    for key, value in args.items():
        if not isinstance(key, str):
            return ArgumentProblem.INVALID
        prop = properties.get(key)
        if prop is None:
            if spec.input_schema.get("additionalProperties", True) is False or not _scalar(value):
                return ArgumentProblem.INVALID
            continue
        problem = _check_value(prop, value, None if caps is None else caps.get(key))
        if problem is ArgumentProblem.INVALID:
            return problem
        out_of_range = out_of_range or problem is ArgumentProblem.OUT_OF_RANGE
    if not partial and any(name not in args for name in spec.input_schema.get("required", ())):
        return ArgumentProblem.INVALID
    return ArgumentProblem.OUT_OF_RANGE if out_of_range else None


# ── v2 템플릿 (FR-04 전환 기간, spec 결정 3) ───────────────────────

_COLOR_PATTERN = "^#[0-9A-Fa-f]{6}$"
_SIGNAL = {"kind": "signal_output"}
_NO_ARGUMENTS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": [], "additionalProperties": False}


def _light_on(brightness_max: int | None, color: bool) -> dict[str, Any]:
    if brightness_max is None:
        return {"name": "on", "description": "조명을 켠다", "input_schema": _NO_ARGUMENTS, "result": _SIGNAL}
    properties: dict[str, Any] = {"brightness": {"type": "integer", "minimum": 0, "maximum": brightness_max}}
    description = "조명을 켜고 밝기를 정한다"
    if color:
        properties["color"] = {"type": "string", "pattern": _COLOR_PATTERN}
        description = "조명을 켜고 밝기와 색을 정한다"
    return {"name": "on", "description": description, "input_schema": _schema(properties), "result": _SIGNAL}


def _speaker_on(volume_max: int | None) -> dict[str, Any]:
    if volume_max is None:
        return {"name": "on", "description": "스피커를 재생한다", "input_schema": _NO_ARGUMENTS,
                "result": _SIGNAL}
    return {"name": "on", "description": "스피커를 재생하고 음량을 정한다",
            "input_schema": _schema({"volume": {"type": "integer", "minimum": 0, "maximum": volume_max}}),
            "result": _SIGNAL}


def legacy_descriptor(
    capability: Capability, occupancy: Occupancy, *, brightness_max: int | None = None,
    color: bool = False, volume_max: int | None = None,
) -> DeviceDescriptor:
    """v2 발표(기능, 점유 방식, 제어 정보)의 선언 템플릿. 참조 펌웨어가 내장할 선언과 같다.

    역할 규칙에 맞지 않는 조합(예: shared 사물함)은 DescriptorError 다.
    """
    capability, occupancy = Capability(capability), Occupancy(occupancy)
    if capability is Capability.LOCKER:
        actions = [
            {"name": "open", "description": "사물함을 연다", "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
            {"name": "close", "description": "사물함을 잠근다. 반납이 아니다", "role": "reoperate",
             "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
        ]
    elif capability is Capability.LIGHT:
        actions = [_light_on(brightness_max, color),
                   {"name": "off", "description": "조명을 끈다", "role": "deactivate",
                    "input_schema": _NO_ARGUMENTS, "result": _SIGNAL}]
    else:
        actions = [_speaker_on(volume_max),
                   {"name": "off", "description": "스피커를 정지한다", "role": "deactivate",
                    "input_schema": _NO_ARGUMENTS, "result": _SIGNAL}]
    return parse_descriptor({
        "format": f"{FORMAT_PREFIX}{CURRENT_FORMAT}",
        "kind": capability.value,
        "occupancy": occupancy.value,
        "vendor": {"name": "nearpilot", "model": capability.value, "firmware": "v2"},
        "actions": actions,
    })


def _integer_cap(spec: ActionSpec | None, arg: str, low: int, high: int) -> int | None:
    if spec is None:
        return None
    prop = spec.properties.get(arg)
    if prop is None or prop.get("type") != "integer":
        return None
    maximum = prop.get("maximum")
    if type(maximum) is not int or not low <= maximum <= high:
        return None
    return maximum


def legacy_controls(
    descriptor: DeviceDescriptor | None,
) -> tuple[tuple[int, bool] | None, int | None]:
    """선언에서 PR ② 전까지 쓰는 조명·스피커 제어 정보를 계산한다 (spec 결정 7).

    반환은 ((brightness_max, color) 또는 None, volume_max 또는 None) 이다.
    """
    if descriptor is None:
        return None, None
    on = descriptor.action("on")
    if descriptor.kind == Capability.LIGHT.value:
        brightness = _integer_cap(on, "brightness", 1, 255)
        if brightness is None:
            return None, None
        return (brightness, "color" in on.properties), None
    if descriptor.kind == Capability.SPEAKER.value:
        return None, _integer_cap(on, "volume", 1, 30)
    return None, None
