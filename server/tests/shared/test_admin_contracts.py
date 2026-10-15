"""FR-01·FR-03·FR-12A·FR-22의 공동 저장 경계 검사."""

from dataclasses import fields, replace
from datetime import datetime
from inspect import signature
import secrets

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    AdminWriteOutcome, AdminWriteReason, ApprovalDecisionReason, ApprovalStatus, Step,
)
from nearpilot.shared.interfaces import AdminRepository, Repository
from nearpilot.shared.models import (
    AccountRegistration, AdminWriteResult, ApprovalDecisionRequest, ApprovalDecisionResult,
    DecisionRecord, PlaceAssignment,
)
from nearpilot.shared.tokens import hash_token


def test_FR01_FR08_NFR08_token_hash_known_vector_and_no_normalization():
    assert hash_token('abc') == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    assert hash_token('abc') != hash_token(' abc')
    assert hash_token('abc') != hash_token('ABC')
    digest = hash_token(secrets.token_urlsafe(32))
    assert len(digest) == 64 and digest == digest.lower()
    assert int(digest, 16) >= 0
    assert 'token' not in {f.name for f in fields(AccountRegistration)}


def test_FR03_owner_decision_cannot_be_pending():
    with pytest.raises(ValueError):
        ApprovalDecisionRequest('ap1', 'owner', ApprovalStatus.PENDING, datetime(2026, 10, 13))


@pytest.mark.parametrize('reason', [ApprovalDecisionReason.NOT_FOUND,
                                   ApprovalDecisionReason.ALREADY_DECIDED,
                                   ApprovalDecisionReason.POLICY_VERSION_MISMATCH,
                                   ApprovalDecisionReason.REQUEST_EXPIRED,
                                   ApprovalDecisionReason.NOT_AUTHORIZED])
def test_FR03_conditional_decision_retains_failure_reason(reason):
    result = ApprovalDecisionResult(False, reason, ApprovalStatus.PENDING, None)
    assert not result.applied and result.reason == reason
    assert not result.succeeded


@pytest.mark.parametrize('status', [ApprovalStatus.APPROVED, ApprovalStatus.DENIED])
def test_FR03_same_decision_retry_succeeds_without_reapplying(status):
    valid_until = datetime(2026, 10, 13, 13) if status == ApprovalStatus.APPROVED else None
    original = ApprovalDecisionResult(True, ApprovalDecisionReason.OK, status, valid_until)
    retry = replace(original, applied=False, reason=ApprovalDecisionReason.ALREADY_APPLIED)
    assert original.succeeded and original.applied
    assert retry.succeeded and not retry.applied
    assert (retry.status, retry.valid_until) == (original.status, original.valid_until)


@pytest.mark.parametrize('status', [ApprovalStatus.APPROVED, ApprovalStatus.DENIED])
def test_FR03_opposite_decision_conflict_preserves_existing_result(status):
    valid_until = datetime(2026, 10, 13, 13) if status == ApprovalStatus.APPROVED else None
    conflict = ApprovalDecisionResult(False, ApprovalDecisionReason.ALREADY_DECIDED, status, valid_until)
    assert not conflict.succeeded and not conflict.applied
    assert conflict.status == status and conflict.valid_until == valid_until


def test_FR22_decision_record_freezes_probabilities_and_keeps_applied_settings():
    probabilities = {'locker-1': 0.6}
    settings = Settings(version='v2', threshold_locker=0.9)
    record = DecisionRecord(settings, 'm1', 'c1', 3, probabilities)
    probabilities['locker-1'] = 1.0
    assert record.posteriors == {'locker-1': 0.6}
    assert record.settings is settings
    with pytest.raises(TypeError):
        record.posteriors['locker-1'] = 1.0
    early = DecisionRecord(settings)
    assert early.model_version is early.calib_version is early.window_start is early.window_end is None


def test_FR22_save_decision_accepts_record_instead_of_free_form_context():
    assert list(signature(Repository.save_decision).parameters) == ['self', 'decision', 'record']


def test_FR12A_place_assignment_freezes_input_node_list():
    nodes = ['locker-1']
    assignment = PlaceAssignment('p1', 'owner', 'room', nodes)
    nodes.append('locker-2')
    assert assignment.node_ids == ('locker-1',)


def test_FR12A_fake_admin_repository_and_conflict_result():
    result = AdminWriteResult(AdminWriteOutcome.CONFLICT, AdminWriteReason.STATE_CONFLICT)
    class FakeAdmin:
        def register_account(self, req): return result
        def register_beacon(self, req): return result
        def set_node_trust(self, node_id, trust_state): return result
        def assign_place(self, req): return result
        def set_policy(self, revision, expected_version): return result

    assert isinstance(FakeAdmin(), AdminRepository)
    assert FakeAdmin().assign_place(PlaceAssignment('p1', 'owner', 'room')).outcome == AdminWriteOutcome.CONFLICT


def test_FR04_FR22_announce_and_admin_audit_steps_are_distinct():
    assert Step.ANNOUNCE == 'announce'
    assert Step.ADMIN == 'admin'
    assert Step.ANNOUNCE != Step.FAULT
