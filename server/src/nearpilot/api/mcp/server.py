"""FR-08·FR-09 MCP 엔드포인트와 도구.

stateless + JSON 응답이다. 요청마다 토큰으로 계정을 정하고, 판정은 `CoreService.request_use` 가 한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from mcp.server.mcpserver import Context, MCPServer
from starlette.applications import Starlette

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.mcp.middleware import ACCOUNT_KEY, AuthMiddleware
from nearpilot.api.mcp.response import (
    denied_response, device_state_denied, device_state_response, devices_response, host_response,
    status_denied, status_response,
)
from nearpilot.shared.enums import Action, ReasonCode
from nearpilot.shared.interfaces import CoreService
from nearpilot.shared.models import Account, TargetSpec, UseRequest

log = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "기기 사용을 요청한다. target_spec 은 nearest:locker, locker-1 형식이고 "
    "action 은 open, on, off 중 하나다. 같은 요청을 다시 보낼 때는 같은 request_id 를 쓴다."
)

_RELEASE_DESCRIPTION = "본인의 사용(use_id)을 종료한다. 반납 검사를 거쳐 종료한다."
_STATUS_DESCRIPTION = "본인의 사용(use_id) 상태와 종료 이력을 조회한다."
_DEVICES_DESCRIPTION = (
    "주변에서 쓸 수 있는 기기 목록을 조회한다. access 가 operable 이면 바로 요청할 수 있고 "
    "approval_required 이면 승인이 필요하다."
)
_DEVICE_STATE_DESCRIPTION = "기기(node_id)의 상태를 조회한다. 근접 검사는 하지 않는다."


def _account_of(ctx: Context) -> Account | None:
    """미들웨어가 검증한 계정. 없으면 None 이다. 기본값은 거부다."""
    account = ctx.request_context.request.scope.get(ACCOUNT_KEY)
    return account if isinstance(account, Account) else None


def build_app(
    core: CoreService, authenticator: Authenticator, clock: Callable[[], datetime]
) -> Starlette:
    mcp = MCPServer("nearpilot")

    @mcp.tool(name="request_use", description=_TOOL_DESCRIPTION)
    async def request_use(target_spec: str, action: str, request_id: str, ctx: Context) -> dict:
        try:
            account = _account_of(ctx)
            if account is None:  # 미들웨어를 거치지 않았다
                return denied_response(ReasonCode.INTERNAL_ERROR, request_id)
            try:
                if not request_id:
                    raise ValueError("request_id 가 비었다")
                req = UseRequest(
                    user_id=account.user_id, request_id=request_id,
                    target=TargetSpec.parse(target_spec), action=Action(action),
                    host_kind=None, received_at=clock())
            except ValueError:
                return denied_response(ReasonCode.INVALID_INPUT, request_id)
            # 판정과 실행 인계는 동기 함수다. 이벤트 루프를 막지 않는다.
            return host_response(await asyncio.to_thread(core.request_use, req))
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 거부한다. 예외 내용은 응답에 넣지 않는다
            log.exception("request_use 도구 처리 중 예외")
            return denied_response(ReasonCode.INTERNAL_ERROR, request_id)

    @mcp.tool(name="release_use", description=_RELEASE_DESCRIPTION)
    async def release_use(use_id: str, ctx: Context) -> dict:
        try:
            account = _account_of(ctx)
            if account is None:
                return denied_response(ReasonCode.INTERNAL_ERROR)
            if not use_id:
                return denied_response(ReasonCode.INVALID_INPUT)
            return host_response(await asyncio.to_thread(core.release_use, account.user_id, use_id))
        except Exception:  # noqa: BLE001
            log.exception("release_use 도구 처리 중 예외")
            return denied_response(ReasonCode.INTERNAL_ERROR)

    @mcp.tool(name="get_use_status", description=_STATUS_DESCRIPTION)
    async def get_use_status(use_id: str, ctx: Context) -> dict:
        try:
            account = _account_of(ctx)
            if account is None:
                return status_denied(ReasonCode.INTERNAL_ERROR)
            if not use_id:
                return status_denied(ReasonCode.INVALID_INPUT)
            return status_response(
                await asyncio.to_thread(core.get_use_status, account.user_id, use_id))
        except Exception:  # noqa: BLE001
            log.exception("get_use_status 도구 처리 중 예외")
            return status_denied(ReasonCode.INTERNAL_ERROR)

    @mcp.tool(name="find_available_devices", description=_DEVICES_DESCRIPTION)
    async def find_available_devices(ctx: Context) -> dict:
        try:
            account = _account_of(ctx)
            if account is None:
                return devices_response([], ReasonCode.INTERNAL_ERROR)
            return devices_response(
                await asyncio.to_thread(core.find_available_devices, account.user_id))
        except Exception:  # noqa: BLE001
            log.exception("find_available_devices 도구 처리 중 예외")
            return devices_response([], ReasonCode.INTERNAL_ERROR)

    @mcp.tool(name="get_device_state", description=_DEVICE_STATE_DESCRIPTION)
    async def get_device_state(node_id: str, ctx: Context) -> dict:
        try:
            account = _account_of(ctx)
            if account is None:
                return device_state_denied(ReasonCode.INTERNAL_ERROR)
            if not node_id:
                return device_state_denied(ReasonCode.INVALID_INPUT)
            return device_state_response(
                await asyncio.to_thread(core.get_device_state, account.user_id, node_id))
        except Exception:  # noqa: BLE001
            log.exception("get_device_state 도구 처리 중 예외")
            return device_state_denied(ReasonCode.INTERNAL_ERROR)

    app = mcp.streamable_http_app(json_response=True, stateless_http=True)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    return app
