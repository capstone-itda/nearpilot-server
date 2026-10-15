"""FR-18 실행 직전 재검사 (#16).

상태 관리가 전송 직전에 부른다. 확정한 대상 하나만 ① 비콘, ② 대상, ③ 근접, ④ 정책으로 다시 검사한다.
⑤⑥은 다시 하지 않고 자기 예약만 확인한다. 저장소를 바꾸지 않고, 예외를 던지지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from nearpilot.core.decision.pipeline import make_decision, select_candidates, threshold_for
from nearpilot.shared.enums import (
    ExecutionKind, ReasonCode, SessionState, Step, TargetKind, TrustState, Verdict,
)
from nearpilot.shared.interfaces import AuditLog, PolicyChecker, ProximityEstimator, Repository
from nearpilot.shared.models import AuditEvent, Decision, ExecutionContext

log = logging.getLogger(__name__)


class _Stop(Exception):
    def __init__(self, decision: Decision):
        super().__init__(decision.reason)
        self.decision = decision


class Rechecker:
    """`DecisionRechecker` 구현."""

    def __init__(
        self,
        repo: Repository,
        proximity: ProximityEstimator,
        policy: PolicyChecker,
        audit: AuditLog,
        clock: Callable[[], datetime],
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repo = repo
        self._proximity = proximity
        self._policy = policy
        self._audit = audit
        self._clock = clock
        self._new_id = new_id

    def recheck(self, ctx: ExecutionContext) -> Decision:
        try:
            try:
                decision, detail = self._run(ctx)
            except _Stop as stop:
                decision, detail = stop.decision, {}
            self._audit_event(ctx, {"verdict": decision.verdict.value,
                                    "reason": decision.reason.value, **detail})
            return decision
        except Exception as exc:  # noqa: BLE001 - 검사 오류는 정상 거부와 구분해 거부한다
            return self._failed(ctx, exc)

    def _run(self, ctx: ExecutionContext) -> tuple[Decision, Mapping[str, Any]]:
        req, s, node_id = ctx.request, ctx.settings, ctx.node_id
        now = self._clock()

        def stop(verdict: Verdict, reason: ReasonCode, **kw: Any) -> _Stop:
            return _Stop(make_decision(req.request_id, ctx.internal_request_id, verdict, reason,
                                       node_id=node_id, **kw))

        # ① 비콘 — 활성 비콘은 0 또는 1 개다 (#42). 대상이 있는 장소의 신뢰 기기가 봐야 한다
        beacons = self._repo.beacons_of(req.user_id)
        if len(beacons) > 1:
            raise ValueError("활성 비콘이 둘 이상이다")
        frames = (
            self._repo.rssi_since(beacons, now - timedelta(seconds=s.beacon_recent_sec))
            if beacons else ()
        )
        if not frames:
            raise stop(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
        all_nodes = self._repo.nodes()
        by_node = {n.node_id: n for n in all_nodes}
        target = by_node.get(node_id)
        if target is None:
            raise stop(Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)
        if target.place_id is None:
            raise ValueError("대상 기기의 장소가 없다")
        place_id = target.place_id
        if not any(f.node_id in by_node and by_node[f.node_id].trust_state is TrustState.TRUSTED
                   and by_node[f.node_id].place_id == place_id for f in frames):
            raise stop(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)

        # ② 대상 — 다른 기기를 고르지 않는다
        explicit = req.target.kind is TargetKind.EXPLICIT
        threshold = threshold_for(s, req.target.capability, explicit)
        if threshold is None:  # 최초 판정이 거부했어야 할 요청이다
            raise ValueError("임계값 설정이 없다")
        candidates = select_candidates(all_nodes, req.target.capability, s, now, place_id)
        if node_id not in {n.node_id for n in candidates}:
            raise stop(Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)

        # ③ 근접 — 전체 후보와 장소 전체 기기를 넘기고 대상의 확률만 읽는다 (#26, #39)
        place_nodes = [n for n in all_nodes if n.place_id == place_id]
        result = self._proximity.posterior(beacons, candidates, now, place_nodes=place_nodes)
        p = dict(result.posteriors).get(node_id, 0.0)
        if p < threshold:
            raise stop(Verdict.ASK, ReasonCode.MOVE_CLOSER, p_max=p)

        # ④ 정책
        check = self._policy.check(req.user_id, node_id, req.action, now)
        if check.verdict is not Verdict.OK:
            if check.verdict not in (Verdict.NEED_APPROVAL, Verdict.DENY):
                raise ValueError(f"정책 검사의 알 수 없는 판정: {check.verdict!r}")
            raise stop(check.verdict, check.reason, p_max=p)

        # 자기 예약 — 타인 점유로 보지 않는다. 대여형만 세션이 있다.
        # 최초 대여는 RESERVED, 재조작은 기존 ACTIVE 세션이어야 한다 (#35)
        if ctx.use_id is not None:
            expected = (SessionState.ACTIVE if ctx.execution_kind is ExecutionKind.RENTAL_REOPERATION
                        else SessionState.RESERVED)
            session = self._repo.session(ctx.use_id)
            if (session is None or session.state is not expected
                    or session.node_id != node_id
                    or not self._repo.session_owner_matches(ctx.use_id, req.user_id)):
                raise stop(Verdict.DENY, ReasonCode.RECHECK_FAILED, p_max=p)

        ok = make_decision(req.request_id, ctx.internal_request_id, Verdict.OK, ReasonCode.OK,
                           node_id=node_id, use_id=ctx.use_id, p_max=p)
        return ok, {"p": p, "threshold": threshold}

    def _failed(self, ctx: ExecutionContext, exc: Exception) -> Decision:
        """예외 내용은 감사 기록에만 남기고 `Decision`에는 넣지 않는다."""
        log.exception("재검사 중 예외")
        d = make_decision(ctx.request.request_id, ctx.internal_request_id, Verdict.DENY,
                          ReasonCode.RECHECK_FAILED, node_id=ctx.node_id)
        try:
            self._audit_event(ctx, {"verdict": d.verdict.value, "reason": d.reason.value,
                                    "error": repr(exc)})
        except Exception:  # noqa: BLE001
            log.exception("재검사 거부 감사 기록 실패")
        return d

    def _audit_event(self, ctx: ExecutionContext, payload: Mapping[str, Any]) -> None:
        self._audit.append(AuditEvent(
            event_id=self._new_id(), step=Step.EXEC,
            payload={"recheck": True, "settings_version": ctx.settings.version, **payload},
            internal_request_id=ctx.internal_request_id))
