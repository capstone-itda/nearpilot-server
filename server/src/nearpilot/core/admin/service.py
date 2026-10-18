"""`AdminService` 구현 (specs/core-ops-cli/spec.md §6 "AdminService와 seed").

`core` 계층이므로 SQL을 직접 쓰지 않고 읽기는 `shared.Repository`, 쓰기는 `shared.AdminRepository`
Protocol에만 의존한다 (`AGENTS.md` 의존 규칙 4). 여기서는 행위자 검사와 정책 리비전 번호만 정한다.
저장 규칙과 감사는 `AdminRepository`가 맡는다.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import Any

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason, Role, TrustState
from nearpilot.shared.interfaces import AdminRepository, Repository
from nearpilot.shared.models import (
    AccountIssue,
    AccountRegistration,
    AdminWriteResult,
    BeaconRegistration,
    PlaceAssignment,
    PolicyRevision,
    PolicySettings,
    PolicyWriteResult,
)
from nearpilot.shared.tokens import hash_token


def default_token() -> str:
    """토큰 원문을 만든다. 32바이트 무작위 값이다 (FR-01, #29)."""
    return secrets.token_urlsafe(32)


def _rejected(reason: AdminWriteReason) -> AdminWriteResult:
    return AdminWriteResult(outcome=AdminWriteOutcome.REJECTED, reason=reason)


def _policy_content(policy: PolicyRevision | PolicySettings) -> tuple[object, ...]:
    """정책 내용만 모은다. 정책 ID, 버전, 기기는 내용이 아니다."""
    return (
        policy.visibility, policy.mode, policy.approver_user_id, policy.approval_mode,
        policy.approval_ttl_sec, policy.max_uses,
    )


class DefaultAdminService:
    """`AdminService` Protocol 구현."""

    def __init__(
        self, repository: Repository, admin: AdminRepository, *, new_token: Callable[[], str] = default_token
    ) -> None:
        self._repository = repository
        self._admin = admin
        self._new_token = new_token

    def register_account(self, user_id: str, name: str, role: Role) -> AccountIssue:
        token = self._new_token()
        result = self._admin.register_account(AccountRegistration(user_id, name, role, hash_token(token)))
        if result.outcome == AdminWriteOutcome.APPLIED:
            return AccountIssue(result, token)
        return AccountIssue(result, None)

    def register_beacon(self, req: BeaconRegistration) -> AdminWriteResult:
        return self._admin.register_beacon(req)

    def assign_place(self, actor_user_id: str, req: PlaceAssignment) -> AdminWriteResult:
        """행위자가 장소의 소유자여야 한다. 다른 장소에 있는 기기를 옮기려면 그 장소의 소유자여야 한다."""
        if actor_user_id != req.owner_user_id:
            return _rejected(AdminWriteReason.NOT_PLACE_OWNER)
        place_of = {node.node_id: node.place_id for node in self._repository.nodes()}
        for node_id in req.node_ids:
            current_place = place_of.get(node_id)
            if current_place is None or current_place == req.place_id:
                continue
            if not self._repository.is_place_owner(actor_user_id, node_id):
                return _rejected(AdminWriteReason.NOT_PLACE_OWNER)
        return self._admin.assign_place(req)

    def set_node_trust(self, actor_user_id: str, node_id: str, trust_state: TrustState) -> AdminWriteResult:
        denied = self._deny_unless_node_owner(actor_user_id, node_id)
        if denied is not None:
            return denied
        return self._admin.set_node_trust(node_id, trust_state)

    def set_release_policy(
        self, actor_user_id: str, node_id: str, release_policy: Mapping[str, Any]
    ) -> AdminWriteResult:
        denied = self._deny_unless_node_owner(actor_user_id, node_id)
        if denied is not None:
            return denied
        return self._admin.set_release_policy(node_id, release_policy)

    def set_policy(self, actor_user_id: str, settings: PolicySettings) -> PolicyWriteResult:
        """내용이 현재 정책과 같으면 쓰지 않는다. 다르면 현재 버전을 기대값으로 새 리비전을 추가한다."""
        node_id = settings.node_id
        current = self._current_policy(node_id)
        current_version = current.version if current is not None else None

        denied = self._deny_unless_node_owner(actor_user_id, node_id)
        if denied is not None:
            return PolicyWriteResult(denied, current_version, False)
        if current is not None and _policy_content(current) == _policy_content(settings):
            applied = AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)
            return PolicyWriteResult(applied, current.version, False)

        revision = PolicyRevision(
            policy_id=current.policy_id if current is not None else f"pol-{node_id}",
            version=current.version + 1 if current is not None else 1,
            node_id=node_id,
            visibility=settings.visibility,
            mode=settings.mode,
            approver_user_id=settings.approver_user_id,
            approval_mode=settings.approval_mode,
            approval_ttl_sec=settings.approval_ttl_sec,
            max_uses=settings.max_uses,
        )
        result = self._admin.set_policy(revision, expected_version=current_version)
        if result.outcome == AdminWriteOutcome.APPLIED:
            return PolicyWriteResult(result, revision.version, True)
        return PolicyWriteResult(result, current_version, False)

    def _deny_unless_node_owner(self, actor_user_id: str, node_id: str) -> AdminWriteResult | None:
        """기기 단위 쓰기의 행위자 검사. 통과하면 None, 아니면 거부 결과를 돌려준다.

        장소에 배치되지 않은 기기는 소유자가 없으므로 거부한다.
        """
        if not any(node.node_id == node_id for node in self._repository.nodes()):
            return _rejected(AdminWriteReason.NODE_NOT_FOUND)
        if not self._repository.is_place_owner(actor_user_id, node_id):
            return _rejected(AdminWriteReason.NOT_PLACE_OWNER)
        return None

    def _current_policy(self, node_id: str) -> PolicyRevision | None:
        try:
            return self._repository.current_policy(node_id)
        except LookupError:
            return None
