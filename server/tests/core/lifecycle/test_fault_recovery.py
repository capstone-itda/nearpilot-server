"""복구 건 생성과 반납 연결 검증. DB·IOT는 shared 계약의 fake다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier, Lock, RLock
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService, settlement_digest
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, ApprovalUsageState, Capability, CommandOutcome, DeviceState, EndReason, ExecutionStatus, Occupancy, RecoveryStatus,
    SafetyCheckOutcome, SessionState, StoreConflictReason, StoreOutcome, TrustState,
)
from nearpilot.shared.errors import StoreError, StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.models import (
    ApprovalUsageSettlement, CommandRecord, NodeInfo, RecoveryCompletion, RecoveryEvidence, RecoveryRecord,
    RecoverySettlement, RestoreBundle, SafetyCheckResult, StoreResult, UseSession,
)


class RepositoryFake:
    def __init__(self, at):
        self.node = NodeInfo(
            "locker-1", "place-1", Capability.LOCKER, Occupancy.RENTAL,
            TrustState.TRUSTED, DeviceState.FAULT, None, at, session_id="connection-1",
        )
        self.use = UseSession(
            "use-1", "request-1", self.node.node_id, SessionState.CLOSING,
            rental_due_at=at - timedelta(seconds=10),
            release_policy={"check_criteria": {"id": "locker-return", "version": 1}},
        )

    def nodes(self, capability=None):
        return (self.node,)

    def session(self, use_id):
        return self.use if use_id == self.use.use_id else None

    def session_owner_matches(self, use_id, user_id):
        return use_id == self.use.use_id and user_id == "visitor-1"


class StoreFake:
    def __init__(self, repository, clock):
        self.repository = repository
        self.clock = clock
        self.records = {}
        self.saves = []
        self.reads = []
        self.restore_reads = 0
        self.save_errors = []
        self.read_errors = []
        self.restore_error = None
        self.conflict = False
        self.commit_before_error = False
        self.isolations = []
        self.usage_state = "held"
        self.command = None
        self.result_record = None

    def restore_pending(self):
        self.restore_reads += 1
        if self.restore_error:
            raise self.restore_error
        return (RestoreBundle(
            self.repository.node, self.clock(), recoveries=tuple(self.records.values()),
        ),)

    def recovery(self, recovery_id):
        self.reads.append(recovery_id)
        if self.read_errors:
            error = self.read_errors.pop(0)
            if error is not None:
                raise error
        return self.records.get(recovery_id)

    def save_recovery(self, record):
        self.saves.append(record)
        if self.conflict:
            return StoreResult(StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.LINK_MISMATCH)
        error = self.save_errors.pop(0) if self.save_errors else None
        if error and not self.commit_before_error:
            raise error
        old = self.records.get(record.recovery_id)
        self.records[record.recovery_id] = record
        if error:
            raise error
        return StoreResult(
            StoreOutcome.ALREADY_APPLIED if old == record else StoreOutcome.APPLIED,
            recovery=self.result_record or record,
        )

    def execution(self, internal_request_id):
        return self.command

    def isolate_device(self, node_id, evidence):
        self.isolations.append((node_id, evidence))
        already = self.repository.node.device_state == DeviceState.FAULT
        self.repository.node = replace(self.repository.node, device_state=DeviceState.FAULT)
        return StoreResult(StoreOutcome.ALREADY_APPLIED if already else StoreOutcome.APPLIED)

    def begin_close(self, use_id, reason):
        self.repository.use = replace(self.repository.use, state=SessionState.CLOSING)
        self.repository.node = replace(self.repository.node, device_state=DeviceState.CLOSING)
        return StoreResult(StoreOutcome.APPLIED, session=self.repository.use)


@pytest.fixture
def env():
    time = [datetime(2026, 10, 15, tzinfo=timezone.utc)]
    clock = lambda: time[0]
    repository = RepositoryFake(clock())
    store = StoreFake(repository, clock)
    locks = {}
    index_lock = Lock()

    def lock_for(node_id):
        with index_lock:
            return locks.setdefault(node_id, RLock())

    def service():
        return FaultRecoveryService(repository=repository, store=store, clock=clock, lock_for=lock_for)

    return SimpleNamespace(
        time=time, clock=clock, repository=repository, store=store,
        service=service(), new_service=service, lock_for=lock_for,
    )


def evidence(env, id="event-1", **changes):
    return replace(RecoveryEvidence(id, "connection-1", env.clock(), safety_confirmed=False), **changes)


def record(env, **changes):
    args = dict(internal_request_ids=("request-1",), command_ids=("command-1",), use_id="use-1",
                evidence=(evidence(env),))
    args.update(changes)
    return env.service.record_isolated("locker-1", **args)


def completed(record, at):
    settlement = RecoverySettlement(
        record.recovery_id, record.node_id, "owner-1", at, "fake-digest",
        DeviceState.FAULT, RecoveryStatus.NOT_READY, {}, {}, record.evidence, (),
        use_id=record.use_id, closed_at=at if record.use_id else None,
        end_reason=EndReason.FAULT if record.use_id else None,
    )
    return replace(record, status=RecoveryStatus.RECOVERED,
                   completion=RecoveryCompletion(settlement, at))


def test_NFR04_isolation_creates_recovery_case(env):
    saved = record(env)
    assert saved == env.store.records[saved.recovery_id]
    assert (saved.node_id, saved.internal_request_ids, saved.command_ids, saved.use_id) == (
        "locker-1", ("request-1",), ("command-1",), "use-1",
    )
    assert saved.status == RecoveryStatus.NOT_READY
    assert saved.created_at == env.clock() and saved.evidence == (evidence(env),)
    assert saved.completion is None


@pytest.mark.parametrize("state", [DeviceState.ACTIVE, DeviceState.AVAILABLE, DeviceState.CLOSING])
def test_NFR04_requires_confirmed_isolation(env, state):
    env.repository.node = replace(env.repository.node, device_state=state)
    with pytest.raises(StoreError):
        record(env)
    assert not env.store.saves


def test_NFR04_same_fault_same_recovery_id(env):
    first = record(env)
    env.time[0] += timedelta(seconds=2)
    second = record(env, evidence=(evidence(env, "event-2"),))
    assert first.recovery_id == second.recovery_id
    assert first.created_at == second.created_at
    assert second.evidence == (first.evidence[0], evidence(env, "event-2"))
    assert len(env.store.records) == 1


def test_NFR03_repeat_fault_does_not_save_twice(env):
    first = record(env)
    assert record(env) == first
    assert len(env.store.saves) == 1


def test_NFR03_concurrent_fault_creates_once(env):
    barrier = Barrier(8)

    def invoke(_):
        barrier.wait(timeout=5)
        return record(env)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(invoke, range(8)))
    assert len({r.recovery_id for r in results}) == 1
    assert len(env.store.saves) == 1


def test_NFR04_retry_save_recovery_same_id(env):
    env.store.save_errors = [StoreNotApplied()]
    with pytest.raises(StoreNotApplied):
        record(env)
    original = env.store.saves[0]
    env.time[0] += timedelta(seconds=5)
    env.service.tick()
    assert env.store.saves == [original, original]
    env.service.tick()
    assert len(env.store.saves) == 2


def test_NFR04_rehandoff_retries_same_id(env):
    env.store.save_errors = [StoreNotApplied()]
    with pytest.raises(StoreNotApplied):
        record(env)
    assert record(env).recovery_id == env.store.saves[0].recovery_id


@pytest.mark.parametrize("committed", [False, True])
def test_NFR04_save_recovery_unknown_requeries(env, committed):
    env.store.save_errors = [StoreOutcomeUnknown()]
    env.store.commit_before_error = committed
    if committed:
        saved = record(env)
        assert saved == env.store.saves[0]
    else:
        with pytest.raises(StoreOutcomeUnknown):
            record(env)
    assert env.store.reads == [env.store.saves[0].recovery_id] * 2
    env.service.tick()
    assert len(env.store.records) == 1
    assert len(env.store.saves) == (1 if committed else 2)


def test_NFR04_unknown_read_failure_preserves_pending(env):
    env.store.save_errors = [StoreOutcomeUnknown()]
    env.store.commit_before_error = True
    env.store.read_errors = [None, StoreError()]
    with pytest.raises(StoreError):
        record(env)
    env.service.tick()
    assert len(env.store.saves) == 1


def test_NFR04_query_failure_before_save_retries_same_id(env):
    env.store.read_errors = [StoreError()]
    with pytest.raises(StoreError):
        record(env)
    env.service.tick()
    assert len(env.store.saves) == 1
    assert len(set(env.store.reads)) == 1


def test_NFR04_restore_query_failure_is_not_empty(env):
    env.store.restore_error = StoreError()
    with pytest.raises(StoreError):
        record(env)
    assert not env.store.saves
    env.store.restore_error = None
    record(env)
    assert len(env.store.saves) == 1


@pytest.mark.parametrize("bundles", [(), None])
def test_NFR04_missing_or_stale_fault_bundle_prevents_new_id(env, bundles):
    if bundles is None:
        bundles = (RestoreBundle(replace(env.repository.node, device_state=DeviceState.ACTIVE), env.clock()),)
    env.store.restore_pending = lambda: bundles
    with pytest.raises(StoreError):
        record(env)
    assert env.store.saves == env.store.reads == []


def test_NFR04_tick_continues_other_devices_after_failure(env):
    second_node = replace(env.repository.node, node_id="locker-2")
    env.repository.nodes = lambda capability=None: (env.repository.node, second_node)
    env.store.restore_pending = lambda: tuple(RestoreBundle(
        node, env.clock(), recoveries=tuple(r for r in env.store.records.values() if r.node_id == node.node_id),
    ) for node in env.repository.nodes())
    env.store.save_errors = [StoreNotApplied(), StoreNotApplied()]
    with pytest.raises(StoreNotApplied):
        record(env)
    with pytest.raises(StoreNotApplied):
        env.service.record_isolated("locker-2")
    env.store.save_errors = [StoreNotApplied()]
    with pytest.raises(StoreNotApplied):
        env.service.tick()
    assert {r.node_id for r in env.store.records.values()} == {"locker-2"}
    env.service.tick()
    assert {r.node_id for r in env.store.records.values()} == {"locker-1", "locker-2"}


def test_NFR04_recovered_without_completion_is_not_reused(env):
    first = record(env)
    env.store.records[first.recovery_id] = replace(first, status=RecoveryStatus.RECOVERED)
    with pytest.raises(StoreError):
        record(env)
    assert env.store.saves == [first]


def test_NFR04_restart_reuses_existing_recovery_id(env):
    first = record(env)
    env.service = env.new_service()
    env.time[0] += timedelta(seconds=5)
    second = record(env, evidence=(evidence(env, "event-2"),))
    assert second.recovery_id == first.recovery_id
    assert second.created_at == first.created_at
    assert len(second.evidence) == 2


def test_NFR04_restart_repairs_missing_case_for_fault_device(env):
    env.service = env.new_service()
    record(env)
    assert len(env.store.records) == 1
    assert not env.store.isolations


@pytest.mark.parametrize("changes", [
    {"use_id": "other-use"}, {"internal_request_ids": ("other-request",)},
    {"command_ids": ("other-command",)},
])
def test_NFR04_same_fault_preserves_target_links(env, changes):
    first = record(env)
    with pytest.raises(StoreError):
        record(env, **changes)
    assert env.store.saves == [first]


def test_NFR04_conflicting_evidence_does_not_overwrite(env):
    first = record(env)
    with pytest.raises(StoreError):
        record(env, evidence=(evidence(env, safety_confirmed=True),))
    assert env.store.records[first.recovery_id] == first


def test_NFR04_save_conflict_keeps_retry(env):
    env.store.conflict = True
    with pytest.raises(StoreError):
        record(env)
    first = env.store.saves[0]
    env.store.conflict = False
    env.service.tick()
    assert env.store.saves == [first, first]


def test_NFR04_unknown_different_target_not_confirmed(env):
    env.store.save_errors = [StoreOutcomeUnknown()]
    with pytest.raises(StoreOutcomeUnknown):
        record(env)
    original = env.store.saves[0]
    env.store.records[original.recovery_id] = replace(original, use_id="other-use")
    with pytest.raises(StoreError):
        env.service.tick()
    assert env.store.saves == [original]


def test_NFR04_unknown_partial_evidence_requires_retry(env):
    first = record(env)
    env.store.save_errors = [StoreOutcomeUnknown()]
    with pytest.raises(StoreOutcomeUnknown):
        record(env, evidence=(evidence(env, "event-2"),))
    assert env.store.records[first.recovery_id] == first
    env.service.tick()
    assert len(env.store.records[first.recovery_id].evidence) == 2


def test_NFR04_pending_does_not_overwrite_newer_evidence(env):
    env.store.save_errors = [StoreNotApplied()]
    with pytest.raises(StoreNotApplied):
        record(env)
    original = env.store.saves[0]
    env.store.records[original.recovery_id] = replace(original, evidence=(evidence(env, "event-2"),))
    env.service.tick()
    saved = env.store.records[original.recovery_id]
    assert {e.evidence_id for e in saved.evidence} == {"event-1", "event-2"}


def test_NFR04_new_fault_new_recovery_id(env):
    first = record(env)
    env.store.records[first.recovery_id] = completed(first, env.clock())
    second = record(env, evidence=(evidence(env, "new-fault"),))
    assert first.recovery_id != second.recovery_id
    assert env.store.records[first.recovery_id].completion is not None


def test_NFR04_late_result_after_settle(env):
    env.store.save_errors = [StoreOutcomeUnknown()]
    with pytest.raises(StoreOutcomeUnknown):
        record(env)
    original = env.store.saves[0]
    done = completed(original, env.clock())
    env.store.records[original.recovery_id] = done
    env.repository.node = replace(env.repository.node, device_state=DeviceState.AVAILABLE)
    env.service.tick()
    assert env.store.records[done.recovery_id] == done
    assert env.store.saves == [original]


def test_FR19_unconfirmed_usage_not_released(env):
    session = env.repository.use
    node = env.repository.node
    record(env, evidence=(evidence(env, execution_outcome=None),))
    assert env.store.usage_state == "held"
    assert env.repository.use == session and env.repository.node == node


def test_NFR04_shared_fault_has_no_use_session(env):
    env.repository.node = replace(env.repository.node, capability=Capability.LIGHT, occupancy=Occupancy.SHARED)
    saved = record(env, use_id=None)
    assert saved.use_id is None
    assert env.repository.use.state == SessionState.CLOSING


def test_NFR04_multiple_active_cases_are_not_guessed(env):
    first = record(env)
    env.store.records["other"] = replace(first, recovery_id="other")
    with pytest.raises(StoreError):
        record(env)
    assert env.store.saves == [first]


def test_NFR04_returned_target_mismatch_keeps_pending(env):
    env.store.result_record = RecoveryRecord(
        "wrong-id", "locker-1", ("request-1",), (), "use-1", env.clock(), RecoveryStatus.NOT_READY,
    )
    with pytest.raises(StoreError):
        record(env)
    env.store.result_record = None
    env.service.tick()
    assert len(env.store.saves) == 1


def test_NFR04_release_adapter_preserves_report_reference(env):
    env.store.command = CommandRecord("command-1", "request-1", "locker-1", Action.OPEN,
                                      ExecutionStatus.SUCCEEDED, use_id="use-1")
    env.service.record_fault(env.repository.use, {
        "event_id": "event-1", "session_id": "connection-1", "checked_at": env.clock(), "outcome": "fault",
    })
    saved = env.store.saves[0]
    assert saved.command_ids == ("command-1",)
    assert saved.evidence == (evidence(env),)


def test_NFR04_release_unknown_has_no_invented_safety_reference(env):
    env.service.record_fault(env.repository.use, {"reason": "safety_unconfirmed", "use_id": "use-1"})
    assert env.store.saves[0].evidence == ()
    assert env.store.saves[0].command_ids == ()


def test_NFR04_release_adapter_rejects_wrong_command_link(env):
    env.store.command = CommandRecord("command-1", "request-1", "other-node", Action.OPEN,
                                      ExecutionStatus.SUCCEEDED, use_id="use-1")
    with pytest.raises(StoreError):
        env.service.record_fault(env.repository.use, {})
    assert not env.store.saves


@pytest.mark.parametrize("changes", [{"session_id": None}, {"checked_at": None}, {"outcome": "passed"}])
def test_NFR04_release_report_requires_reference(env, changes):
    report = dict(event_id="event-1", session_id="connection-1", checked_at=env.clock(), outcome="fault")
    report.update(changes)
    with pytest.raises(ValueError):
        env.service.record_fault(env.repository.use, report)
    assert not env.store.saves


def release_service(env):
    gateway = SimpleNamespace(requests=[])
    gateway.request_check = gateway.requests.append
    service = ReleaseService(
        repository=env.repository, store=env.store, gateway=gateway,
        settings=Settings, clock=env.clock, lock_for=env.lock_for,
        record_fault=env.service.record_fault,
    )
    env.repository.node = replace(env.repository.node, device_state=DeviceState.ACTIVE)
    env.repository.use = replace(env.repository.use, state=SessionState.ACTIVE)
    service.release_use("visitor-1", "use-1")
    request = gateway.requests[0]
    report = SafetyCheckResult(
        request.node_id, request.target_session_id, 1, env.clock(), env.clock(),
        request.check_id, "event-1", request.criteria, env.clock(), SafetyCheckOutcome.FAULT,
        {"sensor_fault": True},
    )
    return service, report


def test_NFR04_release_fault_connected_to_recovery_creation(env):
    release, report = release_service(env)
    release.on_safety_check_result(report)
    assert env.repository.node.device_state == DeviceState.FAULT
    assert env.repository.use.state == SessionState.CLOSING
    assert len(env.store.saves) == 1
    assert env.store.saves[0].use_id == "use-1"
    assert env.store.saves[0].evidence == (evidence(env),)


def test_NFR04_release_rehandoff_after_recovery_save_failure(env):
    release, report = release_service(env)
    env.store.save_errors = [StoreNotApplied()]
    with pytest.raises(StoreNotApplied):
        release.on_safety_check_result(report)
    assert env.repository.node.device_state == DeviceState.FAULT
    release.on_safety_check_result(report)
    assert env.store.saves[0] == env.store.saves[1]
    assert len(env.store.records) == 1


def test_NFR04_release_and_recovery_tick_share_pending_case(env):
    release, report = release_service(env)
    env.store.save_errors = [StoreNotApplied()]
    with pytest.raises(StoreNotApplied):
        release.on_safety_check_result(report)
    env.service.tick()
    release.tick()
    assert len(env.store.saves) == 2
    assert len(env.store.records) == 1


def test_NFR04_release_unconfirmed_limit_connected_to_recovery(env):
    release, _ = release_service(env)
    env.time[0] += timedelta(seconds=Settings().safety_check_timeout_sec)
    release.tick()
    env.time[0] += timedelta(seconds=Settings().safety_unconfirmed_limit_sec)
    release.tick()
    assert env.repository.node.device_state == DeviceState.FAULT
    assert env.repository.use.state == SessionState.CLOSING
    assert env.store.saves[0].evidence == ()
    assert env.store.usage_state == "held"


# ── 정산 요약값 (NFR-04, architecture.md §4 "복구 건과 복구 정산") ─────────

def _digest_fields(**overrides):
    at = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)
    fields = dict(
        recovery_id="recovery-1", node_id="locker-1",
        expected_node_state=DeviceState.FAULT,
        expected_recovery_status=RecoveryStatus.READY,
        expected_commands={"command-1": ExecutionStatus.UNKNOWN, "command-2": ExecutionStatus.FAILED},
        expected_sessions={"use-1": SessionState.CLOSING},
        evidence=(
            RecoveryEvidence("evidence-1", "connection-2", at, safety_confirmed=True),
            RecoveryEvidence("evidence-2", "connection-2", at, physical_state_synced=True),
        ),
        usage_settlements=(
            ApprovalUsageSettlement("usage-1", ApprovalUsageState.HELD, ApprovalUsageState.CONSUMED),
        ),
        use_id="use-1", closed_at=at, end_reason=EndReason.FAULT,
    )
    fields.update(overrides)
    return fields


def test_NFR04_settlement_digest_excludes_approval_account_and_time():
    fields = _digest_fields()
    digest = settlement_digest(**fields)
    first = RecoverySettlement(
        approved_by_user_id="owner-1", approved_at=datetime(2026, 10, 15, 4, tzinfo=timezone.utc),
        settlement_digest=digest, **fields,
    )
    again = replace(first, approved_by_user_id="owner-2", approved_at=first.approved_at + timedelta(hours=1))
    assert settlement_digest(**_settlement_fields(again)) == first.settlement_digest


def test_NFR04_settlement_digest_ignores_input_order_and_time_zone():
    fields = _digest_fields()
    kst = timezone(timedelta(hours=9))
    reordered = _digest_fields(
        expected_commands=dict(reversed(list(fields["expected_commands"].items()))),
        evidence=tuple(
            replace(e, observed_at=e.observed_at.astimezone(kst)) for e in reversed(fields["evidence"])
        ),
        closed_at=fields["closed_at"].astimezone(kst),
    )
    assert settlement_digest(**reordered) == settlement_digest(**fields)


@pytest.mark.parametrize("change", [
    {"recovery_id": "recovery-2"},
    {"expected_commands": {"command-1": ExecutionStatus.UNKNOWN}},
    {"expected_sessions": {"use-1": SessionState.ACTIVE}},
    {"usage_settlements": (
        ApprovalUsageSettlement("usage-1", ApprovalUsageState.HELD, ApprovalUsageState.RELEASED),
    )},
    {"evidence": (
        RecoveryEvidence(
            "evidence-1", "connection-2", datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc),
            safety_confirmed=False,
        ),
    )},
    {"closed_at": datetime(2026, 10, 15, 3, 1, tzinfo=timezone.utc)},
])
def test_NFR04_settlement_digest_changes_with_settlement_content(change):
    assert settlement_digest(**_digest_fields(**change)) != settlement_digest(**_digest_fields())


def test_NFR04_settlement_digest_shared_recovery_has_no_close():
    fields = _digest_fields(expected_sessions={}, use_id=None, closed_at=None, end_reason=None)
    digest = settlement_digest(**fields)
    assert digest != settlement_digest(**_digest_fields())
    assert RecoverySettlement(
        approved_by_user_id="owner-1", approved_at=datetime(2026, 10, 15, 4, tzinfo=timezone.utc),
        settlement_digest=digest, **fields,
    ).use_id is None


def test_NFR04_settlement_digest_canonical_format():
    at = datetime(2026, 10, 15, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    fields = dict(
        recovery_id="r", node_id="n", expected_node_state=DeviceState.FAULT,
        expected_recovery_status=RecoveryStatus.READY,
        expected_commands={}, expected_sessions={},
        evidence=(RecoveryEvidence("e", "c", at, execution_outcome=CommandOutcome.FAILED),),
        usage_settlements=(),
    )
    canonical = (
        '{"closed_at":null,"end_reason":null,"evidence":[{"evidence_id":"e",'
        '"execution_outcome":"failed","observed_at":"2026-10-15T03:00:00+00:00",'
        '"physical_state_synced":null,"safety_confirmed":null,"session_id":"c"}],'
        '"expected_commands":{},"expected_node_state":"FAULT","expected_recovery_status":"ready",'
        '"expected_sessions":{},"node_id":"n","recovery_id":"r","usage_settlements":[],'
        '"use_id":null,"v":1}'
    )
    assert settlement_digest(**fields) == sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("change", [
    {"closed_at": datetime(2026, 10, 15, 3, 0)},
    {"evidence": (RecoveryEvidence("evidence-1", "connection-2", datetime(2026, 10, 15, 3, 0)),)},
    {"evidence": (
        RecoveryEvidence("evidence-1", "c", datetime(2026, 10, 15, tzinfo=timezone.utc)),
        RecoveryEvidence("evidence-1", "c", datetime(2026, 10, 15, tzinfo=timezone.utc)),
    )},
])
def test_NFR04_settlement_digest_rejects_ambiguous_input(change):
    with pytest.raises(ValueError):
        settlement_digest(**_digest_fields(**change))


def _settlement_fields(settlement):
    return {
        name: getattr(settlement, name) for name in (
            "recovery_id", "node_id", "expected_node_state", "expected_recovery_status",
            "expected_commands", "expected_sessions", "evidence", "usage_settlements",
            "use_id", "closed_at", "end_reason",
        )
    }
