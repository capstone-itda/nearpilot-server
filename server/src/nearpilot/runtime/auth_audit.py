"""FR-22 인증 실패의 감사 기록 (specs/core-server-assembly/spec.md 6절 "인증 실패 감사 기록").

api 는 db 를 직접 부르지 않으므로(AGENTS.md 의존 규칙 1) 조립이 `Authenticator` 를 감싸서 남긴다.
MCP, 계정·승인 REST, 대시보드가 모두 `Runtime.authenticator` 를 쓰므로 같은 기록을 남긴다.
인증 실패를 원래 요청과 연결하지 않는다. 사건에 요청 참조를 두지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.shared.enums import ReasonCode, Step
from nearpilot.shared.interfaces import AccountLookup, AuditLog
from nearpilot.shared.models import Account, AuditEvent

log = logging.getLogger(__name__)


class AuditedAuthenticator(Authenticator):
    """인증이 실패하면 `auth` 감사 사건을 남기고 같은 예외를 다시 낸다. 성공은 남기지 않는다.

    payload 는 사유, 자격 증명 유무와 시각뿐이다. 토큰 원문·해시·헤더 값과 계정 정보를 넣지 않는다 (NFR-08).
    기록을 만들거나 저장하다 실패해도 경고만 남긴다. 기록이 인증 결과를 바꾸지 않는다.
    """

    def __init__(self, accounts: AccountLookup, audit: AuditLog, clock: Callable[[], datetime]) -> None:
        super().__init__(accounts)
        self._audit = audit
        self._clock = clock

    def authenticate(self, authorization: str | None) -> Account:
        try:
            return super().authenticate(authorization)
        except AuthenticationError as error:
            self._record(error.reason, authorization)
            raise

    def _record(self, reason: ReasonCode, authorization: str | None) -> None:
        try:
            self._audit.append(AuditEvent(
                event_id=str(uuid4()), step=Step.AUTH,
                payload={"reason": reason.value, "credential": "missing" if not authorization else "presented",
                         "at": self._clock().isoformat()}))
        except Exception:  # noqa: BLE001 - 기록 실패가 인증 결과를 바꾸지 않는다
            log.warning("인증 실패 감사 저장 실패", exc_info=True)
