"""운영 CLI의 파일 인자 규칙, 기본 폴더, 설정 읽기, 종료 코드 (FR-16, specs/core-ops-cli/spec.md 5절·6절 "명령과 파일").

실제 서버는 띄우지 않는다. `nearpilot.cli.main(argv)`를 같은 프로세스에서 부르고, 서버 실행은 가짜로 대신한다.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_INVALID, EXIT_OK, serve
from nearpilot.cli.common import (
    CliError,
    _uri_path,
    dirs_for,
    is_path_argument,
    load_cli_config,
    open_read_db,
    open_write_db,
    require_db_file,
    resolve_input,
    resolve_output,
)
from nearpilot.db.schema import connect


def test_FR16_cli_missing_default_file_lists_candidates(server_dir):
    dirs = dirs_for(server_dir / "config" / "server.toml")
    dirs.seed.mkdir(parents=True)
    for name in ("b.toml", "a.toml", ".hidden.toml"):
        (dirs.seed / name).write_text("", encoding="utf-8")
    (dirs.seed / "sub").mkdir()  # 폴더는 파일 목록에 넣지 않는다

    with pytest.raises(CliError) as error:
        resolve_input("c.toml", dirs.seed, kind="시드")
    assert error.value.exit_code == EXIT_INVALID == 2
    # 찾은 경로와 기본 폴더의 파일을 이름순으로 쓴다. 숨김 파일은 뺀다.
    assert error.value.message == (
        f"시드 파일이 없다: {dirs.seed / 'c.toml'}. {dirs.seed}에 있는 파일: a.toml, b.toml")
    assert resolve_input("a.toml", dirs.seed, kind="시드") == dirs.seed / "a.toml"

    # 폴더가 없거나 비었으면 목록 자리에 (없음)을 쓴다.
    dirs.plans.mkdir()
    for folder in (dirs.plans, dirs.layouts):
        with pytest.raises(CliError) as error:
            resolve_input("c.json", folder, kind="계획")
        assert error.value.exit_code == 2
        assert error.value.message == f"계획 파일이 없다: {folder / 'c.json'}. {folder}에 있는 파일: (없음)"


@pytest.mark.parametrize("arg", ["./x.toml", "sub/x.toml", "sub\\x.toml"])
def test_FR16_cli_path_argument_uses_cwd(tmp_path, monkeypatch, arg):
    cwd, default_dir = tmp_path / "cwd", tmp_path / "default"
    cwd.mkdir()
    default_dir.mkdir()
    (default_dir / "x.toml").write_text("default", encoding="utf-8")  # 같은 이름이 기본 폴더에 있다
    monkeypatch.chdir(cwd)

    assert is_path_argument(arg)  # 구분자 / 와 \ 를 모두 경로로 본다
    target = cwd / arg  # Windows에서는 sub\x.toml이 하위 폴더이고, 그 밖에서는 이름에 \ 가 든 파일이다
    with pytest.raises(CliError) as error:
        resolve_input(arg, default_dir, kind="시드")
    # 실행 폴더에 없으면 기본 폴더의 같은 이름 파일을 대신 쓰지 않는다.
    assert error.value.exit_code == 2
    assert error.value.message == f"시드 파일이 없다: {target.resolve()}"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("cwd", encoding="utf-8")
    found = resolve_input(arg, default_dir, kind="시드")
    assert found == target.resolve() and found.read_text(encoding="utf-8") == "cwd"


def test_FR16_cli_path_argument_rules():
    assert not is_path_argument("demo.toml")
    assert not is_path_argument("demo")
    assert is_path_argument("/abs/demo.toml") and is_path_argument("C:\\demo\\x.toml")


def test_FR16_cli_default_dirs_follow_config_location(tmp_path):
    server = (tmp_path / "server").resolve()
    dirs = dirs_for(tmp_path / "server" / "config" / "server.toml")  # 파일이 없어도 정한다
    assert dirs.config_dir == server / "config"
    assert (dirs.seed, dirs.plans, dirs.layouts, dirs.fit) == tuple(
        server / "config" / name for name in ("seed", "plans", "layouts", "fit"))
    # var/ 는 설정 파일 폴더의 부모 기준이다
    assert (dirs.splits, dirs.records, dirs.reports) == tuple(
        server / "var" / name for name in ("splits", "records", "reports"))
    assert not server.exists()  # 폴더를 만들지 않는다


def test_FR16_cli_resolve_output_rules(tmp_path, monkeypatch):
    cwd, default_dir = tmp_path / "cwd", tmp_path / "default"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    assert resolve_output(None, default_dir, "p-v1.md") == default_dir / "p-v1.md"
    assert resolve_output("mine.md", default_dir, "p-v1.md") == default_dir / "mine.md"
    assert resolve_output("out/mine.md", default_dir, "p-v1.md") == (cwd / "out" / "mine.md").resolve()
    assert not default_dir.exists() and not (cwd / "out").exists()  # 존재 여부와 폴더 생성은 호출자가 한다


def test_FR16_cli_invalid_config_creates_nothing(server_dir, run_cli, write_config, run_server_calls):
    config_dir = server_dir / "config"
    (config_dir / "not-toml.toml").write_text("이것은 = = TOML 이 아니다\n", encoding="utf-8")
    write_config(server_dir, name="unknown-key.toml", extra={"key": 1})
    cases = [
        ("missing.toml", "설정 파일을 읽을 수 없다"),
        ("not-toml.toml", "설정 파일이 TOML 이 아니다"),
        ("unknown-key.toml", "알 수 없는 설정 키: extra"),
    ]

    for name, reason in cases:
        code, out, err = run_cli(["--config", str(config_dir / name), "serve"], cwd=server_dir)
        assert code == EXIT_INVALID, name
        assert err.startswith("설정 오류: ") and reason in err and err.count("\n") == 1, err
        assert out == ""
    # 기본 설정 파일(실행 폴더의 config/server.toml)이 없는 경우도 같다
    code, _, err = run_cli(["serve"], cwd=server_dir / "config")
    assert code == EXIT_INVALID and "설정 오류" in err and "설정 파일을 읽을 수 없다" in err

    assert run_server_calls == []  # 서버를 시작하지 않는다
    assert not (server_dir / "var").exists()  # DB도 감사 파일도 만들지 않는다


def test_FR16_cli_load_cli_config_checks_models_only_on_request(server_dir):
    path = str(server_dir / "config" / "server.toml")
    assert load_cli_config(path).proximity.versions == {}  # 기본은 모델 표를 요구하지 않는다
    with pytest.raises(CliError) as error:
        load_cli_config(path, require_models=True)
    assert error.value.exit_code == EXIT_INVALID and error.value.message.startswith("설정 오류: ")
    assert "proximity.models" in error.value.message
    assert not (server_dir / "var").exists()  # 설정만 읽고 DB를 열지 않는다


def test_FR16_cli_open_write_db_creates_parent_folder(server_dir):
    config = load_cli_config(str(server_dir / "config" / "server.toml"))
    assert not config.database.path.parent.exists()

    conn = open_write_db(config)
    try:
        assert config.database.path.is_file()
        assert conn.execute("SELECT count(*) FROM user").fetchone()[0] == 0  # 스키마를 적용했다
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == config.runtime.sqlite_busy_timeout_ms
    finally:
        conn.close()


def test_FR16_cli_require_db_file_rejects_missing_db(server_dir):
    config = load_cli_config(str(server_dir / "config" / "server.toml"))

    with pytest.raises(CliError) as error:
        require_db_file(config)
    assert error.value.exit_code == EXIT_INVALID == 2
    assert error.value.message == (
        f"DB 파일이 없다: {config.database.path}. 먼저 nearpilot serve --collect-only로 서버를 한 번 시작한다")
    assert not (server_dir / "var").exists()  # 검사만 하고 아무것도 만들지 않는다

    # 폴더는 DB 파일이 아니다
    config.database.path.mkdir(parents=True)
    with pytest.raises(CliError) as error:
        require_db_file(config)
    assert error.value.exit_code == EXIT_INVALID
    config.database.path.rmdir()

    open_write_db(config).close()  # 서버가 한 번 시작해서 DB를 만든 것과 같다
    assert require_db_file(config) == config.database.path


def test_FR16_cli_open_read_db_opens_read_only(server_dir):
    config = load_cli_config(str(server_dir / "config" / "server.toml"))
    open_write_db(config).close()

    conn = open_read_db(config)
    try:
        assert conn.execute("SELECT count(*) FROM user").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):  # 쓰기 문장은 sqlite가 거부한다
            conn.execute("DELETE FROM user")
    finally:
        conn.close()


def test_FR16_cli_open_read_db_does_not_create_a_missing_db(server_dir):
    config = load_cli_config(str(server_dir / "config" / "server.toml"))

    with pytest.raises(CliError) as error:
        open_read_db(config)

    assert error.value.exit_code == EXIT_INVALID
    assert error.value.message.startswith(f"DB 파일이 없다: {config.database.path}. ")
    assert not (server_dir / "var").exists()  # sqlite가 빈 DB를 새로 만들지 않는다


def test_FR16_cli_open_read_db_rejects_other_schema_version(server_dir):
    config = load_cli_config(str(server_dir / "config" / "server.toml"))
    open_write_db(config).close()
    conn = sqlite3.connect(config.database.path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    with pytest.raises(CliError) as error:
        open_read_db(config)

    assert error.value.exit_code == EXIT_INVALID
    assert error.value.message.startswith("DB 스키마 버전 99이 이 도구가 기대하는 ")


# 폴더 이름의 ? 는 Windows 파일 이름에 쓸 수 없다
_NO_QUESTION_MARK_ON_WINDOWS = pytest.mark.skipif(sys.platform == "win32", reason="Windows는 폴더 이름에 ?를 쓸 수 없다")


@pytest.mark.parametrize("folder", [
    "with space",
    "hash#dir",
    "percent%20dir",
    "all of 100% #1",
    "한글 폴더",
    pytest.param("question?dir", marks=_NO_QUESTION_MARK_ON_WINDOWS),
    pytest.param("a b#c%d?e", marks=_NO_QUESTION_MARK_ON_WINDOWS),
])
def test_FR16_cli_read_db_path_with_special_characters(tmp_path, write_config, folder):
    db_path = tmp_path / folder / "nearpilot.db"
    db_path.parent.mkdir()
    writer = connect(str(db_path))
    try:
        writer.execute("INSERT INTO user (user_id, name, token_hash, role) VALUES ('owner', '소유자', 'hash', 'owner')")
    finally:
        writer.close()
    server = tmp_path / "server"
    config_path = write_config(
        server, database={"path": str(db_path), "audit_jsonl": str(db_path.with_name("audit.jsonl"))})

    conn = open_read_db(load_cli_config(str(config_path)))
    try:
        # URI가 다른 파일을 가리켰다면 열리지 않았거나 빈 DB였을 것이다. 쓴 행이 그대로 보인다
        assert [row[0] for row in conn.execute("SELECT user_id FROM user")] == ["owner"]
    finally:
        conn.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([folder, "server"])  # 다른 폴더를 만들지 않았다


def test_FR16_cli_uri_path_encodes_for_the_read_only_uri():
    class Resolved:
        """`Path.resolve()`가 정해진 경로를 돌려주는 경로. 이 PC에서 Windows 경로 모양을 만들 때 쓴다."""

        def __init__(self, resolved) -> None:
            self._resolved = resolved

        def resolve(self):
            return self._resolved

    # Windows: 드라이브 문자로 시작하면 앞에 /를 붙여 file:/C:/… 가 된다
    assert _uri_path(Resolved(PureWindowsPath("C:\\x\\y.db"))) == "/C:/x/y.db"
    windows = Resolved(PureWindowsPath("d:\\내 폴더\\a#b\\c.db"))
    assert _uri_path(windows) == "/d:/%EB%82%B4%20%ED%8F%B4%EB%8D%94/a%23b/c.db"
    # POSIX: URI 문법으로 읽히는 글자(공백, #, %, ?)와 한글을 퍼센트 인코딩한다. / 와 : 는 그대로 둔다
    special = Resolved(PurePosixPath("/data/a b#c%d?e/한글.db"))
    assert _uri_path(special) == "/data/a%20b%23c%25d%3Fe/%ED%95%9C%EA%B8%80.db"
    assert _uri_path(Resolved(PurePosixPath("/data/plain/nearpilot.db"))) == "/data/plain/nearpilot.db"
    assert _uri_path(Resolved(PurePosixPath("/mnt/a:b/x.db"))) == "/mnt/a:b/x.db"  # 경로 중간의 :는 드라이브 문자가 아니다
    assert _uri_path(Resolved(PurePosixPath("/:odd/x.db"))) == "/:odd/x.db"  # 루트 바로 아래 이름이 :로 시작해도 그렇다


def test_FR16_cli_config_option_goes_before_or_after_subcommand(server_dir, run_cli, run_server_calls, tmp_path):
    path = server_dir / "config" / "server.toml"  # 모델 표가 없는 설정이라 --collect-only 로 시작한다
    for argv in (["--config", str(path), "serve", "--collect-only"],
                 ["serve", "--collect-only", "--config", str(path)]):
        code, _, err = run_cli(argv, cwd=tmp_path)  # 실행 폴더와 상관없이 같은 설정을 읽는다
        assert code == EXIT_OK, (argv, err)
    # 쓰지 않으면 실행 폴더 기준 config/server.toml 이다
    code, _, err = run_cli(["serve", "--collect-only"], cwd=server_dir)
    assert code == EXIT_OK, err
    assert [config.source for config, _ in run_server_calls] == [path.resolve()] * 3


def test_FR16_cli_main_returns_codes_without_exiting(run_cli, tmp_path):
    code, out, _ = run_cli(["--help"], cwd=tmp_path)
    assert code == EXIT_OK and "nearpilot" in out and "serve" in out
    code, out, _ = run_cli(["serve", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and "--collect-only" in out and "--config" in out

    for argv in ([], ["unknown-command"], ["serve", "--unknown-option"]):  # 인자 오류는 SystemExit 대신 2다
        code, out, err = run_cli(argv, cwd=tmp_path)
        assert code == EXIT_INVALID, argv
        assert out == "" and "nearpilot" in err, argv  # argparse가 사용법과 사유를 stderr에 쓴다


def test_FR16_cli_main_reports_errors_and_interrupt(server_dir, run_cli, monkeypatch):
    def reject(config, *, collect_only):
        raise CliError("거부한 사유", EXIT_FAILED)

    monkeypatch.setattr(serve, "run_server", reject)
    code, out, err = run_cli(["serve", "--collect-only"], cwd=server_dir)
    assert (code, out, err) == (EXIT_FAILED, "", "거부한 사유\n")  # 사유를 한 줄로 쓰고 CliError의 종료 코드를 돌려준다

    def interrupt(config, *, collect_only):
        raise KeyboardInterrupt

    monkeypatch.setattr(serve, "run_server", interrupt)
    code, out, err = run_cli(["serve", "--collect-only"], cwd=server_dir)
    assert (code, out, err) == (EXIT_INTERRUPTED, "", "Ctrl+C로 멈췄다.\n")
    assert EXIT_INTERRUPTED == 130
