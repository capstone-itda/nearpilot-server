"""운영 명령의 공용 도우미: 파일 인자 해석, 기본 폴더, 설정 읽기 (specs/core-ops-cli/spec.md 6절 "명령과 파일").

파일 인자 규칙: 인자에 `/`나 `\\`가 없으면 확장자를 포함한 파일명이고 명령별 기본 폴더에서 찾는다.
기본 폴더는 설정 파일이 있는 폴더 기준이다. 구분자가 있으면 실행 폴더 기준 경로다.
`config/`는 사람이 쓰고 `var/`는 명령이 만든다. 이 모듈은 폴더를 만들지 않는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from nearpilot.cli import EXIT_INVALID
from nearpilot.db.schema import connect
from nearpilot.runtime import ConfigError, ServerConfig, load_config
from tools.evaluation.db import SchemaVersionMismatch, open_readonly


class CliError(Exception):
    """명령을 끝낼 사유. `main`이 `message`를 stderr에 쓰고 `exit_code`로 끝낸다."""

    def __init__(self, message: str, exit_code: int = EXIT_INVALID) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class Dirs:
    """명령별 기본 폴더. 모두 절대 경로다."""

    config_dir: Path  # 설정 파일이 있는 폴더
    seed: Path
    plans: Path
    layouts: Path
    fit: Path
    splits: Path
    records: Path
    reports: Path


def dirs_for(config_path: Path) -> Dirs:
    """설정 파일 위치에서 기본 폴더를 정한다. `var/`는 설정 파일 폴더의 부모 기준이다."""
    config_dir = config_path.resolve().parent
    var = config_dir.parent / "var"
    return Dirs(
        config_dir=config_dir,
        seed=config_dir / "seed",
        plans=config_dir / "plans",
        layouts=config_dir / "layouts",
        fit=config_dir / "fit",
        splits=var / "splits",
        records=var / "records",
        reports=var / "reports",
    )


def is_path_argument(arg: str) -> bool:
    """`/`나 `\\`가 있으면 경로다. 시연 노트북이 Windows라서 둘 다 본다."""
    return "/" in arg or "\\" in arg


def resolve_input(arg: str, default_dir: Path, *, kind: str) -> Path:
    """읽을 파일 인자를 절대 경로로 바꾼다. 파일이 없으면 `CliError`(종료 코드 2)다."""
    if is_path_argument(arg):
        path = Path(arg).resolve()
        if not path.is_file():
            raise CliError(f"{kind} 파일이 없다: {path}", EXIT_INVALID)
        return path
    path = default_dir / arg
    if not path.is_file():
        names = ", ".join(_file_names(default_dir)) or "(없음)"
        raise CliError(f"{kind} 파일이 없다: {path}. {default_dir}에 있는 파일: {names}", EXIT_INVALID)
    return path


def resolve_output(arg: str | None, default_dir: Path, default_name: str) -> Path:
    """쓸 파일 인자를 절대 경로로 바꾼다. 이미 있는지와 폴더를 만드는 일은 호출자가 한다."""
    if arg is None:
        return default_dir / default_name
    if is_path_argument(arg):
        return Path(arg).resolve()
    return default_dir / arg


def load_cli_config(path: str, *, require_models: bool = False) -> ServerConfig:
    """설정 파일을 읽는다. 잘못되면 `CliError`(종료 코드 2)다. DB를 열지 않는다."""
    try:
        return load_config(path, require_models=require_models)
    except ConfigError as error:
        raise CliError(f"설정 오류: {error}", EXIT_INVALID) from None


def open_write_db(config: ServerConfig) -> sqlite3.Connection:
    """설정의 DB를 쓰기 연결로 연다. DB 파일의 부모 폴더가 없으면 만든다."""
    config.database.path.parent.mkdir(parents=True, exist_ok=True)
    return connect(str(config.database.path), busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms)


def require_db_file(config: ServerConfig) -> Path:
    """설정의 DB 파일 경로를 돌려준다. 파일이 아니면 `CliError`(종료 코드 2)다.

    DB는 `serve`가 처음 시작할 때 만든다. 오타 난 `--config`가 가리키는 곳에 빈 DB를 새로 만들지 않도록
    DB를 여는 명령은 먼저 이 검사를 한다.
    """
    path = config.database.path
    if not path.is_file():
        raise CliError(
            f"DB 파일이 없다: {path}. 먼저 nearpilot serve --collect-only로 서버를 한 번 시작한다", EXIT_INVALID)
    return path


def open_read_db(config: ServerConfig) -> sqlite3.Connection:
    """설정의 DB를 읽기 전용 연결로 연다 (NFR-13). 파일이 없거나 스키마 버전이 다르면 `CliError`(종료 코드 2)다."""
    path = require_db_file(config)
    try:
        return open_readonly(_uri_path(path))
    except SchemaVersionMismatch as error:
        raise CliError(str(error), EXIT_INVALID) from None


def _uri_path(path: Path) -> str:
    """읽기 전용 URI(`file:<경로>?mode=ro`)에 넣을 경로.

    폴더 이름의 `?`, `#`, `%`와 공백이 URI 문법으로 읽히지 않게 퍼센트 인코딩한다.
    Windows 드라이브 문자(`C:/…`)는 앞에 `/`를 붙여 `file:/C:/…`가 되게 한다.
    """
    posix = path.resolve().as_posix()
    if posix[1:3] == ":/":
        posix = "/" + posix
    return quote(posix, safe="/:")


def _file_names(folder: Path) -> list[str]:
    """폴더의 파일 이름을 정렬해서 돌려준다. 숨김 파일은 뺀다. 폴더가 없으면 빈 목록이다."""
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
