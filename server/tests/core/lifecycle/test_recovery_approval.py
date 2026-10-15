"""복구 준비 조회와 복구 승인 (FR-12A, NFR-04). DB·IOT는 shared 계약의 fake다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Lock, RLock
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.fault_recovery import (
    FaultRecoveryService, NotPlaceOwner, settlement_digest,
)
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, ActuatorState, ApprovalUsageState, Capability, CommandOutcome, DeviceState, EndReason,
    ExecutionKind, ExecutionStatus, Occupancy, RecoveryBlocker, RecoveryOutcome, RecoveryReason,
    RecoveryStatus, SafetyCheckOutcome, SessionState, StoreOutcome, TrustState,
)
from nearpilot.shared.errors import StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.interfaces import RecoveryService
from nearpilot.shared.models import (
    ApprovalUsageRecord, CheckCriteria, CommandRecord, NodeInfo, NodeStatus, PhysicalState,
    RecoveryCompletion, RecoveryEvidence, RecoveryRecord, RestoreBundle, SafetyCheckResult,
    StoreResult, UseSession,
)

LOCKER_CRITERIA = {"check_criteria": {"id": "locker-return", "version": 1}}


class RepositoryFake:
    def __init__(self, at):
        self.node = NodeInfo("locker-1", "place-1", Capability.LOCKER, Occupancy.RENTAL,
                             TrustState.TRUSTED, DeviceState.FAULT, None, at,
                             release_policy=LOCKER_CRITERIA, session_id="connection-1")
        self.use = UseSession("use-1", "request-1", "locker-1", SessionState.ACTIVE,
                              rental_due_at=at, release_policy=LOCKER_CRITERIA)
        self.owner_calls = []

    def nodes(self, capability=None):
        return (self.node,)

    def session(self, use_id):
        return self.use if self.use is not None and use_id == self.use.use_id else None

    def is_place_owner(self, user_id, node_id):
        self.owner_calls.append((user_id, node_id))
        return user_id == "owner-1" and node_id == self.node.node_id


class StoreFake:
    def __init__(self, repository, clock):
        self.repository, self.clock = repository, clock
        self.records, self.commands, self.usages = {}, {}, []
        self.saves, self.settlements = [], []
        self.settle_error, self.settle_commit, self.settle_conflict = None, False, False
        self.lock = Lock()

    def restore_pending(self):
        return (RestoreBundle(self.repository.node, self.clock(),
                              recoveries=tuple(self.records.values()),
                              approval_usages=tuple(self.usages)),)

    def recovery(self, recovery_id):
        return self.records.get(recovery_id)

    def save_recovery(self, record):
        self.saves.append(record)
        self.records[record.recovery_id] = record
        return StoreResult(StoreOutcome.APPLIED, recovery=record)

    def execution(self, internal_request_id):
        return self.commands.get(internal_request_id)

    def settle_recovery(self, settlement):
        with self.lock:
            self.settlements.append(settlement)
            current = self.records[settlement.recovery_id]
            if current.completion is not None:
                same = current.completion.settlement.settlement_digest == settlement.settlement_digest
                return StoreResult(StoreOutcome.ALREADY_APPLIED if same else StoreOutcome.CONFLICT,
                                   recovery=current)
            if self.settle_conflict:
                return StoreResult(StoreOutcome.CONFLICT, recovery=current)
            error, self.settle_error = self.settle_error, None
            if error and not self.settle_commit:
                raise error
            done = replace(current, status=RecoveryStatus.RECOVERED,
                           completion=RecoveryCompletion(settlement, self.clock()))
            self.records[settlement.recovery_id] = done
            self.repository.node = replace(self.repository.node, device_state=DeviceState.AVAILABLE)
            if settlement.use_id is not None:
                self.repository.use = replace(self.repository.use, state=SessionState.CLOSED)
            if error:
                raise error
            return StoreResult(StoreOutcome.APPLIED, recovery=done)


class GatewayFake:
    def __init__(self):
        self.requests = []

    def request_check(self, request):
        self.requests.append(request)


@pytest.fixture
def env():
    time = [datetime(2026, 10, 15, tzinfo=timezone.utc)]
    clock = lambda: time[0]
    repository = RepositoryFake(clock())
    store = StoreFake(repository, clock)
    gateway = GatewayFake()
    locks, index_lock = {}, Lock()

    def lock_for(node_id):
        with index_lock:
            return locks.setdefault(node_id, RLock())

    ids = iter(f"check-{n}" for n in range(1, 1000))
    service = FaultRecoveryService(repository=repository, store=store, clock=clock, lock_for=lock_for,
                                   gateway=gateway, settings=Settings, new_id=lambda: next(ids))
    env = SimpleNamespace(time=time, clock=clock, repository=repository, store=store,
                          gateway=gateway, service=service, seq=0)
    fault(env)
    return env


def fault(env, *, outcome=ExecutionStatus.UNKNOWN, attempted=True, use=True):
    command = CommandRecord("command-1", "request-1", "locker-1", Action.OPEN, outcome,
                            ExecutionKind.RENTAL_INITIAL, "use-1" if use else None, "usage-1",
                            env.clock() - timedelta(seconds=30), send_attempted=attempted,
                            outcome=None if outcome in (ExecutionStatus.PENDING, ExecutionStatus.IN_PROGRESS)
                            else CommandOutcome(outcome.value))
    env.store.commands["request-1"] = command
    env.store.usages = [ApprovalUsageRecord("usage-1", "approval-1", "request-1", "command-1",
                                            ApprovalUsageState.HELD)]
    record = RecoveryRecord("recovery-1", "locker-1", ("request-1",), ("command-1",),
                            "use-1" if use else None, env.clock() - timedelta(seconds=20),
                            RecoveryStatus.NOT_READY,
                            (RecoveryEvidence("result-1", "connection-1", env.clock() - timedelta(seconds=25),
                                              execution_outcome=CommandOutcome.UNKNOWN),))
    env.store.records = {record.recovery_id: record}
    if not use:
        env.repository.use = None


def advance(env, seconds):
    env.time[0] += timedelta(seconds=seconds)


def online(env):
    env.repository.node = replace(env.repository.node, last_seen_at=env.clock())


def check_result(env, request=None, outcome=SafetyCheckOutcome.PASSED, **changes):
    request = request or env.gateway.requests[-1]
    advance(env, 1)
    online(env)
    now = env.clock()
    items = {"door_closed": True, "locked": outcome == SafetyCheckOutcome.PASSED}
    evidence = {k: {"value": v, "observed_at": now.isoformat()} for k, v in items.items()}
    env.seq += 1
    result = SafetyCheckResult("locker-1", request.target_session_id, env.seq, now, now,
                               request.check_id, "check-event-" + request.check_id, request.criteria,
                               now, outcome, evidence)
    return replace(result, **changes)


def status(env, **physical):
    advance(env, 1)
    online(env)
    now = env.clock()
    env.seq += 1
    state = replace(PhysicalState(True, None, ActuatorState.IDLE, False), **physical)
    report = NodeStatus("locker-1", now, env.repository.node.session_id, env.seq, now, now, state)
    env.service.on_status(report)
    return report


def make_ready(env):
    online(env)
    env.service.readiness("owner-1", "locker-1")
    assert env.service.on_check_result(check_result(env))
    status(env)
    readiness = env.service.readiness("owner-1", "locker-1")
    assert (readiness.status, readiness.blockers) == (RecoveryStatus.READY, ())
    return readiness


def test_NFR04_recovery_service_contract(env):
    assert isinstance(env.service, RecoveryService)


def test_NFR04_readiness_without_recovery(env):
    env.store.records = {}
    online(env)
    readiness = env.service.readiness("owner-1", "locker-1")
    assert (readiness.recovery_id, readiness.status, readiness.blockers) == (
        None, RecoveryStatus.NOT_READY, (RecoveryBlocker.NO_RECOVERY_REQUIRED,))
    assert env.store.saves == [] and env.gateway.requests == []


def test_NFR04_readiness_not_owner(env):
    online(env)
    with pytest.raises(NotPlaceOwner) as raised:
        env.service.readiness("visitor-1", "locker-1")
    assert "locker-1" not in str(raised.value)
    assert env.gateway.requests == []
    assert env.repository.owner_calls == [("visitor-1", "locker-1")]


def test_NFR04_readiness_does_not_change_state(env):
    online(env)
    readiness = env.service.readiness("owner-1", "locker-1")
    assert readiness.status == RecoveryStatus.NOT_READY
    assert set(readiness.blockers) == {RecoveryBlocker.SAFETY_UNCONFIRMED,
                                       RecoveryBlocker.PHYSICAL_STATE_UNSYNCED}
    request = env.gateway.requests[0]
    assert (request.target_session_id, request.criteria) == ("connection-1", CheckCriteria("locker-return", 1))
    # 응답 기한 안의 재조회는 검사를 다시 보내지 않는다.
    env.service.readiness("owner-1", "locker-1")
    assert len(env.gateway.requests) == 1
    assert env.store.saves == [] and env.store.settlements == []
    assert env.repository.node.device_state == DeviceState.FAULT
    make_ready(env)
    assert env.store.settlements == []
    assert env.repository.node.device_state == DeviceState.FAULT


def test_NFR04_safety_and_sync_evidence_saved_to_recovery(env):
    readiness = make_ready(env)
    saved = env.store.records["recovery-1"].evidence
    assert any(e.safety_confirmed for e in saved)
    assert any(e.physical_state_synced for e in saved)
    assert set(readiness.evidence) <= set(saved)


def test_NFR04_status_before_safety_is_not_sync(env):
    online(env)
    env.service.readiness("owner-1", "locker-1")
    status(env)
    env.service.on_check_result(check_result(env))
    readiness = env.service.readiness("owner-1", "locker-1")
    assert readiness.blockers == (RecoveryBlocker.PHYSICAL_STATE_UNSYNCED,)
    status(env, actuator_state=ActuatorState.BUSY)
    status(env, sensor_fault=True)
    assert env.service.readiness("owner-1", "locker-1").blockers == (RecoveryBlocker.PHYSICAL_STATE_UNSYNCED,)


@pytest.mark.parametrize("change", ["missing_item", "other_connection", "other_criteria", "late"])
def test_NFR04_invalid_check_result_is_not_safety(env, change):
    online(env)
    env.service.readiness("owner-1", "locker-1")
    result = check_result(env)
    if change == "missing_item":
        result = replace(result, evidence={"door_closed": result.evidence["door_closed"]})
    elif change == "other_connection":
        result = replace(result, session_id="connection-2")
    elif change == "other_criteria":
        result = replace(result, criteria=CheckCriteria("locker-return", 2))
    else:
        result = replace(result, received_at=env.clock() + timedelta(seconds=Settings().safety_check_timeout_sec))
    assert env.service.on_check_result(result)
    assert RecoveryBlocker.SAFETY_UNCONFIRMED in env.service.readiness("owner-1", "locker-1").blockers
    assert env.store.saves == []


def test_NFR04_not_ready_check_requests_again(env):
    online(env)
    env.service.readiness("owner-1", "locker-1")
    env.service.on_check_result(check_result(env, outcome=SafetyCheckOutcome.NOT_READY))
    env.service.readiness("owner-1", "locker-1")
    assert [r.check_id for r in env.gateway.requests] == ["check-1", "check-2"]


def test_NFR04_unrelated_check_result_goes_to_release(env):
    online(env)
    env.service.readiness("owner-1", "locker-1")
    other = replace(check_result(env), check_id="release-check")
    assert env.service.on_check_result(other) is False


def test_NFR04_approve_settles_once(env):
    make_ready(env)
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.COMPLETED, RecoveryReason.OK)
    assert result.readiness.status == RecoveryStatus.RECOVERED
    assert result.completed_at == env.clock()
    [settlement] = env.store.settlements
    assert settlement.expected_node_state == DeviceState.FAULT
    assert settlement.expected_commands == {"command-1": ExecutionStatus.UNKNOWN}
    assert settlement.expected_sessions == {"use-1": SessionState.ACTIVE}
    assert (settlement.use_id, settlement.closed_at, settlement.end_reason) == (
        "use-1", env.clock(), EndReason.FAULT)
    # 확인 불가를 미실행으로 보지 않는다.
    assert [(u.usage_id, u.expected_state, u.state) for u in settlement.usage_settlements] == [
        ("usage-1", ApprovalUsageState.HELD, ApprovalUsageState.CONSUMED)]
    assert settlement.settlement_digest == settlement_digest(
        recovery_id="recovery-1", node_id="locker-1", expected_node_state=DeviceState.FAULT,
        expected_recovery_status=RecoveryStatus.NOT_READY,
        expected_commands=settlement.expected_commands, expected_sessions=settlement.expected_sessions,
        evidence=settlement.evidence, usage_settlements=settlement.usage_settlements,
        use_id="use-1", closed_at=env.clock(), end_reason=EndReason.FAULT)
    assert {e.evidence_id for e in settlement.evidence} >= {"result-1"}


@pytest.mark.parametrize("outcome,attempted,expected", [
    (ExecutionStatus.FAILED, True, ApprovalUsageState.RELEASED),
    (ExecutionStatus.SUCCEEDED, True, ApprovalUsageState.CONSUMED),
    (ExecutionStatus.UNKNOWN, True, ApprovalUsageState.CONSUMED),
])
def test_FR19_usage_settlement_follows_execution_result(env, outcome, attempted, expected):
    fault(env, outcome=outcome, attempted=attempted)
    make_ready(env)
    env.service.approve("owner-1", "locker-1", "recovery-1")
    assert env.store.settlements[0].usage_settlements[0].state == expected


def test_FR12A_recovery_service_uses_authenticated_account(env):
    make_ready(env)
    env.service.approve("owner-1", "locker-1", "recovery-1")
    assert env.store.settlements[0].approved_by_user_id == "owner-1"
    assert env.store.settlements[0].approved_at == env.clock()
    assert ("owner-1", "locker-1") in env.repository.owner_calls


def test_NFR04_approve_not_owner(env):
    make_ready(env)
    result = env.service.approve("visitor-1", "locker-1", "recovery-1")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.REJECTED, RecoveryReason.NOT_OWNER)
    assert (result.readiness.recovery_id, result.readiness.evidence) == (None, ())
    assert env.store.settlements == []


def test_NFR04_approve_unconfirmed_execution(env):
    fault(env, outcome=ExecutionStatus.IN_PROGRESS)
    online(env)
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.REJECTED, RecoveryReason.NOT_READY)
    assert RecoveryBlocker.EXECUTION_UNCONFIRMED in result.readiness.blockers
    assert env.store.settlements == [] and env.gateway.requests == []


def test_NFR04_approve_offline(env):
    make_ready(env)
    advance(env, Settings().online_ttl_sec + 1)
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.REJECTED, RecoveryReason.NOT_READY)
    assert RecoveryBlocker.OFFLINE in result.readiness.blockers
    assert env.store.settlements == []


def test_NFR04_approve_untrusted(env):
    make_ready(env)
    env.repository.node = replace(env.repository.node, trust_state=TrustState.DISCOVERED)
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert RecoveryBlocker.NOT_TRUSTED in result.readiness.blockers
    assert env.store.settlements == []


def test_NFR04_approve_recovery_mismatch(env):
    make_ready(env)
    result = env.service.approve("owner-1", "locker-1", "recovery-other")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.REJECTED, RecoveryReason.RECOVERY_MISMATCH)
    assert env.store.settlements == []


def test_NFR03_concurrent_recovery_approve_once(env):
    make_ready(env)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: env.service.approve("owner-1", "locker-1", "recovery-1"), range(16)))
    assert len(env.store.settlements) == 1
    assert {r.outcome for r in results} == {RecoveryOutcome.COMPLETED}


def test_NFR04_repeat_approve_returns_completed(env):
    make_ready(env)
    first = env.service.approve("owner-1", "locker-1", "recovery-1")
    advance(env, 60)
    again = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert (again.outcome, again.completed_at) == (RecoveryOutcome.COMPLETED, first.completed_at)
    assert len(env.store.settlements) == 1
    denied = env.service.approve("visitor-1", "locker-1", "recovery-1")
    assert denied.reason_code == RecoveryReason.NOT_OWNER
    # 과거 복구의 완료는 현재 복구 준비가 아니다.
    readiness = env.service.readiness("owner-1", "locker-1")
    assert readiness.blockers == (RecoveryBlocker.NO_RECOVERY_REQUIRED,)


@pytest.mark.parametrize("commit", [False, True])
def test_NFR04_settle_unknown_pending(env, commit):
    make_ready(env)
    env.store.settle_error, env.store.settle_commit = StoreOutcomeUnknown("settle"), commit
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    if commit:
        assert result.outcome == RecoveryOutcome.COMPLETED
        return
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.PENDING, RecoveryReason.STORE_OUTCOME_UNKNOWN)
    assert result.readiness.status == RecoveryStatus.RECOVERING
    assert env.service.readiness("owner-1", "locker-1").status == RecoveryStatus.RECOVERING
    first = env.store.settlements[0]
    advance(env, 1)
    status(env)
    again = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert again.outcome == RecoveryOutcome.COMPLETED
    assert env.store.settlements[-1] == first


def test_NFR04_settle_not_applied(env):
    make_ready(env)
    env.store.settle_error = StoreNotApplied("settle")
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.REJECTED, RecoveryReason.STORE_NOT_APPLIED)
    assert env.store.records["recovery-1"].completion is None
    assert env.service.approve("owner-1", "locker-1", "recovery-1").outcome == RecoveryOutcome.COMPLETED


def test_NFR04_settle_conflict_rejected(env):
    make_ready(env)
    env.store.settle_conflict = True
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.REJECTED, RecoveryReason.STATE_CONFLICT)


def test_NFR04_reconnect_invalidates_evidence(env):
    make_ready(env)
    env.repository.node = replace(env.repository.node, session_id="connection-2")
    result = env.service.approve("owner-1", "locker-1", "recovery-1")
    assert (result.outcome, result.reason_code) == (RecoveryOutcome.REJECTED, RecoveryReason.NOT_READY)
    assert RecoveryBlocker.SAFETY_UNCONFIRMED in result.readiness.blockers
    assert env.gateway.requests[-1].target_session_id == "connection-2"
    assert env.store.settlements == []


def test_NFR04_new_fault_invalidates_evidence(env):
    make_ready(env)
    record = env.store.records.pop("recovery-1")
    env.store.records["recovery-2"] = replace(record, recovery_id="recovery-2")
    readiness = env.service.readiness("owner-1", "locker-1")
    assert readiness.recovery_id == "recovery-2"
    assert RecoveryBlocker.SAFETY_UNCONFIRMED in readiness.blockers


def test_NFR04_shared_recovery_uses_node_criteria_and_no_close(env):
    fault(env, use=False)
    make_ready(env)
    assert env.gateway.requests[0].criteria == CheckCriteria("locker-return", 1)
    env.service.approve("owner-1", "locker-1", "recovery-1")
    settlement = env.store.settlements[0]
    assert (settlement.use_id, settlement.closed_at, settlement.expected_sessions) == (None, None, {})


def test_NFR04_missing_criteria_stays_unconfirmed(env):
    fault(env, use=False)
    env.repository.node = replace(env.repository.node, release_policy={})
    online(env)
    readiness = env.service.readiness("owner-1", "locker-1")
    assert RecoveryBlocker.SAFETY_UNCONFIRMED in readiness.blockers
    assert env.gateway.requests == []
