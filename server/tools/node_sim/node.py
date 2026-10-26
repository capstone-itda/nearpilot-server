"""Virtual device logic for MQTT v2, independent of the MQTT client.

Methods return outbound messages as (kind, body) pairs; `device.py` publishes
them. `succeeded` means the virtual control signal output finished and the
result was stored; no physical state is observed or reported (2026-10-26).
The simulator shares the server PC clock (#140).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import threading
from uuid import uuid4

from tools.node_sim.store import CommandRecord, CommandStore, StorageError

SCHEMA_VERSION = 2
ACTIONS = {"locker": ("open",), "light": ("on", "off"), "speaker": ("on", "off")}
FINAL_STATES = ("succeeded", "failed", "unknown")
COMMAND_FIELDS = ("node_id", "action", "target_session_id", "issued_at", "expires_at")

Message = tuple[str, dict]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def wire_time(value: datetime) -> str:
    # Full precision: with the shared clock (skew 0) truncation could place a
    # fresh observation before the server's microsecond issue time.
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _canonical(data: dict, fields: tuple[str, ...]) -> str:
    return json.dumps({name: data.get(name) for name in fields}, sort_keys=True)


@dataclass(frozen=True)
class Beacon:
    uuid: str
    major: int
    minor: int
    rssi: float


@dataclass(frozen=True)
class Scenario:
    """Per-device failure scenario from the run configuration.

    actuation_sec has no default: time values come from configuration.
    """

    actuation_sec: float
    final: str = "succeeded"
    drop_final_result: bool = False
    result_delay_sec: float | None = None
    storage_error: bool = False
    beacons: tuple[Beacon, ...] = field(default_factory=tuple)

    def __post_init__(self):
        for name in ("actuation_sec", "result_delay_sec"):
            value = getattr(self, name)
            if value is None and name == "result_delay_sec":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid scenario: {name}")
        if self.final not in FINAL_STATES:
            raise ValueError("Invalid scenario: final")
        object.__setattr__(self, "beacons", tuple(self.beacons))


class SimNode:
    """One virtual device. Command intake, busy check and start are serialized."""

    def __init__(self, node_id: str, capability: str, occupancy: str, store: CommandStore,
                 scenario: Scenario, *, clock: Callable[[], datetime] = utc_now,
                 new_id: Callable[[], str] = lambda: str(uuid4())):
        if capability not in ACTIONS or occupancy not in ("rental", "shared"):
            raise ValueError("Unsupported capability or occupancy")
        self.node_id = node_id
        self.capability = capability
        self.occupancy = occupancy
        self.store = store
        self.scenario = scenario
        self.busy = False  # A control signal output is in progress.
        self.last_output: str | None = None  # Action of the last finished output.
        self.actuations = 0
        self.session_id: str | None = None
        self._clock = clock
        self._new_id = new_id
        self._seq: dict[str, int] = {}
        self._active: CommandRecord | None = None
        self._lock = threading.RLock()
        # A restart cannot prove completion of an interrupted command (FR-07).
        self._recovered = []
        for record in store.unfinished():
            unknown = CommandRecord(record.command_id, record.content, "unknown", self._new_id(),
                                    wire_time(self._clock()))
            store.update_command(unknown)
            self._recovered.append(unknown)
        store.fail_writes = scenario.storage_error

    # ── messages ────────────────────────────────────────────────────

    def _message(self, kind: str, **fields) -> Message:
        self._seq[kind] = self._seq.get(kind, 0) + 1
        body = dict(schema_version=SCHEMA_VERSION, node_id=self.node_id, session_id=self.session_id,
                    seq=self._seq[kind], sent_at=wire_time(self._clock()))
        body.update(fields)
        return kind, body

    def _result(self, record: CommandRecord) -> Message:
        fields = dict(command_id=record.command_id, event_id=record.event_id, state=record.state,
                      occurred_at=record.occurred_at)
        if record.error_code is not None:
            fields["error_code"] = record.error_code
        return self._message("result", **fields)

    def _event(self, command_id: str, content: str, state: str, code: str | None = None):
        return CommandRecord(command_id, content, state, self._new_id(), wire_time(self._clock()),
                             code)

    def connect(self) -> list[Message]:
        """A new MQTT connection: new session, sequences from 1, announce first."""
        with self._lock:
            self.session_id = str(uuid4())
            self._seq.clear()
            messages = [self._message("announce", capability=self.capability,
                                      occupancy=self.occupancy)]
            messages += [self._result(record) for record in self._recovered]
            self._recovered = []
            return messages

    def status(self) -> Message:
        """Command processing state only; no actual state or sensor values."""
        with self._lock:
            return self._message("status", observed_at=wire_time(self._clock()),
                                 command_state="busy" if self.busy else "idle")

    def rssi(self) -> Message:
        with self._lock:
            return self._message("rssi", frame_ts=wire_time(self._clock()), observations=[
                dict(uuid=b.uuid, major=b.major, minor=b.minor, rssi=b.rssi)
                for b in self.scenario.beacons])

    # ── commands (architecture.md §5 MQTT 4절) ──────────────────────

    def handle_command(self, payload: bytes) -> tuple[list[Message], bool]:
        """Returns messages and whether a new actuation started."""
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeError):
            return [], False
        command_id = data.get("command_id") if isinstance(data, dict) else None
        if not isinstance(command_id, str) or not command_id:
            return [], False  # No readable command ID to report against.
        content = _canonical(data, COMMAND_FIELDS)
        with self._lock:
            try:
                record = self.store.command(command_id)
            except StorageError:
                return [self._result(self._event(command_id, content, "rejected",
                                                 "STORAGE_ERROR"))], False
            # Duplicate lookup precedes connection and expiry checks.
            if record is not None:
                if record.content == content:
                    return [self._result(record)], False
                # Do not overwrite the existing record.
                return [self._result(self._event(command_id, content, "rejected",
                                                 "COMMAND_ID_CONFLICT"))], False
            code = "DEVICE_BUSY" if self.busy else self._invalid(data)
            if code is not None:
                rejected = self._event(command_id, content, "rejected", code)
                try:
                    self.store.insert_command(rejected)
                except StorageError:
                    pass  # Still not actuated; the rejection is reported.
                return [self._result(rejected)], False
            accepted = self._event(command_id, content, "accepted")
            try:
                self.store.insert_command(accepted)
            except StorageError:
                return [self._result(self._event(command_id, content, "rejected",
                                                 "STORAGE_ERROR"))], False
            messages = [self._result(accepted)]
            # Check connection and expiry again right before starting.
            code = self._invalid(data)
            state = "failed" if code else "running"
            record = self._event(command_id, content, state, code)
            try:
                self.store.update_command(record)
            except StorageError:
                record = self._event(command_id, content, "failed", "STORAGE_ERROR")
                return messages + [self._result(record)], False
            messages.append(self._result(record))
            if code:
                return messages, False
            self.busy = True
            self._active = record
            self.actuations += 1
            return messages, True

    def _invalid(self, data: dict) -> str | None:
        if data.get("schema_version") != SCHEMA_VERSION or data.get("node_id") != self.node_id:
            return "INVALID_MESSAGE"
        issued, expires = _parse_time(data.get("issued_at")), _parse_time(data.get("expires_at"))
        if issued is None or expires is None or issued >= expires:
            return "INVALID_MESSAGE"
        if data.get("target_session_id") != self.session_id:
            return "SESSION_MISMATCH"
        if data.get("action") not in ACTIONS[self.capability]:
            return "UNSUPPORTED_ACTION"
        now = self._clock()
        if now >= expires:
            return "COMMAND_EXPIRED"
        if issued > now:
            return "INVALID_MESSAGE"  # Future issue time beyond the shared-clock tolerance.
        return None

    def finish(self) -> list[Message]:
        """End the active output; the final result follows the scenario."""
        with self._lock:
            active, self._active = self._active, None
            if active is None:
                return []
            state = self.scenario.final
            if state == "succeeded":
                self.last_output = json.loads(active.content)["action"]
            record = self._event(active.command_id, active.content, state)
            self.busy = False
            try:
                self.store.update_command(record)
            except StorageError:
                return []  # Restart will report unknown from the running record.
            if self.scenario.drop_final_result:
                return []
            return [self._result(record)]
