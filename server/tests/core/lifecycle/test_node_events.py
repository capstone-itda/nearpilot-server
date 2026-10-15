"""기기 이벤트의 실제 상태 관리 연결. DB·IOT·근접 추정은 shared 계약의 fake다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, RLock, Thread
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.execution import ExecutionService
from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService
from nearpilot.core.lifecycle.node_events import NodeEventService
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.core.lifecycle.restore import RestoreService
from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, ActuatorState, Capability, CommandOutcome, DeviceState, EndReason,
    ExecutionKind, ExecutionStatus, NodeResultState, Occupancy, ReasonCode,
    SafetyCheckOutcome, SessionState, StoreOutcome, TrustState, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.interfaces import NodeEventSink
from nearpilot.shared.models import (
    BeaconKey, CheckCriteria, CloseResult, CommandRecord, CommandResult, Decision, ExecutionContext,
    NodeAnnounce, NodeInfo, NodeResult, NodeStatus, PhysicalState, PolicyCheckResult,
    RestoredSession, RestoreBundle, RssiFrame, SafetyCheckResult, StoreResult,
    TargetSpec, UseRequest, UseSession,
)


class RepositoryFake:
    def __init__(self, now):
        self.items = {"locker": NodeInfo("locker", "place", Capability.LOCKER,
            Occupancy.RENTAL, TrustState.TRUSTED, DeviceState.AVAILABLE, None, now,
            session_id="connection")}
        self.sessions, self.frames, self.calls, self.errors = {}, {}, [], {}
        self.beacon = BeaconKey("beacon", 1, 1)

    def fail(self, operation):
        error = self.errors.pop(operation, None)
        if error:
            raise error

    def nodes(self, capability=None):
        self.fail("nodes")
        return tuple(n for n in self.items.values() if capability is None or n.capability == capability)

    def upsert_announced_node(self, report):
        self.calls.append(("announce", report))
        self.fail("announce")
        old = self.items.get(report.node_id)
        if old is None:
            self.items[report.node_id] = NodeInfo(report.node_id, None, report.capability,
                report.occupancy, TrustState.DISCOVERED, DeviceState.AVAILABLE, None, None)
        elif old.capability != report.capability or old.occupancy != report.occupancy:
            self.items[report.node_id] = replace(old, capability=report.capability,
                occupancy=report.occupancy, trust_state=TrustState.DISCOVERED)

    def set_node_session(self, node_id, session_id, first_seen_at):
        self.calls.append(("connection", node_id, session_id, first_seen_at))
        self.fail("connection")
        old = self.items[node_id]
        if old.session_id != session_id:
            self.items[node_id] = replace(old, session_id=session_id, session_first_seen_at=first_seen_at)

    def touch_node(self, node_id, seen_at):
        self.calls.append(("touch", node_id, seen_at))
        self.fail("touch")
        old = self.items[node_id]
        if old.last_seen_at is None or seen_at > old.last_seen_at:
            self.items[node_id] = replace(old, last_seen_at=seen_at)

    def save_rssi(self, frame):
        self.calls.append(("rssi", frame))
        self.fail("rssi")
        if frame.beacon == self.beacon:
            self.frames[(frame.node_id, frame.session_id, frame.seq, frame.beacon)] = frame

    def beacons_of(self, user_id):
        return (self.beacon,) if user_id == "visitor" else ()

    def session(self, use_id):
        return self.sessions.get(use_id)

    def session_owner_matches(self, use_id, user_id):
        return use_id in self.sessions and user_id == "visitor"


class StoreFake:
    def __init__(self, repository, clock):
        self.repo, self.clock = repository, clock
        self.commands, self.recoveries, self.closed, self.usages = {}, {}, {}, {}
        self.calls, self.errors = [], {}
        self.isolate_outcome = None

    def fail(self, name):
        error = self.errors.pop(name, None)
        if error:
            raise error

    def restore_pending(self):
        self.fail("restore")
        return tuple(RestoreBundle(node, self.clock(),
            commands=tuple(c for c in self.commands.values() if c.node_id == node.node_id),
            sessions=tuple(RestoredSession(s, "visitor", self.clock())
                for s in self.repo.sessions.values() if s.node_id == node.node_id),
            recoveries=tuple(r for r in self.recoveries.values() if r.node_id == node.node_id))
            for node in self.repo.items.values())

    def isolate_device(self, node_id, evidence):
        self.calls.append(("isolate", node_id, evidence))
        self.fail("isolate")
        if self.isolate_outcome == StoreOutcome.CONFLICT:
            return StoreResult(StoreOutcome.CONFLICT)
        node = self.repo.items[node_id]
        old = node.device_state
        self.repo.items[node_id] = replace(node, device_state=DeviceState.FAULT)
        self.fail("isolate_after")
        return StoreResult(StoreOutcome.ALREADY_APPLIED if old == DeviceState.FAULT else StoreOutcome.APPLIED)

    def save_recovery(self, record):
        self.calls.append(("recovery", record))
        self.fail("recovery")
        self.recoveries[record.recovery_id] = record
        return StoreResult(StoreOutcome.APPLIED, recovery=record)

    def recovery(self, recovery_id):
        return self.recoveries.get(recovery_id)

    def execution(self, internal_request_id):
        self.fail("execution")
        return self.commands.get(internal_request_id)

    def record_result(self, command_id, outcome, occurred_at, rental_due_at, evidence):
        self.calls.append(("result", command_id, outcome))
        self.fail("result")
        old = next(c for c in self.commands.values() if c.command_id == command_id)
        if old.outcome is not None:
            same = (old.outcome, old.occurred_at, old.evidence) == (outcome, occurred_at, evidence)
            return StoreResult(StoreOutcome.ALREADY_APPLIED if same else StoreOutcome.CONFLICT, command=old)
        saved = replace(old, status=ExecutionStatus(outcome.value), outcome=outcome,
                        occurred_at=occurred_at, evidence=evidence)
        self.commands[old.internal_request_id] = saved
        node = self.repo.items[old.node_id]
        if outcome in (CommandOutcome.UNKNOWN, CommandOutcome.FAILED):
            self.repo.items[node.node_id] = replace(node, device_state=DeviceState.FAULT)
            self.usages[old.internal_request_id] = "held"
        elif outcome == CommandOutcome.SUCCEEDED:
            self.usages[old.internal_request_id] = "consumed"
            if old.use_id is not None:
                session = self.repo.sessions[old.use_id]
                self.repo.sessions[old.use_id] = replace(session, state=SessionState.ACTIVE,
                    rental_due_at=rental_due_at if rental_due_at is not None else session.rental_due_at)
                self.repo.items[node.node_id] = replace(node, device_state=DeviceState.ACTIVE)
        return StoreResult(StoreOutcome.APPLIED, command=saved)

    def begin_close(self, use_id, reason):
        session = self.repo.sessions[use_id]
        self.repo.sessions[use_id] = replace(session, state=SessionState.CLOSING)
        self.repo.items[session.node_id] = replace(self.repo.items[session.node_id], device_state=DeviceState.CLOSING)
        return StoreResult(StoreOutcome.APPLIED, session=self.repo.sessions[use_id])

    def finish_close(self, use_id, closed_at, check_id, evidence):
        self.fail("finish")
        session = self.repo.sessions[use_id]
        self.repo.sessions[use_id] = replace(session, state=SessionState.CLOSED,
            closed_at=closed_at, end_reason=EndReason.RELEASE)
        self.repo.items[session.node_id] = replace(self.repo.items[session.node_id], device_state=DeviceState.AVAILABLE)
        self.closed[use_id] = CloseResult(use_id, closed_at, EndReason.RELEASE, check_id, evidence)
        return StoreResult(StoreOutcome.APPLIED, session=self.repo.sessions[use_id])

    def close_result(self, use_id):
        return self.closed.get(use_id)


@pytest.fixture
def env():
    time = [datetime(2026, 10, 15, tzinfo=timezone.utc)]
    clock = lambda: time[0]
    repo = RepositoryFake(clock())
    store = StoreFake(repo, clock)
    locks, index = {}, Lock()
    def lock_for(node_id):
        with index:
            return locks.setdefault(node_id, RLock())
    settings = Settings()
    policy = SimpleNamespace(check=lambda *args: PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1))
    recovery = FaultRecoveryService(repository=repo, store=store, clock=clock, lock_for=lock_for)
    requests = []
    release = ReleaseService(repository=repo, store=store,
        gateway=SimpleNamespace(request_check=requests.append), settings=lambda: settings,
        clock=clock, lock_for=lock_for, record_fault=recovery.record_fault)
    light = SharedLightService(repository=repo, policy=policy, settings=lambda: settings,
        clock=clock, lock_for=lock_for)
    restore = RestoreService(repository=repo, store=store, release=release, recovery=recovery,
        shared_light=light, settings=lambda: settings, clock=clock, lock_for=lock_for)
    restore.restore()
    execution = ExecutionService(repository=repo, store=store, rechecker=SimpleNamespace(),
        gateway=SimpleNamespace(), clock=clock, lock_for=lock_for,
        can_execute=restore.can_execute, record_isolated=recovery.record_isolated)
    observations, contexts = [], {}
    proximity = SimpleNamespace(observe=lambda frame, *, node: observations.append((frame, node)))
    def create():
        return NodeEventService(repository=repo, store=store, proximity=proximity,
            execution=execution, release=release, restore=restore, recovery=recovery,
            shared_light=light, context_for=contexts.get, lock_for=lock_for)
    return SimpleNamespace(time=time, clock=clock, repo=repo, store=store, settings=settings,
        lock_for=lock_for, release=release, restore=restore, recovery=recovery, light=light,
        execution=execution, observations=observations, contexts=contexts, proximity=proximity,
        requests=requests, create=create, sink=create())


def status(env, **changes):
    at = env.clock()
    return replace(NodeStatus("locker", at, "connection", 1, at, at,
        PhysicalState(True, None, ActuatorState.IDLE, False)), **changes)


def announce(env, **changes):
    at = env.clock()
    return replace(NodeAnnounce("locker", Capability.LOCKER, Occupancy.RENTAL,
        at, "announced-connection", 1, at), **changes)


def frame(env, **changes):
    at = env.clock()
    return replace(RssiFrame("locker", env.repo.beacon, -50, at, "connection", 1, at, at), **changes)


def occupied(env):
    session = UseSession("use", "request", "locker", SessionState.ACTIVE,
        env.clock() + timedelta(seconds=123),
        release_policy={"check_criteria": {"id": "return", "version": 1}})
    env.repo.sessions[session.use_id] = session
    env.repo.items["locker"] = replace(env.repo.items["locker"], device_state=DeviceState.ACTIVE)
    return session


def command(env, *, shared=False):
    if shared:
        env.repo.items["locker"] = replace(env.repo.items["locker"], capability=Capability.LIGHT,
            occupancy=Occupancy.SHARED)
    else:
        occupied(env)
    ctx = ExecutionContext("request", UseRequest("visitor", "external",
        TargetSpec.parse("nearest:light" if shared else "nearest:locker"),
        Action.ON if shared else Action.OPEN, "test", env.clock()), "locker",
        None if shared else "use", "usage", env.settings,
        ExecutionKind.SHARED if shared else ExecutionKind.RENTAL_INITIAL)
    saved = CommandRecord("command", "request", "locker", ctx.request.action,
        ExecutionStatus.IN_PROGRESS, ctx.execution_kind, ctx.use_id, ctx.usage_id,
        env.clock(), env.clock(), True)
    env.store.commands["request"] = saved
    env.contexts["command"] = ctx
    return NodeResult("locker", "connection", 2, env.clock(), env.clock(), "command",
        "event", NodeResultState.SUCCEEDED, env.clock())


def test_FR04_sink_implements_shared_protocol(env):
    assert isinstance(env.sink, NodeEventSink)


def test_FR04_announce_upserts_node(env):
    env.sink.on_announce(announce(env, node_id="new"))
    node = env.repo.items["new"]
    assert node.trust_state == TrustState.DISCOVERED
    assert node.session_id is node.last_seen_at is None


def test_FR04_same_announce_preserves_trust(env):
    env.sink.on_announce(announce(env))
    assert env.repo.items["locker"].trust_state == TrustState.TRUSTED


def test_FR04_changed_announce_forwarded(env):
    env.sink.on_announce(announce(env, capability=Capability.LIGHT))
    node = env.repo.items["locker"]
    assert node.trust_state == TrustState.DISCOVERED
    assert node.capability == Capability.LIGHT
    assert env.store.recoveries == {}


@pytest.mark.parametrize("change", [{"capability": Capability.LIGHT}, {"occupancy": Occupancy.SHARED}])
def test_FR04_changed_announce_during_occupancy(env, change):
    original = occupied(env)
    report = announce(env, **change)
    env.sink.on_announce(report)
    env.sink.on_announce(report)
    node = env.repo.items["locker"]
    assert (node.trust_state, node.device_state) == (TrustState.DISCOVERED, DeviceState.FAULT)
    assert env.repo.sessions["use"] == original
    recovery, = env.store.recoveries.values()
    assert (recovery.use_id, recovery.internal_request_ids) == ("use", ("request",))


def test_FR13_announce_does_not_touch(env):
    original = env.repo.items["locker"]
    env.sink.on_announce(announce(env))
    assert env.repo.items["locker"].last_seen_at == original.last_seen_at
    assert all(c[0] not in ("touch", "connection") for c in env.repo.calls)


def test_FR04_status_sets_node_session(env):
    env.sink.on_status(status(env, session_id="new-connection"))
    node = env.repo.items["locker"]
    assert (node.session_id, node.session_first_seen_at) == ("new-connection", env.clock())


def test_FR13_status_touches_with_received_at(env):
    env.time[0] += timedelta(seconds=1)
    report = status(env, sent_at=env.clock() - timedelta(seconds=1))
    env.sink.on_status(report)
    assert env.repo.items["locker"].last_seen_at == report.received_at
    assert env.sink.latest_status("locker") == report
    assert env.restore.can_execute("locker")


def test_FR05_rssi_saved_and_observed(env):
    report = frame(env)
    env.sink.on_rssi(report)
    assert list(env.repo.frames.values()) == [report]
    assert env.observations == [(report, env.repo.items["locker"])]


def test_FR05_duplicate_rssi_does_not_observe_twice(env):
    report = frame(env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(env.sink.on_rssi, [report] * 20))
    assert len(env.repo.frames) == len(env.observations) == 1


def test_FR05_rssi_progress_does_not_keep_every_completed_frame(env):
    for seq in range(30):
        env.sink.on_rssi(frame(env, seq=seq))
    assert len(env.sink._rssi) == 0
    assert len(env.sink._rssi_done) == 1


@pytest.mark.parametrize("error", [StoreNotApplied("rssi"), StoreOutcomeUnknown("rssi")])
def test_FR05_rssi_store_error_propagates(env, error):
    env.repo.errors["rssi"] = error
    with pytest.raises(type(error)):
        env.sink.on_rssi(frame(env))
    assert env.observations == []
    env.sink.on_rssi(frame(env))
    assert len(env.observations) == 1


def test_FR05_observe_error_keeps_saved_rssi(env, caplog):
    original = env.proximity.observe
    env.proximity.observe = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("model"))
    light_calls = []
    observe = env.light.observe
    env.light.observe = lambda report: (light_calls.append(report), observe(report))[-1]
    report = frame(env)
    with pytest.raises(RuntimeError, match="model"):
        env.sink.on_rssi(report)
    assert list(env.repo.frames.values()) == light_calls == [report]
    assert "근접 관측 인계 실패" in caplog.text
    env.proximity.observe = original
    env.sink.on_rssi(report)
    assert len(env.observations) == len(light_calls) == 1


def test_FR05_failed_light_handover_retries_only_missing_consumer(env):
    original = env.light.observe
    env.light.observe = lambda report: (_ for _ in ()).throw(StoreNotApplied("beacons"))
    with pytest.raises(StoreNotApplied):
        env.sink.on_rssi(frame(env))
    env.light.observe = original
    env.sink.on_rssi(frame(env))
    assert len(env.observations) == 1


def test_FR13_rssi_touches_with_received_at(env):
    env.time[0] += timedelta(seconds=2)
    report = frame(env, frame_ts=env.clock() - timedelta(seconds=1))
    env.sink.on_rssi(report)
    assert env.repo.items["locker"].last_seen_at == report.received_at


def test_FR05_wrong_connection_does_not_update_online(env):
    before = env.repo.items["locker"]
    with pytest.raises(StoreError):
        env.sink.on_rssi(frame(env, session_id="old-connection"))
    assert env.repo.items["locker"] == before
    assert env.repo.frames == {}


def test_NFR04_sensor_fault_isolates(env):
    original = occupied(env)
    env.store.usages["request"] = "held"
    env.sink.on_status(status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True)))
    assert env.repo.items["locker"].device_state == DeviceState.FAULT
    assert env.repo.sessions["use"] == original
    assert env.store.usages == {"request": "held"}
    assert len(env.store.recoveries) == 1
    assert not env.restore.can_execute("locker")


def test_NFR04_repeat_fault_same_recovery(env):
    report = status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True))
    env.sink.on_status(report)
    env.sink.on_status(replace(report, seq=2))
    assert len(env.store.recoveries) == 1


@pytest.mark.parametrize("source", ["status", "announce"])
@pytest.mark.parametrize("error", [StoreNotApplied("save"), StoreOutcomeUnknown("save")])
def test_NFR04_retry_missing_recovery_after_isolation(env, source, error):
    occupied(env)
    report = status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True))
    method = env.sink.on_status
    if source == "announce":
        method, report = env.sink.on_announce, announce(env, capability=Capability.LIGHT)
    env.store.errors["recovery"] = error
    with pytest.raises(type(error)):
        method(report)
    assert env.repo.items["locker"].device_state == DeviceState.FAULT
    assert env.store.recoveries == {}
    first_id = next(c[1].recovery_id for c in env.store.calls if c[0] == "recovery")
    method(report)
    assert list(env.store.recoveries) == [first_id]


def test_NFR04_isolation_unknown_retries_recovery(env):
    env.store.errors["isolate_after"] = StoreOutcomeUnknown("isolation")
    report = status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True))
    with pytest.raises(StoreOutcomeUnknown):
        env.sink.on_status(report)
    env.sink.on_status(report)
    assert len(env.store.recoveries) == 1


def test_NFR04_isolation_conflict_is_not_delivered(env):
    env.store.isolate_outcome = StoreOutcome.CONFLICT
    with pytest.raises(StoreError):
        env.sink.on_status(status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True)))
    assert env.store.recoveries == {}


def test_FR19_busy_actuator_not_fault(env):
    env.sink.on_status(status(env, physical=PhysicalState(True, None, ActuatorState.BUSY, False)))
    assert env.repo.items["locker"].device_state == DeviceState.AVAILABLE
    assert env.store.recoveries == {}


def test_FR07_normal_status_does_not_finish_release(env):
    occupied(env)
    env.release.release_use("visitor", "use")
    env.sink.on_status(status(env))
    assert env.repo.sessions["use"].state == SessionState.CLOSING


def test_FR07_routes_safety_check_to_real_release(env):
    occupied(env)
    env.release.release_use("visitor", "use")
    request, = env.requests
    report = SafetyCheckResult("locker", "connection", 2, env.clock(), env.clock(),
        request.check_id, "safety-event", request.criteria, env.clock(), SafetyCheckOutcome.PASSED,
        {"door_closed": True})
    before = env.repo.items["locker"].last_seen_at
    env.sink.on_safety_check_result(report)
    env.sink.on_safety_check_result(report)
    assert env.repo.sessions["use"].state == SessionState.CLOSED
    assert env.repo.items["locker"].last_seen_at == before
    assert len(env.store.closed) == 1


def test_FR07_routes_result_to_real_execution(env):
    report = command(env)
    env.sink.on_result(report)
    env.sink.on_result(report)
    assert env.store.commands["request"].status == ExecutionStatus.SUCCEEDED
    assert len([c for c in env.store.calls if c[0] == "result"]) == 1


def test_FR13_result_does_not_touch(env):
    report = command(env)
    before = env.repo.items["locker"].last_seen_at
    env.time[0] += timedelta(seconds=10)
    env.sink.on_result(replace(report, received_at=env.clock()))
    assert env.repo.items["locker"].last_seen_at == before
    assert env.sink.latest_status("locker") is None
    assert all(c[0] not in ("touch", "connection") for c in env.repo.calls)


def test_FR07_late_success_does_not_remove_fault(env):
    report = command(env)
    ctx = env.contexts["command"]
    env.execution.on_result(ctx, replace(report, state=NodeResultState.UNKNOWN))
    env.sink.on_result(replace(report, event_id="late"))
    assert env.store.commands["request"].status == ExecutionStatus.UNKNOWN
    assert env.repo.items["locker"].device_state == DeviceState.FAULT
    assert env.store.usages["request"] == "held"


@pytest.mark.parametrize("state", [NodeResultState.FAILED, NodeResultState.UNKNOWN])
def test_FR07_failed_result_creates_recovery(env, state):
    report = command(env)
    env.sink.on_result(replace(report, state=state))
    recovery, = env.store.recoveries.values()
    assert (recovery.command_ids, recovery.use_id) == (("command",), "use")


def test_FR07_result_store_failure_propagates_and_retries(env):
    report = command(env)
    env.store.errors["result"] = StoreNotApplied("result")
    with pytest.raises(StoreNotApplied):
        env.sink.on_result(report)
    env.sink.on_result(report)
    assert env.store.commands["request"].status == ExecutionStatus.SUCCEEDED


def test_FR07_missing_original_context_is_failure(env):
    report = command(env)
    env.contexts.clear()
    with pytest.raises(StoreError, match="문맥"):
        env.sink.on_result(report)
    assert env.store.commands["request"].status == ExecutionStatus.IN_PROGRESS


@pytest.mark.parametrize("change", [{"node_id": "other"}, {"use_id": "other"}, {"usage_id": "other"}])
def test_FR07_wrong_context_blocks_result(env, change):
    report = command(env)
    env.contexts["command"] = replace(env.contexts["command"], **change)
    with pytest.raises(StoreError):
        env.sink.on_result(report)
    assert env.store.commands["request"].status == ExecutionStatus.IN_PROGRESS


def test_FR20_shared_success_and_rssi_update_real_holder(env):
    report = command(env, shared=True)
    env.sink.on_result(report)
    # 복원 확인 이후 이탈 판단을 시작한다. 성공 계정은 메모리 유지 대상자다.
    env.light.resume("locker")
    env.sink.on_status(status(env, physical=PhysicalState(None, True, ActuatorState.IDLE, False)))
    env.time[0] += timedelta(seconds=env.settings.leave_absent_sec)
    env.sink.on_rssi(frame(env))
    env.time[0] += timedelta(seconds=env.settings.leave_grace_sec)
    env.sink.on_status(status(env, seq=2, physical=PhysicalState(None, True, ActuatorState.IDLE, False)))
    assert not env.light.can_request_off("locker")


def test_NFR04_node_events_serialized(env):
    entered, release = Event(), Event()
    original = env.store.isolate_device
    def isolate(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    env.store.isolate_device = isolate
    def observe():
        with env.lock_for("locker"):
            return env.repo.items["locker"].device_state
    report = status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True))
    with ThreadPoolExecutor(max_workers=2) as pool:
        event = pool.submit(env.sink.on_status, report)
        assert entered.wait(3)
        read = pool.submit(observe)
        try:
            assert not read.done()
        finally:
            release.set()
        event.result(timeout=3)
        assert read.result(timeout=3) == DeviceState.FAULT


def test_FR05_anchor_lock_released_before_light_observation(env):
    original = env.light.observe
    def observe(report):
        acquired = Event()
        def lock_available():
            with env.lock_for(report.node_id):
                acquired.set()
        worker = Thread(target=lock_available, daemon=True)
        worker.start()
        assert acquired.wait(3)
        worker.join(timeout=3)
        original(report)
    env.light.observe = observe
    env.sink.on_rssi(frame(env))


@pytest.mark.parametrize("operation", ["connection", "touch"])
def test_FR13_status_store_error_is_not_delivered(env, operation):
    env.repo.errors[operation] = StoreNotApplied(operation)
    report = status(env)
    with pytest.raises(StoreNotApplied):
        env.sink.on_status(report)
    assert not env.restore.can_execute("locker")
    env.sink.on_status(report)
    assert env.restore.can_execute("locker")


def test_FR13_status_retries_failed_restore_handover(env):
    original = env.restore.on_status
    env.restore.on_status = lambda report: (_ for _ in ()).throw(StoreNotApplied("restore"))
    report = status(env)
    with pytest.raises(StoreNotApplied):
        env.sink.on_status(report)
    env.restore.on_status = original
    env.sink.on_status(report)
    assert env.restore.can_execute("locker")


def test_FR04_announce_write_failure_keeps_changed_occupied_node_isolated(env):
    old_use = occupied(env)
    env.repo.errors["announce"] = StoreNotApplied("announce")
    report = announce(env, capability=Capability.LIGHT)
    with pytest.raises(StoreNotApplied):
        env.sink.on_announce(report)
    assert env.repo.items["locker"].device_state == DeviceState.FAULT
    # 새 수신 서비스도 DB의 격리와 복구 건을 보존한다.
    env.create().on_announce(report)
    assert env.repo.sessions["use"] == old_use
    assert len(env.store.recoveries) == 1
    assert env.repo.items["locker"].trust_state == TrustState.DISCOVERED


def test_NFR04_restarted_sink_repairs_missing_recovery(env):
    occupied(env)
    env.store.errors["recovery"] = StoreNotApplied("recovery")
    report = status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True))
    with pytest.raises(StoreNotApplied):
        env.sink.on_status(report)
    fresh_recovery = FaultRecoveryService(repository=env.repo, store=env.store,
        clock=env.clock, lock_for=env.lock_for)
    fresh_restore = RestoreService(repository=env.repo, store=env.store,
        release=env.release, recovery=fresh_recovery, shared_light=env.light,
        settings=lambda: env.settings, clock=env.clock, lock_for=env.lock_for)
    fresh_restore.restore()
    sink = env.create()
    sink._recovery = fresh_recovery
    sink._restore = fresh_restore
    sink.on_status(report)
    recovery, = env.store.recoveries.values()
    assert (recovery.use_id, recovery.internal_request_ids) == ("use", ("request",))


def test_FR07_shared_result_retries_failed_holder_handover(env):
    report = command(env, shared=True)
    original = env.light.on_execution_result
    env.light.on_execution_result = lambda *args: (_ for _ in ()).throw(StoreNotApplied("policy"))
    with pytest.raises(StoreNotApplied):
        env.sink.on_result(report)
    env.light.on_execution_result = original
    env.sink.on_result(report)
    assert len([c for c in env.store.calls if c[0] == "result"]) == 1


@pytest.mark.parametrize("source", ["status", "announce"])
def test_NFR04_link_query_failure_still_isolates_known_fault(env, source):
    occupied(env)
    env.store.errors["restore"] = StoreNotApplied("links")
    report = status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True))
    method = env.sink.on_status
    if source == "announce":
        method, report = env.sink.on_announce, announce(env, capability=Capability.LIGHT)
    with pytest.raises(StoreNotApplied):
        method(report)
    assert env.repo.items["locker"].device_state == DeviceState.FAULT
    method(report)
    assert len(env.store.recoveries) == 1


def test_NFR04_online_write_failure_does_not_skip_sensor_isolation(env):
    env.repo.errors["touch"] = StoreNotApplied("online")
    report = status(env, physical=PhysicalState(None, None, ActuatorState.IDLE, True))
    with pytest.raises(StoreNotApplied):
        env.sink.on_status(report)
    assert env.repo.items["locker"].device_state == DeviceState.FAULT
    assert len(env.store.recoveries) == 1
    env.sink.on_status(report)
    assert len(env.store.recoveries) == 1
    assert env.repo.items["locker"].last_seen_at == report.received_at
    assert env.sink.latest_status("locker") == report


def test_FR07_reports_during_gateway_wait_do_not_deadlock(env):
    report = command(env)
    ctx = env.contexts["command"]
    env.store.commands["request"] = replace(env.store.commands["request"],
        status=ExecutionStatus.PENDING, send_started_at=None, send_attempted=False)
    env.repo.items["locker"] = replace(env.repo.items["locker"], device_state=DeviceState.AVAILABLE)
    env.sink.on_status(status(env))
    env.repo.items["locker"] = replace(env.repo.items["locker"], device_state=DeviceState.RESERVED)
    env.execution._rechecker = SimpleNamespace(recheck=lambda context: Decision(
        context.internal_request_id, context.request.request_id, Verdict.OK, ReasonCode.OK,
        node_id=context.node_id))
    def begin_send(command_id):
        current = env.store.commands["request"]
        current = replace(current, status=ExecutionStatus.IN_PROGRESS,
            send_started_at=env.clock(), send_attempted=True)
        env.store.commands["request"] = current
        return StoreResult(StoreOutcome.APPLIED, command=current)
    env.store.begin_send = begin_send
    with ThreadPoolExecutor(max_workers=1) as pool:
        def send(node_command, timeout_sec):
            pool.submit(env.sink.on_result, report).result(timeout=3)
            return CommandResult(node_command.command_id, CommandOutcome.SUCCEEDED, report)
        env.execution._gateway = SimpleNamespace(send=send)
        result = env.execution.execute(ctx)
    assert result.status == ExecutionStatus.SUCCEEDED
    assert len([c for c in env.store.calls if c[0] == "result"]) == 1


def test_NFR04_recovery_check_and_status_routed_to_recovery(env):
    handled, statuses, released = [], [], []
    recovery = SimpleNamespace(
        on_check_result=lambda result: handled.append(result) or result.check_id == "recovery-check",
        on_status=statuses.append, record_isolated=env.recovery.record_isolated)
    release = SimpleNamespace(on_safety_check_result=released.append)
    sink = NodeEventService(repository=env.repo, store=env.store, proximity=env.proximity,
        execution=env.execution, release=release, restore=env.restore, recovery=recovery,
        shared_light=env.light, context_for=env.contexts.get, lock_for=env.lock_for)
    at = env.clock()
    report = SafetyCheckResult("locker", "connection", 2, at, at, "recovery-check", "event",
        CheckCriteria("locker-return", 1), at, SafetyCheckOutcome.PASSED, {})
    sink.on_safety_check_result(report)
    assert (handled, released) == ([report], [])
    other = replace(report, check_id="release-check")
    sink.on_safety_check_result(other)
    assert released == [other]
    sink.on_status(status(env))
    assert len(statuses) == 1
