"""FR-12B 모니터링 따라 읽기를 실제 조립 서버가 만든 DB 로 확인한다 (specs/core-dashboard-queries/spec.md).

판정, 점유, 실행, 감사는 실제 코드가 남긴 행이고 근접 추정만 fake 다. 서버는 `_dashboard_world.py` 가 모듈마다 한 번 띄워
따라 읽기를 시작하고 흐름 (사물함 열림, 공용 조명, 결과 불명으로 고장, 접근 신청, RSSI) 을 하나씩 만들며 `tick()` 을
부른다. 첫 명령은 전송이 저장된 뒤 기기가 응답하기 전에 한 주기를 돌려서 보내는 중의 상태를 본다. 낸 이벤트는
`api/ws/schema.py` 의 이벤트 검사에 넣는다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from _dashboard_world import REPORTING, DashboardWorld
from _runtime import TOKENS, VISITOR_A, VISITOR_B, query, wait_until
from nearpilot.api.ws.names import PersonNames
from nearpilot.api.ws.schema import render_event
from nearpilot.core.dashboard import MonitorTail
from nearpilot.shared.enums import Action, DeviceState, ReasonCode, Step, Verdict


class Feed:
    """`MonitorFeed` fake. 서버 스레드가 아니라 `tick()` 을 부르는 쪽만 낸다."""

    def __init__(self) -> None:
        self.events: list = []

    def publish(self, event) -> None:
        self.events.append(event)

    def take(self) -> list:
        taken, self.events = self.events, []
        return taken


class Flow:
    """시나리오가 끝난 기록. `ticks` 는 주기마다 낸 이벤트 묶음이고 서버는 아직 떠 있다."""

    def __init__(self, world: DashboardWorld, ticks: list[list], audit_start: int) -> None:
        self.world, self.ticks, self.audit_start = world, ticks, audit_start

    @property
    def events(self) -> list:
        return [event for tick in self.ticks for event in tick]

    def of(self, name: str) -> list:
        """흐름 이름 (`ok`, `light`, `fault`, `access`) 의 요청이 낸 이벤트."""
        rid = self.world.requests[name]
        return [event for event in self.events if event.fields.get("requestId") == rid]


def commands_of(world: DashboardWorld, name: str) -> list[str]:
    rows = query(world.db, "SELECT status FROM node_command WHERE internal_request_id = ?", world.requests[name])
    return [row["status"] for row in rows]


def state_of(world: DashboardWorld, node_id: str) -> str:
    return query(world.db, "SELECT device_state FROM node WHERE node_id = ?", node_id)[0]["device_state"]


def frames(world: DashboardWorld) -> int:
    return query(world.db, "SELECT count(*) c FROM rssi_observation")[0]["c"]


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    feed, ticks = Feed(), []
    with DashboardWorld(tmp_path_factory.mktemp("tail")) as world:
        tail = MonitorTail(
            open_reads=world.open_reads, feed=feed, settings=lambda: world.settings,
            clock=lambda: datetime.now(timezone.utc),
        )
        audit_start = query(world.db, "SELECT COALESCE(MAX(rowid), 0) AS last FROM audit_log")[0]["last"]
        tail.start()

        def tick() -> None:
            tail.tick()
            ticks.append(feed.take())

        # 첫 명령은 전송 시작이 저장된 뒤, 기기가 응답하기 전에 한 주기를 돌린다. 이 기기는 아직 RESERVED 다
        client = world.broker.client
        send = client.publish

        def publish(topic, payload, qos, retain):
            if topic.endswith("/cmd") and not ticks:
                tick()
            return send(topic, payload, qos, retain)

        client.publish = publish

        world.decide("ok", VISITOR_A, "nearest:locker", {"locker-1": 0.97}, Verdict.OK)
        wait_until(lambda: commands_of(world, "ok") == ["succeeded"] and state_of(world, "locker-1") == "ACTIVE")
        tick()

        world.decide("light", VISITOR_B, "nearest:light", {"light-1": 0.9}, Verdict.OK, Action.ON)
        wait_until(lambda: commands_of(world, "light") == ["succeeded"])
        tick()

        world.broker.drop_commands = {"locker-3"}  # 결과가 오지 않는다. 1 초 뒤 결과 불명이 되고 기기를 격리한다
        world.decide("fault", VISITOR_B, "nearest:locker", {"locker-3": 0.97}, Verdict.DENY)
        wait_until(lambda: commands_of(world, "fault") == ["unknown"] and state_of(world, "locker-3") == "FAULT")
        tick()

        world.access("access", VISITOR_A, "light-2", Action.ON)
        tick()

        before = frames(world)
        world.broker.rssi()
        wait_until(lambda: frames(world) >= before + 2 * len(REPORTING))  # 기기마다 방문자 둘의 프레임
        tick()

        tick()  # 더 바뀐 것이 없다
        yield Flow(world, ticks, audit_start)


def types(events) -> list[str]:
    return [event.type for event in events]


def test_FR12B_요청_하나의_이벤트가_순서대로_나간다(flow):
    mine = flow.of("ok")

    assert types(mine) == ["mcp_call", *["step"] * 7, "posterior", "verdict", "command", "exec_result", "mcp_result"]
    call, *rest = mine
    assert dict(call.fields) == {
        "requestId": flow.world.requests["ok"], "tool": "request_use", "action": Action.OPEN,
        "target": {"kind": "nearest", "capability": "locker"},
    }
    assert [(e.fields["step"], e.fields["result"]) for e in rest if e.type == "step"] == [
        (Step(str(i)), "pass") for i in range(7)]
    posterior, verdict, command, result = (
        e for e in rest if e.type in ("posterior", "verdict", "command", "exec_result"))
    assert posterior.fields["posteriors"]["locker-1"] == 0.97 and posterior.fields["threshold"] == 0.5
    assert (verdict.fields["verdict"], verdict.fields["reason"], verdict.fields["nodeId"]) == (
        Verdict.OK, ReasonCode.OK, "locker-1")
    assert (command.fields["nodeId"], command.fields["action"]) == ("locker-1", Action.OPEN)
    assert (result.fields["nodeId"], result.fields["result"]) == ("locker-1", "succeeded")

    # 두 주기에 걸쳐 나간다: 보내는 중에 판정과 command 까지, 결과가 저장된 뒤에 exec_result 와 mcp_result
    first = [e.type for e in flow.ticks[0] if e.fields.get("requestId") == flow.world.requests["ok"]]
    second = [e.type for e in flow.ticks[1] if e.fields.get("requestId") == flow.world.requests["ok"]]
    assert first == types(mine)[:-2] and second == ["exec_result", "mcp_result"]

    names = PersonNames()
    for event in flow.events:  # 모든 이벤트가 형식 검사를 통과한다
        assert render_event(event, names)


def test_FR12B_공용형_요청은_점유_단계를_건너뛴다(flow):
    mine = flow.of("light")

    assert types(mine) == ["mcp_call", *["step"] * 5, "posterior", "verdict", "command", "exec_result", "mcp_result"]
    assert [e.fields["step"] for e in mine if e.type == "step"] == [Step(str(i)) for i in range(5)]


def test_FR12B_결과_불명은_고장_전이와_원인을_발행한다(flow):
    mine = flow.of("fault")

    assert types(mine) == ["mcp_call", *["step"] * 7, "posterior", "verdict", "command", "exec_result", "mcp_result"]
    assert next(e for e in mine if e.type == "exec_result").fields["result"] == "unknown"
    transitions = [e for e in flow.events if e.type == "transition" and e.fields["nodeId"] == "locker-3"]
    # 예약은 주기 사이에 지나갔다. 따라 읽기가 본 상태 (AVAILABLE) 에서 바로 FAULT 가 된 전이 하나다
    assert [(e.fields["to"], e.fields.get("fault")) for e in transitions] == [
        (DeviceState.FAULT, {"cause": ReasonCode.EXEC_UNKNOWN})]


def test_FR12B_실제_흐름의_전이_승인_감사_RSSI(flow):
    # 사물함 열림: 예약되고 실행이 끝나서 ACTIVE 가 된다. 다른 기기의 전이는 이 흐름들이 만든 것뿐이다
    transitions = [(e.fields["nodeId"], e.fields["to"]) for e in flow.events if e.type == "transition"]
    assert [t for t in transitions if t[0] == "locker-1"] == [
        ("locker-1", DeviceState.RESERVED), ("locker-1", DeviceState.ACTIVE)]
    assert {node_id for node_id, _ in transitions} <= {"locker-1", "locker-3", "light-1"}

    # 접근 신청: 승인 신청 이벤트가 나가고 도구 호출은 나가지 않는다
    approvals = [e for e in flow.events if e.type == "approval"]
    assert [(e.fields["kind"], e.fields["nodeId"], e.fields["requestId"]) for e in approvals] == [
        ("requested", "light-2", flow.world.requests["access"])]
    assert "mcp_call" not in types(flow.of("access"))
    assert approvals[0].subject == next(e for e in flow.of("ok") if e.type == "mcp_call").subject

    # 감사: 시작 뒤에 저장된 행을 rowid 순으로 빠짐없이 한 번씩
    stored = query(flow.world.db, "SELECT log_id FROM audit_log WHERE rowid > ? ORDER BY rowid", flow.audit_start)
    assert [e.fields["row"]["id"] for e in flow.events if e.type == "audit"] == [row["log_id"] for row in stored]

    # RSSI: 사람마다 기기별 마지막 dBm 하나. 마지막 주기에 모은 프레임이다
    rssi = [e for e in flow.events if e.type == "rssi"]
    assert {e.subject for e in rssi} == {e.subject for e in flow.events if e.type == "mcp_call"}
    for subject in {e.subject for e in rssi}:
        assert [e for e in rssi if e.subject == subject][-1].fields["rssi"] == dict.fromkeys(REPORTING, -55.0)


def test_FR12B_이미_낸_이벤트를_다시_내지_않는다(flow):
    assert flow.ticks[-1] == []  # 마지막 주기에는 더 바뀐 것이 없다
    keys = [(e.type, e.at, repr(e.fields)) for e in flow.events if e.type not in ("rssi", "audit")]
    assert len(keys) == len(set(keys))


def test_FR11_따라_읽기_이벤트에_사용자_ID와_토큰이_없다(flow):
    names = PersonNames()
    rendered = [render_event(event, names) for event in flow.events]

    # 호스트로 나가는 형태와 이벤트 본문만 본다. 이벤트의 `subject` 는 서버 안에서만 쓰고 스키마가 `personId` 로 바꾼다
    text = repr(rendered) + repr([dict(event.fields) for event in flow.events])
    assert {event["personId"] for event in rendered if "personId" in event} == {"p1", "p2"}  # 사람은 불투명한 이름표
    for secret in (VISITOR_A, VISITOR_B, *TOKENS.values()):
        assert secret not in text
