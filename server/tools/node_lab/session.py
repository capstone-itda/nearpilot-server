"""Local console over the existing Runtime, without HTTP or authentication UI."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import threading
import time
from uuid import uuid4

from nearpilot.core.admin.service import DefaultAdminService
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.runtime import Runtime, load_config
from nearpilot.shared.enums import (
    AccessMode, Action, AdminWriteOutcome, AdminWriteReason, ApprovalMode, Capability, DeviceState,
    RecoveryStatus, Role, TrustState, Verdict, Visibility,
)
from nearpilot.shared.models import BeaconKey, BeaconRegistration, PlaceAssignment, PolicySettings, TargetSpec, UseRequest
from tools.node_lab.setup import node_kind

# Lab action -> capability it drives. Lockers open and close (architecture.md §5 MQTT).
ACTION_CAPABILITY = {"on": Capability.LIGHT, "off": Capability.LIGHT,
                     "open": Capability.LOCKER, "close": Capability.LOCKER}
USAGE = ("watch | on [nearest:light] | off [nearest:light] | open [nearest:locker] | close [nearest:locker] "
         "| release [use_id] | recover <node_id> | quit")


def now():
    return datetime.now(timezone.utc)


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


class Evidence:
    def __init__(self, path: Path, *, console: bool = True):
        self._stream = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self.console = console

    def emit(self, event: str, *, show: bool = True, **data):
        """Always writes the file; `show=False` only keeps the line off the console."""
        record = dict(at=now(), event=event, **data)
        line = json.dumps(record, default=json_default, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
            if self.console and show:
                print(line, flush=True)
        return record

    def close(self):
        self._stream.close()


class Lab:
    def __init__(self, folder: Path, *, console: bool = True):
        self.folder = folder.resolve()
        self.options = json.loads((self.folder / "lab.json").read_text(encoding="utf-8"))
        self.config = load_config(self.folder / "server.toml")
        self.runtime = Runtime(self.config)
        self.evidence = Evidence(self.folder / "evidence.jsonl", console=console)
        self._configured = set()
        self._readiness = {}
        self._snapshot_lock = threading.RLock()
        self._restart_required = False
        self._stop = threading.Event()
        self._watcher = None
        self._shown_at = None  # Monotonic time the periodic observations last reached the console.
        self.last_use_id = None  # Latest rental use opened from this console, for `release`.

    def start(self):
        self.runtime.start()
        with self.runtime.db.scope() as bound:
            admin = DefaultAdminService(bound.repo, SqliteAdminRepository(bound.conn))
            for user, role in ((self.options["owner_id"], Role.OWNER), (self.options["user_id"], Role.VISITOR)):
                result = admin.register_account(user, user, role).result
                if result.outcome != AdminWriteOutcome.APPLIED and result.reason != AdminWriteReason.ACCOUNT_ALREADY_EXISTS:
                    raise ValueError(f"시험 계정 등록 실패: {result.reason}")
            result = admin.register_beacon(BeaconRegistration("lab-beacon", self.options["user_id"],
                BeaconKey(**self.options["beacon"])))
            self._check(result)
        self.evidence.emit("lab_started", mode=self.options["mode"], models=dict(self.config.proximity.versions),
                           model_warning="확률은 연결 확인용이다. 모델 파일의 실측 여부를 확인한다.")

    @staticmethod
    def _check(result):
        if result.outcome != AdminWriteOutcome.APPLIED:
            raise ValueError(f"시험 설정 거부: {result.reason}")

    def _configure(self, bound):
        admin = DefaultAdminService(bound.repo, SqliteAdminRepository(bound.conn))
        selected = [node for node in bound.repo.nodes() if node.node_id in self.options["nodes"]]
        owner = self.options["owner_id"]
        for node in selected:
            if node.node_id in self._configured and node.trust_state == TrustState.TRUSTED and node.place_id == "node-lab":
                continue
            capability, occupancy = node_kind(node.node_id)
            if node.capability.value != capability or node.occupancy.value != occupancy:
                self.evidence.emit("node_rejected", node_id=node.node_id,
                                   reason=f"expected {occupancy} {capability}")
                continue
            self._check(admin.assign_place(owner, PlaceAssignment("node-lab", owner, "연결 시험", (node.node_id,))))
            self._check(admin.set_node_trust(owner, node.node_id, TrustState.TRUSTED))
            self._check(admin.set_policy(owner, PolicySettings(node.node_id, Visibility.PUBLIC, AccessMode.OPEN,
                None, ApprovalMode.ONE_TIME, None, None)).result)
            self._configured.add(node.node_id)
            if "restore_not_loaded" in self.runtime.services.restore.blockers(node.node_id):
                self._restart_required = True
            self.evidence.emit("node_configured", node_id=node.node_id)

    def snapshot(self, *, show: bool = True):
        with self._snapshot_lock:
            result = self._snapshot(show)
            if self._restart_required:
                # Existing RestoreService inventories nodes only on first start.
                # Rebuild via its normal restart path instead of bypassing readiness.
                self.runtime.stop()
                self.runtime = Runtime(self.config)
                self.runtime.start()
                self._restart_required = False
                self.evidence.emit("inventory_reloaded", reason="새 발표 기기를 정상 복원 경로로 읽는다")
                return {}
            return result

    def _snapshot(self, show: bool):
        with self.runtime.db.scope() as bound:
            self._configure(bound)
            nodes = [n for n in bound.repo.nodes() if n.node_id in self.options["nodes"]]
            frames = bound.repo.rssi_since([BeaconKey(**self.options["beacon"])],
                now() - timedelta(seconds=self.config.settings.beacon_recent_sec))
            latest = {}
            for frame in frames:
                latest[frame.node_id] = frame
            for node in nodes:
                frame = latest.get(node.node_id)
                status = self.runtime.services.node_events.latest_status(node.node_id)
                ready = self.runtime.services.restore.can_execute(node.node_id)
                blockers = self.runtime.services.restore.blockers(node.node_id)
                self.report_readiness(node, ready=ready, blockers=blockers, current=now())
                self.evidence.emit("node_observation", show=show, node_id=node.node_id, state=node.device_state,
                    ready=ready, blockers=blockers,
                    rssi=frame.rssi if frame else None, frame_ts=frame.frame_ts if frame else None,
                    received_at=frame.received_at if frame else None,
                    status_observed_at=status.observed_at if status else None,
                    status_received_at=status.received_at if status else None,
                    command_state=status.command_state.value if status else None)
            place_nodes = [n for n in nodes if n.place_id == "node-lab" and n.trust_state == TrustState.TRUSTED]
            if not place_nodes or not latest:
                self.evidence.emit("proximity_pending", show=show, reason="유효한 현재 비콘 관측을 기다린다")
                return {}
            result = self.runtime.services.proximity.posterior([BeaconKey(**self.options["beacon"])],
                place_nodes, now(), place_nodes=place_nodes)
            self.evidence.emit("proximity", show=show, posteriors=dict(result.posteriors), best=result.best,
                model_version=result.model_version, calib_version=result.calib_version,
                thresholds={n.capability.value: self.threshold(n.capability) for n in place_nodes})
            return dict(result.posteriors)

    def threshold(self, capability: Capability):
        return getattr(self.config.settings, f"threshold_{capability.value}")

    def report_readiness(self, node, *, ready: bool, blockers, current):
        """Log only changes. FAULT stays visible; this log never clears or overrides it."""
        ttl = timedelta(seconds=self.config.settings.online_ttl_sec)
        state = dict(session_id=node.session_id,
            online=node.last_seen_at is not None and current - node.last_seen_at <= ttl,
            trusted=node.trust_state == TrustState.TRUSTED, place_id=node.place_id,
            fault=node.device_state == DeviceState.FAULT, ready=ready, blockers=tuple(blockers))
        if self._readiness.get(node.node_id) != state:
            self._readiness[node.node_id] = state
            self.evidence.emit("node_readiness", node_id=node.node_id, **state)

    def use(self, action: str, target: str | None = None, *, request_id: str | None = None):
        with self._snapshot_lock:
            return self._use(action, target, request_id=request_id)

    def _use(self, action: str, target: str | None, *, request_id: str | None):
        if action not in ACTION_CAPABILITY:
            raise ValueError("시험 동작은 on, off, open 또는 close다")
        capability = ACTION_CAPABILITY[action]
        parsed = TargetSpec.parse(target or f"nearest:{capability.value}")
        if parsed.capability != capability:
            raise ValueError(f"{action}의 시험 대상은 {capability.value}다")
        self.snapshot()
        request = UseRequest(self.options["user_id"], request_id or uuid4().hex,
                             parsed, Action(action), "node-lab", now())
        decision = self.runtime.core.request_use(request)
        self.evidence.emit("decision", **asdict(decision))
        with self.runtime.db.scope() as bound:
            command = bound.store.execution(decision.internal_request_id) if decision.internal_request_id else None
            if command:
                self.evidence.emit("command_result", request_id=request.request_id,
                    command_id=command.command_id, node_id=command.node_id,
                    status=command.status, action=command.action)
        if decision.use_id:
            self.last_use_id = decision.use_id
        return decision

    def release(self, use_id: str | None = None):
        """Logical release of the visitor's rental use (FR-19). The door is not observed."""
        with self._snapshot_lock:
            use_id = use_id or self.last_use_id
            if not use_id:
                raise ValueError("반납할 use_id가 없다. 먼저 open을 실행한다")
            decision = self.runtime.core.release_use(self.options["user_id"], use_id)
            self.evidence.emit("release", **asdict(decision))
            if decision.verdict == Verdict.OK and use_id == self.last_use_id:
                self.last_use_id = None
            return decision

    def recover(self, node_id: str):
        """Owner approval of a FAULT recovery through the server's recovery service (FR-12A).

        Same path as `nearpilot recover`: checks readiness, approves only when ready. Never edits the DB.
        """
        owner = self.options["owner_id"]
        with self._snapshot_lock:
            readiness = self.runtime.recovery.readiness(owner, node_id)
            self.evidence.emit("recovery_readiness", **asdict(readiness))
            if readiness.status is not RecoveryStatus.READY or readiness.recovery_id is None:
                return readiness
            result = self.runtime.recovery.approve(owner, node_id, readiness.recovery_id)
            self.evidence.emit("recovery_approval", **asdict(result))
            return result

    def console_due(self, current: float) -> bool:
        """True when periodic observations should reach the console again.

        Sampling stays at `sample_sec`; `console_sec` only thins the screen. Without it every sample is shown.
        """
        interval = self.options.get("console_sec", self.options["sample_sec"])
        if self._shown_at is not None and current - self._shown_at < interval:
            return False
        self._shown_at = current
        return True

    def watch(self):
        while not self._stop.is_set():
            try:
                self.snapshot(show=self.console_due(time.monotonic()))
            except Exception as error:
                self.evidence.emit("observation_error", error_type=type(error).__name__)
            self._stop.wait(self.options["sample_sec"])

    def console(self):
        self._watcher = threading.Thread(target=self.watch, name="lab-observer", daemon=True)
        self._watcher.start()
        print(f"명령: {USAGE}", flush=True)
        while True:
            try:
                words = input("lab> ").split()
            except EOFError:
                break
            if not words:
                continue
            if words == ["quit"]:
                break
            try:
                if words == ["watch"]:
                    self.snapshot()
                elif words[0] in ACTION_CAPABILITY and len(words) <= 2:
                    self.use(words[0], words[1] if len(words) == 2 else None)
                elif words[0] == "release" and len(words) <= 2:
                    self.release(words[1] if len(words) == 2 else None)
                elif words[0] == "recover" and len(words) == 2:
                    self.recover(words[1])
                else:
                    print(USAGE)
            except (ValueError, LookupError, PermissionError) as error:
                self.evidence.emit("input_error", reason=str(error) or type(error).__name__)

    def close(self):
        self._stop.set()
        if self._watcher:
            self._watcher.join(timeout=self.config.runtime.shutdown_timeout_sec)
        try:
            self.runtime.stop()
        finally:
            self.evidence.emit("lab_stopped")
            self.evidence.close()


class DiagnosticHandler(logging.Handler):
    """Capture existing IOT rejection reasons without MQTT payloads or secrets."""
    def __init__(self, evidence):
        super().__init__(logging.WARNING)
        self.evidence = evidence

    def emit(self, record):
        self.evidence.emit("server_diagnostic", logger=record.name, level=record.levelname,
                           message=record.getMessage())


def demo(lab: Lab):
    deadline = time.monotonic() + lab.options["observe_timeout_sec"]
    while time.monotonic() < deadline:
        probabilities = lab.snapshot()
        if probabilities and max(probabilities.values()) >= lab.threshold(Capability.LIGHT):
            with lab.runtime.db.scope():
                ready = all(lab.runtime.services.restore.can_execute(node) for node in lab.options["nodes"])
            if ready and len(probabilities) == len(lab.options["nodes"]):
                break
        time.sleep(lab.options["sample_sec"])
    else:
        raise ValueError("시험 비콘의 근접 확률을 확인하지 못했다")
    for action in ("on", "off"):
        decision = lab.use(action)
        if decision.verdict != Verdict.OK:
            raise ValueError(f"시험 명령이 허가되지 않았다: {decision.reason}")
    lab.evidence.emit("demo_passed", note="시뮬레이터 결과이며 실제 ESP32 검증은 아니다")
