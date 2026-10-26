"""서버 진입점과 서비스 조립 (specs/core-server-assembly/spec.md 5절·8절).

실제 SQLite·runtime 조립·IOT 보고 처리 위에서 시뮬레이터를 fake Paho 로 연결한다.
근접 추정은 fake 로 고정한다. 단, 근접 복원 검사는 실제 HMM 을 쓴다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from _runtime import (
    OWNER, TOKENS, UUID, VISITOR_A, VISITOR_B, FakeProximity, McpCaller, SimBroker, make_config,
    query, running, seed, wait_until,
)
from nearpilot.core.lifecycle.node_events import NodeEventService
from nearpilot.core.lifecycle.restore import RestoreService
from nearpilot.core.proximity.estimator import HmmProximityEstimator
from nearpilot.db.repositories.retention import SqliteRetentionStore
from nearpilot.iot.mqtt import ConnectionFailure, ReportDelivery
from nearpilot.runtime import Runtime, ShutdownError, create_app
from nearpilot.runtime.database import ThreadDatabase
from nearpilot.runtime.tasks import NotAccepting, PeriodicTask
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, ReasonCode, Verdict
from nearpilot.shared.models import BeaconKey, RssiFrame, TargetSpec, UseRequest

EXPLICIT = {"threshold_explicit_locker": 0.5, "threshold_explicit_light": 0.5}
# 방문자 A 는 locker-1, 방문자 B 는 locker-2 앞이다. 둘 다 light-1 가까이 있다.
NEAR = {VISITOR_A: {"locker-1": 0.97, "light-1": 0.9}, VISITOR_B: {"locker-2": 0.97, "light-1": 0.9}}


class World:
    """설정, 시드한 DB, 시뮬레이터와 Runtime 하나."""

    def __init__(self, tmp, *, settings=None, runtime=None, database=None, posteriors=None,
                 node_ids=("locker-1", "locker-2", "light-1"), proximity_factory=None, seeded=True):
        self.tmp = tmp
        self.cfg = make_config(tmp, settings={**EXPLICIT, **(settings or {})}, runtime=runtime,
                               database=database)
        if seeded:
            seed(self.cfg.database.path)
        self.broker = SimBroker(tmp, node_ids)
        self.proximity = FakeProximity(posteriors or NEAR)
        self.runtime = Runtime(self.cfg, client_factory=self.broker.factory,
                               proximity_factory=proximity_factory or (lambda models, s: self.proximity))

    @property
    def db(self):
        return self.cfg.database.path

    def ready(self, *node_ids):
        restore = self.runtime.services.restore
        wait_until(lambda: all(restore.can_execute(n) for n in node_ids))
        before = self._rssi_count()
        # 상태를 보고하지 않은 기기의 RSSI 는 IOT 가 거부한다
        sent = sum(len(node.scenario.beacons) for node_id, node in self.broker.nodes.items()
                   if node_id not in self.broker.silent)
        self.broker.rssi()
        wait_until(lambda: self._rssi_count() >= before + sent)  # 보낸 프레임을 모두 저장했다

    def _rssi_count(self):
        return query(self.db, "SELECT count(*) c FROM rssi_observation")[0]["c"]

    def use(self, user_id, target="locker-1", request_id="r1", action=Action.OPEN):
        return self.runtime.core.request_use(UseRequest(
            user_id, request_id, TargetSpec.parse(target), action, None, datetime.now(timezone.utc)))

    def commands(self, node_id=None):
        rows = query(self.db, "SELECT node_id, action, status FROM node_command")
        return [dict(r) for r in rows if node_id is None or r["node_id"] == node_id]

    def close(self):
        self.broker.close()


@pytest.fixture
def world(tmp_path):
    worlds = []

    def make(**kwargs):
        tmp = tmp_path / f"w{len(worlds)}"
        tmp.mkdir()
        w = World(tmp, **kwargs)
        worlds.append(w)
        return w

    yield make
    for w in worlds:
        try:
            w.runtime.stop()
        except ShutdownError:
            pass
        w.close()


# ── 시작 순서 ──────────────────────────────────────────────────────


def test_FR06_runtime_requires_suback(world, monkeypatch):
    w = world()
    w.broker.granted = [1, 0, 1, 0]  # 결과 구독의 QoS 가 다르다
    restored = []
    monkeypatch.setattr(RestoreService, "restore", lambda self: restored.append(True))
    with pytest.raises(ConnectionFailure):
        w.runtime.start()
    assert restored == [] and not w.runtime.gate.accepting  # 복원과 요청 접수로 가지 않는다
    with pytest.raises(NotAccepting):
        w.use(VISITOR_A)
    assert w.broker.client.stopped and w.broker.commands() == []


def test_FR05_runtime_buffers_startup_reports(world, monkeypatch):
    w = world()
    events = []
    real_restore, real_status = RestoreService.restore, NodeEventService.on_status

    def restore(self):
        events.append(("restore", datetime.now(timezone.utc)))
        real_restore(self)

    def on_status(self, status):
        events.append(("status", status.received_at))
        real_status(self, status)

    monkeypatch.setattr(RestoreService, "restore", restore)
    monkeypatch.setattr(NodeEventService, "on_status", on_status)
    # SUBACK 전에 보고가 하나 더 온다. 보고 작업 시작 전까지 수신 큐에 남는다.
    w.broker.before_ack = lambda client: client._deliver("locker-1", [w.broker.nodes["locker-1"].status()])
    with running(w.runtime):
        w.ready("locker-1")
        kinds = [kind for kind, _ in events]
        assert kinds[0] == "restore" and kinds.count("restore") >= 1
        first_status = next(at for kind, at in events if kind == "status")
        restore_at = events[0][1]
        # 최초 수신 시각을 보존한다. 인계는 최초 복원 뒤다.
        assert first_status <= restore_at
        assert w.use(VISITOR_A).verdict is Verdict.OK


@pytest.mark.parametrize("target", ["restore", "cleanup"])
def test_FR19_runtime_startup_restore_cleanup_failure(world, monkeypatch, target):
    w = world()
    handed = []
    monkeypatch.setattr(NodeEventService, "on_status", lambda self, status: handed.append(status))

    def fail(*args):
        raise sqlite3.OperationalError("disk I/O error")

    if target == "restore":
        monkeypatch.setattr(RestoreService, "restore", fail)
    else:
        monkeypatch.setattr(SqliteRetentionStore, "cleanup", fail)
    with pytest.raises(sqlite3.OperationalError):
        w.runtime.start()
    assert not w.runtime.gate.accepting and w.broker.client.stopped
    time.sleep(0.1)
    assert handed == []  # 보고 인계 작업을 시작하지 않았다
    assert w.runtime.db.open_connections == 0 and not any(t.started for t in w.runtime.tasks)


def test_NFR04_runtime_partial_startup_cleanup(world, monkeypatch):
    w = world()

    def fail(self):
        raise RuntimeError("worker start failed")

    monkeypatch.setattr(ReportDelivery, "start", fail)
    with pytest.raises(RuntimeError, match="worker start failed"):
        w.runtime.start()
    # 시작한 MQTT 만 멈춘다. 시작하지 않은 주기 작업은 건드리지 않는다.
    assert w.broker.client.stopped and not any(t.started for t in w.runtime.tasks)
    assert w.runtime.db.open_connections == 0 and not w.runtime.gate.accepting
    w.runtime.stop()  # 실패한 Runtime 의 종료는 아무것도 하지 않는다


# ── 차단과 판정 ────────────────────────────────────────────────────


def test_FR09_runtime_unset_threshold_stays_blocked(world):
    w = world(settings={"threshold_explicit_locker": None}, node_ids=("locker-1", "speaker-1"),
              posteriors={"locker-1": 0.99, "speaker-1": 0.99})
    assert w.cfg.settings.threshold_speaker is None
    with running(w.runtime):
        w.ready("locker-1", "speaker-1")
        explicit = w.use(VISITOR_A, "locker-1")
        speaker = w.use(VISITOR_A, "nearest:speaker", "r2", Action.ON)
        assert (explicit.verdict, explicit.reason) == (Verdict.DENY, ReasonCode.INVALID_INPUT)
        assert speaker.verdict is Verdict.DENY and speaker.use_id is None
        assert w.broker.commands() == [] and w.commands() == []


def test_NFR04_runtime_pending_node_stays_blocked(world):
    w = world()
    w.broker.silent = {"locker-2"}  # 현재 상태를 보고하지 않는다
    with running(w.runtime):
        w.ready("locker-1")
        restore = w.runtime.services.restore
        assert restore.can_execute("locker-1") and not restore.can_execute("locker-2")
        blocked = w.use(VISITOR_B, "locker-2", "r1")
        assert blocked.verdict is not Verdict.OK and blocked.use_id is None
        ok = w.use(VISITOR_A, "locker-1", "r2")
        assert ok.verdict is Verdict.OK
        assert [c["node_id"] for c in w.broker.commands()] == ["locker-1"]


def test_FR23_runtime_shares_allocation_window(world):
    w = world(settings={"allocation_window_ms": 300})
    with running(w.runtime):
        w.ready("locker-1", "locker-2")
        window = w.runtime.services.window
        batches, allocate = [], window._allocate
        window._allocate = lambda requests: batches.append(len(requests)) or allocate(requests)
        results = {}

        def ask(user_id):
            results[user_id] = w.use(user_id, "any:locker", f"any-{user_id}")

        threads = [threading.Thread(target=ask, args=(u,)) for u in (VISITOR_A, VISITOR_B)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert batches == [2]  # 두 요청을 한 대기창으로 묶었다
        assert {d.verdict for d in results.values()} == {Verdict.OK}
        assert {d.node_id for d in results.values()} == {"locker-1", "locker-2"}


# ── DB 연결과 감사 ─────────────────────────────────────────────────


def test_NFR03_runtime_thread_owned_connections(tmp_path):
    db = ThreadDatabase(tmp_path / "np.db", settings=Settings(), audit_jsonl=tmp_path / "a.jsonl",
                        busy_timeout_ms=5000)
    seen = {}
    with db.scope() as outer:
        with db.scope() as inner:
            assert inner.conn is outer.conn  # 중첩 호출은 같은 연결이다

        def other():
            with db.scope() as mine:
                seen["conn"] = mine.conn
                seen["count"] = db.open_connections
            try:
                outer.conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                seen["shared"] = False

        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert seen["conn"] is not outer.conn and seen["count"] == 2 and seen["shared"] is False
    assert db.open_connections == 0
    with pytest.raises(sqlite3.ProgrammingError):
        outer.conn.execute("SELECT 1")  # 최상위 범위가 끝나면 닫는다


def test_NFR03_runtime_requests_and_results_from_other_threads(world):
    w = world()
    with running(w.runtime):
        w.ready("locker-1", "locker-2")
        results = {}
        threads = [threading.Thread(target=lambda u=u, n=n: results.setdefault(u, w.use(u, n, f"r-{u}")))
                   for u, n in ((VISITOR_A, "locker-1"), (VISITOR_B, "locker-2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        # 요청 스레드와 MQTT 결과 인계 스레드가 각자 연결로 같은 사건을 정산했다.
        assert {d.verdict for d in results.values()} == {Verdict.OK}
        assert sorted(c["status"] for c in w.commands()) == ["succeeded", "succeeded"]
    assert w.runtime.db.open_connections == 0


def test_FR22_runtime_jsonl_failure_is_nonblocking(world):
    w = world(database={"audit_jsonl": "jsonl-is-a-directory"})
    (w.tmp / "jsonl-is-a-directory").mkdir()
    with running(w.runtime):
        w.ready("locker-1")
        assert w.use(VISITOR_A).verdict is Verdict.OK
    assert w.commands("locker-1")[0]["status"] == "succeeded"
    assert query(w.db, "SELECT count(*) c FROM audit_log")[0]["c"] > 0  # 원본 테이블은 저장했다


def test_FR22_runtime_jsonl_exports_audit(world):
    w = world()
    with running(w.runtime):
        w.ready("locker-1")
        w.use(VISITOR_A)
    lines = (w.tmp / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == query(w.db, "SELECT count(*) c FROM audit_log")[0]["c"]


def test_FR22_runtime_cleanup_retries(world, monkeypatch, caplog):
    w = world(settings={"retention_cleanup_interval_sec": 1})
    calls = []
    real = SqliteRetentionStore.cleanup

    def cleanup(self, now):
        calls.append(now)
        if len(calls) == 2:  # 시작 정리 뒤 첫 주기 호출
            raise sqlite3.OperationalError("database is locked")
        return real(self, now)

    monkeypatch.setattr(SqliteRetentionStore, "cleanup", cleanup)
    with caplog.at_level(logging.WARNING), running(w.runtime):
        task = next(t for t in w.runtime.tasks if t.name == "retention")
        assert task.interval_sec == w.cfg.settings.retention_cleanup_interval_sec
        wait_until(lambda: len(calls) >= 3, timeout=5)
    assert "retention" in caplog.text  # 경고를 남기고 다음 주기에 다시 불렀다


def test_FR22_runtime_periodic_task_keeps_schedule_after_failure():
    calls = []

    def fn():
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("store down")

    task = PeriodicTask("unit", 0.02, fn)
    task.start()
    wait_until(lambda: len(calls) >= 3)
    task.signal_stop()
    assert task.join(1)


# ── 근접 복원, 반납과 이탈 ─────────────────────────────────────────


def test_FR16_runtime_restores_recent_observations(world):
    restored = []

    class Spy(HmmProximityEstimator):
        def restore(self, frames, nodes):
            frames = list(frames)
            restored.append(frames)
            super().restore(frames, nodes)

    w = world(proximity_factory=lambda models, s: Spy(models, s))
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(w.db))
    for seq, (at, minor) in enumerate([(now - timedelta(seconds=2), 2), (now - timedelta(hours=1), 2)], 1):
        conn.execute(
            "UPDATE node SET session_id = 's-old' WHERE node_id = 'locker-1'")
        conn.execute(
            "INSERT INTO rssi_observation (obs_id, node_id, beacon_id, rssi, frame_ts, received_at, sent_at, "
            "session_id, seq) VALUES (?, 'locker-1', 'b-visitor-a', -55, ?, ?, ?, 's-old', ?)",
            (f"o{seq}", at.isoformat(), at.isoformat(), at.isoformat(), seq))
    conn.commit()
    conn.close()
    with running(w.runtime):
        # 최근 구간의 관측만 실제 HMM 의 기존 복원 접점으로 넣는다.
        assert [len(frames) for frames in restored] == [1]
        assert restored[0][0].beacon == BeaconKey(UUID, 1, 2)


def test_FR20_runtime_release_and_leave_off(world):
    w = world(settings={"leave_absent_sec": 1.0, "leave_grace_sec": 0.0})
    with running(w.runtime):
        w.ready("locker-1", "light-1")
        rented = w.use(VISITOR_A, "locker-1", "rent")
        assert rented.verdict is Verdict.OK
        released = w.runtime.core.release_use(VISITOR_A, rented.use_id)
        # 반납은 반납 검사 없이 논리적 종료를 저장한다 (2026-10-26 결정).
        assert released.verdict is Verdict.OK
        assert query(w.db, "SELECT state FROM use_session WHERE use_id = ?",
                     rented.use_id)[0]["state"] == "CLOSED"
        assert w.broker.commands("safety_check") == []
        status = w.runtime.core.get_use_status(VISITOR_A, rented.use_id)
        assert status["closed_at"] is not None

        light = w.use(VISITOR_B, "light-1", "light-on", Action.ON)
        assert light.verdict is Verdict.OK
        # 유지 대상자의 관측이 끊기면 주기 이탈 검사가 서버 내부 꺼짐을 보낸다.
        wait_until(lambda: [c["action"] for c in w.broker.commands() if c["node_id"] == "light-1"]
                   == ["on", "off"], timeout=8)
        wait_until(lambda: [c["status"] for c in w.commands("light-1")] == ["succeeded", "succeeded"])


def test_FR20_runtime_shared_result_handover_does_not_block_reports(world, caplog):
    # 공용형 결과를 인계할 때 되살린 실행 문맥이 판정의 문맥과 같아야 한다. 다르면 인계가 거부되고
    # 같은 기기의 뒤 보고가 줄에 막힌다.
    w = world()
    with caplog.at_level(logging.WARNING), running(w.runtime):
        w.ready("locker-1", "light-1")
        light = w.use(VISITOR_B, "light-1", "light-on", Action.ON)
        assert light.verdict is Verdict.OK
        wait_until(lambda: [c["status"] for c in w.commands("light-1")] == ["succeeded"])
        seen = query(w.db, "SELECT last_seen_at FROM node WHERE node_id = 'light-1'")[0]["last_seen_at"]
        time.sleep(0.01)
        w.broker.status("light-1")  # 결과 뒤의 보고도 인계된다
        wait_until(lambda: query(w.db, "SELECT last_seen_at FROM node WHERE node_id = 'light-1'")[0][
            "last_seen_at"] != seen)
    assert "CORE handover failed" not in caplog.text


# ── 종료 ────────────────────────────────────────────────────────────


def test_FR07_runtime_shutdown_timeout_keeps_unknown(world):
    w = world(settings={"exec_timeout_sec": 3.0}, runtime={"shutdown_timeout_sec": 0.3})
    with running(w.runtime, ignore_shutdown_error=True):
        w.ready("locker-1")
        w.broker.drop_commands = {"locker-1"}  # 결과가 오지 않는다
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("d", w.use(VISITOR_A)))
        t.start()
        wait_until(lambda: [c["status"] for c in w.commands("locker-1")] == ["in_progress"])
        with pytest.raises(ShutdownError, match="진행 중인 요청"):
            w.runtime.stop()
        t.join(10)
    # 불명을 성공이나 미전송으로 바꾸지 않는다
    assert out["d"].verdict is not Verdict.OK
    assert [c["status"] for c in w.commands("locker-1")] in (["in_progress"], ["unknown"])

    # 같은 DB 로 다시 시작하면 미완료 기록을 복원 대상으로 다시 확인한다.
    broker = SimBroker(w.tmp / "again", ("locker-1",))
    again = Runtime(w.cfg, client_factory=broker.factory, proximity_factory=lambda models, s: w.proximity)
    try:
        with running(again):
            assert [c["status"] for c in w.commands("locker-1")] == ["unknown"]
            time.sleep(0.2)
            assert not again.services.restore.can_execute("locker-1")
    finally:
        broker.close()


# ── MCP ────────────────────────────────────────────────────────────


def test_FR21_runtime_mcp_ownership(world):
    w = world()
    app = create_app(w.runtime)
    mcp = McpCaller(app)

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "locker-1")
            status, rented = await mcp.call("request_use", {
                "target_spec": "locker-1", "action": "open", "request_id": "m1"}, token=TOKENS[VISITOR_A])
            assert status == 200 and rented["verdict"] == "OK" and rented["use_id"]
            use_id = rented["use_id"]
            _, mine = await mcp.call("get_use_status", {"use_id": use_id}, token=TOKENS[VISITOR_A])
            _, other = await mcp.call("get_use_status", {"use_id": use_id}, token=TOKENS[VISITOR_B])
            _, release = await mcp.call("release_use", {"use_id": use_id}, token=TOKENS[VISITOR_B])
            return rented, mine, other, release

    rented, mine, other, release = asyncio.run(scenario())
    assert mine["state"] == "ACTIVE"
    assert other["reason"] == ReasonCode.USE_NOT_FOUND.value and other["state"] is None
    assert release["verdict"] != "OK"
    assert query(w.db, "SELECT state FROM use_session")[0]["state"] == "ACTIVE"  # 타인은 반납하지 못했다


def test_FR08_runtime_mcp_real_app_tools(world):
    w = world()
    app = create_app(w.runtime)
    mcp = McpCaller(app)

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.to_thread(w.ready, "locker-1", "locker-2")
            denied = await mcp.call("find_available_devices", {}, token="wrong")
            missing = await mcp.call("find_available_devices", {}, token=None)
            token = TOKENS[VISITOR_A]
            out = [
                await mcp.call("find_available_devices", {}, token=token),
                await mcp.call("get_device_state", {"node_id": "locker-2"}, token=token),
                await mcp.call("request_access", {"node_id": "locker-2", "action": "open",
                                                  "request_id": "a1"}, token=token),
                await mcp.call("request_use", {"target_spec": "locker-1", "action": "open",
                                               "request_id": "u1"}, token=token),
            ]
            use_id = out[-1][1]["use_id"]
            out.append(await mcp.call("get_use_status", {"use_id": use_id}, token=token))
            out.append(await mcp.call("release_use", {"use_id": use_id}, token=token))
            return denied, missing, out

    denied, missing, out = asyncio.run(scenario())
    assert denied[0] == 401 and missing[0] == 401
    assert all(status == 200 for status, _ in out)
    devices, state, access, use, status, release = (body for _, body in out)
    assert "locker-1" in {d["node_id"] for d in devices["devices"]}
    assert state["node_id"] == "locker-2" and access["use_id"] is None
    assert use["verdict"] == "OK" and status["state"] == "ACTIVE"
    assert release["verdict"] in ("OK", "BUSY")
    text = repr(out)
    for secret in (VISITOR_A, OWNER, "place-a", TOKENS[VISITOR_A], "p_max"):
        assert secret not in text  # 사용자 ID·점유 주체·토큰을 응답에 넣지 않는다 (FR-11, NFR-08)
