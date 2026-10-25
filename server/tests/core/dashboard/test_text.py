"""FR-11·FR-12A 대시보드 문구와 코드. 감사 payload 는 pipeline.py·execution_store.py·admin_repository.py 가
실제로 쓰는 모양이다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.dashboard.text import (
    audit_code,
    audit_text,
    exec_result,
    fault_cause,
    node_name,
    outcome,
    session_note,
    step_detail,
    timeline_command,
)
from nearpilot.shared.enums import (
    Action,
    DeviceState,
    ExecutionStatus,
    ReasonCode,
    SessionState,
    Step,
    Verdict,
)
from nearpilot.shared.models import DashboardAudit, DashboardCommand, DashboardRequest

T0 = datetime(2026, 10, 24, 14, 0, 0, tzinfo=timezone.utc)
NAMES = {"locker-1": "사물함 1", "locker-2": "사물함 2"}
SECRET = "u-7f3a9c-비밀"  # 사용자 ID, 계정 이름, 예외 repr 자리에 넣는다. 어떤 문구에도 나오면 안 된다


def audit(step: Step, payload: dict, seq: int = 1) -> DashboardAudit:
    return DashboardAudit(
        seq=seq, log_id=f"log-{seq}", internal_request_id="i1", command_id=None, step=step, payload=payload,
        created_at=T0 + timedelta(seconds=seq))


def request(verdict: Verdict | None, reason: ReasonCode | None, action: Action = Action.OPEN) -> DashboardRequest:
    return DashboardRequest(
        seq=1, internal_request_id="i1", user_id="user-1", kind="visitor", target_spec="nearest:locker",
        action=action, verdict=verdict, reason_code=reason, node_id="locker-1", decision_context=None,
        received_at=T0)


def command(
    seq: int, status: ExecutionStatus, result: str | None, *, action: Action = Action.OPEN,
    evidence: dict | None = None,
) -> DashboardCommand:
    return DashboardCommand(
        seq=seq, command_id=f"c{seq}", internal_request_id="i1", node_id="locker-1", action=action, status=status,
        use_id="use-1", send_started_at=T0, outcome=result, occurred_at=T0, evidence=evidence or {})


# ── FR-11: 사용자 ID 와 payload 문자열을 옮기지 않는다 ───────────────────

LEAKY_ROWS = [
    pytest.param(Step.ADMIN, {"event": "register_account", "user_id": SECRET, "role": "owner"}, id="계정 등록"),
    pytest.param(Step.ADMIN, {"event": "register_beacon", "beacon_id": "b-1", "user_id": SECRET,
                              "name": SECRET}, id="비콘 등록"),
    pytest.param(Step.ADMIN, {"event": "set_node_name", "node_id": "locker-1", "name": SECRET}, id="기기 이름"),
    pytest.param(Step.ADMIN, {"event": "assign_place", "place_id": "p1", "owner_user_id": SECRET,
                              "node_ids": ["locker-1"]}, id="장소 지정"),
    pytest.param(Step.ADMIN, {"event": SECRET, "user_id": SECRET}, id="모르는 관리 event"),
    pytest.param(Step.EXEC, {"result": "INTERNAL_ERROR", "error": f"RuntimeError('{SECRET}')"}, id="판정 오류"),
    pytest.param(Step.DEDUP, {"result": "INTERNAL_ERROR", "error": f"ValueError('{SECRET}')"}, id="⓪ 오류"),
    pytest.param(Step.EXEC, {"recheck": True, "settings_version": "v1", "verdict": "DENY",
                             "reason": "RECHECK_FAILED", "error": f"KeyError('{SECRET}')"}, id="재검사 오류"),
    pytest.param(Step.EXEC, {"event": "record_result", "outcome": "unknown",
                             "evidence": {"source": "device_result", "state": "unknown",
                                          "detail": {"user_id": SECRET}}}, id="결과 불명"),
    pytest.param(Step.FAULT, {"event": "isolate_device", "node_id": "locker-1",
                              "evidence": {"source": "sensor_fault", "user_id": SECRET}}, id="격리"),
    pytest.param(Step.EXEC, {"event": "cancel_not_sent", "reason": "NODE_NOT_CONNECTED",
                             "evidence": {"user_id": SECRET}}, id="미전송 취소"),
    pytest.param(Step.CLOSE, {"event": "finish_close", "use_id": SECRET, "check_id": SECRET, "evidence": {}},
                 id="반납 종료"),
    pytest.param(Step.BEACON, {"beacons": 1, "frames": 2, "anchors": ["locker-1"], "beacon_recent_sec": 10,
                               "user_id": SECRET}, id="① 상세"),
    pytest.param(Step.USER_OCCUPANCY, {"verdict": "BUSY", "reason": "USER_BUSY", "settings_version": "v1",
                                       "user_id": SECRET}, id="⑥ 판정"),
    pytest.param(Step.AUTH, {"result": "INVALID_TOKEN", "user_id": SECRET, "token": SECRET}, id="인증"),
    pytest.param(Step.ANNOUNCE, {"node_id": "locker-1", "capability": "locker", "user_id": SECRET}, id="발표"),
]


@pytest.mark.parametrize(("step", "payload"), LEAKY_ROWS)
def test_FR11_감사_문구에_사용자_ID를_넣지_않는다(step, payload):
    text = audit_text(audit(step, payload), NAMES)
    assert text
    assert SECRET not in text and "RuntimeError" not in text and "Error(" not in text


def test_FR11_관리_기록은_event_이름만으로_문구를_고른다():
    expected = {
        "register_account": "계정 등록", "register_beacon": "비콘 등록", "set_node_trust": "기기 신뢰 변경",
        "assign_place": "장소 지정", "set_policy": "접근 정책 변경", "set_release_policy": "반납 정책 변경",
        "set_node_name": "기기 이름 변경", "start_collection": "수집 시작", "end_collection": "수집 종료",
    }
    for event, text in expected.items():
        # payload 의 ID 와 이름은 문구를 바꾸지 못한다
        row = audit(Step.ADMIN, {"event": event, "user_id": SECRET, "name": SECRET, "role": "owner",
                                 "owner_user_id": SECRET})
        assert audit_text(row, NAMES) == text
    assert audit_text(audit(Step.ADMIN, {"event": SECRET}), NAMES) == "관리 기록"
    assert audit_text(audit(Step.ADMIN, {}), NAMES) == "관리 기록"


# ── 감사 행의 code ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("step", "payload", "expected"),
    [
        pytest.param(Step.BEACON, {"verdict": "DENY", "reason": "BEACON_NOT_SEEN", "settings_version": "v1"},
                     ReasonCode.BEACON_NOT_SEEN, id="reason"),
        pytest.param(Step.POLICY, {"node_id": "locker-2", "verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED",
                                   "policy_version": 1}, ReasonCode.APPROVAL_REQUIRED, id="④ 상세의 reason"),
        pytest.param(Step.DEDUP, {"result": "REQUEST_ID_CONFLICT"}, ReasonCode.REQUEST_ID_CONFLICT,
                     id="result 가 ReasonCode 값"),
        pytest.param(Step.EXEC, {"result": "INTERNAL_ERROR", "error": "x"}, ReasonCode.INTERNAL_ERROR,
                     id="result 오류"),
        pytest.param(Step.DEDUP, {"result": "IN_PROGRESS"}, ReasonCode.COMMAND_IN_PROGRESS,
                     id="IN_PROGRESS 는 ReasonCode 값이 아니다"),
        pytest.param(Step.EXEC, {"event": "cancel_not_sent", "reason": "NODE_NOT_CONNECTED", "evidence": {}},
                     ReasonCode.NODE_NOT_CONNECTED, id="미전송 취소의 reason"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "succeeded", "evidence": {}},
                     ReasonCode.OK, id="실행 succeeded"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "failed", "evidence": {}},
                     ReasonCode.EXEC_FAILED, id="실행 failed"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "rejected", "evidence": {}},
                     ReasonCode.EXEC_FAILED, id="실행 rejected"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "unknown", "evidence": {}},
                     ReasonCode.EXEC_UNKNOWN, id="실행 unknown"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "device_busy"}, ReasonCode.OK,
                     id="⑤ 상세의 outcome 은 실행 결과가 아니다"),
        pytest.param(Step.POLICY, {"verdict": "DENY", "reason": "NOT_A_CODE", "settings_version": "v1"},
                     ReasonCode.OK, id="ReasonCode 값이 아닌 reason"),
        pytest.param(Step.CLOSE, {"event": "finish_close", "use_id": "u", "check_id": "c", "evidence": {}},
                     ReasonCode.OK, id="코드 없는 행"),
        pytest.param(Step.FAULT, {"event": "isolate_device", "node_id": "locker-1",
                                  "evidence": {"source": "sensor_fault"}}, ReasonCode.OK, id="격리 행"),
        pytest.param(Step.ANNOUNCE, {}, ReasonCode.OK, id="빈 payload"),
        pytest.param(Step.EXEC, {"reason": ["BEACON_NOT_SEEN"], "result": 3, "outcome": ["failed"]}, ReasonCode.OK,
                     id="문자열이 아닌 값"),
    ],
)
def test_FR12A_감사_행의_코드는_사유_코드다(step, payload, expected):
    assert audit_code(audit(step, payload)) is expected


def test_FR12A_모든_사유_코드에_문구가_있다():
    texts = {}
    for code in ReasonCode:
        # 판정 행은 사유 문구를 그대로 쓴다. ⑥ 행은 단계 상세와 겹치지 않는다
        row = audit(Step.USER_OCCUPANCY, {"verdict": "DENY", "reason": code.value, "settings_version": "v1"})
        text = audit_text(row, NAMES)
        assert text and text != code.value, code
        texts[code] = text
    assert len(set(texts.values())) == len(texts)  # 사유마다 다른 문구다
    assert texts[ReasonCode.BEACON_NOT_SEEN] == "비콘 수신 없음"
    assert texts[ReasonCode.LOW_CONFIDENCE] == "어느 기기 앞인지 확신 부족"
    assert texts[ReasonCode.DEVICE_BUSY] == "다른 사람이 점유 중"


# ── 실행 결과, 고장 원인, 점유 메모, 기기 이름 ────────────────────────────


@pytest.mark.parametrize(
    ("result", "expected"),
    [("succeeded", "succeeded"), ("failed", "failed"), ("rejected", "rejected"), ("unknown", "unknown"),
     ("not_sent", None), (None, None), ("모르는 값", None)],
)
def test_FR12A_실행_결과_변환(result, expected):
    assert exec_result(result) == expected


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        pytest.param({"source": "device_result", "state": "unknown"}, ReasonCode.EXEC_UNKNOWN, id="기기 결과 불명"),
        pytest.param({"source": "device_result", "event_id": "e1", "session_id": "s1", "state": "unknown",
                      "error_code": None, "detail": {}}, ReasonCode.EXEC_UNKNOWN, id="실제 근거 모양"),
        pytest.param({"source": "device_result", "state": "failed"}, ReasonCode.EXEC_FAILED, id="기기 실패"),
        pytest.param({"source": "device_result", "state": "rejected"}, ReasonCode.EXEC_FAILED, id="기기 거절"),
        pytest.param({"source": "device_result"}, ReasonCode.EXEC_FAILED, id="결과 상태 없음"),
        pytest.param({"source": "sensor_fault", "session_id": "s1", "seq": 3}, ReasonCode.TARGET_UNAVAILABLE,
                     id="센서 고장"),
        pytest.param({"source": "announce_changed", "previous_capability": "locker"}, ReasonCode.TARGET_UNAVAILABLE,
                     id="발표 변경"),
        pytest.param({"source": "server_restart"}, ReasonCode.TARGET_UNAVAILABLE, id="서버 재시작"),
        pytest.param({}, ReasonCode.TARGET_UNAVAILABLE, id="근거 없음"),
    ],
)
def test_FR12A_고장_원인_변환(evidence, expected):
    assert fault_cause(evidence) is expected


@pytest.mark.parametrize(
    ("state", "device", "overdue", "expected"),
    [
        pytest.param(SessionState.ACTIVE, DeviceState.FAULT, 0, "고장으로 보존", id="고장"),
        pytest.param(SessionState.ACTIVE, DeviceState.FAULT, 30.0, "고장으로 보존", id="고장은 초과보다 먼저"),
        pytest.param(SessionState.CLOSING, DeviceState.CLOSING, 0, "반납 확인 중", id="반납 확인"),
        pytest.param(SessionState.CLOSING, DeviceState.CLOSING, 7.5, "반납 확인 중", id="반납 확인은 초과보다 먼저"),
        pytest.param(SessionState.CLOSING, DeviceState.FAULT, 0, "고장으로 보존", id="고장은 반납 확인보다 먼저"),
        pytest.param(SessionState.CLOSING, DeviceState.ACTIVE, 0, "반납 확인 중", id="세션만 CLOSING"),
        pytest.param(SessionState.ACTIVE, DeviceState.CLOSING, 0, "반납 확인 중", id="기기만 CLOSING"),
        pytest.param(SessionState.ACTIVE, DeviceState.ACTIVE, 12.5, "대여 시간 초과", id="초과"),
        pytest.param(SessionState.ACTIVE, DeviceState.ACTIVE, 0, None, id="정상"),
        pytest.param(SessionState.RESERVED, DeviceState.RESERVED, 0, None, id="예약"),
        pytest.param(None, DeviceState.ACTIVE, 0, None, id="공용형 유지 대상자"),
    ],
)
def test_FR12A_점유_메모(state, device, overdue, expected):
    assert session_note(state, device, overdue) == expected


def test_FR12A_기기_이름이_없으면_ID를_쓴다():
    assert node_name(NAMES, "locker-1") == "사물함 1"
    assert node_name(NAMES, "locker-9") == "locker-9"
    assert node_name({"locker-9": ""}, "locker-9") == "locker-9"
    assert node_name(NAMES, None) is None


@pytest.mark.parametrize(
    ("status", "result", "action", "expected"),
    [
        pytest.param(ExecutionStatus.SUCCEEDED, "succeeded", Action.OPEN, "열림 · ACTIVE", id="열림"),
        pytest.param(ExecutionStatus.SUCCEEDED, "succeeded", Action.ON, "켜짐 · ACTIVE", id="켜짐"),
        pytest.param(ExecutionStatus.SUCCEEDED, "succeeded", Action.OFF, "꺼짐 · ACTIVE", id="꺼짐"),
        pytest.param(ExecutionStatus.FAILED, "failed", Action.OPEN, "실패 · 고장 격리", id="실패"),
        pytest.param(ExecutionStatus.UNKNOWN, "unknown", Action.OPEN, "결과 불명 · 고장 격리", id="결과 불명"),
        pytest.param(ExecutionStatus.REJECTED, "rejected", Action.OPEN, "거절 · 실행 안 함", id="기기가 거절"),
        pytest.param(ExecutionStatus.NOT_SENT, "not_sent", Action.OPEN, None, id="보내지 않고 취소"),
        pytest.param(ExecutionStatus.IN_PROGRESS, None, Action.OPEN, None, id="결과 기다림"),
    ],
)
def test_FR12A_대여_타임라인_명령_문구(status, result, action, expected):
    assert timeline_command(command(1, status, result, action=action)) == expected


# ── 이력 outcome ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("req", "commands", "expected"),
    [
        pytest.param(request(Verdict.OK, ReasonCode.OK), [command(1, ExecutionStatus.SUCCEEDED, "succeeded")],
                     ("열림", None), id="열림"),
        pytest.param(request(Verdict.OK, ReasonCode.OK, Action.ON),
                     [command(1, ExecutionStatus.SUCCEEDED, "succeeded", action=Action.ON)], ("켜짐", None),
                     id="켜짐"),
        pytest.param(request(Verdict.OK, ReasonCode.OK, Action.OFF),
                     [command(1, ExecutionStatus.SUCCEEDED, "succeeded", action=Action.OFF)], ("꺼짐", None),
                     id="꺼짐"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), [command(1, ExecutionStatus.UNKNOWN, "unknown")],
                     ("결과 불명 → 고장 격리", ReasonCode.EXEC_UNKNOWN), id="결과 불명"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), [command(1, ExecutionStatus.FAILED, "failed")],
                     ("실패 → 고장 격리", ReasonCode.EXEC_FAILED), id="실패"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), [command(1, ExecutionStatus.REJECTED, "rejected")],
                     ("실행 안 함", ReasonCode.EXEC_FAILED), id="기기가 거절"),
        pytest.param(request(Verdict.OK, ReasonCode.OK),
                     [command(1, ExecutionStatus.NOT_SENT, "not_sent",
                              evidence={"source": "no_connection", "reason": "NODE_NOT_CONNECTED"})],
                     ("실행 안 함", ReasonCode.NODE_NOT_CONNECTED), id="보내지 않고 취소 (사유)"),
        pytest.param(request(Verdict.OK, ReasonCode.OK),
                     [command(1, ExecutionStatus.NOT_SENT, "not_sent", evidence={"reason": "NOT_A_CODE"})],
                     ("실행 안 함", ReasonCode.COMMAND_NOT_SENT), id="보내지 않고 취소 (사유 모름)"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), [], ("실행 중", None), id="명령 전"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), [command(1, ExecutionStatus.PENDING, None)],
                     ("실행 중", None), id="전송 전"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), [command(1, ExecutionStatus.IN_PROGRESS, None)],
                     ("실행 중", None), id="결과 기다림"),
        pytest.param(request(Verdict.OK, ReasonCode.OK),
                     [command(2, ExecutionStatus.SUCCEEDED, "succeeded"),
                      command(1, ExecutionStatus.NOT_SENT, "not_sent")],
                     ("열림", None), id="마지막 명령은 seq 가 큰 명령"),
        pytest.param(request(Verdict.BUSY, ReasonCode.DEVICE_BUSY), [], ("실행 안 함", ReasonCode.DEVICE_BUSY),
                     id="점유 중"),
        pytest.param(request(Verdict.ASK, ReasonCode.LOW_CONFIDENCE), [], ("다시 질문", ReasonCode.LOW_CONFIDENCE),
                     id="다시 질문"),
        pytest.param(request(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED), [],
                     ("실행 안 함", ReasonCode.APPROVAL_REQUIRED), id="승인 필요"),
        pytest.param(request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN), [],
                     ("실행 안 함", ReasonCode.BEACON_NOT_SEEN), id="비콘 없음"),
        pytest.param(request(None, None), [], ("판정 중", None), id="판정 중"),
    ],
)
def test_FR12A_이력_outcome_문구와_code(req, commands, expected):
    assert outcome(req, commands) == expected


# ── 판정 단계 상세와 감사 문구 ────────────────────────────────────────────


def beacon(anchors=("locker-1",), beacons=1):
    return {"beacons": beacons, "frames": 3, "anchors": list(anchors), "beacon_recent_sec": 10}


def decision(reason: str, verdict: str = "DENY") -> dict:
    return {"verdict": verdict, "reason": reason, "settings_version": "v1"}


@pytest.mark.parametrize(
    ("step", "payload", "expected"),
    [
        pytest.param(Step.DEDUP, {"result": "REQUEST_ID_CONFLICT"}, "같은 요청 번호에 다른 내용", id="⓪ 충돌"),
        pytest.param(Step.DEDUP, {"result": "IN_PROGRESS"}, "판정 중인 요청을 다시 보냄", id="⓪ 처리 중 재전송"),
        pytest.param(Step.DEDUP, {"result": "INTERNAL_ERROR", "error": "x"}, "서버 오류", id="⓪ 오류"),
        pytest.param(Step.DEDUP, {"event": "link_request_retry", "previous_verdict": "DENY"},
                     "서버 오류로 끝난 요청을 다시 판정", id="⓪ 다시 판정"),
        pytest.param(Step.DEDUP, {**decision("CAPACITY_EXCEEDED"), "capacity_exceeded": True}, "서버가 바쁨",
                     id="⓪ 한도 초과 판정 행"),
        pytest.param(Step.BEACON, beacon(), "비콘 최근 수신", id="① 수신"),
        pytest.param(Step.BEACON, beacon(anchors=()), "비콘 수신 없음", id="① 수신 없음"),
        pytest.param(Step.BEACON, beacon(anchors=(), beacons=0), "활성 비콘 없음", id="① 활성 비콘 없음"),
        pytest.param(Step.BEACON, decision("BEACON_NOT_SEEN"), "비콘 수신 없음", id="① 판정 행"),
        pytest.param(Step.CANDIDATE, {"place_id": "p1", "candidates": ["a", "b", "c"], "online_ttl_sec": 10},
                     "후보 3대", id="② 후보"),
        pytest.param(Step.CANDIDATE, {"place_id": "p1", "candidates": [], "online_ttl_sec": 10}, "후보 없음",
                     id="② 후보 없음"),
        pytest.param(Step.CANDIDATE, {"threshold": None, "explicit": False}, "임계값 설정 없음",
                     id="② 임계값 설정 없음"),
        pytest.param(Step.PROXIMITY, {"posteriors": {"locker-1": 0.97}, "threshold": 0.5, "model_version": "m",
                                      "calib_version": "c", "p": 0.97}, "근접 통과 0.97", id="③ 명시 지정"),
        pytest.param(Step.PROXIMITY, {"posteriors": {"locker-1": 0.91}, "threshold": 0.75, "model_version": "m",
                                      "calib_version": "c", "best": ["locker-1", 0.91]}, "근접 통과 0.91",
                     id="③ 가장 가까운 기기"),
        pytest.param(Step.PROXIMITY, {"posteriors": {"locker-1": 0.31}, "threshold": 0.5, "p": 0.31},
                     "근접 부족 0.31", id="③ 임계값 미만"),
        pytest.param(Step.PROXIMITY, {"posteriors": {"locker-1": 0.5}, "threshold": 0.5, "p": 0.5}, "근접 통과 0.50",
                     id="③ 임계값과 같다"),
        pytest.param(Step.PROXIMITY, {"posteriors": {}, "threshold": 0.5, "best": None}, "근접 후보 없음",
                     id="③ 후보 확률 없음"),
        pytest.param(Step.PROXIMITY, decision("LOW_CONFIDENCE", "ASK"), "어느 기기 앞인지 확신 부족", id="③ 판정 행"),
        pytest.param(Step.POLICY, {"node_id": "locker-1", "verdict": "OK", "reason": "OK", "policy_version": 1},
                     "정책 통과", id="④ 통과"),
        pytest.param(Step.POLICY, {"node_id": "locker-2", "verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED",
                                   "policy_version": 1}, "승인 필요", id="④ 승인 필요"),
        pytest.param(Step.POLICY, {"node_id": "locker-2", "verdict": "DENY", "reason": "POLICY_DENIED",
                                   "policy_version": None}, "정책이 허락하지 않음", id="④ 정책 없음"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "reserved"}, "비어 있음", id="⑤ 예약"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "device_busy"}, "다른 사람이 점유 중",
                     id="⑤ 점유 중"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "user_busy"}, "비어 있음",
                     id="⑤ 기기는 비었다 (⑥ 에서 멈춘다)"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "approval_exhausted"}, "비어 있음",
                     id="⑤ 승인 소진 (⑥ 에서 멈춘다)"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"node_id": "locker-1", "outcome": "reoperating"}, "본인이 점유 중",
                     id="⑤ 본인 재조작"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"allocation": {"allowed": ["locker-1", "locker-2"],
                                                            "assigned": "locker-2"}}, "배정 사물함 2",
                     id="⑤ 배정"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"allocation": {"allowed": ["locker-1"], "assigned": "locker-9"}},
                     "배정 locker-9", id="⑤ 이름 없는 기기를 배정"),
        pytest.param(Step.DEVICE_OCCUPANCY, {"allocation": {"allowed": ["locker-1"], "assigned": None}},
                     "배정 못 받음", id="⑤ 배정 못 받음"),
        pytest.param(Step.DEVICE_OCCUPANCY, decision("DEVICE_BUSY", "BUSY"), "다른 사람이 점유 중", id="⑤ 판정 행"),
        pytest.param(Step.USER_OCCUPANCY, {"node_id": "locker-1", "outcome": "reserved"}, "다른 점유 없음", id="⑥ 예약"),
        pytest.param(Step.USER_OCCUPANCY, {"node_id": "locker-1", "outcome": "reoperating"}, "본인 대여에 다시 조작",
                     id="⑥ 본인 재조작"),
        pytest.param(Step.USER_OCCUPANCY, {"node_id": "locker-1", "outcome": "user_busy"}, "이미 다른 기기를 쓰는 중",
                     id="⑥ 다른 기기를 씀"),
        pytest.param(Step.USER_OCCUPANCY, {"node_id": "locker-1", "outcome": "approval_exhausted"},
                     "사용 횟수를 다 씀", id="⑥ 승인 소진"),
        pytest.param(Step.USER_OCCUPANCY, decision("USER_BUSY", "BUSY"), "이미 다른 기기를 쓰는 중", id="⑥ 판정 행"),
        pytest.param(Step.USER_OCCUPANCY, {"node_id": "locker-1", "outcome": "device_busy"}, None,
                     id="⑥ 닿지 않은 단계의 행"),
        pytest.param(Step.BEACON, {}, None, id="알 수 없는 모양"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "succeeded"}, None, id="판정 단계가 아니다"),
        pytest.param(Step.ADMIN, decision("DEVICE_BUSY"), None, id="관리 기록은 단계 상세가 없다"),
    ],
)
def test_FR12A_단계_상세_문구(step, payload, expected):
    assert step_detail(step, payload, NAMES) == expected


@pytest.mark.parametrize(
    ("step", "payload", "expected"),
    [
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "succeeded", "evidence": {}}, "실행 완료",
                     id="실행 성공"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "failed", "evidence": {}},
                     "실행 실패 → 고장 격리", id="실행 실패"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "rejected", "evidence": {}},
                     "기기가 실행을 거절", id="실행 거절"),
        pytest.param(Step.EXEC, {"event": "record_result", "outcome": "unknown", "evidence": {}},
                     "결과 불명 → 고장 격리", id="결과 불명"),
        pytest.param(Step.EXEC, {"event": "cancel_not_sent", "reason": "NODE_NOT_CONNECTED", "evidence": {}},
                     "보내지 않고 취소 · 기기 연결 없음", id="미전송 취소"),
        pytest.param(Step.EXEC, {"event": "cancel_not_sent", "reason": "NOT_A_CODE", "evidence": {}},
                     "보내지 않고 취소", id="미전송 취소 (사유 모름)"),
        pytest.param(Step.FAULT, {"event": "isolate_device", "node_id": "locker-1",
                                  "evidence": {"source": "device_result", "state": "unknown"}},
                     "결과 불명으로 격리", id="격리 (결과 불명)"),
        pytest.param(Step.FAULT, {"event": "isolate_device", "node_id": "locker-1",
                                  "evidence": {"source": "device_result", "state": "failed"}},
                     "실행 실패로 격리", id="격리 (실패)"),
        pytest.param(Step.FAULT, {"event": "isolate_device", "node_id": "locker-1",
                                  "evidence": {"source": "sensor_fault"}}, "고장으로 격리", id="격리 (센서)"),
        pytest.param(Step.FAULT, {"event": "isolate_device", "node_id": "locker-1"}, "고장으로 격리",
                     id="격리 (근거 없음)"),
        pytest.param(Step.FAULT, {"event": "settle_recovery", "recovery_id": "r1", "node_id": "locker-1"},
                     "고장 복구 정산", id="복구 정산"),
        pytest.param(Step.CLOSE, {"event": "finish_close", "use_id": "u1", "check_id": "c1", "evidence": {}},
                     "반납 완료", id="반납 종료"),
        pytest.param(Step.ANNOUNCE, {"node_id": "locker-1", "previous_capability": "locker", "capability": "light"},
                     "기기 발표가 바뀌어 신뢰 해제", id="발표 변경"),
        pytest.param(Step.EXEC, {"recheck": True, "settings_version": "v1", "verdict": "OK", "reason": "OK",
                                 "p": 0.9, "threshold": 0.5}, "실행 직전 재검사 통과", id="재검사 통과"),
        pytest.param(Step.EXEC, {"recheck": True, "settings_version": "v1", "verdict": "ASK",
                                 "reason": "MOVE_CLOSER"}, "실행 직전 재검사 거부 · 기기에 더 가까이 가야 함",
                     id="재검사 거부"),
        pytest.param(Step.EXEC, {"recheck": True, "settings_version": "v1", "verdict": "DENY",
                                 "reason": "RECHECK_FAILED", "error": "x"}, "실행 직전 재검사 실패", id="재검사 오류"),
        pytest.param(Step.EXEC, {"result": "INTERNAL_ERROR", "error": "x"}, "서버 오류", id="판정 중 오류"),
        pytest.param(Step.PROXIMITY, {"posteriors": {"locker-1": 0.91}, "threshold": 0.75, "best": ["locker-1", 0.91]},
                     "근접 통과 0.91", id="판정 단계는 단계 상세와 같은 문구"),
        pytest.param(Step.AUTH, {"result": "INVALID_TOKEN"}, "토큰 확인 실패", id="인증 거부"),
        pytest.param(Step.AUTH, {}, "인증 기록", id="인증 (코드 없음)"),
        pytest.param(Step.EXEC, {"unexpected": True}, "실행 기록", id="실행 (모르는 모양)"),
        pytest.param(Step.BEACON, {"unexpected": True}, "판정 기록", id="판정 단계 (모르는 모양)"),
    ],
)
def test_FR12A_감사_문구는_event와_단계로_고른다(step, payload, expected):
    assert audit_text(audit(step, payload), NAMES) == expected
