"""FR-11 호스트 응답 변환.

허용 목록의 필드만 내보낸다. 사용자 ID, 점유 주체, 내부 요청 ID, 근접 확률은 내보내지 않는다 (NFR-08).
`Decision` 에 필드가 늘어도 여기에 쓰지 않으면 호스트에 나가지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from nearpilot.api.mcp.messages import build_message
from nearpilot.shared.enums import ReasonCode, Verdict
from nearpilot.shared.models import Decision, DeviceOutcome

_GENERIC_MESSAGE = "요청을 처리하지 못했습니다."


def _device_entry(device: DeviceOutcome) -> dict[str, Any]:
    """`devices` 의 기기 하나. 네 키를 담고, 묶음 요청의 기기(`item` 이 None 이 아님)에는 `item` 을 더한다 (FR-09)."""
    entry: dict[str, Any] = {
        "node_id": device.node_id, "verdict": device.verdict.value, "reason": device.reason.value,
        "use_id": device.use_id}
    if device.item is not None:
        entry["item"] = device.item
    return entry


def host_response(
    decision: Decision, *, action: str | None = None, item_actions: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """기존 여섯 키. 거리 선택자·묶음 판정(`selected_count` 가 None 이 아님)에는 `devices`, `selected_count`, `similar` 를 더한다.

    `devices` 는 판정이 정한 순서 그대로이고 기기마다 네 키를 담는다. 묶음 요청의 기기에는 1부터 센 항목 순서 `item` 을 더한다.
    하위 요청의 내부 ID 는 `DeviceOutcome` 에 없다 (FR-09, FR-11).

    `message` 는 core가 채운 값을 그대로 쓴다. 비어 있으면 `ReasonCode`로 안내 문장을 만든다. `action`과
    `item_actions`(묶음 요청의 1부터 센 항목 순서 → 동작 이름)는 그 문장이 동작 이름을 쓸 때만 참조한다 (FR-09).
    """
    message = decision.message
    if message is None:
        message = build_message(decision, action=action, item_actions=item_actions)
    out: dict[str, Any] = {
        "verdict": decision.verdict.value,
        "reason": decision.reason.value,
        "node_id": decision.node_id,
        "use_id": decision.use_id,
        "request_id": decision.request_id or None,  # 반납 판정에는 요청 ID가 없다
        "message": message,
    }
    if decision.selected_count is not None:
        out["devices"] = [_device_entry(device) for device in decision.devices]
        out["selected_count"] = decision.selected_count
        out["similar"] = decision.similar
    return out


def denied_response(
    reason: ReasonCode, request_id: str = "", *, selector: bool = False, action: str | None = None,
) -> dict[str, Any]:
    """판정까지 가지 못한 거부. 예외 내용 대신 일반 안내만 넣는다.

    `selector` 이면(형식이 맞는 거리 선택자이거나 묶음 형식) 고르기 전에 끝난 판정과 같은 `devices`, `selected_count`, `similar` 를 더한다 (FR-09).
    """
    message = _GENERIC_MESSAGE if reason is ReasonCode.INTERNAL_ERROR else None
    picked: dict[str, Any] = {"devices": (), "selected_count": 0, "similar": False} if selector else {}
    return host_response(Decision(
        internal_request_id="", request_id=request_id, verdict=Verdict.DENY, reason=reason,
        message=message, **picked), action=action)


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


# 동작 목록은 이름, 설명, 인자 스키마만 담는다. 제작사 정보와 선언 해시는 넣지 않는다 (FR-09, FR-11).
_DEVICE_KEYS = ("node_id", "kind", "occupancy", "access", "actions")
_STATE_KEYS = ("node_id", "kind", "occupancy", "state", "online", "trusted", "access_mode", "actions",
               "reported")
_ACTION_KEYS = ("name", "description", "input_schema")
_REPORTED_KEYS = ("value", "observed_at")  # 보고 상태는 값과 관측 시각만 담는다 (FR-09, FR-11, #289)


def _filtered(item: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """허용 목록의 키만 남긴다. 동작 목록도 항목마다 같은 방식으로 거른다 (FR-11)."""
    out = {key: item.get(key) for key in keys}
    if "actions" in out and out["actions"] is not None:
        out["actions"] = [{key: action.get(key) for key in _ACTION_KEYS} for action in out["actions"]]
    if isinstance(out.get("reported"), Mapping):
        out["reported"] = {str(name): {key: entry.get(key) for key in _REPORTED_KEYS}
                           for name, entry in out["reported"].items() if isinstance(entry, Mapping)}
    elif "reported" in out:
        out["reported"] = None
    return out


def devices_response(devices: Sequence[Mapping[str, Any]], reason: ReasonCode | None = None) -> dict[str, Any]:
    """후보 목록. 항목마다 허용 목록의 키만 내보낸다 (FR-11)."""
    return {
        "devices": [_filtered(device, _DEVICE_KEYS) for device in devices],
        "reason": reason.value if reason else None,
    }


def device_state_response(state: Mapping[str, Any]) -> dict[str, Any]:
    """기기 상태. 성공과 실패의 모양이 같다 (FR-14). 허용 목록 밖의 키는 버린다."""
    out: dict[str, Any] = _filtered(state, _STATE_KEYS)
    out["reason"] = state.get("reason_code")
    return out


def device_state_denied(reason: ReasonCode) -> dict[str, Any]:
    return device_state_response({"reason_code": reason.value})
