"""FR-09 request_access. 저장소, 정책, 감사는 fake 로 대신한다."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.decision.access import AccessRequestService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    AccessMode,
    Action,
    ApprovalMode,
    ApprovalStatus,
    Capability,
    DeviceState,
    Occupancy,
    ReasonCode,
    Step,
    StoreOutcome,
    TrustState,
    Verdict,
    Visibility,
)
from nearpilot.shared.models import (
    ApprovalRequestResult,
    Decision,
    NodeInfo,
    PolicyCheckResult,
    PolicyRevision,
    RequestLink,
    StoreResult,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
OWNER = "owner-1"
NODE = "locker-1"


def node(node_id=NODE) -> NodeInfo:
    return NodeInfo(node_id, "p", Capability.LOCKER, Occupancy.RENTAL, TrustState.TRUSTED,
                    DeviceState.AVAILABLE, "g", NOW)


def policy(visibility=Visibility.PUBLIC) -> PolicyRevision:
    return PolicyRevision("pol", 3, NODE, visibility, AccessMode.RESTRICTED, OWNER,
                          ApprovalMode.ONE_TIME, 3600, 1)


NEED = PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, 3)


class FakeRepo:
    def __init__(self):
        self.nodes_list = [node()]
        self.policy: PolicyRevision | None = policy()
        self.link = RequestLink("i1", True, False)
        self.link_calls: list = []
        self.approval_calls: list = []
        self.approval_status = ApprovalStatus.PENDING
        self.approval_is_new = True
        self.approval_error: Exception | None = None
        self.saved: list = []
        self.save_outcome = StoreOutcome.APPLIED

    def link_request(self, req, input_digest):
        self.link_calls.append((req, input_digest))
        return self.link

    def nodes(self, capability=None):
        return self.nodes_list

    def current_policy(self, node_id):
        if self.policy is None:
            raise LookupError(node_id)
        return self.policy

    def create_approval_request(self, req):
        self.approval_calls.append(req)
        if self.approval_error:
            raise self.approval_error
        return ApprovalRequestResult("a1", self.approval_status, NOW, NOW + timedelta(seconds=600),
                                     self.approval_is_new)

    def save_decision(self, decision, record):
        self.saved.append((decision, record))
        return StoreResult(self.save_outcome)


class FakePolicy:
    def __init__(self, result=NEED):
        self.result = result
        self.calls: list = []
        self.error: Exception | None = None

    def check(self, user_id, node_id, action, now):
        self.calls.append((user_id, node_id, action, now))
        if self.error:
            raise self.error
        return self.result


class FakeAudit:
    def __init__(self):
        self.events: list = []

    def append(self, event):
        self.events.append(event)


def build(policy_result=NEED):
    repo, pol, audit = FakeRepo(), FakePolicy(policy_result), FakeAudit()
    ids = iter(f"e{n}" for n in range(100))
    svc = AccessRequestService(repo, pol, audit, Settings(), lambda: NOW, lambda: next(ids))
    return svc, repo, pol, audit


def ask(svc, user="visitor", node_id=NODE, action=Action.OPEN, request_id="r1") -> Decision:
    return svc.request_access(user, node_id, action, request_id)


def test_FR03_승인이_필요하면_신청을_만든다():
    svc, repo, pol, audit = build()
    d = ask(svc)
    assert (d.verdict, d.reason, d.node_id, d.use_id) == (
        Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, NODE, None)
    (req,) = repo.approval_calls
    assert (req.requester_user_id, req.node_id, req.action) == ("visitor", NODE, Action.OPEN)
    assert (req.policy_id, req.policy_version, req.internal_request_id) == ("pol", 3, "i1")
    assert req.created_at == NOW and req.settings == Settings()
    assert pol.calls == [("visitor", NODE, Action.OPEN, NOW)]
    assert audit.events[-1].step is Step.POLICY and audit.events[-1].payload["created"] is True
    (saved, record) = repo.saved[0]
    assert saved == d and record.policy_version == 3


def test_FR03_대기_중이면_기존_신청을_쓴다():
    svc, repo, _, audit = build(PolicyCheckResult(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, 3, "a1"))
    repo.approval_is_new = False
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING)
    assert len(repo.approval_calls) == 1  # DB 가 기존 신청을 돌려준다
    assert audit.events[-1].payload["created"] is False


@pytest.mark.parametrize("reason", [
    ReasonCode.APPROVAL_REQUIRED, ReasonCode.APPROVAL_REJECTED, ReasonCode.APPROVAL_EXPIRED,
    ReasonCode.APPROVAL_REQUEST_EXPIRED, ReasonCode.APPROVAL_EXHAUSTED, ReasonCode.ACTION_NOT_ALLOWED,
])
def test_FR03_거부나_만료_뒤에는_새_신청을_만든다(reason):
    svc, repo, *_ = build(PolicyCheckResult(Verdict.NEED_APPROVAL, reason, 3))
    d = ask(svc)
    assert d.reason is ReasonCode.APPROVAL_PENDING and len(repo.approval_calls) == 1


def test_FR09_승인이_필요_없으면_신청을_만들지_않는다():
    svc, repo, *_ = build(PolicyCheckResult(Verdict.OK, ReasonCode.OK, 3))
    d = ask(svc)
    assert (d.verdict, d.reason, d.use_id, d.node_id) == (Verdict.OK, ReasonCode.OK, None, NODE)
    assert d.message and "request_use" in d.message
    assert repo.approval_calls == []


@pytest.mark.parametrize("raw", ["", "locker", "nearest:locker", "locker-x", "door-1"])
def test_FR09_node_id_형식_오류는_거부한다(raw):
    svc, repo, *_ = build()
    d = ask(svc, node_id=raw)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
    assert repo.link_calls == [] and repo.approval_calls == []


def test_FR10_재전송은_기존_판정을_반환한다():
    svc, repo, pol, _ = build()
    prev = Decision("i1", "r1", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, node_id=NODE)
    repo.link = RequestLink("i1", False, False, prev)
    assert ask(svc) is prev
    assert repo.approval_calls == [] and pol.calls == [] and repo.saved == []


def test_FR10_같은_키_다른_입력은_충돌이다():
    svc, repo, *_ = build()
    repo.link = RequestLink("i1", False, True)
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.REQUEST_ID_CONFLICT)
    assert repo.approval_calls == [] and repo.saved == []


def test_FR10_판정_중인_같은_키는_거부한다():
    svc, repo, *_ = build()
    repo.link = RequestLink("i1", False, False, None)
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert repo.approval_calls == []


def test_FR11_응답에_내부_값이_없다():
    svc, *_ = build()
    d = ask(svc)
    assert {f.name for f in fields(d)} == {
        "internal_request_id", "request_id", "verdict", "reason", "node_id", "use_id", "p_max", "message"}
    assert "visitor" not in repr(d) and "a1" not in repr(d) and "pol" not in repr(d)


def test_FR14_정책이_거부하면_신청하지_않는다():
    svc, repo, *_ = build(PolicyCheckResult(Verdict.DENY, ReasonCode.POLICY_DENIED, 3))
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.POLICY_DENIED)
    assert repo.approval_calls == []


def test_FR14_정책이_없으면_거부한다():
    svc, repo, pol, _ = build()
    repo.policy = None
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.POLICY_DENIED)
    assert repo.approval_calls == [] and pol.calls == []


def test_FR14_없는_기기와_비공개_기기는_같다():
    svc, repo, pol, _ = build()
    repo.nodes_list = []
    missing = ask(svc, request_id="r1")
    repo.nodes_list = [node()]
    repo.policy = policy(Visibility.PRIVATE)
    private = ask(svc, request_id="r2")
    assert (missing.verdict, missing.reason, missing.node_id) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, None)
    assert (private.verdict, private.reason, private.node_id) == (Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, None)
    assert repo.approval_calls == [] and pol.calls == []


def test_FR14_비공개_기기도_소유자는_요청한다():
    svc, repo, *_ = build(PolicyCheckResult(Verdict.OK, ReasonCode.OK, 3))
    repo.policy = policy(Visibility.PRIVATE)
    assert ask(svc, user=OWNER).verdict is Verdict.OK


def test_FR18_저장_예외는_내부_오류로_거부한다():
    svc, repo, _, audit = build()
    repo.approval_error = RuntimeError("db down")
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert d.message and "db down" not in d.message
    assert "db down" in audit.events[-1].payload["error"]
    assert repo.saved[-1][0].reason is ReasonCode.INTERNAL_ERROR


@pytest.mark.parametrize("status", [ApprovalStatus.APPROVED, ApprovalStatus.DENIED])
def test_FR18_알_수_없는_신청_상태는_거부한다(status):
    svc, repo, *_ = build()
    repo.approval_status = status
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)


def test_FR18_정책_검사_예외는_내부_오류로_거부한다():
    svc, repo, pol, _ = build()
    pol.error = RuntimeError("boom")
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
    assert repo.approval_calls == []


def test_FR18_판정_저장이_충돌하면_거부한다():
    svc, repo, *_ = build()
    repo.save_outcome = StoreOutcome.CONFLICT
    d = ask(svc)
    assert (d.verdict, d.reason) == (Verdict.DENY, ReasonCode.INTERNAL_ERROR)
