"""대시보드 API 연결 함수. 조립이 서버 앱에 MCP 앱보다 먼저 붙인다 (architecture.md §2, 2026-10-23 팀 결정)."""

from __future__ import annotations

from fastapi import APIRouter

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.dashboard import add_query_routes
from nearpilot.api.ws.hub import MonitorHub
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.api.ws.stream import add_stream_route
from nearpilot.shared.interfaces import DashboardQueries, MonitorSnapshot


def create_dashboard_router(
    *,
    hub: MonitorHub,
    snapshot: MonitorSnapshot,
    queries: DashboardQueries,
    authenticator: Authenticator,
    settings: DashboardSettings,
) -> APIRouter:
    """스트림(add_stream_route)과 조회(add_query_routes, names=hub.names)를 붙인 라우터."""
    router = APIRouter()
    add_stream_route(router, hub=hub, snapshot=snapshot, authenticator=authenticator, settings=settings)
    add_query_routes(router, queries=queries, names=hub.names, authenticator=authenticator, settings=settings)
    return router
