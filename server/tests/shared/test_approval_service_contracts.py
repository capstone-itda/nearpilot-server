"""FR-03·NFR-08 승인 조회·결정 접점 (#186, specs/core-approval-service/spec.md)."""

import inspect
from dataclasses import fields

from nearpilot.core.approval import OwnerApprovalService
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.shared.interfaces import ApprovalService, CoreService, Repository
from nearpilot.shared.models import OwnerApproval, OwnerApprovalResult


def _params(func):
    return list(inspect.signature(func).parameters)


def test_FR03_approval_service_contract():
    assert _params(ApprovalService.pending_for) == ["self", "user_id"]
    assert _params(ApprovalService.get) == ["self", "user_id", "approval_id"]
    assert _params(ApprovalService.decide) == ["self", "user_id", "approval_id", "decision"]
    assert _params(Repository.pending_approvals_for) == ["self", "user_id", "now"]
    assert _params(Repository.approval) == ["self", "approval_id"]
    assert _params(Repository.is_approver) == ["self", "user_id", "approval_id"]


def test_FR03_approval_service_is_separate_from_core_service():
    for name in ("pending_for", "get", "decide"):
        assert not hasattr(CoreService, name)


def test_NFR08_owner_approval_has_no_requester():
    names = {f.name for f in fields(OwnerApproval)} | {f.name for f in fields(OwnerApprovalResult)}
    assert not any("user" in name or "requester" in name for name in names)
    assert {f.name for f in fields(OwnerApproval)} == {
        "approval_id", "node_id", "action", "status", "created_at", "pending_until", "valid_until",
    }


def test_FR03_implementations_follow_protocols():
    assert isinstance(OwnerApprovalService(None, lambda: None), ApprovalService)
    for name in ("pending_approvals_for", "approval", "is_approver"):
        assert callable(getattr(SqliteRepository, name))
