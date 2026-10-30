"""FR-09 장소별 기기 발견과 실행 판정. 실제 MCP·CORE·DB, fake 브로커와 기기를 쓴다."""

import asyncio

from _runtime import TOKENS, VISITOR_A, McpCaller
from test_server_assembly import world  # noqa: F401 (fixture)

from nearpilot.runtime import create_app


def test_FR09_discover_same_place_with_low_probabilities_then_request_by_returned_id(world):
    nodes = ("locker-1", "locker-2", "light-1", "speaker-1")
    w = world(node_ids=nodes, posteriors={n: 0.01 for n in nodes},
              settings={"threshold_locker": 0.99, "threshold_light": 0.99})
    app = create_app(w.runtime)
    mcp = McpCaller(app)

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, *nodes)
            status, listing = await mcp.call("find_available_devices", {}, token=TOKENS[VISITOR_A])
            assert status == 200 and listing["reason"] is None
            assert [n["node_id"] for n in listing["devices"]] == sorted(nodes)
            assert w.broker.commands() == []  # 조회는 명령이나 예약을 만들지 않는다.
            assert w.commands() == []

            _, nearest = await mcp.call("request_use", {
                "target_spec": "nearest:locker", "action": "open", "request_id": "nearest-low",
            }, token=TOKENS[VISITOR_A])
            assert (nearest["verdict"], nearest["reason"]) == ("ASK", "LOW_CONFIDENCE")
            assert w.broker.commands() == []

            # 이름을 세션에 주입하지 않고 조회 결과의 기능으로 기기를 고른다.
            light = next(n for n in listing["devices"] if n["capability"] == "light")
            _, used = await mcp.call("request_use", {
                "target_spec": light["node_id"], "action": "on", "request_id": "discovered-light",
            }, token=TOKENS[VISITOR_A])
            assert (used["verdict"], used["node_id"]) == ("OK", light["node_id"])
            assert [c["node_id"] for c in w.commands()] == [light["node_id"]]
            assert len(w.broker.commands()) == 1

    asyncio.run(scenario())


def test_FR09_FR15_discovery_and_explicit_use_require_recent_beacon_reception(world):
    w = world(settings={"beacon_recent_sec": 1.0})
    app = create_app(w.runtime)
    mcp = McpCaller(app)

    async def scenario():
        async with app.router.lifespan_context(app):
            token = TOKENS[VISITOR_A]
            _, before = await mcp.call("find_available_devices", {}, token=token)
            assert before == {"devices": [], "reason": None}
            await asyncio.to_thread(w.ready, "locker-1", "locker-2", "light-1")
            _, present = await mcp.call("find_available_devices", {}, token=token)
            assert present["devices"]
            await asyncio.sleep(1.1)  # fake 브로커는 명시적으로 보낼 때만 RSSI를 보고한다.
            _, expired = await mcp.call("find_available_devices", {}, token=token)
            assert expired == {"devices": [], "reason": None}
            _, denied = await mcp.call("request_use", {
                "target_spec": "light-1", "action": "on", "request_id": "no-beacon",
            }, token=token)
            assert (denied["verdict"], denied["reason"]) == ("DENY", "BEACON_NOT_SEEN")
            assert w.broker.commands() == []

    asyncio.run(scenario())
