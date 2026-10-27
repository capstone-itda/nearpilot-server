"""인증 실패의 감사 기록을 실제 Runtime과 SQLite에 연결한다 (FR-22, NFR-08)."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from _runtime import OWNER, TOKENS, UUID, SimBroker, make_config, query, seed
from nearpilot.api.auth.authenticator import AuthenticationError
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.runtime import DashboardConfig, Runtime, create_app
from nearpilot.runtime.auth_audit import AuditedAuthenticator
from nearpilot.shared.enums import ReasonCode, Step
from nearpilot.shared.tokens import hash_token

WRONG = "tok-wrong-7c1d"  # 등록하지 않은 토큰. 감사 테이블에 원문도 해시도 남으면 안 된다
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _server(tmp_path, *, dashboard=False):
    cfg = make_config(tmp_path)
    if dashboard:
        cfg = replace(cfg, dashboard=DashboardConfig(
            DashboardSettings(100, 20, 5.0, 100), -80.0, 0.1, tmp_path / "reports"))
    seed(cfg.database.path)
    broker = SimBroker(tmp_path, node_ids=())
    now = NOW
    runtime = Runtime(cfg, client_factory=broker.factory, clock=lambda: now)
    try:
        with TestClient(create_app(runtime)) as client:
            yield SimpleNamespace(runtime=runtime, client=client, now=now, db=cfg.database.path)
    finally:
        broker.close()
    assert runtime.db.open_connections == 0


def _auth_rows(server):
    return query(server.db, "SELECT * FROM audit_log WHERE step = 'auth'")


# 거부를 일으키는 요청. 각각 거부 상태 코드를 돌려준다. WebSocket 은 닫기 코드다.


def _mcp_without_header(server, token):
    # 미들웨어가 MCP 로 넘기기 전에 거부한다. 본문은 올바른 요청이다
    return server.client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"}).status_code


def _approvals(server, token):
    return server.client.get("/approvals", headers=_auth(token)).status_code


def _account(server, token):
    body = {"uuid": UUID, "major": 2, "minor": 10}
    return server.client.put("/me/beacon", json=body, headers=_auth(token)).status_code


def _dashboard_rest(server, token):
    return server.client.get("/dashboard/places", headers=_auth(token)).status_code


def _dashboard_stream(server, token):
    with server.client.websocket_connect("/dashboard/stream") as ws:
        ws.send_json({"type": "auth", "token": token})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


@pytest.mark.parametrize("send,token,rejected,credential,dashboard", [
    pytest.param(_mcp_without_header, None, 401, "missing", False, id="mcp-no-header"),
    pytest.param(_approvals, WRONG, 401, "presented", False, id="approval-rest-wrong-token"),
    pytest.param(_account, WRONG, 401, "presented", False, id="account-rest-wrong-token"),
    pytest.param(_dashboard_rest, WRONG, 401, "presented", True, id="dashboard-rest-wrong-token"),
    pytest.param(_dashboard_stream, WRONG, 4401, "presented", True, id="dashboard-stream-wrong-token"),
])
def test_FR22_runtime_auth_failure_audited(tmp_path, send, token, rejected, credential, dashboard):
    with _server(tmp_path, dashboard=dashboard) as server:
        assert send(server, token) == rejected  # 기록이 거부 응답을 바꾸지 않는다
        rows = _auth_rows(server)
        assert len(rows) == 1
        row = rows[0]
        assert row["internal_request_id"] is None and row["command_id"] is None
        payload = json.loads(row["payload_json"])
        assert set(payload) == {"reason", "credential", "at"}
        assert payload["reason"] == ReasonCode.INVALID_TOKEN.value
        assert payload["credential"] == credential
        assert datetime.fromisoformat(payload["at"]) == server.now  # Runtime 의 시계다
        table = repr([tuple(r) for r in query(server.db, "SELECT * FROM audit_log")])
        for secret in ([] if token is None else [token, hash_token(token)]):
            assert secret not in table  # 보낸 토큰도 그 해시도 어느 열에도 없다


class _Accounts:
    """토큰을 찾지 못하는 계정 조회. `error` 가 있으면 조회가 예외를 낸다."""

    def __init__(self, error=None):
        self.error = error

    def account_by_token_hash(self, token_hash):
        if self.error is not None:
            raise self.error
        return None


class _Audit:
    """저장한 사건을 모으는 감사 로그. `error` 가 있으면 저장이 예외를 낸다."""

    def __init__(self, error=None):
        self.error = error
        self.events = []

    def append(self, event):
        if self.error is not None:
            raise self.error
        self.events.append(event)


def test_FR22_runtime_auth_audit_failure_is_nonblocking(caplog):
    presented = "tok-lost-5e7a"
    for error, authorization in (
        (sqlite3.OperationalError("database is locked"), None),
        (sqlite3.OperationalError("database is locked"), f"Bearer {presented}"),
        (RuntimeError("database is locked"), None),  # sqlite 밖의 예외도 거부 사유를 바꾸지 않는다
    ):
        auth = AuditedAuthenticator(_Accounts(), _Audit(error), lambda: NOW)
        caplog.clear()
        with caplog.at_level(logging.WARNING), pytest.raises(AuthenticationError) as raised:
            auth.authenticate(authorization)
        assert raised.value.reason is ReasonCode.INVALID_TOKEN  # 저장 실패가 거부 사유를 바꾸지 않는다
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == ["인증 실패 감사 저장 실패"]
        assert "database is locked" in caplog.text  # 예외 정보를 남긴다
        for secret in (presented, hash_token(presented)):
            assert secret not in caplog.text
    # 계정 조회가 예외를 내도 INTERNAL_ERROR 로 기록하고 같은 사유로 다시 낸다
    audit = _Audit()
    auth = AuditedAuthenticator(_Accounts(RuntimeError("accounts down")), audit, lambda: NOW)
    with pytest.raises(AuthenticationError) as raised:
        auth.authenticate(f"Bearer {presented}")
    assert raised.value.reason is ReasonCode.INTERNAL_ERROR
    assert [event.payload["reason"] for event in audit.events] == [ReasonCode.INTERNAL_ERROR.value]


@pytest.mark.parametrize("authorization,credential", [
    (None, "missing"), ("", "missing"), ("Basic dXNlcjpwdw==", "presented"), ("Bearer", "presented"),
    ("Bearer tok-unknown", "presented"),
])
def test_FR22_runtime_auth_failure_credential_kind(authorization, credential):
    audit = _Audit()
    auth = AuditedAuthenticator(_Accounts(), audit, lambda: NOW)
    for _ in range(2):  # 기록 횟수를 제한하지 않는다. 실패마다 사건 ID가 다르다
        with pytest.raises(AuthenticationError):
            auth.authenticate(authorization)
    assert len(audit.events) == 2
    assert audit.events[0].event_id != audit.events[1].event_id
    for event in audit.events:
        assert str(uuid.UUID(event.event_id)) == event.event_id
        assert event.step is Step.AUTH
        assert event.internal_request_id is None and event.command_id is None
        assert dict(event.payload) == {
            "reason": ReasonCode.INVALID_TOKEN.value, "credential": credential, "at": NOW.isoformat()}


def test_FR22_runtime_auth_success_not_audited(tmp_path):
    with _server(tmp_path) as server:
        assert server.client.get("/approvals", headers=_auth(TOKENS[OWNER])).status_code == 200
        assert _auth_rows(server) == []
        # 같은 서버에서 실패는 남는다. 위의 0이 기록 경로가 막혀서가 아님을 보인다
        assert server.client.get("/approvals", headers=_auth(WRONG)).status_code == 401
        assert len(_auth_rows(server)) == 1
