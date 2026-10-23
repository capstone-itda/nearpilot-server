"""앱용 계정 REST (specs/api-rest-account/spec.md). AdminService 와 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.accounts import beacon_id_for, build_account_router
from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason, Role
from nearpilot.shared.models import (
    Account,
    AccountIssue,
    AdminWriteResult,
    BeaconKey,
    BeaconRegistration,
)
from nearpilot.shared.tokens import hash_token

APPLIED = AdminWriteResult(AdminWriteOutcome.APPLIED, AdminWriteReason.OK)
UUID = "e2c56db5-dffb-48d2-b060-d0f5a71096e0"
TOKEN = "token-visitor"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeAdmin:
    def __init__(self) -> None:
        self.accounts: list[tuple[str, str, Role]] = []
        self.beacons: list[BeaconRegistration] = []
        self.account_result = APPLIED
        self.beacon_result = APPLIED
        self.error: Exception | None = None

    def register_account(self, user_id: str, name: str, role: Role) -> AccountIssue:
        if self.error:
            raise self.error
        self.accounts.append((user_id, name, role))
        token = "issued-token" if self.account_result.outcome == AdminWriteOutcome.APPLIED else None
        return AccountIssue(self.account_result, token)

    def register_beacon(self, req: BeaconRegistration) -> AdminWriteResult:
        if self.error:
            raise self.error
        self.beacons.append(req)
        return self.beacon_result


class FakeAccounts:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def account_by_token_hash(self, token_hash: str) -> Account | None:
        if self.error:
            raise self.error
        return Account("user-visitor", Role.VISITOR) if token_hash == hash_token(TOKEN) else None


@pytest.fixture
def admin():
    return FakeAdmin()


@pytest.fixture
def accounts():
    return FakeAccounts()


@pytest.fixture
def client(admin, accounts):
    app = FastAPI()
    app.include_router(build_account_router(admin, Authenticator(accounts), new_id=lambda: "user-new"))
    return TestClient(app)


def test_FR01_create_account_issues_visitor_token(client, admin):
    response = client.post("/accounts", json={"name": "  방문자  "})

    assert response.status_code == 201
    assert response.json() == {"token": "issued-token"}
    assert admin.accounts == [("user-new", "방문자", Role.VISITOR)]


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": 3}, {"name": "x" * 65}])
def test_FR01_create_account_rejects_invalid_name(client, admin, body):
    response = client.post("/accounts", json=body)

    assert response.status_code == 400
    assert response.json() == {"error": "INVALID_INPUT"}
    assert admin.accounts == []


@pytest.mark.parametrize("content", ["[]", "\"name\"", "not json"])
def test_FR01_create_account_rejects_non_object_body(client, admin, content):
    response = client.post("/accounts", content=content, headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {"error": "INVALID_INPUT"}
    assert admin.accounts == []


def test_FR01_create_account_conflict_returns_reason(client, admin):
    admin.account_result = AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.ACCOUNT_ALREADY_EXISTS)

    response = client.post("/accounts", json={"name": "방문자"})

    assert response.status_code == 409
    assert response.json() == {"error": "ACCOUNT_ALREADY_EXISTS"}


def test_FR01_put_beacon_registers_active_beacon(client, admin):
    response = client.put("/me/beacon", json={"uuid": UUID.upper(), "major": 1, "minor": 65535}, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"uuid": UUID, "major": 1, "minor": 65535, "is_active": True}
    key = BeaconKey(UUID, 1, 65535)
    assert admin.beacons == [BeaconRegistration(beacon_id_for("user-visitor", key), "user-visitor", key, True)]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_FR01_put_beacon_requires_token(client, admin, headers):
    response = client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"error": "INVALID_TOKEN"}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert admin.beacons == []


def test_FR01_put_beacon_lookup_failure_is_internal_error(client, admin, accounts):
    accounts.error = RuntimeError("db down")

    response = client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert admin.beacons == []


@pytest.mark.parametrize("body", [
    {"uuid": "not-a-uuid", "major": 1, "minor": 1},
    {"uuid": UUID.replace("-", ""), "major": 1, "minor": 1},
    {"uuid": UUID, "major": -1, "minor": 1},
    {"uuid": UUID, "major": 1, "minor": 65536},
    {"uuid": UUID, "major": 1.5, "minor": 1},
    {"uuid": UUID, "major": "1", "minor": 1},
    {"uuid": UUID, "major": True, "minor": 1},
    {"uuid": UUID, "major": 1},
    {"major": 1, "minor": 1},
])
def test_FR01_put_beacon_rejects_invalid_beacon(client, admin, body):
    response = client.put("/me/beacon", json=body, headers=AUTH)

    assert response.status_code == 400
    assert response.json() == {"error": "INVALID_BEACON"}
    assert admin.beacons == []


def test_FR01_put_beacon_duplicate_returns_reason(client, admin):
    admin.beacon_result = AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.BEACON_ALREADY_REGISTERED)

    response = client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=AUTH)

    assert response.status_code == 409
    assert response.json() == {"error": "BEACON_ALREADY_REGISTERED"}


def test_FR01_put_beacon_same_key_reuses_beacon_id(client, admin):
    client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=AUTH)
    client.put("/me/beacon", json={"uuid": UUID.upper(), "major": 1, "minor": 1}, headers=AUTH)
    client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 2}, headers=AUTH)

    first, again, other = (b.beacon_id for b in admin.beacons)
    assert first == again
    assert other != first


def test_FR01_admin_exception_is_internal_error(client, admin):
    admin.error = RuntimeError("secret detail")

    created = client.post("/accounts", json={"name": "방문자"})
    beacon = client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=AUTH)

    for response in (created, beacon):
        assert response.status_code == 500
        assert response.json() == {"error": "INTERNAL_ERROR"}


def test_FR11_responses_hide_user_and_beacon_ids(client):
    created = client.post("/accounts", json={"name": "방문자"})
    beacon = client.put("/me/beacon", json={"uuid": UUID, "major": 1, "minor": 1}, headers=AUTH)

    for response in (created, beacon):
        text = response.text
        assert "user-" not in text
        assert "beacon-" not in text
        assert "user_id" not in text and "beacon_id" not in text
