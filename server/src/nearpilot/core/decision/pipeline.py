"""FR-18 결정론적 판정 파이프라인 ⓪~⑥.

단계를 부르는 순서와 멈추는 때만 정한다. 단계 내부의 계산은 상대 계층의 Protocol 이 한다.
기본값은 거부다. 예외, 누락, 알 수 없는 상태는 OK 가 되지 않는다.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from statistics import NormalDist
from typing import Any, Protocol

from nearpilot.core.allocation import AllocationRequest
from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import ArgumentProblem, normalize_arguments, validate_arguments
from nearpilot.shared.enums import (
    ActionRole,
    DeviceState,
    ExecutionKind,
    ExecutionStatus,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    Step,
    StoreOutcome,
    TargetKind,
    TrustState,
    Verdict,
)
from nearpilot.shared.interfaces import (
    AuditLog,
    ExecutionReadiness,
    ExecutionStore,
    PolicyChecker,
    ProximityEstimator,
    Repository,
)
from nearpilot.shared.models import (
    AuditEvent,
    BatchChildren,
    BatchItemRecord,
    BatchRecord,
    BeaconKey,
    Decision,
    DecisionRecord,
    DeviceOutcome,
    ExecutionContext,
    NodeInfo,
    PolicyCheckResult,
    ProximityResult,
    ReserveRequest,
    SelectorRecord,
    UseRequest,
)

log = logging.getLogger(__name__)

_GENERIC_MESSAGE = "요청을 처리하지 못했습니다."


@dataclass(frozen=True)
class SelectorDecision:
    """거리 선택자 판정 결과 (FR-09, #253)."""

    decision: Decision  # 상위 요청의 판정. devices·selected_count·similar 를 채운다
    children: tuple[Decision, ...] = ()  # 상위 판정이 OK 일 때 하위 요청의 판정. 거리 순서
    # 이번에 새로 OK 가 난 하위 요청의 인계 context. children 과 같은 순서. 재전송이면 비어 있다
    contexts: tuple[ExecutionContext, ...] = ()


def select_candidates(
    nodes: Sequence[NodeInfo], kind: str | None, settings: Settings, now: datetime,
    place_id: str | None = None, executable: Callable[[str], bool] | None = None,
) -> Sequence[NodeInfo]:
    """② 후보 조건. 최초 판정과 실행 직전 재검사가 같은 조건을 쓴다.

    `kind` 는 기기 선언의 종류 이름이다. None 이면 종류로 거르지 않는다.
    `executable` 이 있으면 새 실행이 불가능한 기기(재시작 복원 중)를 뺀다 (#82).
    """
    ttl = timedelta(seconds=settings.online_ttl_sec)
    return [
        n for n in nodes
        if (kind is None or n.kind == kind)
        and (place_id is None or n.place_id == place_id)
        and n.trust_state is TrustState.TRUSTED
        and n.device_state is not DeviceState.FAULT
        and n.last_seen_at is not None
        and now - n.last_seen_at <= ttl
        and (executable is None or executable(n.node_id))
    ]


def threshold_for(settings: Settings, kind: str | None) -> float | None:
    """근접 지정과 전역 할당의 임계값 `threshold_{kind}`. 명시 지정에는 적용하지 않는다 (FR-17)."""
    return settings.threshold_for(kind)


def target_kind(req: UseRequest, nodes: Sequence[NodeInfo]) -> str | None:
    """요청이 가리키는 종류. 명시 지정은 저장한 기기의 종류다. 없는 기기면 None 이다."""
    if req.target.kind is not TargetKind.EXPLICIT:
        return req.target.device_kind
    return next((n.kind for n in nodes if n.node_id == req.target.node_id), None)


def is_reoperate(node: NodeInfo, action: str) -> bool:
    """기기 선언에서 이 동작의 역할이 `reoperate` 인가 (FR-09). 동작 이름으로 정하지 않는다."""
    spec = None if node.descriptor is None else node.descriptor.action(action)
    return spec is not None and spec.role is ActionRole.REOPERATE


def _similar_pair(a: float | None, b: float | None, similar_db: float) -> bool:
    """경계 두 기기의 정렬 값이 비슷한가. 둘 다 비콘을 못 봤으면 참, 하나만 못 봤으면 거짓이다 (FR-09)."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < similar_db


def selector_edges(
    order: Sequence[str], start: int, count: int | None
) -> tuple[tuple[str, str], ...]:
    """고른 범위의 경계 쌍. 각 쌍은 (거리 순서에서 앞선 기기, 뒤 기기)다. 앞쪽 경계가 먼저다 (FR-09, #278).

    경계는 고른 범위의 끝 기기와 그 바로 바깥 기기다. 앞뒤 중 바깥 기기가 있는 쪽만 있다. count None 은 끝까지다.
    고를 기기가 없으면 () 다.
    """
    first = start - 1
    end = len(order) if count is None else min(first + count, len(order))
    if first >= end:
        return ()
    edges = []
    if first > 0:
        edges.append((order[first - 1], order[first]))
    if end < len(order):
        edges.append((order[end - 1], order[end]))
    return tuple(edges)


def boundary_probability(
    a_value: float | None, a_count: int, b_value: float | None, b_count: int,
    frame_sigma_db: float, static_sigma_db: float,
) -> float:
    """앞선 기기 a 가 뒤 기기 b 보다 가까울 확률 Φ(D/s) (FR-09, #278).

    D 는 a 의 정렬 값에서 b 의 정렬 값을 뺀 값이고 s² = σf²(1/Na + 1/Nb) + 2σs² 다.
    σf 는 프레임 잡음, σs 는 평균으로 줄지 않는 기기별 고정 오차, N 은 그 기기의 프레임 수다.
    b 만 비콘을 보지 못했으면 1, 둘 다 보지 못했으면 0.5 다.
    a 만 보지 못했으면 ValueError 다 (정렬된 순서에서는 생기지 않는다).
    비콘을 본 기기의 프레임 수가 1 미만이어도 ValueError 다.
    """
    if a_value is None:
        if b_value is not None:
            raise ValueError("앞선 기기만 비콘을 보지 못했다. 정렬된 거리 순서에서는 생기지 않는다")
        return 0.5
    if a_count < 1 or (b_value is not None and b_count < 1):
        raise ValueError(f"비콘을 본 기기의 프레임 수는 1 이상이어야 한다: 앞선 기기 {a_count}, 뒤 기기 {b_count}")
    if b_value is None:
        return 1.0
    spread = math.sqrt(frame_sigma_db ** 2 * (1 / a_count + 1 / b_count) + 2 * static_sigma_db ** 2)
    return NormalDist().cdf((a_value - b_value) / spread)


def selector_cut(
    order: Sequence[str], values: Mapping[str, float | None], start: int, count: int | None,
    similar_db: float,
) -> tuple[tuple[str, ...], bool]:
    """요청 방향의 거리 순서에서 시작·개수로 고르고 경계의 "비슷함"을 정한다 (FR-09, #253).

    count None 은 끝까지다. 고를 기기가 없으면 ((), False) 다.
    경계는 `selector_edges` 의 쌍이다. 앞뒤 중 있는 쪽만 보고, 한 쌍이라도 비슷하면 참이다.
    """
    first = start - 1
    end = len(order) if count is None else min(first + count, len(order))
    selected = tuple(order[first:end])
    edges = selector_edges(order, start, count)
    return selected, any(_similar_pair(values[a], values[b], similar_db) for a, b in edges)


def make_decision(
    request_id: str, iid: str, verdict: Verdict, reason: ReasonCode, **kw: Any
) -> Decision:
    message = _GENERIC_MESSAGE if reason is ReasonCode.INTERNAL_ERROR else None
    return Decision(internal_request_id=iid, request_id=request_id, verdict=verdict,
                    reason=reason, message=message, **kw)


@dataclass
class _Applied:
    """판정에 적용한 값. DecisionRecord 와 감사 기록의 재료다."""

    policy_version: int | None = None
    model_version: str | None = None
    calib_version: str | None = None
    posteriors: Mapping[str, float] = field(default_factory=dict)
    window_start: datetime | None = None
    window_end: datetime | None = None
    usage_id: str | None = None
    execution_kind: ExecutionKind | None = None
    reserved_at: datetime | None = None
    reserve_called: bool = False  # reserve_if_free() 를 부르기 직전에 True 가 된다. 오류 뒤 취소 여부를 정한다 (#80)
    arguments: Mapping[str, Any] | None = None  # ③ 뒤에 정한 동작 인자. 실행 인계로 넘긴다 (FR-09)
    reoperate: bool = False  # 동작 역할이 reoperate 다. ④ 정책과 ⑤⑥ 예약에 넘긴다 (FR-09)


class _SaveConflict(Exception):
    """같은 내부 요청에 다른 판정 기록이 이미 있다. 기존 기록을 덮어쓰지 않는다."""


class BatchAllocator(Protocol):
    """`any:` 요청을 대기 구간에 모아 배정한다 (`AllocationWindow`). 배정받은 기기 ID 또는 None."""

    def submit(self, request: AllocationRequest, group: str = "") -> str | None: ...


class _Stop(Exception):
    """단계가 실패해서 판정을 확정했다. 뒤 단계는 실행하지 않는다."""

    def __init__(self, step: Step, decision: Decision):
        super().__init__(decision.reason)
        self.step = step
        self.decision = decision


@dataclass(frozen=True)
class _Pick:
    """거리 순서에서 고른 결과와 하위 요청의 ③이 쓸 값 (FR-09)."""

    beacons: Sequence[BeaconKey]
    candidates: Sequence[NodeInfo]  # ②의 전체 후보. 고른 기기만이 아니다
    place_nodes: Sequence[NodeInfo]  # 비콘을 본 장소의 전체 기기
    order: tuple[str, ...]  # 요청 방향의 거리 순서
    values: Mapping[str, float | None]
    selected: tuple[str, ...]
    similar: bool
    counts: Mapping[str, int]  # near 는 거리 순서의 기기별 프레임 수. far 는 비어 있다
    boundary_probability: float | None  # near 의 가장 낮은 경계 확률. 경계가 없는 요청과 far 는 None 이다 (#278)


@dataclass
class _Child:
    """거리 선택자의 하위 요청 하나. 오류 뒤 정리에 필요한 것만 담는다 (FR-09)."""

    iid: str
    node_id: str
    applied: _Applied = field(default_factory=_Applied)
    decision: Decision | None = None
    step: Step | None = None  # 판정이 멈춘 단계. 통과했으면 마지막 단계
    saved: bool = False  # 판정 기록의 저장을 마쳤다
    cancelled: bool = False  # 옆 기기 때문에 하는 취소를 이미 시도했다


@dataclass
class _SelectorRun:
    """거리 선택자 판정 하나가 진행하는 동안의 값. 하위 요청이 나눠 쓰고, 오류가 났을 때 정리에도 쓴다 (FR-09)."""

    applied: _Applied = field(default_factory=_Applied)  # 상위 요청에 적용한 근접 추정 결과
    children: list[_Child] = field(default_factory=list)
    posterior: ProximityResult | None = None  # 첫 rental 하위 요청이 한 번 부른 결과
    parent_conflict: bool = False  # 상위 판정 기록의 저장이 CONFLICT 였다


@dataclass
class _BatchItem:
    """묶음 요청의 항목 하나가 판정한 것. 상위 판정 기록과 하위 요청의 인계 context 의 재료다 (FR-09, #284)."""

    index: int  # 1부터 센 항목 순서
    req: UseRequest  # 항목의 대상·동작·인자를 가진 요청. 하위 요청의 판정과 인계 context 가 쓴다
    children: list[_Child] = field(default_factory=list)  # 이 항목이 만든 하위 요청. 만든 순서
    pick: _Pick | None = None  # 거리 선택자 항목이 고른 결과. 다른 항목은 None 이다


class DecisionPipeline:
    def __init__(
        self,
        repo: Repository,
        proximity: ProximityEstimator,
        policy: PolicyChecker,
        audit: AuditLog,
        settings: Settings,
        readiness: ExecutionReadiness,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        window: BatchAllocator | None = None,
        store: ExecutionStore | None = None,
    ) -> None:
        self._repo = repo
        self._proximity = proximity
        self._policy = policy
        self._audit = audit
        self._settings = settings
        self._readiness = readiness
        self._window = window
        self._store = store  # None 이면 예약 뒤 오류에도 취소하지 않는다
        self._new_id = new_id

    # ── 진입점 ──────────────────────────────────────────────────────

    def decide(self, req: UseRequest) -> Decision:
        """⓪부터 ⑥까지 같은 순서로 검사하고 판정 하나를 반환한다."""
        return self.decide_for_execution(req)[0]

    def decide_for_execution(self, req: UseRequest) -> tuple[Decision, ExecutionContext | None]:
        """판정과 실행 인계 context 를 반환한다. context 는 이번에 새로 OK 가 난 요청에만 있다 (#16, #35).

        거리 선택자(`near`·`far`)와 묶음 요청은 받지 않는다. 각각 `decide_selector`, `decide_batch` 만 처리한다 (FR-09, #253, #284).
        """
        if req.target.is_selector or req.target.is_batch:
            return self._decision(req, "", Verdict.DENY, ReasonCode.INVALID_INPUT), None
        internal_id = ""
        try:
            link = self._repo.link_request(req, input_digest(req))
            internal_id = link.internal_request_id
            if link.conflict:
                d = self._decision(req, internal_id, Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
                self._audit_step(Step.DEDUP, internal_id, {"result": d.reason.value})
                return d, None  # 기존 판정을 덮어쓰지 않는다
            if not link.is_new:
                if link.previous is not None:
                    return link.previous, None
                # 아직 판정 중이다. 오류가 아니므로 다시 판정하지도, 저장하지도 않는다
                d = self._decision(req, internal_id, Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS)
                self._audit_step(Step.DEDUP, internal_id, {"result": "IN_PROGRESS"})
                return d, None
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return self._internal_error(req, internal_id, Step.DEDUP, exc, save=False), None

        applied = _Applied()
        try:
            try:
                if link.capacity_exceeded:
                    decision = self._decision(
                        req, internal_id, Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED)
                    step, detail = Step.DEDUP, {"capacity_exceeded": True}
                else:
                    decision, step, detail = self._run(req, internal_id, applied)
            except _Stop as stop:
                decision, step, detail = stop.decision, stop.step, {}
            self._audit_step(step, internal_id, {
                "verdict": decision.verdict.value, "reason": decision.reason.value,
                "settings_version": self._settings.version, **detail})
            saved = self._repo.save_decision(decision, self._record(applied))
            if saved.outcome is StoreOutcome.CONFLICT:
                raise _SaveConflict("같은 내부 요청에 다른 판정 기록이 있다")
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            conflict = isinstance(exc, _SaveConflict)
            # 오류 판정을 저장하기 전에 취소한다. 저장 뒤에 온 같은 키의 재판정(#76)이 아직 취소되지 않은 예약을 받지 않는다
            if applied.reserve_called and not conflict:
                self._cancel_reservation(internal_id)
            return self._internal_error(req, internal_id, Step.EXEC, exc, save=not conflict), None
        return decision, self._context(req, internal_id, decision, applied)

    # ── 거리 선택자 (FR-09, #253) ───────────────────────────────────

    def decide_selector(self, req: UseRequest) -> SelectorDecision:
        """거리 선택자 요청을 상위 요청 하나와 고른 기기마다의 하위 요청으로 판정한다.

        고른 기기마다 ③(점유 방식별), 동작·인자, ④, 예약을 하고 처음 걸린 곳에서 멈춘다.
        하나라도 걸리면 앞 하위 요청의 예약을 취소하고 아무것도 인계하지 않는다.
        실행 인계 context 만 돌려준다. 하위 요청의 실행과 결과 묶음은 부른 쪽이 한다.
        재전송에는 저장된 상위·하위 판정을 돌려준다. 현재 시각을 읽지 않고 `req.received_at` 만 쓴다.
        """
        if not req.target.is_selector:
            raise ValueError(f"거리 선택자가 아니다: {req.target.serialize()!r}")
        iid = ""
        try:
            digest = input_digest(req)
            link = self._repo.link_request(req, digest)
            iid = link.internal_request_id
            if link.conflict:
                d = self._decision(req, iid, Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
                self._audit_step(Step.DEDUP, iid, {"result": d.reason.value})
                return SelectorDecision(self._selector_view(d))  # 기존 판정을 덮어쓰지 않는다
            if not link.is_new:
                if link.previous is not None:
                    return self._resend(iid, link.previous)
                # 아직 판정 중이다. 오류가 아니므로 다시 판정하지도, 저장하지도 않는다
                d = self._decision(req, iid, Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS)
                self._audit_step(Step.DEDUP, iid, {"result": "IN_PROGRESS"})
                return SelectorDecision(self._selector_view(d))
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return SelectorDecision(self._selector_view(
                self._internal_error(req, iid, Step.DEDUP, exc, save=False)))

        run = _SelectorRun()
        try:
            return self._judge_selector(req, iid, digest, link.capacity_exceeded, run)
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return self._selector_failed(req, iid, exc, run)

    def _judge_selector(
        self, req: UseRequest, iid: str, digest: str, capacity_exceeded: bool, run: _SelectorRun
    ) -> SelectorDecision:
        """새 요청의 판정과 저장. 예외는 `_selector_failed` 가 정리한다."""
        pick: _Pick | None = None
        detail: dict[str, Any] = {}
        devices: tuple[DeviceOutcome, ...] = ()
        outcomes: tuple[Decision, ...] = ()
        contexts: tuple[ExecutionContext, ...] = ()
        try:
            if capacity_exceeded:
                parent = self._decision(req, iid, Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED)
                step, detail = Step.DEDUP, {"capacity_exceeded": True}
            else:
                pick = self._pick(req, iid)
                if not pick.selected:  # 고를 기기가 없다. 거리 순서는 기록에 남긴다
                    raise _Stop(Step.PROXIMITY, self._decision(
                        req, iid, Verdict.DENY, ReasonCode.NO_CANDIDATE))
                lowest = pick.boundary_probability
                if lowest is not None and lowest < self._settings.order_boundary_min_prob:
                    # 경계의 순서가 애매하다. 하위 요청을 만들기 전이라 취소할 예약이 없다 (spec 결정 25)
                    raise _Stop(Step.PROXIMITY, self._decision(
                        req, iid, Verdict.ASK, ReasonCode.ORDER_UNCERTAIN))
                failed = self._judge_children(req, iid, digest, pick, run)
                if failed is None:
                    parent = self._decision(req, iid, Verdict.OK, ReasonCode.OK)
                    step = Step.DEVICE_OCCUPANCY
                    outcomes = tuple(c.decision for c in run.children)
                    devices = tuple(DeviceOutcome(c.node_id, c.decision.verdict, c.decision.reason,
                                                  c.decision.use_id) for c in run.children)
                    contexts = tuple(self._context(req, c.iid, c.decision, c.applied)
                                     for c in run.children)
                    if None in contexts:  # OK 인 하위 요청은 인계 context 가 있어야 한다
                        raise ValueError("OK 인 하위 요청의 실행 인계를 만들지 못했다")
                else:
                    f = failed.decision
                    self._cancel_siblings(run.children, f)
                    parent = self._decision(req, iid, f.verdict, f.reason)
                    step = failed.step
                    devices = (DeviceOutcome(f.node_id, f.verdict, f.reason, None),)
        except _Stop as stop:
            parent, step = stop.decision, stop.step

        # ORDER_UNCERTAIN 은 하위 요청 없이 끝나므로 고른 개수가 0이다. 재전송도 같다 (spec 결정 27, 30)
        selected_count = 0 if pick is None or parent.reason is ReasonCode.ORDER_UNCERTAIN else len(pick.selected)
        parent = self._selector_view(parent, devices, selected_count, False if pick is None else pick.similar)
        children = tuple(c.iid for c in run.children)
        self._audit_step(step, iid, {
            "verdict": parent.verdict.value, "reason": parent.reason.value,
            "settings_version": self._settings.version, "children": len(children), **detail})
        record = None if pick is None else SelectorRecord(
            req.target.serialize(), pick.order, pick.values, pick.selected, pick.similar, children,
            counts=pick.counts, boundary_probability=pick.boundary_probability)
        saved = self._repo.save_decision(parent, replace(self._record(run.applied), selector=record))
        if saved.outcome is StoreOutcome.CONFLICT:
            run.parent_conflict = True
            raise _SaveConflict("같은 내부 요청에 다른 판정 기록이 있다")
        return SelectorDecision(parent, outcomes, contexts)

    def _pick(self, req: UseRequest, iid: str) -> _Pick:
        """① 비콘, ② 후보, 거리 순서에서 시작·개수로 고르기. 비콘이나 후보가 없으면 멈춘다 (FR-09).

        near 요청이면 경계마다 경계 확률을 계산해 가장 낮은 값을 담는다. 기준과의 비교는 부른 쪽이 한다 (#278).
        경계의 비콘을 본 기기에 프레임 수가 없으면 ValueError 다. 따로 잡지 않고 내부 오류로 거부한다.
        임계값은 여기서 검사하지 않는다. 하위 요청이 점유 방식별로 검사한다 (FR-17).
        """
        s = self._settings
        now = req.received_at
        beacons, all_nodes, place_id = self._see_beacon(req, iid)
        candidates = select_candidates(
            all_nodes, req.target.device_kind, s, now, place_id, self._readiness.can_execute)
        self._audit_step(Step.CANDIDATE, iid, {"place_id": place_id,
                                              "candidates": [n.node_id for n in candidates],
                                              "online_ttl_sec": s.online_ttl_sec})
        if not candidates:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.NO_CANDIDATE))
        order = self._proximity.distance_order(beacons[0], candidates, now)
        near = req.target.kind is TargetKind.NEAR
        # far 는 near 순서를 뒤집은 것이다 (spec 결정 6)
        direction = order.nodes if near else tuple(reversed(order.nodes))
        selected, similar = selector_cut(
            direction, order.values, req.target.start, req.target.count, s.order_similar_db)
        detail: dict[str, Any] = {
            "selector": req.target.serialize(), "order": list(direction), "values": dict(order.values),
            "selected": list(selected), "similar": similar,
            "anchor_recent_sec": s.anchor_recent_sec, "order_similar_db": s.order_similar_db}
        counts: Mapping[str, int] = {}
        lowest: float | None = None
        if near:  # far 는 경계 확률을 계산하지 않는다 (spec 결정 31)
            counts = order.counts
            lowest = min(
                (boundary_probability(order.values[a], counts.get(a, 0), order.values[b], counts.get(b, 0),
                                      s.order_frame_sigma_db, s.order_static_sigma_db)
                 for a, b in selector_edges(order.nodes, req.target.start, req.target.count)),
                default=None)
            detail.update(
                counts=dict(counts), boundary_probability=lowest,
                order_frame_sigma_db=s.order_frame_sigma_db, order_static_sigma_db=s.order_static_sigma_db,
                order_boundary_min_prob=s.order_boundary_min_prob)
        self._audit_step(Step.PROXIMITY, iid, detail)
        return _Pick(beacons, candidates, [n for n in all_nodes if n.place_id == place_id],
                     direction, order.values, selected, similar, counts, lowest)

    def _judge_children(
        self, req: UseRequest, iid: str, digest: str, pick: _Pick, run: _SelectorRun
    ) -> _Child | None:
        """고른 기기마다 하위 요청을 만들어 판정하고 저장한다.

        처음 걸린 하위 요청에서 멈추고(뒤 기기는 하위 요청을 만들지 않는다) 그 하위 요청을 반환한다.
        모두 통과하면 None 이다.
        """
        by_id = {n.node_id: n for n in pick.candidates}
        for node_id in pick.selected:
            node = by_id[node_id]
            child = _Child(self._repo.link_selector_child(iid, req, node_id, digest), node_id)
            run.children.append(child)
            try:
                child.decision, child.step = self._judge_child(req, child, node, pick, run)
            except _Stop as stop:
                child.decision, child.step = stop.decision, stop.step
            decision = child.decision
            self._audit_step(child.step, child.iid, {
                "verdict": decision.verdict.value, "reason": decision.reason.value,
                "settings_version": self._settings.version, "selector_parent": iid})
            saved = self._repo.save_decision(decision, self._record(child.applied))
            if saved.outcome is StoreOutcome.CONFLICT:
                raise _SaveConflict("같은 내부 요청에 다른 판정 기록이 있다")
            child.saved = True
            if decision.verdict is not Verdict.OK:
                return child
        return None

    def _judge_child(
        self, req: UseRequest, child: _Child, node: NodeInfo, pick: _Pick, run: _SelectorRun
    ) -> tuple[Decision, Step]:
        """하위 요청 하나의 ③ 근접(점유 방식별), 동작·인자, ④, ⑤⑥. 단일 기기 요청과 같은 경로다 (FR-18).

        통과하면 (OK 판정, 마지막 단계)를 반환하고 걸리면 그 판정으로 멈춘다.
        """
        s = self._settings
        iid, node_id, applied = child.iid, node.node_id, child.applied
        p: float | None = None
        if node.occupancy is Occupancy.SHARED:  # 사후확률 검사를 하지 않는다. ① 비콘 검사는 이미 했다 (FR-17)
            self._audit_step(Step.PROXIMITY, iid, {"skipped": True, "reason": "selector_shared",
                                                  "node_id": node_id})
        else:
            threshold = threshold_for(s, req.target.device_kind)
            if threshold is None:  # 임계값이 없는 종류의 rental 기기는 허가하지 않는다 (FR-17)
                self._audit_step(Step.PROXIMITY, iid, {"node_id": node_id, "threshold": None})
                raise _Stop(Step.PROXIMITY, self._decision(
                    req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT, node_id=node_id))
            if run.posterior is None:  # 상위 요청 하나에 한 번이다. 후보는 ②의 전체 후보다
                run.posterior = self._proximity.posterior(
                    pick.beacons, pick.candidates, req.received_at, place_nodes=pick.place_nodes)
                self._apply_posterior(run.applied, run.posterior)
            self._apply_posterior(applied, run.posterior)
            p = run.posterior.posteriors.get(node_id, 0.0)
            self._audit_step(Step.PROXIMITY, iid, {
                "node_id": node_id, "p": p, "threshold": threshold,
                "model_version": run.posterior.model_version, "calib_version": run.posterior.calib_version})
            if p < threshold:
                raise _Stop(Step.PROXIMITY, self._decision(
                    req, iid, Verdict.ASK, ReasonCode.LOW_CONFIDENCE, node_id=node_id, p_max=p))
        applied.arguments = self._resolve_action(req, iid, node, p)
        applied.reoperate = is_reoperate(node, req.action)
        check = self._check_policy(req, iid, applied, node_id, p, req.received_at)
        return self._reserve(req, iid, applied, node, p, check)

    @staticmethod
    def _apply_posterior(applied: _Applied, result: ProximityResult) -> None:
        applied.model_version, applied.calib_version = result.model_version, result.calib_version
        applied.posteriors = dict(result.posteriors)
        applied.window_start, applied.window_end = result.window_start, result.window_end

    def _cancel_siblings(
        self, children: Sequence[_Child], failed: Decision, source: str = "selector_sibling"
    ) -> None:
        """처음 걸린 기기 때문에 앞 하위 요청의 예약과 승인 사용분을 미전송으로 취소한다.

        취소가 실패해도 응답은 바뀌지 않는다. 남은 예약은 재시작 복원이 만료한다 (`_cancel_reservation` 과 같다).
        `source` 는 취소 근거의 출처다. 묶음 요청은 `batch_sibling` 이다 (#284).
        """
        if self._store is None:
            return
        evidence = {"source": source, "reason": failed.reason.value, "node_id": failed.node_id}
        for child in children:
            if child.decision.verdict is not Verdict.OK:
                continue
            child.cancelled = True  # 오류 뒤 정리에서 다시 취소하지 않는다
            try:
                result = self._store.cancel_not_sent(
                    child.iid, ExecutionStatus.PENDING, failed.reason, evidence)
            except Exception:  # noqa: BLE001
                log.exception("거리 선택자의 옆 기기 때문에 하는 예약 취소 실패")
                continue
            if result.outcome is StoreOutcome.CONFLICT:
                log.warning("거리 선택자의 옆 기기 때문에 하는 예약 취소 충돌: %s", child.iid)

    def _selector_failed(
        self, req: UseRequest, iid: str, exc: Exception, run: _SelectorRun
    ) -> SelectorDecision:
        """판정 중 예외. 만든 하위 요청의 예약을 취소하고 INTERNAL_ERROR 로 거부한다."""
        # 오류 판정을 저장하기 전에 취소한다 (#80). 옆 기기 때문에 이미 취소한 하위 요청은 다시 취소하지 않는다
        for child in run.children:
            if child.applied.reserve_called and not child.cancelled:
                self._cancel_reservation(child.iid)
        for child in run.children:
            if child.saved:
                continue
            try:
                result = self._repo.save_decision(
                    self._decision(req, child.iid, Verdict.DENY, ReasonCode.INTERNAL_ERROR, node_id=child.node_id),
                    DecisionRecord(settings=self._settings))
            except Exception:  # noqa: BLE001
                log.exception("하위 요청의 거부 판정 저장 실패")
                continue
            if result.outcome is StoreOutcome.CONFLICT:
                log.warning("하위 요청의 거부 판정 저장 충돌: %s", child.iid)
        return SelectorDecision(self._selector_view(
            self._internal_error(req, iid, Step.EXEC, exc, save=not run.parent_conflict)))

    def _resend(self, iid: str, previous: Decision) -> SelectorDecision:
        """재전송에는 저장된 상위·하위 판정을 돌려준다. 새 하위 요청도 예약도 인계도 만들지 않는다 (FR-22)."""
        found = self._repo.selector_children(iid)
        if found is None:  # 거리를 정하기 전에 끝난 판정이다
            if previous.verdict is Verdict.OK:  # OK 상위 판정은 거리 선택자 기록과 하위 요청을 가진다
                raise ValueError("OK 판정에 거리 선택자 기록이 없다")
            return SelectorDecision(self._selector_view(previous))
        record = found.record
        if previous.reason is ReasonCode.ORDER_UNCERTAIN:  # 하위 요청 없이 끝났다. 처음 응답처럼 고른 개수는 0이다 (spec 결정 30)
            return SelectorDecision(self._selector_view(previous, (), 0, record.similar))
        stored = [(c, c.decision) for c in found.children if c.decision is not None]
        if previous.verdict is Verdict.OK:
            if len(stored) != len(record.selected) or any(d.verdict is not Verdict.OK for _, d in stored):
                raise ValueError("OK 판정의 하위 요청이 고른 기기와 맞지 않는다")
            devices = tuple(DeviceOutcome(c.node_id, d.verdict, d.reason, d.use_id) for c, d in stored)
            return SelectorDecision(
                self._selector_view(previous, devices, len(record.selected), record.similar),
                children=tuple(d for _, d in stored))
        first = next(((c, d) for c, d in stored if d.verdict is not Verdict.OK), None)
        devices = () if first is None else (DeviceOutcome(first[0].node_id, first[1].verdict, first[1].reason, None),)
        return SelectorDecision(self._selector_view(previous, devices, len(record.selected), record.similar))

    @staticmethod
    def _selector_view(
        d: Decision, devices: Sequence[DeviceOutcome] = (), selected_count: int = 0, similar: bool = False
    ) -> Decision:
        """거리 선택자 요청의 판정 모양으로 채운다. 상위 판정의 node_id·use_id·p_max 는 None 이다 (FR-09)."""
        return replace(d, devices=tuple(devices), selected_count=selected_count, similar=similar)

    # ── 묶음 요청 (FR-09, #284) ─────────────────────────────────────

    def decide_batch(self, req: UseRequest) -> SelectorDecision:
        """묶음 요청을 상위 요청 하나와 항목마다 고른 기기의 하위 요청으로 판정한다.

        항목 순서대로 대상을 고른다. 거리 선택자 항목은 `decide_selector` 처럼 상위 요청에서 고르고 고른 기기마다,
        그 밖의 항목(근접 지정, 전역 할당, 명시 지정)은 단일 요청의 ①②③ 으로 기기 하나를 고른 뒤 그 기기의
        하위 요청 하나에서 나머지를 한다. 하위 요청은 늘 고른 기기를 가진다.
        앞 항목이 고른 기기를 뒤 항목이 다시 고르면 거부한다. 처음 걸린 곳에서 멈추고 앞 하위 요청의 예약을
        취소하며 아무것도 인계하지 않는다. 실행 인계 context 만 돌려준다. 하위 요청의 실행과 결과 묶음은 부른 쪽이 한다.
        재전송에는 저장된 상위·하위 판정을 돌려준다. 현재 시각을 읽지 않고 `req.received_at` 만 쓴다.
        """
        if not req.target.is_batch:
            raise ValueError(f"묶음 요청이 아니다: {req.target.serialize()!r}")
        iid = ""
        try:
            digest = input_digest(req)
            link = self._repo.link_request(req, digest)
            iid = link.internal_request_id
            if link.conflict:
                d = self._decision(req, iid, Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
                self._audit_step(Step.DEDUP, iid, {"result": d.reason.value})
                return SelectorDecision(self._selector_view(d))  # 기존 판정을 덮어쓰지 않는다
            if not link.is_new:
                if link.previous is not None:
                    return self._resend_batch(req, iid, link.previous)
                # 아직 판정 중이다. 오류가 아니므로 다시 판정하지도, 저장하지도 않는다
                d = self._decision(req, iid, Verdict.BUSY, ReasonCode.REQUEST_IN_PROGRESS)
                self._audit_step(Step.DEDUP, iid, {"result": "IN_PROGRESS"})
                return SelectorDecision(self._selector_view(d))
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return SelectorDecision(self._selector_view(
                self._internal_error(req, iid, Step.DEDUP, exc, save=False)))

        run = _SelectorRun()
        try:
            return self._judge_batch(req, iid, digest, link.capacity_exceeded, run)
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return self._selector_failed(req, iid, exc, run)

    def _judge_batch(
        self, req: UseRequest, iid: str, digest: str, capacity_exceeded: bool, run: _SelectorRun
    ) -> SelectorDecision:
        """새 묶음 요청의 판정과 저장. 예외는 `_selector_failed` 가 정리한다."""
        judged: list[_BatchItem] = []  # 판정을 시작한 항목들. 요청의 항목 순서에서 앞쪽부터다
        detail: dict[str, Any] = {}
        devices: tuple[DeviceOutcome, ...] = ()
        outcomes: tuple[Decision, ...] = ()
        contexts: tuple[ExecutionContext, ...] = ()
        try:
            if capacity_exceeded:
                parent = self._decision(req, iid, Verdict.DENY, ReasonCode.CAPACITY_EXCEEDED)
                step, detail = Step.DEDUP, {"capacity_exceeded": True}
            elif len(req.items) > self._settings.batch_max_items:  # 하위 요청을 만들기 전이다 (spec 결정 21)
                parent = self._decision(req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT)
                step = Step.CANDIDATE
                detail = {"items": len(req.items), "batch_max_items": self._settings.batch_max_items}
            else:
                failed = self._judge_items(req, iid, digest, run, judged)
                if failed is None:
                    parent = self._decision(req, iid, Verdict.OK, ReasonCode.OK)
                    step = Step.DEVICE_OCCUPANCY
                    made = [(entry, child) for entry in judged for child in entry.children]
                    outcomes = tuple(child.decision for _, child in made)
                    devices = tuple(DeviceOutcome(child.node_id, child.decision.verdict, child.decision.reason,
                                                  child.decision.use_id, item=entry.index)
                                    for entry, child in made)
                    contexts = tuple(self._context(entry.req, child.iid, child.decision, child.applied)
                                     for entry, child in made)
                    if None in contexts:  # OK 인 하위 요청은 인계 context 가 있어야 한다
                        raise ValueError("OK 인 하위 요청의 실행 인계를 만들지 못했다")
                else:
                    f = failed.decision
                    self._cancel_siblings(run.children, f, "batch_sibling")
                    parent = self._decision(req, iid, f.verdict, f.reason)
                    step = failed.step
                    # 걸린 하위 요청은 마지막 항목의 것이고, 하위 요청은 늘 고른 기기를 가진다 (spec 결정 13)
                    devices = (DeviceOutcome(failed.node_id, f.verdict, f.reason, None, item=judged[-1].index),)
        except _Stop as stop:  # 하위 요청을 만들기 전에 멈췄다(항목의 고르기). 앞 항목의 예약을 취소한다
            self._cancel_siblings(run.children, stop.decision, "batch_sibling")
            # 상위 판정은 그 판정과 사유만 가진다. 고르다 멈춘 기기와 확률은 담지 않는다
            parent = self._decision(req, iid, stop.decision.verdict, stop.decision.reason)
            step = stop.step

        # 묶음 기록은 요청의 항목을 모두 담는다. 판정하지 않은 항목은 받은 요청뿐이다 (spec 결정 15, 27)
        batch = self._batch_record(req, judged)
        # selected_count 는 항목마다 실제로 고른 기기 수의 합이다. 만든 하위 요청 수가 아니다 (spec 결정 11).
        # similar 는 거리 선택자 항목 하나라도 비슷하면 참이다 (spec 결정 12)
        selected_count = sum(self._item_selected_count(item) for item in batch.items)
        similar = any(entry.pick is not None and entry.pick.similar for entry in judged)
        parent = self._selector_view(parent, devices, selected_count, similar)
        self._audit_step(step, iid, {
            "verdict": parent.verdict.value, "reason": parent.reason.value,
            "settings_version": self._settings.version, "children": len(run.children), **detail})
        # 상위 판정 기록은 근접 추정 값을 담지 않는다. 하위 요청의 기록이 담는다 (spec 결정 19)
        record = DecisionRecord(settings=self._settings, batch=batch)
        saved = self._repo.save_decision(parent, record)
        if saved.outcome is StoreOutcome.CONFLICT:
            run.parent_conflict = True
            raise _SaveConflict("같은 내부 요청에 다른 판정 기록이 있다")
        return SelectorDecision(parent, outcomes, contexts)

    def _judge_items(
        self, req: UseRequest, iid: str, digest: str, run: _SelectorRun, judged: list[_BatchItem]
    ) -> _Child | None:
        """항목 순서대로 대상을 고르고 하위 요청을 판정한다. 처음 걸린 하위 요청을 반환하고 모두 통과하면 None 이다.

        하위 요청을 만들기 전에 멈추면(항목의 고르기: ①②③, 거리 선택자 항목의 고를 기기 없음, 경계 확률) `_Stop` 이 그대로 올라간다.
        `taken` 은 지금까지 통과한 하위 요청의 기기다. 뒤 항목이 다시 고르면 그 하위 요청을 거부한다 (spec 결정 8).
        """
        taken: set[str] = set()
        for index, item in enumerate(req.items, start=1):
            entry = _BatchItem(index, replace(
                req, target=item.target, action=item.action, arguments=item.arguments, items=()))
            judged.append(entry)
            failed = (self._judge_selector_item(entry, iid, digest, run, taken) if item.target.is_selector
                      else self._judge_item_child(entry, iid, digest, run, taken))
            if failed is not None:
                return failed
            taken.update(child.node_id for child in entry.children)
        return None

    def _judge_selector_item(
        self, entry: _BatchItem, iid: str, digest: str, run: _SelectorRun, taken: set[str]
    ) -> _Child | None:
        """거리 선택자 항목. 고르기는 상위 요청 ID 로 감사하고, 고른 기기마다 하위 요청을 만들어 판정한다 (spec 결정 7).

        사후확률은 항목마다 한 번 부른다. 하위 요청 목록은 묶음 전체가 나눠 쓴다 (spec 결정 18).
        처음 걸린 하위 요청을 반환한다. 하위 요청을 만들기 전에 멈추면 `_Stop` 이다.
        """
        req = entry.req
        item_run = _SelectorRun(children=run.children)
        pick = self._pick(req, iid)
        entry.pick = pick  # 멈춰도 거리 순서는 기록에 남긴다
        if not pick.selected:
            raise _Stop(Step.PROXIMITY, self._decision(req, iid, Verdict.DENY, ReasonCode.NO_CANDIDATE))
        lowest = pick.boundary_probability
        if lowest is not None and lowest < self._settings.order_boundary_min_prob:
            raise _Stop(Step.PROXIMITY, self._decision(req, iid, Verdict.ASK, ReasonCode.ORDER_UNCERTAIN))
        by_id = {n.node_id: n for n in pick.candidates}
        for node_id in pick.selected:
            child = self._link_batch_child(iid, entry, node_id, digest, run)
            try:
                if node_id in taken:
                    raise self._duplicate_stop(req, child.iid, node_id)
                decision, step = self._judge_child(req, child, by_id[node_id], pick, item_run)
            except _Stop as stop:
                decision, step = stop.decision, stop.step
            if not self._settle_child(child, decision, step, iid):
                return child
        return None

    def _judge_item_child(
        self, entry: _BatchItem, iid: str, digest: str, run: _SelectorRun, taken: set[str]
    ) -> _Child | None:
        """근접 지정·전역 할당·명시 지정 항목. 단일 요청의 ①②③ 으로 기기를 고른 뒤 그 기기의 하위 요청에서 나머지를 한다 (spec 결정 7).

        고르기(`_choose`)는 상위 요청 ID 로 감사한다. 멈추면 하위 요청 없이 `_Stop` 이 그대로 올라가 묶음이 멈춘다.
        예외는 전역 할당 항목이 ③ 안의 ④ 에서 걸린 경우다. 그 판정은 기기를 가지므로 그 기기로 하위 요청을 만들어 판정을 저장한다 —
        호스트가 `devices` 의 기기로 `request_access` 를 부를 수 있다 (spec 결정 32).
        고른 기기로 하위 요청 행을 만들고, 나머지(`_finish`: 같은 기기 검사, 동작·인자 검사, ④, ⑤⑥)는 하위 요청 ID 로 감사한다.
        하위 요청의 판정 기록이 고르기에서 정한 근접 추정 값을 담는다. 걸린 하위 요청을 반환하고 통과하면 None 이다.
        """
        applied = _Applied()
        try:
            node, p, check = self._choose(entry.req, iid, applied)
        except _Stop as stop:
            if stop.decision.node_id is None:
                raise
            child = self._link_batch_child(iid, entry, stop.decision.node_id, digest, run, applied)
            self._settle_child(child, replace(stop.decision, internal_request_id=child.iid), stop.step, iid)
            return child
        child = self._link_batch_child(iid, entry, node.node_id, digest, run, applied)
        try:
            decision, step = self._finish(entry.req, child.iid, child.applied, node, p, check, taken)
        except _Stop as stop:
            decision, step = stop.decision, stop.step
        return None if self._settle_child(child, decision, step, iid) else child

    def _link_batch_child(
        self, iid: str, entry: _BatchItem, node_id: str, digest: str, run: _SelectorRun,
        applied: _Applied | None = None,
    ) -> _Child:
        """고른 기기의 하위 요청 행을 만들고 항목과 묶음 전체의 하위 요청 목록에 붙인다. `applied` 는 고르기에서 정한 값이다."""
        child = _Child(self._repo.link_selector_child(iid, entry.req, node_id, digest), node_id,
                       _Applied() if applied is None else applied)
        run.children.append(child)
        entry.children.append(child)
        return child

    def _settle_child(self, child: _Child, decision: Decision, step: Step, iid: str) -> bool:
        """하위 요청의 판정을 감사하고 저장한다. 통과했으면 True 다. 저장이 충돌하면 `_SaveConflict` 다."""
        child.decision, child.step = decision, step
        self._audit_step(step, child.iid, {
            "verdict": decision.verdict.value, "reason": decision.reason.value,
            "settings_version": self._settings.version, "batch_parent": iid})
        saved = self._repo.save_decision(decision, self._record(child.applied))
        if saved.outcome is StoreOutcome.CONFLICT:
            raise _SaveConflict("같은 내부 요청에 다른 판정 기록이 있다")
        child.saved = True
        return decision.verdict is Verdict.OK

    def _duplicate_stop(self, req: UseRequest, iid: str, node_id: str, p: float | None = None) -> _Stop:
        """앞 항목이 이미 고른 기기를 다시 골랐다. ② 후보 감사를 남기고 그 하위 요청의 멈춤을 반환한다 (spec 결정 8)."""
        self._audit_step(Step.CANDIDATE, iid, {"duplicate": node_id})
        return _Stop(Step.CANDIDATE, self._decision(
            req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT, node_id=node_id, p_max=p))

    @staticmethod
    def _item_record(entry: _BatchItem) -> BatchItemRecord:
        """항목의 판정 기록. 하위 요청과 고른 기기는 만든 만큼이고, 거리 선택자 항목은 고르기 기록도 담는다 (spec 결정 15)."""
        target = entry.req.target.serialize()
        children = tuple(child.iid for child in entry.children)
        nodes = tuple(child.node_id for child in entry.children)
        pick = entry.pick
        selector = None if pick is None else SelectorRecord(
            target, pick.order, pick.values, pick.selected, pick.similar, children,
            counts=pick.counts, boundary_probability=pick.boundary_probability)
        return BatchItemRecord(target, entry.req.action, entry.req.arguments, children, selector, nodes)

    def _batch_record(self, req: UseRequest, judged: Sequence[_BatchItem]) -> BatchRecord:
        """상위 판정 기록의 묶음 기록. 요청의 항목을 모두 항목 순서대로 담는다 (spec 결정 15, 27).

        판정을 시작한 항목(`judged`. 요청의 항목 순서에서 앞쪽부터)은 판정한 값을 담고, 판정하지 않은 뒤 항목은
        받은 대상·동작·인자만 담는다. 하위 요청·기기·거리 선택자 기록이 없다.
        """
        rest = (BatchItemRecord(item.target.serialize(), item.action, item.arguments)
                for item in req.items[len(judged):])
        return BatchRecord((*(self._item_record(entry) for entry in judged), *rest))

    @staticmethod
    def _item_selected_count(item: BatchItemRecord) -> int:
        """항목이 실제로 고른 기기 수 (FR-09, spec 결정 11). 첫 판정과 재전송이 같은 기록으로 센다.

        거리 선택자 항목은 하위 요청을 하나라도 만들었으면 고른 기기 전체다. 중간 기기에서 걸려 뒤 기기의 하위 요청이
        없어도 같다 — 거리 선택자 요청 하나와 같다. 하위 요청 전에 멈춘 항목(`NO_CANDIDATE`, `ORDER_UNCERTAIN`)은 0이다.
        다른 항목은 만든 하위 요청 수(0 또는 1)다. 판정하지 않은 항목은 0이다.
        """
        if item.selector is not None and item.children:
            return len(item.selector.selected)
        return len(item.children)

    @staticmethod
    def _check_resent_batch(req: UseRequest, found: BatchChildren) -> None:
        """`OK` 로 저장된 묶음 기록이 요청과 맞는지 본다. 하나라도 어긋나면 ValueError 다 (spec 결정 17).

        항목 수와 항목마다 대상·동작·인자가 요청과 같아야 한다. 거리 선택자 항목은 거리 선택자 기록이 있고 하위 요청이
        고른 기기마다 하나다. 다른 항목은 거리 선택자 기록이 없고 하위 요청이 하나다. 하위 판정이 하위 요청마다 있고 모두 OK 다.
        """
        if len(found.items) != len(req.items):
            raise ValueError("OK 판정의 묶음 기록의 항목 수가 요청과 맞지 않는다")
        for index, (entry, item) in enumerate(zip(found.items, req.items, strict=True), start=1):
            record = entry.record
            if (record.target_spec, record.action, record.arguments) != (
                    item.target.serialize(), item.action, item.arguments):
                raise ValueError(f"OK 판정의 묶음 기록의 {index}번째 항목이 요청과 맞지 않는다")
            if item.target.is_selector:
                made = record.selector is not None and len(record.children) == len(record.selector.selected)
            else:
                made = record.selector is None and len(record.children) == 1
            if not made:
                raise ValueError(f"OK 판정의 {index}번째 항목의 하위 요청이 고른 기기와 맞지 않는다")
            if any(c.decision is None or c.decision.verdict is not Verdict.OK for c in entry.children):
                raise ValueError(f"OK 판정의 {index}번째 항목의 하위 판정이 없거나 OK 가 아니다")

    def _resend_batch(self, req: UseRequest, iid: str, previous: Decision) -> SelectorDecision:
        """재전송에는 저장된 상위·하위 판정을 항목 순서로 돌려준다. 새 하위 요청도 예약도 인계도 만들지 않는다 (FR-22).

        `OK` 로 저장된 상위 판정은 묶음 기록이 요청과 맞아야 한다. 맞지 않으면 ValueError 다 (spec 결정 17).
        """
        found = self._repo.batch_children(iid)
        if found is None:  # 항목을 판정하기 전에 끝난 판정이다
            if previous.verdict is Verdict.OK:  # OK 상위 판정은 묶음 기록과 하위 요청을 가진다
                raise ValueError("OK 판정에 묶음 기록이 없다")
            return SelectorDecision(self._selector_view(previous))
        # 판정이 없는 하위 요청은 뺀다. OK 판정이면 `_check_resent_batch` 가 하위 판정이 모두 있음을 확인한다
        stored = [(index, c, c.decision) for index, entry in enumerate(found.items, start=1)
                  for c in entry.children if c.decision is not None]
        # 첫 판정과 같은 규칙으로 센다. 판정하지 않은 항목은 하위 요청이 없어 0이다
        selected_count = sum(self._item_selected_count(entry.record) for entry in found.items)
        similar = any(entry.record.selector is not None and entry.record.selector.similar for entry in found.items)
        if previous.verdict is Verdict.OK:
            self._check_resent_batch(req, found)
            devices = tuple(DeviceOutcome(c.node_id, d.verdict, d.reason, d.use_id, item=index)
                            for index, c, d in stored)
            return SelectorDecision(
                self._selector_view(previous, devices, selected_count, similar),
                children=tuple(d for _, _, d in stored))
        first = next(((index, c, d) for index, c, d in stored if d.verdict is not Verdict.OK), None)
        devices = ()
        if first is not None:  # 걸린 하위 요청은 늘 기기를 가진다
            index, c, d = first
            devices = (DeviceOutcome(c.node_id, d.verdict, d.reason, None, item=index),)
        return SelectorDecision(self._selector_view(previous, devices, selected_count, similar))

    # ── ①~⑥ ───────────────────────────────────────────────────────

    def _run(
        self, req: UseRequest, iid: str, applied: _Applied, taken: frozenset[str] | set[str] = frozenset()
    ) -> tuple[Decision, Step, dict[str, Any]]:
        """단일 요청의 ①~⑥. 기기를 고르는 `_choose` 와 나머지를 마치는 `_finish` 를 같은 `iid`·`applied` 로 이어 부른다 (FR-18).

        `taken` 은 묶음 요청에서 앞 항목이 이미 고른 기기다. 단일 요청은 비어 있다 (FR-09, #284).
        """
        node, p, check = self._choose(req, iid, applied)
        decision, step = self._finish(req, iid, applied, node, p, check, taken)
        return decision, step, {}

    def _choose(
        self, req: UseRequest, iid: str, applied: _Applied
    ) -> tuple[NodeInfo, float | None, PolicyCheckResult | None]:
        """단일 요청의 ①②③. 기기 하나를 골라 (기기, 근접 확률, 정책 검사 결과)를 반환한다 (FR-18).

        전역 할당은 ③ 안에서 ④ 정책 검사와 대기 구간 배정을 하므로 정책 검사 결과를 함께 돌려준다. 다른 요청은 None 이다.
        걸리면 `_Stop` 이다. 감사는 받은 `iid` 로 남기고, 근접 추정 값은 `applied` 에 담는다.
        묶음 요청의 근접 지정·전역 할당·명시 지정 항목은 상위 요청 ID 로 부르고, 고른 기기의 하위 요청이 `_finish` 를 한다 (#284).
        """
        s = self._settings
        now = req.received_at

        beacons, all_nodes, place_id = self._see_beacon(req, iid)

        # ② 후보 — 비콘을 본 장소 안에서 고른다
        any_kind = req.target.kind is TargetKind.ANY
        # 전역 할당은 `any_allowed_kinds` 의 종류만 받는다 (FR-23)
        if any_kind and (self._window is None or req.target.device_kind not in s.any_allowed_kinds):
            self._audit_step(Step.CANDIDATE, iid, {"any_allowed_kinds": list(s.any_allowed_kinds)})
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT))
        explicit = req.target.kind is TargetKind.EXPLICIT
        threshold = None if explicit else threshold_for(s, req.target.device_kind)
        if not explicit and threshold is None:  # 근접 지정과 전역 할당은 `threshold_{kind}` 가 필요하다 (FR-17)
            self._audit_step(Step.CANDIDATE, iid, {"threshold": None, "explicit": explicit})
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT))
        kind = target_kind(req, all_nodes)
        candidates = select_candidates(
            all_nodes, kind, s, now, place_id, self._readiness.can_execute)
        if explicit and kind is None:
            candidates = []  # 없는 기기다. 다른 종류의 기기를 후보로 보지 않는다
        self._audit_step(Step.CANDIDATE, iid, {"place_id": place_id,
                                              "candidates": [n.node_id for n in candidates],
                                              "online_ttl_sec": s.online_ttl_sec})
        by_id = {n.node_id: n for n in candidates}
        if explicit and req.target.node_id not in by_id:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE))
        if not candidates:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.NO_CANDIDATE))

        # ③ 근접 — 명시 지정은 ①의 장소 확인과 ②의 후보 검사 뒤 확률 계산을 생략한다.
        check: PolicyCheckResult | None = None
        p: float | None
        if explicit:
            node_id = req.target.node_id
            p = None
            self._audit_step(Step.PROXIMITY, iid, {"skipped": True, "reason": "explicit_target",
                                                  "node_id": node_id})
        else:
            place_nodes = [n for n in all_nodes if n.place_id == place_id]
            result = self._proximity.posterior(beacons, candidates, now, place_nodes=place_nodes)
            posteriors = dict(result.posteriors)
            applied.model_version, applied.calib_version = result.model_version, result.calib_version
            applied.posteriors = posteriors
            applied.window_start, applied.window_end = result.window_start, result.window_end
            prox_detail = {"posteriors": posteriors, "threshold": threshold,
                           "model_version": result.model_version, "calib_version": result.calib_version,
                           "unzeroed": sorted(result.unzeroed),
                           "conservative": dict(sorted(result.conservative.items()))}
            self._audit_step(Step.PROXIMITY, iid, {**prox_detail, "best": result.best})
            if any_kind:
                node_id, p, check = self._assign_any(req, iid, applied, by_id, result, threshold, now)
            else:
                # 1등은 명목 사후확률로 고르고 2등을 고르지 않는다. 임계값은 1등의 `threshold_probability`로 본다.
                # 1등에 자동 영점이 없으면 보수적 사후확률이 임계값 이상일 때만 진행한다 (FR-17)
                best = result.best
                p_top = None if best is None else result.threshold_probability(best[0])
                if best is None or p_top < threshold or best[0] not in by_id:
                    raise _Stop(Step.PROXIMITY, self._decision(
                        req, iid, Verdict.ASK, ReasonCode.LOW_CONFIDENCE, p_max=p_top))
                node_id, p = best[0], p_top
        return by_id[node_id], p, check

    def _finish(
        self, req: UseRequest, iid: str, applied: _Applied, node: NodeInfo, p: float | None,
        check: PolicyCheckResult | None, taken: frozenset[str] | set[str] = frozenset(),
    ) -> tuple[Decision, Step]:
        """고른 기기에 대한 단일 요청의 나머지: 같은 기기 검사, 동작·인자 검사, ④ 정책, ⑤⑥ (FR-18).

        `check` 가 있으면(전역 할당이 ③ 안에서 ④ 를 마쳤다) ④ 를 다시 하지 않고 그 결과를 예약에 넘긴다.
        `taken` 은 묶음 요청에서 앞 항목이 이미 고른 기기다. 같은 기기면 동작·인자 검사 전에 거부한다.
        통과하면 (OK 판정, 마지막 단계)를 반환하고 걸리면 `_Stop` 이다. 감사는 받은 `iid` 로 남긴다.
        """
        node_id = node.node_id
        if node_id in taken:  # 앞 항목이 이미 고른 기기다. 동작·인자 검사 전에 거부한다. 전역 할당은 배정 뒤다 (spec 결정 8)
            raise self._duplicate_stop(req, iid, node_id, p)
        applied.arguments = self._resolve_action(req, iid, node, p)
        applied.reoperate = is_reoperate(node, req.action)

        # ④ 정책 — 확정한 대상만 검사한다. `any:` 요청은 배정 전에 후보마다 검사했다
        if check is None:
            check = self._check_policy(req, iid, applied, node_id, p, req.received_at)

        # ⑤⑥
        return self._reserve(req, iid, applied, node, p, check)

    # ── 보조 ────────────────────────────────────────────────────────

    def _see_beacon(
        self, req: UseRequest, iid: str
    ) -> tuple[Sequence[BeaconKey], Sequence[NodeInfo], str]:
        """① 비콘. (활성 비콘, 전체 기기, 비콘을 본 장소)를 반환한다. 못 봤으면 BEACON_NOT_SEEN 으로 멈춘다."""
        s = self._settings
        now = req.received_at
        # 활성 비콘은 0 또는 1 개다 (#42)
        beacons = self._repo.beacons_of(req.user_id)
        if len(beacons) > 1:
            raise ValueError("활성 비콘이 둘 이상이다")
        frames = (
            self._repo.rssi_since(beacons, now - timedelta(seconds=s.beacon_recent_sec))
            if beacons
            else ()
        )
        all_nodes: Sequence[NodeInfo] = self._repo.nodes() if frames else ()
        by_node = {n.node_id: n for n in all_nodes}
        anchors = {f.node_id for f in frames
                   if f.node_id in by_node and by_node[f.node_id].trust_state is TrustState.TRUSTED}
        self._audit_step(Step.BEACON, iid, {"beacons": len(beacons), "frames": len(frames),
                                           "anchors": sorted(anchors),
                                           "beacon_recent_sec": s.beacon_recent_sec})
        if not anchors:
            raise _Stop(Step.BEACON, self._decision(req, iid, Verdict.DENY, ReasonCode.BEACON_NOT_SEEN))
        places = {by_node[a].place_id for a in anchors}
        if len(places) != 1 or None in places:
            raise ValueError(f"비콘을 본 기기의 장소가 하나로 정해지지 않는다: {sorted(map(str, places))}")
        return beacons, all_nodes, places.pop()

    def _reserve(
        self, req: UseRequest, iid: str, applied: _Applied, node: NodeInfo, p: float | None,
        check: PolicyCheckResult,
    ) -> tuple[Decision, Step]:
        """⑤⑥ — reserve_if_free() 한 번이 검사와 예약을 한다. 점유 방식별 분기는 DB 가 한다 (#27).

        통과하면 (OK 판정, 마지막 단계)를 반환하고 실패하면 그 판정으로 멈춘다.
        """
        node_id = node.node_id
        applied.reserve_called = True  # 호출이 예외로 끝나도 반영됐을 수 있다
        reserved = self._repo.reserve_if_free(ReserveRequest(
            internal_request_id=iid, user_id=req.user_id, node_id=node_id, action=req.action,
            approval_id=check.approval_id, release_policy=node.release_policy,
            reoperate_only=applied.reoperate))
        self._audit_step(Step.DEVICE_OCCUPANCY, iid, {"node_id": node_id,
                                                     "outcome": reserved.outcome.value})
        if reserved.outcome not in (ReserveOutcome.RESERVED, ReserveOutcome.REOPERATING):
            raise _Stop(Step.DEVICE_OCCUPANCY if reserved.outcome is ReserveOutcome.DEVICE_BUSY
                        else Step.USER_OCCUPANCY, self._reserve_failure(req, iid, node_id, p, reserved.outcome))
        applied.usage_id = reserved.usage_id
        applied.reserved_at = reserved.reserved_at
        applied.execution_kind = self._execution_kind(node, reserved.outcome, reserved.use_id)

        decision = self._decision(req, iid, Verdict.OK, ReasonCode.OK, node_id=node_id,
                                  use_id=reserved.use_id, p_max=p)
        return decision, (Step.USER_OCCUPANCY if reserved.use_id else Step.POLICY)

    def _resolve_action(
        self, req: UseRequest, iid: str, node: NodeInfo, p: float | None
    ) -> Mapping[str, Any] | None:
        """③ 대상 확정 뒤 기기 선언으로 동작과 인자를 검사하고 생략한 인자를 정한다 (FR-09, FR-18).

        다른 기기를 고르지 않는다. 범위 밖 값은 보정하지 않고 거부한다. 인자가 없으면 None 이다.
        생략한 인자는 같은 기기·같은 동작의 마지막 성공 명령, `param_default` 순서로 채우고 둘 다 없으면 생략한다.
        상한은 선언의 maximum 과 `param_max` 중 작은 값이다. 채운 값도 다시 검사한다.
        """
        spec = None if node.descriptor is None else node.descriptor.action(req.action)
        detail: dict[str, Any] = {"node_id": node.node_id, "action": req.action}
        if spec is None:
            self._action_stop(req, iid, node, p, ReasonCode.UNSUPPORTED_ACTION, detail)
        s = self._settings
        caps = s.param_caps(node.kind, req.action)
        given = None if req.arguments is None else dict(req.arguments)
        detail.update(given=given, param_max=dict(caps))
        self._argument_check(req, iid, node, p, validate_arguments(spec, given, caps=caps, partial=True), detail)
        resolved = dict(given or {})
        if any(name not in resolved for name in spec.properties):
            last = self._repo.last_success_arguments(node.node_id, req.action)
            defaults = s.param_defaults(node.kind, req.action)
            detail["last"] = None if last is None else dict(last)
            for name in spec.properties:
                for source in (last or {}, defaults):
                    if name not in resolved and name in source:
                        resolved[name] = source[name]
        resolved = normalize_arguments(spec, resolved)
        detail["resolved"] = resolved
        self._argument_check(req, iid, node, p, validate_arguments(spec, resolved, caps=caps), detail)
        if given is not None or spec.properties:  # 인자 없는 동작은 이전처럼 ③ 근접 감사만 남긴다
            self._audit_step(Step.PROXIMITY, iid, {"arguments": detail})
        return resolved or None

    def _argument_check(self, req: UseRequest, iid: str, node: NodeInfo, p: float | None,
                        problem: ArgumentProblem | None, detail: Mapping[str, Any]) -> None:
        if problem is ArgumentProblem.OUT_OF_RANGE:
            self._action_stop(req, iid, node, p, ReasonCode.PARAMETER_OUT_OF_RANGE, detail)
        if problem is not None:
            self._action_stop(req, iid, node, p, ReasonCode.INVALID_INPUT, detail)

    def _action_stop(self, req: UseRequest, iid: str, node: NodeInfo, p: float | None,
                     reason: ReasonCode, detail: Mapping[str, Any]) -> None:
        self._audit_step(Step.PROXIMITY, iid, {"arguments": {**detail, "result": reason.value}})
        raise _Stop(Step.PROXIMITY, self._decision(
            req, iid, Verdict.DENY, reason, node_id=node.node_id, p_max=p))

    def _check_policy(
        self, req: UseRequest, iid: str, applied: _Applied, node_id: str, p: float | None, now: datetime
    ) -> PolicyCheckResult:
        """④ 정책 검사 하나. OK 가 아니면 그 판정으로 멈춘다."""
        check = self._policy.check(req.user_id, node_id, req.action, now, internal_request_id=iid,
                                   reoperate=applied.reoperate)
        applied.policy_version = check.policy_version
        self._audit_step(Step.POLICY, iid, {"node_id": node_id, "verdict": check.verdict.value,
                                           "reason": check.reason.value,
                                           "policy_version": check.policy_version})
        if check.verdict is not Verdict.OK:
            if check.verdict not in (Verdict.NEED_APPROVAL, Verdict.DENY):
                raise ValueError(f"정책 검사의 알 수 없는 판정: {check.verdict!r}")
            raise _Stop(Step.POLICY, self._decision(
                req, iid, check.verdict, check.reason, node_id=node_id, p_max=p))
        return check

    def _assign_any(
        self, req: UseRequest, iid: str, applied: _Applied, by_id: Mapping[str, NodeInfo],
        result: ProximityResult, threshold: float, now: datetime,
    ) -> tuple[str, float, PolicyCheckResult]:
        """`any:{kind}` ③~④ 와 전역 할당 (FR-23). 대기 구간은 종류별로 따로 연다. 배정받은 (기기, 확률, 정책 검사)를 반환한다.

        허용쌍은 ③ 임계값, ④ 정책 OK, 점유(AVAILABLE)를 모두 만족한 쌍이다. 승인이 필요한 기기는 뺀다.
        ③ 확률은 `threshold_probability`다. 자동 영점이 없는 기기(`unzeroed`)는 보수적 사후확률이 임계값 이상일 때만 허용쌍에 넣고,
        배정 비용(−log p)도 그 값으로 낸다. 명목 1등에 자동 영점이 없고 그 보수적 사후확률이 임계값 미만이면
        다른 기기를 배정하지 않고 ASK 다 (`p_max`는 그 보수적 값).
        """
        if self._window is None:
            raise ValueError("대기 구간이 없다")
        best = result.best
        p_top = None if best is None else result.threshold_probability(best[0])
        if best is not None and best[0] in result.unzeroed and p_top < threshold:
            raise _Stop(Step.PROXIMITY, self._decision(
                req, iid, Verdict.ASK, ReasonCode.LOW_CONFIDENCE, p_max=p_top))
        qualified = sorted(
            (-result.threshold_probability(nid), nid) for nid in by_id
            if result.threshold_probability(nid) >= threshold)
        if not qualified:
            raise _Stop(Step.PROXIMITY, self._decision(
                req, iid, Verdict.ASK, ReasonCode.LOW_CONFIDENCE, p_max=p_top))

        passed: list[tuple[str, float, PolicyCheckResult]] = []
        first_failure: tuple[str, float, PolicyCheckResult] | None = None
        for neg_p, nid in qualified:
            p = -neg_p
            check = self._policy.check(req.user_id, nid, req.action, now, internal_request_id=iid,
                                       reoperate=is_reoperate(by_id[nid], req.action))
            self._audit_step(Step.POLICY, iid, {"node_id": nid, "verdict": check.verdict.value,
                                               "reason": check.reason.value,
                                               "policy_version": check.policy_version})
            if check.verdict is Verdict.OK:
                passed.append((nid, p, check))
            elif check.verdict in (Verdict.NEED_APPROVAL, Verdict.DENY):
                first_failure = first_failure or (nid, p, check)
            else:
                raise ValueError(f"정책 검사의 알 수 없는 판정: {check.verdict!r}")
        if not passed:
            if first_failure is None:
                raise ValueError("정책 검사 결과가 없다")
            nid, p, check = first_failure  # 확률이 가장 큰 후보의 ④ 판정을 그대로 쓴다
            applied.policy_version = check.policy_version
            raise _Stop(Step.POLICY, self._decision(
                req, iid, check.verdict, check.reason, node_id=nid, p_max=p))

        allowed = [item for item in passed if by_id[item[0]].device_state is DeviceState.AVAILABLE]
        if not allowed:
            raise _Stop(Step.DEVICE_OCCUPANCY, self._decision(
                req, iid, Verdict.BUSY, ReasonCode.DEVICE_BUSY, p_max=passed[0][1]))
        assigned = self._window.submit(AllocationRequest(iid, {nid: p for nid, p, _ in allowed}),
                                       group=req.target.device_kind or "")
        self._audit_step(Step.DEVICE_OCCUPANCY, iid, {
            "allocation": {"allowed": [nid for nid, _, _ in allowed], "assigned": assigned}})
        if assigned is None:  # 다른 요청이 가져갔다
            raise _Stop(Step.DEVICE_OCCUPANCY, self._decision(
                req, iid, Verdict.BUSY, ReasonCode.DEVICE_BUSY, p_max=allowed[0][1]))
        chosen = next((item for item in allowed if item[0] == assigned), None)
        if chosen is None:
            raise ValueError(f"허용쌍에 없는 기기를 배정했다: {assigned!r}")
        applied.policy_version = chosen[2].policy_version
        return chosen

    @staticmethod
    def _execution_kind(node: NodeInfo, outcome: ReserveOutcome, use_id: str | None) -> ExecutionKind:
        """실행 종류는 점유 방식과 점유 결과로 정한다 (#35). 어긋나면 불변 조건 위반이다."""
        if node.occupancy is Occupancy.SHARED:
            kind, ok = ExecutionKind.SHARED, use_id is None and outcome is ReserveOutcome.RESERVED
        elif outcome is ReserveOutcome.REOPERATING:
            kind, ok = ExecutionKind.RENTAL_REOPERATION, use_id is not None
        else:
            kind, ok = ExecutionKind.RENTAL_INITIAL, use_id is not None
        if not ok:
            raise ValueError(f"점유 결과가 기기의 점유 방식과 맞지 않는다: {node.occupancy}, {outcome}")
        return kind

    def _record(self, applied: _Applied) -> DecisionRecord:
        return DecisionRecord(
            settings=self._settings, model_version=applied.model_version,
            calib_version=applied.calib_version, policy_version=applied.policy_version,
            posteriors=applied.posteriors, window_start=applied.window_start,
            window_end=applied.window_end)

    def _context(
        self, req: UseRequest, iid: str, decision: Decision, applied: _Applied
    ) -> ExecutionContext | None:
        if decision.verdict is not Verdict.OK or applied.execution_kind is None:
            return None
        # 방문자의 인자 입력은 판정에서만 쓴다. 실행은 정한 값(`arguments`)만 쓰고, 결과 인계 때
        # 저장소가 다시 만드는 문맥과 같아야 하므로 요청에서 뺀다.
        return ExecutionContext(
            internal_request_id=iid, request=replace(req, arguments=None), node_id=decision.node_id,
            use_id=decision.use_id,
            usage_id=applied.usage_id, settings=self._settings, execution_kind=applied.execution_kind,
            reserved_at=applied.reserved_at, arguments=applied.arguments)

    @staticmethod
    def _reserve_failure(req, iid, node_id, p, outcome: ReserveOutcome) -> Decision:
        if outcome is ReserveOutcome.DEVICE_BUSY:
            return make_decision(req.request_id, iid, Verdict.BUSY, ReasonCode.DEVICE_BUSY, node_id=node_id, p_max=p)
        if outcome is ReserveOutcome.USER_BUSY:
            return make_decision(req.request_id, iid, Verdict.BUSY, ReasonCode.USER_BUSY, node_id=node_id, p_max=p)
        if outcome is ReserveOutcome.APPROVAL_EXHAUSTED:
            return make_decision(
                req.request_id, iid, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, node_id=node_id, p_max=p)
        if outcome is ReserveOutcome.USE_NOT_FOUND:
            return make_decision(req.request_id, iid, Verdict.DENY, ReasonCode.USE_NOT_FOUND, node_id=node_id, p_max=p)
        raise ValueError(f"알 수 없는 점유 결과: {outcome!r}")

    @staticmethod
    def _decision(req: UseRequest, iid: str, verdict: Verdict, reason: ReasonCode, **kw: Any) -> Decision:
        return make_decision(req.request_id, iid, verdict, reason, **kw)

    def _internal_error(self, req: UseRequest, iid: str, step: Step, exc: Exception, save: bool) -> Decision:
        """예외 내용은 감사 기록에만 남기고 호스트에는 일반 안내만 준다 (#25)."""
        log.exception("판정 중 예외")
        d = self._decision(req, iid, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
        if iid:
            try:
                self._audit_step(step, iid, {"result": "INTERNAL_ERROR", "error": repr(exc)})
            except Exception:  # noqa: BLE001
                log.exception("거부 감사 기록 실패")
            if save:
                try:
                    self._repo.save_decision(d, DecisionRecord(settings=self._settings))
                except Exception:  # noqa: BLE001
                    log.exception("거부 판정 저장 실패")
        return d

    def _cancel_reservation(self, iid: str) -> None:
        """예약을 부른 뒤 오류가 났으면 이번 요청의 예약과 승인 사용분을 바로 취소한다 (#80).

        취소가 실패해도 응답은 바뀌지 않는다. 남은 예약은 재시작 복원이 만료한다.
        """
        if self._store is None:
            return
        try:
            result = self._store.cancel_not_sent(
                iid, ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR,
                {"source": "decision_error", "reason": ReasonCode.INTERNAL_ERROR.value})
        except Exception:  # noqa: BLE001
            log.exception("판정 오류 뒤 예약 취소 실패")
            return
        if result.outcome is StoreOutcome.CONFLICT:
            log.warning("판정 오류 뒤 예약 취소 충돌: %s", iid)

    def _audit_step(self, step: Step, iid: str, payload: Mapping[str, Any]) -> None:
        self._audit.append(AuditEvent(
            event_id=self._new_id(), step=step, payload=dict(payload),
            internal_request_id=iid or None))
