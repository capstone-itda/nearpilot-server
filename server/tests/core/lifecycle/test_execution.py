"""실행 인계의 경합·저장 장애·늦은 보고. 다른 계층은 shared 계약의 fake다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.execution import ExecutionService
from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.core.lifecycle.restore import RestoreService
from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, CommandState, Capability, CommandOutcome, DeviceState, ExecutionKind, ExecutionStatus,
    NodeResultState, Occupancy, ReasonCode, RejectCode, StoreConflictReason, StoreOutcome,
    TrustState, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.models import (
    CommandRecord, CommandResult, Decision, ExecutionContext, NodeInfo, NodeResult,
    NodeStatus, RestoreBundle, StoreResult, TargetSpec, UseRequest,
)


class RepositoryFake:
    def __init__(self, now):
        self.items = {"locker-1": NodeInfo("locker-1", "place-1", Capability.LOCKER,
            Occupancy.RENTAL, TrustState.TRUSTED, DeviceState.RESERVED, None, now,
            session_id="connection-1")}
        self.due = {}

    def nodes(self, capability=None):
        return tuple(n for n in self.items.values() if capability is None or n.capability == capability)


class StoreFake:
    """조건부 저장의 결과만 모사한다. 실제 DB 구현의 검증은 아니다."""

    def __init__(self, repository, clock):
        self.repository, self.clock = repository, clock
        self.contexts, self.commands, self.recoveries = {}, {}, {}
        self.calls, self.failures = [], {}
        self.read_error = None
        self.link_conflict = None
        self.closed_rental = False
        self.begin_conflict = None
        self.recovery_error = None
        self.reserved_at = clock()
        self.usages = {}

    def _before(self, name):
        failure = self.failures.pop(name, None)
        if failure is not None and not failure[1]:
            raise failure[0]
        return failure

    def _after(self, failure, result):
        if failure is not None:
            raise failure[0]
        return result

    def execution(self, internal_request_id):
        self.calls.append(("execution", internal_request_id))
        if self.read_error:
            raise self.read_error
        return self.commands.get(internal_request_id)

    def last_succeeded(self, node_id):
        done = [c for c in self.commands.values()
                if c.node_id == node_id and c.status == ExecutionStatus.SUCCEEDED]
        return done[-1] if done else None

    def link_command(self, internal_request_id, node_id, action):
        self.calls.append(("link", internal_request_id))
        failure = self._before("link")
        if self.link_conflict:
            return StoreResult(StoreOutcome.CONFLICT, conflict_reason=self.link_conflict)
        ctx = self.contexts[internal_request_id]
        current = self.commands.get(internal_request_id)
        if current is None and self.closed_rental:
            # 반납이 재조작할 대여를 먼저 닫았다. 연결을 거부하고 미전송 명령을 남긴다.
            refused = CommandRecord("command:" + internal_request_id, internal_request_id, node_id,
                action, ExecutionStatus.NOT_SENT, ctx.execution_kind, None, ctx.usage_id,
                self.reserved_at, outcome=CommandOutcome.NOT_SENT, occurred_at=self.clock(),
                evidence={"source": "link_command", "reason": ReasonCode.USE_NOT_FOUND.value})
            self.commands[internal_request_id] = refused
            self.usages[internal_request_id] = "released"
            return self._after(failure, StoreResult(StoreOutcome.CONFLICT, command=refused,
                conflict_reason=StoreConflictReason.STATE_CONFLICT))
        outcome = StoreOutcome.ALREADY_APPLIED if current else StoreOutcome.APPLIED
        if current is None:
            current = CommandRecord("command:" + internal_request_id, internal_request_id, node_id,
                action, ExecutionStatus.PENDING, ctx.execution_kind, ctx.use_id, ctx.usage_id,
                self.reserved_at)
            self.commands[internal_request_id] = current
        return self._after(failure, StoreResult(outcome, command=current))

    def begin_send(self, command_id):
        self.calls.append(("begin", command_id))
        failure = self._before("begin")
        current = self._command(command_id)
        if self.begin_conflict:
            return StoreResult(StoreOutcome.CONFLICT, command=current,
                               conflict_reason=self.begin_conflict)
        if current.status != ExecutionStatus.PENDING:
            return StoreResult(StoreOutcome.ALREADY_APPLIED, command=current)
        if any(c.node_id == current.node_id and c.command_id != command_id
               and c.status == ExecutionStatus.IN_PROGRESS for c in self.commands.values()):
            return StoreResult(StoreOutcome.CONFLICT, command=current,
                               conflict_reason=StoreConflictReason.COMMAND_IN_PROGRESS)
        current = replace(current, status=ExecutionStatus.IN_PROGRESS,
                          send_started_at=self.clock(), send_attempted=True)
        self.commands[current.internal_request_id] = current
        return self._after(failure, StoreResult(StoreOutcome.APPLIED, command=current))

    def cancel_not_sent(self, internal_request_id, expected, reason, evidence):
        self.calls.append(("cancel", internal_request_id, expected, reason, evidence))
        failure = self._before("cancel")
        current = self.commands[internal_request_id]
        if current.status == ExecutionStatus.NOT_SENT and current.evidence == evidence:
            return StoreResult(StoreOutcome.ALREADY_APPLIED, command=current)
        if current.status != expected:
            return StoreResult(StoreOutcome.CONFLICT, command=current)
        current = replace(current, status=ExecutionStatus.NOT_SENT, evidence=evidence)
        self.commands[internal_request_id] = current
        self.usages[internal_request_id] = "released"
        return self._after(failure, StoreResult(StoreOutcome.APPLIED, command=current))

    def record_result(self, command_id, outcome, occurred_at, rental_due_at, evidence):
        self.calls.append(("result", command_id, outcome, occurred_at, rental_due_at, evidence))
        failure = self._before("result")
        current = self._command(command_id)
        if current.outcome is not None:
            same = (current.outcome, current.occurred_at, current.evidence) == (outcome, occurred_at, evidence)
            return StoreResult(StoreOutcome.ALREADY_APPLIED if same else StoreOutcome.CONFLICT,
                               command=current)
        current = replace(current, status=ExecutionStatus(outcome.value), outcome=outcome,
                          occurred_at=occurred_at, evidence=evidence)
        self.commands[current.internal_request_id] = current
        if outcome in (CommandOutcome.FAILED, CommandOutcome.UNKNOWN):
            node = self.repository.items[current.node_id]
            self.repository.items[current.node_id] = replace(node, device_state=DeviceState.FAULT)
            self.usages[current.internal_request_id] = "held"
        elif outcome == CommandOutcome.SUCCEEDED:
            self.usages[current.internal_request_id] = "consumed"
            if rental_due_at is not None:
                self.repository.due[current.use_id] = rental_due_at
        else:
            self.usages[current.internal_request_id] = "released"
        return self._after(failure, StoreResult(StoreOutcome.APPLIED, command=current))

    def restore_pending(self):
        return tuple(RestoreBundle(node, self.clock(),
            commands=tuple(c for c in self.commands.values() if c.node_id == node.node_id),
            recoveries=tuple(r for r in self.recoveries.values() if r.node_id == node.node_id))
            for node in self.repository.items.values())

    def recovery(self, recovery_id):
        return self.recoveries.get(recovery_id)

    def save_recovery(self, record):
        self.calls.append(("recovery", record))
        if self.recovery_error:
            raise self.recovery_error
        self.recoveries[record.recovery_id] = record
        return StoreResult(StoreOutcome.APPLIED, recovery=record)

    def _command(self, command_id):
        return next(c for c in self.commands.values() if c.command_id == command_id)


class RecheckerFake:
    def __init__(self):
        self.calls, self.hook = [], None

    def recheck(self, ctx):
        self.calls.append(ctx)
        if self.hook:
            return self.hook(ctx)
        return Decision(ctx.internal_request_id, ctx.request.request_id, Verdict.OK, ReasonCode.OK,
                        node_id=ctx.node_id)


class GatewayFake:
    def __init__(self, clock):
        self.clock, self.calls, self.hook = clock, [], None

    def send(self, command, timeout_sec):
        self.calls.append((command, timeout_sec))
        if self.hook:
            return self.hook(command)
        now = self.clock()
        report = NodeResult(command.node_id, command.target_session_id, 1, now, now,
                            command.command_id, "event-1", NodeResultState.SUCCEEDED, now)
        return CommandResult(command.command_id, CommandOutcome.SUCCEEDED, report)


@pytest.fixture
def env():
    time = [datetime(2026, 10, 15, tzinfo=timezone.utc)]
    clock = lambda: time[0]
    repository = RepositoryFake(clock())
    store = StoreFake(repository, clock)
    rechecker, gateway = RecheckerFake(), GatewayFake(clock)
    locks = {"locker-1": RLock(), "light-1": RLock(), "locker-2": RLock()}
    lock_for = lambda node_id: locks[node_id]
    recovery = FaultRecoveryService(repository=repository, store=store, clock=clock, lock_for=lock_for)
    restored = {"locker-1": True, "light-1": True, "locker-2": True}
    def create():
        return ExecutionService(repository=repository, store=store, rechecker=rechecker,
            gateway=gateway, clock=clock, lock_for=lock_for,
            can_execute=lambda node_id: restored[node_id], record_isolated=recovery.record_isolated)
    request = UseRequest("visitor-1", "external-1", TargetSpec.parse("locker-1"), Action.OPEN,
                         "test", clock())
    ctx = ExecutionContext("request-1", request, "locker-1", "use-1", "usage-1", Settings(),
                           reserved_at=clock())
    store.contexts[ctx.internal_request_id] = ctx
    return SimpleNamespace(time=time, clock=clock, repository=repository, store=store,
        rechecker=rechecker, gateway=gateway, locks=locks, lock_for=lock_for, recovery=recovery,
        restored=restored, ctx=ctx, create=create, service=create())


def calls(env, name):
    return [c for c in env.store.calls if c[0] == name]


def advance(env, seconds):
    env.time[0] += timedelta(seconds=seconds)


def report_for(env, *, state=NodeResultState.SUCCEEDED, event_id="event-2", **changes):
    current = env.store.commands[env.ctx.internal_request_id]
    now = env.clock()
    return replace(NodeResult(env.ctx.node_id, "connection-1", 2, now, now, current.command_id,
                              event_id, state, now), **changes)


def gateway_outcome(env, outcome, *, state=None, error_code=None):
    def send(command):
        report = None if state is None else report_for(env, state=state, error_code=error_code)
        return CommandResult(command.command_id, outcome, report=report, reported_at=env.clock())
    env.gateway.hook = send


def deny(env, verdict=Verdict.ASK, reason=ReasonCode.MOVE_CLOSER):
    decision = Decision(env.ctx.internal_request_id, env.ctx.request.request_id, verdict, reason,
                        node_id=env.ctx.node_id)
    env.rechecker.hook = lambda ctx: decision
    return decision


def test_FR06_execute_sends_command_after_recheck(env):
    def send(command):
        assert env.rechecker.calls == [env.ctx]
        assert env.store.commands[env.ctx.internal_request_id].status == ExecutionStatus.IN_PROGRESS
        return CommandResult(command.command_id, CommandOutcome.UNKNOWN, reported_at=env.clock())
    env.gateway.hook = send
    env.service.execute(env.ctx)
    command, timeout = env.gateway.calls[0]
    assert command.target_session_id == "connection-1"
    assert timeout == env.ctx.settings.exec_timeout_sec
    assert command.expires_at == command.issued_at + timedelta(seconds=timeout)
    assert not hasattr(command, "user_id")


def test_FR19_initial_success_sets_rental_due_at(env):
    original = env.gateway.send
    def delayed(command, timeout):
        result = original(command, timeout)
        advance(env, 2)
        return result
    env.gateway.send = delayed
    result = env.service.execute(env.ctx)
    assert (result.status, result.use_id) == (ExecutionStatus.SUCCEEDED, "use-1")
    stored = calls(env, "result")[0]
    assert stored[3] == env.clock() - timedelta(seconds=2)
    assert stored[4] == env.clock() + timedelta(seconds=env.ctx.settings.rental_duration_sec)


@pytest.mark.parametrize("kind", [ExecutionKind.RENTAL_REOPERATION, ExecutionKind.SHARED])
def test_FR19_reoperation_and_shared_success_keep_due(env, kind):
    env.ctx = replace(env.ctx, execution_kind=kind,
                      use_id=None if kind == ExecutionKind.SHARED else "use-1")
    env.store.contexts[env.ctx.internal_request_id] = env.ctx
    old_due = env.clock() - timedelta(hours=1)
    env.repository.due["use-1"] = old_due
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED
    assert calls(env, "result")[0][4] is None
    assert env.repository.due["use-1"] == old_due


def test_FR18_duplicate_handover_returns_existing(env):
    first = env.service.execute(env.ctx)
    assert env.service.execute(env.ctx) == first
    assert env.create().execute(env.ctx) == first
    assert len(env.gateway.calls) == len(calls(env, "link")) == 1


def test_NFR03_concurrent_handover_sends_once(env):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: env.service.execute(env.ctx), range(20)))
    assert len(env.gateway.calls) == len(calls(env, "begin")) == 1
    assert all(r.status in (ExecutionStatus.IN_PROGRESS, ExecutionStatus.SUCCEEDED) for r in results)


@pytest.mark.parametrize("verdict", [Verdict.ASK, Verdict.BUSY, Verdict.DENY, Verdict.NEED_APPROVAL])
def test_FR18_recheck_reject_blocks_send(env, verdict):
    decision = deny(env, verdict)
    result = env.service.execute(env.ctx)
    assert result.status == ExecutionStatus.NOT_SENT
    assert result.recheck == decision
    assert result.use_id is None
    assert env.gateway.calls == []
    assert calls(env, "cancel")[0][2:4] == (ExecutionStatus.PENDING, decision.reason)
    assert env.store.usages[env.ctx.internal_request_id] == "released"


def test_FR18_recheck_exception_blocks_send(env):
    def fail(ctx):
        raise RuntimeError("private failure")
    env.rechecker.hook = fail
    result = env.service.execute(env.ctx)
    assert result.status == ExecutionStatus.NOT_SENT
    assert (result.recheck.verdict, result.recheck.reason) == (Verdict.DENY, ReasonCode.RECHECK_FAILED)
    assert "private" not in str(result)
    assert env.gateway.calls == []


@pytest.mark.parametrize("commit", [False, True])
def test_FR18_cancel_unknown_requeries_and_preserves_event(env, commit):
    deny(env)
    env.store.failures["cancel"] = (StoreOutcomeUnknown("cancel"), commit)
    result = env.service.execute(env.ctx)
    if not commit:
        assert result.status == ExecutionStatus.PENDING
        evidence = calls(env, "cancel")[0][4]
        advance(env, 100)
        env.service.tick()
        assert calls(env, "cancel")[-1][4] == evidence
    assert env.service.execute(env.ctx).status == ExecutionStatus.NOT_SENT
    assert env.gateway.calls == []


@pytest.mark.parametrize("commit", [False, True])
def test_FR06_link_unknown_requeries_without_duplicate_command(env, commit):
    env.store.failures["link"] = (StoreOutcomeUnknown("link"), commit)
    first = env.service.execute(env.ctx)
    if not commit:
        assert first.status == ExecutionStatus.PENDING
        assert first.use_id is None
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED
    assert len(env.gateway.calls) == len(env.store.commands) == 1


@pytest.mark.parametrize("commit", [False, True])
def test_FR06_begin_unknown_never_authorizes_send(env, commit):
    env.store.failures["begin"] = (StoreOutcomeUnknown("begin"), commit)
    first = env.service.execute(env.ctx)
    assert first.use_id is None
    env.service.tick()
    env.service.execute(env.ctx)
    assert len(calls(env, "begin")) == 1
    assert env.gateway.calls == []


def test_FR06_begin_not_applied_can_retry(env):
    env.store.failures["begin"] = (StoreNotApplied("begin"), False)
    assert env.service.execute(env.ctx).status == ExecutionStatus.PENDING
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED
    assert len(env.gateway.calls) == 1


@pytest.mark.parametrize("reason,expected", [
    (StoreConflictReason.COMMAND_IN_PROGRESS, ReasonCode.COMMAND_IN_PROGRESS),
    (StoreConflictReason.STATE_CONFLICT, ReasonCode.DEVICE_BUSY),
])
def test_FR19_begin_conflict_blocks_send(env, reason, expected):
    env.store.begin_conflict = reason
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason, result.recheck) == (ExecutionStatus.PENDING, expected, None)
    assert env.gateway.calls == calls(env, "cancel") == []


@pytest.mark.parametrize("stored", [True, False], ids=["command_stored", "conflict_only"])
def test_FR19_reoperation_link_after_close_is_use_not_found(env, stored):
    env.ctx = replace(env.ctx, execution_kind=ExecutionKind.RENTAL_REOPERATION)
    env.store.contexts[env.ctx.internal_request_id] = env.ctx
    # 저장소는 연결을 거부하며 미전송 명령을 남긴다. 명령 없이 STATE_CONFLICT만 주는 충돌도 같은 결과다.
    env.store.closed_rental = stored
    env.store.link_conflict = None if stored else StoreConflictReason.STATE_CONFLICT
    result = env.service.execute(env.ctx)
    refused = env.store.commands.get(env.ctx.internal_request_id)
    assert (refused is not None) == stored
    assert (result.status, result.reason, result.command_id, result.use_id, result.recheck) == (
        ExecutionStatus.NOT_SENT, ReasonCode.USE_NOT_FOUND, refused.command_id if stored else None, None, None)
    assert env.gateway.calls == env.rechecker.calls == calls(env, "begin") == calls(env, "cancel") == []
    # 같은 서비스는 다시 연결하지 않는다. 새 서비스는 저장된 명령을 읽거나 저장소가 같은 충돌을 알려 같은 결과를 낸다.
    assert env.service.execute(env.ctx) == result
    assert len(calls(env, "link")) == 1
    assert env.create().execute(env.ctx) == result
    if stored:
        # 읽을 때는 보이지 않다가 연결할 때 이미 있다고 답하는 재인계(ALREADY_APPLIED)도 같다.
        env.store.execution = lambda internal_request_id: None
        assert env.create().execute(env.ctx) == result
        assert env.store.commands[env.ctx.internal_request_id] == refused
        assert len(calls(env, "link")) == 2
    assert env.gateway.calls == env.rechecker.calls == calls(env, "begin") == calls(env, "cancel") == []


@pytest.mark.parametrize("kind,reason", [
    (ExecutionKind.RENTAL_INITIAL, StoreConflictReason.STATE_CONFLICT),
    (ExecutionKind.SHARED, StoreConflictReason.STATE_CONFLICT),
    (ExecutionKind.RENTAL_REOPERATION, StoreConflictReason.CONTENT_CONFLICT),
])
def test_FR19_other_link_conflicts_stay_link_conflict(env, kind, reason):
    env.ctx = replace(env.ctx, execution_kind=kind,
                      use_id=None if kind == ExecutionKind.SHARED else "use-1")
    env.store.contexts[env.ctx.internal_request_id] = env.ctx
    env.store.link_conflict = reason
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason, result.command_id) == (ExecutionStatus.PENDING, None, None)
    assert env.service.blockers(env.ctx.internal_request_id) == ("link_conflict",)
    assert env.gateway.calls == calls(env, "begin") == calls(env, "cancel") == []


def test_FR06_begin_already_applied_does_not_send(env):
    original = env.store.begin_send
    def start(command_id):
        saved = original(command_id)
        return replace(saved, outcome=StoreOutcome.ALREADY_APPLIED)
    env.store.begin_send = start
    assert env.service.execute(env.ctx).status == ExecutionStatus.IN_PROGRESS
    assert env.gateway.calls == []


@pytest.mark.parametrize("reserved", [None, "future"])
def test_FR19_missing_or_future_reservation_blocks_send(env, reserved):
    env.ctx = replace(env.ctx, reserved_at=None if reserved is None else env.clock() + timedelta(seconds=1))
    env.store.contexts[env.ctx.internal_request_id] = env.ctx
    result = env.service.execute(env.ctx)
    assert result.status == ExecutionStatus.PENDING
    assert env.gateway.calls == calls(env, "cancel") == []
    assert env.service.blockers(env.ctx.internal_request_id) == ("reservation_time_unconfirmed",)


def test_FR19_expired_reservation_cancelled_with_reason(env):
    env.ctx = replace(env.ctx, reserved_at=env.clock() - timedelta(
        seconds=env.ctx.settings.reservation_wait_sec))
    env.store.contexts[env.ctx.internal_request_id] = env.ctx
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.NOT_SENT, ReasonCode.RESERVATION_EXPIRED)
    assert env.gateway.calls == env.rechecker.calls == []
    assert calls(env, "cancel")[0][2:4] == (ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED)
    assert env.store.usages[env.ctx.internal_request_id] == "released"


def test_FR19_shared_without_reservation_executes(env):
    env.ctx = replace(env.ctx, execution_kind=ExecutionKind.SHARED, use_id=None, usage_id=None,
                      reserved_at=None)
    env.store.contexts[env.ctx.internal_request_id] = env.ctx
    advance(env, env.ctx.settings.reservation_wait_sec * 2)
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED


def test_FR19_live_pending_expires(env):
    env.restored[env.ctx.node_id] = False
    assert env.service.execute(env.ctx).status == ExecutionStatus.PENDING
    env.service.tick()
    assert calls(env, "cancel") == []
    advance(env, env.ctx.settings.reservation_wait_sec)
    env.service.tick()
    assert calls(env, "cancel")[0][2:4] == (ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED)
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.NOT_SENT, ReasonCode.RESERVATION_EXPIRED)
    assert env.gateway.calls == []


def test_FR19_expiry_races_begin_send(env):
    env.store.begin_conflict = StoreConflictReason.COMMAND_IN_PROGRESS
    env.service.execute(env.ctx)
    env.store.begin_conflict = None
    command_id = env.store.commands[env.ctx.internal_request_id].command_id
    original = env.store.cancel_not_sent
    def cancel(internal_request_id, expected, reason, evidence):
        env.store.begin_send(command_id)
        return original(internal_request_id, expected, reason, evidence)
    env.store.cancel_not_sent = cancel
    advance(env, env.ctx.settings.reservation_wait_sec)
    env.service.tick()
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.IN_PROGRESS, None)
    assert env.store.usages == {}
    assert env.gateway.calls == []


def test_FR19_expiry_during_recheck_blocks_send(env):
    def check(ctx):
        advance(env, ctx.settings.reservation_wait_sec)
        return Decision(ctx.internal_request_id, ctx.request.request_id, Verdict.OK, ReasonCode.OK)
    env.rechecker.hook = check
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.NOT_SENT, ReasonCode.RESERVATION_EXPIRED)
    assert env.gateway.calls == calls(env, "begin") == []
    assert calls(env, "cancel")[0][3] == ReasonCode.RESERVATION_EXPIRED


def test_FR19_restore_unconfirmed_blocks_send(env):
    env.restored[env.ctx.node_id] = False
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.PENDING, ReasonCode.TARGET_UNAVAILABLE)
    assert env.gateway.calls == env.rechecker.calls == calls(env, "cancel") == []
    env.restored[env.ctx.node_id] = True
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.SUCCEEDED, None)


def test_FR19_restore_invalidated_during_recheck_blocks_send(env):
    def check(ctx):
        env.restored[ctx.node_id] = False
        return Decision(ctx.internal_request_id, ctx.request.request_id, Verdict.OK, ReasonCode.OK)
    env.rechecker.hook = check
    env.service.execute(env.ctx)
    assert env.gateway.calls == calls(env, "begin") == []


def test_FR06_no_session_cancelled_with_reason(env):
    node = env.repository.items[env.ctx.node_id]
    env.repository.items[node.node_id] = replace(node, session_id=None)
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.NOT_SENT, ReasonCode.NODE_NOT_CONNECTED)
    assert env.gateway.calls == []
    assert calls(env, "cancel")[0][2:4] == (ExecutionStatus.PENDING, ReasonCode.NODE_NOT_CONNECTED)
    assert env.store.usages[env.ctx.internal_request_id] == "released"


def test_FR19_iot_not_sent_cancelled_with_reason(env):
    gateway_outcome(env, CommandOutcome.NOT_SENT)
    result = env.service.execute(env.ctx)
    assert (result.status, result.reason) == (ExecutionStatus.NOT_SENT, ReasonCode.COMMAND_NOT_SENT)
    assert calls(env, "cancel")[0][2:4] == (ExecutionStatus.IN_PROGRESS, ReasonCode.COMMAND_NOT_SENT)
    assert calls(env, "result") == []
    assert env.store.usages[env.ctx.internal_request_id] == "released"
    env.service.tick()
    assert env.service.execute(env.ctx) == result
    assert len(env.gateway.calls) == len(calls(env, "cancel")) == 1


def test_FR19_iot_not_sent_cancel_unknown_retries_same_evidence(env):
    gateway_outcome(env, CommandOutcome.NOT_SENT)
    env.store.failures["cancel"] = (StoreOutcomeUnknown("cancel"), False)
    assert env.service.execute(env.ctx).status == ExecutionStatus.IN_PROGRESS
    evidence = calls(env, "cancel")[0][4]
    advance(env, 5)
    env.service.tick()
    assert calls(env, "cancel")[-1][4] == evidence
    assert env.service.execute(env.ctx).status == ExecutionStatus.NOT_SENT
    assert len(env.gateway.calls) == 1


@pytest.mark.parametrize("outcome,state", [(CommandOutcome.FAILED, NodeResultState.FAILED),
                                        (CommandOutcome.UNKNOWN, None),
                                        (CommandOutcome.UNKNOWN, NodeResultState.UNKNOWN)])
def test_NFR04_failed_or_unknown_creates_recovery_and_holds_usage(env, outcome, state):
    gateway_outcome(env, outcome, state=state)
    result = env.service.execute(env.ctx)
    assert result.status == ExecutionStatus(outcome.value)
    assert result.use_id is None
    assert env.store.usages[env.ctx.internal_request_id] == "held"
    assert env.repository.items[env.ctx.node_id].device_state == DeviceState.FAULT
    record = next(iter(env.store.recoveries.values()))
    assert record.command_ids == (result.command_id,)
    assert record.use_id == env.ctx.use_id
    assert bool(record.evidence) == (state is not None)


def test_FR07_gateway_exception_is_unknown_not_not_sent(env):
    def fail(command):
        raise TimeoutError("lost reply")
    env.gateway.hook = fail
    assert env.service.execute(env.ctx).status == ExecutionStatus.UNKNOWN
    assert calls(env, "result")[0][5]["source"] == "gateway_error"
    assert calls(env, "cancel") == []


def test_FR19_device_busy_rejected_not_fault(env):
    gateway_outcome(env, CommandOutcome.REJECTED, state=NodeResultState.REJECTED,
                    error_code=RejectCode.DEVICE_BUSY)
    assert env.service.execute(env.ctx).status == ExecutionStatus.REJECTED
    assert env.store.usages[env.ctx.internal_request_id] == "released"
    assert env.repository.items[env.ctx.node_id].device_state != DeviceState.FAULT
    assert env.store.recoveries == {}
    assert calls(env, "cancel") == []


@pytest.mark.parametrize("outcome", [CommandOutcome.SUCCEEDED, CommandOutcome.FAILED, CommandOutcome.REJECTED])
def test_FR07_no_device_evidence_never_confirms_success_or_rejection(env, outcome):
    gateway_outcome(env, outcome)
    assert env.service.execute(env.ctx).status == ExecutionStatus.UNKNOWN
    assert env.store.usages[env.ctx.internal_request_id] == "held"


@pytest.mark.parametrize("commit", [False, True])
def test_FR19_store_unknown_requeries_before_success(env, commit):
    env.store.failures["result"] = (StoreOutcomeUnknown("result"), commit)
    result = env.service.execute(env.ctx)
    if not commit:
        assert (result.status, result.use_id) == (ExecutionStatus.IN_PROGRESS, None)
        saved = calls(env, "result")[0]
        advance(env, 100)
        env.service.tick()
        assert calls(env, "result")[-1][1:] == saved[1:]
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED
    assert len(env.gateway.calls) == 1


def test_FR19_result_not_applied_retry_keeps_original_due(env):
    env.store.failures["result"] = (StoreNotApplied("result"), False)
    assert env.service.execute(env.ctx).status == ExecutionStatus.IN_PROGRESS
    first = calls(env, "result")[0]
    advance(env, 100)
    env.service.tick()
    assert calls(env, "result")[-1] == first
    assert env.service.execute(env.ctx).use_id == env.ctx.use_id


def test_NFR04_recovery_save_failure_retries_same_id(env):
    gateway_outcome(env, CommandOutcome.UNKNOWN)
    env.store.recovery_error = StoreNotApplied("recovery")
    assert env.service.execute(env.ctx).status == ExecutionStatus.UNKNOWN
    first = calls(env, "recovery")[0][1]
    advance(env, 100)
    env.store.recovery_error = None
    env.service.tick()
    assert next(iter(env.store.recoveries.values())) == first
    assert len(calls(env, "result")) == len(env.gateway.calls) == 1


def test_FR07_late_success_keeps_fault(env):
    gateway_outcome(env, CommandOutcome.UNKNOWN)
    env.service.execute(env.ctx)
    advance(env, 10)
    env.service.on_result(env.ctx, report_for(env))
    assert len(calls(env, "result")) == 2
    assert env.service.execute(env.ctx).status == ExecutionStatus.UNKNOWN
    assert env.repository.items[env.ctx.node_id].device_state == DeviceState.FAULT
    assert env.store.usages[env.ctx.internal_request_id] == "held"


def test_FR07_same_event_settled_once(env):
    env.service.execute(env.ctx)
    original = report_for(env, event_id="event-1")
    env.service.on_result(env.ctx, replace(original, seq=10, received_at=env.clock() + timedelta(seconds=1)))
    assert len(calls(env, "result")) == 1


def test_FR07_conflicting_duplicate_event_is_not_overwritten(env):
    env.service.execute(env.ctx)
    with pytest.raises(StoreError):
        env.service.on_result(env.ctx, report_for(env, event_id="event-1", state=NodeResultState.FAILED))
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED


@pytest.mark.parametrize("changes", [{"command_id": "other"}, {"node_id": "other"},
                                     {"session_id": "other"}, {"event_id": ""}])
def test_FR07_wrong_report_link_not_saved(env, changes):
    env.service.execute(env.ctx)
    with pytest.raises(StoreError):
        env.service.on_result(env.ctx, report_for(env, **changes))
    assert len(calls(env, "result")) == 1


def test_FR07_on_result_store_failure_propagates_and_tick_retries(env):
    gateway_outcome(env, CommandOutcome.NOT_SENT)
    env.service.execute(env.ctx)
    env.store.failures["result"] = (StoreNotApplied("result"), False)
    with pytest.raises(StoreError):
        env.service.on_result(env.ctx, report_for(env))
    env.service.tick()
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED


def test_FR07_accepted_and_running_are_not_success(env):
    gateway_outcome(env, CommandOutcome.NOT_SENT)
    env.service.execute(env.ctx)
    for state in (NodeResultState.ACCEPTED, NodeResultState.RUNNING):
        env.service.on_result(env.ctx, report_for(env, state=state, event_id=state.value))
    assert calls(env, "result") == []
    env.service.on_result(env.ctx, report_for(env, state=NodeResultState.REJECTED,
                         error_code=RejectCode.DEVICE_BUSY))
    assert env.service.execute(env.ctx).status == ExecutionStatus.UNKNOWN


def test_NFR03_report_can_arrive_during_gateway_wait_without_deadlock(env):
    def send(command):
        report = report_for(env)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(env.service.on_result, env.ctx, report).result(timeout=2)
        return CommandResult(command.command_id, CommandOutcome.SUCCEEDED, report)
    env.gateway.hook = send
    assert env.service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED
    assert len(env.gateway.calls) == len(calls(env, "result")) == 1


def test_NFR03_second_request_cannot_start_while_first_waits(env):
    entered, finish = Event(), Event()
    def send(command):
        entered.set()
        assert finish.wait(timeout=2)
        return CommandResult(command.command_id, CommandOutcome.UNKNOWN, reported_at=env.clock())
    env.gateway.hook = send
    ctx2 = replace(env.ctx, internal_request_id="request-2", execution_kind=ExecutionKind.RENTAL_REOPERATION)
    env.store.contexts[ctx2.internal_request_id] = ctx2
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(env.service.execute, env.ctx)
        try:
            assert entered.wait(timeout=2)
            result = pool.submit(env.service.execute, ctx2).result(timeout=2)
            assert result.status == ExecutionStatus.PENDING
        finally:
            finish.set()
        assert first.result(timeout=2).status == ExecutionStatus.UNKNOWN
    assert len(env.gateway.calls) == 1


def test_FR19_corrupt_stored_link_does_not_send_or_expose_use(env):
    env.store.link_command(env.ctx.internal_request_id, env.ctx.node_id, env.ctx.request.action)
    current = env.store.commands[env.ctx.internal_request_id]
    env.store.commands[env.ctx.internal_request_id] = replace(current, usage_id="other")
    result = env.service.execute(env.ctx)
    assert result.use_id is None
    assert env.gateway.calls == []


def test_FR19_lookup_failure_does_not_link_or_send(env):
    env.store.read_error = StoreNotApplied("read")
    assert env.service.execute(env.ctx).status == ExecutionStatus.PENDING
    assert calls(env, "link") == env.gateway.calls == []


def test_NFR03_changed_context_is_rejected(env):
    env.service.execute(env.ctx)
    with pytest.raises(ValueError):
        env.service.execute(replace(env.ctx, usage_id="other"))
    assert len(env.gateway.calls) == 1


def test_FR07_restart_same_event_does_not_recalculate_rental_due(env):
    env.service.execute(env.ctx)
    report = report_for(env, event_id="event-1")
    due = env.repository.due[env.ctx.use_id]
    advance(env, 100)
    env.create().on_result(env.ctx, replace(report, received_at=env.clock(), seq=20))
    assert len(calls(env, "result")) == 1
    assert env.repository.due[env.ctx.use_id] == due


def test_FR19_recheck_cancellation_races_external_begin(env):
    decision = deny(env)
    original = env.store.cancel_not_sent
    def cancel(internal_request_id, expected, reason, evidence):
        env.store.begin_send(env.store.commands[internal_request_id].command_id)
        return original(internal_request_id, expected, reason, evidence)
    env.store.cancel_not_sent = cancel
    result = env.service.execute(env.ctx)
    assert result.status == ExecutionStatus.IN_PROGRESS
    assert result.recheck == decision
    assert env.store.usages == {}
    assert env.gateway.calls == []


def test_FR19_unconfirmed_saved_success_does_not_expose_use(env):
    def incorrect(command_id, outcome, occurred_at, rental_due_at, evidence):
        current = env.store._command(command_id)
        return StoreResult(StoreOutcome.APPLIED, command=replace(current,
            status=ExecutionStatus.SUCCEEDED, outcome=outcome, occurred_at=occurred_at,
            evidence={"event_id": "other"}))
    env.store.record_result = incorrect
    result = env.service.execute(env.ctx)
    assert (result.status, result.use_id) == (ExecutionStatus.IN_PROGRESS, None)


def test_FR18_recheck_wrong_target_blocks_send(env):
    env.rechecker.hook = lambda ctx: Decision(ctx.internal_request_id, ctx.request.request_id,
        Verdict.OK, ReasonCode.OK, node_id="other")
    assert env.service.execute(env.ctx).status == ExecutionStatus.PENDING
    assert env.gateway.calls == calls(env, "begin") == []


def test_FR19_tick_continues_after_another_device_store_failure(env):
    env.store.failures["result"] = (StoreNotApplied("result"), False)
    env.service.execute(env.ctx)
    node = env.repository.items[env.ctx.node_id]
    env.repository.items["locker-2"] = replace(node, node_id="locker-2")
    ctx2 = replace(env.ctx, internal_request_id="request-2", node_id="locker-2", use_id="use-2")
    env.store.contexts[ctx2.internal_request_id] = ctx2
    env.store.failures["result"] = (StoreNotApplied("result"), False)
    env.service.execute(ctx2)
    env.store.failures["result"] = (StoreNotApplied("result"), False)
    with pytest.raises(StoreError):
        env.service.tick()
    assert env.store.commands[env.ctx.internal_request_id].status == ExecutionStatus.IN_PROGRESS
    assert env.store.commands[ctx2.internal_request_id].status == ExecutionStatus.SUCCEEDED
    assert len(env.gateway.calls) == 2


def test_FR19_execution_uses_real_restore_gate(env):
    node = env.repository.items[env.ctx.node_id]
    env.repository.items[node.node_id] = replace(node, device_state=DeviceState.AVAILABLE)
    release = ReleaseService(repository=env.repository, store=env.store,
        settings=lambda: env.ctx.settings, clock=env.clock, lock_for=env.lock_for)
    light = SharedLightService(repository=env.repository, store=env.store,
        policy=SimpleNamespace(check=lambda *args: None), settings=lambda: env.ctx.settings,
        clock=env.clock, lock_for=env.lock_for)
    restore = RestoreService(repository=env.repository, store=env.store, release=release,
        recovery=env.recovery, shared_light=light, settings=lambda: env.ctx.settings,
        clock=env.clock, lock_for=env.lock_for)
    restore.restore()
    service = ExecutionService(repository=env.repository, store=env.store, rechecker=env.rechecker,
        gateway=env.gateway, clock=env.clock, lock_for=env.lock_for,
        can_execute=restore.can_execute, record_isolated=env.recovery.record_isolated)
    assert service.execute(env.ctx).status == ExecutionStatus.PENDING
    assert env.gateway.calls == []
    now = env.clock()
    restore.on_status(NodeStatus(env.ctx.node_id, now, "connection-1", 1, now, now,
        CommandState.IDLE))
    env.repository.items[node.node_id] = replace(node, device_state=DeviceState.RESERVED)
    assert service.execute(env.ctx).status == ExecutionStatus.SUCCEEDED


def test_NFR04_result_committed_but_query_failed_still_creates_recovery(env):
    gateway_outcome(env, CommandOutcome.UNKNOWN)
    original = env.store.record_result
    def save(*args):
        saved = original(*args)
        env.store.read_error = StoreNotApplied("query")
        raise StoreOutcomeUnknown("committed")
    env.store.record_result = save
    assert env.service.execute(env.ctx).status == ExecutionStatus.IN_PROGRESS
    assert env.store.recoveries == {}
    env.store.record_result = original
    env.store.read_error = None
    env.service.tick()
    assert len(env.store.recoveries) == 1
    assert env.service.execute(env.ctx).status == ExecutionStatus.UNKNOWN
    assert len(env.gateway.calls) == 1


def test_FR19_reoperation_reject_keeps_existing_due(env):
    env.ctx = replace(env.ctx, execution_kind=ExecutionKind.RENTAL_REOPERATION)
    env.store.contexts[env.ctx.internal_request_id] = env.ctx
    old_due = env.clock() - timedelta(hours=1)
    env.repository.due[env.ctx.use_id] = old_due
    gateway_outcome(env, CommandOutcome.REJECTED, state=NodeResultState.REJECTED,
                    error_code=RejectCode.DEVICE_BUSY)
    assert env.service.execute(env.ctx).status == ExecutionStatus.REJECTED
    assert calls(env, "result")[0][4] is None
    assert env.repository.due[env.ctx.use_id] == old_due
    assert env.store.recoveries == {}


def test_FR07_other_device_rejection_is_unknown(env):
    gateway_outcome(env, CommandOutcome.REJECTED, state=NodeResultState.REJECTED,
                    error_code=RejectCode.STORAGE_ERROR)
    assert env.service.execute(env.ctx).status == ExecutionStatus.UNKNOWN
    assert env.store.usages[env.ctx.internal_request_id] == "held"


def internal_env(env, *, confirm=True):
    from nearpilot.shared.enums import InternalRequestCause
    from nearpilot.shared.models import InternalRequest
    env.repository.items["light-1"] = NodeInfo("light-1", "place-1", Capability.LIGHT,
        Occupancy.SHARED, TrustState.TRUSTED, DeviceState.AVAILABLE, None, env.clock(),
        session_id="connection-1")
    internal = InternalRequest("internal-1", "light-1", Action.OFF, InternalRequestCause.LEAVE_OFF,
                               Settings(), env.clock())
    ctx = ExecutionContext("internal-1", None, "light-1", None, None, Settings(),
                           ExecutionKind.SERVER_INTERNAL, internal_request=internal)
    env.store.contexts[ctx.internal_request_id] = ctx
    env.store.internal = []
    def link_internal_request(req):
        env.store.calls.append(("link_internal", req.internal_request_id))
        env.store.internal.append(req)
        return StoreResult(StoreOutcome.APPLIED)
    env.store.link_internal_request = link_internal_request
    checks = []
    def check(c):
        checks.append(c)
        return confirm
    service = ExecutionService(repository=env.repository, store=env.store, rechecker=env.rechecker,
        gateway=env.gateway, clock=env.clock, lock_for=env.lock_for,
        can_execute=lambda node_id: env.restored[node_id],
        record_isolated=env.recovery.record_isolated, confirm_internal=check)
    return ctx, service, checks


def test_FR20_internal_request_executes_without_recheck(env):
    ctx, service, checks = internal_env(env)
    result = service.execute(ctx)
    assert (result.status, result.use_id, result.reason) == (ExecutionStatus.SUCCEEDED, None, None)
    assert env.rechecker.calls == []
    assert checks == [ctx]
    assert [c[0] for c in env.store.calls if c[0] in ("link_internal", "link", "begin")] == [
        "link_internal", "link", "begin"]
    command, _ = env.gateway.calls[0]
    assert (command.node_id, command.action) == ("light-1", Action.OFF)
    assert calls(env, "result")[0][4] is None


@pytest.mark.parametrize("cause", ["check", "restore", "connection"])
def test_FR20_internal_request_check_failure_stores_nothing(env, cause):
    ctx, service, _ = internal_env(env, confirm=cause != "check")
    if cause == "restore":
        env.restored["light-1"] = False
    if cause == "connection":
        env.repository.items["light-1"] = replace(env.repository.items["light-1"], session_id=None)
    result = service.execute(ctx)
    assert result.status == ExecutionStatus.NOT_SENT
    assert result.command_id is None
    assert result.reason == {"check": None, "restore": ReasonCode.TARGET_UNAVAILABLE,
                             "connection": ReasonCode.NODE_NOT_CONNECTED}[cause]
    assert env.store.internal == [] and env.store.commands == {}
    assert env.gateway.calls == []
    assert service.execute(ctx) == result


def test_FR20_internal_request_begin_conflict_cancels(env):
    ctx, service, _ = internal_env(env)
    env.store.begin_conflict = StoreConflictReason.COMMAND_IN_PROGRESS
    result = service.execute(ctx)
    assert (result.status, result.reason) == (ExecutionStatus.NOT_SENT, ReasonCode.COMMAND_IN_PROGRESS)
    assert calls(env, "cancel")[0][2:4] == (ExecutionStatus.PENDING, ReasonCode.COMMAND_IN_PROGRESS)
    assert env.gateway.calls == []
