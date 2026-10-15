"""FR-11 호스트 응답 변환.

허용 목록의 필드만 내보낸다. 사용자 ID, 점유 주체, 내부 요청 ID, 근접 확률은 내보내지 않는다 (NFR-08).
`Decision` 에 필드가 늘어도 여기에 쓰지 않으면 호스트에 나가지 않는다.
"""

from __future__ import annotations

from typing import Any

from nearpilot.shared.enums import ReasonCode, Verdict
from nearpilot.shared.models import Decision

_GENERIC_MESSAGE = "요청을 처리하지 못했습니다."


def host_response(decision: Decision) -> dict[str, Any]:
    return {
        "verdict": decision.verdict.value,
        "reason": decision.reason.value,
        "node_id": decision.node_id,
        "use_id": decision.use_id,
        "request_id": decision.request_id,
        "message": decision.message,
    }


def denied_response(reason: ReasonCode, request_id: str = "") -> dict[str, Any]:
    """판정까지 가지 못한 거부. 예외 내용 대신 일반 안내만 넣는다."""
    message = _GENERIC_MESSAGE if reason is ReasonCode.INTERNAL_ERROR else None
    return host_response(Decision(
        internal_request_id="", request_id=request_id, verdict=Verdict.DENY, reason=reason,
        message=message))
