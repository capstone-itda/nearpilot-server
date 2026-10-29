"""`nearpilot broker`: MQTT 브로커(Mosquitto)를 띄운다 (specs/core-ops-cli/spec.md 6절 "시험 폴더와 실행 보조 명령").

`--dir`이 있으면 `DIR/broker.conf`(`tools.node_lab init`의 형식)로, 없으면 설정 폴더의 `mosquitto.conf`로 띄운다.
설정 안의 상대 경로는 `broker_files`가 정한 기준 폴더에서 푼다. 브로커 설정 파일은 고치지 않는다.
`password_file`·`acl_file`이 가리키는 파일은 임시 ASCII 폴더에 복사하고, 그 경로로 바꾼 설정을 임시 폴더에 써서
`mosquitto -c`에 넘긴다 (Windows 비ASCII 경로를 Mosquitto가 열지 못한다, AGENTS.md Gotchas). 끝나면 임시 폴더를 지운다.
출력은 터미널에 그대로 둔다. `[mqtt].host`는 검사하지 않는다 — 브로커의 주소는 브로커 설정의 `listener`가 정한다.
시스템 임시 폴더 경로가 ASCII가 아니면 띄우지 않는다.
종료 코드: Ctrl+C 130(Mosquitto를 끝낸 뒤), Mosquitto가 스스로 끝나면 1, 브로커 설정·가리킨 파일·`mosquitto` 실행 파일이 없거나 임시 폴더 경로가 ASCII가 아니면 2.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID
from nearpilot.cli.common import CliError, load_cli_config
from nearpilot.runtime import ServerConfig
from tools.node_lab.setup import executable

_COPIES = {"password_file": "passwords", "acl_file": "acl"}  # 설정 키 → 임시 폴더에 두는 복사본 이름
_STOP_TIMEOUT_SEC = 5  # 브로커를 끝낼 때 terminate, kill 뒤에 각각 기다리는 시간. 서버의 운영 수치가 아니라 이 명령의 한계다


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "broker", parents=[parent], help="MQTT 브로커(Mosquitto)를 띄운다",
        description="설정에 맞는 Mosquitto를 띄우고 Ctrl+C까지 기다린다. "
                    "--dir이 있으면 DIR/broker.conf, 없으면 설정 폴더의 mosquitto.conf를 쓴다.")
    parser.set_defaults(handler=_broker)


def _broker(args: argparse.Namespace) -> int:
    config = load_cli_config(args.config)
    conf, base = broker_files(config, getattr(args, "dir", None))
    if not conf.is_file():
        raise CliError(f"브로커 설정 파일이 없다: {conf}", EXIT_INVALID)
    try:
        program = executable("mosquitto")
    except ValueError as error:  # PATH와 Windows 기본 설치 폴더에 없다
        raise CliError(str(error), EXIT_INVALID) from None
    with tempfile.TemporaryDirectory(prefix="np-broker-") as temp:
        if not _ascii(temp):  # 계정 이름이 한글이면 시스템 임시 폴더도 한글이다. Mosquitto에 넘길 경로 그대로 본다
            raise CliError(f"임시 폴더 경로에 영문이 아닌 문자가 있다: {temp}. Mosquitto가 열지 못한다. "
                           "환경 변수 TEMP(macOS·Linux는 TMPDIR)를 영문 경로로 바꾼다", EXIT_INVALID)
        rewritten = prepare_config(conf, base, Path(temp))
        process = subprocess.Popen([program, "-c", str(rewritten)], cwd=base)
        try:
            print(f"브로커 실행 중: {conf}. Ctrl+C로 끝낸다.", flush=True)
            code = process.wait()
        finally:  # Ctrl+C로 나갈 때도 브로커를 끝낸 뒤에 임시 폴더를 지운다. KeyboardInterrupt는 main이 130으로 바꾼다
            _stop(process)
    print(f"브로커가 끝났다 (종료 코드 {code})", file=sys.stderr)
    return EXIT_FAILED


def _ascii(path: str) -> bool:
    return path.isascii()


def broker_files(config: ServerConfig, folder: str | None) -> tuple[Path, Path]:
    """(브로커 설정 파일, 상대 경로 기준 폴더). `folder`는 `args.dir`이다.

    `--dir`이 있으면 `DIR/broker.conf`이고 `DIR`이 기준이다. 없으면 설정 폴더의 `mosquitto.conf`이고
    설정 폴더의 부모(`server/`)가 기준이다 — `server/`에서 `mosquitto -c config/mosquitto.conf`로 띄우던 것과 같다.
    """
    if folder is not None:
        base = Path(folder).resolve()
        return base / "broker.conf", base
    config_dir = config.source.parent
    return config_dir / "mosquitto.conf", config_dir.parent


def prepare_config(conf: Path, base: Path, temp: Path) -> Path:
    """`password_file`·`acl_file`이 가리키는 파일을 `temp`에 복사하고, 그 경로로 바꾼 설정을 `temp`에 쓴다.

    줄마다 앞뒤 공백을 뺀 첫 단어가 `password_file`이나 `acl_file`인 줄만 바꾼다. 주석과 다른 줄은 그대로 둔다.
    값은 첫 단어 뒤의 나머지이고, 절대 경로가 아니면 `base` 기준이다. 쓴 설정 파일의 경로를 돌려준다.
    설정 파일을 읽을 수 없거나 가리킨 파일이 없으면 `CliError`(종료 코드 2)다.
    """
    try:
        lines = conf.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as error:
        raise CliError(f"브로커 설정 파일을 읽을 수 없다: {conf}", EXIT_INVALID) from error
    seen: set[str] = set()
    for index, line in enumerate(lines):
        words = line.strip().split(None, 1)
        key = words[0] if words else ""
        if key not in _COPIES:
            continue
        if key in seen:  # 복사본 이름이 키마다 하나라서 둘째 줄의 파일이 첫째 줄의 파일을 덮어쓴다
            raise CliError(f"브로커 설정에 {key} 줄이 둘 이상 있다: {conf}", EXIT_INVALID)
        seen.add(key)
        source = base / (words[1] if len(words) > 1 else "")  # 값이 절대 경로면 base는 쓰이지 않는다
        if not source.is_file():
            raise CliError(f"브로커 설정이 가리키는 파일이 없다: {source}", EXIT_INVALID)
        copy = temp / _COPIES[key]
        try:
            shutil.copyfile(source, copy)
        except OSError as error:
            raise CliError(
                f"브로커 설정이 가리키는 파일을 복사할 수 없다: {source} ({error.strerror or error})",
                EXIT_INVALID) from error
        lines[index] = f"{key} {copy.as_posix()}"  # 따옴표를 붙이지 않는다 (AGENTS.md Gotchas)
    rewritten = temp / "mosquitto.conf"
    rewritten.write_text("\n".join(lines), encoding="utf-8")
    return rewritten


def _stop(process: subprocess.Popen) -> None:
    """브로커가 아직 돌면 끝낸다. `terminate()` 뒤 시간 안에 끝나지 않으면 `kill()`한다."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_STOP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_STOP_TIMEOUT_SEC)
