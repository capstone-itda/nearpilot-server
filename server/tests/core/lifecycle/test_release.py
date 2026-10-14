"""반납 계약의 단위 검증. DB와 IOT는 shared 계약의 fake로 대신한다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock, RLock
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Capability, DeviceState, EndReason, Occupancy, ReasonCode,
    SafetyCheckOutcome, SessionState, StoreConflictReason, StoreOutcome,
    TrustState, Verdict,
)
from nearpilot.shared.errors import StoreError, StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.models import (
    CheckCriteria, CloseResult, NodeInfo, SafetyCheckResult, StoreResult, UseSession,
)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class RepositoryFake:
    def __init__(self, clock):
        self.owner_checks = []
        self.owners = {"use-1": "visitor-1"}
        self.sessions = {"use-1": UseSession(
            "use-1", "internal-1", "locker-1", SessionState.ACTIVE,
            rental_due_at=clock() + timedelta(seconds=10),
            release_policy={"check_criteria": {"id": "locker-return", "version": 1}},
        )}
        self.node_infos = {"locker-1": NodeInfo(
            "locker-1", "place-1", Capability.LOCKER, Occupancy.RENTAL,
            TrustState.TRUSTED, DeviceState.ACTIVE, None, clock(), session_id="connection-1",
        )}

    def session_owner_matches(self, use_id, user_id):
        self.owner_checks.append((use_id, user_id))
        return self.owners.get(use_id) == user_id

    def session(self, use_id):
        return self.sessions.get(use_id)

    def nodes(self, capability=None):
        return tuple(self.node_infos.values())

    def set_node(self, **changes):
        self.node_infos["locker-1"] = replace(self.node_infos["locker-1"], **changes)

    def set_session(self, **changes):
        self.sessions["use-1"] = replace(self.sessions["use-1"], **changes)


class StoreFake:
    def __init__(self, repository):
        self.repository = repository
        self.begins = []
        self.finishes = []
        self.isolations = []
        self.close_reads = []
        self.completed = {}
        self.begin_conflict = None
        self.begin_error = None
        self.finish_error = None
        self.commit_before_error = False
        self.finish_conflict = False
        self.finish_already_applied = False
        self.isolation_error = None
        self.close_error = None

    def begin_close(self, use_id, reason):
        self.begins.append((use_id, reason))
        if self.begin_error:
            raise self.begin_error
        if self.begin_conflict:
            return StoreResult(StoreOutcome.CONFLICT, conflict_reason=self.begin_conflict)
        self.repository.set_session(state=SessionState.CLOSING)
        self.repository.set_node(device_state=DeviceState.CLOSING)
        return StoreResult(StoreOutcome.APPLIED, session=self.repository.session(use_id))

    def finish_close(self, use_id, closed_at, check_id, evidence):
        self.finishes.append((use_id, closed_at, check_id, evidence))
        if self.finish_conflict:
            return StoreResult(StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.STATE_CONFLICT)
        if self.finish_error and not self.commit_before_error:
            raise self.finish_error
        self.completed[use_id] = CloseResult(use_id, closed_at, EndReason.RELEASE, check_id, evidence)
        self.repository.set_session(state=SessionState.CLOSED, closed_at=closed_at, end_reason=EndReason.RELEASE)
        self.repository.set_node(device_state=DeviceState.AVAILABLE)
        if self.finish_error:
            raise self.finish_error
        return StoreResult(StoreOutcome.ALREADY_APPLIED if self.finish_already_applied else StoreOutcome.APPLIED)

    def close_result(self, use_id):
        self.close_reads.append(use_id)
        if self.close_error:
            raise self.close_error
        return self.completed.get(use_id)

    def isolate_device(self, node_id, evidence):
        self.isolations.append((node_id, evidence))
        if self.isolation_error:
            raise self.isolation_error
        already_fault = self.repository.node_infos[node_id].device_state == DeviceState.FAULT
        self.repository.set_node(device_state=DeviceState.FAULT)
        return StoreResult(StoreOutcome.ALREADY_APPLIED if already_fault else StoreOutcome.APPLIED)


class GatewayFake:
    def __init__(self):
        self.requests = []
        self.error = None
        self.callback = None

    def request_check(self, request):
        self.requests.append(request)
        if self.callback:
            self.callback(request)
        if self.error:
            raise self.error


@pytest.fixture
def env():
    clock = Clock()
    repository = RepositoryFake(clock)
    store = StoreFake(repository)
    gateway = GatewayFake()
    settings = [Settings()]
    locks = {}
    lock_index = Lock()
    faults = []
    fault_errors = []

    def lock_for(node_id):
        with lock_index:
            return locks.setdefault(node_id, RLock())

    def record_fault(session, evidence):
        faults.append((session, evidence))
        if fault_errors:
            raise fault_errors.pop(0)

    service = ReleaseService(
        repository=repository, store=store, gateway=gateway,
        settings=lambda: settings[0], clock=clock, lock_for=lock_for, record_fault=record_fault,
    )
    return SimpleNamespace(
        clock=clock, repository=repository, store=store, gateway=gateway, service=service,
        settings=settings, locks=locks, lock_for=lock_for, faults=faults, fault_errors=fault_errors,
    )


def release(env):
    return env.service.release_use("visitor-1", "use-1")


def report(env, outcome=SafetyCheckOutcome.PASSED, **changes):
    request = env.gateway.requests[-1]
    value = SafetyCheckResult(
        request.node_id, request.target_session_id, 1, env.clock(), env.clock(),
        request.check_id, "event-1", request.criteria, env.clock(), outcome,
        {"door_closed": True, "actuator_state": "idle", "sensor_fault": False},
    )
    return replace(value, **changes)


def deliver(env, outcome=SafetyCheckOutcome.PASSED, **changes):
    value = report(env, outcome, **changes)
    env.service.on_safety_check_result(value)
    return value


def test_FR21_release_other_users_handle_denied(env):
    other = env.service.release_use("visitor-2", "use-1")
    missing = env.service.release_use("visitor-1", "missing")
    assert other == missing
    assert other.verdict == Verdict.DENY and other.reason == ReasonCode.USE_NOT_FOUND
    assert env.store.begins == env.gateway.requests == []


def test_FR21_release_checks_owner(env):
    release(env)
    release(env)
    assert len(env.repository.owner_checks) == 4
    assert all(pair == ("use-1", "visitor-1") for pair in env.repository.owner_checks)


def test_FR20_overdue_release_allowed(env):
    env.clock.advance(20)
    decision = release(env)
    assert decision.verdict == Verdict.BUSY
    assert env.store.begins == [("use-1", EndReason.RELEASE)]
    assert len(env.gateway.requests) == 1


def test_FR19_release_blocked_by_command(env):
    env.store.begin_conflict = StoreConflictReason.COMMAND_IN_PROGRESS
    decision = release(env)
    assert (decision.verdict, decision.reason) == (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS)
    assert not env.gateway.requests


def test_FR20_repeat_release_idempotent(env):
    first = release(env)
    second = release(env)
    assert first == second
    assert len(env.store.begins) == len(env.gateway.requests) == 1


def test_NFR03_concurrent_release_starts_once(env):
    barrier = Barrier(8)

    def invoke(_):
        barrier.wait(timeout=5)
        return release(env)

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(invoke, range(8)))
    assert all(result.verdict == Verdict.BUSY for result in decisions)
    assert len(env.store.begins) == len(env.gateway.requests) == 1


def test_FR20_passed_closes_session(env):
    release(env)
    result = deliver(env)
    assert env.repository.session("use-1").state == SessionState.CLOSED
    assert env.repository.node_infos["locker-1"].device_state == DeviceState.AVAILABLE
    assert env.store.finishes == [("use-1", env.clock(), result.check_id, result.evidence)]
    assert release(env).verdict == Verdict.OK


def test_FR07_status_report_not_close_evidence(env):
    release(env)
    env.clock.advance(1)
    env.service.tick()
    assert env.repository.session("use-1").state == SessionState.CLOSING
    assert not env.store.finishes


def test_FR20_request_uses_session_criteria_and_settings(env):
    env.settings[0] = Settings(safety_check_timeout_sec=7)
    env.repository.set_node(release_policy={"check_criteria": {"id": "changed", "version": 9}})
    release(env)
    request = env.gateway.requests[-1]
    assert request.criteria == CheckCriteria("locker-return", 1)
    assert request.target_session_id == "connection-1"
    assert request.expires_at - request.issued_at == timedelta(seconds=7)


def test_FR20_not_ready_retries_with_new_check_id(env):
    release(env)
    first = env.gateway.requests[-1]
    deliver(env, SafetyCheckOutcome.NOT_READY)
    env.clock.advance(0.9)
    env.service.tick()
    assert len(env.gateway.requests) == 1
    env.clock.advance(0.1)
    env.service.tick()
    assert len(env.gateway.requests) == 2
    assert env.gateway.requests[-1].check_id != first.check_id
    assert env.repository.session("use-1").state == SessionState.CLOSING


def test_FR20_not_ready_no_timeout(env):
    release(env)
    for _ in range(1800):
        deliver(env, SafetyCheckOutcome.NOT_READY)
        env.clock.advance(1)
        env.service.tick()
    assert env.repository.session("use-1").state == SessionState.CLOSING
    assert not env.store.isolations
    assert not env.store.finishes


def test_FR20_stale_check_id_ignored(env):
    release(env)
    old = report(env)
    deliver(env, SafetyCheckOutcome.NOT_READY)
    env.clock.advance(1)
    env.service.tick()
    env.service.on_safety_check_result(old)
    assert not env.store.finishes
    deliver(env)
    assert env.repository.session("use-1").state == SessionState.CLOSED


@pytest.mark.parametrize("changes", [
    {"session_id": "old-connection"},
    {"criteria": CheckCriteria("locker-return", 2)},
    {"criteria": CheckCriteria("other", 1)},
])
def test_FR20_session_mismatch_invalid_and_criteria_mismatch_invalid(env, changes):
    release(env)
    deliver(env, **changes)
    assert not env.store.finishes
    env.clock.advance(1)
    env.service.tick()
    assert len(env.gateway.requests) == 2


def test_FR07_old_safety_report_invalid(env):
    env.settings[0] = Settings(safety_check_timeout_sec=10)
    release(env)
    request = env.gateway.requests[-1]
    env.clock.advance(4)
    deliver(env, checked_at=request.issued_at)
    assert not env.store.finishes


def test_FR07_queued_report_must_still_be_fresh_when_adopted(env):
    release(env)
    value = report(env)
    env.clock.advance(4)
    env.service.on_safety_check_result(value)
    assert not env.store.finishes


def test_FR20_synchronous_result_can_complete_during_request(env):
    env.gateway.callback = lambda _: deliver(env)
    decision = release(env)
    assert decision.verdict == Verdict.OK
    assert len(env.store.finishes) == 1


def test_FR20_synchronous_fault_does_not_announce_completion(env):
    env.gateway.callback = lambda _: deliver(env, SafetyCheckOutcome.FAULT)
    assert release(env).verdict == Verdict.DENY
    assert not env.store.finishes and len(env.faults) == 1


def test_FR20_synchronous_store_failure_propagates(env):
    env.store.finish_error = StoreNotApplied("write unavailable")
    env.gateway.callback = lambda _: deliver(env)
    with pytest.raises(StoreNotApplied):
        release(env)
    env.store.finish_error = None
    env.service.tick()
    assert env.repository.session("use-1").state == SessionState.CLOSED


@pytest.mark.parametrize("offset", [-1, 1])
def test_FR07_check_must_be_measured_after_request_not_in_future(env, offset):
    release(env)
    deliver(env, checked_at=env.clock() + timedelta(seconds=offset))
    assert not env.store.finishes


def test_FR20_response_after_deadline_invalid(env):
    release(env)
    env.clock.advance(3.1)
    deliver(env)
    assert not env.store.finishes


def test_FR20_passed_at_deadline_valid(env):
    release(env)
    env.clock.advance(3)
    deliver(env)
    assert env.repository.session("use-1").state == SessionState.CLOSED


def test_FR20_missing_criteria_not_passed(env):
    env.repository.set_session(release_policy={})
    release(env)
    assert not env.gateway.requests
    env.clock.advance(30)
    env.service.tick()
    assert env.repository.node_infos["locker-1"].device_state == DeviceState.FAULT
    assert not env.store.finishes


def test_FR20_unconfirmed_limit_isolates(env):
    release(env)
    env.clock.advance(3)
    env.service.tick()
    env.clock.advance(29)
    env.service.tick()
    assert not env.store.isolations
    env.clock.advance(1)
    env.service.tick()
    assert len(env.store.isolations) == len(env.faults) == 1
    assert env.repository.session("use-1").state == SessionState.CLOSING


def test_FR20_new_check_keeps_timer(env):
    release(env)
    deliver(env, SafetyCheckOutcome.UNKNOWN)
    for _ in range(30):
        env.clock.advance(1)
        env.service.tick()
        if env.repository.node_infos["locker-1"].device_state != DeviceState.FAULT:
            deliver(env, SafetyCheckOutcome.UNKNOWN)
    assert len(env.store.isolations) == 1


def test_FR20_not_ready_clears_unconfirmed_timer(env):
    release(env)
    deliver(env, SafetyCheckOutcome.UNKNOWN)
    env.clock.advance(29)
    env.service.tick()
    deliver(env, SafetyCheckOutcome.NOT_READY)
    env.clock.advance(1)
    env.service.tick()
    deliver(env, SafetyCheckOutcome.UNKNOWN)
    assert not env.store.isolations


def test_FR20_gateway_failure_is_unconfirmed(env):
    env.gateway.error = ConnectionError("transport disconnected")
    release(env)
    env.clock.advance(30)
    env.service.tick()
    assert len(env.store.isolations) == 1
    assert not env.store.finishes


def test_FR20_no_session_is_unconfirmed(env):
    env.repository.set_node(session_id=None)
    release(env)
    assert not env.gateway.requests
    env.clock.advance(30)
    env.service.tick()
    assert len(env.store.isolations) == 1


def test_FR20_fault_isolates_immediately(env):
    release(env)
    deliver(env, SafetyCheckOutcome.FAULT)
    assert len(env.store.isolations) == len(env.faults) == 1
    assert env.repository.node_infos["locker-1"].device_state == DeviceState.FAULT
    assert not env.store.finishes
    assert release(env).verdict == Verdict.DENY


def test_NFR04_fault_keeps_session(env):
    release(env)
    before = env.repository.session("use-1")
    deliver(env, SafetyCheckOutcome.FAULT)
    assert env.repository.session("use-1") == before
    env.clock.advance(40)
    env.service.tick()
    assert len(env.gateway.requests) == 1


def test_NFR04_retry_missing_recovery_after_isolation(env):
    env.fault_errors.append(StoreNotApplied("recovery save failed"))
    release(env)
    with pytest.raises(StoreNotApplied):
        deliver(env, SafetyCheckOutcome.FAULT)
    assert env.repository.node_infos["locker-1"].device_state == DeviceState.FAULT
    env.service.tick()
    assert len(env.faults) == 2
    assert env.faults[0] == env.faults[1]
    assert len(env.gateway.requests) == 1


def test_NFR04_isolation_error_retried_without_new_check(env):
    release(env)
    env.store.isolation_error = StoreOutcomeUnknown("isolation uncertain")
    with pytest.raises(StoreOutcomeUnknown):
        deliver(env, SafetyCheckOutcome.FAULT)
    assert not env.faults
    env.store.isolation_error = None
    env.service.tick()
    assert len(env.faults) == 1 and len(env.gateway.requests) == 1


@pytest.mark.parametrize("commit", [False, True])
def test_FR20_close_unknown_requeries(env, commit):
    release(env)
    env.store.finish_error = StoreOutcomeUnknown("close uncertain")
    env.store.commit_before_error = commit
    deliver(env)
    assert env.store.close_reads == ["use-1"]
    decision = release(env)
    assert decision.verdict == (Verdict.OK if commit else Verdict.BUSY)
    assert len(env.gateway.requests) == 1


def test_FR20_unknown_close_retry_keeps_timestamp_check_and_evidence(env):
    release(env)
    env.store.finish_error = StoreOutcomeUnknown("close uncertain")
    deliver(env)
    first = env.store.finishes[0]
    env.clock.advance(20)
    env.store.finish_error = None
    env.service.tick()
    assert env.store.finishes == [first, first]
    assert env.repository.session("use-1").closed_at == first[1]


def test_FR20_unknown_does_not_infer_completion_from_available(env):
    release(env)
    env.store.finish_error = StoreOutcomeUnknown("close uncertain")
    deliver(env)
    env.repository.set_node(device_state=DeviceState.AVAILABLE)
    assert release(env).verdict == Verdict.BUSY
    assert not env.store.completed


def test_FR20_finish_close_retry_same_check(env):
    release(env)
    env.store.finish_error = StoreNotApplied("write unavailable")
    value = report(env)
    with pytest.raises(StoreNotApplied):
        env.service.on_safety_check_result(value)
    env.clock.advance(2)
    env.store.finish_error = None
    env.store.finish_already_applied = True
    env.service.on_safety_check_result(value)
    assert env.store.finishes[0] == env.store.finishes[1]
    assert release(env).verdict == Verdict.OK


def test_FR20_duplicate_result_settles_once(env):
    release(env)
    value = deliver(env)
    env.service.on_safety_check_result(value)
    assert len(env.store.finishes) == 1


def test_FR20_close_conflict_keeps_occupancy(env):
    release(env)
    env.store.finish_conflict = True
    deliver(env)
    assert env.repository.session("use-1").state == SessionState.CLOSING
    assert release(env).verdict == Verdict.BUSY


def test_FR20_reconnect_invalidates_current_check(env):
    release(env)
    env.repository.set_node(session_id="connection-2")
    deliver(env)
    assert not env.store.finishes
    env.clock.advance(1)
    env.service.tick()
    assert env.gateway.requests[-1].target_session_id == "connection-2"
    deliver(env)
    assert len(env.store.finishes) == 1


def test_FR20_new_fault_invalidates_current_check(env):
    release(env)
    env.repository.set_node(device_state=DeviceState.FAULT)
    deliver(env)
    env.clock.advance(40)
    env.service.tick()
    assert len(env.gateway.requests) == 1
    assert not env.store.finishes


def test_FR20_settings_snapshot_kept_for_entire_release(env):
    release(env)
    env.settings[0] = Settings(safety_check_retry_sec=10, safety_check_timeout_sec=15)
    deliver(env, SafetyCheckOutcome.NOT_READY)
    env.clock.advance(1)
    env.service.tick()
    assert len(env.gateway.requests) == 2
    request = env.gateway.requests[-1]
    assert request.expires_at - request.issued_at == timedelta(seconds=3)


@pytest.mark.parametrize("seconds,expected", [(0, 0), (9, 0), (11, 1), (30, 20)])
def test_FR19_overtime_calculation(env, seconds, expected):
    env.clock.advance(seconds)
    assert env.service.get_use_status("visitor-1", "use-1")["overtime_sec"] == expected


def test_FR19_closed_overtime_uses_closed_at(env):
    env.clock.advance(15)
    release(env)
    deliver(env)
    env.clock.advance(30)
    assert env.service.get_use_status("visitor-1", "use-1")["overtime_sec"] == 5


def test_FR21_status_checks_owner_and_history_retention(env):
    assert env.service.get_use_status("visitor-2", "use-1") == {"reason_code": "USE_NOT_FOUND"}
    release(env)
    deliver(env)
    env.clock.advance(7 * 86400)
    assert env.service.get_use_status("visitor-1", "use-1")["state"] == "CLOSED"
    env.clock.advance(1)
    assert env.service.get_use_status("visitor-1", "use-1") == {"reason_code": "USE_NOT_FOUND"}


def test_FR11_NFR08_responses_exclude_user_and_occupant(env):
    decision = asdict(release(env))
    status = env.service.get_use_status("visitor-1", "use-1")
    assert "visitor-1" not in repr(decision) + repr(status)
    assert "user_id" not in decision and "user_id" not in status
    assert "occupant" not in decision and "occupant" not in status


def test_FR20_resume_closing_requests_fresh_check(env):
    env.repository.set_session(state=SessionState.CLOSING)
    env.repository.set_node(device_state=DeviceState.CLOSING)
    env.service.resume_close("use-1")
    env.service.resume_close("use-1")
    assert len(env.gateway.requests) == 1 and not env.store.begins


def test_FR20_resume_does_not_reopen_active_or_closed(env):
    env.service.resume_close("use-1")
    env.repository.set_session(state=SessionState.CLOSED)
    env.service.resume_close("use-1")
    assert not env.gateway.requests


def test_FR20_repeat_resume_keeps_retry_wait(env):
    release(env)
    deliver(env, SafetyCheckOutcome.NOT_READY)
    env.service.resume_close("use-1")
    assert len(env.gateway.requests) == 1
    env.clock.advance(1)
    env.service.tick()
    assert len(env.gateway.requests) == 2


def test_NFR04_shared_node_lock_serializes_fault_and_close(env):
    release(env)
    ready = Barrier(2)

    def receive():
        ready.wait(timeout=5)
        deliver(env)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with env.lock_for("locker-1"):
            pending = pool.submit(receive)
            ready.wait(timeout=5)
            env.repository.set_node(device_state=DeviceState.FAULT)
            assert not pending.done()
        pending.result(timeout=5)
    assert not env.store.finishes
