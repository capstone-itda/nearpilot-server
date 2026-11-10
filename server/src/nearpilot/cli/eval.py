"""`nearpilot eval`: 공통 모델 파일을 분할 결과의 시험 회차로 평가해 보고서(CSV, Markdown)에 쓴다
(specs/core-ops-cli/spec.md 6절 "eval", specs/core-proximity-common-model/spec.md 6절 결정 11, 13).

지표 계산은 `tools/evaluation/proximity.py`가, 보고서 형식은 `tools/evaluation/report.py`가 한다. 이 모듈은 파일 인자를 풀고,
시험 회차를 읽기 전용 연결로 읽고 (NFR-13), 결과를 쓴다. 학습·검증 회차는 읽지 않는다.
장소는 분할 결과의 `place_id`다. 공통 모델 파일에는 장소가 없고, 시험 회차에서 찾은 자동 영점으로 보정한다.
이미 있는 보고서는 `--force` 없이는 덮어쓰지 않고, 그 확인은 DB를 읽기 전에 한다.
기준에 못 미쳐도 종료 코드는 0이다. 이 명령은 측정 결과를 내고, 통과 여부는 보고서가 말한다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import (
    CliError, dirs_for, is_path_argument, load_cli_config, open_read_db, require_name, resolve_input,
)
from nearpilot.core.proximity.common_model import ModelFileError, ModelMismatchError, load_model_version
from nearpilot.core.proximity.files import FitFileError, load_fit_file
from tools.evaluation.proximity import (
    AUTO_ZERO_CALIB, EvaluationError, collect_samples, compute_metrics, metric_rows,
)
from tools.evaluation.report import write_report
from tools.evaluation.runs import RunLoadError, load_labeled_runs
from tools.evaluation.split import SplitError, load_split


def now_utc() -> datetime:
    """보고서의 생성 시각과 기본 이름의 날짜. 테스트가 monkeypatch 한다."""
    return datetime.now(timezone.utc)


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "eval", parents=[parent], help="시험 회차로 공통 모델의 정면 오선택률과 ECE를 평가한다",
        description="분할 결과의 시험 회차로 공통 모델 파일을 평가해 정면 오선택률과 ECE를 보고서(CSV, Markdown)에 쓴다. "
                    "시험 회차에서 자동 영점을 찾아 보정한다. 학습·검증 회차는 읽지 않는다. 기준에 못 미쳐도 종료 코드는 0이다. "
                    "이미 있는 보고서는 --force 없이는 덮어쓰지 않는다.")
    parser.add_argument("version", metavar="VERSION",
                        help="평가할 모델 버전. 설정의 모델 폴더에서 <VERSION>.json을 읽는다")
    parser.add_argument("--split", metavar="FILE", required=True,
                        help="분할 결과 파일(JSON). 이름이면 var/splits/에서 찾고, 경로(/ 나 \\ 포함)면 실행 폴더 기준이다")
    parser.add_argument("--fit", metavar="FILE",
                        help="적합 설정 파일의 [acceptance]를 읽는다. 이름이면 설정 파일 옆 fit/ 폴더에서 찾고, "
                             "경로(/ 나 \\ 포함)면 실행 폴더 기준이다. 기본은 <분할의 장소>.toml")
    parser.add_argument("--out", metavar="NAME",
                        help="보고서 이름(확장자와 경로 구분자 없이). var/reports/<NAME>.csv, .md에 쓴다. "
                             "기본은 <분할의 장소>-<VERSION>-<UTC 날짜 YYYYMMDD>")
    parser.add_argument("--force", action="store_true", help="이미 있는 보고서를 덮어쓴다")
    parser.set_defaults(handler=_eval)


def _eval(args: argparse.Namespace) -> int:
    version = require_name(args.version, "VERSION")
    config = load_cli_config(args.config)
    dirs = dirs_for(config.source)

    try:
        model = load_model_version(config.proximity.model_root, version)
    except (ModelFileError, ModelMismatchError, OSError) as error:  # 없는 파일은 ModelMismatchError다
        raise CliError(f"모델 파일 오류: {error}", EXIT_INVALID) from None
    split_path = resolve_input(args.split, dirs.splits, kind="분할 결과")
    try:
        split = load_split(split_path)
    except SplitError as error:
        raise CliError(f"분할 결과 파일 오류: {error}", EXIT_INVALID) from None
    place = split.place_id
    fit_path = resolve_input(args.fit or f"{place}.toml", dirs.fit, kind="적합 설정")
    try:
        acceptance = load_fit_file(fit_path).acceptance
    except FitFileError as error:
        raise CliError(f"적합 설정 파일 오류: {error}", EXIT_INVALID) from None

    now = now_utc()
    name = args.out or f"{place}-{version}-{now:%Y%m%d}"
    if is_path_argument(name):
        raise CliError(f"--out은 경로가 아니라 이름이다. /나 \\ 없이 쓴다: {name}", EXIT_INVALID)
    csv_path, md_path = dirs.reports / f"{name}.csv", dirs.reports / f"{name}.md"
    if not args.force and _existing(csv_path, md_path):
        raise _report_exists(csv_path, md_path)

    conn = open_read_db(config)
    try:
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        runs = load_labeled_runs(conn, split.test)  # 시험 회차만 읽는다
    except RunLoadError as error:
        raise CliError(str(error), EXIT_FAILED) from None
    finally:
        conn.close()
    try:
        metrics = compute_metrics(collect_samples(model, runs, bin_sec=config.settings.rssi_frame_sec))
    except EvaluationError as error:
        raise CliError(f"평가 실패: {error}", EXIT_FAILED) from None
    # 무엇을 평가했는지 보고서에 남긴다 (requirements.md §4 "입력, 시나리오, 버전, 표본 수를 보존한다")
    provenance = {"place_id": place, "model_version": model.model_version, "calib_version": AUTO_ZERO_CALIB,
                  "split_file": str(split_path), "fit_file": str(fit_path)}
    rows = [{**row, **provenance} for row in metric_rows(metrics, acceptance)]

    try:
        dirs.reports.mkdir(parents=True, exist_ok=True)
    except OSError as error:  # 폴더 자리에 파일이 있으면 FileExistsError다
        raise CliError(f"보고서 폴더를 만들 수 없다: {dirs.reports} ({error.strerror or error})", EXIT_INVALID) from error
    try:
        write_report(
            rows, db_path=str(config.database.path), schema_version=schema_version, sample_count=metrics.ece_samples,
            generated_at=now, csv_path=str(csv_path), md_path=str(md_path), force=args.force)
    except FileExistsError:  # 위의 확인 뒤에 다른 곳에서 만들었다
        raise _report_exists(csv_path, md_path) from None
    except OSError as error:
        raise CliError(f"보고서를 쓸 수 없다: {csv_path} ({error.strerror or error})", EXIT_INVALID) from error

    _print_table(rows)
    print(f"작성: {csv_path}")
    print(f"작성: {md_path}")
    return EXIT_OK


def _existing(*paths: Path) -> list[Path]:
    return [path for path in paths if path.exists()]


def _report_exists(csv_path: Path, md_path: Path) -> CliError:
    listed = ", ".join(str(path) for path in _existing(csv_path, md_path))
    return CliError(f"보고서가 이미 있다: {listed}. --force로 덮어쓴다", EXIT_FAILED)


def _print_table(rows: list[dict[str, Any]]) -> None:
    """행마다 지표, 값, 기준, 통과 여부. 값은 소수 넷째 자리까지 쓴다. 측정하지 않은 칸은 `-`이고, 통과 칸에는 그 사유(`note`)를 쓴다."""
    print("  ".join(["지표", "값", "기준", "통과"]))
    for row in rows:
        cells = ["-" if row[key] == "" else f"{row[key]:.4f}" for key in ("value", "limit")]
        passed = row["passed"]
        verdict = ("통과" if passed else "미달") if isinstance(passed, bool) else row["note"]
        print("  ".join([row["metric"], *cells, verdict]))
