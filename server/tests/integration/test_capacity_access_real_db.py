"""FR-03·FR-22 한도에서도 실제 승인 신청의 외부 키·재전송을 유지한다."""

from dataclasses import replace

import pytest

from _world import NOW, SETTINGS, VISITOR_A, World, FakeReadiness
from nearpilot.core.decision.access import AccessRequestService
from nearpilot.core.decision.digest import input_digest
from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.core.policy.checker import RepositoryPolicyChecker
from nearpilot.db.audit import SqliteAuditLog
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.shared.enums import Action, ReasonCode, Verdict


@pytest.fixture
def capacity_world():
    world = World()
    settings = replace(SETTINGS, dedup_entry_limit=1)
    repo = SqliteRepository(world.conn, settings)
    policy = RepositoryPolicyChecker(repo)
    audit = SqliteAuditLog(world.conn)
    access = AccessRequestService(repo, policy, audit, settings, lambda: NOW)
    pipeline = DecisionPipeline(repo, world.prox, policy, audit, settings, FakeReadiness())
    try:
        yield world, repo, access, pipeline
    finally:
        world.close()


def fill(world, repo):
    req = world.request(VISITOR_A, request_id="unfinished")
    repo.link_request(req, input_digest(req))


def test_FR03_FR22_full_capacity_keeps_access_replay(capacity_world):
    world, repo, access, _ = capacity_world
    fill(world, repo)
    first = access.request_access(VISITOR_A, "locker-2", Action.OPEN, "approval-1")
    assert first.verdict is Verdict.NEED_APPROVAL
    before_requests = world.count("SELECT COUNT(*) FROM request")
    before_audit = world.count("SELECT COUNT(*) FROM audit_log")
    again = access.request_access(VISITOR_A, "locker-2", Action.OPEN, "approval-1")
    assert (again.internal_request_id, again.verdict, again.reason) == (
        first.internal_request_id, first.verdict, first.reason)
    row = world.conn.execute("SELECT request_id,purpose FROM request WHERE internal_request_id=?",
                             (first.internal_request_id,)).fetchone()
    assert tuple(row) == ("approval-1", "access")
    assert world.count("SELECT COUNT(*) FROM approval") == 1
    assert world.count("SELECT COUNT(*) FROM request") == before_requests
    assert world.count("SELECT COUNT(*) FROM audit_log") == before_audit


def test_FR18_FR22_full_capacity_denial_is_saved_without_reservation(capacity_world):
    world, repo, access, pipeline = capacity_world
    fill(world, repo)
    decision, context = pipeline.decide_for_execution(world.request(VISITOR_A, request_id="new-use"))
    assert (decision.verdict, decision.reason) == (Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED)
    assert decision.request_id == "new-use" and context is None
    row = world.conn.execute(
        "SELECT request_id,verdict,reason_code,model FROM request WHERE internal_request_id=?",
        (decision.internal_request_id,)).fetchone()
    assert tuple(row) == (None, "DENY", "CAPACITY_EXCEEDED", None)
    assert world.count("SELECT COUNT(*) FROM audit_log WHERE internal_request_id=? AND step='0'",
                       decision.internal_request_id) == 1
    assert world.count("SELECT COUNT(*) FROM use_session") == 0
    assert world.count("SELECT COUNT(*) FROM node_command") == 0
    assert access.request_access(VISITOR_A, "locker-2", Action.OPEN, "approval-after").verdict is Verdict.NEED_APPROVAL


def test_FR18_cross_tool_external_key_cannot_replay_as_use(capacity_world):
    world, _, access, pipeline = capacity_world
    first = access.request_access(VISITOR_A, "locker-2", Action.OPEN, "same")
    decision, context = pipeline.decide_for_execution(
        world.request(VISITOR_A, target="locker-2", request_id="same"))
    assert decision.reason is ReasonCode.REQUEST_ID_CONFLICT
    assert decision.internal_request_id == first.internal_request_id
    assert context is None
    assert world.count("SELECT COUNT(*) FROM use_session") == 0
