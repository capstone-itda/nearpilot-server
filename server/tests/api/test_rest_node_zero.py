"""소유자용 영점 관리 REST: 측정 영점(#307)과 영점 재탐색(#305) (FR-16, spec 결정 23, 24). 서비스와 계정 조회는 fake다."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nearpilot.api.auth.authenticator import Authenticator
from nearpilot.api.rest.nodes import build_node_admin_router
from nearpilot.shared.enums import Role, ZeroOutcome
from nearpilot.shared.models import Account, ZeroMeasureResult, ZeroResetResult
from nearpilot.shared.tokens import hash_token

USER = "user-owner"
TOKEN = "token-owner"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeNodes:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.measure = ZeroMeasureResult("light-1", ZeroOutcome.MEASURED, frames=9, median_dbm=-49.0,
                                         zero_db=-5.0, revision=8, distance_m=0.5, reference_dbm=-44.0)
        self.reset = ZeroResetResult(ZeroOutcome.DISCARDED, ("light-1",), 9)
        self.error: Exception | None = None

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.error:
            raise self.error

    def measure_zero(self, user_id, node_id):
        self._call("measure", user_id, node_id)
        return self.measure

    def reset_zero(self, user_id, node_id):
        self._call("reset", user_id, node_id)
        return self.reset

    def reset_place_zeros(self, user_id, place_id):
        self._call("reset_place", user_id, place_id)
        return self.reset


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


def test_FR16_rest_zero_routes(client, nodes):
    response = client.post("/admin/nodes/light-1/zero/measure", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"node_id": "light-1", "frames": 9, "median_dbm": -49.0, "zero_db": -5.0,
                               "revision": 8, "distance_m": 0.5, "reference_dbm": -44.0}
    assert USER not in response.text  # 사용자 ID를 넣지 않는다 (NFR-08)
    response = client.post("/admin/nodes/light-1/zero/reset", headers=AUTH)
    assert (response.status_code, response.json()) == (200, {"nodes": ["light-1"], "revision": 9})
    response = client.post("/admin/places/place-a/zero/reset", headers=AUTH)
    assert response.status_code == 200
    assert nodes.calls == [("measure", USER, "light-1"), ("reset", USER, "light-1"),
                           ("reset_place", USER, "place-a")]


@pytest.mark.parametrize("outcome,status", [
    (ZeroOutcome.NOT_OWNER, 403), (ZeroOutcome.NOT_FOUND, 404), (ZeroOutcome.NOT_TRUSTED, 409),
    (ZeroOutcome.NO_BEACON, 409), (ZeroOutcome.NOT_ENOUGH_FRAMES, 422), (ZeroOutcome.NOT_SAVED, 503)])
def test_FR16_rest_zero_measure_maps_outcomes(client, nodes, outcome, status):
    nodes.measure = ZeroMeasureResult("light-1", outcome, frames=2, distance_m=0.5, reference_dbm=-44.0)
    response = client.post("/admin/nodes/light-1/zero/measure", headers=AUTH)
    assert response.status_code == status
    assert response.json()["error"] == outcome.value and response.json()["frames"] == 2


@pytest.mark.parametrize("outcome,status", [
    (ZeroOutcome.NOT_OWNER, 403), (ZeroOutcome.NOT_FOUND, 404), (ZeroOutcome.NOT_SAVED, 503)])
def test_FR16_rest_zero_reset_maps_outcomes(client, nodes, outcome, status):
    nodes.reset = ZeroResetResult(outcome)
    for url in ("/admin/nodes/light-1/zero/reset", "/admin/places/place-a/zero/reset"):
        response = client.post(url, headers=AUTH)
        assert (response.status_code, response.json()) == (status, {"error": outcome.value})


def test_FR16_rest_zero_requires_token_and_hides_errors(client, nodes):
    for url in ("/admin/nodes/light-1/zero/measure", "/admin/nodes/light-1/zero/reset",
                "/admin/places/place-a/zero/reset"):
        response = client.post(url)
        assert (response.status_code, response.json()) == (401, {"error": "INVALID_TOKEN"})
    assert nodes.calls == []
    nodes.error = RuntimeError("secret detail")
    for url in ("/admin/nodes/light-1/zero/measure", "/admin/places/place-a/zero/reset"):
        response = client.post(url, headers=AUTH)
        assert (response.status_code, response.json()) == (500, {"error": "INTERNAL_ERROR"})
