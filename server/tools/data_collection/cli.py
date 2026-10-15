"""수집 도구 CLI (specs/core-proximity-data-collection/spec.md 6절 "도구").

`run`은 계획 파일을 검사한 뒤 `run_session`으로 회차를 실행하고, `close`는 끝 시각을 기록하지 못한 회차를 닫는다.
`AdminRepository`를 만드는 함수는 `main`의 인자로 받는다. DB 경로와 연결을 만드는 방법이 미결이라 `python -m` 진입점은 만들지 않는다 (#88).
수집 회차는 `AdminRepository`로만 기록하고 SQL을 쓰지 않는다 (#131).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from nearpilot.shared.enums import AdminWriteOutcome
from nearpilot.shared.interfaces import AdminRepository
from tools.data_collection.plan import PlanError, load_plan
from tools.data_collection.session import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    EXIT_OK,
    SessionIO,
    run_session,
)


def build_parser() -> argparse.ArgumentParser:
    """`run PLAN --record PATH [--start-at N]`과 `close --collection-id ID` 두 하위 명령을 가진 파서."""
    parser = argparse.ArgumentParser(description="근접 수집 회차를 실행하고 닫는다.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="계획 파일의 회차를 차례로 실행한다")
    run.add_argument("plan", metavar="PLAN", type=Path, help="수집 계획 파일 (JSON)")
    run.add_argument("--record", metavar="PATH", type=Path, required=True, help="회차 기록 파일 (JSONL). 덧붙여 쓴다")
    run.add_argument("--start-at", metavar="N", type=int, default=1, help="N번째 회차부터 실행한다 (1부터 센다)")

    close = commands.add_parser("close", help="끝 시각을 기록하지 못한 회차를 현재 시각으로 닫는다")
    close.add_argument("--collection-id", metavar="ID", required=True, help="닫을 회차 ID")
    return parser


def default_io() -> SessionIO:
    """실제 시계, 대기, 표준 입력, 표준 출력. 회차 ID는 `SessionIO`의 기본 생성 함수가 낸다."""
    return SessionIO(
        clock=lambda: datetime.now(timezone.utc),
        sleep=time.sleep,
        read_line=input,
        write=print,
    )


def main(
    argv: Sequence[str] | None,
    *,
    repository_factory: Callable[[], AdminRepository],
    io: SessionIO | None = None,
) -> int:
    """명령줄을 읽어 `run` 또는 `close`를 실행하고 종료 코드를 돌려준다. `sys.exit`를 부르지 않는다.

    `argv`가 None이면 `sys.argv[1:]`를 읽고, `io`가 None이면 `default_io()`를 쓴다.
    인자가 틀리면 argparse가 사용법을 쓰고 2를, `--help`면 0을 돌려준다.
    """
    if io is None:
        io = default_io()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_request:  # argparse가 --help에는 0으로, 인자 오류에는 2로 끝낸다
        return exit_request.code
    try:
        if args.command == "run":
            return _run(args, repository_factory, io)
        return _close(args, repository_factory, io)
    except KeyboardInterrupt:
        # run_session이 잡지 않는 코드 몇 줄 사이의 Ctrl+C도 트레이스백 없이 종료 코드로 알린다
        io.write("Ctrl+C로 멈췄다.")
        return EXIT_INTERRUPTED


def _make_repository(repository_factory: Callable[[], AdminRepository], io: SessionIO) -> AdminRepository | None:
    """`AdminRepository`를 만든다. 만들지 못하면 사유를 쓰고 None을 돌려준다."""
    try:
        return repository_factory()
    except Exception as error:
        io.write(f"AdminRepository를 만들다가 예외가 났다 ({error!r}).")
        return None


def _run(args: argparse.Namespace, repository_factory: Callable[[], AdminRepository], io: SessionIO) -> int:
    """계획 파일을 모두 검사한 뒤에 저장소를 만들고 회차를 실행한다. 계획 파일이 틀리면 저장소를 만들지 않는다."""
    try:
        plan = load_plan(args.plan)
    except PlanError as error:
        io.write(f"계획 파일이 올바르지 않다: {error}")
        return EXIT_INVALID
    repository = _make_repository(repository_factory, io)
    if repository is None:
        return EXIT_FAILED
    return run_session(plan, repository, args.record, io, start_at=args.start_at)


def _close(args: argparse.Namespace, repository_factory: Callable[[], AdminRepository], io: SessionIO) -> int:
    """회차 하나의 끝 시각을 현재 시각으로 기록한다. 기록 파일은 고치지 않는다 (`end_failed` 줄이 그대로 남는다)."""
    repository = _make_repository(repository_factory, io)
    if repository is None:
        return EXIT_FAILED
    collection_id = args.collection_id
    ended_at = io.clock()
    try:
        result = repository.end_collection(collection_id, ended_at)
    except Exception as error:
        io.write(f"회차를 닫다가 예외가 났다 ({error!r}). 회차 ID: {collection_id}")
        return EXIT_FAILED
    if result.outcome != AdminWriteOutcome.APPLIED:
        io.write(f"DB가 회차 닫기를 거부했다 ({result.reason}). 회차 ID: {collection_id}")
        return EXIT_FAILED
    io.write(f"회차를 닫았다 ({collection_id}).")
    return EXIT_OK
