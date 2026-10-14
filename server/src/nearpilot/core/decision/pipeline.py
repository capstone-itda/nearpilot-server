"""FR-18 결정론적 판정 파이프라인 ⓪~⑥.

단계를 부르는 순서와 멈추는 때만 정한다. 단계 내부의 계산은 상대 계층의 Protocol 이 한다.
기본값은 거부다. 예외, 누락, 알 수 없는 상태는 OK 가 되지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from nearpilot.core.decision.digest import input_digest
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Capability,
    DeviceState,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    Step,
    TargetKind,
    TrustState,
    Verdict,
)
from nearpilot.shared.interfaces import (
    AuditLog,
    PolicyChecker,
    ProximityEstimator,
    Repository,
)
from nearpilot.shared.models import (
    AuditEvent,
    Decision,
    NodeInfo,
    ReserveRequest,
    UseRequest,
)

log = logging.getLogger(__name__)

_GENERIC_MESSAGE = "요청을 처리하지 못했습니다."


class _Stop(Exception):
    """단계가 실패해서 판정을 확정했다. 뒤 단계는 실행하지 않는다."""

    def __init__(self, step: Step, decision: Decision, detail: Mapping[str, Any] | None = None):
        super().__init__(decision.reason)
        self.step = step
        self.decision = decision
        self.detail = detail or {}


class DecisionPipeline:
    def __init__(
        self,
        repo: Repository,
        proximity: ProximityEstimator,
        policy: PolicyChecker,
        audit: AuditLog,
        settings: Settings,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repo = repo
        self._proximity = proximity
        self._policy = policy
        self._audit = audit
        self._settings = settings
        self._new_id = new_id

    # ── 진입점 ──────────────────────────────────────────────────────

    def decide(self, req: UseRequest) -> Decision:
        """⓪부터 ⑥까지 같은 순서로 검사하고 판정 하나를 반환한다."""
        internal_id = ""
        try:
            link = self._repo.link_request(req, input_digest(req))
            internal_id = link.internal_request_id
            if link.conflict:
                d = self._decision(req, internal_id, Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
                self._audit_step(Step.DEDUP, internal_id, {"result": d.reason.value})
                return d  # 기존 판정을 덮어쓰지 않는다
            if not link.is_new:
                if link.previous is not None:
                    return link.previous
                d = self._decision(req, internal_id, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
                self._audit_step(Step.DEDUP, internal_id, {"result": "IN_PROGRESS"})
                return d
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return self._internal_error(req, internal_id, Step.DEDUP, exc, save=False)

        try:
            try:
                decision, step, detail = self._run(req, internal_id)
            except _Stop as stop:
                decision, step, detail = stop.decision, stop.step, stop.detail
            context = {"step": step.value, **detail}
            self._audit_step(step, internal_id, {
                "verdict": decision.verdict.value, "reason": decision.reason.value,
                "settings_version": self._settings.version, **detail})
            self._repo.save_decision(decision, context, self._settings.version)
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return self._internal_error(req, internal_id, Step.EXEC, exc, save=True)
        return decision

    # ── ①~⑥ ───────────────────────────────────────────────────────

    def _run(self, req: UseRequest, iid: str) -> tuple[Decision, Step, dict[str, Any]]:
        s = self._settings
        now = req.received_at

        # ① 비콘
        beacons = self._repo.beacons_of(req.user_id)
        frames = (
            self._repo.rssi_since(beacons, now - timedelta(seconds=s.beacon_recent_sec))
            if beacons
            else ()
        )
        self._audit_step(Step.BEACON, iid, {"beacons": len(beacons), "frames": len(frames),
                                           "beacon_recent_sec": s.beacon_recent_sec})
        if not frames:
            raise _Stop(Step.BEACON, self._decision(req, iid, Verdict.DENY, ReasonCode.BEACON_NOT_SEEN))

        # ② 후보
        if req.target.kind is TargetKind.ANY:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.INVALID_INPUT))
        candidates = self._candidates(req.target.capability, now)
        self._audit_step(Step.CANDIDATE, iid, {"candidates": [n.node_id for n in candidates],
                                              "online_ttl_sec": s.online_ttl_sec})
        by_id = {n.node_id: n for n in candidates}
        explicit = req.target.kind is TargetKind.EXPLICIT
        if explicit and req.target.node_id not in by_id:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE))
        if not candidates:
            raise _Stop(Step.CANDIDATE, self._decision(req, iid, Verdict.DENY, ReasonCode.NO_CANDIDATE))

        # ③ 근접 — 명시 지정에도 ② 의 전체 후보를 넘긴다 (#26)
        result = self._proximity.posterior(beacons, candidates, now)
        threshold = (
            s.threshold_locker if req.target.capability is Capability.LOCKER else s.threshold_light
        )
        posteriors = dict(result.posteriors)
        prox_detail = {"posteriors": posteriors, "threshold": threshold,
                       "model_version": result.model_version, "calib_version": result.calib_version}
        if explicit:
            node_id = req.target.node_id
            p = posteriors.get(node_id, 0.0)
            self._audit_step(Step.PROXIMITY, iid, {**prox_detail, "p": p})
            if p < threshold:
                raise _Stop(Step.PROXIMITY, self._decision(
                    req, iid, Verdict.ASK, ReasonCode.MOVE_CLOSER, node_id=node_id, p_max=p), prox_detail)
        else:
            best = result.best
            self._audit_step(Step.PROXIMITY, iid, {**prox_detail, "best": best})
            if best is None or best[1] < threshold or best[0] not in by_id:
                p_max = best[1] if best else None
                raise _Stop(Step.PROXIMITY, self._decision(
                    req, iid, Verdict.ASK, ReasonCode.LOW_CONFIDENCE, p_max=p_max), prox_detail)
            node_id, p = best
        node = by_id[node_id]

        # ④ 정책 — 확정한 대상만 검사한다
        check = self._policy.check(req.user_id, node_id, req.action, now)
        self._audit_step(Step.POLICY, iid, {"node_id": node_id, "verdict": check.verdict.value,
                                           "reason": check.reason.value,
                                           "policy_version": check.policy_version})
        if check.verdict is not Verdict.OK:
            if check.verdict not in (Verdict.NEED_APPROVAL, Verdict.DENY):
                raise ValueError(f"정책 검사의 알 수 없는 판정: {check.verdict!r}")
            raise _Stop(Step.POLICY, self._decision(
                req, iid, check.verdict, check.reason, node_id=node_id, p_max=p), prox_detail)

        # ⑤⑥ — 대여형만. reserve_if_free() 한 번이 검사와 RESERVED 생성을 한다
        use_id: str | None = None
        if node.occupancy is Occupancy.RENTAL:
            reserved = self._repo.reserve_if_free(ReserveRequest(
                internal_request_id=iid, user_id=req.user_id, node_id=node_id, action=req.action,
                approval_id=check.approval_id, release_policy=node.release_policy))
            self._audit_step(Step.DEVICE_OCCUPANCY, iid, {"node_id": node_id,
                                                         "outcome": reserved.outcome.value})
            if reserved.outcome is ReserveOutcome.RESERVED:
                use_id = reserved.use_id
            else:
                raise _Stop(Step.DEVICE_OCCUPANCY if reserved.outcome is ReserveOutcome.DEVICE_BUSY
                            else Step.USER_OCCUPANCY, self._reserve_failure(req, iid, node_id, p, reserved.outcome), prox_detail)
        elif check.approval_id is not None:
            # 공용형의 승인 사용분 확보 접점이 미결이다 (spec 7절). 원자적 확보를 못 하므로 거부한다.
            raise _Stop(Step.POLICY, self._decision(
                req, iid, Verdict.DENY, ReasonCode.POLICY_DENIED, node_id=node_id, p_max=p), prox_detail)

        decision = self._decision(req, iid, Verdict.OK, ReasonCode.OK, node_id=node_id,
                                  use_id=use_id, p_max=p)
        return decision, (Step.USER_OCCUPANCY if use_id else Step.POLICY), prox_detail

    # ── 보조 ────────────────────────────────────────────────────────

    def _candidates(self, capability: Capability, now) -> Sequence[NodeInfo]:
        ttl = timedelta(seconds=self._settings.online_ttl_sec)
        return [
            n for n in self._repo.nodes(capability)
            if n.capability is capability
            and n.trust_state is TrustState.TRUSTED
            and n.device_state is not DeviceState.FAULT
            and n.last_seen_at is not None
            and now - n.last_seen_at <= ttl
        ]

    @staticmethod
    def _reserve_failure(req, iid, node_id, p, outcome: ReserveOutcome) -> Decision:
        if outcome is ReserveOutcome.DEVICE_BUSY:
            return DecisionPipeline._decision(req, iid, Verdict.BUSY, ReasonCode.DEVICE_BUSY, node_id=node_id, p_max=p)
        if outcome is ReserveOutcome.USER_BUSY:
            return DecisionPipeline._decision(req, iid, Verdict.BUSY, ReasonCode.USER_BUSY, node_id=node_id, p_max=p)
        if outcome is ReserveOutcome.APPROVAL_EXHAUSTED:
            return DecisionPipeline._decision(
                req, iid, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_EXHAUSTED, node_id=node_id, p_max=p)
        raise ValueError(f"알 수 없는 점유 결과: {outcome!r}")

    @staticmethod
    def _decision(req: UseRequest, iid: str, verdict: Verdict, reason: ReasonCode, **kw: Any) -> Decision:
        message = _GENERIC_MESSAGE if reason is ReasonCode.INTERNAL_ERROR else None
        return Decision(internal_request_id=iid, request_id=req.request_id, verdict=verdict,
                        reason=reason, message=message, **kw)

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
                    self._repo.save_decision(
                        d, {"step": step.value, "error": repr(exc)}, self._settings.version)
                except Exception:  # noqa: BLE001
                    log.exception("거부 판정 저장 실패")
        return d

    def _audit_step(self, step: Step, iid: str, payload: Mapping[str, Any]) -> None:
        self._audit.append(AuditEvent(
            event_id=self._new_id(), step=step, payload=dict(payload),
            internal_request_id=iid or None))
