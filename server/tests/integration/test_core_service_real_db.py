"""FR-09 `build_core_service` 로 만든 CORE 서비스를 실제 SQLite 위에서 부른다.

근접 추정, 감사, 실행은 fake 다. 반납 서비스는 상태 관리가 만들어 넘기므로 fake 로 대신한다.
"""

from __future__ import annotations

from _world import NOW, OWNER, VISITOR_A, FakeReadiness, World
from nearpilot.core.decision.core_service import build_core_service
from nearpilot.shared.enums import Action, ExecutionStatus, ReasonCode, Verdict
from nearpilot.shared.interfaces import CoreService
from nearpilot.shared.models import Decision, ExecutionResult


class FakeExecution:
    def __init__(self):
        self.contexts = []

    def execute(self, ctx):
        self.contexts.append(ctx)
        return ExecutionResult(ctx.internal_request_id, ExecutionStatus.SUCCEEDED, "c1", ctx.use_id)


class FakeStore:
    def execution_by_request(self, internal_request_id):
        return None


class FakeRelease:
    def __init__(self):
        self.calls = []

    def release_use(self, user_id, use_id):
        self.calls.append(("release_use", user_id, use_id))
        return Decision("", "", Verdict.DENY, ReasonCode.USE_NOT_FOUND)

    def get_use_status(self, user_id, use_id):
        self.calls.append(("get_use_status", user_id, use_id))
        return {"reason_code": "USE_NOT_FOUND"}


def build(world: World):
    execution, release = FakeExecution(), FakeRelease()
    svc = build_core_service(
        repo=world.repo, proximity=world.prox, policy=world.policy, audit=world.audit,
        settings=world.settings, clock=lambda: NOW, readiness=FakeReadiness(),
        execution=execution, store=FakeStore(), release=release)
    return svc, execution, release


def test_FR09_core_service_built_on_real_db_serves_six_methods():
    world = World()
    try:
        svc, execution, release = build(world)
        assert isinstance(svc, CoreService)
        assert "locker-1" in {x["node_id"] for x in svc.find_available_devices(VISITOR_A)}

        d = svc.request_use(world.request(VISITOR_A, "locker-1"))
        assert (d.verdict, d.reason, d.node_id) == (Verdict.OK, ReasonCode.OK, "locker-1")
        assert d.use_id and [c.node_id for c in execution.contexts] == ["locker-1"]

        a = svc.request_access(VISITOR_A, "locker-2", Action.OPEN, "acc-1")
        assert (a.verdict, a.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING)

        assert svc.get_device_state(OWNER, "locker-4")["node_id"] == "locker-4"

        assert svc.release_use(VISITOR_A, "use-x").reason is ReasonCode.USE_NOT_FOUND
        assert svc.get_use_status(VISITOR_A, "use-x") == {"reason_code": "USE_NOT_FOUND"}
        assert release.calls == [("release_use", VISITOR_A, "use-x"), ("get_use_status", VISITOR_A, "use-x")]
    finally:
        world.close()
