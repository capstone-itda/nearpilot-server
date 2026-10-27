"""Stateless MQTT v2 conversion (architecture.md §4 and §5).

v2 (2026-10-26): `status` carries only `command_state`; `result.succeeded` means the control
signal output finished. v1 messages are rejected, never reinterpreted.

Delivery metadata, connection freshness and authorization belong to the caller.
Invalid messages raise MessageValidationError without including their contents.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from uuid import UUID

from nearpilot.shared.enums import (
    Action, Capability, CommandState, DeviceState, NodeResultState, Occupancy, RejectCode,
)
from nearpilot.shared.models import (
    BeaconKey, NodeAnnounce, NodeCommand, NodeResult, NodeStatus, RssiFrame,
)

__all__ = ["SCHEMA_VERSION", "MessageValidationError", "decode_message", "encode_command",
           "encode_occupancy"]

SCHEMA_VERSION = 2


class MessageValidationError(ValueError):
    """The message does not satisfy the MQTT v2 contract."""


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


def decode_message(
    topic: str, payload: bytes | str, *, received_at: datetime,
) -> NodeAnnounce | tuple[RssiFrame, ...] | NodeStatus | NodeResult:
    """Validate a complete report before returning shared models.

    RSSI returns a tuple, including an empty tuple for an empty batch. The caller
    supplies the first reception time; a payload cannot override that time.
    """
    if not isinstance(topic, str):
        _fail("topic")
    parts = topic.split("/")
    if len(parts) != 4 or parts[:2] != ["nearpilot", "node"] or not parts[2] or any(c in parts[2] for c in "+#\x00"):
        _fail("topic")
    node_id, kind = parts[2:]
    if kind not in ("announce", "rssi", "status", "result"):
        _fail("topic")
    if not isinstance(received_at, datetime) or received_at.utcoffset() != timedelta(0):
        _fail("received_at")
    if not isinstance(payload, (bytes, str)):
        _fail("payload")
    try:
        raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        data = _object(json.loads(raw, parse_constant=_constant, object_pairs_hook=_pairs), "payload")
        _finite_json(data)
    except (UnicodeError, ValueError, RecursionError):
        raise MessageValidationError("Invalid MQTT v2 JSON") from None
    if _integer(data.get("schema_version"), "schema_version", 1) != SCHEMA_VERSION:
        _fail("schema_version")
    if _string(data.get("node_id"), "node_id") != node_id:
        _fail("node_id")
    common = dict(
        node_id=node_id, session_id=_uuid(data.get("session_id"), "session_id"),
        seq=_integer(data.get("seq"), "seq", 1),
        sent_at=_time(data.get("sent_at"), "sent_at"), received_at=received_at,
    )
    if kind == "announce":
        return NodeAnnounce(**common,
            capability=_enum(Capability, data.get("capability"), "capability"),
            occupancy=_enum(Occupancy, data.get("occupancy"), "occupancy"))
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


def _outbound(request: NodeCommand) -> dict:
    node_id = _string(request.node_id, "node_id")
    if any(c in node_id for c in "/+#\x00"):
        _fail("node_id")
    return dict(schema_version=SCHEMA_VERSION, node_id=node_id,
        target_session_id=_uuid(request.target_session_id, "target_session_id"),
        issued_at=_wire_time(request.issued_at, "issued_at"),
        expires_at=_wire_time(request.expires_at, "expires_at"))


def encode_command(command: NodeCommand) -> bytes:
    """Encode only MQTT command fields; preserve IDs and validity times."""
    data = _outbound(command)
    data.update(command_id=_string(command.command_id, "command_id"),
                action=_enum(Action, command.action, "action").value)
    return json.dumps(data, allow_nan=False).encode("utf-8")


def encode_occupancy(node_id: str, device_state: DeviceState, sent_at: datetime) -> bytes:
    """Encode the retained display of a stored device state. No user or use ID is included."""
    node_id = _string(node_id, "node_id")
    if any(c in node_id for c in "/+#\x00"):
        _fail("node_id")
    return json.dumps(dict(schema_version=SCHEMA_VERSION, node_id=node_id,
        device_state=_enum(DeviceState, device_state, "device_state").value,
        sent_at=_wire_time(sent_at, "sent_at")), allow_nan=False).encode("utf-8")
