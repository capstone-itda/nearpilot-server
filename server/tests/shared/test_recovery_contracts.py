"""FR-18·FR-19·NFR-04의 실행 종류, 복원과 복구 경계 검사."""

import inspect
from dataclasses import fields, replace
from datetime import datetime

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, ApprovalUsageState, Capability, CommandOutcome, DeviceState,
    ExecutionKind, ExecutionStatus, Occupancy, RecoveryBlocker, RecoveryOutcome,
    RecoveryReason, RecoveryStatus, StoreOutcome, TargetKind, TrustState,
)
from nearpilot.shared.interfaces import RecoveryService, Repository
from nearpilot.shared.models import (
    ApprovalUsageSettlement, ExecutionContext, NodeInfo, RecoveryApprovalResult,
    RecoveryCompletion, RecoveryEvidence, RecoveryReadiness, RecoveryRecord,
    RecoverySettlement, RestoreBundle, StoreResult, TargetSpec, UseRequest,
)

NOW = datetime(2026, 10, 13)
REQUEST = UseRequest('u1', 'r1', TargetSpec(TargetKind.EXPLICIT, Capability.LOCKER, 'locker-1'),
                     Action.OPEN, 'chatgpt', NOW)
EVIDENCE = RecoveryEvidence('ev1', 'connection-1', NOW, CommandOutcome.SUCCEEDED, True, True)
SETTLEMENT = RecoverySettlement('rec1', 'locker-1', 'owner', NOW, 'digest1',
                                DeviceState.FAULT, RecoveryStatus.READY,
                                {'cmd1': ExecutionStatus.UNKNOWN}, {}, (EVIDENCE,), ())


def test_FR19_rejected_is_distinct_from_not_sent_and_failure():
    assert CommandOutcome.REJECTED.value == ExecutionStatus.REJECTED.value == 'rejected'
    assert CommandOutcome.REJECTED not in (CommandOutcome.NOT_SENT, CommandOutcome.FAILED)


@pytest.mark.parametrize('kind', [ExecutionKind.RENTAL_INITIAL, ExecutionKind.RENTAL_REOPERATION])
def test_FR18_FR21_rental_execution_requires_existing_use_link(kind):
    ctx = ExecutionContext('ir1', REQUEST, 'locker-1', 'use1', None, Settings(), kind)
    assert ctx.use_id == 'use1'
    with pytest.raises(ValueError):
        replace(ctx, use_id=None)


def test_FR19_shared_execution_has_no_use_session():
    ctx = ExecutionContext('ir1', REQUEST, 'light-1', None, None, Settings(), ExecutionKind.SHARED)
    with pytest.raises(ValueError):
        replace(ctx, use_id='use1')


def test_FR19_recovery_settlement_copies_expected_states_and_evidence():
    source = {'cmd1': ExecutionStatus.UNKNOWN}
    evidence = [EVIDENCE]
    settlement = replace(SETTLEMENT, expected_commands=source, evidence=evidence)
    source['cmd1'] = ExecutionStatus.SUCCEEDED
    evidence.clear()
    assert settlement.expected_commands['cmd1'] == ExecutionStatus.UNKNOWN
    assert settlement.evidence == (EVIDENCE,)
    with pytest.raises(TypeError):
        settlement.expected_commands['cmd1'] = ExecutionStatus.SUCCEEDED


@pytest.mark.parametrize('state', [ApprovalUsageState.RESERVED, ApprovalUsageState.HELD])
def test_FR19_recovery_usage_settlement_requires_final_state(state):
    with pytest.raises(ValueError):
        ApprovalUsageSettlement('usage1', ApprovalUsageState.HELD, state)


def test_FR19_recovery_session_end_is_complete_or_absent():
    with pytest.raises(ValueError):
        replace(SETTLEMENT, use_id='use1')
    with pytest.raises(ValueError):
        replace(SETTLEMENT, closed_at=NOW)
    with pytest.raises(ValueError):
        replace(SETTLEMENT, settlement_digest='')


def test_FR19_restore_bundle_freezes_record_lists_and_preserves_times():
    node = NodeInfo('locker-1', 'place1', Capability.LOCKER, Occupancy.RENTAL,
                    TrustState.TRUSTED, DeviceState.FAULT, None, NOW)
    record = RecoveryRecord('rec1', 'locker-1', ('ir1',), ('cmd1',), None, NOW, RecoveryStatus.READY)
    records = [record]
    bundle = RestoreBundle(node, NOW, recoveries=records)
    records.clear()
    assert bundle.recoveries == (record,)
    assert bundle.recoveries[0].created_at == NOW


@pytest.mark.parametrize('model', [RecoveryReadiness, RecoveryApprovalResult])
def test_FR11_NFR08_recovery_api_models_have_no_occupant_or_user_fields(model):
    assert not {f.name for f in fields(model)} & {'user_id', 'occupant', 'occupant_user_id', 'approved_by_user_id'}


def test_NFR04_recovery_completion_preserves_original_settlement():
    completed = RecoveryCompletion(SETTLEMENT, NOW)
    record = RecoveryRecord('rec1', 'locker-1', ('ir1',), ('cmd1',), None, NOW,
                             RecoveryStatus.RECOVERED, (EVIDENCE,), completed)
    assert StoreResult(StoreOutcome.ALREADY_APPLIED, recovery=record).recovery.completion == completed


def test_NFR04_fake_recovery_service_separates_query_and_approval():
    readiness = RecoveryReadiness('locker-1', None, RecoveryStatus.NOT_READY,
                                  (RecoveryBlocker.NO_RECOVERY_REQUIRED,), (), NOW)
    class FakeRecovery:
        def readiness(self, user_id, node_id):
            return readiness

        def approve(self, user_id, node_id, recovery_id):
            return RecoveryApprovalResult(recovery_id, RecoveryOutcome.REJECTED,
                                           RecoveryReason.RECOVERY_MISMATCH, readiness)

    service = FakeRecovery()
    assert isinstance(service, RecoveryService)
    assert service.readiness('owner', 'locker-1').recovery_id is None
    assert service.approve('owner', 'locker-1', 'rec1').outcome == RecoveryOutcome.REJECTED


def test_FR12A_place_owner_check_returns_only_bool():
    signature = inspect.signature(Repository.is_place_owner)
    assert list(signature.parameters) == ["self", "user_id", "node_id"]
    assert signature.return_annotation in ("bool", bool)
