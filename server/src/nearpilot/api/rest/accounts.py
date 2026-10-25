"""FR-01 앱용 계정 REST (architecture.md §5 REST "계정", #183).

`POST /accounts`는 `visitor` 계정과 토큰을 만들고, `PUT /me/beacon`은 토큰 계정의 활성 비콘을 등록한다.
쓰기는 `AdminService`로만 한다. 응답에 계정 ID와 비콘 ID를 넣지 않는다 (FR-11, NFR-08).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.shared.enums import AdminWriteOutcome, ReasonCode, Role
from nearpilot.shared.interfaces import AdminService
from nearpilot.shared.models import BeaconKey, BeaconRegistration

log = logging.getLogger(__name__)

INVALID_BEACON = "INVALID_BEACON"  # §5 REST가 정한 코드. shared 의 ReasonCode 에는 없다
NAME_MAX_LENGTH = 64

_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ID_RANGE = range(0, 65536)


def new_user_id() -> str:
    return f"user-{uuid.uuid4().hex}"


def beacon_id_for(user_id: str, beacon: BeaconKey) -> str:
    """같은 계정·같은 비콘이면 같은 ID다. 재등록을 DB가 거부하지 않고 다시 활성화한다."""
    digest = hashlib.sha256(f"{user_id}|{beacon.uuid}|{beacon.major}|{beacon.minor}".encode()).hexdigest()
    return f"beacon-{digest[:24]}"


def _error(status: int, reason: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status, headers=headers)


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _parse_name(body: dict[str, Any]) -> str | None:
    name = body.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name if 0 < len(name) <= NAME_MAX_LENGTH else None


def _parse_beacon(body: dict[str, Any]) -> BeaconKey | None:
    raw_uuid, major, minor = body.get("uuid"), body.get("major"), body.get("minor")
    if not isinstance(raw_uuid, str):
        return None
    normalized = raw_uuid.strip().lower()
    if not _UUID_PATTERN.match(normalized):
        return None
    # bool 은 int 의 하위 클래스라서 따로 거른다.
    for value in (major, minor):
        if isinstance(value, bool) or not isinstance(value, int) or value not in _ID_RANGE:
            return None
    return BeaconKey(normalized, major, minor)


def build_account_router(
    admin: AdminService, authenticator: Authenticator, *, new_id: Callable[[], str] = new_user_id
) -> APIRouter:
    router = APIRouter()

    @router.post("/accounts")
    async def create_account(request: Request) -> JSONResponse:
        body = await _json_object(request)
        name = _parse_name(body) if body is not None else None
        if name is None:
            return _error(400, ReasonCode.INVALID_INPUT.value)
        try:
            issue = await asyncio.to_thread(admin.register_account, new_id(), name, Role.VISITOR)
        except Exception:  # noqa: BLE001 - 예외 내용은 응답에 넣지 않는다
            log.exception("계정 등록 중 예외")
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        if issue.result.outcome != AdminWriteOutcome.APPLIED or issue.token is None:
            return _error(409, issue.result.reason.value)
        return JSONResponse({"token": issue.token}, status_code=201)

    @router.put("/me/beacon")
    async def put_beacon(request: Request) -> JSONResponse:
        try:
            account = await asyncio.to_thread(
                authenticator.authenticate, request.headers.get("authorization"))
        except AuthenticationError as error:
            if error.reason is ReasonCode.INVALID_TOKEN:
                return _error(401, ReasonCode.INVALID_TOKEN.value, {"WWW-Authenticate": "Bearer"})
            return _error(500, ReasonCode.INTERNAL_ERROR.value)

        body = await _json_object(request)
        beacon = _parse_beacon(body) if body is not None else None
        if beacon is None:
            return _error(400, INVALID_BEACON)

        registration = BeaconRegistration(
            beacon_id=beacon_id_for(account.user_id, beacon), user_id=account.user_id, beacon=beacon,
            is_active=True)
        try:
            result = await asyncio.to_thread(admin.register_beacon, registration)
        except Exception:  # noqa: BLE001
            log.exception("비콘 등록 중 예외")
            return _error(500, ReasonCode.INTERNAL_ERROR.value)
        if result.outcome != AdminWriteOutcome.APPLIED:
            return _error(409, result.reason.value)
        return JSONResponse(
            {"uuid": beacon.uuid, "major": beacon.major, "minor": beacon.minor, "is_active": True})

    return router
