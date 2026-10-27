"""재시작 복원 검증. DB·IOT는 shared 계약의 fake로 대신한다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.core.lifecycle.restore import RestoreService
from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, CommandState, ApprovalUsageState, Capability, CommandOutcome, DeviceState,
    EndReason, ExecutionKind, ExecutionStatus, Occupancy, ReasonCode, RecoveryStatus, SessionState,
    StoreOutcome, TrustState, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.models import (
    ApprovalUsageRecord, CommandRecord, Decision, NodeInfo, NodeStatus, PendingRequestRecord,
    PolicyCheckResult, RecoveryCompletion, RecoveryEvidence, RecoveryRecord,
    RecoverySettlement, RestoredSession, RestoreBundle, StoreResult, TargetSpec, UseRequest, UseSession,
)


class RepositoryFake:
    def __init__(self, at):
        self.items = {"locker-1": NodeInfo(
            "locker-1", "place-1", Capability.LOCKER, Occupancy.RENTAL,
            TrustState.TRUSTED, DeviceState.AVAILABLE, None, at, session_id="connection-1",
        )}
        self.sessions = {}
        self.error = None

    def nodes(self, capability=None):
        if self.error:
            raise self.error
        return tuple(n for n in self.items.values() if capability is None or n.capability == capability)

    def session(self, use_id):
        return self.sessions.get(use_id)

    def session_owner_matches(self, use_id, user_id):
        return use_id in self.sessions and user_id == "visitor-1"


class StoreFake:
    def __init__(self, repository, clock):
        self.repository, self.clock = repository, clock
        self.commands = {}
        self.requests = {}
        self.usages = {}
        self.recoveries = {}
        self.reads = 0
        self.restore_error = None
        self.execution_error = None
        self.result_calls = []
        self.result_error = None
        self.result_commit = False
        self.result_conflict = False
        self.recovery_error = None
        self.recovery_commit = False
        self.recovery_saves = []
        self.override = None
        self.cancel_calls = []
        self.cancel_error = None
        self.cancel_commit = False
        self.cancel_conflict = False
        self.cancelled = {}
        self.closes = []

    def restore_pending(self):
        self.reads += 1
        if self.restore_error:
            raise self.restore_error
        if self.override is not None:
            return self.override
        return tuple(RestoreBundle(
            node, self.clock(),
            requests=tuple(r for node_id, r in self.requests.values() if node_id == node.node_id),
            commands=tuple(c for c in self.commands.values() if c.node_id == node.node_id),
            sessions=tuple(RestoredSession(s, "visitor-1", self.clock() - timedelta(hours=2))
                           for s in self.repository.sessions.values() if s.node_id == node.node_id),
            approval_usages=tuple(u for node_id, u in self.usages.values() if node_id == node.node_id),
            recoveries=tuple(r for r in self.recoveries.values() if r.node_id == node.node_id),
        ) for node in self.repository.items.values())

    def execution(self, internal_request_id):
        if self.execution_error:
            raise self.execution_error
        return self.commands.get(internal_request_id)

    def record_result(self, command_id, outcome, occurred_at, rental_due_at, evidence):
        self.result_calls.append((command_id, outcome, occurred_at, rental_due_at, evidence))
        current = next(c for c in self.commands.values() if c.command_id == command_id)
        if self.result_conflict:
            return StoreResult(StoreOutcome.CONFLICT, command=current)
        error, self.result_error = self.result_error, None
        if error and not self.result_commit:
            raise error
        assert outcome == CommandOutcome.UNKNOWN
        current = replace(current, status=ExecutionStatus.UNKNOWN, outcome=outcome,
                          occurred_at=occurred_at, evidence=evidence)
        self.commands[current.internal_request_id] = current
        node = self.repository.items[current.node_id]
        self.repository.items[current.node_id] = replace(node, device_state=DeviceState.FAULT)
        for key, (node_id, usage) in self.usages.items():
            if usage.internal_request_id == current.internal_request_id:
                self.usages[key] = node_id, replace(usage, state=ApprovalUsageState.HELD)
        if error:
            raise error
        return StoreResult(StoreOutcome.APPLIED, command=current)

    def cancel_not_sent(self, internal_request_id, expected, reason, evidence):
        self.cancel_calls.append((internal_request_id, expected, reason, evidence))
        if self.cancel_conflict:
            return StoreResult(StoreOutcome.CONFLICT, command=self.commands.get(internal_request_id))
        if internal_request_id in self.cancelled:
            same = self.cancelled[internal_request_id] == evidence
            return StoreResult(StoreOutcome.ALREADY_APPLIED if same else StoreOutcome.CONFLICT,
                               command=self.commands.get(internal_request_id))
        error, self.cancel_error = self.cancel_error, None
        if error and not self.cancel_commit:
            raise error
        current = self.commands.get(internal_request_id)
        if current is not None:
            if current.status != expected:
                return StoreResult(StoreOutcome.CONFLICT, command=current)
            use_id = current.use_id
            current = replace(current, status=ExecutionStatus.NOT_SENT, evidence=evidence)
            self.commands[internal_request_id] = current
        else:
            _, request = self.requests.pop(internal_request_id)
            use_id = request.use_id
        self.cancelled[internal_request_id] = evidence
        for key, (node_id, usage) in self.usages.items():
            if usage.internal_request_id == internal_request_id:
                self.usages[key] = node_id, replace(usage, state=ApprovalUsageState.RELEASED)
        session = self.repository.sessions.get(use_id)
        if session is not None and session.state == SessionState.RESERVED:
            self.repository.sessions[use_id] = replace(session, state=SessionState.CLOSED)
            node = self.repository.items[session.node_id]
            self.repository.items[session.node_id] = replace(node, device_state=DeviceState.AVAILABLE)
        if error:
            raise error
        return StoreResult(StoreOutcome.APPLIED, command=current)

    def finish_close(self, use_id, closed_at, end_reason):
        self.closes.append((use_id, closed_at, end_reason))
        session = self.repository.sessions[use_id]
        node = self.repository.items[session.node_id]
        if session.state != SessionState.CLOSING or node.device_state != DeviceState.CLOSING:
            return StoreResult(StoreOutcome.CONFLICT)
        self.repository.sessions[use_id] = replace(session, state=SessionState.CLOSED, closed_at=closed_at)
        self.repository.items[session.node_id] = replace(node, device_state=DeviceState.AVAILABLE)
        return StoreResult(StoreOutcome.APPLIED)

    def close_result(self, use_id):
        return None

    def last_succeeded(self, node_id):
        """재시작 전 마지막 성공 명령. 조명은 켜기에 성공했다고 둔다."""
        if not node_id.startswith("light"):
            return None
        return CommandRecord("last-on", "request-last-on", node_id, Action.ON, ExecutionStatus.SUCCEEDED,
                             ExecutionKind.SHARED)

    def recovery(self, recovery_id):
        return self.recoveries.get(recovery_id)

    def save_recovery(self, record):
        self.recovery_saves.append(record)
        error, self.recovery_error = self.recovery_error, None
        if error and not self.recovery_commit:
            raise error
        self.recoveries[record.recovery_id] = record
        if error:
            raise error
        return StoreResult(StoreOutcome.APPLIED, recovery=record)


class PolicyFake:
    def check(self, user_id, node_id, action, now):
        return PolicyCheckResult(Verdict.OK, ReasonCode.OK, None)


@pytest.fixture
def env():
    at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    time = [at]
    clock = lambda: time[0]
    repository = RepositoryFake(at)
    store = StoreFake(repository, clock)
    settings = [Settings()]
    locks = {"locker-1": RLock(), "light-1": RLock(), "locker-2": RLock()}
    lock_for = lambda node_id: locks.setdefault(node_id, RLock())
    recovery = FaultRecoveryService(repository=repository, store=store, clock=clock, lock_for=lock_for)
    release = ReleaseService(repository=repository, store=store, settings=lambda: settings[0],
                             clock=clock, lock_for=lock_for)
    light = SharedLightService(repository=repository, store=store, policy=PolicyFake(),
                               settings=lambda: settings[0], clock=clock, lock_for=lock_for)
    def create():
        return RestoreService(repository=repository, store=store, release=release, recovery=recovery,
                              shared_light=light, settings=lambda: settings[0], clock=clock, lock_for=lock_for)
    return SimpleNamespace(time=time, clock=clock, repository=repository, store=store, settings=settings,
                           release=release, recovery=recovery, light=light,
                           create=create, service=create())


def status(env, node_id="locker-1", **changes):
    now = env.clock()
    node = env.repository.items[node_id]
    env.repository.items[node_id] = replace(node, last_seen_at=now)
    report = NodeStatus(node_id, now, node.session_id, int(now.timestamp() * 1000), now, now,
                        CommandState.IDLE)
    report = replace(report, **changes)
    env.service.on_status(report)
    return report


def advance(env, seconds):
    env.time[0] += timedelta(seconds=seconds)


def command(env, status=ExecutionStatus.IN_PROGRESS, *, shared=False, age=20):
    node_id = "light-1" if shared else "locker-1"
    kind = ExecutionKind.SHARED if shared else ExecutionKind.RENTAL_INITIAL
    record = CommandRecord("command-1", "request-1", node_id, Action.ON if shared else Action.OPEN,
                           status, kind, None if shared else "use-1", "usage-1",
                           env.clock() - timedelta(seconds=age),
                           send_started_at=env.clock() - timedelta(seconds=age - 1)
                           if status != ExecutionStatus.PENDING else None,
                           send_attempted=status != ExecutionStatus.PENDING)
    env.store.commands[record.internal_request_id] = record
    return record


def rental(env, state=SessionState.ACTIVE):
    session = UseSession("use-1", "request-1", "locker-1", state,
                         rental_due_at=env.clock() - timedelta(hours=1),
                         end_reason=EndReason.RELEASE if state == SessionState.CLOSING else None)
    env.repository.sessions[session.use_id] = session
    node = env.repository.items[session.node_id]
    env.repository.items[session.node_id] = replace(node, device_state=DeviceState(state.value))
    return session


def light(env):
    env.repository.items["light-1"] = replace(env.repository.items["locker-1"], node_id="light-1",
                                             capability=Capability.LIGHT, occupancy=Occupancy.SHARED)


def pending_request(env, *, shared=False, age=20, verdict=Verdict.OK):
    """명령에 연결되기 전의 예약 요청. verdict가 None이면 판정이 비었다."""
    node_id = "light-1" if shared else "locker-1"
    request = UseRequest("visitor-1", "external-1", TargetSpec.parse(node_id),
                         Action.ON if shared else Action.OPEN, "test", env.clock())
    decision = None if verdict is None else Decision(
        "request-1", "external-1", verdict, ReasonCode.OK if verdict == Verdict.OK else ReasonCode.INTERNAL_ERROR)
    record = PendingRequestRecord("request-1", request, "digest",
                                  ExecutionKind.SHARED if shared else ExecutionKind.RENTAL_INITIAL,
                                  None if shared else "use-1", "usage-1",
                                  env.clock() - timedelta(seconds=age), decision)
    env.store.requests[record.internal_request_id] = (node_id, record)
    env.store.usages["usage-1"] = node_id, ApprovalUsageRecord(
        "usage-1", "approval-1", "request-1", None, ApprovalUsageState.RESERVED,
    )
    return record


def test_FR19_restore_blocks_new_execution_until_confirmed(env):
    assert not env.service.can_execute("locker-1")
    env.service.restore()
    assert not env.service.can_execute("locker-1")
    status(env)
    assert env.service.can_execute("locker-1")


@pytest.mark.parametrize("error", [StoreNotApplied("read"), StoreOutcomeUnknown("read")])
def test_FR19_restore_failure_blocks_all(env, error):
    env.store.restore_error = error
    with pytest.raises(StoreError):
        env.service.restore()
    status(env)
    assert not env.service.can_execute("locker-1")
    env.store.restore_error = None
    env.service.tick()
    assert not env.service.can_execute("locker-1")
    advance(env, 1)
    status(env)
    assert env.service.can_execute("locker-1")


def test_FR19_repository_failure_blocks_all(env):
    env.repository.error = StoreNotApplied("nodes")
    with pytest.raises(StoreError):
        env.service.restore()
    assert env.service.blockers("locker-1") == ("restore_not_loaded",)


def test_FR19_empty_pending_still_waits_for_status(env):
    env.store.override = ()
    env.service.restore()
    assert not env.service.can_execute("locker-1")
    status(env)
    assert env.service.can_execute("locker-1")


def test_FR19_restore_in_progress_isolated(env):
    session = rental(env, SessionState.RESERVED)
    command(env)
    env.service.restore()
    assert env.repository.items["locker-1"].device_state == DeviceState.FAULT
    assert env.repository.sessions["use-1"] == session
    assert len(env.store.result_calls) == 1
    record = next(iter(env.store.recoveries.values()))
    assert record.command_ids == ("command-1",)
    assert record.use_id == "use-1"
    assert env.service.blockers("locker-1") == ("fault_recovery",)
    status(env)
    assert not env.service.can_execute("locker-1")


def test_FR19_shared_in_progress_preserves_approval_usage(env):
    light(env)
    pending_request(env, shared=True)
    command(env, shared=True)
    env.service.restore()
    assert env.store.usages["usage-1"][1].state == ApprovalUsageState.HELD
    assert next(iter(env.store.recoveries.values())).use_id is None
    assert env.repository.sessions == {}


def test_FR19_restart_unknown_has_server_source_without_device_event(env):
    command(env)
    env.service.restore()
    _, outcome, at, due, evidence = env.store.result_calls[0]
    assert outcome == CommandOutcome.UNKNOWN
    assert at == env.clock() and due is None
    assert evidence["source"] == "server_restart"
    assert "event_id" not in evidence


def test_FR19_unknown_store_commit_is_requeried(env):
    command(env)
    env.store.result_error = StoreOutcomeUnknown("commit")
    env.store.result_commit = True
    env.service.restore()
    assert len(env.store.recoveries) == 1
    assert not env.service.can_execute("locker-1")


def test_FR19_unknown_store_without_commit_retries_same_event(env):
    command(env)
    env.store.result_error = StoreOutcomeUnknown("commit")
    with pytest.raises(StoreOutcomeUnknown):
        env.service.restore()
    advance(env, 4)
    env.service.tick()
    assert env.store.result_calls[0] == env.store.result_calls[1]
    assert len(env.store.recoveries) == 1


def test_FR19_not_applied_result_stays_blocked_and_retries(env):
    command(env)
    env.store.result_error = StoreNotApplied("write")
    with pytest.raises(StoreNotApplied):
        env.service.restore()
    assert not env.service.can_execute("locker-1")
    env.service.tick()
    assert len(env.store.recoveries) == 1


def test_FR19_result_conflict_does_not_unlock_or_create_recovery(env):
    command(env)
    env.store.result_conflict = True
    with pytest.raises(StoreError):
        env.service.restore()
    assert env.store.recoveries == {}
    assert not env.service.can_execute("locker-1")


def test_FR19_missing_execution_not_assumed_unsent(env):
    saved = command(env)
    env.store.override = (RestoreBundle(env.repository.items["locker-1"], env.clock(), commands=(saved,)),)
    env.store.commands.clear()
    with pytest.raises(StoreError):
        env.service.restore()
    assert env.store.result_calls == []
    assert not env.service.can_execute("locker-1")


def test_FR19_command_target_mismatch_blocks(env):
    saved = command(env)
    env.store.override = (RestoreBundle(env.repository.items["locker-1"], env.clock(), commands=(saved,)),)
    env.store.commands["request-1"] = replace(saved, command_id="other")
    with pytest.raises(StoreError):
        env.service.restore()
    assert env.store.result_calls == []


def test_FR19_pending_waits_then_expires_after_restart(env):
    session = rental(env, SessionState.RESERVED)
    saved = command(env, ExecutionStatus.PENDING, age=1)
    env.service.restore()
    status(env)
    assert env.service.blockers("locker-1") == ("reservation_pending",)
    assert env.store.commands["request-1"] == saved
    assert env.repository.sessions["use-1"] == session
    assert not env.service.can_execute("locker-1")
    advance(env, env.settings[0].reservation_wait_sec)
    status(env)
    assert env.store.cancel_calls[0][:3] == ("request-1", ExecutionStatus.PENDING,
                                             ReasonCode.RESERVATION_EXPIRED)
    assert env.store.commands["request-1"].status == ExecutionStatus.NOT_SENT
    assert env.repository.sessions["use-1"].state == SessionState.CLOSED
    assert env.service.can_execute("locker-1")


@pytest.mark.parametrize("age", [10, 100])
def test_FR19_pending_expired_during_downtime_cancelled(env, age):
    rental(env, SessionState.RESERVED)
    command(env, ExecutionStatus.PENDING, age=age)
    env.service.restore()
    assert [c[:3] for c in env.store.cancel_calls] == [
        ("request-1", ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED)]
    assert env.store.usages == {} or all(
        u.state == ApprovalUsageState.RELEASED for _, u in env.store.usages.values())
    assert not env.service.can_execute("locker-1")
    status(env)
    assert env.service.can_execute("locker-1")
    assert len(env.store.cancel_calls) == 1


def test_FR20_pending_uses_stored_time_and_settings_snapshot(env):
    saved = command(env, ExecutionStatus.PENDING, age=4)
    env.service.restore()
    env.settings[0] = replace(env.settings[0], reservation_wait_sec=1000)
    advance(env, 6)
    env.service.tick()
    assert env.store.cancel_calls[0][3]["reserved_at"] == saved.reserved_at.isoformat()
    assert env.store.commands["request-1"].reserved_at == saved.reserved_at


@pytest.mark.parametrize("shared", [False, True])
def test_FR19_restore_request_without_command_cancelled(env, shared):
    if shared:
        light(env)
    else:
        rental(env, SessionState.RESERVED)
    pending_request(env, shared=shared)
    env.service.restore()
    node_id = "light-1" if shared else "locker-1"
    status(env, node_id)
    assert [c[:3] for c in env.store.cancel_calls] == [
        ("request-1", ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED)]
    assert env.store.usages["usage-1"][1].state == ApprovalUsageState.RELEASED
    assert env.store.commands == {}
    assert env.service.can_execute(node_id)


@pytest.mark.parametrize("verdict", [Verdict.DENY, None, Verdict.OK])
def test_FR19_restore_cancels_stale_reservation_of_failed_request(env, verdict):
    """판정이 OK가 아니거나 빈 요청은 예약 시각이 방금이어도 바로 취소한다. OK 요청은 대기한다."""
    rental(env, SessionState.RESERVED)
    pending_request(env, age=1, verdict=verdict)
    env.service.restore()
    if verdict == Verdict.OK:
        assert env.store.cancel_calls == []
        assert env.repository.sessions["use-1"].state == SessionState.RESERVED
        assert env.service.blockers("locker-1") == ("reservation_pending",)
        advance(env, env.settings[0].reservation_wait_sec)
        status(env)
        assert [c[:3] for c in env.store.cancel_calls] == [
            ("request-1", ExecutionStatus.PENDING, ReasonCode.RESERVATION_EXPIRED)]
        assert env.store.cancel_calls[0][3]["source"] == "restore_reservation_expired"
    else:
        assert [c[:3] for c in env.store.cancel_calls] == [
            ("request-1", ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR)]
        assert env.store.cancel_calls[0][3]["source"] == "restore_failed_request"
        assert env.store.cancel_calls[0][3]["reason"] == ReasonCode.INTERNAL_ERROR.value
        assert env.repository.sessions["use-1"].state == SessionState.CLOSED
        assert env.store.usages["usage-1"][1].state == ApprovalUsageState.RELEASED
        assert env.store.commands == {}
        assert not env.service.can_execute("locker-1")
        status(env)
        assert env.service.can_execute("locker-1")
        assert len(env.store.cancel_calls) == 1


@pytest.mark.parametrize("commit", [False, True])
def test_FR19_expiry_cancel_unknown_retries_same_evidence(env, commit):
    rental(env, SessionState.RESERVED)
    command(env, ExecutionStatus.PENDING, age=100)
    env.store.cancel_error, env.store.cancel_commit = StoreOutcomeUnknown("cancel"), commit
    if commit:
        env.service.restore()
    else:
        with pytest.raises(StoreError):
            env.service.restore()
        env.service.tick()
    assert env.store.cancel_calls[0][3] == env.store.cancel_calls[-1][3]
    assert env.store.commands["request-1"].status == ExecutionStatus.NOT_SENT
    status(env)
    assert env.service.can_execute("locker-1")


def test_FR19_expiry_cancel_conflict_stays_blocked(env):
    rental(env, SessionState.RESERVED)
    command(env, ExecutionStatus.PENDING, age=100)
    env.store.cancel_conflict = True
    with pytest.raises(StoreError):
        env.service.restore()
    with pytest.raises(StoreError):
        status(env)
    assert not env.service.can_execute("locker-1")


def test_FR19_orphan_approval_usage_kept_blocked(env):
    env.store.usages["usage-1"] = "locker-1", ApprovalUsageRecord(
        "usage-1", "approval-1", "missing", None, ApprovalUsageState.HELD,
    )
    env.service.restore()
    status(env)
    assert env.service.blockers("locker-1") == ("unlinked_approval_usage",)
    assert not env.service.can_execute("locker-1")


def test_FR20_restore_overdue_kept(env):
    session = rental(env)
    env.service.restore()
    advance(env, 100)
    status(env)
    assert env.service.can_execute("locker-1")
    assert env.repository.sessions["use-1"] == session
    result = env.release.get_use_status("visitor-1", "use-1")
    assert result["overtime_sec"] == 3700


def test_FR20_restore_closing_resumes_logical_close(env):
    rental(env, SessionState.CLOSING)
    env.service.restore()
    assert env.store.closes == []  # 현재 연결을 확인하기 전에는 종료를 저장하지 않는다.
    status(env)
    assert [(use_id, reason) for use_id, _, reason in env.store.closes] == [("use-1", EndReason.RELEASE)]
    assert env.repository.sessions["use-1"].state == SessionState.CLOSED
    assert env.repository.items["locker-1"].device_state == DeviceState.AVAILABLE
    assert env.service.is_restored("locker-1")
    env.service.tick()
    assert len(env.store.closes) == 1


def test_FR20_restore_closing_ignores_saved_check_evidence(env):
    session = rental(env, SessionState.CLOSING)
    env.store.override = (RestoreBundle(env.repository.items["locker-1"], env.clock(), sessions=(
        RestoredSession(session, "visitor-1", env.clock(), {"safe": True, "check_id": "old"}),
    )),)
    env.service.restore()
    status(env)
    assert [(use_id, reason) for use_id, _, reason in env.store.closes] == [("use-1", EndReason.RELEASE)]


def test_NFR04_restore_fault_without_recovery(env):
    node = env.repository.items["locker-1"]
    env.repository.items["locker-1"] = replace(node, device_state=DeviceState.FAULT)
    env.service.restore()
    assert len(env.store.recoveries) == 1
    assert next(iter(env.store.recoveries.values())).status == RecoveryStatus.NOT_READY


def test_NFR04_restore_keeps_recovery_id_and_evidence_without_trusting_it(env):
    node = env.repository.items["locker-1"]
    env.repository.items["locker-1"] = replace(node, device_state=DeviceState.FAULT)
    old_at = env.clock() - timedelta(hours=1)
    record = RecoveryRecord("recovery-old", "locker-1", (), (), None, old_at, RecoveryStatus.READY,
                            (RecoveryEvidence("old", "old-connection", old_at,
                                              execution_outcome=CommandOutcome.UNKNOWN, command_idle=True),))
    env.store.recoveries[record.recovery_id] = record
    env.service.restore()
    status(env)
    assert env.store.recoveries == {record.recovery_id: record}
    assert not env.service.is_restored("locker-1")
    assert not env.service.can_execute("locker-1")


@pytest.mark.parametrize("committed", [False, True])
def test_NFR04_recovery_save_failure_preserves_id_on_retry(env, committed):
    command(env)
    env.store.recovery_error = StoreOutcomeUnknown("recovery")
    env.store.recovery_commit = committed
    if committed:
        env.service.restore()
    else:
        with pytest.raises(StoreError):
            env.service.restore()
    first_id = env.store.recovery_saves[0].recovery_id
    env.service.tick()
    assert set(env.store.recoveries) == {first_id}
    assert len(env.store.result_calls) == 1


def test_NFR04_retry_does_not_create_duplicate_recovery(env):
    command(env)
    env.service.restore()
    for _ in range(3):
        env.service.tick()
    assert len(env.store.recoveries) == 1
    assert len(env.store.result_calls) == 1


def test_FR19_old_connection_and_pre_restart_status_cannot_unlock(env):
    env.service.restore()
    status(env, session_id="old-connection")
    assert not env.service.can_execute("locker-1")
    status(env, received_at=env.clock() - timedelta(seconds=1))
    assert not env.service.can_execute("locker-1")
    status(env)
    assert env.service.can_execute("locker-1")


@pytest.mark.parametrize("change", ["future", "old", "busy", "unknown"])
def test_FR19_unconfirmed_status_does_not_unlock(env, change):
    env.service.restore()
    values = {}
    if change == "future":
        values["received_at"] = env.clock() + timedelta(seconds=1)
    elif change == "old":
        values["received_at"] = env.clock() - timedelta(seconds=100)
    else:
        values["command_state"] = CommandState.BUSY if change == "busy" else CommandState.UNKNOWN
    status(env, **values)
    assert not env.service.can_execute("locker-1")


def test_FR19_online_ttl_expiry_blocks_even_after_restore(env):
    env.service.restore()
    status(env)
    advance(env, env.settings[0].online_ttl_sec + 1)
    assert not env.service.can_execute("locker-1")
    status(env)
    assert env.service.can_execute("locker-1")


def test_FR19_reconnect_requires_new_current_status(env):
    env.service.restore()
    status(env)
    node = env.repository.items["locker-1"]
    env.repository.items["locker-1"] = replace(node, session_id="connection-2")
    assert not env.service.can_execute("locker-1")
    advance(env, 1)
    status(env)
    assert env.service.can_execute("locker-1")


def test_FR19_stale_sequence_cannot_replace_busy_report(env):
    env.service.restore()
    old = status(env)
    advance(env, 1)
    status(env, command_state=CommandState.BUSY)
    env.service.on_status(replace(old, received_at=env.clock()))
    assert not env.service.can_execute("locker-1")


def test_FR20_restore_does_not_restore_light_holders(env):
    light(env)
    env.service.restore()
    advance(env, 100)
    assert env.light.tick() == ()
    status(env, "light-1")
    assert env.service.can_execute("light-1")
    assert env.light.tick() == ()
    advance(env, 14)
    status(env, "light-1")
    assert env.light.tick() == ()
    advance(env, 1)
    status(env, "light-1")
    assert env.light.tick() == ("light-1",)


def test_FR20_duplicate_restore_does_not_extend_initial_wait(env):
    light(env)
    env.service.restore()
    status(env, "light-1")
    advance(env, 10)
    env.service.restore()
    status(env, "light-1")
    advance(env, 5)
    status(env, "light-1")
    assert env.light.tick() == ("light-1",)


def test_FR19_failure_in_one_device_does_not_stop_other_restore(env):
    light(env)
    rental(env, SessionState.CLOSING)
    env.service.restore()
    original = env.release.resume_close
    env.release.resume_close = lambda use_id: (_ for _ in ()).throw(StoreNotApplied("handover"))
    with pytest.raises(StoreError):
        status(env)
    status(env, "light-1")
    assert env.service.can_execute("light-1")
    assert not env.service.is_restored("locker-1")
    env.release.resume_close = original
    env.service.tick()
    assert len(env.store.closes) == 1


def test_FR19_restore_does_not_reclassify_runtime_commands(env):
    env.service.restore()
    status(env)
    command(env)
    env.service.tick()
    assert env.store.result_calls == []


def test_NFR03_concurrent_restore_settles_once(env):
    command(env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: env.service.restore(), range(8)))
    assert len(env.store.result_calls) == 1
    assert len(env.store.recoveries) == 1


def test_FR19_unknown_device_stays_blocked(env):
    env.service.restore()
    assert not env.service.can_execute("unknown")


@pytest.mark.parametrize("kind", ["duplicate", "wrong_node"])
def test_FR19_invalid_snapshot_blocks_every_device(env, kind):
    saved = command(env)
    bundle = RestoreBundle(env.repository.items["locker-1"], env.clock(),
                           commands=(replace(saved, node_id="other") if kind == "wrong_node" else saved,))
    env.store.override = (bundle, bundle) if kind == "duplicate" else (bundle,)
    with pytest.raises(StoreError):
        env.service.restore()
    assert env.service.blockers("locker-1") == ("restore_not_loaded",)
    assert env.store.result_calls == []


def complete_recovery(env, record):
    settlement = RecoverySettlement(
        record.recovery_id, record.node_id, "owner-1", env.clock(), "fake-digest",
        DeviceState.FAULT, record.status, {}, {}, record.evidence, (),
        use_id=record.use_id, closed_at=env.clock() if record.use_id else None,
        end_reason=EndReason.FAULT if record.use_id else None,
    )
    env.store.recoveries[record.recovery_id] = replace(
        record, status=RecoveryStatus.RECOVERED, completion=RecoveryCompletion(settlement, env.clock()),
    )
    node = env.repository.items[record.node_id]
    env.repository.items[record.node_id] = replace(node, device_state=DeviceState.AVAILABLE)
    if record.use_id is not None:
        session = env.repository.sessions.get(record.use_id)
        if session is not None:
            env.repository.sessions[record.use_id] = replace(session, state=SessionState.CLOSED,
                closed_at=env.clock(), end_reason=EndReason.FAULT)


def test_FR19_new_reservation_after_restore_can_reach_execution(env):
    env.service.restore()
    status(env)
    rental(env, SessionState.RESERVED)
    assert env.service.can_execute("locker-1")


def test_NFR04_completed_recovery_rechecks_current_status(env):
    rental(env, SessionState.RESERVED)
    command(env)
    env.service.restore()
    record = next(iter(env.store.recoveries.values()))
    advance(env, 30)
    complete_recovery(env, record)
    env.service.tick()
    assert not env.service.can_execute("locker-1")
    status(env)
    assert env.service.can_execute("locker-1")
    assert len(env.store.result_calls) == 1
    assert len(env.store.recoveries) == 1


def test_NFR04_completed_recovery_handles_unlinked_pending_request(env):
    light(env)
    request = pending_request(env, shared=True)
    node = env.repository.items["light-1"]
    env.repository.items["light-1"] = replace(node, device_state=DeviceState.FAULT)
    env.service.restore()
    record = next(iter(env.store.recoveries.values()))
    assert record.internal_request_ids == (request.internal_request_id,)
    complete_recovery(env, record)
    status(env, "light-1")
    assert env.service.can_execute("light-1")


def test_NFR04_recovery_record_missing_is_not_completion(env):
    command(env)
    env.service.restore()
    env.store.recoveries.clear()
    with pytest.raises(StoreError):
        env.service.tick()
    assert not env.service.can_execute("locker-1")


def test_NFR04_changed_recovery_target_stays_blocked(env):
    command(env)
    env.service.restore()
    record = next(iter(env.store.recoveries.values()))
    env.store.recoveries[record.recovery_id] = replace(record, command_ids=("other",))
    with pytest.raises(StoreError):
        env.service.tick()
    assert not env.service.can_execute("locker-1")


def test_FR19_same_status_redelivery_retries_failed_handover(env):
    rental(env, SessionState.CLOSING)
    env.service.restore()
    original = env.release.resume_close
    reports = []
    def failed(use_id):
        reports.append(use_id)
        raise StoreNotApplied("handover")
    env.release.resume_close = failed
    now = env.clock()
    report = NodeStatus("locker-1", now, "connection-1", 1, now, now,
                        CommandState.IDLE)
    with pytest.raises(StoreError):
        env.service.on_status(report)
    env.release.resume_close = original
    env.service.on_status(report)
    assert env.service.is_restored("locker-1")
    assert len(env.store.closes) == 1
    assert reports == ["use-1"]


def test_FR19_shared_light_handover_failure_stays_blocked_and_retries(env):
    light(env)
    env.service.restore()
    original = env.light.on_status
    env.light.on_status = lambda report: (_ for _ in ()).throw(StoreError("handover"))
    with pytest.raises(StoreError):
        status(env, "light-1")
    assert not env.service.is_restored("light-1")
    env.light.on_status = original
    env.service.tick()
    assert env.service.can_execute("light-1")


def test_FR19_tick_continues_other_device_after_handover_failure(env):
    light(env)
    rental(env, SessionState.CLOSING)
    env.service.restore()
    original_release, original_light = env.release.resume_close, env.light.resume
    env.release.resume_close = lambda use_id: (_ for _ in ()).throw(StoreError("release"))
    env.light.resume = lambda node_id: (_ for _ in ()).throw(StoreError("light"))
    with pytest.raises(StoreError):
        status(env)
    with pytest.raises(StoreError):
        status(env, "light-1")
    env.light.resume = original_light
    with pytest.raises(StoreError, match="release"):
        env.service.tick()
    assert env.service.can_execute("light-1")
    assert not env.service.is_restored("locker-1")
    env.release.resume_close = original_release


def test_FR19_late_saved_success_before_restore_not_overwritten(env):
    session = rental(env)
    saved = command(env)
    env.store.override = (RestoreBundle(env.repository.items["locker-1"], env.clock(),
        commands=(saved,), sessions=(RestoredSession(session, "visitor-1", env.clock()),)),)
    env.store.commands["request-1"] = replace(saved, status=ExecutionStatus.SUCCEEDED,
                                               outcome=CommandOutcome.SUCCEEDED)
    env.service.restore()
    status(env)
    assert env.service.can_execute("locker-1")
    assert env.store.result_calls == []


def test_FR19_missing_session_is_not_empty_occupancy(env):
    rental(env)
    env.service.restore()
    env.repository.sessions.clear()
    with pytest.raises(StoreError):
        status(env)
    assert not env.service.is_restored("locker-1")


def test_FR19_snapshot_iteration_failure_does_not_publish_partial_restore(env):
    def broken():
        yield RestoreBundle(env.repository.items["locker-1"], env.clock())
        raise StoreNotApplied("snapshot")
    env.store.override = broken()
    with pytest.raises(StoreError):
        env.service.restore()
    assert env.service.blockers("locker-1") == ("restore_not_loaded",)


def test_FR19_repository_duplicates_block_initial_restore(env):
    node = env.repository.items["locker-1"]
    env.repository.nodes = lambda capability=None: (node, node)
    with pytest.raises(StoreError):
        env.service.restore()
    assert env.service.blockers("locker-1") == ("restore_not_loaded",)


def test_FR19_restore_service_is_execution_readiness(env):
    from nearpilot.shared.interfaces import ExecutionReadiness
    assert isinstance(env.service, ExecutionReadiness)
    assert env.service.can_execute("locker-1") is False


def test_FR07_restore_late_success_keeps_fault(env):
    from nearpilot.core.lifecycle.execution import ExecutionService
    from nearpilot.shared.enums import NodeResultState
    from nearpilot.shared.models import ExecutionContext, NodeResult

    rental(env, SessionState.RESERVED)
    saved = command(env)
    env.service.restore()
    assert env.repository.items["locker-1"].device_state == DeviceState.FAULT
    ctx = ExecutionContext("request-1", UseRequest("visitor-1", "external-1", TargetSpec.parse("locker-1"),
        Action.OPEN, "test", saved.reserved_at), "locker-1", "use-1", "usage-1", Settings(),
        reserved_at=saved.reserved_at)
    execution = ExecutionService(repository=env.repository, store=env.store, rechecker=None, gateway=None,
        clock=env.clock, lock_for=lambda node_id: RLock(), can_execute=env.service.can_execute,
        record_isolated=env.recovery.record_isolated)
    env.store.result_conflict = True
    now = env.clock()
    execution.on_result(ctx, NodeResult("locker-1", "connection-1", 9, now, now, saved.command_id,
                                         "late-event", NodeResultState.SUCCEEDED, now))
    assert env.store.result_calls[-1][1] == CommandOutcome.SUCCEEDED
    assert env.repository.items["locker-1"].device_state == DeviceState.FAULT
    assert len(env.store.recoveries) == 1
