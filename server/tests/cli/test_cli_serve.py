"""`nearpilot serve` (FR-16, specs/core-ops-cli/spec.md 5절·6절 "serve").

일반 serve만 모델 표가 필요하고 `--collect-only`는 모델 표 없이 시작한다.
서버 실행(`run_server`)과 uvicorn은 가짜로 대신한다. 수집 전용 서버의 실제 동작은 `tests/integration/test_cli_collect_only.py`가 확인한다.
"""

from __future__ import annotations

import pytest

from nearpilot.cli import EXIT_INVALID, EXIT_OK, serve
from nearpilot.cli.common import load_cli_config
from nearpilot.runtime import ConfigError, load_config

MODEL_ROOT = "../var/models"


@pytest.mark.parametrize("proximity", [
    {"model_root": MODEL_ROOT},  # 모델 표가 없다
    {"model_root": MODEL_ROOT, "models": {}},  # 빈 표다
], ids=["no-table", "empty-table"])
def test_FR16_serve_requires_models_without_collect_only(
        server_dir, run_cli, run_server_calls, write_config, proximity):
    path = write_config(server_dir, name="no-models.toml", proximity=proximity)

    code, _, err = run_cli(["--config", str(path), "serve"], cwd=server_dir)
    assert code == EXIT_INVALID and err.startswith("설정 오류: ") and "proximity.models" in err
    assert run_server_calls == []  # 서버를 시작하지 않는다

    code, _, _ = run_cli(["--config", str(path), "serve", "--collect-only"], cwd=server_dir)
    assert code == EXIT_OK
    [(config, collect_only)] = run_server_calls
    assert collect_only is True and config.proximity.versions == {}

    # load_config의 기본값은 그대로 모델 표를 요구한다
    with pytest.raises(ConfigError, match=r"proximity\.models"):
        load_config(path)
    assert load_config(path, require_models=False).proximity.versions == {}
    assert not (server_dir / "var").exists()  # 설정만 읽고 DB를 열지 않는다


def test_FR16_serve_collect_only_follows_option_not_models(server_dir, run_cli, run_server_calls, write_config):
    path = write_config(server_dir, name="models.toml",
                        proximity={"model_root": MODEL_ROOT, "models": {"place-a": "v1"}})

    for argv in (["serve", "--config", str(path)], ["serve", "--collect-only", "--config", str(path)]):
        assert run_cli(argv, cwd=server_dir)[0] == EXIT_OK
    # 모델 표가 있어도 수집 전용은 옵션으로만 켠다. 설정은 그대로 읽는다 (모델은 Runtime이 읽지 않는다).
    assert [collect_only for _, collect_only in run_server_calls] == [False, True]
    assert all(dict(config.proximity.versions) == {"place-a": "v1"} for config, _ in run_server_calls)


@pytest.mark.parametrize("proximity, key", [
    ({"model_root": MODEL_ROOT, "models": {"place-a": 3}}, "proximity.models.place-a"),
    ({"model_root": MODEL_ROOT, "models": "v1"}, "proximity.models"),
    ({"models": {}}, "proximity.model_root"),  # model_root는 항상 필수다
    ({"model_root": MODEL_ROOT, "extra": 1}, "proximity.extra"),
], ids=["model-version-type", "models-not-table", "no-model-root", "unknown-key"])
def test_FR16_serve_collect_only_still_checks_proximity_format(
        server_dir, run_cli, run_server_calls, write_config, proximity, key):
    path = write_config(server_dir, name="bad.toml", proximity=proximity)

    code, _, err = run_cli(["serve", "--collect-only", "--config", str(path)], cwd=server_dir)
    assert code == EXIT_INVALID and err.startswith("설정 오류: ") and key in err
    assert run_server_calls == []


def test_FR16_serve_collect_only_is_not_a_config_key(server_dir, run_cli, run_server_calls, write_config):
    # 운영 서버가 실수로 수집 모드로 뜨지 않게 설정 파일에는 두지 않는다.
    path = write_config(server_dir, name="flag.toml", runtime={"collect_only": True})

    code, _, err = run_cli(["serve", "--config", str(path)], cwd=server_dir)
    assert code == EXIT_INVALID and "runtime.collect_only" in err
    assert run_server_calls == []


@pytest.mark.parametrize("collect_only", [False, True])
def test_FR16_serve_runs_one_worker_on_configured_address(server_dir, write_config, monkeypatch, collect_only):
    path = write_config(server_dir, name="address.toml", http={"host": "192.0.2.7", "port": 8765},
                        runtime={"shutdown_timeout_sec": 12.7})
    config = load_cli_config(str(path))
    calls = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert serve.run_server(config, collect_only=collect_only) == EXIT_OK

    [(app, kwargs)] = calls
    # python -m nearpilot과 같은 인자다. 설정의 주소를 쓰고 worker는 하나다.
    assert kwargs == {"host": "192.0.2.7", "port": 8765, "workers": 1, "lifespan": "on",
                      "log_config": None, "timeout_graceful_shutdown": 12}
    assert app.state.runtime.config is config and app.state.runtime.collect_only is collect_only
    assert not (server_dir / "var").exists()  # 앱을 만들 뿐 DB를 열지 않는다
