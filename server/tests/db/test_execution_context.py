"""`ExecutionStore.execution_context` (specs/db-contract-followups/spec.md, #83)."""

from __future__ import annotations

from datetime import datetime, timezone

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, ExecutionKind, ReasonCode, Verdict
from nearpilot.shared.models import Decision, DecisionRecord, TargetSpec, UseRequest

from conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _use_request(user_id="u1", request_id="ext-1"):
    return UseRequest(
        user_id=user_id, request_id=request_id, target=TargetSpec.parse("nearest:locker"),
        action=Action.OPEN, host_kind=None, received_at=NOW,
    )


def test_FR07_execution_context_found(conn, repo, store):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.OK, reason=ReasonCode.OK, node_id="node1",
    )
    settings = Settings(version="7")
    repo.save_decision(decision, DecisionRecord(settings=settings))
    insert_use_session(conn, use_id="use1", internal_request_id=link.internal_request_id, state="RESERVED")
    command = store.link_command(link.internal_request_id, "node1", Action.OPEN).command

    ctx = store.execution_context(command.command_id)

    assert ctx is not None
    assert ctx.internal_request_id == link.internal_request_id
    assert ctx.request is not None
    assert ctx.request.user_id == "u1"
    assert ctx.request.request_id == "ext-1"
    assert ctx.node_id == "node1"
    assert ctx.use_id == "use1"
    assert ctx.execution_kind == ExecutionKind.RENTAL_INITIAL
    assert ctx.settings == settings


def test_FR07_execution_context_uses_saved_settings(conn, repo, store):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.OK, reason=ReasonCode.OK, node_id="node1",
    )
    saved_settings = Settings(version="old", threshold_locker=0.6)
    repo.save_decision(decision, DecisionRecord(settings=saved_settings))
    insert_use_session(conn, use_id="use1", internal_request_id=link.internal_request_id, state="RESERVED")
    command = store.link_command(link.internal_request_id, "node1", Action.OPEN).command

    current_settings = Settings(version="new", threshold_locker=0.99)
    assert current_settings != saved_settings

    ctx = store.execution_context(command.command_id)

    assert ctx is not None
    assert ctx.settings == saved_settings


def test_FR07_execution_context_none(store):
    assert store.execution_context("no-such-command") is None
