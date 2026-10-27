"""FastAPI 앱과 수명주기 (spec 6절 "경로와 실행").

FastAPI 수명주기가 Runtime 의 시작과 종료를 맡는다. 기존 MCP 앱의 수명주기도 함께 연결한다.
MCP 앱을 루트에 마운트해서 외부 경로는 기존 `/mcp` 그대로다.
수집 전용 Runtime 이면 MCP 앱을 만들지도 마운트하지도 않는다. 판정 요청이 닿을 경로가 없다 (FR-06).
계정·승인 라우터를 MCP 앱보다 먼저 붙인다. 수집 전용에서도 붙인다.
복구 라우터는 계정·승인 라우터 다음에 붙이고, 수집 전용에서도 붙인다. 복구 서비스는 시작 뒤에 생기므로 늦게 연결하는 래퍼(`Runtime.recovery`)를 넘긴다.
`[dashboard]` 설정이 있으면 대시보드 라우터를 MCP 앱보다 먼저 붙인다. 수집 전용이어도 붙인다 (읽기만 한다).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nearpilot.api.mcp.server import build_app
from nearpilot.api.rest.accounts import build_account_router
from nearpilot.api.rest.approvals import build_approval_router
from nearpilot.api.rest.recovery import build_recovery_router
from nearpilot.api.ws.router import create_dashboard_router
from nearpilot.runtime.assembly import Runtime


def create_app(runtime: Runtime) -> FastAPI:
    mcp_app = None if runtime.collect_only else build_app(
        runtime.core, runtime.authenticator, runtime.clock,
        public_hosts=runtime.config.http.public_hosts,
        light_brightness_max=runtime.config.settings.light_brightness_max)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 시작 순서 1~9 를 마친 뒤에만 uvicorn 이 요청 소켓을 연다.
        await asyncio.to_thread(runtime.start)
        try:
            if mcp_app is None:
                yield
            else:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
        finally:
            await asyncio.to_thread(runtime.stop)

    app = FastAPI(title="NearPilot", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.runtime = runtime
    parts, dashboard = runtime.dashboard, runtime.config.dashboard
    if parts is not None and dashboard is not None:
        # MCP 앱이 루트(`/`)를 맡는다. 라우터를 먼저 붙여야 `/dashboard/...` 가 MCP 앱으로 가지 않는다.
        app.include_router(create_dashboard_router(
            hub=parts.hub, snapshot=parts.queries, queries=parts.queries,
            authenticator=runtime.authenticator, settings=dashboard.api))
    # 루트 MCP 마운트보다 먼저 등록한다. 대시보드 설정 없이도 앱용 REST를 연다.
    app.include_router(build_account_router(runtime.admin, runtime.authenticator))
    app.include_router(build_approval_router(runtime.approvals, runtime.authenticator))
    app.include_router(build_recovery_router(runtime.recovery, runtime.authenticator))
    if mcp_app is not None:
        app.mount("/", mcp_app)
    return app
