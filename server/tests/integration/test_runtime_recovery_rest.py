"""FAULT 복구 REST를 실제 Runtime과 SQLite에 연결한다 (FR-12A, NFR-04).

판정·상태 관리·IOT·DB 코드는 실제 구현이고 근접 추정만 fake 다. 기기는 fake Paho 위의 시뮬레이터다.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _runtime import (
    OWNER, TOKENS, VISITOR_A, FakeProximity, SimBroker, make_config, query, seed, wait_until,
)
from nearpilot.api.rest.recovery import build_recovery_router
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.runtime import DashboardConfig, Runtime, create_app
from nearpilot.runtime.assembly import _LateRestService
from nearpilot.runtime.tasks import NotAccepting
from nearpilot.shared.enums import Action
from nearpilot.shared.models import TargetSpec, UseRequest

RECOVERY = "/admin/nodes/locker-1/recovery"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _server(tmp_path, *, dashboard=False, collect_only=False, settings=None, node_ids=(), beacons=None,
            posteriors=None):
    cfg = make_config(tmp_path, settings=settings)
    if dashboard:
        cfg = replace(cfg, dashboard=DashboardConfig(
            DashboardSettings(100, 20, 5.0, 100), -80.0, 0.1, tmp_path / "reports"))
    seed(cfg.database.path)
    broker = SimBroker(tmp_path, node_ids=node_ids, beacons=beacons)
    proximity = FakeProximity(posteriors or {})
    runtime = Runtime(cfg, client_factory=broker.factory, collect_only=collect_only,
                      proximity_factory=lambda models, s: proximity)
    try:
        with TestClient(create_app(runtime)) as client:
            yield SimpleNamespace(runtime=runtime, client=client, broker=broker, cfg=cfg)
    finally:
        broker.close()
    assert runtime.db.open_connections == 0


@pytest.mark.parametrize("dashboard,collect_only", [(False, False), (True, False), (False, True), (True, True)])
def test_FR12A_runtime_recovery_rest(tmp_path, dashboard, collect_only):
    with _server(tmp_path, dashboard=dashboard, collect_only=collect_only) as server:
        client, owner = server.client, _auth(TOKENS[OWNER])
        viewed = client.get(RECOVERY, headers=owner)
        assert viewed.status_code == 200
        body = viewed.json()
        assert set(body) == {"node_id", "recovery_id", "status", "blockers", "evidence", "checked_at"}
        assert body["node_id"] == "locker-1" and body["recovery_id"] is None and body["evidence"] == []
        assert body["status"] == "not_ready" and body["blockers"] == ["NO_RECOVERY_REQUIRED"]
        not_owner = client.get(RECOVERY, headers=_auth(TOKENS[VISITOR_A]))
        assert not_owner.status_code == 403
        assert not_owner.json() == {"error": "NOT_OWNER"}
        no_token = client.get(RECOVERY)
        assert no_token.status_code == 401
        assert no_token.json() == {"error": "INVALID_TOKEN"}
        mismatch = client.post(f"{RECOVERY}/없는ID/approve", headers=owner)
        assert mismatch.status_code == 409
        assert mismatch.json() == {"error": "RECOVERY_MISMATCH"}
        assert server.broker.commands() == []  # 조회와 승인은 기기에 명령을 보내지 않는다


@pytest.mark.parametrize("gate_open", [False, True])
def test_FR12A_runtime_recovery_unbound_is_internal_error(tmp_path, gate_open):
    cfg = make_config(tmp_path)
    seed(cfg.database.path)
    runtime = Runtime(cfg)  # 시작하지 않는다. 접수 문은 닫혀 있고 복구 서비스는 연결 전이다
    if gate_open:
        runtime.gate.open()  # 접수 문만 열어서 연결 전 검사에 닿게 한다
    with pytest.raises(RuntimeError) as unbound:
        runtime.recovery.readiness(OWNER, "locker-1")
    if gate_open:
        assert str(unbound.value) == "연결하지 않은 REST 서비스다: recovery"
    else:
        assert isinstance(unbound.value, NotAccepting)  # 접수 문이 먼저 막는다
    app = FastAPI()
    app.include_router(build_recovery_router(runtime.recovery, runtime.authenticator))
    client, owner = TestClient(app), _auth(TOKENS[OWNER])
    for response in (client.get(RECOVERY, headers=owner),
                     client.post(f"{RECOVERY}/r1/approve", headers=owner)):
        assert response.status_code == 500
        assert response.json() == {"error": "INTERNAL_ERROR"}
    assert runtime.db.open_connections == 0


def test_FR12A_runtime_recovery_wrapper_binds_once(tmp_path):
    runtime = Runtime(make_config(tmp_path))
    late = _LateRestService("recovery", runtime.gate, runtime.db)
    late.bind(object())
    with pytest.raises(RuntimeError, match="이미 연결했다: recovery"):
        late.bind(object())


def test_FR12A_runtime_recovery_wrapper_runs_in_gate_and_scope(tmp_path):
    runtime = Runtime(make_config(tmp_path))
    runtime.gate.open()
    seen = {}

    class Service:
        def readiness(self, user_id, node_id):
            # 부르는 동안 접수 문을 지난 호출이 하나 있고 이 스레드의 연결 범위가 열려 있다
            seen["gate_busy"] = not runtime.gate.wait_idle(0)
            seen["connections"] = runtime.db.open_connections
            return user_id, node_id

    late = _LateRestService("recovery", runtime.gate, runtime.db)
    late.bind(Service())
    assert late.readiness(OWNER, "locker-1") == (OWNER, "locker-1")
    assert seen == {"gate_busy": True, "connections": 1}
    assert runtime.gate.wait_idle(0) and runtime.db.open_connections == 0


def _frames(db):
    return query(db, "SELECT count(*) c FROM rssi_observation")[0]["c"]


def _command_statuses(db):
    return [row["status"] for row in query(db, "SELECT status FROM node_command WHERE node_id = 'locker-1'")]


def _device_state(db):
    return query(db, "SELECT device_state FROM node WHERE node_id = 'locker-1'")[0]["device_state"]


def _readiness(client, owner):
    response = client.get(RECOVERY, headers=owner)
    assert response.status_code == 200
    return response.json()


def _wait_ready(client, owner):
    def ready():
        body = _readiness(client, owner)
        return body if body["status"] == "ready" else None

    return wait_until(ready)


def test_NFR04_runtime_recovery_flow(tmp_path):
    # 응답하지 않는 명령은 1 초 뒤 결과 불명이 된다
    settings = {"threshold_explicit_locker": 0.5, "exec_timeout_sec": 1.0}
    with _server(tmp_path, settings=settings, node_ids=("locker-1",), beacons={"locker-1": (VISITOR_A,)},
                 posteriors={VISITOR_A: {"locker-1": 0.97}}) as server:
        runtime, client, broker, db = server.runtime, server.client, server.broker, server.cfg.database.path
        owner = _auth(TOKENS[OWNER])
        # 기기가 실행할 수 있고 방문자 A 의 비콘 관측을 저장한 뒤, 응답하지 않는 기기에 열기를 요청한다
        wait_until(lambda: runtime.services.restore.can_execute("locker-1"))
        before = _frames(db)
        broker.rssi()
        wait_until(lambda: _frames(db) > before)
        broker.drop_commands = {"locker-1"}
        runtime.core.request_use(UseRequest(
            VISITOR_A, "r1", TargetSpec.parse("locker-1"), Action.OPEN, None, datetime.now(timezone.utc)))
        wait_until(lambda: _command_statuses(db) == ["unknown"] and _device_state(db) == "FAULT")

        # 복구 건이 생겼다. 명령 처리 대기 보고가 오기 전에는 준비되지 않았다
        recovery_id = wait_until(lambda: _readiness(client, owner)["recovery_id"])
        assert isinstance(recovery_id, str)
        assert _readiness(client, owner)["status"] == "not_ready"

        # 기기가 다시 응답하고 명령 처리 대기를 보고하면 준비된다
        broker.drop_commands = set()
        broker.status("locker-1")
        ready = _wait_ready(client, owner)
        assert ready["recovery_id"] == recovery_id and ready["blockers"] == []
        assert ready["evidence"]
        for item in ready["evidence"]:
            assert set(item) == {"observed_at", "execution_outcome", "command_idle"}

        approve = client.post(f"{RECOVERY}/{recovery_id}/approve", headers=owner)
        assert approve.status_code == 200
        assert approve.json()["outcome"] == "completed"
        completed_at = approve.json()["completed_at"]
        assert completed_at
        again = client.post(f"{RECOVERY}/{recovery_id}/approve", headers=owner)
        assert again.status_code == 200
        assert again.json()["completed_at"] == completed_at
        assert _device_state(db) == "AVAILABLE"
