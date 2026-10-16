"""`nearpilot fit`: 분할 결과의 학습·검증 회차로 근접 모델을 적합해 모델 파일에 쓴다
(specs/core-ops-cli/spec.md 6절 "fit").

적합 계산은 `core/proximity/fit.py`의 `fit_model`이 한다. 이 모듈은 파일 인자를 풀고, 학습·검증 회차를
읽기 전용 연결로 읽고 (NFR-13), 결과를 쓴다. 시험 회차는 읽지 않는다. 같은 버전의 모델 파일은
덮어쓰지 않고 (#40), 그 확인은 DB를 읽기 전에 한다. `--dry-run`은 모델 파일도 모델 폴더도 만들지 않는다.
"""

from __future__ import annotations

import argparse

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError, dirs_for, load_cli_config, open_read_db, require_name, resolve_input
from nearpilot.core.proximity.files import FitFileError, LayoutFileError, load_fit_file, load_layout
from nearpilot.core.proximity.fit import FitConfig, FitError, FitResult, fit_model, write_model_file
from tools.evaluation.runs import RunLoadError, load_labeled_runs
from tools.evaluation.split import SplitError, load_split

GRID_ROWS = 10  # 조합별 손실 표에 쓰는 줄 수. 손실이 작은 순서다


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "fit", parents=[parent], help="수집 회차로 근접 모델을 적합해 모델 파일에 쓴다",
        description="분할 결과의 학습 회차로 기기마다 RSSI 값을 적합하고, 검증 회차로 τ·stay·window_sec 조합을 골라 "
                    "모델 파일에 쓴다. 시험 회차는 읽지 않는다. 같은 버전의 모델 파일은 덮어쓰지 않는다.")
    parser.add_argument("layout", metavar="LAYOUT",
                        help="배치 파일(JSON). 이름이면 설정 파일 옆 layouts/ 폴더에서 찾고, "
                             "경로(/ 나 \\ 포함)면 실행 폴더 기준이다")
    parser.add_argument("--split", metavar="FILE", required=True,
                        help="분할 결과 파일(JSON). 이름이면 var/splits/에서 찾고, 경로면 실행 폴더 기준이다")
    parser.add_argument("--version", metavar="NAME", required=True,
                        help="모델 버전. 모델 파일 이름이 된다. 같은 버전의 파일이 있으면 덮어쓰지 않는다")
    parser.add_argument("--fit", metavar="FILE",
                        help="적합 설정 파일의 [fit]을 읽는다. 이름이면 설정 파일 옆 fit/ 폴더에서 찾고, "
                             "경로(/ 나 \\ 포함)면 실행 폴더 기준이다. 기본은 <배치의 장소>.toml")
    parser.add_argument("--calib-version", metavar="NAME",
                        help="모델의 보정 버전. 없으면 적합 설정의 [fit].calib_version, 그것도 없으면 --version 값")
    parser.add_argument("--dry-run", action="store_true",
                        help="앵커 값과 조합별 손실만 쓰고 모델 파일은 쓰지 않는다")
    parser.set_defaults(handler=_fit)


def _fit(args: argparse.Namespace) -> int:
    require_name(args.version, "--version")
    config = load_cli_config(args.config)
    dirs = dirs_for(config.source)

    layout_path = resolve_input(args.layout, dirs.layouts, kind="배치")
    try:
        layout = load_layout(layout_path)
    except LayoutFileError as error:
        raise CliError(f"배치 파일 오류: {error}", EXIT_INVALID) from None
    split_path = resolve_input(args.split, dirs.splits, kind="분할 결과")
    try:
        split = load_split(split_path)
    except SplitError as error:
        raise CliError(f"분할 결과 파일 오류: {error}", EXIT_INVALID) from None
    if split.place_id != layout.place_id:
        raise CliError(
            f"분할 결과의 장소({split.place_id})와 배치의 장소({layout.place_id})가 다르다", EXIT_INVALID)
    fit_path = resolve_input(args.fit or f"{layout.place_id}.toml", dirs.fit, kind="적합 설정")
    try:
        fit = load_fit_file(fit_path).fit
    except FitFileError as error:
        raise CliError(f"적합 설정 파일 오류: {error}", EXIT_INVALID) from None

    # 처음 있는 것: --calib-version, [fit].calib_version, --version
    calib_version = next(v for v in (args.calib_version, fit.calib_version, args.version) if v is not None)
    model_path = config.proximity.model_root / layout.place_id / f"{args.version}.json"
    if not args.dry_run and model_path.exists():
        raise CliError(f"모델 파일이 이미 있다: {model_path}. 다른 --version을 쓴다", EXIT_FAILED)

    conn = open_read_db(config)
    try:
        train = load_labeled_runs(conn, split.train)
        validation = load_labeled_runs(conn, split.validation)
    except RunLoadError as error:
        raise CliError(str(error), EXIT_FAILED) from None
    finally:
        conn.close()
    # 앵커가 셋 이상이면 한 기기의 라벨 회차가 없어도 fit_model이 다른 회차로 적합해 버린다. 여기서 막는다.
    missing = sorted(set(layout.nodes) - {run.label for run in train})
    if missing:
        raise CliError(f"학습 회차에 라벨이 없는 기기가 있다: {', '.join(missing)}. "
                       "그 기기 앞에서 더 수집하고 다시 분할한다", EXIT_FAILED)
    fit_config = FitConfig(
        model_version=args.version, calib_version=calib_version, min_distance_m=fit.min_distance_m,
        taus=fit.taus, stays=fit.stays, window_secs=fit.window_secs)
    try:
        result = fit_model(layout, train, validation, fit_config, bin_sec=config.settings.rssi_frame_sec)
    except FitError as error:
        raise CliError(f"적합 실패: {error}", EXIT_FAILED) from None

    _print_anchors(result)
    _print_grid(result)
    if args.dry_run:
        print("드라이런: 모델 파일을 쓰지 않았다.")
        return EXIT_OK

    try:
        written = write_model_file(config.proximity.model_root, result.model)
    except FileExistsError:  # 위의 확인 뒤에 다른 곳에서 만들었다
        raise CliError(f"모델 파일이 이미 있다: {model_path}. 다른 --version을 쓴다", EXIT_FAILED) from None
    except OSError as error:
        raise CliError(f"모델 파일을 쓸 수 없다: {model_path} ({error.strerror or error})", EXIT_INVALID) from error
    print(f"작성: {written}")
    print(f'서버에서 쓰려면 설정의 [proximity.models]에 {layout.place_id} = "{args.version}"을 넣는다.')
    return EXIT_OK


def _print_anchors(result: FitResult) -> None:
    """앵커별 적합 값. 기기 ID 순이고 소수 둘째 자리까지 쓴다."""
    print("  ".join(["기기", "A", "n", "sigma", "none_rssi", "none_sigma"]))
    for node_id in sorted(result.model.nodes):
        node = result.model.nodes[node_id]
        values = (node.A, node.n, node.sigma, node.none_rssi, node.none_sigma)
        print("  ".join([node_id, *(f"{value:.2f}" for value in values)]))


def _print_grid(result: FitResult) -> None:
    """검증 손실이 작은 조합부터 `GRID_ROWS`개. 고른 조합의 줄 끝에 `← 선택`을 쓴다. 손실이 무한대면 `inf`로 나온다."""
    model = result.model
    chosen = (model.calibration_tau, model.stay, model.window_sec)
    ranked = sorted(result.losses, key=lambda grid: grid.loss)  # 정렬은 안정적이라 같은 손실은 후보 순서다
    print("  ".join(["tau", "stay", "window_sec", "손실"]))
    for grid in ranked[:GRID_ROWS]:
        line = "  ".join([f"{grid.tau:g}", f"{grid.stay:g}", f"{grid.window_sec:g}", f"{grid.loss:.4f}"])
        print(f"{line}  ← 선택" if (grid.tau, grid.stay, grid.window_sec) == chosen else line)
