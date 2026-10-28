"""FR-09 조명 밝기·색 제어의 MCP → CORE → DB → MQTT 경로 (specs/core-light-control/spec.md).

실제 SQLite·runtime 조립 위에서 시뮬레이터를 fake Paho 로 연결한다. 근접 추정은 fake 로 고정한다.
"""

from __future__ import annotations

import asyncio
import json
import logging

from _runtime import TOKENS, VISITOR_A, McpCaller, query, wait_until
from test_server_assembly import world  # noqa: F401  (fixture)

from nearpilot.runtime import create_app

RING = {"brightness_max": 64, "color": True}


def announce_controls(w, controls):
    node = w.broker.nodes["light-1"]
    w.broker.report("light-1", [node._message("announce", capability="light", occupancy="shared",
                                              controls=controls)])
    expected = json.dumps(controls, sort_keys=True, separators=(",", ":")) if controls else None
    wait_until(lambda: _normalized(query(w.db, "SELECT light_controls FROM node WHERE node_id = 'light-1'")[0]
                                   ["light_controls"]) == _normalized(expected))


def _normalized(text):
    return json.loads(text) if text else None


def light_commands(w):
    return [c for c in w.broker.commands() if c["node_id"] == "light-1"]


def test_FR09_runtime_light_setting_reaches_device(world, caplog):  # noqa: F811
    w = world(node_ids=("light-1",))
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]

    async def call(args):
        return (await mcp.call("request_use", {"target_spec": "light-1", "action": "on", **args},
                               token=token))[1]

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "light-1")
            await asyncio.to_thread(announce_controls, w, RING)
            devices = (await mcp.call("find_available_devices", {}, token=token))[1]
            out = {"devices": devices}
            out["blue"] = await call({"request_id": "blue", "brightness": 30, "color": "#0000ff"})
            await asyncio.to_thread(wait_until, lambda: [c["status"] for c in w.commands("light-1")]
                                    == ["succeeded"])
            # 저장소가 다시 만든 실행 문맥도 조명 설정을 담아야 결과 인계가 끝나고 뒤 보고가 막히지 않는다.
            seen = query(w.db, "SELECT last_seen_at FROM node WHERE node_id = 'light-1'")[0]["last_seen_at"]
            await asyncio.sleep(0.01)
            w.broker.status("light-1")
            await asyncio.to_thread(wait_until, lambda: query(
                w.db, "SELECT last_seen_at FROM node WHERE node_id = 'light-1'")[0]["last_seen_at"] != seen)
            out["above"] = await call({"request_id": "above", "brightness": 65})
            out["bad"] = await call({"request_id": "bad", "brightness": "30"})
            out["keep"] = await call({"request_id": "keep", "brightness": 10})
            await asyncio.to_thread(wait_until, lambda: [c["status"] for c in w.commands("light-1")]
                                    == ["succeeded", "succeeded"])
            out["conflict"] = await call({"request_id": "blue", "brightness": 31, "color": "#0000FF"})
            out["repeat"] = await call({"request_id": "blue", "brightness": 30, "color": "#0000FF"})
            return out

    with caplog.at_level(logging.WARNING):
        out = asyncio.run(scenario())

    assert "CORE handover failed" not in caplog.text
    light = next(d for d in out["devices"]["devices"] if d["node_id"] == "light-1")
    assert light["controls"] == RING
    assert out["blue"]["verdict"] == "OK"
    assert (out["above"]["verdict"], out["above"]["reason"]) == ("DENY", "PARAMETER_OUT_OF_RANGE")
    assert (out["bad"]["verdict"], out["bad"]["reason"]) == ("DENY", "INVALID_INPUT")
    assert out["keep"]["verdict"] == "OK"
    assert out["conflict"]["reason"] == "REQUEST_ID_CONFLICT"
    assert out["repeat"] == out["blue"]  # 같은 입력의 재전송은 저장한 결과다

    # 거부한 요청은 명령을 만들지 않는다. 생략한 색은 마지막 성공 on 의 값을 따른다.
    assert [c["parameters"] for c in light_commands(w)] == [
        {"brightness": 30, "color": "#0000FF"}, {"brightness": 10, "color": "#0000FF"}]
    stored = [json.loads(r["light"]) for r in query(w.db, "SELECT light FROM node_command ORDER BY rowid")]
    assert stored == [c["parameters"] for c in light_commands(w)]


def test_FR09_runtime_light_without_controls_stays_plain(world):  # noqa: F811
    w = world(node_ids=("light-1",))
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "light-1")
            plain = (await mcp.call("request_use", {"target_spec": "light-1", "action": "on",
                                                    "request_id": "plain"}, token=token))[1]
            with_args = (await mcp.call("request_use", {"target_spec": "light-1", "action": "on",
                                                        "request_id": "blue", "brightness": 30},
                                        token=token))[1]
            return plain, with_args

    plain, with_args = asyncio.run(scenario())
    assert plain["verdict"] == "OK"
    assert (with_args["verdict"], with_args["reason"]) == ("DENY", "UNSUPPORTED_PARAMETER")
    assert ["parameters" in c for c in light_commands(w)] == [False]
