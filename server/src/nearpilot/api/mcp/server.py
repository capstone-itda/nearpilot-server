"""FR-08·FR-09 MCP 엔드포인트와 request_use 도구.

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
from nearpilot.api.mcp.response import denied_response, host_response
from nearpilot.shared.enums import Action, ReasonCode
from nearpilot.shared.interfaces import CoreService
from nearpilot.shared.models import Account, TargetSpec, UseRequest

log = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "기기 사용을 요청한다. target_spec 은 nearest:locker, locker-1 형식이고 "
    "action 은 open, on, off 중 하나다. 같은 요청을 다시 보낼 때는 같은 request_id 를 쓴다."
)


def build_app(
    core: CoreService, authenticator: Authenticator, clock: Callable[[], datetime]
) -> Starlette:
    mcp = MCPServer("nearpilot")

    @mcp.tool(name="request_use", description=_TOOL_DESCRIPTION)
    async def request_use(target_spec: str, action: str, request_id: str, ctx: Context) -> dict:
        try:
            account = ctx.request_context.request.scope.get(ACCOUNT_KEY)
            if not isinstance(account, Account):  # 미들웨어를 거치지 않았다. 기본값은 거부다
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

    app = mcp.streamable_http_app(json_response=True, stateless_http=True)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    return app
