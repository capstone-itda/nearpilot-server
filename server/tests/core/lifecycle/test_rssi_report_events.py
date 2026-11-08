"""rssi 보고를 근접 추정에 넘기는 상태 관리 연결 (FR-09, #252, specs/core-distance-selector/spec.md).

`NodeEventService.on_rssi_report` 는 비콘이 없는 빈 보고를 포함한 보고를 저장소에 쓰지 않고
근접 추정의 `observe_report` 로 넘긴다. 저장소와 근접 추정은 이 파일 안의 fake 다.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import inspect
from types import SimpleNamespace

import pytest

from nearpilot.core.lifecycle.node_events import NodeEventService
from nearpilot.shared.enums import DeviceState, Occupancy, TrustState
from nearpilot.shared.errors import StoreError
from nearpilot.shared.interfaces import NodeEventSink
from nearpilot.shared.models import NodeInfo, RssiReport

T0 = datetime(2026, 11, 8, 3, 0, 0, tzinfo=timezone.utc)
NODE = "light-1"


class Repository:
    """on_rssi_report 가 읽는 `nodes()` 만 있다. 그 밖의 메서드 호출은 모두 쓰기로 보고 기록한다."""

    def __init__(self, *nodes):
        self.items = nodes
        self.writes = []

    def nodes(self, capability=None):
        return self.items

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.writes.append((name, args, kwargs))

        return record


class Locks:
    """기기별 잠금 fake. 잠금을 잡고 있는 기기와 잡았던 순서를 기록한다."""

    def __init__(self):
        self.held = set()
        self.entered = []

    @contextmanager
    def __call__(self, node_id):
        self.entered.append(node_id)
        self.held.add(node_id)
        try:
            yield
        finally:
            self.held.discard(node_id)


class Proximity:
    """observe_report 를 부른 보고와 기기, 그리고 부를 때 그 기기의 잠금을 잡고 있었는지를 기록한다."""

    def __init__(self, locks):
        self.locks = locks
        self.calls = []
        self.error = None

    def observe_report(self, report, *, node):
        self.calls.append((report, node, node.node_id in self.locks.held))
        if self.error is not None:
            raise self.error


def make_node(**changes):
    fields = dict(node_id=NODE, place_id="place", kind="light", occupancy=Occupancy.SHARED,
                  trust_state=TrustState.TRUSTED, device_state=DeviceState.AVAILABLE,
                  exclusive_group=None, last_seen_at=T0, session_id="connection")
    return NodeInfo(**(fields | changes))


def make_report(**changes):
    fields = dict(node_id=NODE, session_id="connection", seq=1, frame_ts=T0, sent_at=T0,
                  received_at=T0 + timedelta(seconds=0.1))
    return RssiReport(**(fields | changes))


@pytest.fixture
def env():
    node = make_node()
    repository, locks = Repository(node), Locks()
    proximity = Proximity(locks)
    unused = SimpleNamespace()  # 이 경로가 쓰지 않는 협력자다. 쓰면 AttributeError 로 시험이 실패한다.
    sink = NodeEventService(
        repository=repository, store=unused, proximity=proximity, execution=unused,
        restore=unused, recovery=unused, shared_light=unused,
        context_for=lambda command_id: None, lock_for=locks)
    return SimpleNamespace(sink=sink, repository=repository, proximity=proximity, locks=locks, node=node)


def test_FR09_node_events_forward_rssi_report(env):
    report = make_report()
    env.sink.on_rssi_report(report)
    # 보고와 저장소의 기기 정보를 한 번 넘긴다.
    assert [(item, node) for item, node, _ in env.proximity.calls] == [(report, env.node)]
    # touch_node 와 save_rssi 를 포함해 저장소에 쓰지 않는다. 빈 보고도 기기의 last_seen_at 을 바꾸지 않는다.
    assert env.repository.writes == []

    # 현재 연결과 다른 session_id 의 보고는 IOT가 다시 인계하도록 StoreError 이고 넘기지 않는다.
    with pytest.raises(StoreError):
        env.sink.on_rssi_report(make_report(session_id="old-connection"))
    assert len(env.proximity.calls) == 1
    assert env.repository.writes == []


def test_FR09_node_events_rssi_report_unknown_node(env):
    with pytest.raises(StoreError):
        env.sink.on_rssi_report(make_report(node_id="ghost"))
    assert env.proximity.calls == []
    assert env.repository.writes == []


def test_FR09_node_events_rssi_report_retried_after_connection_recorded(env):
    """상태 보고가 연결을 기록하기 전의 보고는 거부하고, 기록한 뒤에 다시 인계하면 받는다."""
    env.repository.items = (make_node(session_id=None),)
    with pytest.raises(StoreError):
        env.sink.on_rssi_report(make_report())
    assert env.proximity.calls == []
    env.repository.items = (make_node(session_id="connection"),)
    env.sink.on_rssi_report(make_report())
    assert len(env.proximity.calls) == 1


def test_FR09_node_events_rssi_report_observe_error_propagates(env):
    """근접 추정이 실패하면 예외를 그대로 올린다. IOT가 같은 보고를 다시 인계한다."""
    env.proximity.error = RuntimeError("estimator down")
    with pytest.raises(RuntimeError, match="estimator down"):
        env.sink.on_rssi_report(make_report())
    env.proximity.error = None
    env.sink.on_rssi_report(make_report())
    assert [item for item, _, _ in env.proximity.calls] == [make_report(), make_report()]


def test_FR09_node_events_rssi_report_observes_under_node_lock(env):
    env.sink.on_rssi_report(make_report())
    assert env.locks.entered == [NODE]
    (_, _, held), = env.proximity.calls
    assert held  # observe_report 를 부를 때 그 기기의 잠금을 잡고 있었다
    assert env.locks.held == set()


def test_FR09_node_events_rssi_report_signature_matches_protocol():
    expected = inspect.signature(NodeEventSink.on_rssi_report)
    actual = inspect.signature(NodeEventService.on_rssi_report)
    assert list(actual.parameters) == list(expected.parameters) == ["self", "report"]
