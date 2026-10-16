"""FastAPI 앱과 수명주기 (spec 6절 "경로와 실행").

FastAPI 수명주기가 Runtime 의 시작과 종료를 맡는다. 기존 MCP 앱의 수명주기도 함께 연결한다.
MCP 앱을 루트에 마운트해서 외부 경로는 기존 `/mcp` 그대로다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nearpilot.api.mcp.server import build_app
from nearpilot.runtime.assembly import Runtime


def create_app(runtime: Runtime) -> FastAPI:
    mcp_app = build_app(runtime.core, runtime.authenticator, runtime.clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 시작 순서 1~9 를 마친 뒤에만 uvicorn 이 요청 소켓을 연다.
        await asyncio.to_thread(runtime.start)
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            await asyncio.to_thread(runtime.stop)

    app = FastAPI(title="NearPilot", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.runtime = runtime
    app.mount("/", mcp_app)
    return app
