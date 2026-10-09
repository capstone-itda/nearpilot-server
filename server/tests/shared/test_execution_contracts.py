"""#16 실행 인계와 #17 사건별 저장의 shared 계약 검사."""

from dataclasses import fields
from datetime import datetime

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action,
    Capability,
    CommandOutcome,
    EndReason,
    ExecutionStatus,
    ReasonCode,
    SessionState,
    StoreOutcome,
    TargetKind,
    Verdict,
)
from nearpilot.shared.errors import StoreError, StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.interfaces import (
    DecisionRechecker,
    ExecutionService,
    ExecutionStore,
    Repository,
)
from nearpilot.shared.models import (
    CommandRecord,
    Decision,
    ExecutionContext,
    ExecutionResult,
    StoreResult,
    TargetSpec,
    UseRequest,
    UseSession,
)

NOW = datetime(2026, 10, 9)
REQUEST = UseRequest(
    "u1", "req-1", TargetSpec(TargetKind.NEAREST, Capability.LOCKER), Action.OPEN, "claude", NOW
)
CONTEXT = ExecutionContext("ir-1", REQUEST, "locker-1", "use-1", None, Settings())


def test_fr18_execution_status_separates_progress_and_outcomes():
    assert {s.value for s in ExecutionStatus} == {
        "pending", "in_progress", "succeeded", "failed", "unknown", "not_sent",
    }


def test_fr19_store_outcome_values():
    assert {o.value for o in StoreOutcome} == {"applied", "already_applied", "conflict"}


def test_fr19_cancelled_end_reason_exists():
    assert EndReason.CANCELLED == "cancelled"


def test_fr19_store_errors_distinguish_not_applied_and_unknown():
    assert issubclass(StoreNotApplied, StoreError)
    assert issubclass(StoreOutcomeUnknown, StoreError)
    assert not issubclass(StoreNotApplied, StoreOutcomeUnknown)


def test_fr11_execution_result_has_no_user_fields():
    names = {f.name for f in fields(ExecutionResult)}
    assert not names & {"user_id", "occupant", "occupant_user_id"}


def test_fr18_context_keeps_fixed_target_and_settings():
    assert CONTEXT.node_id == "locker-1"
    assert CONTEXT.settings.version == Settings().version
    assert CONTEXT.request.user_id == "u1"


def test_fr20_use_session_rental_due_is_empty_before_success():
    session = UseSession("use-1", "ir-1", "locker-1", SessionState.RESERVED)
    assert session.rental_due_at is None
    assert session.closed_at is None


def test_fr18_fake_rechecker_and_service_satisfy_protocols():
    class FakeRechecker:
        def recheck(self, ctx):
            return Decision(ctx.internal_request_id, ctx.request.request_id,
                            Verdict.DENY, ReasonCode.RECHECK_FAILED)

    class FakeService:
        def execute(self, ctx):
            return ExecutionResult(ctx.internal_request_id, ExecutionStatus.PENDING)

    assert isinstance(FakeRechecker(), DecisionRechecker)
    assert isinstance(FakeService(), ExecutionService)


def test_fr19_fake_store_satisfies_protocol():
    record = CommandRecord("cmd-1", "ir-1", "locker-1", Action.OPEN, ExecutionStatus.PENDING)

    class FakeStore:
        def link_command(self, internal_request_id, node_id, action):
            return StoreResult(StoreOutcome.APPLIED, record)

        def begin_send(self, command_id):
            return StoreResult(StoreOutcome.APPLIED, record)

        def cancel_not_sent(self, internal_request_id, expected, reason, evidence):
            return StoreResult(StoreOutcome.APPLIED, record)

        def record_result(self, command_id, outcome, occurred_at, rental_due_at, evidence):
            return StoreResult(StoreOutcome.APPLIED, record)

        def isolate_device(self, node_id, evidence):
            return StoreResult(StoreOutcome.APPLIED)

        def begin_close(self, use_id, reason):
            return StoreResult(StoreOutcome.APPLIED)

        def finish_close(self, use_id, closed_at, evidence):
            return StoreResult(StoreOutcome.APPLIED)

        def execution(self, internal_request_id):
            return record

    assert isinstance(FakeStore(), ExecutionStore)


@pytest.mark.parametrize(
    "removed",
    ["create_command", "record_command_result", "transition_session",
     "transition_device", "settle_approval_usage"],
)
def test_fr19_repository_has_no_piecewise_state_writes(removed):
    assert not hasattr(Repository, removed)


def test_fr19_command_outcome_and_execution_status_share_final_values():
    finals = {CommandOutcome.SUCCEEDED, CommandOutcome.FAILED,
              CommandOutcome.UNKNOWN, CommandOutcome.NOT_SENT}
    assert {o.value for o in finals} <= {s.value for s in ExecutionStatus}
