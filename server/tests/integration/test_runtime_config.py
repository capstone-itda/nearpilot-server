"""FR-22·NFR-08 서버 TOML 설정의 검증 (specs/core-server-assembly/spec.md 5절).

선택 섹션 `[dashboard]` 의 검증은 FR-12A 다 (specs/core-dashboard-queries/spec.md 2절·5절).
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

from _runtime import SERVER, SimBroker, make_config
from nearpilot.api.ws.settings import DashboardSettings
from nearpilot.cli.common import dirs_for
from nearpilot.core.proximity.model import ModelMismatchError
from nearpilot.runtime import ConfigError, DashboardConfig, Runtime, load_config
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
    (lambda r: r["http"].update(public_hosts="mcp.example.test"), "http.public_hosts"),
    (lambda r: r["http"].update(public_hosts=["https://mcp.example.test"]), "http.public_hosts"),
    (lambda r: r["http"].update(public_hosts=[""]), "http.public_hosts"),
])
def test_FR22_runtime_invalid_config_stops_startup(tmp_path, change, key):
    raw = _raw(tmp_path)
    change(raw)
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        parse_config(raw, base=tmp_path)


def test_FR22_runtime_http_public_hosts_is_optional(tmp_path):
    raw = _raw(tmp_path)
    assert parse_config(raw, base=tmp_path).http.public_hosts == ()
    raw["http"]["public_hosts"] = ["mcp.example.test"]
    assert parse_config(raw, base=tmp_path).http.public_hosts == ("mcp.example.test",)


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


# ── [dashboard] (선택 섹션) ─────────────────────────────────────────

DASHBOARD = {"queue_max_events": 1000, "batch_max_events": 50, "auth_timeout_sec": 5.0, "audit_limit_max": 200,
             "weak_rssi_dbm": -80, "tail_interval_sec": 0.5}
_COUNTS = ("queue_max_events", "batch_max_events", "audit_limit_max")  # 1 이상의 정수
_POSITIVES = ("auth_timeout_sec", "tail_interval_sec")  # 0보다 큰 유한한 수
# 값마다 오류 메시지에 값이 새지 않는지 본다. 문자열과 음수는 눈에 띄는 값을 쓴다
_SECRET, _NEGATIVE = "hunter2-secret", -31337
_BAD_VALUES = (
    [(key, value) for key in _COUNTS for value in (0, _NEGATIVE, True, 1.5, _SECRET, [5])]
    + [(key, value) for key in _POSITIVES
       for value in (0, _NEGATIVE, math.inf, -math.inf, math.nan, True, _SECRET, [5])]
    + [("weak_rssi_dbm", value) for value in (math.inf, -math.inf, math.nan, True, _SECRET, [5])]
)


def _with_dashboard(tmp: Path, **change) -> dict:
    raw = _raw(tmp)
    raw["dashboard"] = {**DASHBOARD, **change}
    return raw


def test_FR12A_대시보드_섹션이_없으면_설정에_없다(tmp_path):
    assert parse_config(_raw(tmp_path), base=tmp_path).dashboard is None


def test_FR12A_대시보드_섹션이_DashboardConfig가_된다(tmp_path):
    source = tmp_path / "server.toml"
    cfg = parse_config(_with_dashboard(tmp_path), base=tmp_path, source=source)

    assert cfg.dashboard == DashboardConfig(
        api=DashboardSettings(queue_max_events=1000, batch_max_events=50, auth_timeout_sec=5.0, audit_limit_max=200),
        weak_rssi_dbm=-80, tail_interval_sec=0.5, reports_dir=tmp_path.resolve().parent / "var" / "reports")
    # runtime 은 cli 를 import 하지 않는다. 평가 리포트 폴더의 규칙이 cli 와 같은지 여기서 맞춘다
    assert cfg.dashboard.reports_dir == dirs_for(source).reports


def test_FR12A_대시보드_설정은_음수_수신_세기와_정수_간격을_받는다(tmp_path):
    cfg = parse_config(_with_dashboard(tmp_path, weak_rssi_dbm=0, tail_interval_sec=2, auth_timeout_sec=1),
                       base=tmp_path)

    assert (cfg.dashboard.weak_rssi_dbm, cfg.dashboard.tail_interval_sec) == (0, 2)
    assert cfg.dashboard.api.auth_timeout_sec == 1


def test_FR12A_예시_설정의_대시보드_섹션이_읽힌다():
    cfg = load_config(SERVER / "config" / "server.example.toml")

    assert cfg.dashboard is not None
    assert cfg.dashboard.reports_dir == SERVER.resolve() / "var" / "reports"  # 설정 폴더(server/config)의 부모 기준


@pytest.mark.parametrize("key, value", _BAD_VALUES, ids=[f"{key}={value!r}" for key, value in _BAD_VALUES])
def test_FR12A_대시보드_설정이_틀리면_ConfigError(tmp_path, key, value):
    with pytest.raises(ConfigError) as error:
        parse_config(_with_dashboard(tmp_path, **{key: value}), base=tmp_path)

    message = str(error.value)
    assert f"dashboard.{key}" in message  # 키 이름만 알린다
    assert _SECRET not in message and str(_NEGATIVE) not in message  # 값을 쓰지 않는다 (NFR-08)


@pytest.mark.parametrize("key", list(DASHBOARD))
def test_FR12A_대시보드_설정이_틀리면_ConfigError_키가_빠졌다(tmp_path, key):
    raw = _with_dashboard(tmp_path)
    del raw["dashboard"][key]

    with pytest.raises(ConfigError, match=f"dashboard\\.{key}"):
        parse_config(raw, base=tmp_path)


def test_FR12A_대시보드_설정이_틀리면_ConfigError_모르는_키(tmp_path):
    with pytest.raises(ConfigError, match=r"dashboard\.extra_key"):
        parse_config(_with_dashboard(tmp_path, extra_key=1), base=tmp_path)


def test_FR12A_대시보드_설정이_틀리면_ConfigError_빈_섹션과_표가_아닌_값(tmp_path):
    raw = _raw(tmp_path)
    raw["dashboard"] = {}  # 섹션이 있으면 키 6개가 모두 필요하다. 비어 있다고 없는 것으로 보지 않는다
    with pytest.raises(ConfigError, match=r"dashboard\.queue_max_events"):
        parse_config(raw, base=tmp_path)
    raw["dashboard"] = 5
    with pytest.raises(ConfigError, match=r"\[dashboard\]"):
        parse_config(raw, base=tmp_path)
