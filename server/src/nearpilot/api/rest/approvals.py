"""FR-03 앱용 승인 REST (architecture.md §5 REST "승인", #183, #186).

세 경로는 토큰으로 계정을 식별해 `ApprovalService`의 `pending_for`, `get`, `decide`에 하나씩 넘긴다.
승인 권한, 대기 상태, 정책 버전은 CORE가 검증한다. API는 사유를 바꾸지 않는다 (NFR-07).
응답에 요청자 정보를 넣지 않는다 (NFR-08).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.shared.enums import ApprovalDecisionReason, ApprovalStatus, ReasonCode
from nearpilot.shared.interfaces import ApprovalService
from nearpilot.shared.models import Account, OwnerApproval, OwnerApprovalResult

log = logging.getLogger(__name__)

_STATUS: dict[ApprovalDecisionReason, int] = {
    ApprovalDecisionReason.OK: 200,
    ApprovalDecisionReason.ALREADY_APPLIED: 200,
    ApprovalDecisionReason.NOT_FOUND: 404,
    ApprovalDecisionReason.NOT_AUTHORIZED: 403,
    ApprovalDecisionReason.ALREADY_DECIDED: 409,
    ApprovalDecisionReason.POLICY_VERSION_MISMATCH: 409,
    ApprovalDecisionReason.REQUEST_EXPIRED: 409,
}
_DECISIONS = {ApprovalStatus.APPROVED.value: ApprovalStatus.APPROVED, ApprovalStatus.DENIED.value: ApprovalStatus.DENIED}


def _error(status: int, reason: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status, headers=headers)


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def approval_json(approval: OwnerApproval) -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "node_id": approval.node_id,
        "action": approval.action.value,
        "status": approval.status.value,
        "created_at": _time(approval.created_at),
        "pending_expires_at": _time(approval.pending_until),
        "valid_until": _time(approval.valid_until),
    }


def _result_response(result: OwnerApprovalResult) -> JSONResponse:
    status = _STATUS[result.reason]
    if status != 200:
        return _error(status, result.reason.value)
    if result.approval is None:
        log.error("성공 결과에 승인이 없다")
        return _error(500, ReasonCode.INTERNAL_ERROR.value)
    return JSONResponse(approval_json(result.approval))


def build_approval_router(approvals: ApprovalService, authenticator: Authenticator) -> APIRouter:
    router = APIRouter()

    async def authenticate(request: Request) -> Account | JSONResponse:
        try:
            return await asyncio.to_thread(authenticator.authenticate, request.headers.get("authorization"))
        except AuthenticationError as error:
            if error.reason is ReasonCode.INVALID_TOKEN:
                return _error(401, ReasonCode.INVALID_TOKEN.value, {"WWW-Authenticate": "Bearer"})
            return _error(500, ReasonCode.INTERNAL_ERROR.value)

    async def call(func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except Exception:  # noqa: BLE001 - 예외 내용은 응답에 넣지 않는다
            log.exception("승인 서비스 호출 중 예외")
            return None

    @router.get("/approvals")
    async def list_approvals(request: Request) -> JSONResponse:
        account = await authenticate(request)
        if isinstance(account, JSONResponse):
            return account
        pending = await call(approvals.pending_for, account.user_id)
        if pending is None:
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return JSONResponse([approval_json(a) for a in pending])

    @router.get("/approvals/{approval_id}")
    async def get_approval(approval_id: str, request: Request) -> JSONResponse:
        account = await authenticate(request)
        if isinstance(account, JSONResponse):
            return account
        result = await call(approvals.get, account.user_id, approval_id)
        if result is None:
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return _result_response(result)

    @router.post("/approvals/{approval_id}/decision")
    async def decide(approval_id: str, request: Request) -> JSONResponse:
        account = await authenticate(request)
        if isinstance(account, JSONResponse):
            return account
        try:
            body = await request.json()
        except ValueError:
            body = None
        raw = body.get("decision") if isinstance(body, dict) else None
        decision = _DECISIONS.get(raw) if isinstance(raw, str) else None
        if decision is None:
            return _error(400, ReasonCode.INVALID_INPUT.value)
        result = await call(approvals.decide, account.user_id, approval_id, decision)
        if result is None:
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return _result_response(result)

    return router
