"""#82 기기별 실행 가능 여부와 #83 명령 ID 의 실행 문맥 조회 계약 검사."""

import inspect

from nearpilot.shared.interfaces import ExecutionReadiness, ExecutionStore
from nearpilot.shared.models import ExecutionContext


def test_FR19_execution_readiness_is_per_node_bool():
    class RestoreGate:
        def __init__(self):
            self.restored = {"locker-1"}

        def can_execute(self, node_id):
            return node_id in self.restored

    gate = RestoreGate()
    assert isinstance(gate, ExecutionReadiness)
    assert gate.can_execute("locker-1") and not gate.can_execute("locker-2")
    assert list(inspect.signature(ExecutionReadiness.can_execute).parameters) == ["self", "node_id"]


def test_FR07_execution_context_is_looked_up_by_command_id():
    signature = inspect.signature(ExecutionStore.execution_context)
    assert list(signature.parameters) == ["self", "command_id"]
    assert signature.return_annotation in ("ExecutionContext | None", ExecutionContext | None)

