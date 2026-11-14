"""FR-09, FR-11, FR-14, FR-21 호스트에 보여줄 안내 문장.

core가 이미 채운 `message`는 건드리지 않는다. core가 비워 둔 경우에만 `ReasonCode`로 문장을 만든다.
말투는 합쇼체로 통일한다. 사용자 ID, 점유 주체, 내부 요청 ID, 근접 확률은 문장에 넣지 않는다 (NFR-08).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from nearpilot.shared.enums import ReasonCode, Verdict
from nearpilot.shared.models import Decision, DeviceOutcome

# ── 기기 이름 (node_id → "{번호}번 {한국어 이름}") ───────────────────────

_BATCHIM = {"obj": "을", "topic": "은", "with": "과"}
_NO_BATCHIM = {"obj": "를", "topic": "는", "with": "와"}

# 저장소에서 실제로 쓰는 kind 전부. 모르는 kind는 node_id를 그대로 쓴다 (예: 서버가 모르는 새 kind, 테스트용 kind).
_KIND_NAMES: dict[str, tuple[str, dict[str, str]]] = {
    "light": ("조명", _BATCHIM),
    "locker": ("사물함", _BATCHIM),
    "speaker": ("스피커", _NO_BATCHIM),
    "display": ("디스플레이", _NO_BATCHIM),
    "fan": ("선풍기", _NO_BATCHIM),
}
_GENERIC_DEVICE = ("기기", _NO_BATCHIM)
_NODE_ID_RE = re.compile(r"^(?P<kind>.+)-(?P<num>\d+)$")


def _kind_info(node_id: str) -> tuple[str, dict[str, str]] | None:
    match = _NODE_ID_RE.match(node_id)
    return _KIND_NAMES.get(match["kind"]) if match else None


def _number(node_id: str) -> str | None:
    match = _NODE_ID_RE.match(node_id)
    return match["num"] if match else None


def device_name(node_id: str | None) -> tuple[str, dict[str, str]]:
    """표시 이름과 조사 집합. 번호가 없거나 모르는 kind면 node_id를 그대로 쓰고, node_id가 없으면 "기기"다.

    node_id를 그대로 쓸 때의 조사는 받침 여부를 알 수 없어 받침 없는 쪽으로 둔다 (가정).
    """
    if not node_id:
        return _GENERIC_DEVICE
    info = _kind_info(node_id)
    if info is None:
        return node_id, _NO_BATCHIM
    name, particles = info
    return f"{_number(node_id)}번 {name}", particles


# ── 동작 동사 (성공/실패) ────────────────────────────────────────────

# 기기 자체가 동작의 목적어다 (device를/을 켰습니다).
_OBJECT_ACTIONS: dict[str, tuple[str, str]] = {
    "on": ("켰습니다", "켜지 못했습니다"),
    "off": ("껐습니다", "끄지 못했습니다"),
    "open": ("열었습니다", "열지 못했습니다"),
    "close": ("닫았습니다", "닫지 못했습니다"),
}
# 동작이 기기 자신이 아닌 값을 목적어로 가진다 (device에서 음량을 조절했습니다).
_LOCATIVE_ACTIONS: dict[str, tuple[str, str]] = {
    "set_volume": ("음량을 조절했습니다", "음량을 조절하지 못했습니다"),
    "set_mute": ("음소거를 설정했습니다", "음소거를 설정하지 못했습니다"),
    "set_picture_mode": ("화면 모드를 바꿨습니다", "화면 모드를 바꾸지 못했습니다"),
    "set_sound_mode": ("소리 모드를 바꿨습니다", "소리 모드를 바꾸지 못했습니다"),
    "set_channel": ("채널을 바꿨습니다", "채널을 바꾸지 못했습니다"),
    "launch_app": ("앱을 실행했습니다", "앱을 실행하지 못했습니다"),
}
_GENERIC_ACTION = ("요청한 동작을 실행했습니다", "요청한 동작을 실행하지 못했습니다")


def _predicate_sentence(subject: str, particles: Mapping[str, str], action: str | None, *, success: bool) -> str:
    """"{주어}{을/를} {동사}." 또는 "{주어}에서 {동사}."."""
    index = 0 if success else 1
    if action in _OBJECT_ACTIONS:
        return f"{subject}{particles['obj']} {_OBJECT_ACTIONS[action][index]}"
    verb = _LOCATIVE_ACTIONS.get(action, _GENERIC_ACTION)[index]
    return f"{subject}에서 {verb}"


def _execute_sentence(node_id: str | None, action: str | None, *, success: bool) -> str:
    name, particles = device_name(node_id)
    return _predicate_sentence(name, particles, action, success=success)


# ── 단일 판정(장치 묶음이 없는 결정)의 reason별 문장 ──────────────────

def _target_unavailable(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['topic']} 지금 사용할 수 없습니다. 신뢰되지 않거나 연결이 끊겼거나 점검 중인 기기입니다."


def _move_closer(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['with']}의 거리가 멀어 실행하지 않았습니다. {name} 가까이 오셔서 다시 요청해 주십시오."


def _unsupported_action(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['topic']} 요청한 동작을 지원하지 않습니다. 지원하는 동작으로 다시 요청해 주십시오."


def _unsupported_parameter(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['topic']} 요청한 인자를 지원하지 않아 실행하지 않았습니다. 지원하는 값으로 다시 요청해 주십시오."


def _parameter_out_of_range(name: str, particles: Mapping[str, str]) -> str:
    return f"요청한 값이 {name}의 설정 범위를 벗어나 실행하지 않았습니다. 범위 안의 값으로 다시 요청해 주십시오."


def _approval_required(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['topic']} 소유자 승인이 필요합니다. 승인 요청을 보내시겠습니까?"


def _policy_denied(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['topic']} 정책상 사용할 수 없습니다."


def _device_busy(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['topic']} 다른 사용자가 사용 중입니다. 잠시 후 다시 시도해 주십시오."


def _exec_unknown(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}의 실행 결과가 확인되지 않았습니다. 잠시 후 상태를 다시 확인해 주십시오."


def _node_not_connected(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}{particles['topic']} 지금 연결되어 있지 않아 실행하지 않았습니다. 기기의 연결 상태를 확인해 주십시오."


def _command_not_sent(name: str, particles: Mapping[str, str]) -> str:
    return f"{name}에 명령을 보내지 못해 실행하지 않았습니다. 잠시 후 다시 시도해 주십시오."


def _approval_pending(name: str, particles: Mapping[str, str]) -> str:
    return "소유자의 승인을 기다리고 있습니다. 승인되면 다시 요청해 주십시오."


# 기기와 무관한 문장. 인자는 무시한다.
def _const(text: str):
    def _(name: str, particles: Mapping[str, str]) -> str:
        return text
    return _


_GENERIC_MESSAGE = "요청을 처리하지 못했습니다."

_REASON_TEMPLATES = {
    ReasonCode.INVALID_TOKEN: _const("인증 정보가 올바르지 않아 요청을 처리하지 못했습니다. 관리자에게 문의해 주십시오."),
    ReasonCode.INVALID_INPUT: _const("요청 형식이 올바르지 않아 실행하지 않았습니다. 다시 요청해 주십시오."),
    ReasonCode.REQUEST_ID_CONFLICT: _const(
        "같은 요청이 다른 내용으로 이미 접수되어 처리하지 못했습니다. 다시 요청해 주십시오."),
    ReasonCode.REQUEST_IN_PROGRESS: _const("같은 요청을 아직 처리하고 있습니다. 잠시 후 다시 시도해 주십시오."),
    ReasonCode.CAPACITY_EXCEEDED: _const("요청이 많아 지금은 새 요청을 받을 수 없습니다. 잠시 후 다시 시도해 주십시오."),
    ReasonCode.BEACON_NOT_SEEN: _const("주변 기기에서 휴대폰 신호가 확인되지 않습니다. 앱이 켜져 있는지 확인해 주십시오."),
    ReasonCode.NO_CANDIDATE: _const("근처에서 사용할 수 있는 기기를 찾지 못했습니다. 기기 가까이 다가가 다시 요청해 주십시오."),
    ReasonCode.TARGET_UNAVAILABLE: _target_unavailable,
    ReasonCode.LOW_CONFIDENCE: _const(
        "가까운 기기가 여러 개라 어느 쪽인지 확실하지 않습니다. 조금 더 다가가시거나 어느 기기인지 말씀해 주십시오."),
    ReasonCode.ORDER_UNCERTAIN: _const(
        "경계에 있는 기기들의 순서가 애매해 실행하지 않았습니다. 어느 기기인지 더 구체적으로 말씀해 주십시오."),
    ReasonCode.MOVE_CLOSER: _move_closer,
    ReasonCode.UNSUPPORTED_ACTION: _unsupported_action,
    ReasonCode.UNSUPPORTED_PARAMETER: _unsupported_parameter,
    ReasonCode.PARAMETER_OUT_OF_RANGE: _parameter_out_of_range,
    ReasonCode.APPROVAL_REQUIRED: _approval_required,
    ReasonCode.POLICY_DENIED: _policy_denied,
    ReasonCode.APPROVAL_PENDING: _approval_pending,
    ReasonCode.APPROVAL_REJECTED: _const("소유자가 승인을 거부했습니다. 다른 방법으로 요청해 주십시오."),
    ReasonCode.APPROVAL_EXPIRED: _const("승인 유효 시간이 지나 실행하지 않았습니다. 승인을 다시 요청해 주십시오."),
    ReasonCode.APPROVAL_REQUEST_EXPIRED: _const("승인 요청이 만료되었습니다. 승인을 다시 요청해 주십시오."),
    ReasonCode.APPROVAL_EXHAUSTED: _const("승인된 사용 횟수를 모두 사용했습니다. 승인을 다시 요청해 주십시오."),
    ReasonCode.ACTION_NOT_ALLOWED: _const(
        "승인받은 동작과 달라 실행하지 않았습니다. 승인받은 동작으로 다시 요청해 주십시오."),
    ReasonCode.DEVICE_BUSY: _device_busy,
    ReasonCode.USER_BUSY: _const("이미 사용 중인 다른 기기가 있어 실행하지 않았습니다. 사용을 마친 뒤 다시 시도해 주십시오."),
    ReasonCode.NOT_ASSIGNED: _const("지금 배정할 수 있는 기기가 없습니다. 잠시 후 다시 요청해 주십시오."),
    ReasonCode.USE_NOT_FOUND: _const("해당 사용 내역을 찾을 수 없습니다."),
    ReasonCode.RECHECK_FAILED: _const("실행 직전 확인에서 문제가 있어 실행하지 않았습니다. 다시 요청해 주십시오."),
    ReasonCode.INTERNAL_ERROR: _const(_GENERIC_MESSAGE),
    ReasonCode.COMMAND_IN_PROGRESS: _const("다른 명령이 진행 중이라 지금은 실행할 수 없습니다. 잠시 후 다시 시도해 주십시오."),
    ReasonCode.EXEC_UNKNOWN: _exec_unknown,
    ReasonCode.RESERVATION_EXPIRED: _const("대기 시간이 지나 실행하지 않았습니다. 다시 요청해 주십시오."),
    ReasonCode.NODE_NOT_CONNECTED: _node_not_connected,
    ReasonCode.COMMAND_NOT_SENT: _command_not_sent,
}


def _single_message(reason: ReasonCode, node_id: str | None, action: str | None) -> str:
    if reason is ReasonCode.OK:
        return _execute_sentence(node_id, action, success=True) + "."
    if reason is ReasonCode.EXEC_FAILED:
        return (f"{_execute_sentence(node_id, action, success=False)}. "
                "기기에서 동작이 확인되지 않았습니다. 전원이나 연결 상태를 확인해 주십시오.")
    name, particles = device_name(node_id)
    return _REASON_TEMPLATES[reason](name, particles)


# ── 거리 선택자·묶음 요청(devices가 있는 판정)의 묶음 문장 ─────────────

# 실행 전에 걸린 원인. 비어 있으면 (EXEC_FAILED 등) 원인 없이 동작 실패만 말한다.
_CAUSE_CLAUSES: dict[ReasonCode, str] = {
    ReasonCode.DEVICE_BUSY: "다른 사용자가 사용 중이라",
    ReasonCode.USER_BUSY: "이미 사용 중인 다른 기기가 있어",
    ReasonCode.MOVE_CLOSER: "거리가 멀어",
    ReasonCode.APPROVAL_REQUIRED: "소유자 승인이 필요해",
    ReasonCode.POLICY_DENIED: "정책상 사용할 수 없어",
    ReasonCode.TARGET_UNAVAILABLE: "사용할 수 없어",
    ReasonCode.UNSUPPORTED_ACTION: "지원하지 않는 동작이라",
    ReasonCode.UNSUPPORTED_PARAMETER: "지원하지 않는 값이라",
    ReasonCode.PARAMETER_OUT_OF_RANGE: "값이 설정 범위를 벗어나",
    ReasonCode.COMMAND_IN_PROGRESS: "다른 명령이 진행 중이라",
    ReasonCode.NODE_NOT_CONNECTED: "연결되어 있지 않아",
    ReasonCode.COMMAND_NOT_SENT: "명령을 보내지 못해",
    ReasonCode.RECHECK_FAILED: "실행 직전 확인에서 문제가 있어",
    ReasonCode.RESERVATION_EXPIRED: "대기 시간이 지나",
    ReasonCode.EXEC_UNKNOWN: "실행 결과가 확인되지 않아",
}
_DEFAULT_CAUSE = "문제가 있어"
_SIMILAR_NOTE = "비슷한 거리의 기기가 있어 선택 결과가 다를 수 있습니다."


def _action_for(device: DeviceOutcome, action: str | None, item_actions: Mapping[int, str] | None) -> str | None:
    if device.item is not None and item_actions:
        return item_actions.get(device.item, action)
    return action


def _group_key(device: DeviceOutcome, action: str | None) -> tuple[str | None, str]:
    info = _kind_info(device.node_id)
    return (action, info[0] if info is not None else device.node_id)


def _grouped_ok_sentence(node_ids: Sequence[str], action: str | None) -> str:
    info = _kind_info(node_ids[0])
    if info is not None:
        name, particles = info
        numbers = ", ".join(f"{_number(n)}번" for n in node_ids)
        subject = f"{numbers} {name}"
    else:
        subject, particles = device_name(node_ids[0])
    return _predicate_sentence(subject, particles, action, success=True) + "."


def _failure_verb(action: str | None) -> str:
    if action in _OBJECT_ACTIONS:
        return _OBJECT_ACTIONS[action][1]
    return _LOCATIVE_ACTIONS.get(action, _GENERIC_ACTION)[1]


def _device_failure_sentence(device: DeviceOutcome, action: str | None) -> str:
    """"{기기}{은/는} {원인} {실패 동사}." 원인이 없으면(EXEC_FAILED) "{기기}{은/는} {실패 동사}."."""
    name, particles = device_name(device.node_id)
    cause = _CAUSE_CLAUSES.get(device.reason, "" if device.reason is ReasonCode.EXEC_FAILED else _DEFAULT_CAUSE)
    verb = _failure_verb(action)
    if not cause:
        return f"{name}{particles['topic']} {verb}."
    return f"{name}{particles['topic']} {cause} {verb}."


def _aggregate_message(decision: Decision, action: str | None, item_actions: Mapping[int, str] | None) -> str:
    ok_order: list[tuple[str | None, str]] = []
    ok_groups: dict[tuple[str | None, str], list[str]] = {}
    failure_sentences: list[str] = []
    for device in decision.devices:
        act = _action_for(device, action, item_actions)
        if device.verdict is Verdict.OK:
            key = _group_key(device, act)
            if key not in ok_groups:
                ok_groups[key] = []
                ok_order.append(key)
            ok_groups[key].append(device.node_id)
        else:
            failure_sentences.append(_device_failure_sentence(device, act))
    ok_sentences = [_grouped_ok_sentence(ok_groups[key], key[0]) for key in ok_order]
    sentences = ok_sentences + failure_sentences
    if decision.similar:
        sentences.append(_SIMILAR_NOTE)
    return " ".join(sentences)


def build_message(
    decision: Decision, *, action: str | None = None, item_actions: Mapping[int, str] | None = None,
) -> str:
    """`decision.message`가 비어 있을 때만 부른다. `devices`가 있으면 기기별 결과를 묶은 문장이다."""
    if decision.devices:
        return _aggregate_message(decision, action, item_actions)
    return _single_message(decision.reason, decision.node_id, action)
