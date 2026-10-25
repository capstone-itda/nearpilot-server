"""FR-03 승인 조회·결정 서비스와 실제 SQLite (specs/core-approval-service/spec.md, #186).

방문자의 `request_access`가 만든 대기 승인을 소유자가 조회하고 결정한다.
"""

from __future__ import annotations

from datetime import timedelta

from nearpilot.core.approval import OwnerApprovalService
from nearpilot.shared.enums import Action, ApprovalDecisionReason, ApprovalStatus, Verdict

from _world import NOW, OWNER, VISITOR_A, VISITOR_B, World


def _setup(clock=lambda: NOW):
    world = World()
    decision = world.access.request_access(VISITOR_A, "locker-2", Action.OPEN, "a1")
    assert decision.verdict is Verdict.NEED_APPROVAL
    return world, OwnerApprovalService(world.repo, clock)


def test_FR03_real_db_approve_flow():
    world, service = _setup()
    try:
        [pending] = service.pending_for(OWNER)
        assert pending.node_id == "locker-2"
        assert pending.status is ApprovalStatus.PENDING
        assert service.pending_for(VISITOR_A) == []
        assert service.get(VISITOR_B, pending.approval_id).reason is ApprovalDecisionReason.NOT_AUTHORIZED

        approved = service.decide(OWNER, pending.approval_id, ApprovalStatus.APPROVED)
        again = service.decide(OWNER, pending.approval_id, ApprovalStatus.APPROVED)

        assert approved.reason is ApprovalDecisionReason.OK
        assert approved.approval.status is ApprovalStatus.APPROVED
        assert approved.approval.valid_until == NOW + timedelta(seconds=3600)
        assert again.reason is ApprovalDecisionReason.ALREADY_APPLIED
        assert again.approval == approved.approval
        assert service.pending_for(OWNER) == []
    finally:
        world.close()


def test_FR03_real_db_decide_reasons():
    world, service = _setup()
    try:
        [pending] = service.pending_for(OWNER)
        service.decide(OWNER, pending.approval_id, ApprovalStatus.APPROVED)
        opposite = service.decide(OWNER, pending.approval_id, ApprovalStatus.DENIED)
        assert opposite.reason is ApprovalDecisionReason.ALREADY_DECIDED
        assert opposite.approval.status is ApprovalStatus.APPROVED
    finally:
        world.close()

    world, service = _setup()
    try:
        [pending] = service.pending_for(OWNER)
        world.conn.execute(
            "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
            "approval_ttl_sec, approval_mode, max_uses) "
            "VALUES ('pol-locker-2', 2, 'public', 'restricted', ?, 3600, 'one_time', 1)", (OWNER,))
        world.conn.execute("UPDATE access_policy SET current_version = 2 WHERE policy_id = 'pol-locker-2'")
        mismatch = service.decide(OWNER, pending.approval_id, ApprovalStatus.APPROVED)
        assert mismatch.reason is ApprovalDecisionReason.POLICY_VERSION_MISMATCH
    finally:
        world.close()

    late = NOW + timedelta(seconds=world.settings.approval_pending_ttl_sec + 1)
    world, service = _setup(clock=lambda: late)
    try:
        assert service.pending_for(OWNER) == []
        approval_id = world.conn.execute("SELECT approval_id FROM approval").fetchone()[0]
        expired = service.decide(OWNER, approval_id, ApprovalStatus.APPROVED)
        assert expired.reason is ApprovalDecisionReason.REQUEST_EXPIRED
        assert expired.approval.status is ApprovalStatus.PENDING
    finally:
        world.close()
