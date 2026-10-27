"""FR-12A 요청 한 건의 판정 단계 복원. 손으로 만든 감사 행은 pipeline.py 가 실제로 쓰는 payload 모양이다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.dashboard import StepState, proximity, request_steps, request_tool, responded_at
from nearpilot.shared.enums import Action, Occupancy, ReasonCode, Step, Verdict
from nearpilot.shared.models import DashboardAudit, DashboardRequest

T0 = datetime(2026, 10, 24, 14, 0, 0, tzinfo=timezone.utc)
NAMES = {"locker-1": "사물함 1", "locker-2": "사물함 2"}
STEPS = [Step.DEDUP, Step.BEACON, Step.CANDIDATE, Step.PROXIMITY, Step.POLICY, Step.DEVICE_OCCUPANCY,
         Step.USER_OCCUPANCY]


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def trail(*steps: tuple[Step, dict]) -> list[DashboardAudit]:
    """감사 행을 rowid 1부터 차례로 붙인다. 행 시각은 rowid 초다."""
    return [
        DashboardAudit(seq=i, log_id=f"log-{i}", internal_request_id="i1", command_id=None, step=step,
                       payload=payload, created_at=at(i))
        for i, (step, payload) in enumerate(steps, start=1)
    ]


def request(verdict: Verdict | None, reason: ReasonCode | None, node_id: str | None = "locker-1") -> DashboardRequest:
    return DashboardRequest(
        seq=1, internal_request_id="i1", user_id="user-1", kind="visitor", target_spec="nearest:locker",
        action=Action.OPEN, verdict=verdict, reason_code=reason, node_id=node_id, decision_context=None,
        received_at=T0)


# ── 판정 흐름이 남기는 감사 행 (pipeline.py 의 _audit_step 과 같은 모양) ─────────────

BEACON = (Step.BEACON, {"beacons": 1, "frames": 3, "anchors": ["locker-1"], "beacon_recent_sec": 10})
NO_BEACON = (Step.BEACON, {"beacons": 1, "frames": 0, "anchors": [], "beacon_recent_sec": 10})
CANDIDATE = (Step.CANDIDATE, {"place_id": "p1", "candidates": ["locker-1", "locker-2"], "online_ttl_sec": 10})
NO_CANDIDATE = (Step.CANDIDATE, {"place_id": "p1", "candidates": [], "online_ttl_sec": 10})


def near(p: float, *, explicit: bool = False, threshold: float = 0.5) -> tuple[Step, dict]:
    detail = {"posteriors": {"locker-1": p, "locker-2": 0.05}, "threshold": threshold, "model_version": "m1",
              "calib_version": "c1"}
    return Step.PROXIMITY, detail | ({"p": p} if explicit else {"best": ["locker-1", p]})


def policy(verdict: str = "OK", reason: str = "OK", node_id: str = "locker-1") -> tuple[Step, dict]:
    return Step.POLICY, {"node_id": node_id, "verdict": verdict, "reason": reason, "policy_version": 1}


def reserve(outcome: str, node_id: str = "locker-1") -> tuple[Step, dict]:
    return Step.DEVICE_OCCUPANCY, {"node_id": node_id, "outcome": outcome}


def decided(step: Step, verdict: str, reason: str, **extra) -> tuple[Step, dict]:
    """판정 단계가 판정을 확정하며 남기는 마지막 행 (`_Stop` 이거나 OK 의 마지막 단계)."""
    return step, {"verdict": verdict, "reason": reason, "settings_version": "v1", **extra}


OK_RENTAL = trail(BEACON, CANDIDATE, near(0.91), policy(), reserve("reserved"),
                  decided(Step.USER_OCCUPANCY, "OK", "OK"))
OK_SHARED = trail(BEACON, CANDIDATE, near(0.97, explicit=True), policy(), reserve("reserved"),
                  decided(Step.POLICY, "OK", "OK"))  # 공용형은 ④ 에 판정 행이 남는다 (use_id 가 없다)

RETRY = (Step.DEDUP, {"event": "link_request_retry", "previous_verdict": "DENY",
                      "previous_reason_code": "INTERNAL_ERROR", "previous_node_id": None, "previous_p_max": None,
                      "previous_policy_ver": None})  # 오류로 끝난 요청을 다시 판정하기 시작한 ⓪ 행
ERROR = (Step.EXEC, {"result": "INTERNAL_ERROR", "error": "RuntimeError('x')"})

P, S, K, W = "pass", "stop", "skip", "pending"


@pytest.mark.parametrize(
    ("req", "rows", "occupancy", "expected"),
    [
        pytest.param(request(Verdict.OK, ReasonCode.OK), OK_RENTAL, Occupancy.RENTAL, [P, P, P, P, P, P, P],
                     id="OK 대여: 일곱 칸 pass"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), OK_SHARED, Occupancy.SHARED, [P, P, P, P, P, K, K],
                     id="OK 공용: ⑤⑥ skip"),
        pytest.param(request(Verdict.OK, ReasonCode.OK), OK_SHARED, None, [P, P, P, P, P, P, P],
                     id="OK 점유형을 모르면 공용형 규칙을 쓰지 않는다"),
        pytest.param(request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None),
                     trail(NO_BEACON, decided(Step.BEACON, "DENY", "BEACON_NOT_SEEN")), Occupancy.RENTAL,
                     [P, S, K, K, K, K, K], id="① 거부: ⓪ pass, ① stop, ②~⑥ skip"),
        pytest.param(request(Verdict.DENY, ReasonCode.NO_CANDIDATE, None),
                     trail(BEACON, NO_CANDIDATE, decided(Step.CANDIDATE, "DENY", "NO_CANDIDATE")), None,
                     [P, P, S, K, K, K, K], id="② 후보 없음"),
        pytest.param(request(Verdict.DENY, ReasonCode.INVALID_INPUT, None),
                     trail(BEACON, decided(Step.CANDIDATE, "DENY", "INVALID_INPUT")), None,
                     [P, P, S, K, K, K, K], id="② 상세 행 없이 판정 행만 남은 거부"),
        pytest.param(request(Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None),
                     trail(BEACON, CANDIDATE, near(0.31), decided(Step.PROXIMITY, "ASK", "LOW_CONFIDENCE")),
                     Occupancy.RENTAL, [P, P, P, S, K, K, K], id="③ ASK"),
        pytest.param(request(Verdict.ASK, ReasonCode.MOVE_CLOSER),
                     trail(BEACON, CANDIDATE, near(0.31, explicit=True),
                           decided(Step.PROXIMITY, "ASK", "MOVE_CLOSER")),
                     Occupancy.RENTAL, [P, P, P, S, K, K, K], id="③ 명시 지정의 ASK"),
        pytest.param(request(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, "locker-2"),
                     trail(BEACON, CANDIDATE, near(0.91),
                           policy("NEED_APPROVAL", "APPROVAL_REQUIRED", "locker-2"),
                           decided(Step.POLICY, "NEED_APPROVAL", "APPROVAL_REQUIRED")),
                     Occupancy.RENTAL, [P, P, P, P, S, K, K], id="④ 승인 필요"),
        pytest.param(request(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED),
                     trail(BEACON, CANDIDATE, near(0.91), policy("NEED_APPROVAL", "APPROVAL_EXHAUSTED"),
                           decided(Step.POLICY, "NEED_APPROVAL", "APPROVAL_EXHAUSTED")),
                     Occupancy.RENTAL, [P, P, P, P, S, K, K], id="④ 정책이 돌려준 승인 소진은 ④ 에서 멈춘다"),
        pytest.param(request(Verdict.BUSY, ReasonCode.DEVICE_BUSY),
                     trail(BEACON, CANDIDATE, near(0.91), policy(), reserve("device_busy"),
                           decided(Step.DEVICE_OCCUPANCY, "BUSY", "DEVICE_BUSY")),
                     Occupancy.RENTAL, [P, P, P, P, P, S, K], id="⑤ DEVICE_BUSY"),
        pytest.param(request(Verdict.BUSY, ReasonCode.USER_BUSY),
                     trail(BEACON, CANDIDATE, near(0.91), policy(), reserve("user_busy"),
                           decided(Step.USER_OCCUPANCY, "BUSY", "USER_BUSY")),
                     Occupancy.RENTAL, [P, P, P, P, P, P, S], id="⑥ USER_BUSY: ⑤ pass"),
        pytest.param(request(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED),
                     trail(BEACON, CANDIDATE, near(0.91), policy(), reserve("approval_exhausted"),
                           decided(Step.USER_OCCUPANCY, "NEED_APPROVAL", "APPROVAL_EXHAUSTED")),
                     Occupancy.RENTAL, [P, P, P, P, P, P, S], id="⑥ 예약 단계의 승인 소진: ⑤ pass"),
        pytest.param(request(Verdict.BUSY, ReasonCode.DEVICE_BUSY, None),
                     trail(BEACON, CANDIDATE, near(0.91), policy(), policy("OK", "OK", "locker-2"),
                           decided(Step.DEVICE_OCCUPANCY, "BUSY", "DEVICE_BUSY")),
                     None, [P, P, P, P, P, S, K], id="⑤ any: 허용쌍이 없어 상세 행 없이 판정 행만 남음"),
        pytest.param(request(Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED, None),
                     trail(decided(Step.DEDUP, "DENY", "CAPACITY_EXCEEDED", capacity_exceeded=True)), None,
                     [S, K, K, K, K, K, K], id="⓪ 한도 초과: ⓪ stop"),
        pytest.param(request(Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT, None),
                     trail((Step.DEDUP, {"result": "REQUEST_ID_CONFLICT"})), None,
                     [S, K, K, K, K, K, K], id="⓪ 충돌 행만 있는 거부: ⓪ stop"),
        pytest.param(request(Verdict.DENY, ReasonCode.INTERNAL_ERROR, None),
                     trail(BEACON, CANDIDATE, near(0.91),
                           (Step.EXEC, {"result": "INTERNAL_ERROR", "error": "RuntimeError('x')"})), None,
                     [P, P, P, S, K, K, K], id="서버 오류: 판정 행이 없으면 닿은 마지막 단계에서 멈춘다"),
        pytest.param(request(Verdict.DENY, ReasonCode.INTERNAL_ERROR, None), [], None,
                     [S, K, K, K, K, K, K], id="서버 오류: 감사 행이 없으면 ⓪ 에서 멈춘다"),
        pytest.param(request(None, None, None), [], None, [W, W, W, W, W, W, W], id="판정 중: 감사 행이 없다"),
        pytest.param(request(None, None, None), trail(BEACON), None, [P, P, W, W, W, W, W],
                     id="판정 중: ⓪ 은 ① 이 있으면 pass"),
        pytest.param(request(None, None, None), trail(BEACON, CANDIDATE, near(0.91), policy(), reserve("reserved")),
                     Occupancy.RENTAL, [P, P, P, P, P, P, W], id="판정 중: 닿지 않은 단계는 pending"),
    ],
)
def test_FR12A_단계_복원_규칙(req, rows, occupancy, expected):
    states = request_steps(req, rows, occupancy, NAMES)
    assert [s.step for s in states] == STEPS
    assert [s.result for s in states] == expected


def test_FR12A_감사_행_순서와_상관없이_같은_단계를_만든다():
    req = request(Verdict.OK, ReasonCode.OK)
    assert request_steps(req, list(reversed(OK_RENTAL)), Occupancy.RENTAL, NAMES) == \
        request_steps(req, OK_RENTAL, Occupancy.RENTAL, NAMES)


@pytest.mark.parametrize("verdict,reason", [
    (Verdict.OK, ReasonCode.OK),
    (Verdict.DENY, ReasonCode.POLICY_DENIED),
])
def test_FR12A_FR17_명시_지정의_근접_생략은_skip이고_확률이_없다(verdict, reason):
    rows = trail(BEACON, CANDIDATE,
                 (Step.PROXIMITY, {"skipped": True, "reason": "explicit_target", "node_id": "locker-1"}),
                 policy(verdict.value, reason.value),
                 decided(Step.POLICY, verdict.value, reason.value))
    states = request_steps(request(verdict, reason), rows, Occupancy.RENTAL, NAMES)
    assert states[3].result == "skip"
    assert states[4].result == ("pass" if verdict is Verdict.OK else "stop")
    assert proximity(rows) is None


def test_FR12A_단계의_상세와_시각은_그_단계의_감사_행에서_온다():
    states = request_steps(request(Verdict.OK, ReasonCode.OK), OK_RENTAL, Occupancy.RENTAL, NAMES)
    assert [(s.detail, s.at) for s in states] == [
        (None, None),  # ⓪ 은 새 요청에 감사 행이 없다
        ("비콘 최근 수신", at(1)),
        ("후보 2대", at(2)),
        ("근접 통과 0.91", at(3)),
        ("정책 통과", at(4)),
        ("비어 있음", at(5)),
        ("다른 점유 없음", at(5)),  # ⑤⑥ 은 감사 행 하나를 같이 쓴다
    ]


def test_FR12A_건너뛴_칸과_기다리는_칸에는_상세도_시각도_없다():
    stopped = request_steps(
        request(Verdict.BUSY, ReasonCode.DEVICE_BUSY),
        trail(BEACON, CANDIDATE, near(0.91), policy(), reserve("device_busy"),
              decided(Step.DEVICE_OCCUPANCY, "BUSY", "DEVICE_BUSY")), Occupancy.RENTAL, NAMES)
    assert stopped[5] == StepState(Step.DEVICE_OCCUPANCY, "stop", "다른 사람이 점유 중", at(5))
    assert stopped[6] == StepState(Step.USER_OCCUPANCY, "skip", None, None)
    pending = request_steps(request(None, None, None), trail(BEACON), None, NAMES)
    assert pending[2] == StepState(Step.CANDIDATE, "pending", None, None)
    shared = request_steps(request(Verdict.OK, ReasonCode.OK), OK_SHARED, Occupancy.SHARED, NAMES)
    assert shared[5:] == [StepState(Step.DEVICE_OCCUPANCY, "skip", None, None),
                          StepState(Step.USER_OCCUPANCY, "skip", None, None)]


def test_FR12A_멈춘_단계의_상세는_상세_행을_쓰고_없으면_판정_행의_사유를_쓴다():
    with_detail = request_steps(
        request(Verdict.ASK, ReasonCode.LOW_CONFIDENCE, None),
        trail(BEACON, CANDIDATE, near(0.58, threshold=0.75), decided(Step.PROXIMITY, "ASK", "LOW_CONFIDENCE")),
        Occupancy.RENTAL, NAMES)
    assert with_detail[3] == StepState(Step.PROXIMITY, "stop", "근접 부족 0.58", at(3))  # 판정 행 시각(4)이 아니다
    without_detail = request_steps(
        request(Verdict.BUSY, ReasonCode.DEVICE_BUSY, None),
        trail(BEACON, CANDIDATE, near(0.91), policy(), decided(Step.DEVICE_OCCUPANCY, "BUSY", "DEVICE_BUSY")),
        None, NAMES)
    assert without_detail[5] == StepState(Step.DEVICE_OCCUPANCY, "stop", "다른 사람이 점유 중", at(5))


def test_FR12A_같은_사유도_판정_행이_남은_단계에서_멈춘다():
    # APPROVAL_EXHAUSTED 는 ④ 정책에서도 ⑥ 예약에서도 나온다. 사유만으로는 단계를 정할 수 없다
    by_policy = request_steps(
        request(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED),
        trail(BEACON, CANDIDATE, near(0.91), policy("NEED_APPROVAL", "APPROVAL_EXHAUSTED"),
              decided(Step.POLICY, "NEED_APPROVAL", "APPROVAL_EXHAUSTED")), Occupancy.RENTAL, NAMES)
    by_reserve = request_steps(
        request(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED),
        trail(BEACON, CANDIDATE, near(0.91), policy(), reserve("approval_exhausted"),
              decided(Step.USER_OCCUPANCY, "NEED_APPROVAL", "APPROVAL_EXHAUSTED")), Occupancy.RENTAL, NAMES)
    assert [s.step for s in by_policy if s.result == "stop"] == [Step.POLICY]
    assert [s.step for s in by_reserve if s.result == "stop"] == [Step.USER_OCCUPANCY]
    assert by_policy[4].detail == "사용 횟수를 다 씀"
    assert by_reserve[5].detail == "비어 있음" and by_reserve[6].detail == "사용 횟수를 다 씀"


def test_FR12A_다시_판정한_요청은_앞_시도가_더_멀리_닿았어도_마지막_판정_단계에서_멈춘다():
    # 근접 단계 뒤에서 오류로 끝난 요청을 다시 판정했더니 비콘 단계에서 거부됐다. 앞 시도의 행은 남아 있다
    rows = trail(BEACON, CANDIDATE, near(0.91), ERROR, RETRY, NO_BEACON,
                 decided(Step.BEACON, "DENY", "BEACON_NOT_SEEN"))
    states = request_steps(request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None), rows, None, NAMES)
    assert [s.result for s in states] == [P, S, K, K, K, K, K]
    assert states[0].detail == "서버 오류로 끝난 요청을 다시 판정"
    assert states[1] == StepState(Step.BEACON, "stop", "비콘 수신 없음", at(6))  # 앞 시도의 ① 행(1)이 아니다
    assert proximity(rows) is None  # 앞 시도의 ③ 은 마지막 시도의 근접이 아니다


def test_FR12A_다시_판정하는_중인_요청은_앞_시도의_행을_닿은_단계로_세지_않는다():
    # 오류로 끝난 요청의 verdict 가 비워지고 새 시도가 막 시작됐다
    rows = trail(BEACON, CANDIDATE, near(0.91), ERROR, RETRY, BEACON)
    states = request_steps(request(None, None, None), rows, None, NAMES)
    assert [s.result for s in states] == [P, P, W, W, W, W, W]
    assert states[0] == StepState(Step.DEDUP, "pass", "서버 오류로 끝난 요청을 다시 판정", at(5))
    assert request_steps(request(None, None, None), rows[:5], None, NAMES)[1].result == W  # 재판정 행만 있다
    assert proximity(rows) is None
    # 오류가 두 번 났어도 마지막 재판정 뒤의 행만 센다
    twice = trail(BEACON, CANDIDATE, near(0.91), ERROR, RETRY, BEACON, CANDIDATE, near(0.9), ERROR, RETRY, BEACON)
    assert [s.result for s in request_steps(request(None, None, None), twice, None, NAMES)] == [P, P, W, W, W, W, W]
    assert proximity(twice) is None
    # 앞 시도가 판정 행까지 남겼어도(저장이 실패한 경우) 새 시도의 판정으로 본다
    saved_failed = trail(BEACON, CANDIDATE, near(0.91), policy(), decided(Step.POLICY, "OK", "OK"), ERROR, RETRY)
    assert [s.result for s in request_steps(request(None, None, None), saved_failed, None, NAMES)] == \
        [P, W, W, W, W, W, W]


def test_FR12A_any_대상은_요청한_기기의_정책_점유_행을_쓴다():
    # any: 는 ④ 를 후보마다 남기고 ⑤ 에 배정 행과 점유 행을 남긴다. 마지막 행이 다른 기기의 것이어도 배정된 기기의 행을 쓴다
    rows = trail(
        BEACON, CANDIDATE, near(0.8),
        policy("OK", "OK", "locker-2"), policy("NEED_APPROVAL", "APPROVAL_REQUIRED", "locker-1"),
        (Step.DEVICE_OCCUPANCY, {"allocation": {"allowed": ["locker-2"], "assigned": "locker-2"}}),
        reserve("reserved", "locker-2"), decided(Step.USER_OCCUPANCY, "OK", "OK"))
    states = request_steps(request(Verdict.OK, ReasonCode.OK, "locker-2"), rows, Occupancy.RENTAL, NAMES)
    assert states[4] == StepState(Step.POLICY, "pass", "정책 통과", at(4))
    assert states[5] == StepState(Step.DEVICE_OCCUPANCY, "pass", "비어 있음", at(7))
    # 기기가 정해지지 않으면(배정 못 받음) 마지막 행을 쓴다
    lost = trail(
        BEACON, CANDIDATE, near(0.8), policy("OK", "OK", "locker-2"),
        (Step.DEVICE_OCCUPANCY, {"allocation": {"allowed": ["locker-2"], "assigned": None}}),
        decided(Step.DEVICE_OCCUPANCY, "BUSY", "DEVICE_BUSY"))
    states = request_steps(request(Verdict.BUSY, ReasonCode.DEVICE_BUSY, None), lost, Occupancy.RENTAL, NAMES)
    assert states[5] == StepState(Step.DEVICE_OCCUPANCY, "stop", "배정 못 받음", at(5))


def test_FR12A_판정_뒤에_붙은_행은_단계를_바꾸지_않는다():
    # 같은 요청 번호를 다른 내용으로 다시 보내면 기존 요청에 ⓪ 충돌 행이 뒤늦게 붙는다. 처리 중 재전송도 같다
    base = request_steps(request(Verdict.OK, ReasonCode.OK), OK_RENTAL, Occupancy.RENTAL, NAMES)
    late = [
        DashboardAudit(seq=7, log_id="log-7", internal_request_id="i1", command_id=None, step=Step.DEDUP,
                       payload={"result": "REQUEST_ID_CONFLICT"}, created_at=at(7)),
        DashboardAudit(seq=8, log_id="log-8", internal_request_id="i1", command_id=None, step=Step.DEDUP,
                       payload={"result": "IN_PROGRESS"}, created_at=at(8)),
    ]
    assert request_steps(request(Verdict.OK, ReasonCode.OK), [*OK_RENTAL, *late], Occupancy.RENTAL, NAMES) == base
    stopped = trail(NO_BEACON, decided(Step.BEACON, "DENY", "BEACON_NOT_SEEN"))
    req = request(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN, None)
    assert request_steps(req, [*stopped, *late[:1]], None, NAMES) == request_steps(req, stopped, None, NAMES)


def test_FR12A_판정_뒤의_실행_행은_단계에_닿지_않는다():
    exec_rows = [
        DashboardAudit(seq=7, log_id="log-7", internal_request_id="i1", command_id="c1", step=Step.EXEC,
                       payload={"event": "record_result", "outcome": "succeeded", "evidence": {}},
                       created_at=at(7)),
        DashboardAudit(seq=8, log_id="log-8", internal_request_id="i1", command_id=None, step=Step.CLOSE,
                       payload={"event": "finish_close", "use_id": "u1", "check_id": "c1", "evidence": {}},
                       created_at=at(8)),
    ]
    req = request(Verdict.OK, ReasonCode.OK)
    assert request_steps(req, [*OK_RENTAL, *exec_rows], Occupancy.RENTAL, NAMES) == \
        request_steps(req, OK_RENTAL, Occupancy.RENTAL, NAMES)


def test_FR12A_판정_중에는_닿은_단계까지만_pass이고_앞_칸은_나아가도_그대로다():
    req = request(None, None, None)
    previous = request_steps(req, [], None, NAMES)
    for k in range(1, len(OK_RENTAL)):  # 행이 하나씩 쌓인다 (마지막 판정 행은 저장 전이라 아직 없다)
        current = request_steps(req, OK_RENTAL[:k], Occupancy.RENTAL, NAMES)
        for before, after in zip(previous, current):
            if before.result == "pass":
                assert after == before
        previous = current


# ── 도구, 근접, 응답 시각 ─────────────────────────────────────────────


def test_FR12A_도구는_첫_감사_행으로_가린다():
    assert request_tool([]) is None
    assert request_tool(trail(BEACON, CANDIDATE)) == "request_use"  # ① 부터
    assert request_tool(trail((Step.DEDUP, {"result": "REQUEST_ID_CONFLICT"}))) == "request_use"  # ⓪ 부터
    assert request_tool(trail(decided(Step.DEDUP, "DENY", "CAPACITY_EXCEEDED", capacity_exceeded=True))) == \
        "request_use"
    assert request_tool(trail(policy("NEED_APPROVAL", "APPROVAL_REQUIRED"))) == "request_access"  # ④ 만
    assert request_tool(trail(policy(), policy("OK", "OK", "locker-2"))) == "request_access"
    # 도구는 rowid 가 가장 작은 행으로 가린다. 넘긴 순서와 상관없다
    assert request_tool(list(reversed(trail(BEACON, CANDIDATE, near(0.9), policy())))) == "request_use"
    assert request_tool(list(reversed(trail(policy(), policy("OK", "OK", "locker-2"))))) == "request_access"


def test_FR12A_근접은_근접_행의_확률과_임계값이다():
    rows = trail(BEACON, CANDIDATE, near(0.91), policy())
    assert proximity(rows) == ({"locker-1": 0.91, "locker-2": 0.05}, 0.5)
    assert proximity(trail(BEACON, CANDIDATE)) is None
    assert proximity([]) is None
    # 정수로 저장된 값도 실수로 돌려준다
    ints = trail((Step.PROXIMITY, {"posteriors": {"locker-1": 1}, "threshold": 1, "best": ["locker-1", 1]}))
    posteriors, threshold = proximity(ints)
    assert posteriors == {"locker-1": 1.0} and threshold == 1.0
    assert all(isinstance(v, float) for v in posteriors.values()) and isinstance(threshold, float)


def test_FR12A_근접은_임계값이_없으면_None이다():
    no_threshold = trail((Step.PROXIMITY, {"posteriors": {"locker-1": 0.9}}))
    none_threshold = trail((Step.PROXIMITY, {"posteriors": {"locker-1": 0.9}, "threshold": None}))
    assert proximity(no_threshold) is None and proximity(none_threshold) is None


def test_FR12A_근접은_근접_단계의_상세_행만_본다():
    # ③ 의 판정 행에는 posteriors 가 없다. 다른 단계의 행에 posteriors 가 있어도 ③ 이 아니면 쓰지 않는다
    rows = trail(BEACON, CANDIDATE, near(0.58, threshold=0.75), decided(Step.PROXIMITY, "ASK", "LOW_CONFIDENCE"))
    assert proximity(rows) == ({"locker-1": 0.58, "locker-2": 0.05}, 0.75)
    other = trail((Step.POLICY, {"posteriors": {"locker-1": 0.9}, "threshold": 0.5}))
    assert proximity(other) is None
    # ③ 행이 여럿이면 마지막 행이다
    twice = trail(near(0.2), near(0.7))
    assert proximity(twice) == ({"locker-1": 0.7, "locker-2": 0.05}, 0.5)


def test_FR12A_응답_시각은_마지막_감사_행의_시각이다():
    rows = trail(BEACON, CANDIDATE, near(0.91))
    assert responded_at(rows) == at(3)
    assert responded_at(list(reversed(rows))) == at(3)  # 넘긴 순서가 아니라 rowid 로 고른다
    assert responded_at([]) is None
