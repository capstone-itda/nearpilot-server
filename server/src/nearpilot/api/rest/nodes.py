"""FR-12A NFR-04 NFR-12 소유자용 기기 관리 REST: 원격 재시작(#235)과 기기 접속 설정 묶음(#234).

토큰으로 계정을 식별해 `NodeAdminService.restart`에 넘긴다. 소유권과 결과는 상태 관리가 정한다 (NFR-07).
응답에 사용자 ID, 토큰, 기기 연결 세션 ID를 넣지 않는다 (NFR-08). MCP에는 열지 않는다.
묶음 응답은 기기 비밀번호를 담는다. 캐시하지 않게 하고(`no-store`) 로그에 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.shared.enums import ReasonCode, RestartOutcome
from nearpilot.shared.interfaces import NodeAdminService
from nearpilot.shared.models import Account

log = logging.getLogger(__name__)

_REJECTED_STATUS: dict[RestartOutcome, int] = {
    RestartOutcome.NOT_OWNER: 403,
    RestartOutcome.NODE_OFFLINE: 409,
    RestartOutcome.NOT_SENT: 503,
}


def _error(status: int, reason: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status, headers=headers)


def build_node_admin_router(nodes: NodeAdminService, authenticator: Authenticator) -> APIRouter:
    router = APIRouter()

    async def authenticate(request: Request) -> Account | JSONResponse:
        try:
            return await asyncio.to_thread(authenticator.authenticate, request.headers.get("authorization"))
        except AuthenticationError as error:
            if error.reason is ReasonCode.INVALID_TOKEN:
                return _error(401, ReasonCode.INVALID_TOKEN.value, {"WWW-Authenticate": "Bearer"})
            return _error(500, ReasonCode.INTERNAL_ERROR.value)

    @router.post("/admin/nodes/{node_id}/restart")
    async def restart_node(node_id: str, request: Request) -> JSONResponse:
        account = await authenticate(request)
        if isinstance(account, JSONResponse):
            return account
        try:
            result = await asyncio.to_thread(nodes.restart, account.user_id, node_id)
        except Exception:  # noqa: BLE001 - 예외 내용은 응답에 넣지 않는다
            log.exception("기기 재시작 요청 중 예외: node_id=%s", node_id)
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        if result.outcome is RestartOutcome.SENT and result.restart_id and result.expires_at:
            return JSONResponse({"node_id": result.node_id, "restart_id": result.restart_id,
                                 "expires_at": result.expires_at.isoformat()}, status_code=202)
        if result.outcome not in _REJECTED_STATUS:
            log.error("기기 재시작 결과에 상태 코드가 없다: %s", result.outcome.value)
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return _error(_REJECTED_STATUS[result.outcome], result.outcome.value)

    @router.get("/admin/nodes/{node_id}/provisioning")
    async def node_provisioning(node_id: str, request: Request) -> JSONResponse:
        account = await authenticate(request)
        if isinstance(account, JSONResponse):
            return account
        try:
            bundle = await asyncio.to_thread(nodes.provisioning, account.user_id, node_id)
        except PermissionError:
            return _error(403, RestartOutcome.NOT_OWNER.value)
        except LookupError:
            return _error(404, "NOT_FOUND")
        except Exception as error:  # noqa: BLE001 - 내용(비밀번호가 있을 수 있다)은 응답과 로그에 쓰지 않는다
            log.error("기기 접속 설정 조회 중 예외: node_id=%s type=%s", node_id, type(error).__name__)
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        return JSONResponse({
            "node_id": bundle.node_id, "mqtt_user": bundle.mqtt_user, "mqtt_password": bundle.mqtt_password,
            "mqtt_host": bundle.mqtt_host, "mqtt_port": bundle.mqtt_port,
            "capability": bundle.capability, "occupancy": bundle.occupancy.value,
        }, headers={"Cache-Control": "no-store"})

    return router
