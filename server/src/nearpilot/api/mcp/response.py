"""FR-11 호스트 응답 변환.

허용 목록의 필드만 내보낸다. 사용자 ID, 점유 주체, 내부 요청 ID, 근접 확률은 내보내지 않는다 (NFR-08).
`Decision` 에 필드가 늘어도 여기에 쓰지 않으면 호스트에 나가지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
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
        "request_id": decision.request_id or None,  # 반납 판정에는 요청 ID가 없다
        "message": decision.message,
    }


def denied_response(reason: ReasonCode, request_id: str = "") -> dict[str, Any]:
    """판정까지 가지 못한 거부. 예외 내용 대신 일반 안내만 넣는다."""
    message = _GENERIC_MESSAGE if reason is ReasonCode.INTERNAL_ERROR else None
    return host_response(Decision(
        internal_request_id="", request_id=request_id, verdict=Verdict.DENY, reason=reason,
        message=message))


_STATUS_TIMES = ("rental_due_at", "closed_at")


def status_response(status: Mapping[str, Any]) -> dict[str, Any]:
    """사용 상태 조회 결과. 성공과 실패의 모양이 같다 (FR-21). 허용 목록 밖의 키는 버린다."""
    out: dict[str, Any] = {
        "use_id": status.get("use_id"),
        "state": status.get("state"),
        "rental_due_at": status.get("rental_due_at"),
        "overtime_sec": status.get("overtime_sec"),
        "closed_at": status.get("closed_at"),
        "reason": status.get("reason_code"),
    }
    for key in _STATUS_TIMES:
        if isinstance(out[key], datetime):
            out[key] = out[key].isoformat()
    return out


def status_denied(reason: ReasonCode) -> dict[str, Any]:
    return status_response({"reason_code": reason.value})


_DEVICE_KEYS = ("node_id", "capability", "occupancy", "access", "controls")
_STATE_KEYS = ("node_id", "capability", "occupancy", "state", "online", "trusted", "access_mode", "controls")


def devices_response(devices: Sequence[Mapping[str, Any]], reason: ReasonCode | None = None) -> dict[str, Any]:
    """후보 목록. 항목마다 허용 목록의 키만 내보낸다 (FR-11)."""
    return {
        "devices": [{key: device.get(key) for key in _DEVICE_KEYS} for device in devices],
        "reason": reason.value if reason else None,
    }


def device_state_response(state: Mapping[str, Any]) -> dict[str, Any]:
    """기기 상태. 성공과 실패의 모양이 같다 (FR-14). 허용 목록 밖의 키는 버린다."""
    out: dict[str, Any] = {key: state.get(key) for key in _STATE_KEYS}
    out["reason"] = state.get("reason_code")
    return out


def device_state_denied(reason: ReasonCode) -> dict[str, Any]:
    return device_state_response({"reason_code": reason.value})
