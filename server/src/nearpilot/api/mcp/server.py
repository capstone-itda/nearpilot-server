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
from nearpilot.shared.config import is_light_color
from nearpilot.shared.enums import Action, Capability, ReasonCode
from nearpilot.shared.interfaces import CoreService
from nearpilot.shared.models import Account, LightSetting, TargetSpec, UseRequest

log = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "기기 사용을 요청한다. target_spec 은 nearest:locker, locker-1 형식이고 "
    "action 은 open, close, on, off 중 하나다. close 는 본인이 대여 중인 사물함을 닫는다. 반납이 아니다. "
    "조명(nearest:light, light-1)의 on 에만 선택 인자 brightness 와 color 를 쓸 수 있다. "
    "brightness 는 0 이상 k 이하의 정수다. k 는 find_available_devices 나 get_device_state 의 "
    "controls.brightness_max 다. color 는 #RRGGBB 형식이고, controls.color 가 true 인 기기만 받는다. "
    "범위 밖 값과 지원하지 않는 인자는 실행하지 않고 거부한다. 생략한 인자는 그 조명의 마지막 설정을 유지한다. "
    "이미 켜진 조명에도 on 으로 밝기와 색을 바꾼다. 끄려면 brightness 0 대신 off 를 쓴다. "
    "같은 요청을 다시 보낼 때는 같은 request_id 와 같은 인자를 쓴다."
)

_ACCESS_DESCRIPTION = (
    "제한 기기의 접근 승인을 요청한다. node_id 는 locker-1 형식이고 action 은 open, on, off 중 하나다. "
    "승인 요청은 실행 허가가 아니다. 소유자가 승인한 뒤 request_use 를 호출한다. "
    "같은 요청을 다시 보낼 때는 같은 request_id 를 쓴다."
)
_RELEASE_DESCRIPTION = "본인의 사용(use_id)을 종료한다. 반납 검사를 거쳐 종료한다."
_STATUS_DESCRIPTION = "본인의 사용(use_id) 상태와 종료 이력을 조회한다."
_DEVICES_DESCRIPTION = (
    "주변에서 쓸 수 있는 기기 목록을 조회한다. access 가 operable 이면 바로 요청할 수 있고 "
    "approval_required 이면 승인이 필요하다."
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


def _light_input(target: TargetSpec, action: Action, brightness: Any, color: Any) -> LightSetting | None:
    """조명 설정 인자의 자료형, 형식, 대상 기능과 동작을 검사한다 (FR-09). 틀리면 ValueError.

    문자열 숫자, 실수, 불리언을 정수로 바꾸지 않는다. 기기별 지원과 상한은 판정이 검사한다.
    """
    if brightness is None and color is None:
        return None
    if target.capability is not Capability.LIGHT or action is not Action.ON:
        raise ValueError("조명 on 이 아닌 요청의 인자")
    if brightness is not None and (type(brightness) is not int or not 0 <= brightness <= 255):
        raise ValueError("brightness 형식 오류")
    if color is not None and not is_light_color(color):
        raise ValueError("color 형식 오류")
    return LightSetting(brightness, color)


def build_app(
    core: CoreService, authenticator: Authenticator, clock: Callable[[], datetime],
    *, public_hosts: Sequence[str] = (), light_brightness_max: int = 255,
) -> Starlette:
    mcp = MCPServer("nearpilot")
    # 스키마는 형식만 알린다. 값은 Any 로 받아서 SDK 가 자료형을 바꾸지 않게 하고 직접 검사한다.
    brightness_schema = WithJsonSchema({
        "type": "integer", "minimum": 0, "maximum": light_brightness_max,
        "description": "조명 밝기. 0 이상 controls.brightness_max 이하의 정수. 조명 on 에만 쓴다."})
    color_schema = WithJsonSchema({
        "type": "string", "pattern": "^#[0-9A-Fa-f]{6}$",
        "description": "조명 색. #RRGGBB. controls.color 가 true 인 조명의 on 에만 쓴다."})

    async def request_use(
        target_spec: str, action: str, request_id: str, ctx: Context,
        brightness: Any = None, color: Any = None,
    ) -> dict:
        try:
            account = _account_of(ctx)
            if account is None:  # 미들웨어를 거치지 않았다
                return denied_response(ReasonCode.INTERNAL_ERROR, request_id)
            try:
                if not request_id:
                    raise ValueError("request_id 가 비었다")
                target, act = TargetSpec.parse(target_spec), Action(action)
                req = UseRequest(
                    user_id=account.user_id, request_id=request_id,
                    target=target, action=act,
                    host_kind=None, received_at=clock(),
                    light=_light_input(target, act, brightness, color))
            except ValueError:
                return denied_response(ReasonCode.INVALID_INPUT, request_id)
            # 판정과 실행 인계는 동기 함수다. 이벤트 루프를 막지 않는다.
            return host_response(await asyncio.to_thread(core.request_use, req))
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 거부한다. 예외 내용은 응답에 넣지 않는다
            log.exception("request_use 도구 처리 중 예외")
            return denied_response(ReasonCode.INTERNAL_ERROR, request_id)

    # 상한은 설정 값이라 주석 문자열로 쓸 수 없다. 평가한 주석 객체로 바꾼 뒤 도구로 등록한다.
    request_use.__annotations__.update(
        brightness=Annotated[Any, brightness_schema, Field(default=None)],
        color=Annotated[Any, color_schema, Field(default=None)])
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
                act = Action(action)
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
