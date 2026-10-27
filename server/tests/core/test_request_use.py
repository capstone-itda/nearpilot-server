"""FR-09 request_use 조립. 판정, 실행 인계, 저장소는 fake 로 대신한다."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nearpilot.core.decision.service import RequestUseService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action,
    ExecutionKind,
    ExecutionStatus,
    ReasonCode,
    Verdict,
)
from nearpilot.shared.models import (
    CommandRecord,
    Decision,
    ExecutionContext,
    ExecutionResult,
    TargetSpec,
    UseRequest,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
REQ = UseRequest("u1", "r1", TargetSpec.parse("nearest:locker"), Action.OPEN, "claude", NOW)
OK = Decision("i1", "r1", Verdict.OK, ReasonCode.OK, node_id="locker-1", use_id="use-1", p_max=0.97)
CTX = ExecutionContext("i1", REQ, "locker-1", "use-1", None, Settings(version="v1"))


class FakePipeline:
    def __init__(self, decision=OK, ctx=CTX):
        self.decision, self.ctx = decision, ctx
        self.calls = 0

    def decide_for_execution(self, req):
        self.calls += 1
        return self.decision, self.ctx


class FakeExecution:
    def __init__(self, result=None):
        self.result = result or ExecutionResult("i1", ExecutionStatus.SUCCEEDED, "c1", "use-1")
        self.contexts: list[ExecutionContext] = []
        self.error: Exception | None = None

    def execute(self, ctx):
        self.contexts.append(ctx)
        if self.error:
            raise self.error
        return self.result


class FakeStore:
    def __init__(self, record=None):
        self.record = record
        self.calls: list[str] = []
        self.error: Exception | None = None

    def execution(self, internal_request_id):
        self.calls.append(internal_request_id)
        if self.error:
            raise self.error
        return self.record


def record(status, use_id="use-1") -> CommandRecord:
    return CommandRecord("c1", "i1", "locker-1", Action.OPEN, status, use_id=use_id)


def build(decision=OK, ctx=CTX, result=None, rec=None):
    pipe, ex, store = FakePipeline(decision, ctx), FakeExecution(result), FakeStore(rec)
    return RequestUseService(pipe, ex, store), pipe, ex, store


def test_FR09_새_OK는_context로_인계한다():
    svc, pipe, ex, store = build()
    svc.request_use(REQ)
    assert ex.contexts == [CTX] and store.calls == []


def test_FR09_실행_성공이면_OK와_use_id를_반환한다():
    svc, *_ = build()
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id, d.node_id) == (Verdict.OK, ReasonCode.OK, "use-1", "locker-1")
    assert (d.internal_request_id, d.request_id, d.p_max) == ("i1", "r1", 0.97)


def test_FR09_공용형_실행_성공은_use_id가_None이다():
    shared = Decision("i1", "r1", Verdict.OK, ReasonCode.OK, node_id="light-1")
    ctx = ExecutionContext("i1", REQ, "light-1", None, None, Settings(), ExecutionKind.SHARED)
    svc, *_ = build(shared, ctx, ExecutionResult("i1", ExecutionStatus.SUCCEEDED, "c1", None))
    d = svc.request_use(REQ)
    assert (d.verdict, d.use_id) == (Verdict.OK, None)


@pytest.mark.parametrize("verdict,reason", [
    (Verdict.ASK, ReasonCode.LOW_CONFIDENCE),
    (Verdict.DENY, ReasonCode.BEACON_NOT_SEEN),
    (Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING),
])
def test_FR09_OK가_아닌_판정은_그대로_반환하고_인계하지_않는다(verdict, reason):
    denied = Decision("i1", "r1", verdict, reason)
    svc, pipe, ex, store = build(denied, None)
    assert svc.request_use(REQ) is denied
    assert ex.contexts == [] and store.calls == []


def test_FR09_재검사가_막으면_그_판정을_반환한다():
    blocked = Decision("i1", "r1", Verdict.ASK, ReasonCode.MOVE_CLOSER, node_id="locker-1", p_max=0.4)
    svc, *_ = build(result=ExecutionResult("i1", ExecutionStatus.NOT_SENT, None, None, blocked))
    assert svc.request_use(REQ) is blocked


@pytest.mark.parametrize("status,verdict,reason", [
    (ExecutionStatus.REJECTED, Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    (ExecutionStatus.FAILED, Verdict.DENY, ReasonCode.EXEC_FAILED),
    (ExecutionStatus.UNKNOWN, Verdict.DENY, ReasonCode.EXEC_UNKNOWN),
    (ExecutionStatus.PENDING, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    (ExecutionStatus.IN_PROGRESS, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
])
def test_FR09_실행_결과별_판정(status, verdict, reason):
    svc, *_ = build(result=ExecutionResult("i1", status, "c1", None))
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id, d.node_id) == (verdict, reason, None, "locker-1")


def test_FR18_FR09_실행_성공_전에는_use_id를_반환하지_않는다():
    for status in ExecutionStatus:
        if status is ExecutionStatus.SUCCEEDED:
            continue
        svc, *_ = build(result=ExecutionResult("i1", status, "c1", "use-1"))
        assert svc.request_use(REQ).use_id is None


def test_FR09_사유를_모르는_NOT_SENT는_INTERNAL_ERROR():
    svc, *_ = build(result=ExecutionResult("i1", ExecutionStatus.NOT_SENT, None, None))
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR, None)


def test_FR09_인계가_예외를_내면_INTERNAL_ERROR():
    svc, pipe, ex, store = build()
    ex.error = RuntimeError("secret internal detail")
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR, None)
    assert "secret" not in repr(d)


@pytest.mark.parametrize("status,verdict,reason,use_id", [
    (ExecutionStatus.SUCCEEDED, Verdict.OK, ReasonCode.OK, "use-1"),
    (ExecutionStatus.FAILED, Verdict.DENY, ReasonCode.EXEC_FAILED, None),
    (ExecutionStatus.UNKNOWN, Verdict.DENY, ReasonCode.EXEC_UNKNOWN, None),
    (ExecutionStatus.IN_PROGRESS, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS, None),
    (ExecutionStatus.NOT_SENT, Verdict.DENY, ReasonCode.INTERNAL_ERROR, None),
])
def test_FR09_재전송은_저장된_실행_상태로_반환한다(status, verdict, reason, use_id):
    svc, pipe, ex, store = build(OK, None, rec=record(status))
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id) == (verdict, reason, use_id)
    assert ex.contexts == [] and store.calls == ["i1"]  # 새 명령을 만들지 않는다


def test_FR18_재전송인데_실행_기록이_없으면_REQUEST_IN_PROGRESS():
    svc, pipe, ex, store = build(OK, None, rec=None)
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id) == (Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS, None)
    assert ex.contexts == []  # 인계가 끝나지 않은 요청이라 다시 인계하지 않는다


def test_FR09_재전송된_거부_판정은_그대로_반환한다():
    denied = Decision("i1", "r1", Verdict.DENY, ReasonCode.POLICY_DENIED)
    svc, pipe, ex, store = build(denied, None, rec=record(ExecutionStatus.SUCCEEDED))
    assert svc.request_use(REQ) is denied and store.calls == []


def test_FR09_실행_기록_조회가_실패하면_INTERNAL_ERROR():
    svc, pipe, ex, store = build(OK, None)
    store.error = RuntimeError("secret internal detail")
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert "secret" not in repr(d)


def test_FR09_같은_실행_결과에는_같은_판정을_반환한다():
    assert build()[0].request_use(REQ) == build()[0].request_use(REQ)


def test_FR11_request_use_Decision에는_사용자_ID가_없다():
    for svc in (build()[0], build(OK, None, rec=record(ExecutionStatus.SUCCEEDED))[0]):
        assert "u1" not in repr(svc.request_use(REQ))


# ── 재검사 밖의 중단 사유 (#78, #79) ────────────────────────────────


def stopped(reason, status=ExecutionStatus.NOT_SENT, recheck=None):
    return ExecutionResult("i1", status, None, None, recheck, reason)


@pytest.mark.parametrize("reason,verdict,host_reason", [
    (ReasonCode.COMMAND_IN_PROGRESS, Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS),
    (ReasonCode.DEVICE_BUSY, Verdict.BUSY, ReasonCode.DEVICE_BUSY),
    (ReasonCode.TARGET_UNAVAILABLE, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    (ReasonCode.NODE_NOT_CONNECTED, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    (ReasonCode.RESERVATION_EXPIRED, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
    (ReasonCode.COMMAND_NOT_SENT, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE),
])
def test_FR19_중단_사유별_호스트_판정(reason, verdict, host_reason):
    svc, *_ = build(result=stopped(reason))
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id, d.node_id) == (verdict, host_reason, None, "locker-1")


def test_FR18_재검사_판정이_있으면_먼저_쓴다():
    blocked = Decision("i1", "r1", Verdict.ASK, ReasonCode.MOVE_CLOSER, node_id="locker-1", p_max=0.4)
    svc, *_ = build(result=stopped(None, recheck=blocked))
    assert svc.request_use(REQ) is blocked


def test_FR18_알_수_없는_중단_사유는_거부한다():
    svc, *_ = build(result=stopped(ReasonCode.BEACON_NOT_SEEN))
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)


def test_FR18_사유가_없는_미전송은_거부한다():
    svc, *_ = build(result=stopped(None))
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)


@pytest.mark.parametrize("reason", [
    ReasonCode.NODE_NOT_CONNECTED, ReasonCode.RESERVATION_EXPIRED, ReasonCode.COMMAND_NOT_SENT])
def test_FR11_중단_사유의_내부_원인을_공개하지_않는다(reason):
    svc, *_ = build(result=stopped(reason))
    d = svc.request_use(REQ)
    assert reason.value not in repr(d) and d.reason is ReasonCode.TARGET_UNAVAILABLE


# ── 재전송된 NOT_SENT 는 취소 근거로 첫 응답을 다시 만든다 ──────────────────


def not_sent(evidence) -> CommandRecord:
    return CommandRecord("c1", "i1", "locker-1", Action.OPEN, ExecutionStatus.NOT_SENT,
                         use_id="use-1", evidence=evidence)


RECHECK_ASK = {"source": "recheck", "verdict": "ASK", "reason": "MOVE_CLOSER",
               "confirmed_at": "2026-01-01T12:00:05+00:00"}
RECHECK_APPROVAL = {"source": "recheck", "verdict": "NEED_APPROVAL", "reason": "APPROVAL_REQUIRED",
                    "confirmed_at": "2026-01-01T12:00:05+00:00"}


@pytest.mark.parametrize("evidence,verdict,reason", [
    pytest.param(RECHECK_ASK, Verdict.ASK, ReasonCode.MOVE_CLOSER, id="재검사 ASK"),
    pytest.param(RECHECK_APPROVAL, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, id="재검사 승인 필요"),
    pytest.param({"source": "no_connection", "reason": "NODE_NOT_CONNECTED"},
                 Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, id="중단 사유 연결 없음"),
    pytest.param({"source": "reservation_expired", "reason": "RESERVATION_EXPIRED"},
                 Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, id="중단 사유 예약 만료"),
    pytest.param({"source": "begin_send_conflict", "reason": "COMMAND_IN_PROGRESS"},
                 Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS, id="중단 사유 다른 명령 진행 중"),
    pytest.param({}, Verdict.DENY, ReasonCode.INTERNAL_ERROR, id="근거 없음"),
    pytest.param({**RECHECK_ASK, "verdict": "NOPE"}, Verdict.DENY, ReasonCode.INTERNAL_ERROR,
                 id="재검사 verdict 가 열거형 밖"),
    pytest.param({**RECHECK_ASK, "reason": "NOPE"}, Verdict.DENY, ReasonCode.INTERNAL_ERROR,
                 id="재검사 reason 이 열거형 밖"),
    pytest.param({**RECHECK_ASK, "verdict": "OK", "reason": "OK"}, Verdict.DENY, ReasonCode.INTERNAL_ERROR,
                 id="재검사 근거가 OK 여도 OK 가 되지 않는다"),
    pytest.param({"source": "no_connection", "reason": "BEACON_NOT_SEEN"},
                 Verdict.DENY, ReasonCode.INTERNAL_ERROR, id="중단 사유 표에 없는 사유"),
    pytest.param({"source": "no_connection", "reason": ["NODE_NOT_CONNECTED"]},
                 Verdict.DENY, ReasonCode.INTERNAL_ERROR, id="사유 값이 문자열이 아님"),
])
def test_FR18_재전송된_NOT_SENT는_첫_응답과_같은_판정을_준다(evidence, verdict, reason):
    svc, pipe, ex, store = build(OK, None, rec=not_sent(evidence))
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id, d.node_id) == (verdict, reason, None, "locker-1")
    assert (d.internal_request_id, d.request_id, d.p_max) == ("i1", "r1", 0.97)  # 최초 판정의 p_max 를 유지한다
    assert ex.contexts == [] and store.calls == ["i1"]  # 새 명령을 만들지 않는다


@pytest.mark.parametrize("result,evidence", [
    pytest.param(
        stopped(None, recheck=Decision("i1", "r1", Verdict.ASK, ReasonCode.MOVE_CLOSER,
                                       node_id="locker-1", p_max=0.4)),
        RECHECK_ASK, id="재검사 거부"),
    pytest.param(stopped(ReasonCode.NODE_NOT_CONNECTED),
                 {"source": "no_connection", "reason": "NODE_NOT_CONNECTED"}, id="연결 없음"),
    pytest.param(stopped(ReasonCode.COMMAND_IN_PROGRESS),
                 {"source": "begin_send_conflict", "reason": "COMMAND_IN_PROGRESS"}, id="다른 명령 진행 중"),
])
def test_FR18_재전송은_첫_호출과_같은_판정을_준다(result, evidence):
    first = build(result=result)[0].request_use(REQ)
    again = build(OK, None, rec=not_sent(evidence))[0].request_use(REQ)
    assert (again.verdict, again.reason, again.node_id, again.use_id) == \
        (first.verdict, first.reason, first.node_id, first.use_id)
