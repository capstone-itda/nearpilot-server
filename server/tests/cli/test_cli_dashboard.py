"""`nearpilot dashboard` (FR-12B, specs/core-ops-cli/spec.md 5절·6절 "시험 폴더와 실행 보조 명령").

실제 npm은 부르지 않는다. `shutil.which`와 `subprocess.run`을 가짜로 바꿔 끼운다 (autouse라서 모든 테스트가 쓴다).
실제 `dashboard/` 폴더도 쓰지 않는다 (`node_modules`가 없을 수 있다). `--dashboard-dir`로 임시 폴더(`package.json`, `node_modules/`)를 준다.
임시 대시보드 폴더(`dashboard` fixture)는 한글·공백 경로다. 실행 폴더(`server_dir`) 기준 상대 경로는 `DASH_ARG`다.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.dashboard import default_dashboard_dir

NPM = "/fake/bin/npm.cmd"  # Windows에서 shutil.which("npm")은 npm.cmd를 찾는다. 찾은 경로를 그대로 첫 인자로 쓴다
DASH_ARG = "../대시보드 폴더"  # `dashboard` fixture는 server_dir과 같은 부모 아래에 폴더를 만든다


@dataclass
class NpmCall:
    args: list[str]
    kwargs: dict[str, Any]  # args 말고 넘긴 인자 (cwd, env)


class FakeNpm:
    """`shutil.which`와 `subprocess.run`을 대신한다. `path`가 None이면 npm이 없는 PATH다."""

    def __init__(self) -> None:
        self.path: str | None = NPM
        self.returncode = 0
        self.interrupt = False  # True면 npm이 도는 동안 사용자가 Ctrl+C를 누른다
        self.calls: list[NpmCall] = []

    def which(self, name: str) -> str | None:
        assert name == "npm"
        return self.path

    def run(self, args: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(NpmCall(list(args), kwargs))
        if self.interrupt:
            raise KeyboardInterrupt
        return SimpleNamespace(returncode=self.returncode)


@pytest.fixture(autouse=True)
def fake_npm(monkeypatch: pytest.MonkeyPatch) -> FakeNpm:
    """잘못 부르면 진짜 npm이 개발 서버를 띄우고 끝나지 않으니 모든 테스트에서 바꿔 끼운다."""
    fake = FakeNpm()
    monkeypatch.setattr("nearpilot.cli.dashboard.shutil.which", fake.which)
    monkeypatch.setattr("nearpilot.cli.dashboard.subprocess.run", fake.run)
    return fake


@pytest.fixture
def dashboard(tmp_path: Path) -> Path:
    """대시보드 폴더 모양의 임시 폴더 `tmp_path/대시보드 폴더`: `package.json`, `node_modules/`. 절대 경로를 돌려준다."""
    folder = tmp_path / "대시보드 폴더"
    (folder / "node_modules").mkdir(parents=True)
    (folder / "package.json").write_text('{"name": "nearpilot-dashboard"}\n', encoding="utf-8")
    return folder.resolve()


def test_FR12B_dashboard_targets_configured_server(server_dir, write_config, run_cli, fake_npm, dashboard, monkeypatch):
    write_config(server_dir, http={"host": "0.0.0.0", "port": 8123})  # 기본 포트(8000)와 다르다
    monkeypatch.setenv("NEARPILOT_TEST_KEPT", "kept")
    fake_npm.returncode = 1  # npm이 0이 아닌 값으로 끝난다

    code, out, err = run_cli(["dashboard", "--dashboard-dir", DASH_ARG], cwd=server_dir)

    assert code == EXIT_FAILED and out == "" and err == ""
    [call] = fake_npm.calls
    # 같은 AP의 다른 기기에서도 열리게 0.0.0.0으로 연다. which가 찾은 경로를 그대로 첫 인자로 쓴다
    assert call.args == [NPM, "run", "dev", "--", "--host", "0.0.0.0"]
    # 대시보드 폴더에서 띄운다. cwd와 env 말고는 넘기지 않는다 (셸을 거치지 않고 출력은 터미널 그대로다)
    assert call.kwargs.keys() == {"cwd", "env"} and call.kwargs["cwd"] == dashboard
    # 프록시가 가리킬 서버는 설정의 포트다. 다른 환경 변수는 그대로 넘긴다
    env = call.kwargs["env"]
    assert env["NEARPILOT_SERVER"] == "http://127.0.0.1:8123"
    assert env["NEARPILOT_TEST_KEPT"] == "kept"


def test_FR12B_dashboard_default_dir_is_repository_dashboard():
    # 파일을 읽지 않고 위치만 본다: server/tests/cli/ 에서 저장소 루트의 dashboard/
    assert default_dashboard_dir() == Path(__file__).resolve().parents[3] / "dashboard"


@pytest.mark.parametrize(("returncode", "expected"), [
    pytest.param(0, EXIT_OK, id="exit-0"),
    pytest.param(1, EXIT_FAILED, id="exit-1"),
    pytest.param(2, EXIT_FAILED, id="exit-2-is-not-an-input-error"),  # 2는 이 명령에서 입력 오류의 값이다
    pytest.param(-15, EXIT_FAILED, id="killed"),
])
def test_FR12B_dashboard_npm_exit_code(server_dir, run_cli, fake_npm, dashboard, returncode, expected):
    fake_npm.returncode = returncode

    code, out, err = run_cli(["dashboard", "--dashboard-dir", DASH_ARG], cwd=server_dir)

    assert code == expected and out == "" and err == ""


def test_FR12B_dashboard_overrides_inherited_server_variable(server_dir, run_cli, fake_npm, dashboard, monkeypatch):
    monkeypatch.setenv("NEARPILOT_SERVER", "http://old-host:9999")  # 셸에 남은 값

    code, _, _ = run_cli(["dashboard", "--dashboard-dir", DASH_ARG], cwd=server_dir)

    assert code == EXIT_OK
    [call] = fake_npm.calls
    assert call.kwargs["env"]["NEARPILOT_SERVER"] == "http://127.0.0.1:8000"
    assert os.environ["NEARPILOT_SERVER"] == "http://old-host:9999"  # 이 프로세스의 환경은 바꾸지 않는다


def test_FR12B_dashboard_defaults_to_repository_dashboard(server_dir, run_cli, fake_npm, dashboard, monkeypatch):
    # --dashboard-dir이 없으면 default_dashboard_dir()를 쓴다. 실제 dashboard/ 대신 임시 폴더로 바꿔 끼운다
    monkeypatch.setattr("nearpilot.cli.dashboard.default_dashboard_dir", lambda: dashboard)

    code, _, _ = run_cli(["dashboard"], cwd=server_dir)

    assert code == EXIT_OK
    [call] = fake_npm.calls
    assert call.kwargs["cwd"] == dashboard


def test_FR12B_dashboard_reads_port_from_folder_config(server_dir, write_config, run_cli, fake_npm, dashboard, tmp_path):
    # 시험 폴더의 server.toml이 정한 포트로 붙는다. server/config/server.toml의 포트는 기본 8000이다
    lab = tmp_path / "시험 폴더"
    lab.mkdir()
    write_config(server_dir, name="lab.toml", http={"host": "0.0.0.0", "port": 8123}).replace(lab / "server.toml")

    code, _, _ = run_cli(["dashboard", "--dir", "../시험 폴더", "--dashboard-dir", DASH_ARG], cwd=server_dir)

    assert code == EXIT_OK
    [call] = fake_npm.calls
    assert call.kwargs["env"]["NEARPILOT_SERVER"] == "http://127.0.0.1:8123"


def _without_folder(dashboard: Path, fake: FakeNpm) -> str:
    shutil.rmtree(dashboard)
    return f"대시보드 폴더가 아니다: {dashboard}. --dashboard-dir로 준다\n"


def _without_package_json(dashboard: Path, fake: FakeNpm) -> str:
    (dashboard / "package.json").unlink()
    return f"대시보드 폴더가 아니다: {dashboard}. --dashboard-dir로 준다\n"


def _without_node_modules(dashboard: Path, fake: FakeNpm) -> str:
    (dashboard / "node_modules").rmdir()
    return f"대시보드 의존성이 없다. {dashboard}에서 npm install을 먼저 실행한다\n"


def _without_npm(dashboard: Path, fake: FakeNpm) -> str:
    fake.path = None
    return "npm 실행 파일이 필요하다. Node.js를 설치하고 PATH에 추가한다\n"


@pytest.mark.parametrize("break_it", [
    pytest.param(_without_folder, id="no-folder"),
    pytest.param(_without_package_json, id="no-package-json"),
    pytest.param(_without_node_modules, id="no-node-modules"),
    pytest.param(_without_npm, id="no-npm"),
])
def test_FR12B_dashboard_missing_prerequisites_exit_2(server_dir, run_cli, fake_npm, dashboard, break_it):
    expected = break_it(dashboard, fake_npm)

    code, out, err = run_cli(["dashboard", "--dashboard-dir", DASH_ARG], cwd=server_dir)

    assert code == EXIT_INVALID and out == "" and err == expected
    assert fake_npm.calls == []  # npm을 부르지 않는다


def test_FR12B_dashboard_without_config_exit_2(tmp_path, run_cli, fake_npm, dashboard):
    # 서버 포트를 설정에서 읽는다. 설정이 없는 폴더에서 실행하면 npm을 부르지 않는다
    code, out, err = run_cli(["dashboard", "--dashboard-dir", str(dashboard)], cwd=tmp_path)

    assert code == EXIT_INVALID and out == "" and err.startswith("설정 오류: ")
    assert fake_npm.calls == []


def test_FR12B_dashboard_ctrl_c_exits_130(server_dir, run_cli, fake_npm, dashboard):
    fake_npm.interrupt = True

    code, out, err = run_cli(["dashboard", "--dashboard-dir", DASH_ARG], cwd=server_dir)

    assert code == EXIT_INTERRUPTED and out == "" and err == "Ctrl+C로 멈췄다.\n"
    assert len(fake_npm.calls) == 1


def test_FR12B_dashboard_help_describes_command(server_dir, run_cli):
    code, out, err = run_cli(["--help"], cwd=server_dir)
    assert code == EXIT_OK and err == ""
    assert "대시보드 개발 서버(npm run dev)를 띄운다" in " ".join(out.split())  # 도움말이 줄을 어디서 바꿔도 같게 본다

    code, out, err = run_cli(["dashboard", "--help"], cwd=server_dir)
    assert code == EXIT_OK and err == ""
    assert "--dashboard-dir PATH" in out and "--dir DIR" in out and "--config PATH" in out
