"""계정·승인 REST를 실제 Runtime과 SQLite에 연결한다 (FR-01, FR-03, NFR-03)."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from _runtime import OWNER, TOKENS, UUID, VISITOR_A, SimBroker, make_config, query, seed, wait_until
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.core.admin import DefaultAdminService
from nearpilot.core.approval import OwnerApprovalService
from nearpilot.runtime import DashboardConfig, Runtime, create_app
from nearpilot.shared.enums import Action, Role
from nearpilot.shared.models import ApprovalRequest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _server(tmp_path, *, dashboard=False, collect_only=False):
    cfg = make_config(tmp_path)
    if dashboard:
        cfg = replace(cfg, dashboard=DashboardConfig(
            DashboardSettings(100, 20, 5.0, 100), -80.0, 0.1, tmp_path / "reports"))
    seed(cfg.database.path, modes={"locker-2": "restricted"})
    broker = SimBroker(tmp_path, node_ids=())
    now = datetime.now(timezone.utc)
    runtime = Runtime(cfg, client_factory=broker.factory, clock=lambda: now, collect_only=collect_only)
    try:
        with TestClient(create_app(runtime)) as client:
            yield SimpleNamespace(runtime=runtime, client=client, broker=broker, now=now, cfg=cfg)
    finally:
        broker.close()
    assert runtime.db.open_connections == 0


def _pending(server):
    return server.runtime.db.repository().create_approval_request(ApprovalRequest(
        requester_user_id=VISITOR_A, node_id="locker-2", action=Action.OPEN,
        policy_id="pol-locker-2", policy_version=1, internal_request_id="rest-access",
        created_at=server.now, settings=server.cfg.settings)).approval_id


@pytest.mark.parametrize("dashboard,collect_only", [(False, False), (True, False), (False, True), (True, True)])
def test_FR01_runtime_account_rest(tmp_path, dashboard, collect_only):
    with _server(tmp_path, dashboard=dashboard, collect_only=collect_only) as server:
        client, runtime = server.client, server.runtime
        created = client.post("/accounts", json={"name": "방문자"})
        assert created.status_code == 201
        assert set(created.json()) == {"token"}
        token = created.json()["token"]
        account = runtime.authenticator.authenticate(f"Bearer {token}")
        assert account.role is Role.VISITOR
        assert query(server.cfg.database.path, "SELECT token_hash FROM user WHERE user_id = ?",
                     account.user_id)[0]["token_hash"] != token
        beacon = {"uuid": UUID, "major": 2, "minor": 10}
        assert client.put("/me/beacon", json=beacon).status_code == 401
        for minor in (10, 10, 11):
            beacon["minor"] = minor
            response = client.put("/me/beacon", json=beacon, headers=_auth(token))
            assert response.status_code == 200
            assert response.json() == {**beacon, "is_active": True}
        active = query(server.cfg.database.path,
                       "SELECT minor FROM beacon WHERE user_id = ? AND is_active = 1", account.user_id)
        assert [row["minor"] for row in active] == [11]
        duplicate = client.put("/me/beacon", json=beacon, headers=_auth(TOKENS[VISITOR_A]))
        assert duplicate.status_code == 409
        assert duplicate.json() == {"error": "BEACON_ALREADY_REGISTERED"}
        assert server.broker.commands() == []
        if collect_only:
            assert client.post("/mcp").status_code == 404
        if dashboard:
            assert client.get("/dashboard/places", headers=_auth(TOKENS[OWNER])).status_code == 200


@pytest.mark.parametrize("dashboard,collect_only", [(False, False), (True, False), (False, True), (True, True)])
def test_FR03_runtime_approval_rest(tmp_path, dashboard, collect_only):
    with _server(tmp_path, dashboard=dashboard, collect_only=collect_only) as server:
        approval_id = _pending(server)
        client, owner, visitor = server.client, _auth(TOKENS[OWNER]), _auth(TOKENS[VISITOR_A])
        url = f"/approvals/{approval_id}"
        assert client.get("/approvals").status_code == 401
        listed = client.get("/approvals", headers=owner)
        assert listed.status_code == 200
        [item] = listed.json()
        assert item["approval_id"] == approval_id
        assert set(item) == {"approval_id", "node_id", "action", "status", "created_at",
                             "pending_expires_at", "valid_until"}
        assert client.get(url, headers=owner).json() == item
        assert client.get("/approvals", headers=visitor).json() == []
        assert client.get(url, headers=visitor).status_code == 403
        assert client.post(f"{url}/decision", headers=visitor,
                           json={"decision": "approved"}).status_code == 403
        for decision in ("approved", "approved"):
            response = client.post(f"{url}/decision", headers=owner, json={"decision": decision})
            assert response.status_code == 200
            assert response.json()["status"] == "approved"
            assert datetime.fromisoformat(response.json()["valid_until"]) == server.now + timedelta(seconds=3600)
        opposite = client.post(f"{url}/decision", headers=owner, json={"decision": "denied"})
        assert opposite.status_code == 409
        assert opposite.json() == {"error": "ALREADY_DECIDED"}
        assert client.get("/approvals", headers=owner).json() == []
        assert server.broker.commands() == []


@pytest.mark.parametrize("service", ["admin", "approvals"])
def test_NFR03_runtime_rest_scope_and_shutdown(tmp_path, monkeypatch, service):
    with _server(tmp_path) as server:
        runtime, client = server.runtime, server.client
        approval_id = _pending(server)
        entered, release = threading.Event(), threading.Event()
        seen = {}
        klass, method = ((DefaultAdminService, "register_account") if service == "admin"
                         else (OwnerApprovalService, "get"))
        original = getattr(klass, method)

        def blocked(self, *args, **kwargs):
            # 프록시가 이미 연결을 열었다. 중첩 범위는 같은 연결을 사용해야 한다.
            with runtime.db.scope() as bound:
                seen["conn"] = bound.conn
                seen["thread"] = threading.get_ident()
                assert bound.repo._conn is bound.admin._conn
            entered.set()
            assert release.wait(5)
            result = original(self, *args, **kwargs)
            with runtime.db.scope() as bound:
                assert bound.conn is seen["conn"]
            return result

        monkeypatch.setattr(klass, method, blocked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            if service == "admin":
                request = pool.submit(client.post, "/accounts", json={"name": "진행 중"})
            else:
                request = pool.submit(client.get, f"/approvals/{approval_id}", headers=_auth(TOKENS[OWNER]))
            try:
                assert entered.wait(5)
                stopping = pool.submit(runtime.stop)
                wait_until(lambda: not runtime.gate.accepting)
                assert not stopping.done()
                rejected = client.post("/accounts", json={"name": "종료 중"})
                assert rejected.status_code == 500
                assert rejected.json() == {"error": "INTERNAL_ERROR"}
            finally:
                release.set()
            assert request.result(timeout=5).status_code == (201 if service == "admin" else 200)
            stopping.result(timeout=5)
        assert seen["thread"] != threading.get_ident()
        with pytest.raises(sqlite3.ProgrammingError):
            seen["conn"].execute("SELECT 1")
        assert runtime.db.open_connections == 0
        assert runtime.gate.wait_idle(0)


def test_NFR03_runtime_rest_exception_closes_scope(tmp_path, monkeypatch):
    with _server(tmp_path) as server:
        seen = {}

        def fail(self, *args):
            with server.runtime.db.scope() as bound:
                seen["conn"] = bound.conn
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(DefaultAdminService, "register_account", fail)
        response = server.client.post("/accounts", json={"name": "실패"})
        assert response.status_code == 500
        assert response.json() == {"error": "INTERNAL_ERROR"}
        with pytest.raises(sqlite3.ProgrammingError):
            seen["conn"].execute("SELECT 1")
        assert server.runtime.gate.wait_idle(0)
