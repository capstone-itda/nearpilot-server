"""서버 조립 시나리오의 도구: 실제 SQLite, 실제 runtime 조립, fake Paho 위의 기기 시뮬레이터.

fake Paho 는 브로커 대신 `tools.node_sim` 의 SimNode 에 발행을 넘긴다. 보고는 Paho 처럼
별도 스레드에서 `on_message` 로 들어온다. 판정·상태 관리·IOT·DB 코드는 모두 실제 구현이다.
"""

from __future__ import annotations

import asyncio
import json
import queue
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SERVER = Path(__file__).resolve().parents[2]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from tools.node_sim.node import Beacon, Scenario, SimNode  # noqa: E402
from tools.node_sim.store import CommandStore  # noqa: E402

from nearpilot.db.schema import connect  # noqa: E402
from nearpilot.runtime import Runtime, ShutdownError  # noqa: E402
from nearpilot.runtime.config import ServerConfig, parse_config  # noqa: E402
from nearpilot.shared.models import ProximityResult  # noqa: E402
from nearpilot.shared.tokens import hash_token  # noqa: E402

MODEL_ROOT = SERVER / "tests" / "core" / "proximity" / "fixtures" / "models"
GRANTED = [1, 0, 1, 1]
OWNER, VISITOR_A, VISITOR_B = "owner", "visitor-a", "visitor-b"
TOKENS = {OWNER: "tok-owner", VISITOR_A: "tok-a", VISITOR_B: "tok-b"}
UUID = "11111111-2222-3333-4444-555555555555"
MINOR = {OWNER: 1, VISITOR_A: 2, VISITOR_B: 3}
# fixture 모델(place-a)의 기기와 같다
NODES = {
    "locker-1": ("locker", "rental"),
    "locker-2": ("locker", "rental"),
    "locker-3": ("locker", "rental"),
    "light-1": ("light", "shared"),
    "light-2": ("light", "shared"),
    "speaker-1": ("speaker", "shared"),
}


def wait_until(predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("조건을 기다리다 시간이 지났다")
        time.sleep(interval)


# ── 설정과 DB ──────────────────────────────────────────────────────


def make_config(tmp: Path, *, settings: Mapping[str, Any] | None = None,
                runtime: Mapping[str, Any] | None = None, delivery: Mapping[str, Any] | None = None,
                database: Mapping[str, Any] | None = None, models: Mapping[str, str] | None = None,
                mqtt: Mapping[str, Any] | None = None) -> ServerConfig:
    secret = tmp / ".env.mqtt-password"
    secret.write_text("test-only-password\n", encoding="utf-8")
    raw = {
        "settings": {"version": "t1", **(settings or {})},
        "runtime": {"execution_tick_sec": 0.05, "release_tick_sec": 0.05, "restore_tick_sec": 0.05,
                    "recovery_tick_sec": 0.05, "shared_light_tick_sec": 0.1,
                    "shutdown_timeout_sec": 5.0, **(runtime or {})},
        "http": {"host": "127.0.0.1", "port": 8000},
        "database": {"path": "np.db", "audit_jsonl": "audit.jsonl", **(database or {})},
        "proximity": {"model_root": str(MODEL_ROOT), "models": dict(models or {"place-a": "syn-a-1"})},
        "mqtt": {"host": "127.0.0.1", "port": 1883, "client_id": "test-server",
                 "username": "nearpilot-server", "password_file": secret.name, "keepalive_sec": 30,
                 "connect_timeout_sec": 1.0, "start_timeout_sec": 2.0, "stop_timeout_sec": 2.0,
                 "reconnect_min_sec": 1, "reconnect_max_sec": 2, **(mqtt or {})},
        "delivery": {"clock_skew_sec": {node_id: 0.0 for node_id in NODES}, "pending_limit": 1000,
                     "retry_interval_sec": 0.05, "stop_timeout_sec": 2.0, **(delivery or {})},
    }
    return parse_config(raw, base=tmp)


def seed(db_path: Path, *, modes: Mapping[str, str] | None = None,
         nodes: Mapping[str, tuple[str, str]] = NODES) -> None:
    """계정·비콘·장소·기기·정책을 넣는다. 기기의 연결과 온라인은 시뮬레이터 보고가 정한다."""
    conn = connect(str(db_path))
    try:
        for user_id, role in ((OWNER, "owner"), (VISITOR_A, "visitor"), (VISITOR_B, "visitor")):
            conn.execute("INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
                         (user_id, user_id, hash_token(TOKENS[user_id]), role))
            conn.execute("INSERT INTO beacon (beacon_id, user_id, uuid, major, minor, is_active) "
                         "VALUES (?, ?, ?, 1, ?, 1)", (f"b-{user_id}", user_id, UUID, MINOR[user_id]))
        conn.execute("INSERT INTO place (place_id, owner_user_id, name) VALUES ('place-a', ?, 'lab')",
                     (OWNER,))
        for node_id, (capability, occupancy) in nodes.items():
            policy = "{}"
            conn.execute(
                "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
                "exclusive_group, release_policy) VALUES (?, 'place-a', ?, ?, 'trusted', 'AVAILABLE', NULL, ?)",
                (node_id, capability, occupancy, policy))
            conn.execute("INSERT INTO access_policy (policy_id, node_id, current_version) VALUES (?, ?, 1)",
                         (f"pol-{node_id}", node_id))
            conn.execute(
                "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
                "approval_ttl_sec, approval_mode, max_uses) VALUES (?, 1, 'public', ?, ?, 3600, 'one_time', 1)",
                (f"pol-{node_id}", (modes or {}).get(node_id, "open"), OWNER))
    finally:
        conn.close()


def query(db_path: Path, sql: str, *params: Any) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class FakeProximity:
    """사후확률을 고정한다. 값은 기기별 표 하나 또는 계정별 기기 표다. 복원 호출을 기록한다."""

    def __init__(self, posteriors: Mapping[str, Any]) -> None:
        self.posteriors = dict(posteriors)
        self.restored: list[Any] = []

    def observe(self, frame, *, node) -> None:
        pass

    def restore(self, frames, nodes) -> None:
        self.restored.extend(frames)

    def posterior(self, beacons, candidates, at, *, place_nodes) -> ProximityResult:
        table = self.posteriors
        if table and all(isinstance(v, Mapping) for v in table.values()):
            user = next(u for u, minor in MINOR.items() if minor == beacons[0].minor)
            table = table.get(user, {})
        wanted = {n.node_id for n in candidates}
        return ProximityResult({k: v for k, v in table.items() if k in wanted}, at, at,
                               "syn-a-1", "none-1")


# ── 기기 시뮬레이터와 fake Paho ─────────────────────────────────────


class SimBroker:
    """fake Paho 클라이언트를 만들고 발행을 시뮬레이터에 넘긴다."""

    def __init__(self, tmp: Path, node_ids=("locker-1", "locker-2", "light-1"), *,
                 beacons: Mapping[str, tuple[str, ...]] | None = None) -> None:
        users = beacons or {node_id: (VISITOR_A, VISITOR_B) for node_id in node_ids}
        self.nodes: dict[str, SimNode] = {}
        for node_id in node_ids:
            capability, occupancy = NODES[node_id]
            scenario = Scenario(actuation_sec=0.0, beacons=tuple(
                Beacon(UUID, 1, MINOR[user], -55.0) for user in users.get(node_id, ())))
            store = CommandStore(tmp / "sim" / f"{node_id}.sqlite3")
            self.nodes[node_id] = SimNode(node_id, capability, occupancy, store, scenario)
        self.granted = list(GRANTED)
        self.silent: set[str] = set()  # 상태 보고를 보내지 않는 기기
        self.drop_commands: set[str] = set()  # 명령에 응답하지 않는 기기
        self.before_ack: Callable[[FakePaho], None] | None = None
        self.clients: list[FakePaho] = []
        self.published: list[tuple[str, dict]] = []

    def factory(self, **kwargs) -> FakePaho:
        client = FakePaho(self, **kwargs)
        self.clients.append(client)
        return client

    @property
    def client(self) -> FakePaho:
        return self.clients[-1]

    def commands(self, kind: str = "cmd") -> list[dict]:
        return [body for topic, body in self.published if topic.endswith(f"/{kind}")]

    def report(self, node_id: str, messages) -> None:
        self.client.deliver(node_id, messages)

    def status(self, *node_ids: str) -> None:
        for node_id in node_ids or tuple(self.nodes):
            self.report(node_id, [self.nodes[node_id].status()])

    def rssi(self, *node_ids: str) -> None:
        for node_id in node_ids or tuple(self.nodes):
            self.report(node_id, [self.nodes[node_id].rssi()])

    def close(self) -> None:
        for node in self.nodes.values():
            node.store.close()


class FakePaho:
    def __init__(self, broker: SimBroker, **kwargs) -> None:
        self.broker = broker
        self.options = kwargs
        self.mid = 0
        self.connected = False
        self.stopped = False
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="fake-paho", daemon=True)

    # Paho API
    def username_pw_set(self, username, password): self.credentials = (username, password)
    def reconnect_delay_set(self, minimum, maximum): self.backoff = (minimum, maximum)
    def connect_async(self, host, port, keepalive): self.address = (host, port, keepalive)

    def loop_start(self):
        self._thread.start()
        self._queue.put(self._connect)
        return 0

    def subscribe(self, topics):
        self.mid += 1
        self.topics = topics
        return 0, self.mid

    def publish(self, topic, payload, qos, retain):
        body = json.loads(payload)
        self.broker.published.append((topic, body))
        node_id, kind = topic.split("/")[2:4]
        self._queue.put(lambda: self._handle(node_id, kind, payload))
        return SimpleNamespace(rc=0, mid=len(self.broker.published))

    def disconnect(self):
        self.connected = False
        self.on_disconnect(self, None, None, 0, None)
        return 0

    def loop_stop(self):
        self._queue.put(None)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(2)
        self.stopped = True
        return 0

    # 시뮬레이터 연결
    def deliver(self, node_id: str, messages) -> None:
        self._queue.put(lambda: self._deliver(node_id, messages))

    def _deliver(self, node_id: str, messages) -> None:
        for kind, body in messages:
            self.on_message(self, None, SimpleNamespace(
                topic=f"nearpilot/node/{node_id}/{kind}", payload=json.dumps(body).encode(),
                qos=1, retain=kind == "announce", dup=False))

    def _connect(self) -> None:
        self.connected = True
        self.on_connect(self, None, None, 0, None)
        for node_id, node in self.broker.nodes.items():
            messages = node.connect()
            if node_id not in self.broker.silent:
                messages.append(node.status())
            self._deliver(node_id, messages)
        if self.broker.before_ack is not None:
            self.broker.before_ack(self)
        self.on_subscribe(self, None, self.mid, list(self.broker.granted), None)

    def _handle(self, node_id: str, kind: str, payload: bytes) -> None:
        node = self.broker.nodes.get(node_id)
        if node is None or node_id in self.broker.drop_commands:
            return
        if kind == "cmd":
            messages, started = node.handle_command(payload)
            if started:
                messages += node.finish()
            self._deliver(node_id, messages + [node.status()])
        else:
            self._deliver(node_id, node.handle_check(payload))

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job()
            except Exception:  # noqa: BLE001 - Paho 처럼 콜백 오류로 루프를 멈추지 않는다
                import logging
                logging.getLogger(__name__).exception("fake paho job failed")


# ── 시작과 MCP 호출 ────────────────────────────────────────────────


@contextmanager
def running(runtime: Runtime, *, ignore_shutdown_error: bool = False):
    runtime.start()
    try:
        yield runtime
    finally:
        try:
            runtime.stop()
        except ShutdownError:
            if not ignore_shutdown_error:
                raise


async def asgi_post(app, path: str, headers: Mapping[str, str], body: bytes):
    """ASGI 앱에 HTTP POST 를 직접 보낸다. CI 의 dev 의존성에는 httpx 가 없다."""
    host = {"host": "127.0.0.1:8000", "content-length": str(len(body)), **headers}
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in host.items()],
    }
    sent, start, chunks = False, {}, []

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(status=message["status"])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return start["status"], b"".join(chunks).decode()


class McpCaller:
    """FastAPI 앱의 수명주기 안에서 MCP 도구를 부른다."""

    HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def __init__(self, app) -> None:
        self.app = app

    async def call(self, tool: str, args: Mapping[str, Any], *, token: str | None, id_: int = 1):
        headers = dict(self.HEADERS)
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                "params": {"name": tool, "arguments": dict(args)}}
        status, text = await asgi_post(self.app, "/mcp", headers, json.dumps(body).encode())
        if status != 200:
            return status, text
        return status, json.loads(json.loads(text)["result"]["content"][0]["text"])
