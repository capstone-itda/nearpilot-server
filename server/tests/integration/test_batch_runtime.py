"""FR-09 묶음 요청의 MCP → CORE → DB → MQTT 경로 (specs/core-request-batch/spec.md).

실제 SQLite·runtime 조립 위에서 시뮬레이터를 fake Paho 로 연결한다. 근접 추정은 정렬 값을 고정한 fake 다.
조명 둘과 스피커 하나를 하위 요청 스레드에서 겹쳐 실행하는 실제 DB 쓰기와 재전송을 확인한다.
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

DEVICES = ("light-1", "light-2", "speaker-1")
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
    parents = query(w.db, "SELECT internal_request_id, node_id, target_spec, action FROM request "
                          "WHERE request_id = 'batch-1'")
    assert len(parents) == 1  # 외부 request_id 가 있는 행은 상위 요청 하나뿐이다
    parent = parents[0]
    children = query(w.db, "SELECT node_id, request_id, target_spec, action FROM request "
                           "WHERE selector_parent_id = ?", parent["internal_request_id"])
    return {
        "parent": (parent["target_spec"], parent["action"], parent["node_id"]),
        "commands": sorted((c["node_id"], c["action"]) for c in w.commands()),
        "published": sorted(c["node_id"] for c in w.broker.commands()),
        "children": sorted((r["node_id"], r["request_id"], r["target_spec"], r["action"]) for r in children),
        "linked_rows": query(w.db, "SELECT count(*) c FROM request WHERE selector_parent_id IS NOT NULL")[0]["c"],
    }


def test_FR09_batch_lights_and_speaker_real_db(world, monkeypatch, caplog):  # noqa: F811
    # 방문자 A 에게 light-2 가 light-1 보다 가깝다. speaker-1 은 명시 지정이라 거리 순서에 서지 않는다
    prox = OrderedProximity({"light-2": -50.0, "light-1": -60.0})
    w = world(node_ids=DEVICES, proximity_factory=lambda models, s: prox)
    # 세 하위 요청이 모두 명령을 발행해야 시뮬레이터가 첫 명령을 처리한다. 세 실행이 반드시 겹친다.
    # 하위 요청을 차례로 실행하면 첫 결과가 오지 않아 이 시험이 실패한다 (FR-18, NFR-01).
    handle = _runtime.FakePaho._handle

    def handle_after_all_published(self, node_id, kind, payload):
        if kind == "cmd":
            wait_until(lambda: len(self.broker.commands()) >= len(DEVICES))
        handle(self, node_id, kind, payload)

    monkeypatch.setattr(_runtime.FakePaho, "_handle", handle_after_all_published)
    app = create_app(w.runtime)
    mcp = McpCaller(app)
    token = TOKENS[VISITOR_A]
    request = {"requests": [{"target_spec": "near:light:1:all", "action": "on"},
                            {"target_spec": "speaker-1", "action": "on"}],
               "request_id": "batch-1"}

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, *DEVICES)
            # 프레임을 저장한 뒤에 같은 보고의 수신 시각이 근접 추정에 닿는다
            await asyncio.to_thread(wait_until, lambda: prox.reported() >= set(DEVICES))
            first = await mcp.call("request_use", request, token=token)
            await asyncio.to_thread(wait_until, lambda: [c["status"] for c in w.commands()]
                                    == ["succeeded"] * len(DEVICES))
            after_first = snapshot(w)
            again = await mcp.call("request_use", request, token=token)
            return first, after_first, again, snapshot(w)

    with caplog.at_level(logging.WARNING):
        (status, first), after_first, (again_status, again), after_again = asyncio.run(scenario())

    # 하위 요청 스레드의 실행·인계 예외는 로그만 남기고 응답을 바꾸지 않는다. 조용한 실패가 없어야 한다.
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert status == again_status == 200
    assert (first["verdict"], first["reason"]) == ("OK", "OK")
    # 항목 1 의 조명 둘은 거리 순서이고 기기 ID 순서가 아니다. 항목 2 가 speaker-1 이다
    assert [(d["node_id"], d["item"]) for d in first["devices"]] == [("light-2", 1), ("light-1", 1), ("speaker-1", 2)]
    assert [(d["verdict"], d["reason"], d["use_id"]) for d in first["devices"]] == [("OK", "OK", None)] * 3
    assert (first["selected_count"], first["similar"]) == (3, False)  # 고른 범위의 바깥 기기가 없어 경계도 없다
    assert (first["node_id"], first["use_id"], first["request_id"]) == (None, None, "batch-1")
    sent = json.dumps(first)
    assert VISITOR_A not in sent
    internal_ids = [row["internal_request_id"] for row in query(w.db, "SELECT internal_request_id FROM request")]
    assert len(internal_ids) == 4  # 상위 요청 하나와 하위 요청 셋. 응답에 이 ID 가 없다 (FR-11)
    assert not any(internal_id in sent for internal_id in internal_ids)

    # 세 기기에 명령이 있다. 상위 요청 행은 묶음 표기이고 기기가 없다. 하위 요청 행은 외부 request_id 가 비어 있고
    # 그 항목의 대상과 동작을 담는다. 대상을 ③에서 정하는 speaker-1 행도 판정이 기기를 채웠다.
    expected = {
        "parent": ("batch:", "batch", None),
        "commands": [("light-1", "on"), ("light-2", "on"), ("speaker-1", "on")],
        "published": ["light-1", "light-2", "speaker-1"],
        "children": [("light-1", None, "near:light:1:all", "on"), ("light-2", None, "near:light:1:all", "on"),
                     ("speaker-1", None, "speaker-1", "on")],
        "linked_rows": 3,
    }
    assert after_first == expected

    # 재전송은 같은 응답이고 새 명령도 새 하위 요청도 만들지 않는다
    assert again == first
    assert after_again == expected
