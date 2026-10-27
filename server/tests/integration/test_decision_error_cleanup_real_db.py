"""FR-19 예약 뒤 판정 오류의 예약 정리(#80)와 실제 DB(SqliteRepository, SqliteExecutionStore)의 통합 시나리오.

판정이 예약 뒤 오류로 끝나면 이번 요청의 예약을 바로 취소해서 기기를 다른 방문자가 쓸 수 있는지 확인한다.
그 취소마저 실패하면 재시작 복원이 남은 예약을 대기 없이 취소하는지도 확인한다.
취소한 요청을 같은 request_id로 다시 판정하면(#76) 새로 예약하고 명령 연결까지 가는지도 확인한다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from threading import RLock

import pytest

from _world import NOW, VISITOR_A, VISITOR_B, FakeAudit, FakeReadiness, World
from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.core.lifecycle.restore import RestoreService
from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.shared.enums import ExecutionKind, ReasonCode, Step, StoreOutcome, Verdict
from nearpilot.shared.errors import StoreNotApplied


class FailingAudit(FakeAudit):
    """예약을 부른 뒤 단계(⑤)의 감사 기록에서 예외를 던진다."""

    def append(self, event):
        if event.step is Step.DEVICE_OCCUPANCY:
            raise RuntimeError("감사 저장 장애")
        super().append(event)


class FailOnceAudit(FakeAudit):
    """예약을 부른 뒤 단계(⑤)의 감사 기록에서 처음 한 번만 예외를 던진다. 일시 장애다."""

    def __init__(self):
        super().__init__()
        self.failed = False

    def append(self, event):
        if event.step is Step.DEVICE_OCCUPANCY and not self.failed:
            self.failed = True
            raise RuntimeError("감사 저장 장애")
        super().append(event)


class CancelFailingStore(SqliteExecutionStore):
    """미전송 취소가 실패하는 저장소. 남은 예약은 재시작 복원이 정리한다."""

    def cancel_not_sent(self, *args, **kwargs):
        raise StoreNotApplied("취소 실패")


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


def _pipeline(world, store):
    return DecisionPipeline(
        world.repo, world.prox, world.policy, FailingAudit(), world.settings, FakeReadiness(), store=store)


def _restore_service(world):
    """예약 시각에서 1초 뒤(예약 대기 제한 안)에 서버가 다시 시작했다."""
    clock = lambda: NOW + timedelta(seconds=1)  # noqa: E731
    settings = lambda: world.settings  # noqa: E731
    lock_for = defaultdict(RLock).__getitem__
    store = SqliteExecutionStore(world.conn)
    return RestoreService(
        repository=world.repo, store=store, settings=settings, clock=clock, lock_for=lock_for,
        release=ReleaseService(repository=world.repo, store=store, settings=settings, clock=clock,
                               lock_for=lock_for),
        recovery=FaultRecoveryService(repository=world.repo, store=store, clock=clock, lock_for=lock_for),
        shared_light=SharedLightService(repository=world.repo, store=store, policy=world.policy,
                                        settings=settings, clock=clock, lock_for=lock_for))


def test_FR19_runtime_decision_error_releases_reservation(world):
    pipeline = _pipeline(world, SqliteExecutionStore(world.conn))

    d = pipeline.decide(world.request(VISITOR_A))

    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    saved = world.conn.execute("SELECT verdict, reason_code, node_id FROM request").fetchone()
    assert tuple(saved) == ("DENY", "INTERNAL_ERROR", None)  # 오류 판정은 대상 기기를 비워 저장한다
    session = world.conn.execute("SELECT state, end_reason FROM use_session").fetchone()
    assert tuple(session) == ("CLOSED", "cancelled")
    assert world.count("SELECT COUNT(*) FROM node WHERE node_id = 'locker-1' AND device_state = 'AVAILABLE'") == 1
    other = world.pipeline.decide(world.request(VISITOR_B, request_id="r2"))
    assert (other.verdict, other.reason) == (Verdict.OK, ReasonCode.OK)


def test_FR19_runtime_failed_cancel_reservation_is_cleaned_by_restore(world):
    pipeline = _pipeline(world, CancelFailingStore(world.conn))
    d = pipeline.decide(world.request(VISITOR_A))
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert world.count("SELECT COUNT(*) FROM use_session WHERE state = 'RESERVED'") == 1  # 취소가 실패했다
    busy = world.pipeline.decide(world.request(VISITOR_B, request_id="r2"))
    assert (busy.verdict, busy.reason) == (Verdict.BUSY, ReasonCode.DEVICE_BUSY)

    _restore_service(world).restore()

    session = world.conn.execute("SELECT state, end_reason FROM use_session").fetchone()
    assert tuple(session) == ("CLOSED", "cancelled")
    audit = [json.loads(p) for (p,) in world.conn.execute(
        "SELECT payload_json FROM audit_log WHERE internal_request_id = ?", (d.internal_request_id,))]
    assert [(a["event"], a["evidence"]["source"]) for a in audit] == [
        ("cancel_not_sent", "restore_failed_request")]
    other = world.pipeline.decide(world.request(VISITOR_B, request_id="r3"))
    assert (other.verdict, other.reason) == (Verdict.OK, ReasonCode.OK)


def test_FR18_runtime_retry_after_cancelled_reservation_executes(world):
    store = SqliteExecutionStore(world.conn)
    pipeline = DecisionPipeline(
        world.repo, world.prox, world.policy, FailOnceAudit(), world.settings, FakeReadiness(), store=store)
    request = world.request(VISITOR_A)

    first = pipeline.decide(request)

    assert (first.verdict, first.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    cancelled = world.conn.execute("SELECT use_id, state, end_reason FROM use_session").fetchone()
    assert (cancelled["state"], cancelled["end_reason"]) == ("CLOSED", "cancelled")

    retry, context = pipeline.decide_for_execution(request)

    assert (retry.verdict, retry.reason) == (Verdict.OK, ReasonCode.OK)
    assert retry.internal_request_id == first.internal_request_id
    assert retry.use_id not in (None, cancelled["use_id"])
    assert context is not None
    assert (context.execution_kind, context.use_id) == (ExecutionKind.RENTAL_INITIAL, retry.use_id)
    sessions = {r["use_id"]: (r["state"], r["end_reason"]) for r in world.conn.execute("SELECT * FROM use_session")}
    assert sessions == {cancelled["use_id"]: ("CLOSED", "cancelled"), retry.use_id: ("RESERVED", None)}
    assert world.count("SELECT COUNT(*) FROM node WHERE node_id = 'locker-1' AND device_state = 'RESERVED'") == 1

    linked = store.link_command(retry.internal_request_id, retry.node_id, request.action)

    assert linked.outcome is StoreOutcome.APPLIED
    assert linked.command.execution_kind is ExecutionKind.RENTAL_INITIAL
    assert linked.command.use_id == retry.use_id
