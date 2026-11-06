"""Stateless MQTT conversion (architecture.md §4 and §5).

v2 (2026-10-26): `status` carries only `command_state`; `result.succeeded` means the control
signal output finished. v1 messages are rejected, never reinterpreted.
v3 (#245): `announce` carries a device descriptor and `cmd` carries `arguments`. During the
transition `announce` v2 is converted with the per-kind template, and `cmd` follows the version
the node announced last (specs/iot-device-descriptor/spec.md).

Delivery metadata, connection freshness and authorization belong to the caller.
Invalid messages raise MessageValidationError without including their contents.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from uuid import UUID

from nearpilot.shared.descriptor import (
    ANNOUNCE_MAX_BYTES, DescriptorError, DeviceDescriptor, legacy_descriptor, parse_descriptor,
)
from nearpilot.shared.enums import (
    Action, Capability, CommandState, DeviceState, NodeResultState, Occupancy, RejectCode,
)
from nearpilot.shared.models import (
    BeaconKey, NodeAnnounce, NodeCommand, NodeResult, NodeStatus, RssiFrame,
)

__all__ = ["SCHEMA_VERSION", "DESCRIPTOR_SCHEMA_VERSION", "COMMAND_SCHEMA_VERSIONS",
           "MessageValidationError", "command_arguments", "decode_message", "decode_time_request",
           "encode_command", "encode_occupancy", "encode_restart", "encode_time"]

SCHEMA_VERSION = 2  # announce·cmd 밖의 모든 메시지
DESCRIPTOR_SCHEMA_VERSION = 3  # 기기 선언을 담는 announce 와 arguments 를 담는 cmd (#245)
ANNOUNCE_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, DESCRIPTOR_SCHEMA_VERSION})  # 전환 기간
COMMAND_SCHEMA_VERSIONS = ANNOUNCE_SCHEMA_VERSIONS
# PR ② 전까지 판정·조회·대시보드가 Capability 열거형을 쓴다. 그 밖의 종류는 받지 않는다 (spec 결정 6).
_SUPPORTED_KINDS = frozenset(capability.value for capability in Capability)
REQUEST_ID_MAX = 64  # time_request.request_id 의 최대 길이 (iot-mqtt-time-sync spec 5절)


class MessageValidationError(ValueError):
    """The message does not satisfy the MQTT contract.

    `session_id` is set when a rejected announce already had a valid connection ID, so the
    caller can audit the rejection once per connection (spec 결정 12).
    """

    session_id: str | None = None


def _fail(field: str) -> None:
    raise MessageValidationError(f"Invalid MQTT v2 field: {field}")


def _object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        _fail(field)
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(field)
    return value


def _integer(value: object, field: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _fail(field)
    return value


def _uuid(value: object, field: str, *, canonical: bool = False) -> str:
    value = _string(value, field)
    try:
        parsed = UUID(value)
    except ValueError:
        _fail(field)
    if canonical and str(parsed) != value:
        _fail(field)
    return value


def _time(value: object, field: str) -> datetime:
    value = _string(value, field)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|\+00:00)", value):
        _fail(field)
    try:
        return datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    except ValueError:
        _fail(field)


def _enum(kind, value: object, field: str):
    _string(value, field)
    try:
        return kind(value)
    except ValueError:
        _fail(field)


def _constant(value: str) -> None:
    _fail("JSON number")


def _pairs(pairs: list) -> dict:
    data = {}
    for key, value in pairs:
        if key in data:
            _fail("duplicate JSON key")
        data[key] = value
    return data


def _finite_json(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        _fail("JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _finite_json(child)


def _legacy_descriptor(data: dict) -> DeviceDescriptor:
    """v2 `announce`(`capability`, `occupancy`, `controls?`) → 선언 템플릿 (FR-04 전환 기간).

    `controls`는 `light`, `speaker` 기기만 보낸다. 기능마다 정한 키가 모두 맞아야 한다 (FR-04, FR-09).
    """
    capability =_enum(Capability, data.get("capability"), "capability")
    occupancy = _enum(Occupancy, data.get("occupancy"), "occupancy")
    controls: dict = {}
    if "controls" in data:
        raw = _object(data["controls"], "controls")
        if capability is Capability.SPEAKER:
            if set(raw) != {"volume_max"}:
                _fail("controls")
            controls = dict(volume_max=_integer(raw["volume_max"], "controls.volume_max", 1, 30))
        else:
            if capability is not Capability.LIGHT or set(raw) != {"brightness_max", "color"}:
                _fail("controls")
            if not isinstance(raw["color"], bool):
                _fail("controls.color")
            controls = dict(brightness_max=_integer(raw["brightness_max"], "controls.brightness_max", 1, 255),
                            color=raw["color"])
    try:
        return legacy_descriptor(capability, occupancy, **controls)
    except DescriptorError as error:
        raise MessageValidationError(f"Invalid MQTT v2 announce: {error}") from None


def _descriptor(data: dict, raw_size: int) -> DeviceDescriptor:
    """v3 `announce.descriptor` (device-protocol.md 3·4·5절)."""
    if raw_size > ANNOUNCE_MAX_BYTES:
        _fail("announce size")
    try:
        descriptor = parse_descriptor(data.get("descriptor"))
    except DescriptorError as error:
        raise MessageValidationError(f"Invalid device descriptor: {error}") from None
    if descriptor.kind not in _SUPPORTED_KINDS:
        raise MessageValidationError("Invalid device descriptor: kind not supported yet")
    return descriptor


def _topic(topic: object) -> tuple[str, str]:
    if not isinstance(topic, str):
        _fail("topic")
    parts = topic.split("/")
    if len(parts) != 4 or parts[:2] != ["nearpilot", "node"] or not parts[2] or any(c in parts[2] for c in "+#\x00"):
        _fail("topic")
    return parts[2], parts[3]


def _load(payload: object, node_id: str, versions: frozenset[int] = frozenset({SCHEMA_VERSION})) -> dict:
    """JSON object with an accepted version and the topic's node_id."""
    if not isinstance(payload, (bytes, str)):
        _fail("payload")
    try:
        raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        data = _object(json.loads(raw, parse_constant=_constant, object_pairs_hook=_pairs), "payload")
        _finite_json(data)
    except (UnicodeError, ValueError, RecursionError):
        raise MessageValidationError("Invalid MQTT v2 JSON") from None
    if _integer(data.get("schema_version"), "schema_version", 1) not in versions:
        _fail("schema_version")
    if _string(data.get("node_id"), "node_id") != node_id:
        _fail("node_id")
    return data


def decode_time_request(topic: str, payload: bytes | str) -> tuple[str, str]:
    """`time_request` → (node_id, request_id). It has no seq or sent_at: the clock is not set yet (#236)."""
    node_id, kind = _topic(topic)
    if kind != "time_request":
        _fail("topic")
    data = _load(payload, node_id)
    _uuid(data.get("session_id"), "session_id")
    request_id = _string(data.get("request_id"), "request_id")
    if len(request_id) > REQUEST_ID_MAX or any(c < " " or c == "\x7f" for c in request_id):
        _fail("request_id")
    return node_id, request_id


def encode_time(node_id: str, request_id: str, server_time: datetime, *, max_error_sec: float,
                trust_sec: int, resync_sec: int) -> bytes:
    """`time` reply. server_time is the server UTC clock right before publishing (#236)."""
    node_id = _string(node_id, "node_id")
    if any(c in node_id for c in "/+#\x00"):
        _fail("node_id")
    return json.dumps(dict(schema_version=SCHEMA_VERSION, node_id=node_id,
        request_id=_string(request_id, "request_id"), server_time=_wire_time(server_time, "server_time"),
        max_error_sec=max_error_sec, trust_sec=trust_sec, resync_sec=resync_sec),
        allow_nan=False).encode("utf-8")


def decode_message(
    topic: str, payload: bytes | str, *, received_at: datetime,
) -> NodeAnnounce | tuple[RssiFrame, ...] | NodeStatus | NodeResult:
    """Validate a complete report before returning shared models.

    RSSI returns a tuple, including an empty tuple for an empty batch. The caller
    supplies the first reception time; a payload cannot override that time.
    """
    node_id, kind = _topic(topic)
    if kind not in ("announce", "rssi", "status", "result"):
        _fail("topic")
    if not isinstance(received_at, datetime) or received_at.utcoffset() != timedelta(0):
        _fail("received_at")
    versions = ANNOUNCE_SCHEMA_VERSIONS if kind == "announce" else frozenset({SCHEMA_VERSION})
    data = _load(payload, node_id, versions)
    common = dict(
        node_id=node_id, session_id=_uuid(data.get("session_id"), "session_id"),
        seq=_integer(data.get("seq"), "seq", 1),
        sent_at=_time(data.get("sent_at"), "sent_at"), received_at=received_at,
    )
    if kind == "announce":
        version = data["schema_version"]
        try:
            if version == DESCRIPTOR_SCHEMA_VERSION:
                size = len(payload.encode("utf-8") if isinstance(payload, str) else payload)
                descriptor = _descriptor(data, size)
            else:
                descriptor = _legacy_descriptor(data)
        except MessageValidationError as error:
            error.session_id = common["session_id"]
            raise
        return NodeAnnounce(**common, descriptor=descriptor, schema_version=version)
    if kind == "rssi":
        frame_ts = _time(data.get("frame_ts"), "frame_ts")
        observations = data.get("observations")
        if not isinstance(observations, list):
            _fail("observations")
        validated, seen = [], set()
        for observation in observations:
            item = _object(observation, "observation")
            beacon = BeaconKey(
                _uuid(item.get("uuid"), "uuid", canonical=True),
                _integer(item.get("major"), "major", 0, 65535),
                _integer(item.get("minor"), "minor", 0, 65535))
            rssi = item.get("rssi")
            if type(rssi) not in (int, float) or not -math.inf < rssi < math.inf or beacon in seen:
                _fail("rssi observation")
            seen.add(beacon)
            validated.append((beacon, rssi))
        return tuple(
            RssiFrame(**common, beacon=beacon, rssi=rssi, frame_ts=frame_ts)
            for beacon, rssi in validated
        )
    if kind == "status":
        return NodeStatus(**common, observed_at=_time(data.get("observed_at"), "observed_at"),
            command_state=_enum(CommandState, data.get("command_state"), "command_state"))
    state = _enum(NodeResultState, data.get("state"), "state")
    error = data.get("error_code")
    if "error_code" in data:
        _string(error, "error_code")
    if state == NodeResultState.REJECTED:
        _enum(RejectCode, error, "error_code")
    return NodeResult(**common, state=state, error_code=error,
        command_id=_string(data.get("command_id"), "command_id"),
        event_id=_uuid(data.get("event_id"), "event_id"),
        occurred_at=_time(data.get("occurred_at"), "occurred_at"))


def _wire_time(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        _fail(field)
    return value.isoformat().replace("+00:00", "Z")


def _outbound(request: NodeCommand, schema_version: int = SCHEMA_VERSION) -> dict:
    node_id = _string(request.node_id, "node_id")
    if any(c in node_id for c in "/+#\x00"):
        _fail("node_id")
    return dict(schema_version=schema_version, node_id=node_id,
        target_session_id=_uuid(request.target_session_id, "target_session_id"),
        issued_at=_wire_time(request.issued_at, "issued_at"),
        expires_at=_wire_time(request.expires_at, "expires_at"))


def command_arguments(command: NodeCommand) -> dict | None:
    """The command's action arguments, or None for a command without arguments.

    Until PR ② the decision hands light and speaker settings separately; both map to the
    same keys as v3 `arguments` (architecture.md §5 MQTT 3절).
    """
    light, speaker = command.light, command.speaker
    if light is not None and speaker is not None:
        _fail("arguments")  # 한 명령에는 기능 하나의 인자만 담는다.
    if light is not None:
        # 서버가 정한 밝기와 색을 모두 보낸다. 조명 on 에만 담는다 (FR-09).
        if not light.complete or command.action is not Action.ON:
            _fail("arguments")
        return {"brightness": light.brightness, "color": light.color}
    if speaker is not None:
        # 서버가 정한 음량을 보낸다. 스피커 on 에만 담는다 (FR-09).
        if not speaker.complete or command.action is not Action.ON:
            _fail("arguments")
        return {"volume": speaker.volume}
    return None


def encode_command(command: NodeCommand, *, schema_version: int) -> bytes:
    """Encode only MQTT command fields; preserve IDs and validity times.

    `schema_version` is the version the target connection announced (spec 결정 11·13). v3 sends
    `arguments`; v2 sends the same values as `parameters`.
    """
    if schema_version not in COMMAND_SCHEMA_VERSIONS:
        _fail("schema_version")
    data = _outbound(command, schema_version)
    data.update(command_id=_string(command.command_id, "command_id"),
                action=_enum(Action, command.action, "action").value)
    arguments = command_arguments(command)
    if arguments is not None:
        data["arguments" if schema_version == DESCRIPTOR_SCHEMA_VERSION else "parameters"] = arguments
    return json.dumps(data, allow_nan=False).encode("utf-8")


def encode_restart(node_id: str, restart_id: str, target_session_id: str, issued_at: datetime,
                   expires_at: datetime) -> bytes:
    """`restart` for the node's current connection (#235). No user or token is included."""
    node_id = _string(node_id, "node_id")
    if any(c in node_id for c in "/+#\x00"):
        _fail("node_id")
    restart_id = _string(restart_id, "restart_id")
    if len(restart_id) > REQUEST_ID_MAX or any(c < " " or c == "\x7f" for c in restart_id):
        _fail("restart_id")
    issued, expires = _wire_time(issued_at, "issued_at"), _wire_time(expires_at, "expires_at")
    if not issued_at < expires_at:
        _fail("expires_at")
    return json.dumps(dict(schema_version=SCHEMA_VERSION, node_id=node_id, restart_id=restart_id,
        target_session_id=_uuid(target_session_id, "target_session_id"),
        issued_at=issued, expires_at=expires), allow_nan=False).encode("utf-8")


def encode_occupancy(node_id: str, device_state: DeviceState, sent_at: datetime) -> bytes:
    """Encode the retained display of a stored device state. No user or use ID is included."""
    node_id = _string(node_id, "node_id")
    if any(c in node_id for c in "/+#\x00"):
        _fail("node_id")
    return json.dumps(dict(schema_version=SCHEMA_VERSION, node_id=node_id,
        device_state=_enum(DeviceState, device_state, "device_state").value,
        sent_at=_wire_time(sent_at, "sent_at")), allow_nan=False).encode("utf-8")
