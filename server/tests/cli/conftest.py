"""`tests/cli/` 공통 fixture (specs/core-ops-cli/spec.md).

테스트 모듈은 이 파일을 import하지 않고 fixture로만 받는다. 이 디렉터리에 `__init__.py`를 두지 않는다.
테스트 모듈 이름은 저장소 전체에서 겹치지 않게 `test_cli_*.py`로 짓는다.
`server_dir`은 `server/` 폴더 모양의 임시 폴더다. `config/server.toml`은 유효하고 `[proximity.models]`가 없다.
`var/`는 명령이 만드는 폴더라서 fixture가 만들지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from nearpilot.cli import main, serve

# 유효한 최소 설정. 절 하나를 통째로 바꾸려면 `write_config(server_dir, proximity={...})`처럼 넘긴다.
BASE_SECTIONS: dict[str, dict[str, Any]] = {
    "settings": {"version": "t1"},
    "http": {"host": "127.0.0.1", "port": 8000},
    "database": {"path": "../var/nearpilot.db", "audit_jsonl": "../var/audit.jsonl"},
    "proximity": {"model_root": "../var/models"},
    "mqtt": {"host": "127.0.0.1", "port": 1883, "client_id": "nearpilot-server",
             "username": "nearpilot-server", "password_file": ".env.mqtt-password", "keepalive_sec": 30,
             "connect_timeout_sec": 5.0, "start_timeout_sec": 10.0, "stop_timeout_sec": 5.0,
             "reconnect_min_sec": 1, "reconnect_max_sec": 8},
    "delivery": {"pending_limit": 1000, "retry_interval_sec": 0.5, "stop_timeout_sec": 5.0,
                 "clock_skew_sec": {"locker-1": 0.0}},
}


def _toml_table(name: str, table: Mapping[str, Any]) -> list[str]:
    """표 하나를 TOML 줄로 쓴다. 값은 JSON 문자열·숫자 표기가 TOML과 같다. 표 안의 표는 `[이름.하위]`로 쓴다."""
    lines = [f"[{name}]"]
    lines += [f"{key} = {json.dumps(value)}" for key, value in table.items() if not isinstance(value, Mapping)]
    for key, value in table.items():
        if isinstance(value, Mapping):
            lines += ["", *_toml_table(f"{name}.{key}", value)]
    return lines


def _write_config(server: Path, *, name: str = "server.toml", **sections: Mapping[str, Any]) -> Path:
    config_dir = server / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env.mqtt-password").write_text("test-only-password\n", encoding="utf-8")
    tables = {**BASE_SECTIONS, **sections}
    text = "\n\n".join("\n".join(_toml_table(table_name, table)) for table_name, table in tables.items())
    path = config_dir / name
    path.write_text(text + "\n", encoding="utf-8")
    return path


@pytest.fixture
def write_config() -> Callable[..., Path]:
    """`write_config(server_dir, name="server.toml", **절)`: `config/<name>`을 쓰고 경로를 돌려준다. 넘긴 절은 기본 절을 통째로 바꾼다."""
    return _write_config


@pytest.fixture
def server_dir(tmp_path: Path) -> Path:
    server = tmp_path / "server"
    _write_config(server)
    return server


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """`run_cli(argv, cwd=폴더)`: 실행 폴더를 바꾸고 `main(argv)`를 같은 프로세스에서 부른다. `(종료 코드, stdout, stderr)`를 돌려준다."""

    def run(argv: list[str], *, cwd: Path) -> tuple[int, str, str]:
        monkeypatch.chdir(cwd)
        code = main(argv)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture(autouse=True)
def _no_real_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 서버를 띄우지 않는다. 잘못 부르면 테스트가 끝나지 않으니 바로 실패시킨다."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("테스트는 실제 uvicorn을 띄우지 않는다")

    monkeypatch.setattr(serve.uvicorn, "run", refuse)


@pytest.fixture
def run_server_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, bool]]:
    """`serve.run_server`를 대신한다. 불린 `(config, collect_only)`를 차례로 쌓고 0을 돌려준다."""
    calls: list[tuple[Any, bool]] = []

    def fake(config: Any, *, collect_only: bool) -> int:
        calls.append((config, collect_only))
        return 0

    monkeypatch.setattr(serve, "run_server", fake)
    return calls
