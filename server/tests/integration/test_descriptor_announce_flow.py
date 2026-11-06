"""FR-04·FR-06 v2 기기와 v3 기기가 같은 서버에서 발표하고 명령을 받는다 (specs/iot-device-descriptor/spec.md).

실제 SQLite·runtime 조립 위에서 시뮬레이터를 fake Paho 로 연결한다. 시뮬레이터는 아직 v2 기기다.
v3 기기는 같은 시뮬레이터 연결로 v3 발표를 보내서 흉내 낸다. 근접 추정은 fake 로 고정한다.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from _runtime import TOKENS, VISITOR_A, McpCaller, query, wait_until
from test_server_assembly import world  # noqa: F401  (fixture)

from nearpilot.runtime import create_app
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Capability, Occupancy

RING = {"brightness_max": 64, "color": True}


def stored(w, node_id):
    return query(w.db, "SELECT trust_state, descriptor, descriptor_hash FROM node WHERE node_id = ?", node_id)[0]


def retrust(w, node_id):
    """선언 해시가 바뀌면 신뢰가 풀린다. 소유자가 다시 신뢰한 것으로 둔다 (FR-04)."""
    conn = sqlite3.connect(str(w.db))
    try:
        conn.execute("UPDATE node SET trust_state = 'trusted' WHERE node_id = ?", (node_id,))
        conn.commit()
    finally:
        conn.close()


def test_FR04_v2_and_v3_nodes_share_one_server(world):  # noqa: F811
    w = world(node_ids=("light-1", "speaker-1"))
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]
    template = legacy_descriptor(Capability.LIGHT, Occupancy.SHARED, **RING)
    upgraded = template.to_json() | {"vendor": {"name": "nearpilot", "model": "light-ring", "firmware": "3.0.0"}}

    async def request(node_id, request_id, **arguments):
        return (await mcp.call("request_use", {"target_spec": node_id, "action": "on",
                                               "request_id": request_id, **arguments}, token=token))[1]

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "light-1", "speaker-1")
            node = w.broker.nodes["light-1"]
            # v2 펌웨어가 제어 정보와 함께 발표한다. 해시가 바뀌어 소유자가 다시 신뢰한다.
            w.broker.report("light-1", [node._message("announce", capability="light", occupancy="shared",
                                                      controls=RING)])
            await asyncio.to_thread(wait_until, lambda: stored(w, "light-1")["descriptor_hash"] == template.hash)
            retrust(w, "light-1")
            # 같은 기기를 v3 펌웨어로 올렸다. 선언 해시가 템플릿과 같아 신뢰가 유지된다.
            w.broker.report("light-1", [node._message("announce", schema_version=3, descriptor=upgraded)])
            await asyncio.to_thread(wait_until, lambda: json.loads(
                stored(w, "light-1")["descriptor"])["vendor"]["firmware"] == "3.0.0")
            out = {"v3": await request("light-1", "v3", brightness=30, color="#0000ff"),
                   "v2": await request("speaker-1", "v2")}
            await asyncio.to_thread(wait_until, lambda: len(w.broker.commands()) == 2)
            return out

    out = asyncio.run(scenario())
    row = stored(w, "light-1")
    assert row["trust_state"] == "trusted" and row["descriptor_hash"] == template.hash
    assert out["v2"]["verdict"] == "OK"
    # 시뮬레이터는 아직 v2 명령만 받아서 v3 명령을 거부한다 (PR ④에서 v3로 올린다).
    # 이 시험은 서버가 v3 기기에 v3 명령을 보내는지만 본다.
    assert out["v3"]["reason"] != "UNSUPPORTED_PARAMETER"
    commands = {c["node_id"]: c for c in w.broker.commands()}
    v3, v2 = commands["light-1"], commands["speaker-1"]
    assert (v3["schema_version"], v3["arguments"]) == (3, {"brightness": 30, "color": "#0000FF"})
    assert "parameters" not in v3
    assert v2["schema_version"] == 2 and "arguments" not in v2
    wait_until(lambda: [c["status"] for c in w.commands("speaker-1")] == ["succeeded"])
