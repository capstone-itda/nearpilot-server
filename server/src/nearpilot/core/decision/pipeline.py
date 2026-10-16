"""FR-18 결정론적 판정 파이프라인 ⓪~⑥.

단계를 부르는 순서와 멈추는 때만 정한다. 단계 내부의 계산은 상대 계층의 Protocol 이 한다.
기본값은 거부다. 예외, 누락, 알 수 없는 상태는 OK 가 되지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from nearpilot.core.allocation import AllocationRequest
from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Capability,
    DeviceState,
    ExecutionKind,
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
    PolicyChecker,
    ProximityEstimator,
    Repository,
)
from nearpilot.shared.models import (
    AuditEvent,
    Decision,
    DecisionRecord,
    ExecutionContext,
    NodeInfo,
    PolicyCheckResult,
    ReserveRequest,
    UseRequest,
)

log = logging.getLogger(__name__)

_GENERIC_MESSAGE = "요청을 처리하지 못했습니다."


def select_candidates(
    nodes: Sequence[NodeInfo], capability: Capability, settings: Settings, now: datetime,
    place_id: str | None = None, executable: Callable[[str], bool] | None = None,
) -> Sequence[NodeInfo]:
    """② 후보 조건. 최초 판정과 실행 직전 재검사가 같은 조건을 쓴다.

    `executable` 이 있으면 새 실행이 불가능한 기기(재시작 복원 중)를 뺀다 (#82).
    """
    ttl = timedelta(seconds=settings.online_ttl_sec)
    return [
        n for n in nodes
        if n.capability is capability
        and (place_id is None or n.place_id == place_id)
        and n.trust_state is TrustState.TRUSTED
        and n.device_state is not DeviceState.FAULT
        and n.last_seen_at is not None
        and now - n.last_seen_at <= ttl
        and (executable is None or executable(n.node_id))
    ]


def threshold_for(settings: Settings, capability: Capability, explicit: bool) -> float | None:
    """요청 종류와 명시 지정 여부로 임계값을 고른다. 설정에 없으면 None 이다."""
    name = f"threshold_explicit_{capability.value}" if explicit else f"threshold_{capability.value}"
    return getattr(settings, name, None)


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


class _SaveConflict(Exception):
    """같은 내부 요청에 다른 판정 기록이 이미 있다. 기존 기록을 덮어쓰지 않는다."""


class BatchAllocator(Protocol):
    """`any:` 요청을 대기 구간에 모아 배정한다 (`AllocationWindow`). 배정받은 기기 ID 또는 None."""

    def submit(self, request: AllocationRequest) -> str | None: ...


class _Stop(Exception):
    """단계가 실패해서 판정을 확정했다. 뒤 단계는 실행하지 않는다."""

    def __init__(self, step: Step, decision: Decision):
        super().__init__(decision.reason)
        self.step = step
        self.decision = decision


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
    ) -> None:
        self._repo = repo
        self._proximity = proximity
        self._policy = policy
        self._audit = audit
        self._settings = settings
        self._readiness = readiness
        self._window = window
        self._new_id = new_id

    # ── 진입점 ──────────────────────────────────────────────────────

    def decide(self, req: UseRequest) -> Decision:
        """⓪부터 ⑥까지 같은 순서로 검사하고 판정 하나를 반환한다."""
        return self.decide_for_execution(req)[0]

    def decide_for_execution(self, req: UseRequest) -> tuple[Decision, ExecutionContext | None]:
        """판정과 실행 인계 context 를 반환한다. context 는 이번에 새로 OK 가 난 요청에만 있다 (#16, #35)."""
        internal_id = ""
        if req.purpose != "use":
            return self._decision(req, internal_id, Verdict.DENY, ReasonCode.INVALID_INPUT), None
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
                d = self._decision(req, internal_id, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
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
            return self._internal_error(
                req, internal_id, Step.EXEC, exc, save=not isinstance(exc, _SaveConflict)), None
        return decision, self._context(req, internal_id, decision, applied)

    # ── ①~⑥ ───────────────────────────────────────────────────────

    def _run(
        self, req: UseRequest, iid: str, applied: _Applied
    ) -> tuple[Decision, Step, dict[str, Any]]:
        s = self._settings
        now = req.received_at

        # ① 비콘 — 활성 비콘은 0 또는 1 개다 (#42)
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
        place_id = places.pop()

        # ② 후보 — 비콘을 본 장소 안에서 고른다
        any_kind = req.target.kind is TargetKind.ANY
        if any_kind and (self._window is None or req.target.capability is not Capability.LOCKER):
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT))
        explicit = req.target.kind is TargetKind.EXPLICIT
        threshold = threshold_for(s, req.target.capability, explicit)
        if threshold is None:  # 설정이 없는 요청 종류는 실행을 허가하지 않는다
            self._audit_step(Step.CANDIDATE, iid, {"threshold": None, "explicit": explicit})
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT))
        candidates = select_candidates(
            all_nodes, req.target.capability, s, now, place_id, self._readiness.can_execute)
        self._audit_step(Step.CANDIDATE, iid, {"place_id": place_id,
                                              "candidates": [n.node_id for n in candidates],
                                              "online_ttl_sec": s.online_ttl_sec})
        by_id = {n.node_id: n for n in candidates}
        if explicit and req.target.node_id not in by_id:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE))
        if not candidates:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.NO_CANDIDATE))

        # ③ 근접 — 명시 지정에도 ② 의 전체 후보를 넘긴다 (#26). 장소 전체 기기도 넘긴다 (#39)
        place_nodes = [n for n in all_nodes if n.place_id == place_id]
        result = self._proximity.posterior(beacons, candidates, now, place_nodes=place_nodes)
        posteriors = dict(result.posteriors)
        applied.model_version, applied.calib_version = result.model_version, result.calib_version
        applied.posteriors = posteriors
        applied.window_start, applied.window_end = result.window_start, result.window_end
        prox_detail = {"posteriors": posteriors, "threshold": threshold,
                       "model_version": result.model_version, "calib_version": result.calib_version}
        check: PolicyCheckResult | None = None
        if explicit:
            node_id = req.target.node_id
            p = posteriors.get(node_id, 0.0)
            self._audit_step(Step.PROXIMITY, iid, {**prox_detail, "p": p})
            if p < threshold:
                raise _Stop(Step.PROXIMITY, self._decision(
                    req, iid, Verdict.ASK, ReasonCode.MOVE_CLOSER, node_id=node_id, p_max=p))
        elif any_kind:
            self._audit_step(Step.PROXIMITY, iid, {**prox_detail, "best": result.best})
            node_id, p, check = self._assign_any(
                req, iid, applied, by_id, posteriors, result.best, threshold, now)
        else:
            best = result.best
            self._audit_step(Step.PROXIMITY, iid, {**prox_detail, "best": best})
            if best is None or best[1] < threshold or best[0] not in by_id:
                p_max = best[1] if best else None
                raise _Stop(Step.PROXIMITY, self._decision(
                    req, iid, Verdict.ASK, ReasonCode.LOW_CONFIDENCE, p_max=p_max))
            node_id, p = best
        node = by_id[node_id]

        # ④ 정책 — 확정한 대상만 검사한다. `any:` 요청은 배정 전에 후보마다 검사했다
        if check is None:
            check = self._check_policy(req, iid, applied, node_id, p, now)

        # ⑤⑥ — reserve_if_free() 한 번이 검사와 예약을 한다. 점유 방식별 분기는 DB 가 한다 (#27)
        reserved = self._repo.reserve_if_free(ReserveRequest(
            internal_request_id=iid, user_id=req.user_id, node_id=node_id, action=req.action,
            approval_id=check.approval_id, release_policy=node.release_policy))
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
        return decision, (Step.USER_OCCUPANCY if reserved.use_id else Step.POLICY), {}

    # ── 보조 ────────────────────────────────────────────────────────

    def _check_policy(
        self, req: UseRequest, iid: str, applied: _Applied, node_id: str, p: float, now: datetime
    ) -> PolicyCheckResult:
        """④ 정책 검사 하나. OK 가 아니면 그 판정으로 멈춘다."""
        check = self._policy.check(req.user_id, node_id, req.action, now)
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
        posteriors: Mapping[str, float], best: tuple[str, float] | None, threshold: float,
        now: datetime,
    ) -> tuple[str, float, PolicyCheckResult]:
        """`any:locker` ③~④ 와 전역 할당 (FR-23). 배정받은 (기기, 확률, 정책 검사)를 반환한다.

        허용쌍은 ③ 임계값, ④ 정책 OK, 점유(AVAILABLE)를 모두 만족한 쌍이다. 승인이 필요한 기기는 뺀다.
        """
        if self._window is None:
            raise ValueError("대기 구간이 없다")
        qualified = sorted(
            (-posteriors.get(nid, 0.0), nid) for nid in by_id if posteriors.get(nid, 0.0) >= threshold)
        if not qualified:
            raise _Stop(Step.PROXIMITY, self._decision(
                req, iid, Verdict.ASK, ReasonCode.LOW_CONFIDENCE, p_max=best[1] if best else None))

        passed: list[tuple[str, float, PolicyCheckResult]] = []
        first_failure: tuple[str, float, PolicyCheckResult] | None = None
        for neg_p, nid in qualified:
            p = -neg_p
            check = self._policy.check(req.user_id, nid, req.action, now)
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
        assigned = self._window.submit(AllocationRequest(iid, {nid: p for nid, p, _ in allowed}))
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
        return ExecutionContext(
            internal_request_id=iid, request=req, node_id=decision.node_id, use_id=decision.use_id,
            usage_id=applied.usage_id, settings=self._settings, execution_kind=applied.execution_kind,
            reserved_at=applied.reserved_at)

    @staticmethod
    def _reserve_failure(req, iid, node_id, p, outcome: ReserveOutcome) -> Decision:
        if outcome is ReserveOutcome.DEVICE_BUSY:
            return make_decision(req.request_id, iid, Verdict.BUSY, ReasonCode.DEVICE_BUSY, node_id=node_id, p_max=p)
        if outcome is ReserveOutcome.USER_BUSY:
            return make_decision(req.request_id, iid, Verdict.BUSY, ReasonCode.USER_BUSY, node_id=node_id, p_max=p)
        if outcome is ReserveOutcome.APPROVAL_EXHAUSTED:
            return make_decision(
                req.request_id, iid, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, node_id=node_id, p_max=p)
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

    def _audit_step(self, step: Step, iid: str, payload: Mapping[str, Any]) -> None:
        self._audit.append(AuditEvent(
            event_id=self._new_id(), step=step, payload=dict(payload),
            internal_request_id=iid or None))
