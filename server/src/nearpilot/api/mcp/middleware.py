"""FR-08 /mcp 앞의 토큰 검증.

모든 HTTP 요청에서 `Authorization: Bearer` 를 검증한다. 실패하면 MCP 로 넘기지 않는다.
검증한 계정은 요청 scope 에 담고, 도구가 이 값으로 계정을 정한다.
"""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.shared.enums import ReasonCode

ACCOUNT_KEY = "nearpilot.account"


class AuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self._authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan 은 통과시킨다
            await self.app(scope, receive, send)
            return
        raw = next((v for k, v in scope["headers"] if k == b"authorization"), None)
        header = raw.decode("latin-1") if raw is not None else None
        try:
            # 계정 조회는 동기 함수다. 이벤트 루프를 막지 않는다.
            scope[ACCOUNT_KEY] = await asyncio.to_thread(self._authenticator.authenticate, header)
        except AuthenticationError as error:
            if error.reason is ReasonCode.INVALID_TOKEN:
                response = JSONResponse({"error": ReasonCode.INVALID_TOKEN.value}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
            else:
                response = JSONResponse({"error": ReasonCode.INTERNAL_ERROR.value}, status_code=500)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
