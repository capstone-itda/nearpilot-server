"""`nearpilot --dir` (NFR-10, specs/core-ops-cli/spec.md 5절·6절 "시험 폴더와 실행 보조 명령").

시험 폴더(`tools.node_lab init`이 만드는 폴더)는 `server.toml`이 폴더 바로 아래에 있다. `lab` fixture가 그런 폴더를
`server_dir/var/lab/`에 만들고, 테스트는 실행 폴더(`server_dir`) 기준 상대 경로로 준다.
서버 실행은 `run_server_calls`가 대신하고, 시드는 `--dry-run`이 쓰는 줄로 어느 파일을 읽었는지 본다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from nearpilot.cli import EXIT_INVALID, EXIT_OK

LAB_PORT = 8123  # 기본 설정의 8000과 달라야 어느 설정 파일을 읽었는지 알 수 있다


def write_text(path: Path, text: str) -> Path:
    """부모 폴더를 만들고 UTF-8로 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def seed_of(user_id: str) -> str:
    """계정 하나만 있는 시드. `--dry-run`이 쓰는 `계정 <user_id> 등록` 줄로 어느 시드를 읽었는지 안다."""
    return f'[[accounts]]\nuser_id = "{user_id}"\nname = "{user_id}"\nrole = "owner"\n'


@pytest.fixture
def lab(server_dir: Path, write_config: Callable[..., Path]) -> Path:
    """시험 폴더 `server_dir/var/lab/`. 기본 설정(`server_dir/config/server.toml`)과 포트가 다르고 모델 표가 있다.

    `server.toml`이 폴더 바로 아래에 있고 DB도 이 폴더에 둔다. `write_config`는 `config/` 아래에만 쓰므로 쓴 파일을 옮긴다.
    """
    written = write_config(
        server_dir, name="lab.toml", http={"host": "127.0.0.1", "port": LAB_PORT},
        database={"path": "lab.db", "audit_jsonl": "audit.jsonl"},
        proximity={"model_root": "models", "models": {"node-lab": "synthetic-1"}})
    folder = server_dir / "var" / "lab"
    folder.mkdir(parents=True)
    written.replace(folder / "server.toml")
    return folder


@pytest.mark.parametrize("place", [
    pytest.param(lambda rest: ["--dir", "var/lab", *rest], id="before-subcommand"),
    pytest.param(lambda rest: [*rest, "--dir", "var/lab"], id="after-subcommand"),
])
def test_NFR10_cli_dir_reads_folder_config(server_dir, run_cli, run_server_calls, lab, place):
    code, _, _ = run_cli(place(["serve"]), cwd=server_dir)

    assert code == EXIT_OK
    [(config, collect_only)] = run_server_calls
    # 실행 폴더의 config/server.toml(포트 8000, 모델 표 없음)이 아니라 시험 폴더의 server.toml을 읽었다
    assert config.source == (lab / "server.toml").resolve() and collect_only is False
    assert config.http.port == LAB_PORT and dict(config.proximity.versions) == {"node-lab": "synthetic-1"}

    # 다른 하위 명령도 같은 설정을 읽는다. 시드의 기본 폴더(seed/)가 시험 폴더 기준이다
    write_text(lab / "seed" / "demo.toml", seed_of("lab-owner"))
    write_text(server_dir / "config" / "seed" / "demo.toml", seed_of("default-owner"))

    assert run_cli(place(["seed", "demo.toml", "--dry-run"]), cwd=server_dir) == (
        EXIT_OK, "(dry-run) 계정 lab-owner 등록\n", "")


@pytest.mark.parametrize("place", [
    pytest.param(lambda folder, config: ["--dir", folder, "--config", config, "seed", "demo.toml"], id="both-before"),
    pytest.param(lambda folder, config: ["seed", "demo.toml", "--dir", folder, "--config", config], id="both-after"),
    pytest.param(lambda folder, config: ["--dir", folder, "seed", "demo.toml", "--config", config],
                 id="dir-before-config-after"),
    pytest.param(lambda folder, config: ["--config", config, "seed", "demo.toml", "--dir", folder],
                 id="config-before-dir-after"),
])
def test_NFR10_cli_dir_and_config_conflict(server_dir, run_cli, write_config, lab, place):
    other = write_config(server_dir, name="other.toml")
    # 어느 설정으로도 시드를 읽을 수 있다. 막지 않으면 어느 쪽이든 DB가 생긴다
    write_text(lab / "seed" / "demo.toml", seed_of("lab-owner"))
    write_text(server_dir / "config" / "seed" / "demo.toml", seed_of("default-owner"))

    code, out, err = run_cli(place("var/lab", str(other)), cwd=server_dir)

    assert code == EXIT_INVALID and out == ""
    assert "--dir" in err and "--config" in err and "함께 쓸 수 없다" in err
    assert list(server_dir.rglob("*.db")) == []  # 어느 설정의 DB도 만들지 않았다


def test_NFR10_cli_dir_and_config_conflict_is_checked_before_reading_config(server_dir, run_cli, run_server_calls):
    # 두 파일이 모두 없어도 사유는 설정 오류가 아니라 충돌이다. 설정 파일을 읽기 전에 막는다
    code, out, err = run_cli(["--dir", "no-such-lab", "--config", "no-such.toml", "serve"], cwd=server_dir)

    assert code == EXIT_INVALID and out == ""
    assert "함께 쓸 수 없다" in err and "설정 오류" not in err
    assert run_server_calls == []


def test_NFR10_seed_dir_default_file(server_dir, run_cli, lab):
    # --dir이 있으면 FILE을 생략할 수 있다. 시험 폴더 바로 아래의 seed.toml을 읽는다 (--dir은 하위 명령 앞뒤 어디든)
    write_text(lab / "seed.toml", seed_of("lab-owner"))
    for argv in (["--dir", "var/lab", "seed", "--dry-run"], ["seed", "--dry-run", "--dir", "var/lab"]):
        assert run_cli(argv, cwd=server_dir) == (EXIT_OK, "(dry-run) 계정 lab-owner 등록\n", "")

    # --dir이 없으면 생략할 수 없다
    code, out, err = run_cli(["seed", "--dry-run"], cwd=server_dir)
    assert code == EXIT_INVALID and out == ""
    assert "--dir이 있을 때만 생략할 수 있다" in err

    # --dir이 있는데 DIR/seed.toml이 없다. 기본 폴더(seed/)의 같은 이름 파일로 대신하지 않는다
    (lab / "seed.toml").unlink()
    write_text(lab / "seed" / "seed.toml", seed_of("decoy-owner"))
    code, out, err = run_cli(["--dir", "var/lab", "seed", "--dry-run"], cwd=server_dir)
    assert code == EXIT_INVALID and out == ""
    assert err == f"시드 파일이 없다: {(lab / 'seed.toml').resolve()}\n"


def test_NFR10_cli_dir_help_describes_option(server_dir, run_cli):
    code, out, err = run_cli(["--help"], cwd=server_dir)
    assert code == EXIT_OK and err == "" and "--dir DIR" in out

    code, out, err = run_cli(["seed", "--help"], cwd=server_dir)
    assert code == EXIT_OK and err == ""
    shown = " ".join(out.split())  # 도움말이 줄을 어디서 바꿔도 같게 본다
    assert "[FILE]" in shown and "--dir DIR" in shown
    assert "--dir이 있으면 생략할 수 있다(DIR/seed.toml)" in shown
