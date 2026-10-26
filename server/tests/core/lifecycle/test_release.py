"""반납 계약의 단위 검증. DB는 shared 계약의 fake로 대신한다.

반납 완료는 논리적 점유 해제다. 반납 검사와 센서 근거를 쓰지 않는다 (2026-10-26 결정).
"""

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
    SessionState, StoreConflictReason, StoreOutcome, TrustState, Verdict,
)
from nearpilot.shared.errors import StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.models import CloseResult, NodeInfo, StoreResult, UseSession


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
        self.close_reads = []
        self.completed = {}
        self.begin_conflict = None
        self.begin_error = None
        self.finish_error = None
        self.commit_before_error = False
        self.finish_conflict = None
        self.close_error = None

    def begin_close(self, use_id, reason):
        self.begins.append((use_id, reason))
        if self.begin_error:
            raise self.begin_error
        if self.begin_conflict:
            return StoreResult(StoreOutcome.CONFLICT, conflict_reason=self.begin_conflict)
        self.repository.set_session(state=SessionState.CLOSING, end_reason=reason)
        self.repository.set_node(device_state=DeviceState.CLOSING)
        return StoreResult(StoreOutcome.APPLIED, session=self.repository.session(use_id))

    def finish_close(self, use_id, closed_at, end_reason):
        self.finishes.append((use_id, closed_at, end_reason))
        if self.finish_conflict:
            return StoreResult(StoreOutcome.CONFLICT, conflict_reason=self.finish_conflict)
        if use_id in self.completed:
            return StoreResult(StoreOutcome.ALREADY_APPLIED)
        if self.finish_error and not self.commit_before_error:
            raise self.finish_error
        self.completed[use_id] = CloseResult(use_id, closed_at, end_reason)
        self.repository.set_session(state=SessionState.CLOSED, closed_at=closed_at)
        self.repository.set_node(device_state=DeviceState.AVAILABLE)
        if self.finish_error:
            raise self.finish_error
        return StoreResult(StoreOutcome.APPLIED)

    def close_result(self, use_id):
        self.close_reads.append(use_id)
        if self.close_error:
            raise self.close_error
        return self.completed.get(use_id)


@pytest.fixture
def env():
    clock = Clock()
    repository = RepositoryFake(clock)
    store = StoreFake(repository)
    settings = [Settings()]
    locks = {}
    lock_index = Lock()

    def lock_for(node_id):
        with lock_index:
            return locks.setdefault(node_id, RLock())

    service = ReleaseService(
        repository=repository, store=store, settings=lambda: settings[0], clock=clock,
        lock_for=lock_for,
    )
    return SimpleNamespace(
        clock=clock, repository=repository, store=store, service=service,
        settings=settings, locks=locks, lock_for=lock_for,
    )


def release(env):
    return env.service.release_use("visitor-1", "use-1")


def test_FR21_release_other_users_handle_denied(env):
    other = env.service.release_use("visitor-2", "use-1")
    missing = env.service.release_use("visitor-1", "missing")
    assert other == missing
    assert other.verdict == Verdict.DENY and other.reason == ReasonCode.USE_NOT_FOUND
    assert env.store.begins == env.store.finishes == []


def test_FR21_release_checks_owner(env):
    release(env)
    release(env)
    assert len(env.repository.owner_checks) == 4
    assert all(pair == ("use-1", "visitor-1") for pair in env.repository.owner_checks)


def test_FR20_release_closes_logically(env):
    decision = release(env)
    assert (decision.verdict, decision.reason) == (Verdict.OK, ReasonCode.OK)
    assert "점유를 해제" in decision.message
    assert env.store.begins == [("use-1", EndReason.RELEASE)]
    assert env.store.finishes == [("use-1", env.clock(), EndReason.RELEASE)]
    assert env.repository.session("use-1").state == SessionState.CLOSED
    assert env.repository.node_infos["locker-1"].device_state == DeviceState.AVAILABLE


def test_FR20_release_does_not_wait_for_check_or_sensor(env):
    """ReleaseService는 검사 게이트웨이와 센서 상태를 받지 않는다."""
    assert not hasattr(env.service, "on_safety_check_result")
    with pytest.raises(TypeError):
        ReleaseService(repository=env.repository, store=env.store, gateway=object(),
                       settings=Settings, clock=env.clock, lock_for=env.lock_for)


def test_FR20_overdue_release_allowed(env):
    env.clock.advance(20)
    decision = release(env)
    assert decision.verdict == Verdict.OK
    assert env.store.begins == [("use-1", EndReason.RELEASE)]


def test_FR19_release_blocked_by_command(env):
    env.store.begin_conflict = StoreConflictReason.COMMAND_IN_PROGRESS
    decision = release(env)
    assert (decision.verdict, decision.reason) == (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS)
    assert not env.store.finishes
    assert env.repository.session("use-1").state == SessionState.ACTIVE


def test_FR19_finish_blocked_by_command_keeps_closing(env):
    env.store.finish_conflict = StoreConflictReason.COMMAND_IN_PROGRESS
    decision = release(env)
    assert (decision.verdict, decision.reason) == (Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS)
    assert env.repository.session("use-1").state == SessionState.CLOSING


def test_FR20_fault_node_release_goes_to_recovery(env):
    env.repository.set_node(device_state=DeviceState.FAULT)
    decision = release(env)
    assert (decision.verdict, decision.reason) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)
    assert env.store.begins == env.store.finishes == []
    assert env.repository.session("use-1").state == SessionState.ACTIVE


def test_FR20_fault_during_closing_does_not_finish(env):
    env.repository.set_session(state=SessionState.CLOSING, end_reason=EndReason.RELEASE)
    env.repository.set_node(device_state=DeviceState.FAULT)
    assert release(env).reason == ReasonCode.TARGET_UNAVAILABLE
    env.service.resume_close("use-1")
    env.service.tick()
    assert not env.store.finishes


def test_FR20_repeat_release_returns_existing_completion(env):
    first = release(env)
    env.clock.advance(5)
    second = release(env)
    assert first == second
    assert len(env.store.begins) == len(env.store.finishes) == 1
    assert env.repository.session("use-1").closed_at == env.clock() - timedelta(seconds=5)


def test_NFR03_concurrent_release_closes_once(env):
    barrier = Barrier(8)

    def invoke(_):
        barrier.wait(timeout=5)
        return release(env)

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(invoke, range(8)))
    assert all(result.verdict == Verdict.OK for result in decisions)
    assert len(env.store.begins) == len(env.store.finishes) == 1


def test_FR20_closing_from_other_reason_not_released(env):
    env.repository.set_session(state=SessionState.CLOSING, end_reason=EndReason.FAULT)
    env.repository.set_node(device_state=DeviceState.CLOSING)
    assert release(env).reason == ReasonCode.TARGET_UNAVAILABLE
    assert not env.store.finishes


@pytest.mark.parametrize("commit", [False, True])
def test_FR20_close_unknown_requeries(env, commit):
    env.store.finish_error = StoreOutcomeUnknown("close uncertain")
    env.store.commit_before_error = commit
    decision = release(env)
    assert env.store.close_reads == ["use-1"]
    assert decision.verdict == (Verdict.OK if commit else Verdict.BUSY)


def test_FR20_unknown_close_retry_keeps_timestamp(env):
    env.store.finish_error = StoreOutcomeUnknown("close uncertain")
    release(env)
    first = env.store.finishes[0]
    env.clock.advance(20)
    env.store.finish_error = None
    env.service.tick()
    assert env.store.finishes == [first, first]
    assert env.repository.session("use-1").closed_at == first[1]
    assert release(env).verdict == Verdict.OK


def test_FR20_unknown_does_not_infer_completion_from_available(env):
    env.store.finish_error = StoreOutcomeUnknown("close uncertain")
    release(env)
    env.repository.set_node(device_state=DeviceState.AVAILABLE)
    assert release(env).verdict != Verdict.OK
    assert not env.store.completed


def test_FR20_not_applied_close_retried_by_tick(env):
    env.store.finish_error = StoreNotApplied("write unavailable")
    decision = release(env)
    assert (decision.verdict, decision.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert env.repository.session("use-1").state == SessionState.CLOSING
    env.clock.advance(2)
    env.store.finish_error = None
    env.service.tick()
    assert env.store.finishes[0] == env.store.finishes[1]
    assert release(env).verdict == Verdict.OK


def test_FR20_close_state_conflict_keeps_occupancy(env):
    env.store.finish_conflict = StoreConflictReason.STATE_CONFLICT
    decision = release(env)
    assert decision.reason == ReasonCode.TARGET_UNAVAILABLE
    assert env.repository.session("use-1").state == SessionState.CLOSING


def test_FR20_closed_by_recovery_is_not_release_completion(env):
    env.repository.set_session(state=SessionState.CLOSED, closed_at=env.clock(), end_reason=EndReason.FAULT)
    env.store.completed["use-1"] = CloseResult("use-1", env.clock(), EndReason.FAULT)
    decision = release(env)
    assert (decision.verdict, decision.reason) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)


def test_FR20_closed_without_record_is_not_announced(env):
    env.repository.set_session(state=SessionState.CLOSED, closed_at=env.clock(), end_reason=EndReason.RELEASE)
    assert release(env).verdict == Verdict.BUSY


@pytest.mark.parametrize("seconds,expected", [(0, 0), (9, 0), (11, 1), (30, 20)])
def test_FR19_overtime_calculation(env, seconds, expected):
    env.clock.advance(seconds)
    assert env.service.get_use_status("visitor-1", "use-1")["overtime_sec"] == expected


def test_FR19_closed_overtime_uses_closed_at(env):
    env.clock.advance(15)
    release(env)
    env.clock.advance(30)
    assert env.service.get_use_status("visitor-1", "use-1")["overtime_sec"] == 5


def test_FR21_status_checks_owner_and_history_retention(env):
    assert env.service.get_use_status("visitor-2", "use-1") == {"reason_code": "USE_NOT_FOUND"}
    release(env)
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


def test_FR20_resume_closing_rechecks_and_finishes(env):
    env.repository.set_session(state=SessionState.CLOSING, end_reason=EndReason.RELEASE)
    env.repository.set_node(device_state=DeviceState.CLOSING)
    env.service.resume_close("use-1")
    env.service.resume_close("use-1")
    assert len(env.store.finishes) == 1 and not env.store.begins
    assert env.repository.session("use-1").state == SessionState.CLOSED


def test_FR20_resume_does_not_reopen_active_or_closed(env):
    env.service.resume_close("use-1")
    env.repository.set_session(state=SessionState.CLOSED)
    env.service.resume_close("use-1")
    assert not env.store.finishes and not env.store.begins


def test_NFR04_shared_node_lock_serializes_fault_and_close(env):
    env.repository.set_session(state=SessionState.CLOSING, end_reason=EndReason.RELEASE)
    env.repository.set_node(device_state=DeviceState.CLOSING)
    ready = Barrier(2)

    def resume():
        ready.wait(timeout=5)
        env.service.resume_close("use-1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with env.lock_for("locker-1"):
            pending = pool.submit(resume)
            ready.wait(timeout=5)
            env.repository.set_node(device_state=DeviceState.FAULT)
            assert not pending.done()
        pending.result(timeout=5)
    assert not env.store.finishes
