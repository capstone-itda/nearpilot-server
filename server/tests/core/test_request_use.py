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


def test_FR09_재전송인데_실행_기록이_없으면_INTERNAL_ERROR():
    svc, pipe, ex, store = build(OK, None, rec=None)
    d = svc.request_use(REQ)
    assert (d.verdict, d.reason, d.use_id) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR, None)
    assert ex.contexts == []


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
