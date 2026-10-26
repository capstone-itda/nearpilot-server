"""FR-12A NFR-04 소유자용 FAULT 복구 REST (architecture.md §4 "복구 준비 조회와 복구 승인", §5 REST "복구", #89).

두 경로는 토큰으로 계정을 식별해 `RecoveryService`의 `readiness`, `approve`에 하나씩 넘긴다.
소유권, 준비 판단, 재검사는 CORE가 한다. API는 사유를 바꾸지 않는다 (NFR-07).
응답에 사용자 ID, 토큰, 기기 연결 세션 ID를 넣지 않는다 (NFR-08).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.shared.enums import ReasonCode, RecoveryOutcome, RecoveryReason
from nearpilot.shared.interfaces import RecoveryService
from nearpilot.shared.models import Account, RecoveryApprovalResult, RecoveryEvidence, RecoveryReadiness

log = logging.getLogger(__name__)

_REJECTED_STATUS: dict[RecoveryReason, int] = {
    RecoveryReason.NOT_OWNER: 403,
    RecoveryReason.RECOVERY_MISMATCH: 409,
    RecoveryReason.NOT_READY: 409,
    RecoveryReason.STATE_CONFLICT: 409,
    RecoveryReason.STORE_NOT_APPLIED: 503,
}


def _error(status: int, reason: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status, headers=headers)


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _evidence_json(evidence: RecoveryEvidence) -> dict[str, Any]:
    """근거 ID와 기기 연결 세션 ID는 뺀다. 근거 ID 안에 세션 ID가 들어 있다 (NFR-08)."""
    outcome = evidence.execution_outcome
    return {
        "observed_at": _time(evidence.observed_at),
        "execution_outcome": outcome.value if outcome is not None else None,
        "command_idle": evidence.command_idle,
    }


def readiness_json(readiness: RecoveryReadiness) -> dict[str, Any]:
    return {
        "node_id": readiness.node_id,
        "recovery_id": readiness.recovery_id,
        "status": readiness.status.value,
        "blockers": [blocker.value for blocker in readiness.blockers],
        "evidence": [_evidence_json(e) for e in readiness.evidence],
        "checked_at": _time(readiness.checked_at),
    }


def _approval_json(result: RecoveryApprovalResult) -> dict[str, Any]:
    return {
        "recovery_id": result.recovery_id,
        "outcome": result.outcome.value,
        "reason_code": result.reason_code.value,
        "completed_at": _time(result.completed_at),
        "readiness": readiness_json(result.readiness),
    }


def _approval_response(result: RecoveryApprovalResult) -> JSONResponse:
    reason = result.reason_code
    if result.outcome is RecoveryOutcome.REJECTED:
        if reason not in _REJECTED_STATUS:
            log.error("복구 승인의 거부 사유에 상태 코드가 없다: %s", reason.value)
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return _error(_REJECTED_STATUS[reason], reason.value)
    status = 200 if result.outcome is RecoveryOutcome.COMPLETED else 202
    return JSONResponse(_approval_json(result), status_code=status)


def build_recovery_router(recovery: RecoveryService, authenticator: Authenticator) -> APIRouter:
    router = APIRouter()

    async def authenticate(request: Request) -> Account | JSONResponse:
        try:
            return await asyncio.to_thread(authenticator.authenticate, request.headers.get("authorization"))
        except AuthenticationError as error:
            if error.reason is ReasonCode.INVALID_TOKEN:
                return _error(401, ReasonCode.INVALID_TOKEN.value, {"WWW-Authenticate": "Bearer"})
            return _error(500, ReasonCode.INTERNAL_ERROR.value)

    @router.get("/admin/nodes/{node_id}/recovery")
    async def get_recovery(node_id: str, request: Request) -> JSONResponse:
        account = await authenticate(request)
        if isinstance(account, JSONResponse):
            return account
        try:
            readiness = await asyncio.to_thread(recovery.readiness, account.user_id, node_id)
        except PermissionError:  # 상태 관리의 소유권 없음 예외. 일반 예외보다 먼저 받는다
            return _error(403, RecoveryReason.NOT_OWNER.value)
        except Exception:  # noqa: BLE001 - 예외 내용은 응답에 넣지 않는다
            log.exception("복구 준비 조회 중 예외: node_id=%s", node_id)
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return JSONResponse(readiness_json(readiness))

    @router.post("/admin/nodes/{node_id}/recovery/{recovery_id}/approve")
    async def approve_recovery(node_id: str, recovery_id: str, request: Request) -> JSONResponse:
        account = await authenticate(request)
        if isinstance(account, JSONResponse):
            return account
        try:
            result = await asyncio.to_thread(recovery.approve, account.user_id, node_id, recovery_id)
        except Exception:  # noqa: BLE001 - 소유권 없음은 결과로 온다. 예외는 내용 없이 500이다
            log.exception("복구 승인 중 예외: node_id=%s recovery_id=%s", node_id, recovery_id)
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return _approval_response(result)

    return router
