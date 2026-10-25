"""FR-12A·FR-12B 대시보드를 서버에 붙인 조립 (specs/core-dashboard-queries/spec.md 5절·8절).

실제 `Runtime`·`create_app`·SQLite 위에서 fake Paho 의 기기 시뮬레이터가 보고하고, `TestClient` 가 앱의 수명주기를 돌려
서버를 띄운다. 근접 추정만 fake 다 (`FakeProximity`). 판정은 MCP 요청이 닿는 `runtime.core` 를 직접 부른다.
서버는 모듈마다 한 번 띄워 (`flow`) 방문자 A 의 사물함, 방문자 B 의 공용 조명, 접근 신청을 만들고 스트림과 조회 REST 에서
받은 것을 담는다. 시나리오가 서로 섞이는 시험 (섹션 없음, 수집 전용, 발행 실패, 종료, 시작 실패) 은 서버를 따로 띄운다.
TestClient 의 웹소켓 수신에는 기한이 없어서 읽기 스레드가 원문을 큐에 쌓고 시험이 기한을 두고 꺼낸다.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from _runtime import (
    NODES, OWNER, TOKENS, VISITOR_A, VISITOR_B, FakeProximity, SimBroker, make_config, query, seed, wait_until,
)
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.api.ws.stream import STREAM_PATH
from nearpilot.core.dashboard import MonitorTail
from nearpilot.core.proximity.files import AcceptanceSettings
from nearpilot.core.proximity.model import load_models
from nearpilot.db.repositories import SqliteDashboardReads
from nearpilot.iot.mqtt import MqttConnection
from nearpilot.runtime import DashboardConfig, Runtime, create_app
from nearpilot.runtime.database import ThreadDatabase
from nearpilot.runtime.tasks import NotAccepting
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, ApprovalMode, Verdict
from nearpilot.shared.models import Decision, TargetSpec, UseRequest
from tools.evaluation.proximity import Metrics, metric_rows
from tools.evaluation.report import write_report

# 명시 지정 임계값을 낮춰 fake 확률 0.97 이 넘게 한다. 시험 도중에 기기가 오프라인이 되거나 비콘 수신이 끊기지 않게 시간을 길게 둔다
SETTINGS = {
    "threshold_explicit_locker": 0.5, "threshold_explicit_light": 0.5, "beacon_recent_sec": 30.0,
    "online_ttl_sec": 300.0, "leave_absent_sec": 600.0,
}
# 방문자 A 는 locker-1, 방문자 B 는 locker-2 앞이다. 둘 다 light-1 가까이 있다
NEAR = {VISITOR_A: {"locker-1": 0.97, "light-1": 0.9}, VISITOR_B: {"locker-2": 0.97, "light-1": 0.9}}
DASHBOARD_API = DashboardSettings(queue_max_events=1000, batch_max_events=50, auth_timeout_sec=5.0, audit_limit_max=200)
WEAK_RSSI_DBM = -80.0
# 계정 이름. 조회와 이벤트에 이름이 새는지 이름으로 찾도록 눈에 띄는 값을 쓴다
ACCOUNT_NAMES = {OWNER: "acct-name-owner", VISITOR_A: "acct-name-a", VISITOR_B: "acct-name-b"}
REQUEST_EVENTS = ["mcp_call", *["step"] * 7, "posterior", "verdict", "command", "exec_result", "mcp_result"]


# ── 서버와 스트림 ────────────────────────────────────────────────────


class Server:
    """실제 조립 서버 하나: 시드한 DB, fake Paho 위의 기기 시뮬레이터, `create_app` 앱, 그 수명주기를 돌리는 `TestClient`.

    `with` 에 들어가면 서버가 뜨고 나오면 멈춘다. `dashboard=False` 면 `[dashboard]` 가 없는 설정이다.
    `tail_interval_sec` 을 크게 주면 따라 읽기가 스스로 돌지 않아서 시험이 `tick()` 을 부른다.
    """

    def __init__(self, tmp: Path, *, dashboard: bool = True, collect_only: bool = False,
                 tail_interval_sec: float = 0.05) -> None:
        cfg = make_config(tmp, settings=SETTINGS)
        if dashboard:
            cfg = replace(cfg, dashboard=DashboardConfig(DASHBOARD_API, WEAK_RSSI_DBM, tail_interval_sec, tmp / "reports"))
        self.cfg, self.db = cfg, cfg.database.path
        seed(self.db, modes={"light-2": "restricted"})  # light-2 는 보고하지 않는 기기이고 승인이 필요하다
        self._name_accounts()
        self.broker = SimBroker(tmp)
        self.proximity = FakeProximity(NEAR)
        self.runtime = Runtime(cfg, client_factory=self.broker.factory, collect_only=collect_only,
                               proximity_factory=lambda models, s: self.proximity)
        self.app = create_app(self.runtime)
        self.client: TestClient
        self._stack = ExitStack()

    def __enter__(self) -> Server:
        with ExitStack() as stack:
            stack.callback(self.broker.close)  # 맨 나중에: 서버가 멈춘 뒤 시뮬레이터 저장소를 닫는다
            self.client = stack.enter_context(TestClient(self.app))
            self._stack = stack.pop_all()
        return self

    def __exit__(self, *exc_info) -> bool | None:
        return self._stack.__exit__(*exc_info)

    def _name_accounts(self) -> None:
        conn = sqlite3.connect(str(self.db))
        try:
            conn.executemany("UPDATE user SET name = ? WHERE user_id = ?",
                             [(name, user_id) for user_id, name in ACCOUNT_NAMES.items()])
            conn.commit()
        finally:
            conn.close()

    def get(self, path: str, token: str | None = TOKENS[OWNER]):
        return self.client.get(path, headers={} if token is None else {"Authorization": f"Bearer {token}"})

    def ready(self) -> None:
        """기기마다 실행할 수 있고, 시뮬레이터가 보낸 RSSI 프레임을 모두 저장할 때까지 기다린다."""
        restore = self.runtime.services.restore
        wait_until(lambda: all(restore.can_execute(node_id) for node_id in self.broker.nodes))
        self.send_rssi()

    def send_rssi(self) -> None:
        before = self.frames()
        self.broker.rssi()
        sent = sum(len(node.scenario.beacons) for node in self.broker.nodes.values())
        wait_until(lambda: self.frames() >= before + sent)

    def frames(self) -> int:
        return query(self.db, "SELECT count(*) c FROM rssi_observation")[0]["c"]

    def use(self, user_id: str, target: str, request_id: str, action: Action = Action.OPEN) -> Decision:
        return self.runtime.core.request_use(UseRequest(
            user_id, request_id, TargetSpec.parse(target), action, None, datetime.now(timezone.utc)))

    def access(self, user_id: str, node_id: str, action: Action, request_id: str) -> Decision:
        return self.runtime.core.request_access(user_id, node_id, action, request_id)

    def commands(self, node_id: str) -> list[str]:
        rows = query(self.db, "SELECT status FROM node_command WHERE node_id = ? ORDER BY rowid", node_id)
        return [row["status"] for row in rows]


class Stream:
    """모니터링 스트림 연결 하나. `texts` 는 받은 메시지 원문 전부, `events` 는 그 안의 이벤트 전부다.

    `close()` 는 서버를 멈추기 전에 부른다. 읽기 스레드가 기다리는 수신은 연결을 닫아야 끝난다.
    """

    def __init__(self, client: TestClient, token: str) -> None:
        self.texts: list[str] = []
        self.events: list[dict] = []
        self._inbox: queue.Queue = queue.Queue()
        self._session = client.websocket_connect(STREAM_PATH)
        self._ws = self._session.__enter__()
        self._ws.send_json({"type": "auth", "token": token})
        self._reader = threading.Thread(target=self._read, name="stream-reader", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        try:
            while True:
                self._inbox.put(self._ws.receive_text())
        except BaseException as error:  # noqa: BLE001 - 연결이 닫혔다. 기다리는 시험이 본다
            self._inbox.put(error)

    def pull_message(self, timeout: float = 10.0) -> list[dict]:
        """메시지 하나를 기다려 그 이벤트들을 돌려준다. 기한을 넘기거나 연결이 닫히면 AssertionError 다."""
        try:
            item = self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("스트림 메시지를 기다리다 시간이 지났다") from None
        if isinstance(item, BaseException):
            raise AssertionError(f"스트림이 닫혔다: {type(item).__name__}") from item
        self.texts.append(item)
        events = json.loads(item)["events"]
        self.events.extend(events)
        return events

    def pull_until(self, done: Callable[[list[dict]], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not done(self.events):
            self.pull_message(max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        self._ws.close(1000)
        try:
            self._session.__exit__(None, None, None)
        except CancelledError:  # 핸들러가 정리하는 중에 TestClient 가 취소했다. 연결은 이미 닫혔다
            pass
        self._reader.join(5)


def of(events: list[dict], request_id: str) -> list[dict]:
    return [event for event in events if event.get("requestId") == request_id]


def has_result(request_id: str) -> Callable[[list[dict]], bool]:
    return lambda events: any(e["type"] == "mcp_result" for e in of(events, request_id))


# ── 모듈마다 한 번 띄우는 서버 ───────────────────────────────────────


@dataclass
class Flow:
    """흐름이 끝난 서버 (아직 떠 있다). 스트림에서 받은 것과 조회 REST 로 받은 것 (상태 코드, 본문 원문)."""

    server: Server
    stream: Stream
    current: list[dict]  # 연결 직후 받은 지금 상태
    decisions: dict[str, Decision]
    rest: dict[str, tuple[int, str]]

    @property
    def events(self) -> list[dict]:
        return self.stream.events

    def rid(self, name: str) -> str:
        return self.decisions[name].internal_request_id

    def body(self, name: str):
        return json.loads(self.rest[name][1])


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    with Server(tmp_path_factory.mktemp("dashboard")) as server:
        server.ready()
        stream = Stream(server.client, TOKENS[OWNER])
        try:
            current = stream.pull_message()
            decisions: dict[str, Decision] = {}

            # 기기가 보낸 RSSI 가 스트림까지 온다
            mark = len(stream.events)
            server.send_rssi()
            stream.pull_until(lambda events: any(e["type"] == "rssi" for e in events[mark:]))

            # 방문자 A 가 사물함을, 방문자 B 가 공용 조명을 연다. 결과가 저장되면 mcp_result 가 온다
            for name, user_id, target, action in (
                ("open", VISITOR_A, "locker-1", Action.OPEN), ("light", VISITOR_B, "light-1", Action.ON),
            ):
                decisions[name] = server.use(user_id, target, name, action)
                assert decisions[name].verdict is Verdict.OK, name
                stream.pull_until(has_result(decisions[name].internal_request_id))

            # 승인이 필요한 기기의 접근 신청
            decisions["access"] = server.access(VISITOR_A, "light-2", Action.ON, "access")
            assert decisions["access"].verdict is Verdict.NEED_APPROVAL
            stream.pull_until(lambda events: any(e["type"] == "approval" for e in events))

            paths = {
                "places": "/dashboard/places", "uses": "/dashboard/uses", "approvals": "/dashboard/approvals",
                "audit": "/dashboard/audit?limit=5", "history": "/dashboard/history?period=today",
                "request": f"/dashboard/requests/{decisions['open'].internal_request_id}",
                "trust": "/dashboard/trust", "rental": "/dashboard/rentals/locker-1", "health": "/dashboard/health",
            }
            rest = {}
            for name, path in paths.items():
                response = server.get(path)
                rest[name] = (response.status_code, response.text)
            yield Flow(server, stream, current, decisions, rest)
        finally:
            stream.close()


def check_stream(flow: Flow) -> None:
    # 연결 직후: 기기마다 transition, 활성 비콘의 사람마다 beacon
    assert sorted(e["nodeId"] for e in flow.current if e["type"] == "transition") == sorted(NODES)
    assert [e["type"] for e in flow.current].count("beacon") == 3  # 소유자, 방문자 A, 방문자 B

    # 사물함을 열면 그 요청의 이벤트가 판정 순서대로 스트림으로 온다
    mine = of(flow.events, flow.rid("open"))
    assert [e["type"] for e in mine] == REQUEST_EVENTS
    call, verdict, command, result = (e for e in mine if e["type"] in ("mcp_call", "verdict", "command", "exec_result"))
    assert (call["tool"], call["action"], call["target"]["nodeId"]) == ("request_use", "open", "locker-1")
    assert (verdict["verdict"], verdict["reason"], verdict["nodeId"]) == ("OK", "OK", "locker-1")
    assert (command["nodeId"], command["action"]) == ("locker-1", "open")
    assert (result["nodeId"], result["result"]) == ("locker-1", "succeeded")
    assert [e["to"] for e in flow.events[len(flow.current):] if e["type"] == "transition" and e["nodeId"] == "locker-1"][-1] == "ACTIVE"

    # 공용 조명도 같은 줄기다. 접근 신청은 도구 호출이 아니라 승인 신청과 감사로 온다
    light = [e["type"] for e in of(flow.events, flow.rid("light"))]
    assert light[0] == "mcp_call" and light[-3:] == ["command", "exec_result", "mcp_result"] and "verdict" in light
    requested = [e for e in of(flow.events, flow.rid("access")) if e["type"] == "approval"]
    assert [(e["kind"], e["nodeId"]) for e in requested] == [("requested", "light-2")]
    assert not any(e["type"] == "mcp_call" for e in of(flow.events, flow.rid("access")))

    # 기기 RSSI 와 감사 기록도 온다
    rssi = [e for e in flow.events if e["type"] == "rssi"]
    assert rssi and all(set(e["rssi"]) <= set(NODES) and e["personId"] for e in rssi)
    audits = [e["row"] for e in flow.events if e["type"] == "audit"]
    assert audits and all(set(row) >= {"id", "at", "step", "text", "code"} for row in audits)


def check_rest(flow: Flow) -> None:
    assert {name: status for name, (status, _) in flow.rest.items()} == dict.fromkeys(flow.rest, 200)

    # 장소: 시작할 때 읽은 모델의 버전, 이름이 없는 기기는 nodeId 가 이름이다
    cfg = flow.server.cfg
    model = load_models(cfg.proximity.model_root, cfg.proximity.versions)["place-a"]
    places = flow.body("places")
    assert places["weakRssiDbm"] == WEAK_RSSI_DBM
    [place] = places["places"]
    assert (place["placeId"], place["modelVersion"], place["calibrationVersion"]) == (
        "place-a", model.model_version, model.calib_version)
    assert {d["nodeId"] for d in place["devices"]} == set(NODES)
    assert all(d["name"] == d["nodeId"] for d in place["devices"])

    # 점유: 대여형이 먼저, 공용형은 유지 대상자
    assert [(u["nodeId"], u["occupancy"], u["session"]) for u in flow.body("uses")] == [
        ("locker-1", "rental", "ACTIVE"), ("light-1", "shared", "ACTIVE")]
    approvals = flow.body("approvals")
    assert [len(approvals[section]) for section in ("pending", "active", "ended")] == [1, 0, 0]
    assert (approvals["pending"][0]["nodeId"], approvals["pending"][0]["mode"]) == ("light-2", ApprovalMode.ONE_TIME.value)
    audit = flow.body("audit")
    assert len(audit) == 5 and all(set(row) >= {"id", "at", "step", "text", "code"} for row in audit)

    # 이력: 접근 신청은 없고 최근 것부터. 스트림과 같은 사람은 같은 personId 다
    history = flow.body("history")
    assert [row["requestId"] for row in history] == [flow.rid("light"), flow.rid("open")]
    opened = history[1]
    assert (opened["verdict"], opened["outcome"], opened["nodeId"]) == ("OK", "열림", "locker-1")
    call = next(e for e in of(flow.stream.events, flow.rid("open")) if e["type"] == "mcp_call")
    assert opened["personId"] == call["personId"]

    # 요청 한 건: 일곱 단계가 모두 지났고 실행 결과와 응답 시각이 있다
    request = flow.body("request")
    assert (request["id"], request["tool"], request["verdict"], request["reason"]) == (
        flow.rid("open"), "request_use", "OK", "OK")
    assert [step["result"] for step in request["steps"]] == ["pass"] * 7
    assert request["execution"] == "succeeded" and request["respondedAt"] and request["personId"] == call["personId"]

    # 신뢰도: 서버를 시작한 뒤의 판정 둘. 평가 리포트 폴더가 없으니 evaluation 은 없다
    live = flow.body("trust")["live"]
    assert (live["decisions"], live["doubleOccupancy"]) == (2, 0)
    assert live["counts"]["OK"] == 2 and live["latencyMedianSec"] is not None
    assert "evaluation" not in flow.body("trust")

    rental = flow.body("rental")
    assert rental["nodeId"] == "locker-1" and rental["timeline"][-1].get("now") is True

    # 점검: 보고한 기기만 연결됐고, MQTT 연결과 모델은 서버가 시작한 뒤에 붙인 값이다
    health = flow.body("health")
    assert {d["nodeId"]: d["connected"] for d in health["devices"]} == {
        node_id: node_id in flow.server.broker.nodes for node_id in NODES}
    assert len(health["beacons"]) == 3
    assert health["server"] == {"database": True, "mqtt": True, "model": f"{model.model_version} · {model.calib_version}"}


def test_FR12B_서버에서_대시보드까지_이어진다(flow):
    check_stream(flow)
    check_rest(flow)


def test_FR12A_소유자_토큰만_조회를_받는다(flow):
    assert flow.server.get("/dashboard/places", TOKENS[VISITOR_A]).status_code == 403
    assert flow.server.get("/dashboard/places", None).status_code == 401
    assert flow.server.get("/dashboard/places", "wrong-token").status_code == 401


# 응답에는 사람의 role 값(`owner`, `visitor`)이 들어간다. 시드의 소유자 ID 도 `owner` 라서 그 값만 빼고 찾는다
_ROLE = re.compile(r'"role":\s*"(?:owner|visitor)"')


def test_FR11_조회와_이벤트에_사용자_ID가_없다(flow):
    texts = [*flow.stream.texts, *(text for _, text in flow.rest.values())]
    assert len(texts) > len(flow.rest) and all(texts)
    assert sum(1 for text in texts if _ROLE.search(text)) > 0  # role 을 뺀 뒤에도 비교할 본문이 남는다
    secrets = [OWNER, VISITOR_A, VISITOR_B, *TOKENS.values(), *ACCOUNT_NAMES.values()]
    for text in texts:
        scrubbed = _ROLE.sub('"role":""', text)
        assert [secret for secret in secrets if secret in scrubbed] == []


# ── 서버를 따로 띄우는 시험 ──────────────────────────────────────────


def test_FR12A_대시보드_섹션이_없으면_라우터를_붙이지_않는다(tmp_path):
    with Server(tmp_path, dashboard=False) as server:
        runtime = server.runtime
        assert runtime.dashboard is None and runtime.config.dashboard is None
        assert runtime.tasks and not any(task.name == "dashboard_tail" for task in runtime.tasks)
        assert not any(getattr(route, "path", "").startswith("/dashboard") for route in server.app.routes)

        response = server.get("/dashboard/places")  # 소유자 토큰이어도 MCP 앱으로 간다
        assert response.status_code == 404 and "weakRssiDbm" not in response.text
        assert server.get("/dashboard/health").status_code == 404


def test_FR12A_수집_전용_서버에도_대시보드를_붙인다(tmp_path):
    with Server(tmp_path, collect_only=True) as server:
        response = server.get("/dashboard/places")
        assert response.status_code == 200
        [place] = response.json()["places"]
        assert (place["modelVersion"], place["calibrationVersion"]) == ("없음", "없음")  # 모델을 읽지 않았다
        # 수집 전용은 판정 요청이 닿을 경로를 열지 않는다 (FR-06). 대시보드 라우터가 그 경로를 만들지 않는다
        assert server.client.post("/mcp", json={}).status_code == 404


def test_FR12A_점검_mqtt는_연결_준비_상태를_따른다(tmp_path, monkeypatch):
    server = Server(tmp_path)
    before_start = server.runtime.dashboard.queries._mqtt_ready()  # 조회 서비스는 서버가 시작하기 전에 만든다

    def mqtt() -> bool:
        return server.get("/dashboard/health").json()["server"]["mqtt"]

    with server:
        assert before_start is False  # 시작 전에는 연결 상태가 거짓이다 (연결을 만들기 전이라 서비스가 없다)
        assert mqtt() is True  # 연결이 준비됐다
        with monkeypatch.context() as patch:
            patch.setattr(MqttConnection, "ready", property(lambda self: False))
            assert mqtt() is False  # 서버가 떠 있어도 연결이 준비되지 않았으면 거짓이다
        assert mqtt() is True


def test_FR12A_신뢰도는_설정한_평가_리포트_폴더를_읽는다(tmp_path):
    reports = tmp_path / "reports"  # Server 가 DashboardConfig.reports_dir 로 넘기는 폴더다
    reports.mkdir()
    rows = metric_rows(Metrics(0.04, 100, 4, 0.049, 250), AcceptanceSettings(max_misselection=0.05, max_ece=0.05))
    write_report(
        rows, db_path="np.db", schema_version=1, sample_count=250,
        generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        csv_path=str(reports / "lab.csv"), md_path=str(reports / "lab.md"), git_commit="0123abc",
    )
    with Server(tmp_path) as server:
        evaluation = server.get("/dashboard/trust").json()["evaluation"]

    assert evaluation["date"] == "2026-01-02"
    assert [metric["name"] for metric in evaluation["metrics"]] == ["앞 기기 오선택률", "보정 오차", "전환 시간", "흔들림"]


def test_FR12A_종료_중_조회는_받지_않는다(tmp_path):
    with Server(tmp_path) as server:
        server.ready()
        queries = server.runtime.dashboard.queries
        assert queries.places()["places"]  # 열려 있을 때는 읽는다

        server.runtime.stop()  # 문이 닫히고 주기 작업이 멈춘다. 열린 DB 연결 없이 끝난다 (ShutdownError 가 없다)
        assert server.runtime.db.open_connections == 0
        for read in (
            queries.places, queries.uses, queries.approvals, lambda: queries.audit(5),
            lambda: queries.history("today", None, None), lambda: queries.request("none"), queries.trust,
            lambda: queries.rental("locker-1"), queries.health, queries.current_events,
        ):
            with pytest.raises(NotAccepting):
                read()
        assert server.runtime.db.open_connections == 0  # 거절한 조회가 연결을 열지 않았다
        assert server.get("/dashboard/places").status_code == 500  # API 는 서버 오류로 답한다


def test_FR12A_종료는_진행_중인_조회를_기다린다(tmp_path, monkeypatch):
    with Server(tmp_path) as server:
        server.ready()
        queries = server.runtime.dashboard.queries
        inside, release = threading.Event(), threading.Event()
        real = SqliteDashboardReads.places

        def slow(self):
            inside.set()
            assert release.wait(10)
            return real(self)

        monkeypatch.setattr(SqliteDashboardReads, "places", slow)
        result: dict = {}
        reading = threading.Thread(target=lambda: result.update(places=queries.places()))
        reading.start()
        assert inside.wait(5)

        stopping = threading.Thread(target=server.runtime.stop)
        stopping.start()
        stopping.join(0.5)
        assert stopping.is_alive()  # 조회가 접수 문 안에 있어서 종료가 기다린다
        release.set()
        reading.join(5)
        stopping.join(10)
        assert not stopping.is_alive() and result["places"]["places"]  # 조회는 끝까지 읽었고 종료도 끝났다
        assert server.runtime.db.open_connections == 0


def test_NFR07_발행_실패가_판정을_바꾸지_않는다(tmp_path, monkeypatch, caplog):
    with Server(tmp_path, tail_interval_sec=3600) as server:  # 따라 읽기가 스스로 돌지 않는다. tick 은 시험이 부른다
        server.ready()
        parts = server.runtime.dashboard
        assert next(task for task in server.runtime.tasks if task.name == "dashboard_tail").interval_sec == 3600

        def fail(event):
            raise RuntimeError(f"발행 실패 {VISITOR_A} {TOKENS[VISITOR_A]}")

        # 허브 발행이 예외를 내도 판정과 실행 결과는 그대로이고 tick 은 예외를 올리지 않는다
        first = server.use(VISITOR_A, "locker-1", "r1")
        assert (first.verdict, first.reason.value) == (Verdict.OK, "OK")
        wait_until(lambda: server.commands("locker-1") == ["succeeded"])
        with monkeypatch.context() as patch, caplog.at_level(logging.WARNING):
            patch.setattr(parts.hub, "publish", fail)
            parts.tail.tick()
        assert "예외=RuntimeError" in caplog.text  # 발행을 시도했고 실패를 로그에 남겼다
        assert VISITOR_A not in caplog.text and TOKENS[VISITOR_A] not in caplog.text  # 로그에 사용자 ID 와 토큰이 없다

        # 따라 읽기의 읽기가 실패해도 마찬가지이고, 읽지 못한 행은 다음 주기에 나간다
        second = server.use(VISITOR_B, "locker-2", "r2")
        assert (second.verdict, second.reason.value) == (Verdict.OK, "OK")
        wait_until(lambda: server.commands("locker-2") == ["succeeded"])

        def unreadable(self, seq):
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as patch:
            patch.setattr(SqliteDashboardReads, "requests_after", unreadable)
            parts.tail.tick()
        published: list = []
        monkeypatch.setattr(parts.hub, "publish", published.append)
        parts.tail.tick()
        types = [event.type for event in published if event.fields.get("requestId") == second.internal_request_id]
        assert types[0] == "mcp_call" and types[-1] == "mcp_result" and "verdict" in types
        assert server.commands("locker-1") == ["succeeded"] and server.commands("locker-2") == ["succeeded"]
    # 주기 작업이 도는 동안에는 열린 연결 수가 0 이 아닐 수 있다. 서버가 멈춘 뒤에 남은 연결이 없어야 한다
    assert server.runtime.db.open_connections == 0


def test_FR12B_따라_읽기는_주기_작업과_요청_접수보다_먼저_시작한다(tmp_path, monkeypatch):
    server = Server(tmp_path)
    seen: list[tuple[bool, list[str]]] = []
    real = MonitorTail.start

    def start(self):
        seen.append((server.runtime.gate.accepting, [task.name for task in server.runtime.tasks if task.started]))
        real(self)

    monkeypatch.setattr(MonitorTail, "start", start)
    with server:
        assert seen == [(False, [])]  # 접수 문이 닫혀 있고 주기 작업이 하나도 돌지 않을 때 부른다
        task = next(task for task in server.runtime.tasks if task.name == "dashboard_tail")
        assert task.started and task.interval_sec == server.cfg.dashboard.tail_interval_sec


def test_FR12B_따라_읽기를_시작하지_못하면_서버_시작이_실패한다(tmp_path, monkeypatch):
    server = Server(tmp_path)

    def broken(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(MonitorTail, "start", broken)
    try:
        with pytest.raises(sqlite3.OperationalError):
            server.runtime.start()
        runtime = server.runtime
        assert not runtime.gate.accepting and server.broker.client.stopped  # 요청을 받지 않고 MQTT 를 정리했다
        assert runtime.db.open_connections == 0 and not any(task.started for task in runtime.tasks)
    finally:
        server.broker.close()


def test_FR12A_읽기_범위는_연결을_다시_쓰고_트랜잭션을_남기지_않는다(tmp_path):
    db = ThreadDatabase(tmp_path / "np.db", settings=Settings(), audit_jsonl=tmp_path / "audit.jsonl",
                        busy_timeout_ms=5000)
    with db.scope() as bound:  # 주기 작업의 범위다
        with db.dashboard_reads() as reads:
            assert db.open_connections == 1  # 같은 연결을 쓴다
            assert bound.conn.in_transaction
            reads.ping()
        assert not bound.conn.in_transaction  # 읽기 트랜잭션은 롤백했고 범위의 연결에는 아무것도 남지 않았다
        with pytest.raises(RuntimeError, match="읽다 실패"):
            with db.dashboard_reads():
                raise RuntimeError("읽다 실패")
        assert not bound.conn.in_transaction and db.open_connections == 1
    with db.dashboard_reads() as reads:  # 범위 밖에서 부르면 이 읽기가 연결 범위를 연다
        assert db.open_connections == 1
        reads.ping()
    assert db.open_connections == 0


def test_NFR03_대시보드_읽기는_쓰기_잠금을_잡지_않는다(tmp_path):
    path = tmp_path / "np.db"
    # 쓰기 잠금을 기다리는 읽기가 오래 붙잡히지 않고 실패로 드러나도록 기다리는 시간을 작게 둔다
    db = ThreadDatabase(path, settings=Settings(), audit_jsonl=tmp_path / "audit.jsonl", busy_timeout_ms=100)
    with db.scope():  # 스키마를 만든다
        pass
    writer = sqlite3.connect(str(path), isolation_level=None)
    probe = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")  # 판정 쪽 쓰기처럼 다른 연결이 쓰기 잠금을 쥐고 있다
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            probe.execute("BEGIN IMMEDIATE")  # 잠금이 실제로 쥐어져 있다

        with db.dashboard_reads() as reads:  # 읽기 트랜잭션이 쓰기 잠금을 얻으려 했다면 여기서 실패한다
            reads.ping()
            assert reads.places() == []
    finally:
        probe.close()
        writer.close()
    assert db.open_connections == 0
