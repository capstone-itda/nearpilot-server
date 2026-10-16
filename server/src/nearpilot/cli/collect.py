"""`nearpilot collect`: 수집 회차를 실행하고(`run`), 닫고(`close`), 조회한다(`list`)
(specs/core-ops-cli/spec.md 6절 "collect").

`run`과 `close`는 기존 `tools/data_collection/cli.main`을 그대로 부른다. 수집 도구 코드는 바꾸지 않고
설정의 DB로 만든 `SqliteAdminRepository`와 기본 기록 파일 경로만 넘긴다.
`list`는 `tools/evaluation/db.open_readonly`로 읽기 전용 연결을 열어 회차를 읽는다 (NFR-13).
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from nearpilot.cli import EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import (
    CliError,
    dirs_for,
    load_cli_config,
    open_read_db,
    open_write_db,
    require_db_file,
    resolve_input,
    resolve_output,
)
from nearpilot.core.proximity.files import FitFileError, load_fit_file
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.runtime import ServerConfig
from nearpilot.shared.interfaces import AdminRepository
from tools.data_collection.cli import main as collection_main
from tools.data_collection.session import SessionIO
from tools.evaluation.runs import RunInfo, list_runs

# None이면 수집 도구가 실제 시계, 대기, 표준 입출력을 쓴다. 테스트는 가짜 `SessionIO`로 바꿔서 입력과 시간을 기다리지 않는다.
_session_io: SessionIO | None = None

_LIST_HEADER = "회차 ID  라벨  장소  시작(UTC)  길이  프레임  상태"


def today_utc() -> date:
    """기본 기록 파일 이름의 날짜. 테스트가 monkeypatch 한다."""
    return datetime.now(timezone.utc).date()


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    collect = subparsers.add_parser(
        "collect", parents=[parent], help="수집 회차를 실행하고, 닫고, 조회한다",
        description="근접 모델을 적합할 RSSI 수집 회차를 다룬다. 수집하는 동안 서버는 `serve --collect-only`로 켜 둔다.")
    commands = collect.add_subparsers(dest="collect_command", required=True, metavar="{run,close,list}")

    run = commands.add_parser(
        "run", parents=[parent], help="계획 파일의 회차를 차례로 실행한다",
        description="수집 계획 파일의 회차를 차례로 실행한다. 회차마다 Enter를 눌러 시작하고, q를 입력하면 끝낸다.")
    run.add_argument("file", metavar="FILE",
                     help="수집 계획 파일 이름(설정 파일 옆 plans/ 폴더에서 찾는다) 또는 경로(/ 나 \\ 포함, 실행 폴더 기준)")
    run.add_argument("--record", metavar="FILE",
                     help="회차 기록 파일(JSONL, 덧붙여 쓴다). 이름이면 var/records/에, 경로면 실행 폴더 기준으로 쓴다. "
                          "기본은 var/records/<계획 파일 이름>-<UTC 날짜>.jsonl")
    run.add_argument("--start-at", metavar="N", type=int, default=1, help="N번째 회차부터 실행한다 (1부터 센다)")
    run.set_defaults(handler=_run)

    close = commands.add_parser(
        "close", parents=[parent], help="끝 시각을 기록하지 못한 회차를 지금 시각으로 닫는다",
        description="진행 중인 회차를 지금 시각으로 닫는다. 닫을 회차 ID는 `collect list --open`으로 찾는다.")
    close.add_argument("--collection-id", metavar="ID", required=True, help="닫을 회차 ID")
    close.set_defaults(handler=_close)

    list_parser = commands.add_parser(
        "list", parents=[parent], help="수집 회차를 조회한다",
        description="DB의 수집 회차를 읽기 전용으로 시작 시각 순으로 보여 준다.")
    list_parser.add_argument("--place", metavar="P", help="이 장소의 회차만 본다")
    list_parser.add_argument("--label", metavar="L", help="이 라벨의 회차만 본다 (기기 ID 또는 none)")
    list_parser.add_argument("--open", action="store_true", help="진행 중인(끝 시각이 없는) 회차만 본다")
    list_parser.add_argument("--summary", action="store_true",
                             help="라벨별로 닫힌 회차 수와 프레임 수를 본다. --place가 있으면 분할 가능 여부도 본다")
    list_parser.add_argument("--fit", metavar="FILE",
                             help="--summary와 --place를 함께 쓸 때 읽는 적합 설정 파일. "
                                  "기본은 <장소>.toml(설정 파일 옆 fit/ 폴더). 이름이거나 경로(/ 나 \\ 포함, 실행 폴더 기준)")
    list_parser.set_defaults(handler=_list)


# ── run, close ───────────────────────────────────────────────────────


def _run(args: argparse.Namespace) -> int:
    config = load_cli_config(args.config)
    dirs = dirs_for(config.source)
    plan = resolve_input(args.file, dirs.plans, kind="수집 계획")
    require_db_file(config)  # 빈 DB를 새로 만들거나 기록 폴더를 만들기 전에 막는다
    record = resolve_output(args.record, dirs.records, f"{plan.stem}-{today_utc():%Y%m%d}.jsonl")
    try:
        record.parent.mkdir(parents=True, exist_ok=True)  # 수집 도구는 기록 파일을 여는 일만 한다
    except OSError as error:
        raise CliError(f"기록 파일의 폴더를 만들 수 없다: {record.parent} ({error.strerror or error})") from error
    print(f"기록 파일: {record}")
    return _call_collection_tool(
        config, ["run", str(plan), "--record", str(record), "--start-at", str(args.start_at)])


def _close(args: argparse.Namespace) -> int:
    config = load_cli_config(args.config)
    require_db_file(config)
    return _call_collection_tool(config, ["close", "--collection-id", args.collection_id])


def _call_collection_tool(config: ServerConfig, argv: list[str]) -> int:
    """기존 수집 도구의 `main`을 부르고 종료 코드를 그대로 돌려준다.

    저장소는 도구가 계획 파일과 `--start-at`을 검사한 뒤에야 만든다. 그때 연 DB 연결은 끝나면 닫는다.
    """
    connections: list[sqlite3.Connection] = []

    def repository_factory() -> AdminRepository:
        conn = open_write_db(config)
        connections.append(conn)
        return SqliteAdminRepository(conn)

    try:
        return collection_main(argv, repository_factory=repository_factory, io=_session_io)
    finally:
        for conn in connections:
            conn.close()


# ── list ─────────────────────────────────────────────────────────────


def _list(args: argparse.Namespace) -> int:
    config = load_cli_config(args.config)
    runs = [run for run in _read_runs(config) if _selected(run, args)]
    if not runs:
        print("수집 회차가 없다.")
        return EXIT_OK
    if args.summary:
        _print_summary(runs, args.place, args.fit, dirs_for(config.source).fit)
    else:
        print(_LIST_HEADER)
        for run in runs:
            print(_run_line(run))
    return EXIT_OK


def _read_runs(config: ServerConfig) -> list[RunInfo]:
    """DB의 모든 회차를 읽기 전용 연결로 읽는다."""
    conn = open_read_db(config)
    try:
        return list_runs(conn)
    finally:
        conn.close()


def _selected(run: RunInfo, args: argparse.Namespace) -> bool:
    return (
        (args.place is None or run.place_id == args.place)
        and (args.label is None or run.label == args.label)
        and (not args.open or run.ended_at is None)
    )


def _run_line(run: RunInfo) -> str:
    """목록의 회차 한 줄. 진행 중이면 닫는 명령을 끝에 붙인다."""
    started = run.started_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    length = "-" if run.ended_at is None else f"{(run.ended_at - run.started_at).total_seconds():.1f}초"
    if run.ended_at is None:
        status = f"진행 중 (닫기: nearpilot collect close --collection-id {run.collection_id})"
    elif run.place_problem is not None:
        status = f"제외: {run.place_problem}"
    else:
        status = "닫힘"
    place = "-" if run.place_id is None else run.place_id
    return "  ".join([run.collection_id, run.label, place, started, length, str(run.frame_count), status])


def _print_summary(runs: list[RunInfo], place: str | None, fit_arg: str | None, fit_dir: Path) -> None:
    """라벨별 닫힌 회차 수와 프레임 합을 쓴다. 닫힌 회차는 끝이 있고, 프레임이 1개 이상이고, 장소가 있는 회차다.

    장소를 골랐으면 그 장소의 적합 설정으로 라벨마다 분할 가능 여부(닫힌 회차 수 >= `min_runs_per_label`)를 덧붙인다.
    """
    min_runs = None
    fit_path = None
    if place is not None:
        # 직접 준 --fit은 다른 명령처럼 없으면 입력 오류다. 기본 파일만 없어도 된다.
        fit_path = (resolve_input(fit_arg, fit_dir, kind="적합 설정") if fit_arg is not None
                    else resolve_output(None, fit_dir, f"{place}.toml"))
        if fit_path.is_file():
            try:
                min_runs = load_fit_file(fit_path).split.min_runs_per_label
            except FitFileError as error:
                raise CliError(f"적합 설정 파일 오류: {error}", EXIT_INVALID) from None

    closed = [run for run in runs if run.ended_at is not None and run.frame_count >= 1 and run.place_id is not None]
    columns = ["라벨", "닫힌 회차", "프레임"] + (["분할 가능"] if place is not None else [])
    print("  ".join(columns))
    for label in sorted({run.label for run in runs}):
        counted = [run for run in closed if run.label == label]
        row = [label, str(len(counted)), str(sum(run.frame_count for run in counted))]
        if place is not None:
            row.append(_splittable(len(counted), min_runs))
        print("  ".join(row))
    if place is not None and min_runs is None:
        print(f"적합 설정 파일이 없어 분할 가능 여부를 보지 않았다: {fit_path}")


def _splittable(closed_count: int, min_runs: int | None) -> str:
    if min_runs is None:
        return "-"
    return "예" if closed_count >= min_runs else "아니오"
