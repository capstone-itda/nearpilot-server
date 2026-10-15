"""Virtual device logic for MQTT v1, independent of the MQTT client.

Methods return outbound messages as (kind, body) pairs; `device.py` publishes
them. Physical values here are virtual observations, not real sensor ability
(NFR-10, #142). The simulator shares the server PC clock (#140).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import threading
from uuid import uuid4

from tools.node_sim.store import CheckRecord, CommandRecord, CommandStore, StorageError

ACTIONS = {"locker": ("open",), "light": ("on", "off"), "speaker": ("on", "off")}
# architecture.md §4 반납 검사 v1의 기준과 필수 항목 (#141)
CHECK_ITEMS = {"locker-return": ("door_closed", "locked"),
               "light-return": ("actuator_idle",), "speaker-return": ("actuator_idle",)}
CRITERIA = {"locker": "locker-return", "light": "light-return", "speaker": "speaker-return"}
FINAL_STATES = ("succeeded", "failed", "unknown")
COMMAND_FIELDS = ("node_id", "action", "target_session_id", "issued_at", "expires_at")
CHECK_FIELDS = ("node_id", "target_session_id", "criteria_id", "criteria_version",
                "issued_at", "expires_at")

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
    light_observable: bool = True
    unmeasurable: tuple[str, ...] = ()
    sensor_fault: bool = False
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
        object.__setattr__(self, "unmeasurable", tuple(self.unmeasurable))
        object.__setattr__(self, "beacons", tuple(self.beacons))


@dataclass
class Physical:
    door_closed: bool = True
    locked: bool = True
    light_on: bool = False
    playing: bool = False
    busy: bool = False


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
        self.physical = Physical()
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
        body = dict(schema_version=1, node_id=self.node_id, session_id=self.session_id,
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
        with self._lock:
            p = self.physical
            light = (p.light_on if self.scenario.light_observable else None
                     ) if self.capability == "light" else None
            return self._message("status", observed_at=wire_time(self._clock()), physical_state=dict(
                door_closed=p.door_closed if self.capability == "locker" else None,
                light_on=light, actuator_state="busy" if p.busy else "idle",
                sensor_fault=self.scenario.sensor_fault))

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
            code = "DEVICE_BUSY" if self.physical.busy else self._invalid(data)
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
            self.physical.busy = True
            self._active = record
            self.actuations += 1
            return messages, True

    def _invalid(self, data: dict) -> str | None:
        if data.get("schema_version") != 1 or data.get("node_id") != self.node_id:
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
        """End the active actuation; the final result follows the scenario."""
        with self._lock:
            active, self._active = self._active, None
            if active is None:
                return []
            action = json.loads(active.content)["action"]
            state = self.scenario.final
            if state != "failed":
                self._apply(action)
            if (state == "succeeded" and self.capability == "light"
                    and not self.scenario.light_observable):
                state = "unknown"  # IR output alone is not completion (#142).
            record = self._event(active.command_id, active.content, state)
            self.physical.busy = False
            try:
                self.store.update_command(record)
            except StorageError:
                return []  # Restart will report unknown from the running record.
            if self.scenario.drop_final_result:
                return []
            return [self._result(record)]

    def _apply(self, action: str) -> None:
        if self.capability == "locker":
            self.physical.locked = False
        elif self.capability == "light":
            self.physical.light_on = action == "on"
        else:
            self.physical.playing = action == "on"

    def set_door(self, closed: bool) -> bool:
        """Scenario hook: open an unlocked door, or close and re-lock it."""
        with self._lock:
            if self.capability != "locker" or (not closed and self.physical.locked):
                return False
            self.physical.door_closed = closed
            if closed:
                self.physical.locked = True
            return True

    # ── safety checks (architecture.md §4 반납 검사와 종료) ──────────

    def handle_check(self, payload: bytes) -> list[Message]:
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeError):
            return []
        check_id = data.get("check_id") if isinstance(data, dict) else None
        if not isinstance(check_id, str) or not check_id:
            return []
        content = _canonical(data, CHECK_FIELDS)
        with self._lock:
            try:
                record = self.store.check(check_id)
            except StorageError:
                return []
            if record is not None:
                # Same content: report the stored result. Different: keep it, report nothing.
                return [self._message("safety_check_result", **json.loads(record.result))
                        ] if record.content == content else []
            issued, expires = _parse_time(data.get("issued_at")), _parse_time(data.get("expires_at"))
            now = self._clock()
            if (data.get("schema_version") != 1 or data.get("node_id") != self.node_id
                    or data.get("target_session_id") != self.session_id or issued is None
                    or expires is None or issued >= expires or now >= expires
                    or not isinstance(data.get("criteria_id"), str)
                    or type(data.get("criteria_version")) is not int):
                return []
            result = self._measure(check_id, data["criteria_id"], data["criteria_version"], now)
            try:
                self.store.insert_check(CheckRecord(check_id, content, json.dumps(result)))
            except StorageError:
                pass
            return [self._message("safety_check_result", **result)]

    def _measure(self, check_id: str, criteria_id: str, version: int, now: datetime) -> dict:
        # Unknown criteria or another device type's criteria cannot be measured here.
        items = (CHECK_ITEMS[criteria_id]
                 if version == 1 and criteria_id == CRITERIA[self.capability] else None)
        observed = dict(door_closed=self.physical.door_closed, locked=self.physical.locked,
                        actuator_idle=not self.physical.busy)
        evidence = {}
        for item in items or ():
            if item in self.scenario.unmeasurable:
                continue  # Never invent a bool for an unmeasured item (#143).
            evidence[item] = dict(value=observed[item], observed_at=wire_time(now))
        complete = items is not None and len(evidence) == len(items)
        if self.scenario.sensor_fault and complete:
            outcome = "fault"
        elif not complete:
            outcome = "unknown"
        elif all(item["value"] for item in evidence.values()):
            outcome = "passed"
        else:
            outcome = "not_ready"
        return dict(check_id=check_id, event_id=self._new_id(), criteria_id=criteria_id,
                    criteria_version=version, checked_at=wire_time(now), outcome=outcome,
                    evidence=evidence)
