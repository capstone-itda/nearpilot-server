"""FR-09 request_access: 제한 기기의 접근 승인 요청.

승인 신청만 만든다. 신청은 실행 허가가 아니다. 소유자의 결정은 다음 `request_use` 의 ④ 가 확인한다.
승인이 필요한지는 `PolicyChecker` 의 결과를 그대로 쓴다. 기본값은 거부다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from nearpilot.core.decision.digest import input_digest
from nearpilot.core.decision.pipeline import make_decision
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action,
    ApprovalStatus,
    ReasonCode,
    Step,
    StoreOutcome,
    TargetKind,
    Verdict,
    Visibility,
)
from nearpilot.shared.interfaces import AuditLog, PolicyChecker, Repository
from nearpilot.shared.models import (
    ApprovalRequest,
    AuditEvent,
    Decision,
    DecisionRecord,
    TargetSpec,
    UseRequest,
)

log = logging.getLogger(__name__)

_NOT_REQUIRED = "승인이 필요 없습니다. request_use를 호출하세요."
_PENDING = "소유자의 승인을 기다립니다. 승인 요청은 실행 허가가 아닙니다."


class AccessRequestService:
    def __init__(
        self,
        repo: Repository,
        policy: PolicyChecker,
        audit: AuditLog,
        settings: Settings,
        clock: Callable[[], datetime],
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repo = repo
        self._policy = policy
        self._audit = audit
        self._settings = settings
        self._clock = clock
        self._new_id = new_id

    def request_access(self, user_id: str, node_id: str, action: Action, request_id: str) -> Decision:
        now = self._clock()
        try:
            target = TargetSpec.parse(node_id)
        except ValueError:  # 형식 오류는 요청 행을 만들지 않는다
            return make_decision(request_id, "", Verdict.DENY, ReasonCode.INVALID_INPUT)
        if target.kind is not TargetKind.EXPLICIT:  # nearest:·any: 는 기기를 지정하지 않는다
            return make_decision(request_id, "", Verdict.DENY, ReasonCode.INVALID_INPUT)
        req = UseRequest(user_id, request_id, target, action, None, now, purpose="access")

        iid = ""
        try:
            link = self._repo.link_request(req, input_digest(req))
            iid = link.internal_request_id
            if link.conflict:  # 기존 판정을 덮어쓰지 않는다
                d = make_decision(request_id, iid, Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
                self._audit_step(iid, {"result": d.reason.value})
                return d
            if not link.is_new:
                if link.previous is not None:
                    return link.previous
                d = make_decision(request_id, iid, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
                self._audit_step(iid, {"result": "IN_PROGRESS"})
                return d
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return self._internal_error(request_id, iid, exc, save=False)

        try:
            decision, policy_version, detail = self._decide(req, iid, now)
            self._audit_step(iid, {
                "verdict": decision.verdict.value, "reason": decision.reason.value,
                "settings_version": self._settings.version, **detail})
            saved = self._repo.save_decision(
                decision, DecisionRecord(settings=self._settings, policy_version=policy_version))
            if saved.outcome is StoreOutcome.CONFLICT:
                raise RuntimeError("같은 내부 요청에 다른 판정 기록이 있다")
            return decision
        except Exception as exc:  # noqa: BLE001 - 알 수 없는 오류는 거부한다
            return self._internal_error(request_id, iid, exc, save=True)

    def _decide(self, req: UseRequest, iid: str, now: datetime) -> tuple[Decision, int | None, dict]:
        rid, node_id = req.request_id, req.target.node_id
        if node_id is None:
            raise ValueError("명시 지정 대상에 node_id 가 없다")
        node = next((n for n in self._repo.nodes() if n.node_id == node_id), None)
        if node is None:
            return self._deny(rid, iid, ReasonCode.TARGET_UNAVAILABLE), None, {}
        try:
            policy = self._repo.current_policy(node_id)
        except LookupError:  # 정책이 없는 기기는 거부한다
            return self._deny(rid, iid, ReasonCode.POLICY_DENIED, node_id), None, {}
        if policy.visibility is not Visibility.PUBLIC and policy.approver_user_id != req.user_id:
            return self._deny(rid, iid, ReasonCode.TARGET_UNAVAILABLE), None, {}  # 없는 기기와 같다

        checked = self._policy.check(req.user_id, node_id, req.action, now)
        if checked.verdict is Verdict.OK:
            d = make_decision(rid, iid, Verdict.OK, ReasonCode.OK, node_id=node_id)
            return _with_message(d, _NOT_REQUIRED), policy.version, {"created": False}
        if checked.verdict is Verdict.DENY:
            return self._deny(rid, iid, checked.reason, node_id), policy.version, {"created": False}
        if checked.verdict is not Verdict.NEED_APPROVAL:
            raise ValueError(f"알 수 없는 정책 판정: {checked.verdict!r}")

        # 대기 중인 신청이 있어도 부른다. DB 가 기존 신청을 반환하고 만료를 연장하지 않는다 (FR-03).
        result = self._repo.create_approval_request(ApprovalRequest(
            requester_user_id=req.user_id, node_id=node_id, action=req.action,
            policy_id=policy.policy_id, policy_version=policy.version,
            internal_request_id=iid, created_at=now, settings=self._settings))
        if result.status is not ApprovalStatus.PENDING:
            raise ValueError(f"알 수 없는 신청 상태: {result.status!r}")
        d = make_decision(rid, iid, Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, node_id=node_id)
        return _with_message(d, _PENDING), policy.version, {"created": result.is_new}

    @staticmethod
    def _deny(rid: str, iid: str, reason: ReasonCode, node_id: str | None = None) -> Decision:
        return make_decision(rid, iid, Verdict.DENY, reason, node_id=node_id)

    def _internal_error(self, rid: str, iid: str, exc: Exception, save: bool) -> Decision:
        """예외 내용은 감사 기록에만 남기고 호스트에는 일반 안내만 준다 (#25)."""
        log.exception("request_access 중 예외")
        d = make_decision(rid, iid, Verdict.DENY, ReasonCode.INTERNAL_ERROR)
        if iid:
            try:
                self._audit_step(iid, {"result": "INTERNAL_ERROR", "error": repr(exc)})
            except Exception:  # noqa: BLE001
                log.exception("거부 감사 기록 실패")
            if save:
                try:
                    self._repo.save_decision(d, DecisionRecord(settings=self._settings))
                except Exception:  # noqa: BLE001
                    log.exception("거부 판정 저장 실패")
        return d

    def _audit_step(self, iid: str, payload: dict) -> None:
        self._audit.append(AuditEvent(
            event_id=self._new_id(), step=Step.POLICY, payload=payload,
            internal_request_id=iid or None))


def _with_message(decision: Decision, message: str) -> Decision:
    return replace(decision, message=message)
