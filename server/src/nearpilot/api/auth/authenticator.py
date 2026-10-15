"""FR-08 토큰으로 계정 식별.

호스트가 보낸 `Authorization: Bearer <토큰>` 헤더에서 계정을 찾는다. 프레임워크에 의존하지 않는다.
토큰 원문은 로그, 오류, 반환 값에 쓰지 않는다 (NFR-08). 계정 조회는 api 가 db 를 부르는 유일한 경로다.
"""

from __future__ import annotations

import logging

from nearpilot.shared.enums import ReasonCode
from nearpilot.shared.interfaces import AccountLookup
from nearpilot.shared.models import Account
from nearpilot.shared.tokens import hash_token

log = logging.getLogger(__name__)

_SCHEME = "bearer"


class AuthenticationError(Exception):
    """인증을 거부했다. `reason` 만 갖는다. 토큰 원문과 계정 정보를 담지 않는다."""

    def __init__(self, reason: ReasonCode):
        super().__init__(reason.value)
        self.reason = reason


class Authenticator:
    def __init__(self, accounts: AccountLookup) -> None:
        self._accounts = accounts

    def authenticate(self, authorization: str | None) -> Account:
        """`Bearer <토큰>` 이면 계정을 반환한다. 아니면 `AuthenticationError` 를 던진다."""
        token = self._token_of(authorization)
        if token is None:
            raise AuthenticationError(ReasonCode.INVALID_TOKEN)
        try:
            account = self._accounts.account_by_token_hash(hash_token(token))
        except Exception:  # noqa: BLE001 - 조회 실패는 토큰 오류가 아니다. 기본값은 거부다
            log.exception("계정 조회 실패")
            raise AuthenticationError(ReasonCode.INTERNAL_ERROR) from None
        if account is None:
            raise AuthenticationError(ReasonCode.INVALID_TOKEN)
        return account

    @staticmethod
    def _token_of(authorization: str | None) -> str | None:
        """형식이 맞으면 토큰을, 아니면 None 을 반환한다. 토큰을 정규화하지 않는다."""
        if not authorization:
            return None
        scheme, sep, token = authorization.partition(" ")
        if not sep or scheme.lower() != _SCHEME:
            return None
        if not token or any(c.isspace() for c in token):
            return None
        return token
