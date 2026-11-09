"""FR-08·FR-09 MCP 엔드포인트와 도구.

stateless + JSON 응답이다. 요청마다 토큰으로 계정을 정하고, 판정은 `CoreService.request_use` 가 한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, WithJsonSchema
from starlette.applications import Starlette

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.mcp.middleware import ACCOUNT_KEY, AuthMiddleware
from nearpilot.api.mcp.response import (
    denied_response, device_state_denied, device_state_response, devices_response, host_response,
    status_denied, status_response,
)
from nearpilot.shared.descriptor import is_name
from nearpilot.shared.enums import ReasonCode
from nearpilot.shared.interfaces import CoreService
from nearpilot.shared.models import Account, TargetSpec, UseRequest

log = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "기기 사용을 요청한다. target_spec 은 nearest:{kind}(가장 가까운 기기), 기기 ID(예: locker-1), "
    "any:{kind}(허용된 종류의 전역 배정) 중 하나다. "
    "네 번째 형식은 거리 선택자 near:{kind}:{start}:{count} 와 far:{kind}:{start}:{count} 다. "
    "서버가 요청 순간 방문자와의 거리 순서로 기기를 고른다. near 는 가까운 순, far 는 먼 순이다. "
    "start 는 1부터 센 시작 위치이고, count 는 1 이상의 개수 또는 all(끝까지)이다. "
    "예: '가까운 조명 2개 꺼 줘' 는 near:light:1:2 와 off, '제일 먼 조명 켜 줘' 는 far:light:1:1 과 on, "
    "'나 있는 쪽 빼고 다 켜 줘' 는 near:light:2:all 과 on 이다. "
    "'왼쪽 조명' 처럼 방향이나 장소 이름으로 가리키면 거리 선택자로 바꾸지 말고 사용자에게 되묻는다. "
    "거리 선택자의 응답은 devices(기기별 node_id, verdict, reason, use_id), selected_count(실제로 고른 개수), "
    "similar(경계의 기기들이 비슷한 거리라 고른 결과가 다를 수 있음)를 더 담는다. "
    "거리 선택자 요청이 NEED_APPROVAL 이면 devices 의 node_id 로 request_access 를 부른다. "
    "action 은 find_available_devices 나 get_device_state 가 "
    "돌려준 그 기기의 actions[].name 중 하나다. 각 동작의 description 이 하는 일을 설명한다. "
    "선택 인자 arguments 는 객체이고, 그 동작의 actions[].input_schema 를 따른다. "
    "범위 밖 값과 스키마에 맞지 않는 인자는 실행하지 않고 거부한다. 생략한 인자는 그 기기의 마지막 성공값이나 "
    "서버 기본값으로 채운다. 같은 요청을 다시 보낼 때는 같은 request_id 와 같은 인자를 쓴다."
)

_ACCESS_DESCRIPTION = (
    "제한 기기의 접근 승인을 요청한다. node_id 는 기기 ID 이고 action 은 그 기기의 actions[].name 중 하나다. "
    "승인 요청은 실행 허가가 아니다. 소유자가 승인한 뒤 request_use 를 호출한다. "
    "같은 요청을 다시 보낼 때는 같은 request_id 를 쓴다."
)
_RELEASE_DESCRIPTION = "본인의 사용(use_id)을 종료한다. 반납 검사를 거쳐 종료한다."
_STATUS_DESCRIPTION = "본인의 사용(use_id) 상태와 종료 이력을 조회한다."
_DEVICES_DESCRIPTION = (
    "등록 비콘의 최근 수신으로 확인한 같은 장소의 공개·온라인·미점유 기기를 조회한다. "
    "기기 이름을 모르면 이 도구로 node_id, kind, actions 를 먼저 확인한다. "
    "기기별 근접 확률은 목록을 제한하지 않는다. 비콘 수신이 없으면 빈 목록이다. "
    "access 가 operable 이면 사용을 요청할 수 있고 approval_required 이면 승인이 필요하다. "
    "조회는 실행 허가가 아니다. 실제 실행은 request_use 가 지정 방식에 따라 별도로 판정한다."
)
_DEVICE_STATE_DESCRIPTION = "기기(node_id)의 상태를 조회한다. 근접 검사는 하지 않는다."

# MCP SDK 가 127.0.0.1 바인딩에 자동으로 켜는 DNS 리바인딩 방어의 기본 허용 목록이다.
_LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOCAL_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")


def _account_of(ctx: Context) -> Account | None:
    """미들웨어가 검증한 계정. 없으면 None 이다. 기본값은 거부다."""
    account = ctx.request_context.request.scope.get(ACCOUNT_KEY)
    return account if isinstance(account, Account) else None


def _transport_security(public_hosts: Sequence[str]) -> TransportSecuritySettings:
    """localhost 와 공개 Host 만 받는다. 터널로 들어온 요청은 공개 Host 로 온다. 다른 Host 는 421 이다."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*_LOCAL_HOSTS, *public_hosts],
        allowed_origins=[*_LOCAL_ORIGINS, *(f"https://{h}" for h in public_hosts)],
    )


def _action(action: Any) -> str:
    """동작 이름 규칙만 검사한다. 기기가 선언했는지는 판정이 검사한다 (FR-09). 틀리면 ValueError."""
    if not is_name(action):
        raise ValueError("action 형식 오류")
    return action


def _arguments(arguments: Any) -> dict | None:
    """`arguments` 가 객체인지만 검사한다 (FR-09). 안쪽은 판정이 기기 선언으로 검사한다. 틀리면 ValueError.

    문자열 숫자, 실수, 불리언을 바꾸지 않는다.
    """
    if arguments is None:
        return None
    if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
        raise ValueError("arguments 는 객체여야 한다")
    return arguments


def _is_selector(target_spec: object) -> bool:
    """형식이 맞는 거리 선택자인가. 판정 전 거부의 응답 모양을 정할 때만 쓴다 (FR-09)."""
    try:
        return TargetSpec.parse(target_spec).is_selector
    except Exception:  # noqa: BLE001 - 형식이 틀리면 거리 선택자인지 알 수 없다
        return False


def build_app(
    core: CoreService, authenticator: Authenticator, clock: Callable[[], datetime],
    *, public_hosts: Sequence[str] = (),
) -> Starlette:
    mcp = MCPServer("nearpilot")
    # 도구 입력 스키마는 기기 종류와 관계없이 같다 (FR-09). 값은 Any 로 받아서 SDK 가 자료형을 바꾸지 않게 하고
    # 직접 검사한다. 동작별 인자 스키마는 조회 응답의 actions[].input_schema 가 알린다.
    arguments_schema = WithJsonSchema({
        "type": "object",
        "description": "동작 인자. 대상 기기 actions[].input_schema 를 따른다. 인자가 없으면 생략한다."})

    async def request_use(
        target_spec: str, action: str, request_id: str, ctx: Context, arguments: Any = None,
    ) -> dict:
        def deny(reason: ReasonCode) -> dict:
            # 형식이 맞는 거리 선택자는 판정 전에 거부해도 거리 선택자 응답 키를 담는다 (FR-09)
            return denied_response(reason, request_id, selector=_is_selector(target_spec))

        try:
            account = _account_of(ctx)
            if account is None:  # 미들웨어를 거치지 않았다
                return deny(ReasonCode.INTERNAL_ERROR)
            try:
                if not request_id:
                    raise ValueError("request_id 가 비었다")
                req = UseRequest(
                    user_id=account.user_id, request_id=request_id,
                    target=TargetSpec.parse(target_spec), action=_action(action),
                    host_kind=None, received_at=clock(), arguments=_arguments(arguments))
            except ValueError:
                return deny(ReasonCode.INVALID_INPUT)
            # 판정과 실행 인계는 동기 함수다. 이벤트 루프를 막지 않는다.
            return host_response(await asyncio.to_thread(core.request_use, req))
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 거부한다. 예외 내용은 응답에 넣지 않는다
            log.exception("request_use 도구 처리 중 예외")
            return deny(ReasonCode.INTERNAL_ERROR)

    # 스키마는 주석 문자열로 쓸 수 없다. 평가한 주석 객체로 바꾼 뒤 도구로 등록한다.
    request_use.__annotations__.update(arguments=Annotated[Any, arguments_schema, Field(default=None)])
    mcp.tool(name="request_use", description=_TOOL_DESCRIPTION)(request_use)

    @mcp.tool(name="request_access", description=_ACCESS_DESCRIPTION)
    async def request_access(node_id: str, action: str, request_id: str, ctx: Context) -> dict:
        try:
            account = _account_of(ctx)
            if account is None:
                return denied_response(ReasonCode.INTERNAL_ERROR, request_id)
            try:
                if not node_id or not request_id:
                    raise ValueError("node_id 나 request_id 가 비었다")
                act = _action(action)
            except ValueError:
                return denied_response(ReasonCode.INVALID_INPUT, request_id)
            decision = await asyncio.to_thread(
                core.request_access, account.user_id, node_id, act, request_id)
            # 승인 요청은 실행 허가가 아니다. 서비스가 담아도 use_id 는 내보내지 않는다.
            return {**host_response(decision), "use_id": None}
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 거부한다. 예외 내용은 응답에 넣지 않는다
            log.exception("request_access 도구 처리 중 예외")
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

    app = mcp.streamable_http_app(json_response=True, stateless_http=True,
                                 transport_security=_transport_security(public_hosts))
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    return app
