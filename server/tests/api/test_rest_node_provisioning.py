"""소유자 앱용 기기 접속 설정 묶음 REST (FR-12A, NFR-08, #234, specs/iot-ble-provisioning/spec.md)."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.nodes import build_node_admin_router
from nearpilot.shared.enums import Role
from nearpilot.shared.models import Account, ProvisioningBundle
from nearpilot.shared.tokens import hash_token

USER = "user-owner"
TOKEN = "token-owner"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NODE = "light-1"
URL = f"/admin/nodes/{NODE}/provisioning"
SECRET = "node-secret-abcdef"
BUNDLE = ProvisioningBundle(NODE, NODE, SECRET, "nearpilot.local", 18883)


class FakeNodes:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def provisioning(self, user_id, node_id):
        self.calls.append((user_id, node_id))
        if self.error:
            raise self.error
        return BUNDLE


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


def test_FR12A_provisioning_route_returns_bundle(client, nodes):
    response = client.get(URL, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"node_id": NODE, "mqtt_user": NODE, "mqtt_password": SECRET,
                               "mqtt_host": "nearpilot.local", "mqtt_port": 18883}  # 종류·점유 방식은 없다 (#245)
    assert nodes.calls == [(USER, NODE)]
    assert USER not in response.text


@pytest.mark.parametrize("error,status,reason", [
    (PermissionError("not owner"), 403, "NOT_OWNER"), (LookupError("none"), 404, "NOT_FOUND")])
def test_FR12A_provisioning_route_maps_errors(client, nodes, error, status, reason):
    nodes.error = error
    response = client.get(URL, headers=AUTH)
    assert (response.status_code, response.json()) == (status, {"error": reason})


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_FR12A_provisioning_route_requires_token(client, nodes, headers):
    response = client.get(URL, headers=headers)
    assert (response.status_code, response.json()) == (401, {"error": "INVALID_TOKEN"})
    assert nodes.calls == []


def test_NFR08_provisioning_never_logged(client, nodes, caplog):
    with caplog.at_level(logging.DEBUG):
        assert client.get(URL, headers=AUTH).status_code == 200
        nodes.error = RuntimeError(f"broken password={SECRET}")
        response = client.get(URL, headers=AUTH)
    assert (response.status_code, response.json()) == (500, {"error": "INTERNAL_ERROR"})
    assert SECRET not in caplog.text and SECRET not in response.text
