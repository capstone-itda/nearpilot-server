"""묶음 요청 판정과 실행 시험이 함께 쓰는 fake (specs/core-request-batch, 3단계).

거리 선택자의 fake(`_selector_fakes`)를 이어 쓴다. 다른 시험 파일을 import 하지 않는다.
시험 파일은 `from _batch_fakes import ...` 로 읽는다 (`tests/core/` 에는 `__init__.py` 가 없다).
"""

from __future__ import annotations

from typing import Any

from _selector_fakes import (
    LIGHT_VALUES,
    NOW,
    FakeAudit,
    FakePolicy,
    FakeProximity,
    FakeReadiness,
    FakeRepo,
    FakeStore,
    lights,
    node,
)

from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Occupancy
from nearpilot.shared.models import (
    BATCH_ACTION,
    BatchChildren,
    BatchItemChildren,
    NodeInfo,
    SelectorChild,
    TargetSpec,
    UseItem,
    UseRequest,
)


class BatchRepo(FakeRepo):
    """묶음 요청의 저장소 접점. 거리 선택자의 fake 에 묶음 기록의 하위 요청 조회를 더한다.

    `link_selector_child` 는 판정이 고른 기기를 받고 `FakeRepo` 가 그대로 기록한다 (판정은 기기를 고른 뒤에 하위 요청을 만든다).
    하위 요청의 기기는 DB 행처럼 판정을 저장하기 전에는 행을 만들 때 받은 기기고, 저장한 뒤에는 저장된 판정의 `node_id` 다.
    """

    def batch_children(self, parent_internal_request_id: str) -> BatchChildren | None:
        """저장한 상위 기록의 `batch` 가 가리키는 하위 요청들 (DB 와 같다)."""
        self._hit("batch_children")
        record = self.records.get(parent_internal_request_id)
        if record is None or record.batch is None:
            return None
        items = tuple(BatchItemChildren(item, tuple(self._child(child) for child in item.children))
                      for item in record.batch.items)
        return BatchChildren(record.batch, items)

    def _child(self, child_id: str) -> SelectorChild:
        decision = self._stored(child_id)
        linked = next((link.node_id for link in self.child_links if link.child == child_id), None)
        node_id = linked if decision is None else decision.node_id
        return SelectorChild(child_id, node_id, decision)


class FakeWindow:
    """`any:` 의 대기 구간 대신 정해 둔 기기를 돌려준다 (`BatchAllocator`). 받은 배정 요청을 `requests` 에 남긴다."""

    def __init__(self, assign: str) -> None:
        self.assign = assign
        self.requests: list[Any] = []

    def submit(self, request: Any, group: str = "") -> str:
        self.requests.append(request)
        return self.assign


def speaker(node_id: str = "speaker-1") -> NodeInfo:
    """공용형 스피커. 동작은 `on`·`off` 다."""
    return node(node_id, "speaker", Occupancy.SHARED)


def locker(node_id: str) -> NodeInfo:
    """대여형 사물함. 동작은 `open`·`close` 다. 명시 지정은 근접 임계값을 보지 않는다."""
    return node(node_id, "locker", Occupancy.RENTAL)


def world() -> list[NodeInfo]:
    """조명 셋(`light-1`~`light-3`)과 스피커 하나(`speaker-1`). 모두 같은 장소에서 비콘을 봤다."""
    return [*lights(), speaker()]


def dimmable_lights() -> list[NodeInfo]:
    """`lights()` 와 같은 조명 셋이고 `on` 이 `brightness`(0~100)를 받는다. 기본 조명의 `on` 은 인자가 없다."""
    descriptor = legacy_descriptor("light", Occupancy.SHARED, brightness_max=100)
    return [node(node_id, "light", Occupancy.SHARED, descriptor=descriptor) for node_id in LIGHT_VALUES]


def batch_settings(**changes: Any) -> Settings:
    """`_selector_fakes.build` 의 기본 설정(경계 확률 기준 0.5)에 `changes` 를 더한다.

    기본 설정은 거리 순서의 경계 확률 검사에 걸리지 않는다. 그 검사를 시험하려면 `order_boundary_min_prob` 를 넘긴다.
    """
    return Settings(**{"version": "t", "order_boundary_min_prob": 0.5, **changes})


def batch_request(*items: tuple[Any, ...], request_id: str = "r1", user_id: str = "visitor") -> UseRequest:
    """묶음 요청. 항목은 `(target_spec, action)` 또는 `(target_spec, action, arguments)` 다."""
    return UseRequest(
        user_id=user_id, request_id=request_id, target=TargetSpec.parse("batch:"), action=BATCH_ACTION,
        host_kind="claude", received_at=NOW,
        items=tuple(UseItem(TargetSpec.parse(target), action, *rest) for target, action, *rest in items))


def build_batch(
    nodes: list[NodeInfo] | None = None, values: dict[str, float | None] | None = None,
    posteriors: dict[str, float] | None = None, policy: FakePolicy | None = None,
    settings: Settings | None = None, counts: dict[str, int] | None = None, window: FakeWindow | None = None,
) -> tuple[DecisionPipeline, BatchRepo, FakeProximity, FakeStore, FakeAudit]:
    """`_selector_fakes.build` 와 같은 구성이다. 저장소만 `BatchRepo` 고 `any:` 시험은 `window` 를 넘긴다.

    `nodes` 를 주지 않으면 `world()`, `values`(거리 순서의 정렬 값)를 주지 않으면 조명 세 대의 `LIGHT_VALUES` 다.
    `settings` 를 주지 않으면 `batch_settings()` 다.
    """
    repo = BatchRepo(world() if nodes is None else nodes)
    prox = FakeProximity(LIGHT_VALUES if values is None else values, posteriors, counts)
    store = FakeStore()
    audit = FakeAudit()
    ids = iter(range(100_000))
    pipeline = DecisionPipeline(
        repo, prox, policy or FakePolicy(), audit, settings or batch_settings(), FakeReadiness(),
        new_id=lambda: f"e{next(ids)}", window=window, store=store)
    return pipeline, repo, prox, store, audit
