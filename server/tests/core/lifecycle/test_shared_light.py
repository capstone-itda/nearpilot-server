"""공용 조명의 이탈 시나리오. 실제 MQTT 전송은 검증 범위 밖이다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock, RLock
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, ActuatorState, Capability, DeviceState, ExecutionKind, ExecutionStatus,
    InternalRequestCause, Occupancy, ReasonCode, TrustState, Verdict,
)
from nearpilot.shared.errors import StoreError
from nearpilot.shared.models import (
    BeaconKey, ExecutionContext, ExecutionResult, NodeInfo, NodeStatus, PhysicalState,
    PolicyCheckResult, RssiFrame, TargetSpec, UseRequest,
)


class RepositoryFake:
    def __init__(self, at):
        self.light = NodeInfo(
            "light-1", "place-1", Capability.LIGHT, Occupancy.SHARED,
            TrustState.TRUSTED, DeviceState.AVAILABLE, None, at, session_id="connection-1",
        )
        self.anchor = replace(self.light, node_id="locker-1", capability=Capability.LOCKER,
                              occupancy=Occupancy.RENTAL, session_id="anchor-connection")
        self.other_anchor = replace(self.anchor, node_id="locker-2", place_id="place-2")
        self.beacons = {"visitor-a": (BeaconKey("uuid-a", 1, 1),),
                        "visitor-b": (BeaconKey("uuid-b", 1, 1),)}
        self.beacon_error = None
        self.nodes_error = None

    def nodes(self, capability=None):
        if self.nodes_error:
            raise self.nodes_error
        return (self.light, self.anchor, self.other_anchor)

    def beacons_of(self, user_id):
        if self.beacon_error:
            raise self.beacon_error
        return self.beacons.get(user_id, ())


class PolicyFake:
    def __init__(self):
        self.revoked = set()
        self.error_user = None
        self.calls = []

    def check(self, user_id, node_id, action, now):
        self.calls.append((user_id, node_id, action, now))
        if user_id == self.error_user:
            raise StoreError("정책 조회 실패")
        return PolicyCheckResult(
            Verdict.NEED_APPROVAL if user_id in self.revoked else Verdict.OK,
            ReasonCode.APPROVAL_EXPIRED if user_id in self.revoked else ReasonCode.OK, 1,
        )


@pytest.fixture
def env():
    time = [datetime(2026, 10, 15, tzinfo=timezone.utc)]
    settings = [Settings()]
    repository = RepositoryFake(time[0])
    policy = PolicyFake()
    locks, index_lock = {}, Lock()

    def lock_for(node_id):
        with index_lock:
            return locks.setdefault(node_id, RLock())

    def new_service():
        return SharedLightService(repository=repository, policy=policy,
                                  settings=lambda: settings[0], clock=lambda: time[0], lock_for=lock_for)

    result = SimpleNamespace(time=time, settings=settings, repository=repository,
                             policy=policy, new_service=new_service, lock_for=lock_for, seq=0)
    result.service = new_service()
    result.service.resume("light-1")
    return result


def advance(env, seconds):
    env.time[0] += timedelta(seconds=seconds)


def status(env, **changes):
    env.seq += 1
    now = env.time[0]
    env.repository.light = replace(env.repository.light, last_seen_at=now)
    report = NodeStatus("light-1", now, env.repository.light.session_id, env.seq, now, now,
                        PhysicalState(None, True, ActuatorState.IDLE, False))
    report = replace(report, **changes)
    env.service.on_status(report)
    return report


def execution(env, user="visitor-a", request_id="request-1", action=Action.ON,
              node_id="light-1", **changes):
    now = env.time[0]
    request = UseRequest(user, request_id, TargetSpec.parse(node_id), action, "host", now)
    context = ExecutionContext(request_id, request, node_id, None, None, env.settings[0],
                               ExecutionKind.SHARED)
    result = ExecutionResult(request_id, ExecutionStatus.SUCCEEDED, "command-" + request_id)
    result = replace(result, **changes)
    env.service.on_execution_result(context, result)
    return context, result


def observe(env, user="visitor-a", anchor="locker-1", **changes):
    now = env.time[0]
    beacon = env.repository.beacons[user][0]
    frame = RssiFrame(anchor, beacon, -50, now, "anchor-connection", 1, now, now)
    frame = replace(frame, **changes)
    env.service.observe(frame)
    return frame


def candidates(env):
    status(env)
    return env.service.tick()


def test_FR20_on_success_adds_holder(env):
    execution(env)
    advance(env, 14)
    observe(env)
    advance(env, 1)
    assert candidates(env) == ()


@pytest.mark.parametrize("failed", [ExecutionStatus.PENDING, ExecutionStatus.IN_PROGRESS,
                                   ExecutionStatus.FAILED, ExecutionStatus.UNKNOWN,
                                   ExecutionStatus.NOT_SENT, ExecutionStatus.REJECTED])
def test_FR20_only_success_adds_holder(env, failed):
    execution(env, status=failed)
    advance(env, 15)
    assert candidates(env) == ("light-1",)


def test_FR20_light_stays_on_while_holder_remains(env):
    execution(env)
    execution(env, "visitor-b", "request-2")
    advance(env, 14)
    observe(env, "visitor-b")
    advance(env, 1)
    assert candidates(env) == ()
    advance(env, 14)
    assert candidates(env) == ("light-1",)


def test_FR20_last_holder_leave_off_candidate(env):
    execution(env)
    advance(env, 10)
    assert candidates(env) == ()
    advance(env, 4.999)
    assert candidates(env) == ()
    advance(env, .001)
    assert candidates(env) == ("light-1",)


def test_NFR12_signal_loss_within_grace_keeps_holder(env):
    execution(env)
    advance(env, 14)
    observe(env)
    advance(env, 1)
    assert candidates(env) == ()
    advance(env, 13.999)
    assert candidates(env) == ()
    advance(env, .001)
    assert candidates(env) == ("light-1",)


def test_FR20_same_place_anchor_resets_timer(env):
    execution(env)
    advance(env, 14)
    observe(env, anchor="locker-1")
    advance(env, 1)
    assert candidates(env) == ()


@pytest.mark.parametrize("changes", [{"node_id": "locker-2"}, {"session_id": "old-session"},
                                    {"beacon": BeaconKey("uuid-b", 1, 1)}])
def test_FR20_other_place_account_connection_does_not_reset(env, changes):
    execution(env)
    advance(env, 14)
    observe(env, **changes)
    advance(env, 1)
    assert candidates(env) == ("light-1",)


def test_FR20_past_retransmission_does_not_reset(env):
    execution(env)
    advance(env, 1)
    first = observe(env)
    advance(env, 13)
    env.service.observe(replace(first, received_at=env.time[0], seq=99))
    advance(env, 2)
    assert candidates(env) == ("light-1",)


def test_FR20_absence_uses_received_time(env):
    execution(env)
    advance(env, 5)
    observe(env, frame_ts=env.time[0] - timedelta(seconds=2))
    advance(env, 14.999)
    assert candidates(env) == ()
    advance(env, .001)
    assert candidates(env) == ("light-1",)


def test_FR20_inactive_beacon_does_not_reset(env):
    execution(env)
    old_beacon = env.repository.beacons["visitor-a"][0]
    env.repository.beacons["visitor-a"] = ()
    advance(env, 14)
    frame = RssiFrame("locker-1", old_beacon, -50, env.time[0], "anchor-connection", 1,
                      env.time[0], env.time[0])
    env.service.observe(frame)
    advance(env, 1)
    assert candidates(env) == ("light-1",)


def test_FR20_recheck_holders_before_off(env):
    execution(env)
    advance(env, 15)
    assert candidates(env) == ("light-1",)
    execution(env, "visitor-b", "request-2")
    assert not env.service.can_request_off("light-1")


def test_FR20_expired_permission_removes_holder(env):
    execution(env)
    advance(env, 15)
    observe(env)
    env.policy.revoked.add("visitor-a")
    assert candidates(env) == ("light-1",)


def test_FR20_no_time_based_off(env):
    execution(env)
    for _ in range(100):
        advance(env, 9)
        observe(env)
        assert candidates(env) == ()


def test_FR20_restart_waits_initial_period(env):
    advance(env, 3600)
    env.service = env.new_service()
    assert candidates(env) == ()
    env.service.resume("light-1")
    assert candidates(env) == ()
    advance(env, 14.999)
    assert candidates(env) == ()
    advance(env, .001)
    assert candidates(env) == ("light-1",)


def test_FR20_holders_not_restored_after_restart(env):
    execution(env)
    advance(env, 100)
    env.service = env.new_service()
    env.service.resume("light-1")
    observe(env)
    advance(env, 15)
    assert candidates(env) == ("light-1",)


def test_FR20_resume_duplicate_does_not_extend_wait(env):
    advance(env, 14)
    env.service.resume("light-1")
    advance(env, 1)
    assert candidates(env) == ("light-1",)


@pytest.mark.parametrize("physical", [PhysicalState(None, None, ActuatorState.IDLE, False),
                                      PhysicalState(None, False, ActuatorState.IDLE, False),
                                      PhysicalState(None, True, ActuatorState.IDLE, True),
                                      PhysicalState(None, True, ActuatorState.IDLE, None)])
def test_FR20_restart_requires_confirmed_normal_light(env, physical):
    advance(env, 15)
    status(env, physical=physical)
    assert env.service.tick() == ()


@pytest.mark.parametrize("changes", [{"device_state": DeviceState.FAULT},
                                    {"trust_state": TrustState.REVOKED},
                                    {"session_id": "new-connection"},
                                    {"last_seen_at": None}])
def test_FR20_unavailable_light_not_candidate(env, changes):
    advance(env, 15)
    status(env)
    env.repository.light = replace(env.repository.light, **changes)
    assert env.service.tick() == ()


def test_FR20_offline_or_stale_status_not_candidate(env):
    status(env)
    advance(env, 16)
    assert env.service.tick() == ()
    env.repository.light = replace(env.repository.light, last_seen_at=env.time[0])
    assert env.service.tick() == ()
    status(env)
    assert env.service.tick() == ("light-1",)


def test_FR20_restart_unconfirmed_light_not_off(env):
    advance(env, 15)
    assert env.service.tick() == ()


def test_FR20_explicit_off_clears_holders(env):
    execution(env)
    execution(env, "visitor-b", "request-2")
    execution(env, request_id="request-off", action=Action.OFF)
    advance(env, 15)
    # 새 상태 보고가 켜짐을 다시 확인한 경우에만 후보를 만든다.
    assert candidates(env) == ("light-1",)


def test_FR20_off_success_invalidates_previous_status(env):
    advance(env, 15)
    old = status(env)
    assert env.service.tick() == ("light-1",)
    execution(env, request_id="request-off", action=Action.OFF)
    assert env.service.tick() == ()
    advance(env, 1)
    env.service.on_status(replace(old, received_at=env.time[0]))
    assert env.service.tick() == ()


def test_FR20_duplicate_success_does_not_reset_timer(env):
    context, result = execution(env)
    advance(env, 14)
    env.service.on_execution_result(context, result)
    advance(env, 1)
    assert candidates(env) == ("light-1",)


def test_FR20_duplicate_on_after_off_does_not_restore_holder(env):
    context, result = execution(env)
    execution(env, request_id="off-1", action=Action.OFF)
    advance(env, 15)
    env.service.on_execution_result(context, result)
    assert candidates(env) == ("light-1",)


def test_FR20_duplicate_off_does_not_remove_new_holder(env):
    context, result = execution(env, request_id="off-1", action=Action.OFF)
    advance(env, 15)
    execution(env, "visitor-b", "request-2")
    env.service.on_execution_result(context, result)
    assert candidates(env) == ()


def test_FR20_query_failure_keeps_holders(env):
    execution(env)
    execution(env, "visitor-b", "request-2")
    advance(env, 14)
    observe(env)
    observe(env, "visitor-b")
    advance(env, 1)
    env.policy.revoked.add("visitor-a")
    env.policy.error_user = "visitor-b"
    with pytest.raises(StoreError):
        candidates(env)
    env.policy.revoked.clear()
    env.policy.error_user = None
    # A를 먼저 지웠다면 B만 만료되는 시점에 잘못 후보를 만든다.
    advance(env, 13)
    observe(env)
    advance(env, 1)
    assert candidates(env) == ()


def test_FR20_observation_failure_propagates(env):
    execution(env)
    env.repository.beacon_error = StoreError("비콘 조회 실패")
    with pytest.raises(StoreError):
        observe(env)
    env.repository.beacon_error = None
    advance(env, 14)
    assert candidates(env) == ()


def test_FR20_settings_control_leave_deadline(env):
    env.settings[0] = replace(env.settings[0], leave_absent_sec=20, leave_grace_sec=8)
    execution(env)
    advance(env, 27.999)
    assert candidates(env) == ()
    advance(env, .001)
    assert candidates(env) == ("light-1",)


def test_FR20_rental_light_does_not_use_holders(env):
    env.repository.light = replace(env.repository.light, occupancy=Occupancy.RENTAL)
    execution(env)
    advance(env, 15)
    assert candidates(env) == ()


def test_FR20_concurrent_success_is_idempotent(env):
    context, result = execution(env)
    advance(env, 14)
    barrier = Barrier(4)

    def handover():
        barrier.wait()
        env.service.on_execution_result(context, result)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: handover(), range(4)))
    advance(env, 1)
    assert candidates(env) == ("light-1",)


def test_FR20_observation_after_grace_does_not_rejoin(env):
    execution(env)
    advance(env, 16)
    observe(env)
    assert candidates(env) == ("light-1",)


def test_FR20_nodes_query_failure_keeps_holders(env):
    execution(env)
    advance(env, 14)
    env.policy.revoked.add("visitor-a")
    env.repository.nodes_error = StoreError("기기 조회 실패")
    with pytest.raises(StoreError):
        env.service.tick()
    env.repository.nodes_error = None
    env.policy.revoked.clear()
    observe(env)
    advance(env, 1)
    assert candidates(env) == ()


def test_FR20_changed_success_handover_rejected(env):
    context, result = execution(env)
    with pytest.raises(ValueError):
        env.service.on_execution_result(context, replace(result, command_id="other-command"))


def test_FR20_result_link_mismatch_rejected(env):
    context, result = execution(env)
    with pytest.raises(ValueError):
        env.service.on_execution_result(context, replace(result, internal_request_id="other-request"))


def test_FR20_unknown_node_cannot_request_off(env):
    assert not env.service.can_request_off("unknown")


def test_FR20_past_status_does_not_replace_newer_off(env):
    advance(env, 15)
    old_on = status(env)
    advance(env, 1)
    status(env, physical=PhysicalState(None, False, ActuatorState.IDLE, False))
    advance(env, 1)
    env.service.on_status(replace(old_on, received_at=env.time[0]))
    assert env.service.tick() == ()


def test_FR20_reconnect_requires_resume_and_new_status(env):
    advance(env, 15)
    status(env)
    env.repository.light = replace(env.repository.light, session_id="connection-2")
    status(env)
    assert env.service.tick() == ()
    env.service.resume("light-1")
    assert env.service.tick() == ()
    advance(env, 15)
    assert candidates(env) == ("light-1",)


def test_FR20_multiple_lights_keep_independent_holders(env):
    execution(env)
    second = replace(env.repository.light, node_id="light-2", place_id="place-2")
    original_nodes = env.repository.nodes
    env.repository.nodes = lambda capability=None: (*original_nodes(capability), second)
    env.service.resume("light-2")
    execution(env, "visitor-b", "request-2", node_id="light-2")
    advance(env, 14)
    observe(env, "visitor-b", anchor="locker-2")
    advance(env, 1)
    second = replace(second, last_seen_at=env.time[0])
    report = status(env)
    env.service.on_status(replace(report, node_id="light-2"))
    assert env.service.tick() == ("light-1",)


def test_FR20_existing_holder_keeps_settings_snapshot(env):
    execution(env)
    env.settings[0] = replace(env.settings[0], leave_absent_sec=30, leave_grace_sec=10)
    advance(env, 15)
    assert candidates(env) == ("light-1",)


def active(env, outcome=ExecutionStatus.SUCCEEDED):
    """실행 함수를 주입한 서비스다. 실행 서비스처럼 전송 직전에 can_request_off를 확인한다."""
    calls = []
    def execute(ctx):
        calls.append(ctx)
        if not service.can_request_off(ctx.node_id):
            return ExecutionResult(ctx.internal_request_id, ExecutionStatus.NOT_SENT)
        return ExecutionResult(ctx.internal_request_id, outcome[0] if isinstance(outcome, list) else outcome,
                               "command-" + ctx.internal_request_id)
    ids = iter(f"internal-{n}" for n in range(1, 100))
    service = SharedLightService(repository=env.repository, policy=env.policy,
        settings=lambda: env.settings[0], clock=lambda: env.time[0], lock_for=env.lock_for,
        execute=execute, new_id=lambda: next(ids))
    service.resume("light-1")
    env.service = service
    return calls


def test_FR20_last_holder_leave_requests_off(env):
    calls = active(env)
    execution(env)
    advance(env, 14)
    assert candidates(env) == ()
    assert calls == []
    advance(env, 1)
    assert candidates(env) == ("light-1",)
    ctx = calls[0]
    assert ctx.execution_kind == ExecutionKind.SERVER_INTERNAL
    assert ctx.request is None and ctx.use_id is None and ctx.usage_id is None
    assert (ctx.internal_request.node_id, ctx.internal_request.action) == ("light-1", Action.OFF)
    assert ctx.internal_request.cause == InternalRequestCause.LEAVE_OFF
    assert ctx.internal_request.internal_request_id == ctx.internal_request_id == "internal-1"


def test_FR20_pending_off_not_duplicated(env):
    outcome = [ExecutionStatus.IN_PROGRESS]
    calls = active(env, outcome)
    execution(env)
    advance(env, 15)
    assert candidates(env) == ("light-1",)
    advance(env, 1)
    assert candidates(env) == ("light-1",)
    assert [c.internal_request_id for c in calls] == ["internal-1", "internal-1"]
    outcome[0] = ExecutionStatus.UNKNOWN
    env.service.tick()
    outcome[0] = ExecutionStatus.SUCCEEDED
    advance(env, 1)
    candidates(env)
    assert [c.internal_request_id for c in calls] == ["internal-1"] * 3 + ["internal-2"]


def test_FR20_internal_off_success_clears_light(env):
    calls = active(env)
    execution(env)
    advance(env, 15)
    assert candidates(env) == ("light-1",)
    assert env.service.tick() == ()
    assert len(calls) == 1
    execution(env, user="visitor-b", request_id="request-2")
    advance(env, 1)
    assert env.service.tick() == ()


def test_FR20_exhausted_approval_keeps_holder(env):
    original = env.policy.check
    def check(user_id, node_id, action, now):
        if user_id == "visitor-a":
            return PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, 1)
        return original(user_id, node_id, action, now)
    env.policy.check = check
    execution(env)
    advance(env, 14)
    observe(env)
    advance(env, 1)
    assert candidates(env) == ()


def test_FR20_internal_off_through_real_execution(env):
    from nearpilot.core.lifecycle.execution import ExecutionService
    from nearpilot.shared.enums import CommandOutcome, NodeResultState, StoreOutcome
    from nearpilot.shared.models import CommandRecord, CommandResult, NodeResult, StoreResult

    class Store:
        def __init__(self):
            self.commands, self.internal, self.calls = {}, [], []
        def execution(self, internal_request_id):
            return self.commands.get(internal_request_id)
        def link_internal_request(self, req):
            self.internal.append(req)
            return StoreResult(StoreOutcome.APPLIED)
        def link_command(self, internal_request_id, node_id, action):
            req = next(r for r in self.internal if r.internal_request_id == internal_request_id)
            record = CommandRecord("command-1", internal_request_id, node_id, action,
                                   ExecutionStatus.PENDING, ExecutionKind.SERVER_INTERNAL)
            self.commands[internal_request_id] = record
            return StoreResult(StoreOutcome.APPLIED, command=record)
        def begin_send(self, command_id):
            # 전송 시작 직전에 새 켜기 성공이 끼어들 수 없음을 확인한다.
            assert not service_holders()
            record = replace(self.commands["internal-1"], status=ExecutionStatus.IN_PROGRESS)
            self.commands["internal-1"] = record
            return StoreResult(StoreOutcome.APPLIED, command=record)
        def record_result(self, command_id, outcome, occurred_at, rental_due_at, evidence):
            record = replace(self.commands["internal-1"], status=ExecutionStatus(outcome.value),
                             outcome=outcome, occurred_at=occurred_at, evidence=evidence)
            self.commands["internal-1"] = record
            return StoreResult(StoreOutcome.APPLIED, command=record)

    class Gateway:
        def __init__(self):
            self.sent = []
        def send(self, command, timeout):
            self.sent.append(command)
            now = env.time[0]
            report = NodeResult(command.node_id, command.target_session_id, 99, now, now,
                                command.command_id, "event-off", NodeResultState.SUCCEEDED, now)
            return CommandResult(command.command_id, CommandOutcome.SUCCEEDED, report)

    store, gateway = Store(), Gateway()
    holder = {}
    service_holders = lambda: holder["light"]._lights["light-1"].holders
    executor = ExecutionService(repository=env.repository, store=store, rechecker=None, gateway=gateway,
        clock=lambda: env.time[0], lock_for=env.lock_for, can_execute=lambda node_id: True,
        record_isolated=None, confirm_internal=lambda ctx: light.can_request_off(ctx.node_id))
    light = SharedLightService(repository=env.repository, policy=env.policy,
        settings=lambda: env.settings[0], clock=lambda: env.time[0], lock_for=env.lock_for,
        execute=executor.execute, new_id=lambda: "internal-1")
    holder["light"] = light
    light.resume("light-1")
    env.service = light
    execution(env)
    advance(env, 15)
    assert candidates(env) == ("light-1",)
    assert [c.action for c in gateway.sent] == [Action.OFF]
    assert store.internal[0].cause == InternalRequestCause.LEAVE_OFF
    assert store.commands["internal-1"].status == ExecutionStatus.SUCCEEDED
    assert light.tick() == ()
