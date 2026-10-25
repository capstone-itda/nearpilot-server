"""FR-12A 대시보드 조회 REST `GET /dashboard/...` 9개.

소유자 토큰만 받는다. 처리 순서는 인증 → 인자 검사 → 조회 접점 → 형식 검사이고, 어느 단계에서든 실패하면 다음으로 가지 않는다.
인증을 인자 검사보다 먼저 해서 인증하지 않은 호출이 인자 규칙을 알아내지 못한다 (spec 6절).
오류 응답은 `{"error": "<사유 코드>"}` 이다 (architecture.md §5 REST 공통 규칙). 응답과 로그에 예외 내용을 쓰지 않는다 — 예외 메시지에 사용자 ID 가 있을 수 있다.
`/dashboard/` 아래에서 조회 9개가 받지 않은 요청(쓰기 메서드, 없는 경로)은 받이 경로가 405, 404 로 막는다. 인증하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.routing import Match

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import SchemaError, render_response
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.shared.enums import ReasonCode, Role, Verdict
from nearpilot.shared.interfaces import DashboardQueries

log = logging.getLogger(__name__)

QUERY_PREFIX = "/dashboard"

_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]


def _error(status: int, reason: ReasonCode, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": reason.value}, status_code=status, headers=headers)


async def _authorize(request: Request, authenticator: Authenticator) -> JSONResponse | None:
    """소유자 토큰이면 None 이고, 아니면 돌려줄 오류 응답이다."""
    try:
        # 계정 조회는 동기 함수다. 이벤트 루프를 막지 않는다.
        account = await asyncio.to_thread(authenticator.authenticate, request.headers.get("authorization"))
    except AuthenticationError as error:  # 토큰 오류가 아니면 서버 오류다. 기본값은 거부다
        if error.reason is ReasonCode.INVALID_TOKEN:
            return _error(401, ReasonCode.INVALID_TOKEN)
        return _error(500, ReasonCode.INTERNAL_ERROR)
    return None if account.role == Role.OWNER else _error(403, ReasonCode.POLICY_DENIED)


def _audit_args(request: Request, limit_max: int) -> tuple[Any, ...]:
    """감사 조회의 `limit`. 없으면 한도다. 1 이상의 10진 정수여야 하고, 한도보다 크면 한도로 줄인다. 틀리면 ValueError."""
    raw = request.query_params.get("limit")
    if raw is None:
        return (limit_max,)
    if not (raw.isascii() and raw.isdigit()):  # 부호, 공백, 밑줄, 전각 숫자를 받지 않는다
        raise ValueError("limit")
    digits = raw.lstrip("0")
    if not digits:  # 0
        raise ValueError("limit")
    if len(digits) > len(str(limit_max)):  # 자릿수가 한도보다 많다. int() 는 아주 긴 수를 거부하므로 먼저 줄인다
        return (limit_max,)
    return (min(int(digits), limit_max),)


def _history_args(request: Request) -> tuple[Any, ...]:
    """이력 조회의 `period`, `verdict`, `nodeId`. period 는 공백뿐이면 안 되고, 값은 고치지 않고 넘긴다. 틀리면 ValueError."""
    params = request.query_params
    period = params.get("period")
    if period is None or not period.strip():
        raise ValueError("period")
    raw = params.get("verdict")
    verdict = None if raw is None else Verdict(raw)  # 판정 값이 아니면 ValueError
    return period, verdict, params.get("nodeId") or None


def add_query_routes(
    router: APIRouter,
    *,
    queries: DashboardQueries,
    names: PersonNames,
    authenticator: Authenticator,
    settings: DashboardSettings,
) -> None:
    """`router` 에 조회 경로 9개와 받이 경로를 붙인다. GET 만 둔다 — 대시보드는 서버에 쓰지 않는다. 소유자 토큰만 받는다."""

    async def serve(
        request: Request,
        kind: str,
        call: Callable[..., Any],
        args: Callable[[], tuple[Any, ...]] | None = None,
        *,
        nullable: bool = False,
    ) -> JSONResponse:
        """경로 하나를 처리한다. args 는 접점에 넘길 인자를 읽고 틀리면 ValueError 를 올린다.

        nullable 이면 접점이 None 을 돌려줄 때 없는 것(404)이다. 아니면 None 은 형식이 틀린 결과다.
        """
        denied = await _authorize(request, authenticator)
        if denied is not None:
            return denied
        try:
            values = () if args is None else args()
        except ValueError:
            return _error(400, ReasonCode.INVALID_INPUT)
        try:
            # 조회 접점은 동기 함수다. 이벤트 루프를 막지 않는다.
            data = await asyncio.to_thread(call, *values)
        except Exception as error:  # noqa: BLE001 - 조회가 실패하면 500 이다. 예외 종류만 남긴다
            log.error("대시보드 조회가 실패했다: kind=%s 예외=%s", kind, type(error).__name__)
            return _error(500, ReasonCode.INTERNAL_ERROR)
        if data is None and nullable:
            return _error(404, ReasonCode.USE_NOT_FOUND)
        try:
            body = render_response(kind, data, names)
        except SchemaError as error:  # 메시지는 경로뿐이다
            log.error("대시보드 조회 결과의 형식이 틀리다: kind=%s 경로=%s", kind, error)
            return _error(500, ReasonCode.INTERNAL_ERROR)
        return JSONResponse(body)

    first_query = len(router.routes)

    @router.get(f"{QUERY_PREFIX}/places")
    async def places(request: Request) -> JSONResponse:
        return await serve(request, "places", queries.places)

    @router.get(f"{QUERY_PREFIX}/uses")
    async def uses(request: Request) -> JSONResponse:
        return await serve(request, "uses", queries.uses)

    @router.get(f"{QUERY_PREFIX}/approvals")
    async def approvals(request: Request) -> JSONResponse:
        return await serve(request, "approvals", queries.approvals)

    @router.get(f"{QUERY_PREFIX}/audit")
    async def audit(request: Request) -> JSONResponse:
        return await serve(request, "audit", queries.audit, lambda: _audit_args(request, settings.audit_limit_max))

    @router.get(f"{QUERY_PREFIX}/history")
    async def history(request: Request) -> JSONResponse:
        return await serve(request, "history", queries.history, lambda: _history_args(request))

    @router.get(f"{QUERY_PREFIX}/requests/{{request_id}}")
    async def request_progress(request: Request, request_id: str) -> JSONResponse:
        return await serve(request, "request", queries.request, lambda: (request_id,), nullable=True)

    @router.get(f"{QUERY_PREFIX}/trust")
    async def trust(request: Request) -> JSONResponse:
        return await serve(request, "trust", queries.trust)

    @router.get(f"{QUERY_PREFIX}/rentals/{{node_id}}")
    async def rental(request: Request, node_id: str) -> JSONResponse:
        return await serve(request, "rental", queries.rental, lambda: (node_id,), nullable=True)

    @router.get(f"{QUERY_PREFIX}/health")
    async def health(request: Request) -> JSONResponse:
        return await serve(request, "health", queries.health)

    query_routes = router.routes[first_query:]  # 위 조회 경로 9개

    # 조회 9개 뒤에 등록한다. 앞에 두면 GET 조회가 이 경로로 간다.
    # 조립한 앱은 MCP 앱을 루트(`/`)에 마운트한다. 메서드만 다른 조회 경로는 일부만 맞아서 MCP 앱(완전히 맞음)에 밀린다.
    # 모든 메서드를 받는 이 경로가 `/dashboard/` 아래를 완전히 맞아야 쓰기와 없는 경로가 MCP 앱으로 가지 않는다.
    @router.api_route(f"{QUERY_PREFIX}/{{rest:path}}", methods=_ALL_METHODS, include_in_schema=False)
    async def unrouted(request: Request) -> JSONResponse:
        """조회 경로가 받지 않은 요청. 인증하지 않고 조회 접점을 부르지 않는다."""
        # 경로 인자 자리까지 포함해 조회 경로 하나와 경로가 맞으면 메서드만 틀린 것이다 (GET 이면 위 조회 경로가 이미 받았다)
        if any(route.matches(request.scope)[0] is not Match.NONE for route in query_routes):
            return _error(405, ReasonCode.INVALID_INPUT, {"Allow": "GET"})
        return _error(404, ReasonCode.USE_NOT_FOUND)  # 조회 경로가 아닌 `/dashboard/...` 는 메서드와 상관없이 없는 경로다
