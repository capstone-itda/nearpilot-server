"""FR-09 거리 선택자의 MCP → CORE → DB → MQTT 경로 (specs/core-distance-selector/spec.md).

실제 SQLite·runtime 조립 위에서 시뮬레이터를 fake Paho 로 연결한다. 근접 추정은 정렬 값을 고정한 fake 다.
조명 세 대 중 두 대를 하위 요청 스레드에서 겹쳐 실행하는 실제 DB 쓰기와 재전송을 확인한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime

import _runtime
from _runtime import TOKENS, VISITOR_A, FakeProximity, McpCaller, query, wait_until
from test_server_assembly import world  # noqa: F401  (fixture)

from nearpilot.runtime import create_app
from nearpilot.shared.models import BeaconKey, DistanceOrder, NodeInfo, RssiReport

LIGHTS = ("light-1", "light-2", "light-3")
HEARD_FRAMES = 30  # 정렬 값이 있는 기기의 프레임 수 (경계 확률의 N, #278). 값이 없는 기기는 0이다.


class OrderedProximity(FakeProximity):
    """정렬 값을 고정한 근접 추정 (FR-09, FR-16).

    `observe_report` 로 보고를 받은 기기만 거리 순서에 선다. 그래서 이 시험은 IOT → `_ScopedSink.on_rssi_report` →
    `NodeEventService.on_rssi_report` → 근접 추정의 보고 경로도 덮는다.
    정렬 규칙은 `HmmProximityEstimator.distance_order` 와 같다: 값이 큰(가까운) 기기부터, 같으면 기기 ID 순서이고
    값이 없는 기기는 끝에 기기 ID 순서다. 프레임 수는 정렬 값이 있으면 `HEARD_FRAMES`, 없으면 0이다.
    """

    def __init__(self, values: Mapping[str, float | None]) -> None:
        super().__init__({})
        self.values = dict(values)
        self._reported: set[str] = set()
        self._lock = threading.Lock()

    def observe_report(self, report: RssiReport, *, node: NodeInfo) -> None:
        with self._lock:
            self._reported.add(report.node_id)

    def reported(self) -> set[str]:
        with self._lock:
            return set(self._reported)

    def distance_order(self, beacon: BeaconKey, candidates: Sequence[NodeInfo], at: datetime) -> DistanceOrder:
        participants = {node.node_id for node in candidates} & self.reported()
        heard = sorted((n for n in participants if self.values.get(n) is not None),
                       key=lambda n: (-self.values[n], n))
        unheard = sorted(participants - set(heard))
        order = (*heard, *unheard)
        return DistanceOrder(
            nodes=order, values={n: self.values.get(n) for n in order},
            counts={n: 0 if self.values.get(n) is None else HEARD_FRAMES for n in order})


def snapshot(w):
    """DB 와 MQTT 에 남은 흔적: 명령, 발행한 명령, 상위 요청 행과 하위 요청 행."""
    parents = query(w.db, "SELECT internal_request_id FROM request WHERE request_id = 'sel-1'")
    assert len(parents) == 1  # 외부 request_id 가 있는 행은 상위 요청 하나뿐이다
    children = query(w.db, "SELECT node_id, request_id FROM request WHERE selector_parent_id = ?",
                     parents[0]["internal_request_id"])
    return {
        "commands": sorted((c["node_id"], c["action"]) for c in w.commands()),
        "published": sorted(c["node_id"] for c in w.broker.commands()),
        "children": sorted((r["node_id"], r["request_id"]) for r in children),
        "linked_rows": query(w.db, "SELECT count(*) c FROM request WHERE selector_parent_id IS NOT NULL")[0]["c"],
    }


def test_FR09_selector_lights_real_db(world, monkeypatch, caplog):  # noqa: F811
    # runtime 시험 세계의 조명은 light-1, light-2 뿐이다. 이 시험 안에서만 세 번째 조명을 더한다.
    # seed 와 SimBroker 가 같은 표를 읽는다.
    monkeypatch.setitem(_runtime.NODES, "light-3", ("light", "shared"))
    # 방문자 A 에게 light-2 가 가장 가깝고 light-3 이 가장 멀다
    prox = OrderedProximity({"light-2": -50.0, "light-1": -60.0, "light-3": -75.0})
    w = world(node_ids=LIGHTS, proximity_factory=lambda models, s: prox)
    # 두 하위 요청이 모두 명령을 발행해야 시뮬레이터가 첫 명령을 처리한다. 두 실행이 반드시 겹친다.
    # 하위 요청을 차례로 실행하면 첫 결과가 오지 않아 이 시험이 실패한다 (FR-18, NFR-01).
    handle = _runtime.FakePaho._handle

    def handle_after_both_published(self, node_id, kind, payload):
        if kind == "cmd":
            wait_until(lambda: len(self.broker.commands()) >= 2)
        handle(self, node_id, kind, payload)

    monkeypatch.setattr(_runtime.FakePaho, "_handle", handle_after_both_published)
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]
    request = {"target_spec": "near:light:1:2", "action": "on", "request_id": "sel-1"}

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, *LIGHTS)
            # 프레임을 저장한 뒤에 같은 보고의 수신 시각이 근접 추정에 닿는다
            await asyncio.to_thread(wait_until, lambda: prox.reported() >= set(LIGHTS))
            first = await mcp.call("request_use", request, token=token)
            await asyncio.to_thread(wait_until, lambda: [c["status"] for c in w.commands()]
                                    == ["succeeded", "succeeded"])
            after_first = snapshot(w)
            again = await mcp.call("request_use", request, token=token)
            return first, after_first, again, snapshot(w)

    with caplog.at_level(logging.WARNING):
        (status, first), after_first, (again_status, again), after_again = asyncio.run(scenario())

    # 하위 요청 스레드의 실행·인계 예외는 로그만 남기고 응답을 바꾸지 않는다. 조용한 실패가 없어야 한다.
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert status == again_status == 200
    assert (first["verdict"], first["reason"]) == ("OK", "OK")
    assert [d["node_id"] for d in first["devices"]] == ["light-2", "light-1"]  # 거리 순서이고 기기 ID 순서가 아니다
    assert [(d["verdict"], d["reason"], d["use_id"]) for d in first["devices"]] == [("OK", "OK", None)] * 2
    assert (first["selected_count"], first["similar"]) == (2, False)  # 경계 -60 과 -75 는 15 dB 차이다
    assert (first["node_id"], first["use_id"], first["request_id"]) == (None, None, "sel-1")
    assert VISITOR_A not in json.dumps(first)

    # 두 기기에만 명령이 있다: light-3 은 실행하지 않는다. 하위 요청 행은 외부 request_id 가 비어 있다.
    expected = {
        "commands": [("light-1", "on"), ("light-2", "on")],
        "published": ["light-1", "light-2"],
        "children": [("light-1", None), ("light-2", None)],
        "linked_rows": 2,
    }
    assert after_first == expected

    # 재전송은 같은 응답이고 새 명령도 새 하위 요청도 만들지 않는다
    assert again == first
    assert after_again == expected
