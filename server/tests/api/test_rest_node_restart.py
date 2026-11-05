"""소유자용 기기 원격 재시작 REST (FR-12A, #235, specs/iot-node-restart/spec.md). 서비스와 계정 조회는 fake다."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.nodes import build_node_admin_router
from nearpilot.shared.enums import RestartOutcome, Role
from nearpilot.shared.models import Account, RestartResult
from nearpilot.shared.tokens import hash_token

USER = "user-owner"
TOKEN = "token-owner"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NODE = "locker-1"
URL = f"/admin/nodes/{NODE}/restart"
EXPIRES = datetime(2026, 11, 5, 3, 0, 3, tzinfo=timezone.utc)


class FakeNodes:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.result = RestartResult(NODE, RestartOutcome.SENT, "r-1", EXPIRES)
        self.error: Exception | None = None

    def restart(self, user_id, node_id):
        self.calls.append((user_id, node_id))
        if self.error:
            raise self.error
        return self.result


class FakeAccounts:
    def account_by_token_hash(self, token_hash):
        return Account(USER, Role.OWNER) if token_hash == hash_token(TOKEN) else None


@pytest.fixture
def nodes():
    return FakeNodes()


@pytest.fixture
def client(nodes):
    app = FastAPI()
    app.include_router(build_node_admin_router(nodes, Authenticator(FakeAccounts())))
    return TestClient(app)


def test_FR12A_restart_route_accepts(client, nodes):
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"node_id": NODE, "restart_id": "r-1", "expires_at": "2026-11-05T03:00:03+00:00"}
    assert nodes.calls == [(USER, NODE)]
    assert USER not in response.text  # 사용자 ID와 세션 ID를 넣지 않는다 (NFR-08)


@pytest.mark.parametrize("outcome,status", [
    (RestartOutcome.NOT_OWNER, 403), (RestartOutcome.NODE_OFFLINE, 409), (RestartOutcome.NOT_SENT, 503)])
def test_FR12A_restart_route_maps_outcomes(client, nodes, outcome, status):
    nodes.result = RestartResult(NODE, outcome)
    response = client.post(URL, headers=AUTH)
    assert (response.status_code, response.json()) == (status, {"error": outcome.value})


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_FR12A_restart_route_requires_token(client, nodes, headers):
    response = client.post(URL, headers=headers)
    assert (response.status_code, response.json()) == (401, {"error": "INVALID_TOKEN"})
    assert nodes.calls == []


def test_FR12A_restart_route_hides_errors(client, nodes):
    nodes.error = RuntimeError("password=secret")
    response = client.post(URL, headers=AUTH)
    assert (response.status_code, response.json()) == (500, {"error": "INTERNAL_ERROR"})
    assert "secret" not in response.text
