"""FR-11·FR-12A·FR-12B 대시보드 문구와 코드 (specs/core-dashboard-queries/spec.md 6절).

감사 행, 요청 행, 명령 결과를 짧은 한국어 문구와 사유 코드로 바꾼다. 스트림과 조회 REST 가 이 파일의 함수를
함께 써서 같은 문구를 보인다. 조회 서비스(`queries`)와 평가 리포트(`evaluation`)가 쓰는 고정 문구도 여기에 둔다.
문구에 쓰는 것은 코드, 열거형 값, 수, 기기 이름뿐이다.
감사 payload 의 문자열(`user_id`, 계정 이름, `error`)은 옮기지 않는다 (FR-11, NFR-08).
DB 를 읽지 않고 현재 시각을 읽지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from nearpilot.shared.enums import (
    Action,
    CommandOutcome,
    DeviceState,
    NodeResultState,
    ReasonCode,
    ReserveOutcome,
    SessionState,
    Step,
    Verdict,
)
from nearpilot.shared.models import DashboardAudit, DashboardCommand, DashboardRequest

# 판정 사유마다 한 줄. 모든 ReasonCode 에 있어야 한다.
_REASON_TEXT: dict[ReasonCode, str] = {
    ReasonCode.OK: "통과",
    ReasonCode.INVALID_TOKEN: "토큰 확인 실패",
    ReasonCode.INVALID_INPUT: "요청 형식이 맞지 않음",
    ReasonCode.REQUEST_ID_CONFLICT: "같은 요청 번호에 다른 내용",
    ReasonCode.CAPACITY_EXCEEDED: "서버가 바쁨",
    ReasonCode.BEACON_NOT_SEEN: "비콘 수신 없음",
    ReasonCode.NO_CANDIDATE: "쓸 수 있는 기기 없음",
    ReasonCode.TARGET_UNAVAILABLE: "지금 쓸 수 없는 기기",
    ReasonCode.LOW_CONFIDENCE: "어느 기기 앞인지 확신 부족",
    ReasonCode.MOVE_CLOSER: "기기에 더 가까이 가야 함",
    ReasonCode.APPROVAL_REQUIRED: "승인 필요",
    ReasonCode.POLICY_DENIED: "정책이 허락하지 않음",
    ReasonCode.APPROVAL_PENDING: "소유자 응답 기다림",
    ReasonCode.APPROVAL_REJECTED: "소유자 거부",
    ReasonCode.APPROVAL_EXPIRED: "승인 유효시간 끝남",
    ReasonCode.APPROVAL_REQUEST_EXPIRED: "승인 신청 만료",
    ReasonCode.APPROVAL_EXHAUSTED: "사용 횟수를 다 씀",
    ReasonCode.ACTION_NOT_ALLOWED: "승인과 다른 동작",
    ReasonCode.DEVICE_BUSY: "다른 사람이 점유 중",
    ReasonCode.USER_BUSY: "이미 다른 기기를 쓰는 중",
    ReasonCode.NOT_ASSIGNED: "배정받지 못함",
    ReasonCode.USE_NOT_FOUND: "찾을 수 없는 사용 건",
    ReasonCode.RECHECK_FAILED: "실행 직전 재검사 실패",
    ReasonCode.INTERNAL_ERROR: "서버 오류",
    ReasonCode.COMMAND_IN_PROGRESS: "다른 명령을 실행하는 중",
    ReasonCode.EXEC_FAILED: "기기가 실패를 알림",
    ReasonCode.EXEC_UNKNOWN: "결과 불명으로 격리",
    ReasonCode.RESERVATION_EXPIRED: "예약 대기 시간 끝남",
    ReasonCode.NODE_NOT_CONNECTED: "기기 연결 없음",
    ReasonCode.COMMAND_NOT_SENT: "명령을 보내지 못함",
}

# 관리 기록은 event 이름만으로 문구를 고른다. payload 의 ID 와 이름은 쓰지 않는다.
_ADMIN_TEXT: dict[str, str] = {
    "register_account": "계정 등록",
    "register_beacon": "비콘 등록",
    "set_node_trust": "기기 신뢰 변경",
    "assign_place": "장소 지정",
    "set_policy": "접근 정책 변경",
    "set_release_policy": "반납 정책 변경",
    "set_node_name": "기기 이름 변경",
    "start_collection": "수집 시작",
    "end_collection": "수집 종료",
}

# 실행 결과 기록 (event == record_result). 어떤 동작이었는지는 payload 에 없다.
_RESULT_TEXT: dict[str, str] = {
    CommandOutcome.SUCCEEDED.value: "실행 완료",
    CommandOutcome.FAILED.value: "실행 실패 → 고장 격리",
    CommandOutcome.REJECTED.value: "기기가 실행을 거절",
    CommandOutcome.UNKNOWN.value: "결과 불명 → 고장 격리",
}
_EVENT_TEXT: dict[str, str] = {"finish_close": "반납 완료", "settle_recovery": "고장 복구 정산"}
_ISOLATE_TEXT: dict[ReasonCode, str] = {
    ReasonCode.EXEC_UNKNOWN: "결과 불명으로 격리",
    ReasonCode.EXEC_FAILED: "실행 실패로 격리",
}
# 문구를 정할 수 없는 행의 대신 문구
_STEP_NOTE: dict[Step, str] = {
    Step.AUTH: "인증 기록",
    Step.EXEC: "실행 기록",
    Step.CLOSE: "반납 기록",
    Step.FAULT: "고장 기록",
}

_DONE_TEXT: dict[Action, str] = {Action.OPEN: "열림", Action.ON: "켜짐", Action.OFF: "꺼짐"}
_CODE_OF_RESULT: dict[str, ReasonCode] = {  # 실행 결과 → 감사 행 code (spec 6절)
    CommandOutcome.SUCCEEDED.value: ReasonCode.OK,
    CommandOutcome.FAILED.value: ReasonCode.EXEC_FAILED,
    CommandOutcome.REJECTED.value: ReasonCode.EXEC_FAILED,
    CommandOutcome.UNKNOWN.value: ReasonCode.EXEC_UNKNOWN,
}
_EXEC_RESULT: dict[str, str] = {  # CommandOutcome → exec_result 이벤트의 result. 거절은 격리하지 않아 실패와 따로 보인다
    CommandOutcome.SUCCEEDED.value: "succeeded",
    CommandOutcome.FAILED.value: "failed",
    CommandOutcome.REJECTED.value: "rejected",
    CommandOutcome.UNKNOWN.value: "unknown",
}
_HISTORY_OUTCOME: dict[str, tuple[str, ReasonCode]] = {  # 성공이 아닌 마지막 명령 결과 → (문구, code)
    CommandOutcome.FAILED.value: ("실패 → 고장 격리", ReasonCode.EXEC_FAILED),
    CommandOutcome.UNKNOWN.value: ("결과 불명 → 고장 격리", ReasonCode.EXEC_UNKNOWN),
    CommandOutcome.REJECTED.value: ("실행 안 함", ReasonCode.EXEC_FAILED),
}
_TIMELINE_FAILED: dict[str, str] = {  # 성공이 아닌 명령 결과 → 대여 타임라인 문구
    CommandOutcome.FAILED.value: "실패 · 고장 격리",
    CommandOutcome.UNKNOWN.value: "결과 불명 · 고장 격리",
    CommandOutcome.REJECTED.value: "거절 · 실행 안 함",
}
_METRIC_NAME: dict[str, str] = {  # 평가 리포트의 지표 이름 → 화면 이름
    "front_misselection_rate": "앞 기기 오선택률",
    "ece": "보정 오차",
    "switch_time": "전환 시간",
    "flicker": "흔들림",
}
_METRIC_LABEL: dict[bool | None, str] = {True: "통과", False: "✕ 미통과", None: "미측정"}  # 통과 여부 → 지표 문구

# 조회 서비스와 평가 리포트가 쓰는 고정 문구
NO_MODEL = "없음"  # 근접 모델이 없는 장소의 모델·보정 버전
NO_SIGNAL = "최근 신호 없음"  # 점검에서 최근 수신이 없는 비콘의 note
NO_SAMPLE = "—"  # 시행 수가 없는 평가 지표의 sample
OVERDUE = "대여 시간 초과"  # 점유 메모(`session_note`)와 대여 타임라인이 함께 쓴다
TIMELINE_RESERVED = f"예약 · {SessionState.RESERVED.value}"  # 대여 타임라인의 첫 항목


def _reason(value: Any) -> ReasonCode | None:
    """payload 의 값이 ReasonCode 값이면 그 열거형, 아니면 None."""
    if not isinstance(value, str):
        return None
    try:
        return ReasonCode(value)
    except ValueError:
        return None


def _reason_text(code: ReasonCode) -> str:
    return _REASON_TEXT.get(code, code.value)


def node_name(names: Mapping[str, str], node_id: str | None) -> str | None:
    """기기 이름. 이름이 없으면 node_id 를 쓴다."""
    if node_id is None:
        return None
    return names.get(node_id) or node_id


# ── 감사 행 ─────────────────────────────────────────────────────────


def audit_code(a: DashboardAudit) -> ReasonCode:
    """감사 행의 code (spec 6절). `reason` 이나 `result` 가 ReasonCode 값이면 그 값이다."""
    payload = a.payload
    for key in ("reason", "result"):
        code = _reason(payload.get(key))
        if code is not None:
            return code
    if payload.get("result") == "IN_PROGRESS":  # 판정 중인 요청의 재전송. ReasonCode 값이 아니다
        return ReasonCode.COMMAND_IN_PROGRESS
    result = payload.get("outcome")
    return _CODE_OF_RESULT.get(result, ReasonCode.OK) if isinstance(result, str) else ReasonCode.OK


def audit_text(a: DashboardAudit, names: Mapping[str, str]) -> str:
    """감사 행의 한 줄 문구. payload 의 `event` 와 단계로 고른다."""
    payload = a.payload
    event = payload.get("event") if isinstance(payload.get("event"), str) else None
    if a.step is Step.ADMIN:
        return _ADMIN_TEXT.get(event or "", "관리 기록")
    if a.step is Step.ANNOUNCE:
        return "기기 발표가 바뀌어 신뢰 해제"
    text = None if event is None else _event_text(event, payload)
    if text is None and payload.get("recheck") is True:
        text = _recheck_text(payload)
    if text is None:
        text = step_detail(a.step, payload, names)
    if text is not None:
        return text
    code = audit_code(a)
    return _reason_text(code) if code is not ReasonCode.OK else _STEP_NOTE.get(a.step, "판정 기록")


def _event_text(event: str, payload: Mapping[str, Any]) -> str | None:
    """실행·종료·고장 기록의 문구. 모르는 event 는 None."""
    if event == "record_result":
        result = payload.get("outcome")
        return _RESULT_TEXT.get(result) if isinstance(result, str) else None
    if event == "cancel_not_sent":
        reason = _reason(payload.get("reason"))
        return "보내지 않고 취소" if reason is None else f"보내지 않고 취소 · {_reason_text(reason)}"
    if event == "isolate_device":
        evidence = payload.get("evidence")
        return _ISOLATE_TEXT.get(fault_cause(evidence if isinstance(evidence, Mapping) else {}), "고장으로 격리")
    return _EVENT_TEXT.get(event)


def _recheck_text(payload: Mapping[str, Any]) -> str:
    """실행 직전 재검사 기록. 판정이 OK 가 아니면 사유를 덧붙인다."""
    if payload.get("verdict") == Verdict.OK.value:
        return "실행 직전 재검사 통과"
    reason = _reason(payload.get("reason"))
    if reason is None:
        return "실행 직전 재검사 거부"
    if reason is ReasonCode.RECHECK_FAILED:
        return _reason_text(reason)
    return f"실행 직전 재검사 거부 · {_reason_text(reason)}"


# ── 판정 단계 상세 ───────────────────────────────────────────────────


def step_detail(step: Step, payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    """판정 단계(⓪~⑥) 상세 행의 payload 로 그 단계의 문구를 만든다. 문구를 정할 수 없으면 None.

    단계 상세 행이 아니라 판정 행(`reason` 만 있다)이 와도 사유 문구를 돌려준다.
    ⑤⑥은 감사 행 하나(`outcome`)를 같이 쓰므로 같은 payload 로 단계에 맞는 문구를 만든다.
    """
    detail = _DETAIL.get(step)
    if detail is None:
        return None
    text = detail(payload, names)
    if text is not None:
        return text
    reason = _reason(payload.get("reason"))
    return None if reason is None else _reason_text(reason)


def _dedup(payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    result = payload.get("result")
    if result == "IN_PROGRESS":
        return "판정 중인 요청을 다시 보냄"
    code = _reason(result)
    if code is not None:
        return _reason_text(code)
    if payload.get("event") == "link_request_retry":
        return "서버 오류로 끝난 요청을 다시 판정"
    return None


def _beacon(payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    anchors = payload.get("anchors")
    if anchors is None:
        return None
    if payload.get("beacons") == 0:
        return "활성 비콘 없음"
    return "비콘 최근 수신" if anchors else _reason_text(ReasonCode.BEACON_NOT_SEEN)


def _candidate(payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    candidates = payload.get("candidates")
    if candidates is not None:
        return f"후보 {len(candidates)}대" if candidates else "후보 없음"
    if "threshold" in payload and payload["threshold"] is None:
        return "임계값 설정 없음"
    return None


def _proximity(payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    if "posteriors" not in payload:
        return None
    p = payload.get("p")  # 명시 지정은 대상의 확률, 아니면 가장 큰 후보 (기기 ID, 확률)
    if p is None and payload.get("best"):
        p = payload["best"][1]
    if p is None:
        return "근접 후보 없음"
    threshold = payload.get("threshold")
    below = threshold is not None and p < threshold
    return f"근접 {'부족' if below else '통과'} {p:.2f}"


def _policy(payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    reason = _reason(payload.get("reason"))
    if reason is None:
        return None
    return "정책 통과" if reason is ReasonCode.OK else _reason_text(reason)


def _device(payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    allocation = payload.get("allocation")
    if isinstance(allocation, Mapping):
        assigned = allocation.get("assigned")
        return f"배정 {node_name(names, assigned)}" if isinstance(assigned, str) else "배정 못 받음"
    result = payload.get("outcome")
    if result == ReserveOutcome.DEVICE_BUSY.value:
        return _reason_text(ReasonCode.DEVICE_BUSY)
    if result == ReserveOutcome.REOPERATING.value:
        return "본인이 점유 중"
    if result in (ReserveOutcome.RESERVED.value, ReserveOutcome.USER_BUSY.value,
                  ReserveOutcome.APPROVAL_EXHAUSTED.value):
        return "비어 있음"  # 기기는 비어 있었다. 뒤의 실패는 ⑥ 이다
    return None


def _user(payload: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    result = payload.get("outcome")
    if result == ReserveOutcome.RESERVED.value:
        return "다른 점유 없음"
    if result == ReserveOutcome.REOPERATING.value:
        return "본인 대여에 다시 조작"
    if result == ReserveOutcome.USER_BUSY.value:
        return _reason_text(ReasonCode.USER_BUSY)
    if result == ReserveOutcome.APPROVAL_EXHAUSTED.value:
        return _reason_text(ReasonCode.APPROVAL_EXHAUSTED)
    return None


_DETAIL: dict[Step, Callable[[Mapping[str, Any], Mapping[str, str]], str | None]] = {
    Step.DEDUP: _dedup,
    Step.BEACON: _beacon,
    Step.CANDIDATE: _candidate,
    Step.PROXIMITY: _proximity,
    Step.POLICY: _policy,
    Step.DEVICE_OCCUPANCY: _device,
    Step.USER_OCCUPANCY: _user,
}


# ── 이력·점유·고장 ───────────────────────────────────────────────────


def outcome(req: DashboardRequest, commands: Sequence[DashboardCommand]) -> tuple[str, ReasonCode | None]:
    """이력 행의 (outcome 문구, code). 실행이 없으면 판정 사유, 실행이 있으면 마지막 명령의 결과."""
    if req.verdict is None:
        return "판정 중", None
    if req.verdict is not Verdict.OK:
        return ("다시 질문" if req.verdict is Verdict.ASK else "실행 안 함"), req.reason_code
    last = max(commands, key=lambda c: c.seq, default=None)
    if last is None or last.outcome is None:
        return "실행 중", None
    if last.outcome == CommandOutcome.SUCCEEDED.value:
        return _DONE_TEXT[last.action], None
    if last.outcome == CommandOutcome.NOT_SENT.value:  # 보내지 않고 취소했다. 취소 사유가 code 다
        return "실행 안 함", _reason(last.evidence.get("reason")) or ReasonCode.COMMAND_NOT_SENT
    return _HISTORY_OUTCOME.get(last.outcome, ("실행 중", None))


def session_note(state: SessionState | None, device_state: DeviceState, overdue_minutes: float) -> str | None:
    """점유 행의 메모 (spec 6절). 고장, 반납 확인, 대여 시간 초과 순으로 먼저 맞는 것 하나."""
    if device_state is DeviceState.FAULT:
        return "고장으로 보존"
    if state is SessionState.CLOSING or device_state is DeviceState.CLOSING:
        return "반납 확인 중"
    if overdue_minutes > 0:
        return OVERDUE
    return None


def exec_result(outcome: str | None) -> str | None:
    """CommandOutcome 값 → exec_result 이벤트의 result. 보내지 않은 명령과 결과가 없는 명령은 None."""
    return None if outcome is None else _EXEC_RESULT.get(outcome)


def fault_cause(evidence: Mapping[str, Any]) -> ReasonCode:
    """격리 근거 → fault.cause (spec 6절). 기기가 보고한 결과가 근거이면 결과 불명과 실패를 가른다."""
    if evidence.get("source") == "device_result":
        unknown = evidence.get("state") == NodeResultState.UNKNOWN.value
        return ReasonCode.EXEC_UNKNOWN if unknown else ReasonCode.EXEC_FAILED
    return ReasonCode.TARGET_UNAVAILABLE


# ── 대여 타임라인·평가 리포트 ──────────────────────────────────────────


def timeline_command(cmd: DashboardCommand) -> str | None:
    """대여 타임라인에 쓰는 명령 결과 문구. 결과가 없거나 보내지 않고 취소한 명령은 `None`."""
    if cmd.outcome == CommandOutcome.SUCCEEDED.value:
        return f"{_DONE_TEXT[cmd.action]} · {SessionState.ACTIVE.value}"
    return _TIMELINE_FAILED.get(cmd.outcome)


def metric_name(metric: str) -> str:
    """평가 리포트의 지표 이름 → 화면 이름. 모르는 지표는 이름을 그대로 쓴다."""
    return _METRIC_NAME.get(metric, metric)


def metric_label(passed: bool | None) -> str:
    """평가 지표의 통과 여부 → 문구. 비어 있으면(`None`) 측정하지 못한 것이다."""
    return _METRIC_LABEL[passed]
