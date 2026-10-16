"""FR-22·NFR-08 서버 TOML 설정의 검증 (specs/core-server-assembly/spec.md 5절)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from _runtime import SERVER, SimBroker, make_config
from nearpilot.core.proximity.model import ModelMismatchError
from nearpilot.runtime import ConfigError, Runtime, load_config
from nearpilot.runtime.config import parse_config


def _raw(tmp: Path) -> dict:
    cfg = make_config(tmp)  # 비밀 파일을 만든다
    return {
        "settings": {"version": "t1"},
        "http": {"host": "127.0.0.1", "port": 8000},
        "database": {"path": "np.db", "audit_jsonl": "audit.jsonl"},
        "proximity": {"model_root": str(cfg.proximity.model_root), "models": {"place-a": "syn-a-1"}},
        "mqtt": {"host": "127.0.0.1", "port": 1883, "client_id": "c", "username": "nearpilot-server",
                 "password_file": ".env.mqtt-password", "keepalive_sec": 30, "connect_timeout_sec": 1.0,
                 "start_timeout_sec": 1.0, "stop_timeout_sec": 1.0, "reconnect_min_sec": 1,
                 "reconnect_max_sec": 2},
        "delivery": {"clock_skew_sec": {}, "pending_limit": 10, "retry_interval_sec": 0.1,
                     "stop_timeout_sec": 1.0},
    }


def test_FR22_runtime_example_config_loads():
    cfg = load_config(SERVER / "config" / "server.example.toml")
    base = (SERVER / "config").resolve()
    assert cfg.database.path == (base / "../var/nearpilot.db").resolve()  # 설정 파일 기준 상대 경로
    assert cfg.mqtt.password_file == base / ".env.mqtt-password"
    assert cfg.settings.threshold_speaker is None  # 미설정 임계값은 비워 둔다
    assert cfg.runtime.shutdown_timeout_sec == 30.0 and cfg.runtime.sqlite_busy_timeout_ms == 5000


@pytest.mark.parametrize("change, key", [
    (lambda r: r["settings"].update(threshold_locker=1.5), "threshold_locker"),
    (lambda r: r["settings"].update(unknown_value=1), "settings.unknown_value"),
    (lambda r: r["settings"].pop("version"), "settings.version"),
    (lambda r: r.update(extra={}), "extra"),
    (lambda r: r["mqtt"].pop("stop_timeout_sec"), "mqtt.stop_timeout_sec"),  # 기본값이 없다
    (lambda r: r["delivery"].pop("pending_limit"), "delivery.pending_limit"),
    (lambda r: r["proximity"].update(models={}), "proximity.models"),
    (lambda r: r.setdefault("runtime", {}).update(shutdown_timeout_sec=0), "runtime.shutdown_timeout_sec"),
    (lambda r: r["settings"].update(online_ttl_sec=0.5, status_report_sec=1.0), "online_ttl_sec"),
])
def test_FR22_runtime_invalid_config_stops_startup(tmp_path, change, key):
    raw = _raw(tmp_path)
    change(raw)
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        parse_config(raw, base=tmp_path)


def test_FR22_runtime_invalid_model_stops_startup(tmp_path):
    cfg = make_config(tmp_path, models={"place-a": "missing-version"})
    broker = SimBroker(tmp_path)
    runtime = Runtime(cfg, client_factory=broker.factory)
    with pytest.raises(ModelMismatchError):
        runtime.start()
    # 모델을 먼저 검증한다. MQTT 와 DB 를 열지 않고 요청을 받지 않는다.
    assert broker.clients == [] and runtime.db.open_connections == 0
    assert not runtime.gate.accepting and not cfg.database.path.exists()
    broker.close()


def test_NFR08_runtime_config_keeps_secret_out(tmp_path, caplog):
    raw = _raw(tmp_path)
    raw["mqtt"]["username"] = "bad\x00user-secret"
    with caplog.at_level(logging.DEBUG), pytest.raises(ConfigError) as error:
        parse_config(raw, base=tmp_path)
    assert "secret" not in str(error.value)
    cfg = make_config(tmp_path)
    # 비밀은 파일 경로로만 받는다. 설정 객체의 표현에 경로도 원문도 없다.
    assert "test-only-password" not in repr(cfg) and ".env.mqtt-password" not in repr(cfg.mqtt)


def test_FR22_runtime_main_rejects_invalid_config(tmp_path, monkeypatch):
    from nearpilot import __main__ as entry

    bad = tmp_path / "server.toml"
    bad.write_text('[settings]\nversion = "t1"\n', encoding="utf-8")
    started = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **kw: started.append(a))
    assert entry.main(["--config", str(bad)]) == 2
    assert started == []  # 설정이 잘못되면 HTTP 서버를 시작하지 않는다
