"""FR-09 스피커 음량 제어의 MCP → CORE → DB → MQTT 경로 (specs/core-speaker-volume/spec.md).

실제 SQLite·runtime 조립 위에서 시뮬레이터를 fake Paho 로 연결한다. 근접 추정은 fake 로 고정한다.
스피커의 근접 임계값이 없어서 요청 대상은 명시 지정 speaker-1 이다.
"""

from __future__ import annotations

import asyncio
import json
import logging

from _runtime import TOKENS, VISITOR_A, McpCaller, asgi_post, query, wait_until
from test_server_assembly import world  # noqa: F401  (fixture)

from nearpilot.runtime import create_app

CONTROLS = {"volume_max": 20}


def announce_controls(w, controls):
    node = w.broker.nodes["speaker-1"]
    w.broker.report("speaker-1", [node._message("announce", capability="speaker", occupancy="shared",
                                                controls=controls)])
    wait_until(lambda: _stored_controls(w) == controls)


def _stored_controls(w):
    text = query(w.db, "SELECT speaker_controls FROM node WHERE node_id = 'speaker-1'")[0]["speaker_controls"]
    return json.loads(text) if text else None


def wait_succeeded(w, count):
    wait_until(lambda: [c["status"] for c in w.commands("speaker-1")] == ["succeeded"] * count)


def speaker_commands(w):
    return [c for c in w.broker.commands() if c["node_id"] == "speaker-1"]


def test_FR09_runtime_schema_uses_speaker_volume_max(world):  # noqa: F811
    """설정의 `speaker_volume_max`가 조립 서버의 `request_use` 스키마까지 닿는다."""
    w = world(settings={"speaker_volume_max": 15, "speaker_default_volume": 15})
    app = create_app(w.runtime)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    headers = {**McpCaller.HEADERS, "Authorization": f"Bearer {TOKENS[VISITOR_A]}"}

    async def scenario():
        async with app.router.lifespan_context(app):
            return await asgi_post(app, "/mcp", headers, body)

    status, text = asyncio.run(scenario())
    tool = next(t for t in json.loads(text)["result"]["tools"] if t["name"] == "request_use")
    assert status == 200 and tool["inputSchema"]["properties"]["volume"]["maximum"] == 15


def test_FR09_runtime_speaker_volume_reaches_device(world, caplog):  # noqa: F811
    w = world(node_ids=("speaker-1",))
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]

    async def call(args):
        return (await mcp.call("request_use", {"target_spec": "speaker-1", "action": "on", **args},
                               token=token))[1]

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "speaker-1")
            await asyncio.to_thread(announce_controls, w, CONTROLS)
            out = {"state": (await mcp.call("get_device_state", {"node_id": "speaker-1"}, token=token))[1]}
            out["loud"] = await call({"request_id": "loud", "volume": 12})
            await asyncio.to_thread(wait_succeeded, w, 1)
            # 저장소가 다시 만든 실행 문맥도 음량을 담아야 결과 인계가 끝나고 뒤 보고가 막히지 않는다.
            seen = query(w.db, "SELECT last_seen_at FROM node WHERE node_id = 'speaker-1'")[0]["last_seen_at"]
            await asyncio.sleep(0.01)
            w.broker.status("speaker-1")
            await asyncio.to_thread(wait_until, lambda: query(
                w.db, "SELECT last_seen_at FROM node WHERE node_id = 'speaker-1'")[0]["last_seen_at"] != seen)
            out["keep"] = await call({"request_id": "keep"})
            await asyncio.to_thread(wait_succeeded, w, 2)
            out["above"] = await call({"request_id": "above", "volume": 21})
            out["off"] = await call({"request_id": "off", "action": "off"})
            await asyncio.to_thread(wait_succeeded, w, 3)
            return out

    with caplog.at_level(logging.WARNING):
        out = asyncio.run(scenario())

    assert "CORE handover failed" not in caplog.text
    assert out["state"]["controls"] == CONTROLS
    assert out["loud"]["verdict"] == "OK"
    assert out["keep"]["verdict"] == "OK"
    assert (out["above"]["verdict"], out["above"]["reason"]) == ("DENY", "PARAMETER_OUT_OF_RANGE")
    assert out["off"]["verdict"] == "OK"

    # 거부한 요청은 명령을 만들지 않는다. 생략한 음량은 마지막 성공 on 의 값을 따르고, off 는 인자가 없다.
    commands = speaker_commands(w)
    assert [(c["action"], c.get("parameters")) for c in commands] == [
        ("on", {"volume": 12}), ("on", {"volume": 12}), ("off", None)]
    assert "parameters" not in commands[-1]
    stored = [json.loads(r["speaker"]) if r["speaker"] else None
              for r in query(w.db, "SELECT speaker FROM node_command ORDER BY rowid")]
    assert stored == [c.get("parameters") for c in commands]


def test_FR09_runtime_speaker_without_controls_stays_plain(world):  # noqa: F811
    w = world(node_ids=("speaker-1",))
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "speaker-1")
            plain = (await mcp.call("request_use", {"target_spec": "speaker-1", "action": "on",
                                                    "request_id": "plain"}, token=token))[1]
            with_args = (await mcp.call("request_use", {"target_spec": "speaker-1", "action": "on",
                                                        "request_id": "loud", "volume": 12},
                                        token=token))[1]
            return plain, with_args

    plain, with_args = asyncio.run(scenario())
    assert plain["verdict"] == "OK"
    assert (with_args["verdict"], with_args["reason"]) == ("DENY", "UNSUPPORTED_PARAMETER")
    assert ["parameters" in c for c in speaker_commands(w)] == [False]


def test_FR22_runtime_same_request_id_other_volume_conflicts(world):  # noqa: F811
    w = world(node_ids=("speaker-1",))
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]

    async def call(args):
        return (await mcp.call("request_use", {"target_spec": "speaker-1", "action": "on", **args},
                               token=token))[1]

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "speaker-1")
            await asyncio.to_thread(announce_controls, w, CONTROLS)
            first = await call({"request_id": "r1", "volume": 12})
            other = await call({"request_id": "r1", "volume": 13})
            repeat = await call({"request_id": "r1", "volume": 12})
            return first, other, repeat

    first, other, repeat = asyncio.run(scenario())
    assert first["verdict"] == "OK"
    assert (other["verdict"], other["reason"]) == ("DENY", "REQUEST_ID_CONFLICT")
    assert repeat == first  # 같은 입력의 재전송은 저장한 결과다
    assert [c["parameters"] for c in speaker_commands(w)] == [{"volume": 12}]  # 충돌과 재전송은 명령을 더하지 않는다
