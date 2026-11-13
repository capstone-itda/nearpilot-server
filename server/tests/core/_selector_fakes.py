"""거리 선택자 판정과 실행 시험이 함께 쓰는 fake (specs/core-distance-selector, 5·6단계).

다른 계층은 fake 로 대신한다. 다른 시험 파일을 import 하지 않는다.
시험 파일은 `from _selector_fakes import ...` 로 읽는다 (`tests/core/` 에는 `__init__.py` 가 없다).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from nearpilot.core.decision.pipeline import DecisionPipeline
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor, parse_descriptor
from nearpilot.shared.enums import (
    DeviceState,
    ExecutionStatus,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    StoreOutcome,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import (
    BeaconKey,
    CommandRecord,
    Decision,
    DecisionRecord,
    DistanceOrder,
    ExecutionContext,
    ExecutionResult,
    NodeInfo,
    PolicyCheckResult,
    ProximityResult,
    RequestLink,
    ReserveRequest,
    ReserveResult,
    RssiFrame,
    SelectorChild,
    SelectorChildren,
    StoreResult,
    TargetSpec,
    UseRequest,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("u", 1, 1)

_SIGNAL = {"kind": "signal_output"}
_NO_ARGUMENTS = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _declaration(kind: str, occupancy: str, actions: list[dict[str, Any]]):
    return parse_descriptor({
        "format": "nearpilot.device/1", "kind": kind, "occupancy": occupancy,
        "vendor": {"name": "acme", "model": f"{kind}-1", "firmware": "1.0"}, "actions": actions})


# 서버가 모르는 종류. 임계값 설정이 없다.
LAMP = _declaration("lamp", "shared", [
    {"name": "on", "description": "램프를 켠다", "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
    {"name": "off", "description": "램프를 끈다", "role": "deactivate",
     "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
])
BOX = _declaration("box", "rental", [
    {"name": "open", "description": "상자를 연다", "input_schema": _NO_ARGUMENTS, "result": _SIGNAL},
])


def node(node_id: str, kind: str, occupancy: Occupancy, **kw: Any) -> NodeInfo:
    """후보 조건을 모두 채운 기기. 선언은 v2 템플릿이고, 템플릿에 없는 `lamp`·`box` 는 이 파일의 선언이다."""
    base: dict[str, Any] = dict(
        node_id=node_id, place_id="p", kind=kind, occupancy=occupancy,
        trust_state=TrustState.TRUSTED, device_state=DeviceState.AVAILABLE,
        exclusive_group="g", last_seen_at=NOW - timedelta(seconds=1),
    )
    base.update(kw)
    if "descriptor" not in base:
        base["descriptor"] = {"lamp": LAMP, "box": BOX}.get(kind) or legacy_descriptor(kind, occupancy)
    return NodeInfo(**base)


# 조명 세 대. 값이 클수록 가깝다. 가까운 순서는 light-2, light-1, light-3 이다.
LIGHT_VALUES: dict[str, float | None] = {"light-1": -60.0, "light-2": -50.0, "light-3": -75.0}


def lights() -> list[NodeInfo]:
    return [node(node_id, "light", Occupancy.SHARED) for node_id in LIGHT_VALUES]


def frame(node_id: str) -> RssiFrame:
    return RssiFrame(node_id, BEACON, -60.0, NOW, "s1", 1, NOW, NOW)


def request(target: str, request_id: str = "r1", action: str = "on", user_id: str = "visitor") -> UseRequest:
    return UseRequest(
        user_id=user_id, request_id=request_id, target=TargetSpec.parse(target),
        action=action, host_kind="claude", received_at=NOW)


@dataclass(frozen=True)
class ChildLink:
    """`link_selector_child` 호출 하나."""

    parent: str
    req: UseRequest
    node_id: str
    digest: str
    child: str  # 돌려준 하위 요청 ID


class FakeRepo:
    """판정이 부르는 저장소 접점. 판정과 기록을 보관하고 DB 처럼 `previous` 와 하위 요청을 돌려준다."""

    def __init__(self, nodes: list[NodeInfo]) -> None:
        self.nodes_list = list(nodes)
        self.frames = [frame(n.node_id) for n in nodes]  # 모든 기기가 비콘을 봤다
        self.beacons = [BEACON]
        self.capacity_exceeded = False
        self.reserve_results: dict[str, ReserveOutcome] = {}  # 기기별 reserve_if_free 결과. 기본은 RESERVED
        self.calls: list[str] = []
        self.digests: list[str] = []
        self.keys: dict[tuple[str, str], tuple[str, str]] = {}  # (계정, 외부 request_id) → (내부 ID, digest)
        self.external: dict[str, str] = {}  # 내부 ID → 외부 request_id
        self.child_links: list[ChildLink] = []
        self.reserved: list[ReserveRequest] = []
        self.saved: list[Decision] = []  # 저장한 순서
        self.save_attempts: list[str] = []  # 저장을 시도한 내부 ID. 충돌해서 저장되지 않은 시도도 센다
        self.decisions: dict[str, Decision] = {}
        self.records: dict[str, DecisionRecord] = {}

    def _hit(self, name: str) -> None:
        self.calls.append(name)

    # ⓪ 중복
    def link_request(self, req: UseRequest, input_digest: str) -> RequestLink:
        self._hit("link_request")
        self.digests.append(input_digest)
        key = (req.user_id, req.request_id)
        if key not in self.keys:
            iid = f"i{len(self.keys) + 1}"
            self.keys[key] = (iid, input_digest)
            self.external[iid] = req.request_id
            return RequestLink(iid, True, False, capacity_exceeded=self.capacity_exceeded)
        iid, digest = self.keys[key]
        if digest != input_digest:
            return RequestLink(iid, False, True)
        return RequestLink(iid, False, False, previous=self._stored(iid))

    def _stored(self, iid: str) -> Decision | None:
        """DB 가 읽어 주는 모양의 판정. 거리 선택자 필드와 안내 문구는 저장하지 않는다."""
        d = self.decisions.get(iid)
        if d is None:
            return None
        return Decision(iid, self.external.get(iid, ""), d.verdict, d.reason, node_id=d.node_id,
                        use_id=d.use_id if d.verdict is Verdict.OK else None, p_max=d.p_max)

    def save_decision(self, decision: Decision, record: DecisionRecord) -> StoreResult:
        self._hit("save_decision")
        iid = decision.internal_request_id
        self.save_attempts.append(iid)
        if iid in self.decisions:
            return StoreResult(StoreOutcome.CONFLICT)
        self.saved.append(decision)
        self.decisions[iid] = decision
        self.records[iid] = record
        return StoreResult(StoreOutcome.APPLIED)

    # 거리 선택자 하위 요청
    def link_selector_child(
        self, parent_internal_request_id: str, req: UseRequest, node_id: str, input_digest: str
    ) -> str:
        self._hit("link_selector_child")
        child = f"c{len(self.child_links) + 1}"
        self.child_links.append(ChildLink(parent_internal_request_id, req, node_id, input_digest, child))
        self.external[child] = self.external[parent_internal_request_id]
        return child

    def selector_children(self, parent_internal_request_id: str) -> SelectorChildren | None:
        """저장한 상위 기록이 가리키는 하위 요청들. 기기는 기록의 `selected` 에서 읽는다 (DB 와 같다)."""
        self._hit("selector_children")
        record = self.records.get(parent_internal_request_id)
        if record is None or record.selector is None:
            return None
        selector = record.selector
        children = tuple(SelectorChild(child, node_id, self._stored(child))
                         for child, node_id in zip(selector.children, selector.selected))
        return SelectorChildren(selector, children)

    # ① 비콘
    def beacons_of(self, user_id: str) -> list[BeaconKey]:
        self._hit("beacons_of")
        return self.beacons

    def rssi_since(self, beacons: Any, since: datetime) -> list[RssiFrame]:
        self._hit("rssi_since")
        return self.frames

    # ② 후보
    def nodes(self, kind: str | None = None) -> list[NodeInfo]:
        self._hit("nodes")
        return [n for n in self.nodes_list if kind is None or n.kind == kind]

    def last_success_arguments(self, node_id: str, action: str) -> None:
        self._hit("last_success_arguments")
        return None

    # ⑤⑥
    def reserve_if_free(self, req: ReserveRequest) -> ReserveResult:
        self._hit("reserve_if_free")
        self.reserved.append(req)
        outcome = self.reserve_results.get(req.node_id, ReserveOutcome.RESERVED)
        if outcome not in (ReserveOutcome.RESERVED, ReserveOutcome.REOPERATING):
            return ReserveResult(outcome)
        target = next(n for n in self.nodes_list if n.node_id == req.node_id)
        use_id = None if target.occupancy is Occupancy.SHARED else f"use-{req.node_id}"
        return ReserveResult(outcome, use_id=use_id)


HEARD_FRAMES = 30  # `counts` 를 주지 않았을 때 정렬 값이 있는 기기의 프레임 수 (경계 확률의 N, #278)


class FakeProximity:
    """`values` 에 있는 후보만 근접 참여 기기다. 정렬 값이 None 이면 비콘을 못 본 기기다.

    `counts` 는 기기별 프레임 수다. 주지 않으면(None) 정렬 값이 있는 기기는 `HEARD_FRAMES`, None 인 기기는 0 이다.
    주면 근접 참여 기기의 값만 거리 순서에 그대로 넘긴다. 빈 사전이면 프레임 수가 없는 거리 순서가 된다.
    """

    def __init__(
        self, values: dict[str, float | None], posteriors: dict[str, float] | None = None,
        counts: dict[str, int] | None = None,
    ) -> None:
        self.values = values
        self.posteriors = {} if posteriors is None else posteriors
        self.counts = counts
        self.order_calls: list[list[str]] = []  # distance_order 에 넘긴 후보
        self.posterior_calls: list[tuple[list[str], list[str]]] = []  # (후보, 장소의 전체 기기)

    def distance_order(self, beacon: BeaconKey, candidates: Any, at: datetime) -> DistanceOrder:
        """`HmmProximityEstimator.distance_order` 와 같은 정렬이다. 값이 큰 기기부터, 같으면 기기 ID 순서, None 은 끝."""
        self.order_calls.append([n.node_id for n in candidates])
        participants = [n.node_id for n in candidates if n.node_id in self.values]
        heard = sorted((n for n in participants if self.values[n] is not None),
                       key=lambda n: (-self.values[n], n))
        unheard = sorted(n for n in participants if self.values[n] is None)
        nodes = (*heard, *unheard)
        if self.counts is None:
            counts = {n: 0 if self.values[n] is None else HEARD_FRAMES for n in nodes}
        else:
            counts = {n: self.counts[n] for n in nodes if n in self.counts}
        return DistanceOrder(nodes=nodes, values={n: self.values[n] for n in nodes}, counts=counts)

    def posterior(self, beacons: Any, candidates: Any, at: datetime, *, place_nodes: Any) -> ProximityResult:
        self.posterior_calls.append(
            ([n.node_id for n in candidates], [n.node_id for n in place_nodes]))
        posteriors = {n.node_id: self.posteriors[n.node_id] for n in candidates if n.node_id in self.posteriors}
        return ProximityResult(posteriors, at, at, "m", "c")

    def observe(self, frame: RssiFrame, *, node: NodeInfo) -> None:
        pass

    def observe_report(self, report: Any, *, node: NodeInfo) -> None:
        pass


class FakePolicy:
    """기기별 ④ 결과. 지정하지 않은 기기는 OK 다."""

    def __init__(self, by_node: dict[str, PolicyCheckResult] | None = None) -> None:
        self.by_node = {} if by_node is None else by_node
        self.calls: list[str] = []
        self.request_ids: list[str | None] = []

    def check(
        self, user_id: str, node_id: str, action: str, now: datetime,
        *, internal_request_id: str | None = None, reoperate: bool = False,
    ) -> PolicyCheckResult:
        self.calls.append(node_id)
        self.request_ids.append(internal_request_id)
        return self.by_node.get(node_id, PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1))


class FakeStore:
    """판정이 부르는 `cancel_not_sent` 를 기록하고, 상태 관리가 남긴 실행 기록(`execution`)을 6단계 시험이 채운다."""

    def __init__(self) -> None:
        self.cancels: list[tuple[str, Any, ReasonCode, dict[str, Any]]] = []
        self.outcome = StoreOutcome.APPLIED
        self.error: Exception | None = None
        self.executions: dict[str, CommandRecord] = {}

    def cancel_not_sent(self, internal_request_id: str, expected: Any, reason: ReasonCode,
                        evidence: Any) -> StoreResult:
        self.cancels.append((internal_request_id, expected, reason, dict(evidence)))
        if self.error:
            raise self.error
        return StoreResult(self.outcome)

    def execution(self, internal_request_id: str) -> CommandRecord | None:
        return self.executions.get(internal_request_id)


@dataclass(frozen=True)
class Rejected:
    """실행 직전 재검사가 전송을 막았다. `FakeExecution` 의 기기별 결과로 쓴다."""

    verdict: Verdict
    reason: ReasonCode


class FakeExecution:
    """`ExecutionService.execute` 의 fake (6단계). 기기별 결과를 지정한다. 지정하지 않은 기기는 성공한다.

    결과는 `ExecutionStatus`, 예외, 재검사 거부(`Rejected`) 중 하나다. 결과를 낸 하위 요청은 상태 관리처럼
    `FakeStore.executions` 에 기록을 남긴다 (재전송이 읽는다). 예외를 던진 하위 요청은 기록을 남기지 않는다.
    `barrier` 가 있으면 결과를 내기 전에 기다린다. `after` 는 {기기: 먼저 끝나야 하는 기기} 다. 그 기기의 execute 가
    끝난 뒤에 결과를 낸다 (완료 순서를 정한다). 기다리는 기기가 시작하지 않으면 `RuntimeError` 다.
    스레드마다 불리므로 호출한 순서는 보장하지 않는다.
    """

    def __init__(
        self, store: FakeStore, results: dict[str, ExecutionStatus | Exception | Rejected] | None = None,
        barrier: threading.Barrier | None = None, after: dict[str, str] | None = None,
    ) -> None:
        self.store = store
        self.results = {} if results is None else results
        self.barrier = barrier
        self.after = {} if after is None else after
        self.finished: dict[str, threading.Event] = {}  # 기기별 execute 가 끝났다
        self._events_lock = threading.Lock()
        self.contexts: list[ExecutionContext] = []

    def _finished(self, node_id: str) -> threading.Event:
        with self._events_lock:
            return self.finished.setdefault(node_id, threading.Event())

    def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        try:
            return self._execute(ctx)
        finally:
            self._finished(ctx.node_id).set()

    def _execute(self, ctx: ExecutionContext) -> ExecutionResult:
        self.contexts.append(ctx)
        if self.barrier is not None:
            self.barrier.wait()
        first = self.after.get(ctx.node_id)
        # 기다리는 기기가 시작하지 않으면(차례로 실행하는 구현) 대기 시간이 지나 이 하위 요청이 실패한다
        if first is not None and not self._finished(first).wait(timeout=10):
            raise RuntimeError(f"{first} 의 실행이 끝나지 않았다")
        result = self.results.get(ctx.node_id, ExecutionStatus.SUCCEEDED)
        if isinstance(result, Exception):
            raise result
        iid, command_id = ctx.internal_request_id, f"cmd-{ctx.internal_request_id}"
        if isinstance(result, Rejected):
            evidence = {"source": "recheck", "verdict": result.verdict.value, "reason": result.reason.value}
            self._record(ctx, command_id, ExecutionStatus.NOT_SENT, evidence)
            recheck = Decision(iid, ctx.request.request_id, result.verdict, result.reason, node_id=ctx.node_id)
            return ExecutionResult(iid, ExecutionStatus.NOT_SENT, command_id, None, recheck)
        self._record(ctx, command_id, result, {})
        return ExecutionResult(iid, result, command_id, ctx.use_id)

    def _record(self, ctx: ExecutionContext, command_id: str, status: ExecutionStatus,
                evidence: dict[str, Any]) -> None:
        self.store.executions[ctx.internal_request_id] = CommandRecord(
            command_id, ctx.internal_request_id, ctx.node_id, ctx.request.action, status,
            execution_kind=ctx.execution_kind, use_id=ctx.use_id, evidence=evidence)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def append(self, event: Any) -> None:
        self.events.append(event)


class FakeReadiness:
    """상태 관리의 기기별 실행 가능 여부. 기본은 모두 가능하다."""

    def __init__(self, not_ready: tuple[str, ...] = ()) -> None:
        self.not_ready = set(not_ready)

    def can_execute(self, node_id: str) -> bool:
        return node_id not in self.not_ready


def build(
    nodes: list[NodeInfo], values: dict[str, float | None], posteriors: dict[str, float] | None = None,
    policy: FakePolicy | None = None, settings: Settings | None = None, counts: dict[str, int] | None = None,
) -> tuple[DecisionPipeline, FakeRepo, FakeProximity, FakeStore, FakeAudit]:
    """`settings` 를 주지 않으면 경계 확률 기준을 0.5 로 둔다. 거리 순서는 정렬돼 있어 경계의 D ≥ 0 이므로
    경계 확률은 0.5 이상이다. 그래서 경계 확률이 목적이 아닌 시험은 경계 검사에 걸리지 않는다.
    경계 확률을 시험하는 시험은 설정을 직접 넘긴다 (FR-09, #278).
    """
    repo = FakeRepo(nodes)
    prox = FakeProximity(values, posteriors, counts)
    store = FakeStore()
    audit = FakeAudit()
    ids = iter(range(100_000))
    pipeline = DecisionPipeline(
        repo, prox, policy or FakePolicy(), audit, settings or Settings(version="t", order_boundary_min_prob=0.5),
        FakeReadiness(), new_id=lambda: f"e{next(ids)}", store=store)
    return pipeline, repo, prox, store, audit
