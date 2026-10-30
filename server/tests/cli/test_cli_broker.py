"""`nearpilot broker` (NFR-12, specs/core-ops-cli/spec.md 5절·6절 "시험 폴더와 실행 보조 명령").

실제 Mosquitto는 띄우지 않는다. `executable`과 `subprocess.Popen`을 가짜로 바꿔 끼운다. 가짜 `Popen`은 불린 순간에
임시 설정 파일과 복사본의 내용을 읽어 둔다 (임시 폴더는 명령이 끝나면 지워진다).
명령이 만드는 임시 폴더는 `temp_root` 안으로 모으고, 끝난 뒤 남은 것이 없는지 본다.
시험 폴더(`lab` fixture)는 한글 경로다. 실행 폴더(`server_dir`) 기준 상대 경로는 `LAB_ARG`다.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.broker import prepare_config
from nearpilot.cli.common import CliError

EXECUTABLE = "/fake/bin/mosquitto"
LAB_ARG = "../시험 폴더"  # `lab` fixture는 server_dir과 같은 부모 아래에 시험 폴더를 만든다

# 시험 폴더의 broker.conf: tools.node_lab init의 형식에 주석 한 줄을 더했다. 경로는 시험 폴더 기준이다.
BROKER_CONF = """\
# 시험 폴더 브로커
listener 1883 127.0.0.1
allow_anonymous false
# password_file과 acl_file은 아래 두 줄이 정한다
password_file .env.broker-passwords
acl_file broker.acl
persistence false
log_type error
log_type warning
"""
# 운영 설정: server/config/mosquitto.conf의 모양. 경로는 server/ 기준이다.
OPERATIONAL_CONF = """\
# Run from server/: mosquitto -c config/mosquitto.conf
listener 1883 127.0.0.1
allow_anonymous false
password_file config/.env.mosquitto-passwords
acl_file config/mosquitto.acl.example
persistence false
connection_messages false
log_type error
log_type warning
"""
PASSWORDS = "node-a:$7$hash-a\nnode-b:$7$hash-b\n"
ACL = "user node-a\ntopic write nearpilot/node/node-a/announce\n"


@dataclass
class Launch:
    """`Popen`이 불린 순간의 모습. 임시 폴더는 명령이 끝나면 지워지므로 그때 읽어 둔다."""

    args: list[str]
    kwargs: dict[str, Any]  # args 말고 넘긴 인자 (cwd 등)
    temp: Path  # 바꾼 설정이 든 임시 폴더
    files: dict[str, str]  # 임시 폴더 안의 파일 이름 → 내용


class FakeBroker:
    """`executable("mosquitto")`와 `subprocess.Popen`을 대신하고, 돌고 있는 브로커 프로세스 역할도 한다.

    `waits`는 `wait()`를 부를 때마다 앞에서부터 하나씩 따른다. 예외면 던지고 아니면 종료 코드로 돌려준다.
    기본은 사용자가 Ctrl+C를 누르는 것이다. 브로커는 `terminate()` 뒤에 끝난다 (두 번째 `wait`).
    """

    def __init__(self) -> None:
        self.waits: list[Any] = [KeyboardInterrupt(), -15]
        self.executable_error: ValueError | None = None
        self.launches: list[Launch] = []
        self.events: list[str] = []  # 프로세스에 한 일. `wait(timeout=5)`는 "wait:5"다
        self.returncode: int | None = None

    def executable(self, name: str) -> str:
        assert name == "mosquitto"
        if self.executable_error is not None:
            raise self.executable_error
        return EXECUTABLE

    def popen(self, args: list[str], **kwargs: Any) -> FakeBroker:
        temp = Path(args[2]).parent  # [실행 파일, "-c", 바꾼 설정]
        files = {path.name: path.read_text(encoding="utf-8") for path in temp.iterdir()}
        self.launches.append(Launch(args, kwargs, temp, files))
        return self

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait" if timeout is None else f"wait:{timeout}")
        outcome = self.waits.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = outcome
        return outcome

    def poll(self) -> int | None:
        self.events.append("poll")
        return self.returncode

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """명령이 만드는 임시 폴더를 이 테스트의 폴더 안으로 모은다. 명령이 끝나면 비어 있어야 한다."""
    root = tmp_path / "temp-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    if not str(root).isascii():  # pytest 임시 폴더가 한글 계정 아래에 있다. ASCII 검사는 따로 시험한다
        monkeypatch.setattr("nearpilot.cli.broker._ascii", lambda path: True)
    return root


@pytest.fixture
def fake_broker(monkeypatch: pytest.MonkeyPatch, temp_root: Path) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr("nearpilot.cli.broker.executable", fake.executable)
    monkeypatch.setattr("nearpilot.cli.broker.subprocess.Popen", fake.popen)
    return fake


@pytest.fixture
def lab(tmp_path: Path, server_dir: Path, write_config: Callable[..., Path]) -> Path:
    """시험 폴더 `tmp_path/시험 폴더`: `server.toml`, `broker.conf`, 그 설정이 가리키는 두 파일. 절대 경로를 돌려준다."""
    folder = tmp_path / "시험 폴더"
    folder.mkdir()
    write_config(server_dir, name="lab.toml").replace(folder / "server.toml")
    (folder / "broker.conf").write_text(BROKER_CONF, encoding="utf-8")
    (folder / ".env.broker-passwords").write_text(PASSWORDS, encoding="utf-8")
    (folder / "broker.acl").write_text(ACL, encoding="utf-8")
    return folder.resolve()


def write_operational(server: Path) -> None:
    """`server/config/`에 운영용 브로커 설정과 그 설정이 가리키는 두 파일을 쓴다."""
    folder = server / "config"
    (folder / "mosquitto.conf").write_text(OPERATIONAL_CONF, encoding="utf-8")
    (folder / ".env.mosquitto-passwords").write_text(PASSWORDS, encoding="utf-8")
    (folder / "mosquitto.acl.example").write_text(ACL, encoding="utf-8")


def rewritten(conf: str, temp: Path, *, password_file: str, acl_file: str) -> str:
    """원본 설정에서 두 줄만 임시 폴더의 복사본 경로로 바꾼 모양."""
    return conf.replace(f"password_file {password_file}", f"password_file {(temp / 'passwords').as_posix()}").replace(
        f"acl_file {acl_file}", f"acl_file {(temp / 'acl').as_posix()}")


@pytest.mark.parametrize("place", [
    pytest.param(lambda rest: ["--dir", LAB_ARG, *rest], id="before-subcommand"),
    pytest.param(lambda rest: [*rest, "--dir", LAB_ARG], id="after-subcommand"),
])
def test_NFR12_broker_uses_folder_broker_conf(server_dir, run_cli, fake_broker, temp_root, lab, place):
    code, out, err = run_cli(place(["broker"]), cwd=server_dir)

    assert code == EXIT_INTERRUPTED and err == "Ctrl+C로 멈췄다.\n"
    assert out == f"브로커 실행 중: {lab / 'broker.conf'}. Ctrl+C로 끝낸다.\n"
    [launch] = fake_broker.launches
    # 시험 폴더에서 띄운다. 출력은 터미널 그대로다 (stdout, stderr를 넘기지 않는다)
    assert launch.args == [EXECUTABLE, "-c", str(launch.temp / "mosquitto.conf")]
    assert launch.kwargs == {"cwd": lab}
    # 설정이 가리키는 두 파일은 임시 폴더로 복사되고 설정은 그 경로를 따옴표 없이 쓴다. 다른 줄은 그대로다
    assert launch.temp.parent == temp_root and launch.temp.name.startswith("np-broker-")
    assert launch.files == {
        "passwords": PASSWORDS, "acl": ACL,
        "mosquitto.conf": rewritten(
            BROKER_CONF, launch.temp, password_file=".env.broker-passwords", acl_file="broker.acl")}
    # Ctrl+C: 브로커가 아직 돌고 있어서 끝낸다. 시간 안에 끝나므로 kill은 없다
    assert fake_broker.events == ["wait", "poll", "terminate", "wait:5"]
    # 임시 폴더는 지워지고 시험 폴더의 원본은 그대로다
    assert list(temp_root.iterdir()) == []
    assert (lab / "broker.conf").read_text(encoding="utf-8") == BROKER_CONF


def test_NFR12_broker_uses_operational_conf(server_dir, run_cli, fake_broker, temp_root):
    write_operational(server_dir)

    code, out, err = run_cli(["broker"], cwd=server_dir)

    assert code == EXIT_INTERRUPTED and err == "Ctrl+C로 멈췄다.\n"
    assert out == f"브로커 실행 중: {server_dir.resolve() / 'config' / 'mosquitto.conf'}. Ctrl+C로 끝낸다.\n"
    [launch] = fake_broker.launches
    # 경로는 설정 폴더(config/)가 아니라 그 부모(server/) 기준이다. server/에서 mosquitto -c config/mosquitto.conf로 띄우던 것과 같다
    assert launch.kwargs == {"cwd": server_dir.resolve()}
    assert launch.files == {
        "passwords": PASSWORDS, "acl": ACL,
        "mosquitto.conf": rewritten(
            OPERATIONAL_CONF, launch.temp, password_file="config/.env.mosquitto-passwords",
            acl_file="config/mosquitto.acl.example")}
    assert list(temp_root.iterdir()) == []
    assert (server_dir / "config" / "mosquitto.conf").read_text(encoding="utf-8") == OPERATIONAL_CONF


def test_NFR12_broker_conf_folder_follows_config_option(server_dir, run_cli, fake_broker, write_config):
    # 설정 파일이 다른 서버 폴더에 있다. 실행 폴더의 config/에는 mosquitto.conf가 없다
    other = server_dir.parent / "other-server"
    config = write_config(other)
    write_operational(other)

    code, _, _ = run_cli(["--config", str(config), "broker"], cwd=server_dir)

    assert code == EXIT_INTERRUPTED
    [launch] = fake_broker.launches
    assert launch.kwargs == {"cwd": other.resolve()}
    assert launch.files["passwords"] == PASSWORDS and launch.files["acl"] == ACL


def _without_conf(lab: Path, fake: FakeBroker) -> str:
    (lab / "broker.conf").unlink()
    return f"브로커 설정 파일이 없다: {lab / 'broker.conf'}\n"


def _without_password_file(lab: Path, fake: FakeBroker) -> str:
    (lab / ".env.broker-passwords").unlink()
    return f"브로커 설정이 가리키는 파일이 없다: {lab / '.env.broker-passwords'}\n"


def _without_acl_file(lab: Path, fake: FakeBroker) -> str:
    (lab / "broker.acl").unlink()  # 비밀번호 파일은 이미 복사한 뒤다. 임시 폴더는 그래도 지워져야 한다
    return f"브로커 설정이 가리키는 파일이 없다: {lab / 'broker.acl'}\n"


def _without_mosquitto(lab: Path, fake: FakeBroker) -> str:
    fake.executable_error = ValueError("mosquitto 실행 파일이 필요하다. PATH에 추가한다.")
    return "mosquitto 실행 파일이 필요하다. PATH에 추가한다.\n"


def _with_repeated_key(lab: Path, fake: FakeBroker) -> str:
    with (lab / "broker.conf").open("a", encoding="utf-8") as stream:
        stream.write("password_file .env.broker-passwords\n")
    return f"브로커 설정에 password_file 줄이 둘 이상 있다: {lab / 'broker.conf'}\n"


@pytest.mark.parametrize("break_it", [
    pytest.param(_without_conf, id="no-conf"),
    pytest.param(_with_repeated_key, id="repeated-key"),
    pytest.param(_without_password_file, id="no-password-file"),
    pytest.param(_without_acl_file, id="no-acl-file"),
    pytest.param(_without_mosquitto, id="no-mosquitto"),
])
def test_NFR12_broker_missing_files_exit_2(server_dir, run_cli, fake_broker, temp_root, lab, break_it):
    expected = break_it(lab, fake_broker)

    code, out, err = run_cli(["--dir", LAB_ARG, "broker"], cwd=server_dir)

    assert code == EXIT_INVALID and out == "" and err == expected
    assert fake_broker.launches == [] and fake_broker.events == []  # 브로커를 띄우지 않는다
    assert list(temp_root.iterdir()) == []


def test_NFR12_broker_rejects_non_ascii_temp(server_dir, run_cli, fake_broker, lab, tmp_path, monkeypatch):
    korean = tmp_path / "임시 폴더"  # 계정 이름이 한글인 Windows의 시스템 임시 폴더와 같다
    korean.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(korean))
    monkeypatch.setattr("nearpilot.cli.broker._ascii", str.isascii)  # temp_root가 바꿨을 수 있다
    prepared = []
    monkeypatch.setattr("nearpilot.cli.broker.prepare_config", lambda *args: prepared.append(args))

    code, out, err = run_cli(["--dir", LAB_ARG, "broker"], cwd=server_dir)

    assert code == EXIT_INVALID and out == ""
    assert err.startswith("임시 폴더 경로에 영문이 아닌 문자가 있다:") and "TEMP" in err
    assert prepared == []  # 복사하기 전에 멈춘다
    assert fake_broker.launches == [] and fake_broker.events == []  # 브로커를 띄우지 않는다
    assert list(korean.iterdir()) == []  # 복사본을 만들지 않고 임시 폴더도 지운다


@pytest.mark.parametrize("exit_code", [0, 3], ids=["exit-0", "exit-3"])
def test_NFR12_broker_exit_fails(server_dir, run_cli, fake_broker, temp_root, lab, exit_code):
    fake_broker.waits = [exit_code]  # Ctrl+C 없이 브로커가 스스로 끝난다

    code, out, err = run_cli(["--dir", LAB_ARG, "broker"], cwd=server_dir)

    assert code == EXIT_FAILED and err == f"브로커가 끝났다 (종료 코드 {exit_code})\n"
    assert out == f"브로커 실행 중: {lab / 'broker.conf'}. Ctrl+C로 끝낸다.\n"
    assert fake_broker.events == ["wait", "poll"]  # 이미 끝난 브로커를 다시 끝내려 하지 않는다
    assert list(temp_root.iterdir()) == []


def test_NFR12_broker_announces_with_flush(server_dir, run_cli, fake_broker, lab, monkeypatch):
    # 출력이 파일이나 파이프로 가도 Mosquitto의 출력보다 먼저 보이게 바로 비운다
    printed = []
    monkeypatch.setattr("nearpilot.cli.broker.print", lambda *args, **kwargs: printed.append((args, kwargs)),
                        raising=False)

    run_cli(["--dir", LAB_ARG, "broker"], cwd=server_dir)

    assert printed == [((f"브로커 실행 중: {lab / 'broker.conf'}. Ctrl+C로 끝낸다.",), {"flush": True})]


def test_NFR12_broker_kills_when_terminate_times_out(server_dir, run_cli, fake_broker, temp_root, lab):
    # Ctrl+C 뒤 terminate()한 브로커가 시간 안에 끝나지 않는다
    fake_broker.waits = [KeyboardInterrupt(), subprocess.TimeoutExpired("mosquitto", 5), -9]

    code, _, err = run_cli(["--dir", LAB_ARG, "broker"], cwd=server_dir)

    assert code == EXIT_INTERRUPTED and err == "Ctrl+C로 멈췄다.\n"
    assert fake_broker.events == ["wait", "poll", "terminate", "wait:5", "kill", "wait:5"]
    assert list(temp_root.iterdir()) == []


def test_NFR12_broker_prepare_config_rewrites_only_the_two_keys(tmp_path):
    base = tmp_path / "기준 폴더"
    (base / "sub dir").mkdir(parents=True)
    (base / "sub dir" / "pw file.txt").write_text(PASSWORDS, encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    absolute = tmp_path / "elsewhere" / "rules.acl"  # 기준 폴더 밖의 절대 경로
    absolute.write_text(ACL, encoding="utf-8")
    conf = tmp_path / "broker.conf"
    conf.write_text(
        "# password_file old.txt\n"  # 주석
        "#acl_file old.acl\n"
        "listener 1883\n"
        "  \tpassword_file \t sub dir/pw file.txt  \n"  # 들여쓰기, 탭, 값 안의 공백, 끝 공백
        f"acl_file {absolute}\n"
        "password_file_extra keep\n"  # 첫 단어가 키와 다르다
        "persistence false\n", encoding="utf-8")
    temp = tmp_path / "temp"
    temp.mkdir()

    result = prepare_config(conf, base, temp)

    assert result == temp / "mosquitto.conf"
    assert result.read_text(encoding="utf-8") == (
        "# password_file old.txt\n"
        "#acl_file old.acl\n"
        "listener 1883\n"
        f"password_file {(temp / 'passwords').as_posix()}\n"
        f"acl_file {(temp / 'acl').as_posix()}\n"
        "password_file_extra keep\n"
        "persistence false\n")
    assert (temp / "passwords").read_text(encoding="utf-8") == PASSWORDS
    assert (temp / "acl").read_text(encoding="utf-8") == ACL
    assert sorted(path.name for path in temp.iterdir()) == ["acl", "mosquitto.conf", "passwords"]


def test_NFR12_broker_prepare_config_without_the_keys_copies_nothing(tmp_path):
    text = "listener 1883 127.0.0.1\nallow_anonymous true\n"
    conf = tmp_path / "broker.conf"
    conf.write_text(text, encoding="utf-8")
    temp = tmp_path / "temp"
    temp.mkdir()

    result = prepare_config(conf, tmp_path, temp)

    assert result.read_text(encoding="utf-8") == text
    assert [path.name for path in temp.iterdir()] == ["mosquitto.conf"]


def test_NFR12_broker_prepare_config_rejects_a_repeated_key(tmp_path):
    # 복사본 이름이 키마다 하나라서, 막지 않으면 둘째 줄의 파일이 첫째 줄의 파일을 덮어쓴다
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    conf = tmp_path / "broker.conf"
    conf.write_text("password_file a.txt\npassword_file b.txt\n", encoding="utf-8")
    temp = tmp_path / "temp"
    temp.mkdir()

    with pytest.raises(CliError) as error:
        prepare_config(conf, tmp_path, temp)

    assert error.value.exit_code == EXIT_INVALID and error.value.message == f"브로커 설정에 password_file 줄이 둘 이상 있다: {conf}"
    assert not (temp / "mosquitto.conf").exists()


@pytest.mark.parametrize("content", [None, b"\xb0\xa1 = cp949"], ids=["missing", "not-utf8"])
def test_NFR12_broker_prepare_config_unreadable_conf_exit_2(tmp_path, content):
    conf = tmp_path / "broker.conf"
    if content is not None:
        conf.write_bytes(content)
    temp = tmp_path / "temp"
    temp.mkdir()

    with pytest.raises(CliError) as error:
        prepare_config(conf, tmp_path, temp)

    assert error.value.exit_code == EXIT_INVALID and error.value.message == f"브로커 설정 파일을 읽을 수 없다: {conf}"
    assert list(temp.iterdir()) == []


def test_NFR12_broker_help_describes_command(server_dir, run_cli):
    code, out, err = run_cli(["--help"], cwd=server_dir)
    assert code == EXIT_OK and err == ""
    assert "MQTT 브로커(Mosquitto)를 띄운다" in " ".join(out.split())  # 도움말이 줄을 어디서 바꿔도 같게 본다

    code, out, err = run_cli(["broker", "--help"], cwd=server_dir)
    assert code == EXIT_OK and err == "" and "--dir DIR" in out and "--config PATH" in out
