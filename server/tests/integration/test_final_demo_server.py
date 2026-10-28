"""Real broker and all seven simulated devices through the existing REST, MCP and dashboard app."""
from contextlib import ExitStack
from concurrent.futures import CancelledError
import json
import socket
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from nearpilot.cli.seed import apply_seed, load_seed_file
from nearpilot.core.admin import DefaultAdminService
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.runtime import Runtime, create_app, load_config
from tools.final_demo import prepare
from tools.node_lab.setup import broker_process, executable
from tools.node_sim.config import build_device, load_config as load_fleet


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(check, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("시연 기기의 발표·복원·비콘 관측을 확인하지 못했다")


def test_FR01_FR04_FR12A_FR13_FR14_FR16_final_demo_real_broker(tmp_path):
    try:
        executable("mosquitto")
        executable("mosquitto_passwd")
    except ValueError:
        pytest.skip("실제 Mosquitto와 mosquitto_passwd가 필요하다")
    mqtt_port, http_port = free_port(), free_port()
    while http_port == mqtt_port:
        http_port = free_port()
    folder = prepare(tmp_path / "final-demo", host="127.0.0.1", mode="sim",
                     mqtt_port=mqtt_port, http_port=http_port)
    cfg = load_config(folder / "server.toml")
    fleet = load_fleet(folder / "fleet.json")
    expected = {device.node_id for device in fleet.devices}
    tokens = []
    with ExitStack() as stack:
        broker = stack.enter_context(broker_process(folder))
        time.sleep(0.2)
        assert broker.poll() is None
        devices = []
        discovery = Runtime(cfg, collect_only=True)
        with TestClient(create_app(discovery)):
            for config in fleet.devices:
                device = build_device(fleet, config)
                stack.callback(device.node.store.close)
                stack.callback(device.stop)
                device.start()
                devices.append(device)

            def discovered():
                with discovery.db.scope() as bound:
                    return {n.node_id for n in bound.repo.nodes()} == expected

            wait_until(discovered)
            with discovery.db.scope() as bound:
                def new_token():
                    token = uuid4().hex + uuid4().hex
                    tokens.append(token)
                    return token

                service = DefaultAdminService(bound.repo, SqliteAdminRepository(bound.conn), new_token=new_token)
                summary = apply_seed(load_seed_file(folder / "seed.toml"), service, bound.repo, write=lambda line: None)
                assert summary.rejected == 0

        runtime = Runtime(cfg)
        with TestClient(create_app(runtime), base_url=f"http://127.0.0.1:{http_port}") as client:
            def ready():
                with runtime.db.scope() as bound:
                    nodes = tuple(bound.repo.nodes())
                return all(runtime.services.restore.blockers(n.node_id) == () for n in nodes)

            wait_until(ready)
            owner, visitor = tokens
            owner_auth = {"Authorization": f"Bearer {owner}"}
            visitor_auth = {"Authorization": f"Bearer {visitor}"}
            places = client.get("/dashboard/places", headers=owner_auth)
            assert places.status_code == 200
            assert {n["nodeId"] for p in places.json()["places"] for n in p["devices"]} == expected
            assert client.get("/dashboard/places", headers=visitor_auth).status_code == 403
            session = client.websocket_connect("/dashboard/stream")
            ws = session.__enter__()
            try:
                ws.send_json({"type": "auth", "token": owner})
                assert ws.receive_json()["v"] == 1
            finally:
                ws.close()
                try:
                    session.__exit__(None, None, None)
                except CancelledError:
                    pass  # TestClient cancels the already-closed streaming handler during cleanup.

            # App REST is available alongside MCP and the owner dashboard.
            account = client.post("/accounts", json={"name": "앱 연결 확인"})
            assert account.status_code == 201
            response = client.put("/me/beacon", json={"uuid": "11111111-2222-3333-4444-555555555555", "major": 2, "minor": 77},
                                  headers={"Authorization": f'Bearer {account.json()["token"]}'})
            assert response.status_code == 200

            headers = {**visitor_auth, "Accept": "application/json, text/event-stream"}

            def mcp(method, params=None, extra_headers=None):
                response = client.post("/mcp", json={"jsonrpc": "2.0", "id": str(uuid4()), "method": method,
                                                    "params": params or {}}, headers={**headers, **(extra_headers or {})})
                assert response.status_code == 200, response.text
                assert "error" not in response.json(), response.text
                return response.json()["result"]

            def call(name, arguments):
                result = mcp("tools/call", {"name": name, "arguments": arguments})
                assert not result.get("isError"), result
                return json.loads(result["content"][0]["text"])

            assert len(mcp("tools/list")["tools"]) == 6
            # Registered beacon observations must reach the real estimator before explicit use.
            wait_until(lambda: bool(call("find_available_devices", {})["devices"]))
            # The nearby list contains only devices above the confidence threshold, not the inventory.
            assert "speaker-1" not in {n["node_id"] for n in call("find_available_devices", {})["devices"]}
            for node in expected:
                state = call("get_device_state", {"node_id": node})
                assert state["node_id"] == node and state["online"] and state["trusted"], state
            unknown_host = client.post("/mcp", headers={**headers, "Host": "unknown.invalid:8000"},
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert unknown_host.status_code == 421
            for node in ("locker-1", "locker-2", "locker-3"):
                opened = call("request_use", {"target_spec": node, "action": "open", "request_id": str(uuid4())})
                assert opened["verdict"] == "OK", opened
                # Existing SimNode supports locker open only; product firmware also supports close.
                released = call("release_use", {"use_id": opened["use_id"]})
                assert released["verdict"] == "OK", released
            for node in ("light-1", "light-2", "light-3", "speaker-1"):
                for action in ("on", "off"):
                    result = call("request_use", {"target_spec": node, "action": action, "request_id": str(uuid4())})
                    assert result["verdict"] == "OK", (node, action, result)
                    if action == "on":
                        # An in-flight status report may still say RUNNING after the final result.
                        # Let the unmodified simulator publish its next periodic IDLE report.
                        next_report = time.monotonic() + fleet.settings.status_report_sec

                        def idle():
                            with runtime.db.scope():
                                return time.monotonic() >= next_report and runtime.services.restore.can_execute(node)

                        wait_until(idle)
            assert [device.node.actuations for device in devices] == [1, 1, 1, 2, 2, 2, 2]
        assert runtime.db.open_connections == 0
