"""공용 조명의 이탈 시나리오. 실제 MQTT 전송은 검증 범위 밖이다.

켜짐 여부는 저장한 마지막 성공 명령으로 판단한다. status는 명령 처리 상태만 담는다 (2026-10-26 결정).
"""

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Lock, RLock
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.shared_light import SharedHolder, SharedLightService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, CommandState, Capability, DeviceState, ExecutionKind, ExecutionStatus,
    InternalRequestCause, Occupancy, ReasonCode, TrustState, Verdict,
)
from nearpilot.shared.errors import StoreError
from nearpilot.shared.models import (
    BeaconKey, CommandRecord, ExecutionContext, ExecutionResult, NodeInfo, NodeStatus, PolicyCheckResult, RssiFrame,
    LightSetting, SpeakerSetting, TargetSpec, UseRequest,
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


class StoreFake:
    """`ExecutionStore.last_succeeded` 만 쓴다. 기본값은 재시작 전에 켜기에 성공한 조명이다."""

    def __init__(self):
        self.last = {"light-1": Action.ON}

    def last_succeeded(self, node_id):
        action = self.last.get(node_id)
        if action is None:
            return None
        return CommandRecord(f"last-{node_id}", f"request-last-{node_id}", node_id, action,
                             ExecutionStatus.SUCCEEDED, ExecutionKind.SHARED)


@pytest.fixture
def env():
    time = [datetime(2026, 10, 15, tzinfo=timezone.utc)]
    settings = [Settings()]
    repository = RepositoryFake(time[0])
    policy = PolicyFake()
    store = StoreFake()
    locks, index_lock = {}, Lock()

    def lock_for(node_id):
        with index_lock:
            return locks.setdefault(node_id, RLock())

    def new_service():
        return SharedLightService(repository=repository, store=store, policy=policy,
                                  settings=lambda: settings[0], clock=lambda: time[0], lock_for=lock_for)

    result = SimpleNamespace(time=time, settings=settings, repository=repository, store=store,
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
                        CommandState.IDLE)
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
    if result.status == ExecutionStatus.SUCCEEDED:
        env.store.last[node_id] = action  # 실제 DB는 성공 명령을 저장한다.
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


def test_FR20_brightness_zero_on_success_adds_holder(env):
    """밝기 0의 on 도 켜기 요청이다. 성공하면 유지 대상자가 된다 (FR-09)."""
    now = env.time[0]
    request = UseRequest("visitor-a", "request-dim", TargetSpec.parse("light-1"), Action.ON, "host", now,
                         light=LightSetting(0, None))
    context = ExecutionContext("request-dim", request, "light-1", None, None, env.settings[0],
                               ExecutionKind.SHARED, light=LightSetting(0, "#FFFFFF"))
    env.store.last["light-1"] = Action.ON
    env.service.on_execution_result(
        context, ExecutionResult("request-dim", ExecutionStatus.SUCCEEDED, "command-dim"))
    advance(env, 14)
    observe(env)
    advance(env, 1)
    assert candidates(env) == ()


def test_FR20_zero_volume_on_is_on(env):
    """음량 0의 on 도 켜기 요청이다. 성공하면 유지 대상자가 되고, 이탈하면 꺼짐 후보다 (FR-09)."""
    # 이 파일의 보조 함수는 light-1 만 다룬다. 공용형 기기를 speaker-1 로 바꾸고 상태 보고는 직접 보낸다.
    env.repository.light = replace(env.repository.light, node_id="speaker-1", capability=Capability.SPEAKER)
    env.service.resume("speaker-1")
    now = env.time[0]
    request = UseRequest("visitor-a", "request-quiet", TargetSpec.parse("speaker-1"), Action.ON, "host", now,
                         speaker=SpeakerSetting(0))
    context = ExecutionContext("request-quiet", request, "speaker-1", None, None, env.settings[0],
                               ExecutionKind.SHARED, speaker=SpeakerSetting(0))
    env.store.last["speaker-1"] = Action.ON
    env.service.on_execution_result(
        context, ExecutionResult("request-quiet", ExecutionStatus.SUCCEEDED, "command-quiet"))

    def tick():
        env.seq += 1
        speaker = env.repository.light = replace(env.repository.light, last_seen_at=env.time[0])
        env.service.on_status(NodeStatus("speaker-1", env.time[0], speaker.session_id, env.seq,
                                         env.time[0], env.time[0], CommandState.IDLE))
        return env.service.tick()

    advance(env, 14)
    observe(env)
    advance(env, 1)
    assert tick() == ()
    advance(env, 14)
    assert tick() == ("speaker-1",)


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


@pytest.mark.parametrize("command_state", [CommandState.BUSY, CommandState.UNKNOWN])
def test_FR20_restart_requires_idle_command_state(env, command_state):
    advance(env, 15)
    status(env, command_state=command_state)
    assert env.service.tick() == ()


@pytest.mark.parametrize("last", [None, Action.OFF])
def test_FR20_off_requires_last_control_on(env, last):
    """기록이 없거나 마지막 성공이 꺼짐이면 실제 켜짐을 추정해서 꺼짐을 보내지 않는다."""
    env.store.last["light-1"] = last
    advance(env, 15)
    assert candidates(env) == ()


def test_FR20_status_has_no_light_observation(env):
    assert not hasattr(status(env), "physical")
    advance(env, 15)
    assert candidates(env) == ("light-1",)


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
    assert env.service.holders() == []
    # 마지막 제어 상태가 꺼짐이므로 꺼짐을 다시 요청하지 않는다.
    assert candidates(env) == ()


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
    assert env.service.holders() == []
    assert candidates(env) == ()  # 마지막 성공은 꺼짐이다.


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
    old_idle = status(env)
    advance(env, 1)
    status(env, command_state=CommandState.BUSY)
    advance(env, 1)
    env.service.on_status(replace(old_idle, received_at=env.time[0]))
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
        status = outcome[0] if isinstance(outcome, list) else outcome
        if status == ExecutionStatus.SUCCEEDED:
            env.store.last[ctx.node_id] = Action.OFF  # 실제 DB는 성공한 꺼짐을 저장한다.
        return ExecutionResult(ctx.internal_request_id, status, "command-" + ctx.internal_request_id)
    ids = iter(f"internal-{n}" for n in range(1, 100))
    service = SharedLightService(repository=env.repository, store=env.store, policy=env.policy,
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


def test_FR20_shared_speaker_follows_light_rules(env):
    """공용형 스피커의 on은 재생, off는 정지다. fixture의 `light-1`을 스피커로 바꿔 조명과 같은 규칙을 확인한다."""
    env.repository.light = replace(env.repository.light, capability=Capability.SPEAKER)
    calls = active(env)
    execution(env)
    assert [holder.user_id for holder in env.service.holders()] == ["visitor-a"]
    advance(env, 14)
    assert candidates(env) == ()
    assert calls == []
    advance(env, 1)
    assert candidates(env) == ("light-1",)
    ctx = calls[0]
    assert ctx.execution_kind == ExecutionKind.SERVER_INTERNAL
    assert (ctx.internal_request.node_id, ctx.internal_request.action) == ("light-1", Action.OFF)
    assert ctx.internal_request.cause == InternalRequestCause.LEAVE_OFF
    # 명시적 off는 다른 유지 대상자가 남아 있어도 유지 대상자를 비운다.
    execution(env, "visitor-a", "request-2")
    execution(env, "visitor-b", "request-3")
    execution(env, request_id="request-off", action=Action.OFF)
    assert env.service.holders() == []
    advance(env, 15)
    assert candidates(env) == ()
    assert len(calls) == 1
    # 대여형 스피커와 사물함은 대상이 아니다.
    for capability in (Capability.SPEAKER, Capability.LOCKER):
        env.repository.light = replace(env.repository.light, capability=capability,
                                       occupancy=Occupancy.RENTAL)
        env.service = env.new_service()
        execution(env)
        advance(env, 15)
        assert env.service.holders() == []
        assert candidates(env) == ()


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
        def link_command(self, internal_request_id, node_id, action, light=None, speaker=None):
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
        def last_succeeded(self, node_id):
            done = [c for c in self.commands.values() if c.status == ExecutionStatus.SUCCEEDED]
            return done[-1] if done else env.store.last_succeeded(node_id)

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
    light = SharedLightService(repository=env.repository, store=store, policy=env.policy,
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


def test_FR20_유지_대상자_읽기(env):
    second = replace(env.repository.light, node_id="light-0", session_id="connection-0")
    env.repository.nodes = lambda capability=None: (
        env.repository.light, second, env.repository.anchor, env.repository.other_anchor,
    )
    start = env.time[0]
    assert env.service.holders() == []

    execution(env, "visitor-b", "request-1")
    advance(env, 3)
    execution(env, "visitor-a", "request-2")
    execution(env, "visitor-a", "request-3", node_id="light-0")
    joined = start + timedelta(seconds=3)

    # 넣은 순서가 아니라 node_id, user_id 순이다
    assert env.service.holders() == [
        SharedHolder("light-0", "visitor-a", joined),
        SharedHolder("light-1", "visitor-a", joined),
        SharedHolder("light-1", "visitor-b", start),
    ]

    # 같은 장소에서 비콘이 다시 보이면 그 사람의 수신 시각만 바뀐다
    advance(env, 9)
    observe(env, "visitor-b")
    seen = start + timedelta(seconds=12)
    assert env.service.holders() == [
        SharedHolder("light-0", "visitor-a", joined),
        SharedHolder("light-1", "visitor-a", joined),
        SharedHolder("light-1", "visitor-b", seen),
    ]

    # 이탈한 사람은 tick 뒤에 사라진다 (visitor-a 만 유예가 끝났다)
    advance(env, 8)
    assert env.service.tick() == ()
    assert env.service.holders() == [SharedHolder("light-1", "visitor-b", seen)]

    # 꺼짐 성공은 그 기기의 유지 대상자를 모두 지운다
    execution(env, "visitor-a", "request-4", action=Action.OFF)
    assert env.service.holders() == []


def test_FR20_유지_대상자_읽기는_기기_잠금_안에서_복사한다(env):
    execution(env)
    held, release = Event(), Event()

    def hold_lock():
        with env.lock_for("light-1"):
            held.set()
            release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lock)
        assert held.wait(5)
        reader = executor.submit(env.service.holders)
        done, _ = wait([reader], timeout=0.2)
        assert not done  # 기기의 잠금이 풀릴 때까지 읽지 못한다
        release.set()
        assert [row.user_id for row in reader.result(5)] == ["visitor-a"]
        holder.result(5)
