"""#86 서버 내부 요청의 실행 문맥 계약 검사."""

import inspect
from datetime import datetime

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, Capability, ExecutionKind, InternalRequestCause, TargetKind,
)
from nearpilot.shared.interfaces import ExecutionStore
from nearpilot.shared.models import ExecutionContext, InternalRequest, TargetSpec, UseRequest

NOW = datetime(2026, 10, 15)
INTERNAL = InternalRequest("ir-off", "light-1", Action.OFF, InternalRequestCause.LEAVE_OFF, Settings(), NOW)


def test_FR20_internal_request_has_no_user():
    assert not {"user_id", "request_id", "host_kind"} & set(InternalRequest.__dataclass_fields__)


def test_FR20_server_internal_context_carries_internal_request_only():
    ctx = ExecutionContext("ir-off", None, "light-1", None, None, Settings(),
                           internal_request=INTERNAL, execution_kind=ExecutionKind.SERVER_INTERNAL)
    assert ctx.request is None and ctx.internal_request.action == Action.OFF


@pytest.mark.parametrize("changes", [
    {"request": UseRequest("u1", "r1", TargetSpec(TargetKind.NEAREST, Capability.LIGHT),
                           Action.OFF, None, NOW)},
    {"internal_request": None},
    {"usage_id": "usage-1"},
    {"use_id": "use-1"},
    {"node_id": "light-2"},
])
def test_FR20_server_internal_context_rejects_visitor_links(changes):
    fields = dict(internal_request_id="ir-off", request=None, node_id="light-1", use_id=None,
                  usage_id=None, settings=Settings(), internal_request=INTERNAL,
                  execution_kind=ExecutionKind.SERVER_INTERNAL)
    fields.update(changes)
    with pytest.raises(ValueError):
        ExecutionContext(**fields)


def test_FR20_visitor_context_cannot_carry_internal_request():
    req = UseRequest("u1", "r1", TargetSpec(TargetKind.NEAREST, Capability.LIGHT), Action.ON, None, NOW)
    with pytest.raises(ValueError):
        ExecutionContext("ir-off", req, "light-1", None, None, Settings(),
                         internal_request=INTERNAL, execution_kind=ExecutionKind.SHARED)


def test_FR20_store_links_internal_request():
    assert list(inspect.signature(ExecutionStore.link_internal_request).parameters) == ["self", "req"]
