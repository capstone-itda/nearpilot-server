"""FR-12B 모니터링 WebSocket `/dashboard/stream`.

연결 뒤 첫 메시지 `{"type": "auth", "token": "..."}` 로 소유자 토큰을 인증한다. 통과하면 지금 상태를 보내고 허브의 묶음을 이어서 보낸다.
서버가 보내는 메시지는 모두 `{"v": 1, "events": [...]}` 텍스트 프레임이다. 인증 뒤 클라이언트가 보내는 메시지는 읽고 버린다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.api.ws.hub import MonitorHub, Subscription
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.shared.enums import ReasonCode, Role
from nearpilot.shared.interfaces import MonitorSnapshot

log = logging.getLogger(__name__)

STREAM_PATH = "/dashboard/stream"
PROTOCOL_VERSION = 1

# 닫기 코드 (spec 6절). 대시보드가 코드로 다시 연결할지(1011, 1013) 사람에게 알릴지(4401, 4403)를 고른다
CLOSE_INVALID_TOKEN = 4401
CLOSE_NOT_OWNER = 4403
CLOSE_AUTH_TIMEOUT = 4408
CLOSE_SERVER_ERROR = 1011
CLOSE_OVERFLOW = 1013


def _token_of(text: object) -> str | None:
    """인증 메시지 `{"type": "auth", "token": "..."}` 의 토큰. 형식이 틀리면 None 이다."""
    if not isinstance(text, str):  # binary 프레임
        return None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # 깊게 중첩된 JSON 은 RecursionError 를 올린다
        return None
    if not isinstance(data, dict) or data.get("type") != "auth":
        return None
    token = data.get("token")
    if not isinstance(token, str) or not token:
        return None
    try:
        token.encode("utf-8")  # 짝 없는 서로게이트는 해시에서 서버 오류가 된다. 토큰 오류로 닫는다
    except UnicodeEncodeError:
        return None
    return token


async def _authenticate(websocket: WebSocket, authenticator: Authenticator, timeout_sec: float) -> int | None:
    """첫 메시지로 소유자를 인증한다. 통과하면 None 이고, 아니면 연결을 닫을 코드다.

    기다리는 동안 클라이언트가 끊으면 WebSocketDisconnect 를 올린다.
    """
    try:
        async with asyncio.timeout(timeout_sec):
            message = await websocket.receive()
    except TimeoutError:
        return CLOSE_AUTH_TIMEOUT
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    token = _token_of(message.get("text"))
    if token is None:
        return CLOSE_INVALID_TOKEN
    try:
        # 계정 조회는 동기 함수다. 이벤트 루프를 막지 않는다.
        account = await asyncio.to_thread(authenticator.authenticate, f"Bearer {token}")
    except AuthenticationError as error:
        return CLOSE_INVALID_TOKEN if error.reason is ReasonCode.INVALID_TOKEN else CLOSE_SERVER_ERROR
    return None if account.role == Role.OWNER else CLOSE_NOT_OWNER


async def _current_events(hub: MonitorHub, snapshot: MonitorSnapshot) -> list[dict[str, Any]]:
    """지금 상태 이벤트를 메시지로 바꾼다. 읽지 못하면 빈 목록이다 — 스트림은 이어 간다."""
    try:
        # 지금 상태 조회는 동기 함수다. 이벤트 루프를 막지 않는다.
        events = await asyncio.to_thread(snapshot.current_events)
        messages = (hub.render(event) for event in events)
        return [message for message in messages if message is not None]
    except Exception as error:  # noqa: BLE001 - 지금 상태를 못 읽어도 스트림은 이어 간다. 예외 종류만 남긴다
        log.error("지금 상태를 읽지 못했다: 예외=%s", type(error).__name__)
        return []


async def _send(websocket: WebSocket, events: list[dict[str, Any]]) -> None:
    await websocket.send_text(json.dumps({"v": PROTOCOL_VERSION, "events": events}, ensure_ascii=False))


async def _close(websocket: WebSocket, code: int) -> None:
    """닫기 코드를 보내고 닫는다. 이미 닫혔거나 끊긴 소켓의 오류는 삼킨다."""
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001 - 닫기가 실패해도 할 일이 없다
        pass


async def _send_all(websocket: WebSocket, sub: Subscription, first: list[dict[str, Any]]) -> None:
    """지금 상태를 보낸 뒤 허브의 묶음을 이어서 보낸다. 대기열이 넘쳤으면 1013 으로 닫고 끝낸다."""
    await _send(websocket, first)  # 비어도 보낸다. 묶음 크기로 나누지 않는다
    while (batch := await sub.next_batch()) is not None:
        await _send(websocket, batch)
    await _close(websocket, CLOSE_OVERFLOW)


async def _discard(websocket: WebSocket) -> None:
    """클라이언트가 보내는 메시지를 읽고 버린다. 끊김을 알아채려고 읽는다. 끊기면 끝난다."""
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


async def _stream(websocket: WebSocket, hub: MonitorHub, snapshot: MonitorSnapshot) -> None:
    """지금 상태와 이벤트 묶음을 보낸다. 클라이언트가 끊거나 대기열이 넘치면 끝난다. 끝나면 항상 구독을 뺀다."""
    sub = hub.subscribe()  # 지금 상태를 읽기 전에 등록해야 읽는 사이의 이벤트를 잃지 않는다 (spec 6절)
    try:
        first = await _current_events(hub, snapshot)
        sending = asyncio.create_task(_send_all(websocket, sub, first))
        receiving = asyncio.create_task(_discard(websocket))
        try:
            done, _ = await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # 보내거나 받다 난 예외를 올린다
        finally:  # 먼저 끝난 쪽이 있으면 나머지를 멈춘다
            sending.cancel()
            receiving.cancel()
            await asyncio.gather(sending, receiving, return_exceptions=True)
    finally:
        hub.unsubscribe(sub)


def add_stream_route(
    router: APIRouter,
    *,
    hub: MonitorHub,
    snapshot: MonitorSnapshot,
    authenticator: Authenticator,
    settings: DashboardSettings,
) -> None:
    """`router` 에 모니터링 WebSocket `STREAM_PATH` 를 붙인다. 소유자 토큰만 받는다."""

    @router.websocket(STREAM_PATH)
    async def stream(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            code = await _authenticate(websocket, authenticator, settings.auth_timeout_sec)
            if code is None:
                await _stream(websocket, hub, snapshot)
            else:
                await _close(websocket, code)
        except WebSocketDisconnect:  # 클라이언트가 먼저 끊었다
            pass
        except Exception as error:  # noqa: BLE001 - 알 수 없는 오류는 서버 오류로 닫는다. 예외 종류만 남긴다
            log.error("모니터링 스트림 처리 중 예외: 예외=%s", type(error).__name__)
            await _close(websocket, CLOSE_SERVER_ERROR)
