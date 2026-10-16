"""FR-22 실제 DB에서 한도 이상의 새 조작과 승인 신청을 용량 초과로 거부한다."""

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


def test_FR18_FR22_full_capacity_denial_is_saved_without_reservation(capacity_world):
    world, repo, _, pipeline = capacity_world
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


def test_FR22_full_capacity_denies_access_request_without_approval(capacity_world):
    world, repo, access, _ = capacity_world
    fill(world, repo)
    decision = access.request_access(VISITOR_A, "locker-2", Action.OPEN, "approval-1")
    assert (decision.verdict, decision.reason) == (Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED)
    assert decision.request_id == "approval-1"
    assert world.count("SELECT COUNT(*) FROM approval") == 0


def test_FR18_FR22_full_capacity_keeps_existing_replay(capacity_world):
    world, repo, _, pipeline = capacity_world
    first, _ = pipeline.decide_for_execution(world.request(VISITOR_A, request_id="first"))
    assert first.reason is not ReasonCode.CAPACITY_EXCEEDED
    again, context = pipeline.decide_for_execution(world.request(VISITOR_A, request_id="first"))
    assert (again.internal_request_id, again.verdict, again.reason) == (
        first.internal_request_id, first.verdict, first.reason)
    assert context is None
