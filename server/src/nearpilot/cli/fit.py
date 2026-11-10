"""`nearpilot fit`: 분할 결과의 학습·검증 회차로 공통 근접 모델을 적합해 모델 파일에 쓴다
(specs/core-ops-cli/spec.md 6절 "fit", specs/core-proximity-common-model/spec.md 6절 결정 13).

적합 계산은 `core/proximity/common_fit.py`의 `fit_common_model`이 한다. 이 모듈은 파일 인자를 풀고, 학습·검증 회차를
읽기 전용 연결로 읽고 (NFR-13), 결과를 쓴다. `--split`은 장소마다 하나씩 여러 번 받고, 모델 파일 하나를 모든 장소가 같이 쓴다.
시험 회차는 읽지 않는다. 같은 버전의 모델 파일은 덮어쓰지 않고 (#40), 그 확인은 DB를 읽기 전에 한다.
`--dry-run`은 모델 파일도 모델 폴더도 만들지 않는다.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError, dirs_for, load_cli_config, open_read_db, require_name, resolve_input
from nearpilot.core.proximity.common_fit import (
    CommonFitConfig, CommonFitResult, FitError, PlaceRuns, fit_common_model, place_anchors,
)
from nearpilot.core.proximity.common_model import write_common_model_file
from nearpilot.core.proximity.files import FitFileError, load_fit_file
from tools.evaluation.runs import RunLoadError, load_labeled_runs
from tools.evaluation.split import SplitError, SplitResult, load_split

GRID_ROWS = 10  # 조합별 손실 표에 쓰는 줄 수. 손실이 작은 순서다
DEFAULT_FIT_FILE = "common.toml"  # `--fit`을 주지 않을 때 읽는 적합 설정. 공통 모델은 장소 이름이 없다


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "fit", parents=[parent], help="수집 회차로 공통 근접 모델을 적합해 모델 파일에 쓴다",
        description="분할 결과의 학습 회차로 역할(기기 앞, 다른 기기 앞, 어느 기기 앞도 아님)별 RSSI 값을 적합하고, "
                    "검증 회차로 τ·stay·window_sec 조합을 골라 모델 파일에 쓴다. 장소마다 분할 결과를 --split으로 하나씩 준다. "
                    "시험 회차는 읽지 않는다. 같은 버전의 모델 파일은 덮어쓰지 않는다.")
    parser.add_argument("--split", metavar="FILE", action="append", required=True,
                        help="분할 결과 파일(JSON). 장소마다 하나씩 여러 번 준다. 이름이면 var/splits/에서 찾고, "
                             "경로(/ 나 \\ 포함)면 실행 폴더 기준이다. 같은 장소의 분할 결과를 둘 줄 수 없다")
    parser.add_argument("--version", metavar="NAME", required=True,
                        help="모델 버전. 모델 파일 이름이 된다. 같은 버전의 파일이 있으면 덮어쓰지 않는다")
    parser.add_argument("--fit", metavar="FILE",
                        help="적합 설정 파일의 [fit]을 읽는다. 이름이면 설정 파일 옆 fit/ 폴더에서 찾고, "
                             f"경로(/ 나 \\ 포함)면 실행 폴더 기준이다. 기본은 {DEFAULT_FIT_FILE}")
    parser.add_argument("--dry-run", action="store_true",
                        help="역할별 값, 장소별 자동 영점, 조합별 손실만 쓰고 모델 파일은 쓰지 않는다")
    parser.set_defaults(handler=_fit)


def _fit(args: argparse.Namespace) -> int:
    require_name(args.version, "--version")
    config = load_cli_config(args.config)
    dirs = dirs_for(config.source)

    model_path = config.proximity.model_root / f"{args.version}.json"
    if not args.dry_run and model_path.exists():
        raise _model_exists(model_path)

    splits: list[SplitResult] = []
    places: dict[str, Path] = {}  # 장소 → 그 장소의 분할 결과 파일
    for arg in args.split:
        split_path = resolve_input(arg, dirs.splits, kind="분할 결과")
        try:
            split = load_split(split_path)
        except SplitError as error:
            raise CliError(f"분할 결과 파일 오류: {error}", EXIT_INVALID) from None
        if split.place_id in places:
            raise CliError(
                f"같은 장소의 분할 결과가 둘이다: {split.place_id} ({places[split.place_id]}, {split_path}). "
                "장소마다 하나만 준다", EXIT_INVALID)
        places[split.place_id] = split_path
        splits.append(split)
    fit_path = resolve_input(args.fit or DEFAULT_FIT_FILE, dirs.fit, kind="적합 설정")
    try:
        fit = load_fit_file(fit_path).fit
    except FitFileError as error:
        raise CliError(f"적합 설정 파일 오류: {error}", EXIT_INVALID) from None

    train: list[PlaceRuns] = []
    validation: list[PlaceRuns] = []
    conn = open_read_db(config)
    try:
        for split in splits:
            train.append(PlaceRuns(split.place_id, load_labeled_runs(conn, split.train)))
            validation.append(PlaceRuns(split.place_id, load_labeled_runs(conn, split.validation)))
    except RunLoadError as error:
        raise CliError(str(error), EXIT_FAILED) from None
    finally:
        conn.close()
    # 앵커가 셋 이상이면 한 기기의 라벨 회차가 없어도 적합이 다른 회차로 해 버린다. 여기서 막는다.
    anchors = {group.place_id: place_anchors(group, held_out) for group, held_out in zip(train, validation)}
    missing = sorted({
        node_id for group in train for node_id in anchors[group.place_id] - {run.label for run in group.runs}})
    if missing:
        raise CliError(f"학습 회차에 라벨이 없는 기기가 있다: {', '.join(missing)}. "
                       "그 기기 앞에서 더 수집하고 다시 분할한다", EXIT_FAILED)
    fit_config = CommonFitConfig(
        model_version=args.version, taus=fit.taus, stays=fit.stays, window_secs=fit.window_secs,
        auto_zero_min_bins=fit.auto_zero_min_bins)
    try:
        result = fit_common_model(train, validation, fit_config, bin_sec=config.settings.rssi_frame_sec)
    except FitError as error:
        raise CliError(f"적합 실패: {error}", EXIT_FAILED) from None

    _print_roles(result)
    _print_zeros(result, anchors)
    _print_grid(result)
    if args.dry_run:
        print("드라이런: 모델 파일을 쓰지 않았다.")
        return EXIT_OK

    try:
        written = write_common_model_file(config.proximity.model_root, result.model)
    except OSError as error:
        # Windows는 폴더 자리에 파일이 있어도 mkdir가 FileExistsError다. 모델 파일이 있을 때만 이미 있다로 본다
        if isinstance(error, FileExistsError) and model_path.exists():  # 위의 확인 뒤에 다른 곳에서 만들었다
            raise _model_exists(model_path) from None
        raise CliError(f"모델 파일을 쓸 수 없다: {model_path} ({error.strerror or error})", EXIT_INVALID) from error
    print(f"작성: {written}")
    print(f'서버에서 쓰려면 설정의 [proximity]에 model_version = "{args.version}"을 넣는다.')
    return EXIT_OK


def _model_exists(path: Path) -> CliError:
    return CliError(f"모델 파일이 이미 있다: {path}. 다른 --version을 쓴다", EXIT_FAILED)


def _print_roles(result: CommonFitResult) -> None:
    """역할별 적합 값. 앞, 옆, 없음 순이고 소수 둘째 자리까지 쓴다."""
    model = result.model
    print("  ".join(["역할", "mean", "sigma"]))
    for role, params in (("front", model.front), ("other", model.other), ("none", model.none)):
        print("  ".join([role, f"{params.mean:.2f}", f"{params.sigma:.2f}"]))


def _print_zeros(result: CommonFitResult, anchors: Mapping[str, frozenset[str]]) -> None:
    """장소별 자동 영점. 장소 ID 순, 기기 ID 순이고 소수 둘째 자리까지 쓴다. 영점을 찾지 못한 기기는 `-`다."""
    print("  ".join(["장소", "기기", "영점"]))
    for place_id in sorted(anchors):
        for node_id in sorted(anchors[place_id]):
            zero = result.zeros.get(place_id, {}).get(node_id)
            print("  ".join([place_id, node_id, "-" if zero is None else f"{zero:.2f}"]))


def _print_grid(result: CommonFitResult) -> None:
    """검증 손실이 작은 조합부터 `GRID_ROWS`개. 고른 조합의 줄 끝에 `← 선택`을 쓴다. 손실이 무한대면 `inf`로 나온다."""
    model = result.model
    chosen = (model.calibration_tau, model.stay, model.window_sec)
    ranked = sorted(result.losses, key=lambda grid: grid.loss)  # 정렬은 안정적이라 같은 손실은 후보 순서다
    print("  ".join(["tau", "stay", "window_sec", "손실"]))
    for grid in ranked[:GRID_ROWS]:
        line = "  ".join([f"{grid.tau:g}", f"{grid.stay:g}", f"{grid.window_sec:g}", f"{grid.loss:.4f}"])
        print(f"{line}  ← 선택" if (grid.tau, grid.stay, grid.window_sec) == chosen else line)
