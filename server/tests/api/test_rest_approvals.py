"""앱용 승인 REST (specs/api-rest-approval/spec.md). ApprovalService 와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.approvals import build_approval_router
from nearpilot.shared.enums import Action, ApprovalDecisionReason, ApprovalStatus, Role
from nearpilot.shared.models import Account, OwnerApproval, OwnerApprovalResult
from nearpilot.shared.tokens import hash_token

NOW = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
TOKEN = "token-owner"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PENDING = OwnerApproval(
    "a1", "locker-1", Action.OPEN, ApprovalStatus.PENDING, NOW, NOW + timedelta(minutes=10), None)
APPROVED = OwnerApproval(
    "a1", "locker-1", Action.OPEN, ApprovalStatus.APPROVED, NOW, NOW + timedelta(minutes=10),
    NOW + timedelta(hours=1))


class FakeApprovals:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.pending = [PENDING]
        self.result = OwnerApprovalResult(ApprovalDecisionReason.OK, PENDING)
        self.decide_result = OwnerApprovalResult(ApprovalDecisionReason.OK, APPROVED)
        self.error: Exception | None = None

    def pending_for(self, user_id):
        self.calls.append(("pending_for", user_id))
        if self.error:
            raise self.error
        return self.pending

    def get(self, user_id, approval_id):
        self.calls.append(("get", user_id, approval_id))
        if self.error:
            raise self.error
        return self.result

    def decide(self, user_id, approval_id, decision):
        self.calls.append(("decide", user_id, approval_id, decision))
        if self.error:
            raise self.error
        return self.decide_result


class FakeAccounts:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def account_by_token_hash(self, token_hash):
        if self.error:
            raise self.error
        return Account("user-owner", Role.OWNER) if token_hash == hash_token(TOKEN) else None


@pytest.fixture
def approvals():
    return FakeApprovals()


@pytest.fixture
def accounts():
    return FakeAccounts()


@pytest.fixture
def client(approvals, accounts):
    app = FastAPI()
    app.include_router(build_approval_router(approvals, Authenticator(accounts)))
    return TestClient(app)


def _item(approval: OwnerApproval) -> dict:
    return {
        "approval_id": approval.approval_id,
        "node_id": approval.node_id,
        "action": approval.action.value,
        "status": approval.status.value,
        "created_at": approval.created_at.isoformat(),
        "pending_expires_at": approval.pending_until.isoformat(),
        "valid_until": approval.valid_until.isoformat() if approval.valid_until else None,
    }


def test_FR03_list_returns_pending_approvals(client, approvals):
    response = client.get("/approvals", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == [_item(PENDING)]
    assert approvals.calls == [("pending_for", "user-owner")]

    approvals.pending = []
    assert client.get("/approvals", headers=AUTH).json() == []


def test_FR03_get_returns_approval(client, approvals):
    response = client.get("/approvals/a1", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body == _item(PENDING)
    assert body["valid_until"] is None
    assert datetime.fromisoformat(body["pending_expires_at"]) == PENDING.pending_until
    assert approvals.calls == [("get", "user-owner", "a1")]


def test_FR03_decide_passes_choice(client, approvals):
    approved = client.post("/approvals/a1/decision", headers=AUTH, json={"decision": "approved"})
    denied = client.post("/approvals/a1/decision", headers=AUTH, json={"decision": "denied"})

    assert approved.status_code == 200
    assert approved.json() == _item(APPROVED)
    assert denied.status_code == 200
    assert approvals.calls == [
        ("decide", "user-owner", "a1", ApprovalStatus.APPROVED),
        ("decide", "user-owner", "a1", ApprovalStatus.DENIED),
    ]


def test_FR03_decide_same_twice_returns_200(client, approvals):
    approvals.decide_result = OwnerApprovalResult(ApprovalDecisionReason.ALREADY_APPLIED, APPROVED)

    response = client.post("/approvals/a1/decision", headers=AUTH, json={"decision": "approved"})

    assert response.status_code == 200
    assert response.json() == _item(APPROVED)


@pytest.mark.parametrize("reason, status", [
    (ApprovalDecisionReason.NOT_FOUND, 404),
    (ApprovalDecisionReason.NOT_AUTHORIZED, 403),
    (ApprovalDecisionReason.ALREADY_DECIDED, 409),
    (ApprovalDecisionReason.POLICY_VERSION_MISMATCH, 409),
    (ApprovalDecisionReason.REQUEST_EXPIRED, 409),
])
def test_FR03_reasons_map_to_status(client, approvals, reason, status):
    approvals.result = OwnerApprovalResult(reason, None)
    approvals.decide_result = OwnerApprovalResult(reason, PENDING)

    got = client.get("/approvals/a1", headers=AUTH)
    decided = client.post("/approvals/a1/decision", headers=AUTH, json={"decision": "denied"})

    for response in (got, decided):
        assert response.status_code == status
        assert response.json() == {"error": reason.value}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_FR03_requires_token(client, approvals, headers):
    for response in (
        client.get("/approvals", headers=headers),
        client.get("/approvals/a1", headers=headers),
        client.post("/approvals/a1/decision", headers=headers, json={"decision": "approved"}),
    ):
        assert response.status_code == 401
        assert response.json() == {"error": "INVALID_TOKEN"}
    assert approvals.calls == []


def test_FR03_lookup_failure_is_internal_error(client, accounts, approvals):
    accounts.error = RuntimeError("db down")

    response = client.get("/approvals", headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert approvals.calls == []


@pytest.mark.parametrize("body", [
    {"decision": "pending"}, {"decision": "APPROVED"}, {"decision": ["approved"]}, {}, ["approved"], "approved",
])
def test_FR03_decide_rejects_invalid_body(client, approvals, body):
    response = client.post("/approvals/a1/decision", headers=AUTH, json=body)

    assert response.status_code == 400
    assert response.json() == {"error": "INVALID_INPUT"}
    assert approvals.calls == []


def test_FR03_decide_rejects_non_json_body(client, approvals):
    response = client.post("/approvals/a1/decision", headers=AUTH, content=b"not json")

    assert response.status_code == 400
    assert approvals.calls == []


def test_FR03_service_error_is_internal_error(client, approvals):
    approvals.error = RuntimeError("secret detail")

    for response in (
        client.get("/approvals", headers=AUTH),
        client.get("/approvals/a1", headers=AUTH),
        client.post("/approvals/a1/decision", headers=AUTH, json={"decision": "approved"}),
    ):
        assert response.status_code == 500
        assert response.json() == {"error": "INTERNAL_ERROR"}
        assert "secret" not in response.text


def test_NFR08_responses_hide_requester(client):
    texts = [
        client.get("/approvals", headers=AUTH).text,
        client.get("/approvals/a1", headers=AUTH).text,
        client.post("/approvals/a1/decision", headers=AUTH, json={"decision": "approved"}).text,
    ]
    for text in texts:
        assert "user" not in text
        assert "requester" not in text
