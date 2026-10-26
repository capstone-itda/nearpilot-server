"""소유자용 FAULT 복구 REST (specs/api-rest-recovery/spec.md). RecoveryService 와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.recovery import build_recovery_router
from nearpilot.shared.enums import (
    CommandOutcome,
    RecoveryBlocker,
    RecoveryOutcome,
    RecoveryReason,
    RecoveryStatus,
    Role,
)
from nearpilot.shared.models import Account, RecoveryApprovalResult, RecoveryEvidence, RecoveryReadiness
from nearpilot.shared.tokens import hash_token

OBSERVED = datetime(2026, 1, 1, 0, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
DONE = datetime(2026, 1, 1, 0, 6, tzinfo=timezone.utc)
USER = "user-owner"
TOKEN = "token-owner"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NODE = "locker-1"
RECOVERY_ID = "rec-1"
SESSION_ID = "session-secret"
READINESS_URL = f"/admin/nodes/{NODE}/recovery"
APPROVE_URL = f"{READINESS_URL}/{RECOVERY_ID}/approve"
PATHS = [pytest.param("GET", READINESS_URL, id="readiness"), pytest.param("POST", APPROVE_URL, id="approve")]
LOGGER = "nearpilot.api.rest.recovery"

EVIDENCE = RecoveryEvidence("ev-1", SESSION_ID, OBSERVED, CommandOutcome.UNKNOWN, True)
READY = RecoveryReadiness(NODE, RECOVERY_ID, RecoveryStatus.READY, (), (EVIDENCE,), NOW)
RECOVERED = RecoveryReadiness(NODE, RECOVERY_ID, RecoveryStatus.RECOVERED, (), (EVIDENCE,), NOW)
RECOVERING = RecoveryReadiness(
    NODE, RECOVERY_ID, RecoveryStatus.RECOVERING, (RecoveryBlocker.STORE_OUTCOME_UNKNOWN,), (EVIDENCE,), NOW)
NO_RECOVERY = RecoveryReadiness(
    NODE, None, RecoveryStatus.NOT_READY, (RecoveryBlocker.NO_RECOVERY_REQUIRED,), (), NOW)
UNCONFIRMED = RecoveryReadiness(
    NODE, RECOVERY_ID, RecoveryStatus.NOT_READY,
    (RecoveryBlocker.EXECUTION_UNCONFIRMED, RecoveryBlocker.COMMAND_STATE_UNCONFIRMED),
    (RecoveryEvidence("ev-2", SESSION_ID, OBSERVED),), NOW)
COMPLETED = RecoveryApprovalResult(RECOVERY_ID, RecoveryOutcome.COMPLETED, RecoveryReason.OK, RECOVERED, DONE)

READY_BODY = {
    "node_id": NODE,
    "recovery_id": RECOVERY_ID,
    "status": "ready",
    "blockers": [],
    "evidence": [{
        "evidence_id": "ev-1",
        "observed_at": "2026-01-01T00:04:00+00:00",
        "execution_outcome": "unknown",
        "command_idle": True,
    }],
    "checked_at": "2026-01-01T00:05:00+00:00",
}
NO_RECOVERY_BODY = {
    **READY_BODY, "recovery_id": None, "status": "not_ready", "blockers": ["NO_RECOVERY_REQUIRED"], "evidence": []}
UNCONFIRMED_BODY = {
    **READY_BODY,
    "status": "not_ready",
    "blockers": ["EXECUTION_UNCONFIRMED", "COMMAND_STATE_UNCONFIRMED"],
    "evidence": [{
        "evidence_id": "ev-2",
        "observed_at": "2026-01-01T00:04:00+00:00",
        "execution_outcome": None,
        "command_idle": None,
    }],
}


class NotOwner(PermissionError):
    """상태 관리의 NotPlaceOwner 대역. API 는 core 의 예외 클래스를 import 하지 않는다."""


class FakeRecovery:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.readiness_result = READY
        self.approve_result = COMPLETED
        self.error: Exception | None = None

    def readiness(self, user_id, node_id):
        self.calls.append(("readiness", user_id, node_id))
        if self.error:
            raise self.error
        return self.readiness_result

    def approve(self, user_id, node_id, recovery_id):
        self.calls.append(("approve", user_id, node_id, recovery_id))
        if self.error:
            raise self.error
        return self.approve_result


class FakeAccounts:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def account_by_token_hash(self, token_hash):
        if self.error:
            raise self.error
        return Account(USER, Role.OWNER) if token_hash == hash_token(TOKEN) else None


@pytest.fixture
def recovery():
    return FakeRecovery()


@pytest.fixture
def accounts():
    return FakeAccounts()


@pytest.fixture
def client(recovery, accounts):
    app = FastAPI()
    app.include_router(build_recovery_router(recovery, Authenticator(accounts)))
    return TestClient(app)


@pytest.mark.parametrize("readiness, body", [
    pytest.param(READY, READY_BODY, id="ready"),
    pytest.param(NO_RECOVERY, NO_RECOVERY_BODY, id="no_recovery"),
    pytest.param(UNCONFIRMED, UNCONFIRMED_BODY, id="unconfirmed"),
])
def test_FR12A_readiness_returns_owner_view(client, recovery, readiness, body):
    recovery.readiness_result = readiness

    response = client.get(READINESS_URL, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == body
    assert recovery.calls == [("readiness", USER, NODE)]


def test_FR12A_readiness_not_owner_is_403(client, recovery):
    recovery.error = NotOwner("not owner")

    response = client.get(READINESS_URL, headers=AUTH)

    assert response.status_code == 403
    assert response.json() == {"error": "NOT_OWNER"}
    assert recovery.calls == [("readiness", USER, NODE)]


@pytest.mark.parametrize("method, url", PATHS)
@pytest.mark.parametrize("headers", [
    pytest.param({}, id="no_header"),
    pytest.param({"Authorization": "Bearer wrong"}, id="wrong_token"),
])
def test_FR12A_recovery_requires_token(client, recovery, method, url, headers):
    response = client.request(method, url, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"error": "INVALID_TOKEN"}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert recovery.calls == []


@pytest.mark.parametrize("method, url", PATHS)
def test_FR12A_recovery_lookup_failure_is_internal_error(client, accounts, recovery, method, url):
    accounts.error = RuntimeError("db down")

    response = client.request(method, url, headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert recovery.calls == []


@pytest.mark.parametrize("method, url", PATHS)
def test_FR12A_recovery_service_error_is_internal_error(client, recovery, method, url):
    recovery.error = RuntimeError("비밀 문구")

    response = client.request(method, url, headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert "비밀 문구" not in response.text


def test_NFR04_approve_passes_ids(client, recovery):
    response = client.post(APPROVE_URL, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "recovery_id": RECOVERY_ID,
        "outcome": "completed",
        "reason_code": "OK",
        "completed_at": "2026-01-01T00:06:00+00:00",
        "readiness": {**READY_BODY, "status": "recovered"},
    }
    assert recovery.calls == [("approve", USER, NODE, RECOVERY_ID)]


def test_NFR04_approve_pending_is_202(client, recovery):
    recovery.approve_result = RecoveryApprovalResult(
        RECOVERY_ID, RecoveryOutcome.PENDING, RecoveryReason.STORE_OUTCOME_UNKNOWN, RECOVERING)

    response = client.post(APPROVE_URL, headers=AUTH)

    assert response.status_code == 202
    body = response.json()
    assert body["recovery_id"] == RECOVERY_ID
    assert body["outcome"] == "pending"
    assert body["reason_code"] == "STORE_OUTCOME_UNKNOWN"
    assert body["completed_at"] is None
    assert body["readiness"]["status"] == "recovering"
    assert body["readiness"]["blockers"] == ["STORE_OUTCOME_UNKNOWN"]


def test_NFR04_approve_completed_twice_returns_200(client, recovery):
    first = client.post(APPROVE_URL, headers=AUTH)
    second = client.post(APPROVE_URL, headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert recovery.calls == [("approve", USER, NODE, RECOVERY_ID)] * 2


@pytest.mark.parametrize("reason, status", [
    (RecoveryReason.NOT_OWNER, 403),
    (RecoveryReason.RECOVERY_MISMATCH, 409),
    (RecoveryReason.NOT_READY, 409),
    (RecoveryReason.STATE_CONFLICT, 409),
    (RecoveryReason.STORE_NOT_APPLIED, 503),
])
def test_NFR04_approve_reasons_map_to_status(client, recovery, reason, status):
    recovery.approve_result = RecoveryApprovalResult(RECOVERY_ID, RecoveryOutcome.REJECTED, reason, UNCONFIRMED)

    response = client.post(APPROVE_URL, headers=AUTH)

    assert response.status_code == status
    assert response.json() == {"error": reason.value}


@pytest.mark.parametrize("reason", [RecoveryReason.OK, RecoveryReason.STORE_OUTCOME_UNKNOWN])
def test_NFR04_approve_unmapped_reason_is_internal_error(client, recovery, caplog, reason):
    recovery.approve_result = RecoveryApprovalResult(RECOVERY_ID, RecoveryOutcome.REJECTED, reason, UNCONFIRMED)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        response = client.post(APPROVE_URL, headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.ERROR]


def test_NFR04_approve_permission_error_is_internal_error(client, recovery):
    recovery.error = NotOwner("not owner")

    response = client.post(APPROVE_URL, headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


@pytest.mark.parametrize("kwargs", [
    pytest.param({"json": {"user_id": "other", "settlement": {"usage_id": "u1", "state": "released"}}}, id="json"),
    pytest.param({"content": b"not json"}, id="not_json"),
])
def test_NFR04_approve_ignores_body(client, recovery, kwargs):
    response = client.post(APPROVE_URL, headers=AUTH, **kwargs)

    assert response.status_code == 200
    assert recovery.calls == [("approve", USER, NODE, RECOVERY_ID)]


def test_NFR08_recovery_responses_hide_internal_ids(client):
    responses = [client.get(READINESS_URL, headers=AUTH), client.post(APPROVE_URL, headers=AUTH)]

    for response in responses:
        assert response.status_code == 200
        assert "ev-1" in response.text
        assert SESSION_ID not in response.text
        assert "session_id" not in response.text
        assert USER not in response.text
        assert TOKEN not in response.text
