"""FR-05·FR-20 동반 기기로 SmartThings 기기를 실행한다 (specs/iot-smartthings-companion/spec.md).

실제 SQLite·runtime 조립 위에서 동반 기기 시뮬레이터(display 선언)를 fake Paho 로 연결하고,
SmartThings 는 가짜 transport 로 둔다. 판정·상태 관리·라우팅·SmartThings 게이트웨이는 실제 구현이다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from _runtime import (
    MINOR, OWNER, TOKENS, UUID, VISITOR_A, FakeProximity, McpCaller, SimBroker, make_config, query, seed,
    wait_until,
)
from tools.node_sim.node import Beacon, Scenario, SimNode
from tools.node_sim.store import CommandStore

from nearpilot.db.repositories._common import descriptor_to_json
from nearpilot.iot.smartthings.actions import ACTIONS
from nearpilot.runtime import Runtime, ShutdownError, create_app
from nearpilot.runtime.config import SmartThingsBinding, SmartThingsConfig
from nearpilot.shared.descriptor import parse_descriptor
from nearpilot.shared.models import BeaconKey, DistanceOrder, NodeInfo, RssiReport

DISPLAY = "display-1"
DEVICE_ID = "st-device-secret-0001"
TOKEN = "st-token-secret"
# 펌웨어 DISPLAY_SMARTTHINGS_DESCRIPTOR 와 같다 (tests/tools/test_firmware_descriptors.py 가 대조한다)
DESCRIPTOR = parse_descriptor({
    "format": "nearpilot.device/1", "kind": "display", "occupancy": "shared",
    "vendor": {"name": "nearpilot", "model": "display", "firmware": "v3"},
    "actions": [dict(action.declaration) for action in ACTIONS.values()],
})


class FakeSmartThings:
    """명령을 받으면 기기 상태를 바꾸는 가짜 SmartThings. `deviceId` 하나만 안다."""

    def __init__(self) -> None:
        self.state = {("switch", "switch"): "off"}
        self.posted: list[tuple[str, str, list]] = []
        self._lock = threading.Lock()

    def post_command(self, device_id, capability, command, arguments):
        assert device_id == DEVICE_ID
        with self._lock:
            self.posted.append((capability, command, list(arguments)))
            if capability == "switch":
                self.state[("switch", "switch")] = command

    def get_attribute(self, device_id, capability, attribute):
        assert device_id == DEVICE_ID
        with self._lock:
            return self.state.get((capability, attribute))

    def commands(self) -> list[tuple[str, str, list]]:
        with self._lock:
            return [c for c in self.posted if c[0] != "refresh"]


class ReportedProximity(FakeProximity):
    """RSSI 보고를 받은 기기만 거리 순서에 세운다 (FR-09). 사후확률은 쓰지 않는다."""

    def __init__(self) -> None:
        super().__init__({})
        self._reported: set[str] = set()
        self._lock = threading.Lock()

    def observe_report(self, report: RssiReport, *, node: NodeInfo) -> None:
        with self._lock:
            self._reported.add(report.node_id)

    def reported(self) -> set[str]:
        with self._lock:
            return set(self._reported)

    def distance_order(self, beacon: BeaconKey, candidates: Sequence[NodeInfo], at: datetime) -> DistanceOrder:
        order = tuple(sorted({n.node_id for n in candidates} & self.reported()))
        return DistanceOrder(nodes=order, values={n: -50.0 for n in order})


def seed_display(db_path) -> None:
    """동반 기기 행. 운영에서는 발표와 소유자 신뢰로 생긴다."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO node (node_id, place_id, kind, occupancy, trust_state, device_state, exclusive_group, "
            "release_policy, descriptor, descriptor_hash) VALUES (?, 'place-a', 'display', 'shared', 'trusted', "
            "'AVAILABLE', NULL, '{}', ?, ?)", (DISPLAY, descriptor_to_json(DESCRIPTOR), DESCRIPTOR.hash))
        conn.execute("INSERT INTO access_policy (policy_id, node_id, current_version) VALUES (?, ?, 1)",
                     (f"pol-{DISPLAY}", DISPLAY))
        conn.execute(
            "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, approval_ttl_sec, "
            "approval_mode, max_uses) VALUES (?, 1, 'public', 'open', ?, 3600, 'one_time', 1)",
            (f"pol-{DISPLAY}", OWNER))
        conn.commit()
    finally:
        conn.close()


class Companion:
    """light-1(MQTT)과 동반 기기 display-1(SmartThings 바인딩)이 있는 서버 하나."""

    def __init__(self, tmp, *, settings=None) -> None:
        token_file = tmp / ".env.smartthings-token"
        token_file.write_text(json.dumps({"access_token": TOKEN}), encoding="utf-8")
        cfg = make_config(tmp, settings={"exec_timeout_sec": 1.0, "exec_timeout_sec.display": 3.0,
                                         **(settings or {})})
        self.cfg = replace(cfg, smartthings=SmartThingsConfig(
            token_file, confirm_poll_sec=0.05, request_timeout_sec=1.0,
            bindings=(SmartThingsBinding(DISPLAY, DEVICE_ID),)))
        seed(self.cfg.database.path, nodes={"light-1": ("light", "shared")})
        seed_display(self.cfg.database.path)
        self.broker = SimBroker(tmp, ("light-1",))
        # 동반 기기 시뮬레이터: 펌웨어와 같은 display 선언을 발표하고 RSSI·상태를 보낸다
        scenario = Scenario(actuation_sec=0.0, beacons=(Beacon(UUID, 1, MINOR[VISITOR_A], -55.0),))
        self.broker.nodes[DISPLAY] = SimNode(DISPLAY, DESCRIPTOR.to_json(),
                                             CommandStore(tmp / "sim" / f"{DISPLAY}.sqlite3"), scenario)
        self.smartthings = FakeSmartThings()
        self.proximity = ReportedProximity()
        self.runtime = Runtime(self.cfg, client_factory=self.broker.factory,
                               proximity_factory=lambda models, s: self.proximity,
                               smartthings_transport_factory=lambda config: self.smartthings)

    @property
    def db(self):
        return self.cfg.database.path

    def ready(self) -> None:
        restore = self.runtime.services.restore
        wait_until(lambda: restore.can_execute(DISPLAY) and restore.can_execute("light-1"))
        self.broker.rssi()
        wait_until(lambda: DISPLAY in self.proximity.reported())

    def commands(self, node_id=DISPLAY):
        rows = query(self.db, "SELECT action, status FROM node_command WHERE node_id = ? ORDER BY rowid", node_id)
        return [(r["action"], r["status"]) for r in rows]

    def status_then(self, predicate):
        """동반 기기의 주기 상태 보고 한 번 뒤에 조건을 본다."""
        self.broker.status(DISPLAY)
        return predicate()

    def device_state(self) -> str:
        return query(self.db, "SELECT device_state FROM node WHERE node_id = ?", DISPLAY)[0]["device_state"]

    def close(self) -> None:
        try:
            self.runtime.stop()
        except ShutdownError:
            pass
        self.broker.close()


def test_FR05_companion_display_near_selector_succeeds(tmp_path, caplog):
    w = Companion(tmp_path)
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready)
            devices = (await mcp.call("find_available_devices", {}, token=token))[1]
            status, body = await mcp.call("request_use", {"target_spec": "near:display:1:1", "action": "on",
                                                          "request_id": "d-1"}, token=token)
            await asyncio.to_thread(wait_until, lambda: w.commands() == [("on", "succeeded")])
            evidence = query(w.db, "SELECT evidence FROM node_command WHERE node_id = ?", DISPLAY)[0]["evidence"]
            return devices, status, body, evidence

    try:
        with caplog.at_level(logging.INFO):
            devices, status, body, evidence = asyncio.run(scenario())
        state = w.device_state()
    finally:
        w.close()

    assert "MQTT report rejected" not in caplog.text  # 동반 기기의 display 선언 발표를 받았다
    assert status == 200
    assert (body["verdict"], body["reason"]) == ("OK", "OK")
    assert [d["node_id"] for d in body["devices"]] == [DISPLAY]
    # SmartThings 로만 보냈다. MQTT 명령은 없다
    assert w.smartthings.commands() == [("switch", "on", [])]
    assert [c for c in w.broker.commands() if c["node_id"] == DISPLAY] == []
    assert state != "FAULT"
    assert json.loads(evidence) == {"source": "server_state_confirmed", "confirmation": {
        "source": "smartthings_state", "capability": "switch", "attribute": "switch", "value": "on"}}
    # 호스트 응답, 로그에 deviceId 와 토큰이 없다 (NFR-08)
    host_text = json.dumps([devices, body], ensure_ascii=False)
    assert DEVICE_ID not in host_text and TOKEN not in host_text
    assert DEVICE_ID not in caplog.text and TOKEN not in caplog.text
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8") if (tmp_path / "audit.jsonl").exists() else ""
    assert DEVICE_ID not in audit and TOKEN not in audit


def test_FR20_companion_display_off_on_leave(tmp_path):
    w = Companion(tmp_path, settings={"leave_absent_sec": 1.0, "leave_grace_sec": 0.0})
    app = create_app(w.runtime)
    mcp = McpCaller(app)

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready)
            body = (await mcp.call("request_use", {"target_spec": DISPLAY, "action": "on", "request_id": "d-2"},
                                   token=TOKENS[VISITOR_A]))[1]
            # 유지 대상자의 관측이 끊기면 주기 이탈 검사가 서버 내부 꺼짐을 SmartThings 로 보낸다.
            # 동반 기기는 실기처럼 상태를 주기적으로 보고한다(RSSI 는 보내지 않는다).
            done = lambda: w.commands() == [("on", "succeeded"), ("off", "succeeded")]  # noqa: E731
            await asyncio.to_thread(wait_until, lambda: w.status_then(done), 8.0, 0.2)
            return body

    try:
        body = asyncio.run(scenario())
        state = w.device_state()
    finally:
        w.close()

    assert body["verdict"] == "OK"
    assert w.smartthings.commands() == [("switch", "on", []), ("switch", "off", [])]
    assert w.smartthings.state[("switch", "switch")] == "off"
    assert [c for c in w.broker.commands() if c["node_id"] == DISPLAY] == []
    assert state != "FAULT"
