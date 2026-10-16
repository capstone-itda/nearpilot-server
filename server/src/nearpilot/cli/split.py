"""`nearpilot split`: 장소의 수집 회차를 회차 단위로 학습·검증·시험에 나눠 분할 결과 파일에 쓴다
(specs/core-ops-cli/spec.md 6절 "split").

분할 로직은 `tools/evaluation/split.py`에 있다. 이 모듈은 파일 인자를 풀고, DB를 읽기 전용 연결로 읽고 (NFR-13),
결과를 쓴다. 이미 있는 결과 파일은 덮어쓰지 않고, 그 확인은 DB를 읽기 전에 한다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import (
    CliError, dirs_for, load_cli_config, open_read_db, require_name, resolve_input, resolve_output,
)
from nearpilot.core.proximity.files import FitFileError, load_fit_file
from tools.evaluation.runs import list_runs
from tools.evaluation.split import SplitError, split_runs, split_to_json


def now_utc() -> datetime:
    """분할 결과 파일의 작성 시각. 테스트가 monkeypatch 한다."""
    return datetime.now(timezone.utc)


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "split", parents=[parent], help="수집 회차를 학습·검증·시험으로 나눈다",
        description="장소의 닫힌 수집 회차를 회차 단위로 학습·검증·시험에 나눠 분할 결과 파일에 쓴다. "
                    "같은 회차의 프레임은 한 분할에만 들어간다. 이미 있는 결과 파일은 덮어쓰지 않는다.")
    parser.add_argument("place", metavar="PLACE", help="나눌 장소 ID")
    parser.add_argument("--seed", metavar="N", type=int, default=42,
                        help="섞는 시드 (기본 %(default)s). 같은 시드는 같은 분할을 만든다")
    parser.add_argument("--fit", metavar="FILE",
                        help="적합 설정 파일의 [split]을 읽는다. 이름이면 설정 파일 옆 fit/ 폴더에서 찾고, "
                             "경로(/ 나 \\ 포함)면 실행 폴더 기준이다. 기본은 <PLACE>.toml")
    parser.add_argument("--out", metavar="FILE",
                        help="분할 결과 파일(JSON). 이름이면 var/splits/에, 경로면 실행 폴더 기준으로 쓴다. "
                             "기본은 var/splits/<PLACE>-seed<N>.json")
    parser.set_defaults(handler=_split)


def _split(args: argparse.Namespace) -> int:
    require_name(args.place, "장소")
    config = load_cli_config(args.config)
    dirs = dirs_for(config.source)
    fit_path = resolve_input(args.fit or f"{args.place}.toml", dirs.fit, kind="적합 설정")
    try:
        settings = load_fit_file(fit_path).split
    except FitFileError as error:
        raise CliError(f"적합 설정 파일 오류: {error}", EXIT_INVALID) from None
    out = resolve_output(args.out, dirs.splits, f"{args.place}-seed{args.seed}.json")
    if out.exists():
        raise CliError(f"분할 결과가 이미 있다: {out}. 다른 --seed나 --out을 쓴다", EXIT_FAILED)

    conn = open_read_db(config)
    try:
        runs = list_runs(conn)
    finally:
        conn.close()
    try:
        result = split_runs(runs, args.place, settings, args.seed)
    except SplitError as error:
        raise CliError(str(error), EXIT_FAILED) from None

    text = split_to_json(result, created_at=now_utc(), db_path=str(config.database.path))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as error:
        raise CliError(f"분할 결과를 쓸 수 없다: {out} ({error.strerror or error})", EXIT_INVALID) from error

    print("  ".join(["라벨", "학습", "검증", "시험"]))
    for label, counts in result.labels.items():
        print("  ".join([label, *map(str, counts)]))
    if result.excluded:
        print("제외 회차  사유")
        for collection_id, reason in result.excluded:
            print(f"{collection_id}  {reason}")
    print(f"작성: {out}")
    return EXIT_OK
