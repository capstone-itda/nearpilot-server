"""`serve --collect-only` 의 서버 동작 (FR-05, FR-06, specs/core-ops-cli/spec.md 5절·6절 "serve").

실제 SQLite·runtime 조립·IOT 보고 처리 위에서 시뮬레이터를 fake Paho 로 연결한다 (`_runtime.py`).
수집 전용 Runtime 은 근접 모델 없이 시작해서 RSSI 와 기기 보고를 저장하고, `create_app` 은 MCP 를 열지 않는다.
`_runtime.make_config` 는 `require_models` 를 넘기지 않으므로 모델 표 없는 설정은 이 파일에서 만든다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from _runtime import (
    NODES, TOKENS, VISITOR_A, McpCaller, SimBroker, make_config, query, running, seed, wait_until,
)
from nearpilot.core.proximity.estimator import HmmProximityEstimator
from nearpilot.runtime import Runtime, create_app
from nearpilot.runtime.config import ServerConfig, parse_config

USE_LOCKER = ("request_use", {"target_spec": "locker-1", "action": "open", "request_id": "m1"})


def _config_without_models(tmp: Path) -> ServerConfig:
    """`make_config` 와 같은 설정에서 `[proximity.models]` 를 뺀다. model_root 에는 파일이 없다."""
    secret = tmp / ".env.mqtt-password"
    secret.write_text("test-only-password\n", encoding="utf-8")
    raw = {
        "settings": {"version": "t1"},
        "runtime": {"execution_tick_sec": 0.05, "release_tick_sec": 0.05, "restore_tick_sec": 0.05,
                    "recovery_tick_sec": 0.05, "shared_light_tick_sec": 0.1, "shutdown_timeout_sec": 5.0},
        "http": {"host": "127.0.0.1", "port": 8000},
        "database": {"path": "np.db", "audit_jsonl": "audit.jsonl"},
        "proximity": {"model_root": "no-models"},
        "mqtt": {"host": "127.0.0.1", "port": 1883, "client_id": "test-server",
                 "username": "nearpilot-server", "password_file": secret.name, "keepalive_sec": 30,
                 "connect_timeout_sec": 1.0, "start_timeout_sec": 2.0, "stop_timeout_sec": 2.0,
                 "reconnect_min_sec": 1, "reconnect_max_sec": 2},
        "delivery": {"clock_skew_sec": {node_id: 0.0 for node_id in NODES}, "pending_limit": 1000,
                     "retry_interval_sec": 0.05, "stop_timeout_sec": 2.0},
    }
    return parse_config(raw, base=tmp, require_models=False)


def _rssi_count(cfg: ServerConfig) -> int:
    return query(cfg.database.path, "SELECT count(*) c FROM rssi_observation")[0]["c"]


def _frames_sent(broker: SimBroker) -> int:
    """`broker.rssi()` 한 번에 저장해야 하는 관측 수. 프레임마다 비콘 하나가 한 행이다."""
    return sum(len(node.scenario.beacons) for node in broker.nodes.values())


def _wait_restored(runtime: Runtime, broker: SimBroker) -> None:
    restore = runtime.services.restore
    wait_until(lambda: all(restore.can_execute(node_id) for node_id in broker.nodes))


def test_FR05_collect_only_starts_without_models(tmp_path, caplog):
    cfg = _config_without_models(tmp_path)
    assert dict(cfg.proximity.versions) == {} and not cfg.proximity.model_root.exists()
    seed(cfg.database.path)  # 계정, 비콘, 기기
    broker = SimBroker(tmp_path)
    built = []

    def proximity_factory(models, settings):
        built.append(dict(models))
        return HmmProximityEstimator(models, settings)

    runtime = Runtime(cfg, client_factory=broker.factory, proximity_factory=proximity_factory,
                      collect_only=True)
    assert runtime.collect_only
    try:
        with caplog.at_level(logging.INFO), running(runtime):  # 모델 파일이 없어도 시작한다
            _wait_restored(runtime, broker)
            broker.status()
            before, sent = _rssi_count(cfg), _frames_sent(broker)
            assert sent > 0
            broker.rssi()
            wait_until(lambda: _rssi_count(cfg) >= before + sent)
            # 보낸 프레임 수만큼 저장했다
            assert _rssi_count(cfg) == before + sent
            # 기기 상태 보고도 평소처럼 저장한다
            seen = query(cfg.database.path,
                         "SELECT node_id FROM node WHERE session_id IS NOT NULL AND last_seen_at IS NOT NULL")
            assert {row["node_id"] for row in seen} == set(broker.nodes)
    finally:
        broker.close()
    assert built == [{}]  # 빈 모델로 근접 추정기를 만들었다
    assert "수집 전용 모드: 근접 모델을 읽지 않는다. MCP 를 열지 않는다" in caplog.text


def test_FR05_collect_only_ignores_configured_models(tmp_path):
    cfg = make_config(tmp_path, models={"place-a": "missing-version"})  # 일반 serve 라면 시작하지 못한다
    seed(cfg.database.path)
    broker = SimBroker(tmp_path)
    built = []

    def proximity_factory(models, settings):
        built.append(dict(models))
        return HmmProximityEstimator(models, settings)

    runtime = Runtime(cfg, client_factory=broker.factory, proximity_factory=proximity_factory,
                      collect_only=True)
    try:
        with running(runtime):
            _wait_restored(runtime, broker)
    finally:
        broker.close()
    assert built == [{}]  # 설정의 [proximity.models] 를 읽지 않는다


async def _request_use_in_lifespan(app, broker: SimBroker, cfg: ServerConfig):
    """서버가 도는 동안 기기 보고가 계속 들어오게 하고, 방문자의 사용 요청을 `/mcp` 로 보낸다."""
    async with app.router.lifespan_context(app):
        await asyncio.to_thread(_wait_restored, app.state.runtime, broker)
        before, sent = _rssi_count(cfg), _frames_sent(broker)
        broker.rssi()
        result = await McpCaller(app).call(*USE_LOCKER, token=TOKENS[VISITOR_A])
        await asyncio.to_thread(wait_until, lambda: _rssi_count(cfg) >= before + sent)
        return result


def _app(tmp: Path, *, collect_only: bool):
    tmp.mkdir()
    cfg = _config_without_models(tmp) if collect_only else make_config(tmp)
    seed(cfg.database.path)
    broker = SimBroker(tmp)
    app = create_app(Runtime(cfg, client_factory=broker.factory, collect_only=collect_only))
    return cfg, broker, app


def test_FR06_collect_only_has_no_mcp(tmp_path):
    # 대조군: 모델이 있는 일반 모드에서는 같은 요청이 404 가 아니다. 요청이 CORE 까지 닿는다.
    cfg, broker, app = _app(tmp_path / "normal", collect_only=False)
    try:
        status, body = asyncio.run(_request_use_in_lifespan(app, broker, cfg))
        assert status == 200 and "verdict" in body
        assert query(cfg.database.path, "SELECT count(*) c FROM request")[0]["c"] == 1
    finally:
        broker.close()

    cfg, broker, app = _app(tmp_path / "collect", collect_only=True)
    try:
        status, _ = asyncio.run(_request_use_in_lifespan(app, broker, cfg))
        assert status == 404  # 토큰이 맞아도 /mcp 가 없다
        # 같은 시간 동안 서버는 보고를 저장했지만 기기에 실행 명령도 반납 검사도 보내지 않았다
        assert broker.commands() == [] and broker.commands("safety_check") == []
        # 요청이 CORE 에 닿지 않았다
        assert query(cfg.database.path, "SELECT count(*) c FROM request")[0]["c"] == 0
        assert query(cfg.database.path, "SELECT count(*) c FROM node_command")[0]["c"] == 0
    finally:
        broker.close()
