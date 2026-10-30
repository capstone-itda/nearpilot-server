"""`nearpilot sim` (NFR-10, specs/core-ops-cli/spec.md 5절·6절 "시험 폴더와 실행 보조 명령").

실제 가상 기기는 띄우지 않는다. `node_sim_main`을 가짜로 바꿔 끼운다. 가짜는 받은 인자를 쌓고 정해 둔 종료 코드를 돌려준다.
시험 폴더(`lab` fixture)는 한글·공백 경로다. 실행 폴더(`server_dir`) 기준 상대 경로는 `LAB_ARG`다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from nearpilot.cli import EXIT_INVALID, EXIT_OK

LAB_ARG = "../시험 폴더"  # `lab` fixture는 server_dir과 같은 부모 아래에 시험 폴더를 만든다
NO_LAB_JSON = "lab.json이 없다: {lab_json}. 가상 기기는 node_lab init --mode sim 폴더에서만 띄운다\n"
NOT_SIM = "가상 기기는 mode가 sim인 시험 폴더에서만 띄운다: mode={mode}\n"


class FakeNodeSim:
    """`tools.node_sim.__main__.main`을 대신한다. 인자 목록 하나만 받는다."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.result = 0

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return self.result


@pytest.fixture(autouse=True)
def fake_sim(monkeypatch: pytest.MonkeyPatch) -> FakeNodeSim:
    """진짜 `main`은 입력을 기다리며 끝나지 않으므로 모든 테스트에서 바꿔 끼운다."""
    fake = FakeNodeSim()
    monkeypatch.setattr("nearpilot.cli.sim.node_sim_main", fake)
    return fake


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def lab(tmp_path: Path, server_dir: Path, write_config: Callable[..., Path]) -> Path:
    """시험 폴더 `tmp_path/시험 폴더`: `server.toml`, `lab.json`(mode=sim), `fleet.json`. 절대 경로를 돌려준다."""
    folder = tmp_path / "시험 폴더"
    folder.mkdir()
    write_config(server_dir, name="lab.toml").replace(folder / "server.toml")
    write_json(folder / "lab.json", {"mode": "sim", "nodes": ["light-1", "light-2"]})
    write_json(folder / "fleet.json", {"devices": []})  # 가짜는 읽지 않는다
    return folder.resolve()


@pytest.mark.parametrize("place", [
    pytest.param(lambda rest: ["--dir", LAB_ARG, *rest], id="before-subcommand"),
    pytest.param(lambda rest: [*rest, "--dir", LAB_ARG], id="after-subcommand"),
])
def test_NFR10_sim_runs_folder_fleet(server_dir, run_cli, fake_sim, lab, place):
    code, out, err = run_cli(place(["sim"]), cwd=server_dir)

    assert code == EXIT_OK and out == "" and err == ""
    # 시험 폴더의 fleet.json을 절대 경로로 넘긴다
    assert fake_sim.calls == [["run", "--config", str(lab / "fleet.json")]]


def _hardware(lab: Path) -> str:
    write_json(lab / "lab.json", {"mode": "hardware", "nodes": ["light-1", "light-2"]})  # node_lab init은 fleet.json도 쓴다
    return NOT_SIM.format(mode="hardware")


def _without_lab_json(lab: Path) -> str:
    (lab / "lab.json").unlink()
    return NO_LAB_JSON.format(lab_json=lab / "lab.json")


def _without_fleet_json(lab: Path) -> str:
    (lab / "fleet.json").unlink()
    return f"fleet.json이 없다: {lab / 'fleet.json'}\n"


@pytest.mark.parametrize("break_it", [
    pytest.param(_hardware, id="hardware"),
    pytest.param(_without_lab_json, id="no-lab-json"),
    pytest.param(_without_fleet_json, id="no-fleet-json"),
])
def test_NFR10_sim_rejects_hardware_lab(server_dir, run_cli, fake_sim, lab, break_it):
    expected = break_it(lab)

    code, out, err = run_cli(["--dir", LAB_ARG, "sim"], cwd=server_dir)

    assert code == EXIT_INVALID and out == "" and err == expected
    assert fake_sim.calls == []  # 가상 기기를 띄우지 않는다


@pytest.mark.parametrize(("content", "message"), [
    pytest.param(b'{"mode": "sim"', "lab.json을 읽을 수 없다: {lab_json}\n", id="truncated-json"),
    pytest.param(b"\xb0\xa1 = cp949", "lab.json을 읽을 수 없다: {lab_json}\n", id="not-utf8"),
    pytest.param(b"{}", NOT_SIM.format(mode="None"), id="no-mode"),
    pytest.param(b'["sim"]', NOT_SIM.format(mode="None"), id="not-an-object"),
    pytest.param(b'{"mode": "Sim"}', NOT_SIM.format(mode="Sim"), id="mode-case-differs"),
])
def test_NFR10_sim_rejects_unusable_lab_json(server_dir, run_cli, fake_sim, lab, content, message):
    (lab / "lab.json").write_bytes(content)

    code, out, err = run_cli(["--dir", LAB_ARG, "sim"], cwd=server_dir)

    assert code == EXIT_INVALID and out == "" and err == message.format(lab_json=lab / "lab.json")
    assert fake_sim.calls == []


@pytest.mark.parametrize("result", [0, 2, 3])
def test_NFR10_sim_returns_node_sim_exit_code(server_dir, run_cli, fake_sim, lab, result):
    fake_sim.result = result  # 2는 node_sim이 fleet.json의 형식 오류를 직접 쓰고 돌려주는 값이다

    code, _, _ = run_cli(["--dir", LAB_ARG, "sim"], cwd=server_dir)

    assert code == result


def test_NFR10_sim_uses_folder_of_config_file(server_dir, run_cli, fake_sim, lab):
    # --dir이 아니라 --config로 시험 폴더의 설정을 줘도, 설정 파일이 있는 폴더가 설정 폴더다
    code, _, _ = run_cli(["sim", "--config", f"{LAB_ARG}/server.toml"], cwd=server_dir)

    assert code == EXIT_OK
    assert fake_sim.calls == [["run", "--config", str(lab / "fleet.json")]]


def test_NFR10_sim_operational_config_folder_is_not_a_lab(server_dir, run_cli, fake_sim):
    # --dir 없이 server/config/server.toml을 읽으면 설정 폴더(config/)에 lab.json이 없다
    code, out, err = run_cli(["sim"], cwd=server_dir)

    assert code == EXIT_INVALID and out == ""
    assert err == NO_LAB_JSON.format(lab_json=server_dir.resolve() / "config" / "lab.json")
    assert fake_sim.calls == []


def test_NFR10_sim_help_describes_command(server_dir, run_cli):
    code, out, err = run_cli(["--help"], cwd=server_dir)
    assert code == EXIT_OK and err == ""
    assert "시험 폴더의 가상 기기를 띄운다 (lab.json의 mode가 sim일 때만)" in " ".join(out.split())  # 도움말이 줄을 어디서 바꿔도 같게 본다

    code, out, err = run_cli(["sim", "--help"], cwd=server_dir)
    assert code == EXIT_OK and err == "" and "--dir DIR" in out and "--config PATH" in out
