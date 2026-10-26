"""`nearpilot evaluate-server`, `nearpilot evaluate-reask`: 서버 지표를 재서 보고서(CSV, Markdown), 원자료(JSON), 그래프(PNG)에 쓴다
(specs/db-evaluation-server-metrics/spec.md 6절 "결정", NFR-13).

지표를 재는 일은 `tools/evaluation/`의 `metric_*.py`와 `reask.py`가, 보고서 형식은 `report.py`가, 그래프는 `plots.py`가 한다.
이 모듈은 설정을 읽어 검사하고, 출력 경로를 확인하고, 지표를 돌리고, 결과를 쓴다. 기준에 못 미쳐도 종료 코드는 0이다.
이 명령은 측정 결과를 내고, 통과 여부는 보고서가 말한다. 종료 코드: 0 보고서를 썼다, 1 실행 실패(하네스·서버), 2 입력·설정 오류.

`evaluate-server`는 지표 9를 서버 없이 먼저 계산하고, 나머지는 임시 폴더의 하네스 하나 안에서 `HARNESS_ORDER`로 돌린다.
지표 번호를 어떤 순서로 줘도 도는 순서는 같고, 보고서의 행은 지표 번호순이다. 지표 사이에는 시뮬레이터 기기의 시나리오를
처음 상태로 돌린다. 시드는 지표 3과 9가 각자 받는 `random.Random(시드)`로 들어가서, 같은 시드와 설정이면 무작위 입력이 같다.
시드와 도는 순서는 원자료와 보고서의 열에 남긴다. 측정이 도중에 실패하면 하네스는 모두 끄고 (하네스가 한다), 그때까지 끝난
지표의 원자료만 `<NAME>-raw.json`에 남긴다. 보고서와 그래프는 쓰지 않는다.
`evaluate-reask`는 `nearpilot eval`처럼 분할 결과의 시험 회차만 읽기 전용 연결로 읽어 비용 비율별 재질문 곡선(지표 2)을 만든다.
이미 있는 출력 파일은 `--force` 없이는 덮어쓰지 않고, 그 확인은 하네스를 띄우거나 DB를 읽기 전에 한다.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import random
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli.common import CliError, dirs_for, is_path_argument, load_cli_config, open_read_db, resolve_input
from nearpilot.cli.eval import _print_table, _split_model_argument
from nearpilot.core.proximity.model import ModelFileError, load_model
from tools.evaluation.db import EXPECTED_SCHEMA_VERSION
from tools.evaluation.harness import Harness, HarnessError, find_mosquitto
from tools.evaluation.metric_load import run_metric3, run_metric7
from tools.evaluation.metric_safety import run_metric5, run_metric6
from tools.evaluation.metric_select import run_metric1, run_metric4, run_metric9
from tools.evaluation.plots import write_plots
from tools.evaluation.proximity import EvaluationError, collect_samples
from tools.evaluation.reask import CurvePoint, reask_curve, reask_rows
from tools.evaluation.report import write_report
from tools.evaluation.rows import MetricOutcome
from tools.evaluation.runs import RunLoadError, load_labeled_runs
from tools.evaluation.server_config import EvalConfig, EvalConfigError, load_eval_config
from tools.evaluation.split import SplitError, load_split
from tools.node_sim import Scenario

SERVER_ROOT = Path(__file__).resolve().parents[3]  # 이 checkout의 server/. 하네스가 코드와 서버 설정 예시를 여기서 읽는다
# 하네스 안에서 지표가 도는 순서. 지표 1·4는 요청 ID가 고정이라 하네스 하나에서 한 번만 돌고, 지표 3의 부하는 뒤 지표의
# 근접 상태를 흔들 수 있어서 마지막에 둔다. 지표 9는 서버 없이 먼저 돈다.
HARNESS_ORDER = (1, 4, 5, 6, 7, 3)
SERVER_METRICS = tuple(sorted((*HARNESS_ORDER, 9)))  # evaluate-server가 재는 지표. 지표 2는 evaluate-reask, 8은 eval이 잰다
DEFAULT_RATIOS = "1,2,5,10"  # evaluate-reask 곡선의 비용 비율. --ratios로 바꾼다
_NO_DB = "없음 (서버를 띄우지 않았다)"  # 지표 9만 잴 때 보고서 머리말의 DB 경로 칸

# 지표마다 읽는 설정 키: (counts, acceptance, rssi). 지표 모듈은 키를 지표를 돌릴 때 읽는다. 하네스를 띄운 뒤에 빠진 키를
# 알게 되지 않도록, 띄우기 전에 이 표로 확인한다. 지표 모듈이 읽는 키를 바꾸면 이 표도 바꾼다.
_READS: dict[int, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    1: (("m1_utterances",), ("m1_max_wrong_rate",), ("near", "far")),
    3: (("m3_requests", "m3_retransmits", "m3_clients"), ("m3_max_double_occupancy", "m3_max_duplicate_execution"),
        ("near",)),
    4: (("m4_each",), ("m4_max_cross_user_malfunction", "m4_min_success_rate"), ("near", "far")),
    5: (("m5_each",), ("m5_max_out_of_scope", "m5_max_untrusted_candidates", "m5_min_valid_approval_success_rate"),
        ("near",)),
    6: (("m6_each",), ("m6_max_auto_release", "m6_max_foreign_release", "m6_max_release_during_fault",
                       "m6_max_unapproved_recovery", "m6_min_recovery_success_rate"), ("near",)),
    7: (("m7_server_requests",), ("m7_max_median_sec", "m7_max_p95_sec"), ("near",)),
    9: (("m9_sets",), ("m9_min_match_rate",), ()),
}


def now_utc() -> datetime:
    """보고서의 생성 시각과 기본 이름의 날짜. 테스트가 monkeypatch 한다."""
    return datetime.now(timezone.utc)


def register(subparsers: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    server = subparsers.add_parser(
        "evaluate-server", parents=[parent], help="임시 브로커·서버·시뮬레이터로 서버 지표 1·3·4·5·6·7·9를 잰다",
        description="평가 설정(TOML)대로 임시 폴더에 Mosquitto, 서버, 시뮬레이터 기기를 띄워 지표 1·3·4·5·6·7을 재고, "
                    "서버 없이 지표 9를 계산해 보고서(CSV, Markdown), 원자료(JSON), 그래프(PNG)에 쓴다. 운영 DB와 실제 기기에는 접속하지 않는다. "
                    "기준에 못 미쳐도 종료 코드는 0이다. 이미 있는 보고서는 --force 없이는 덮어쓰지 않는다.")
    server.add_argument("eval_config", metavar="EVAL_TOML",
                        help="평가 설정 파일(TOML. 서버 설정이 아니다). 이름이면 설정 파일 옆 eval/ 폴더에서 찾고, "
                             "경로(/ 나 \\ 포함)면 실행 폴더 기준이다")
    server.add_argument("--metrics", metavar="LIST", default=",".join(map(str, SERVER_METRICS)),
                        help="잴 지표 번호를 쉼표로 이어 쓴다 (기본 %(default)s). 번호를 어떤 순서로 줘도 지표 9가 먼저, "
                             f"나머지는 {','.join(map(str, HARNESS_ORDER))} 순서로 돈다. 부하를 거는 지표 3을 마지막에 둔다")
    server.add_argument("--seed", metavar="N", type=int,
                        help="지표 3과 9의 무작위 입력의 시드. 같은 시드와 설정이면 같다. 기본은 설정 파일의 seed")
    _add_output_options(server, "server-metrics-<UTC 날짜 YYYYMMDD>")
    server.set_defaults(handler=_evaluate_server)

    reask = subparsers.add_parser(
        "evaluate-reask", parents=[parent], help="시험 회차로 비용 비율별 재질문률·오작동률 곡선(지표 2)을 만든다",
        description="분할 결과의 시험 회차로 모델의 비용 비율별 재질문률·오작동률 곡선을 만들어 보고서(CSV, Markdown), "
                    "원자료(JSON), 그래프(PNG)에 쓴다. 학습·검증 회차는 읽지 않는다. 통과 여부는 판정하지 않는다 (비교 기준 미결). "
                    "이미 있는 보고서는 --force 없이는 덮어쓰지 않는다.")
    reask.add_argument("model", metavar="PLACE/VERSION",
                       help="평가할 모델. 설정의 모델 폴더에서 <PLACE>/<VERSION>.json을 읽는다")
    reask.add_argument("--split", metavar="FILE", required=True,
                       help="분할 결과 파일(JSON). 이름이면 var/splits/에서 찾고, 경로(/ 나 \\ 포함)면 실행 폴더 기준이다")
    reask.add_argument("--ratios", metavar="LIST", default=DEFAULT_RATIOS,
                       help="비용 비율 r을 쉼표로 이어 쓴다 (기본 %(default)s). r마다 확률 임계값 r/(1+r) 아래면 재질문으로 센다")
    _add_output_options(reask, "<PLACE>-<VERSION>-reask-<UTC 날짜 YYYYMMDD>")
    reask.set_defaults(handler=_evaluate_reask)


def _add_output_options(parser: argparse.ArgumentParser, default_name: str) -> None:
    parser.add_argument("--out", metavar="NAME",
                        help="보고서 이름(확장자와 경로 구분자 없이). var/reports/<NAME>.csv, .md, -raw.json과 그래프 <NAME>-*.png에 쓴다. "
                             f"기본은 {default_name}")
    parser.add_argument("--force", action="store_true", help="이미 있는 보고서를 덮어쓴다")


# ── evaluate-server ──────────────────────────────────────────────────


def _evaluate_server(args: argparse.Namespace) -> int:
    numbers = _parse_metrics(args.metrics)
    config = load_cli_config(args.config)
    dirs = dirs_for(config.source)
    eval_path = resolve_input(args.eval_config, dirs.config_dir / "eval", kind="평가 설정")
    try:
        eval_config = load_eval_config(eval_path)
    except EvalConfigError as error:
        raise CliError(f"평가 설정 오류: {error}", EXIT_INVALID) from None
    _require_settings(eval_config, numbers)
    seed = eval_config.seed if args.seed is None else args.seed

    name = _report_name(args.out, f"server-metrics-{now_utc():%Y%m%d}")
    csv_path, md_path, raw_path = _report_paths(dirs.reports, name)
    plots = _plot_paths(dirs.reports, name, ("latency", "pass") if 7 in numbers else ("pass",))
    _refuse_existing([csv_path, md_path, raw_path, *plots], force=args.force)
    _make_folder(dirs.reports)  # 긴 측정이 끝난 뒤가 아니라 시작하기 전에 만든다

    order = _run_order(numbers)
    header = {"command": "evaluate-server", "seed": seed, "config_file": str(eval_path), "order": order}
    kept: dict[str, Any] = {}  # 지표가 끝날 때마다 그 원자료를 넣는다
    try:
        outcomes, db_path = _run_metrics(eval_config, numbers, seed, kept)
    except BaseException as error:  # Ctrl+C도 그때까지의 원자료를 남긴다
        _keep_raw(raw_path, header, kept, error, force=args.force)
        if isinstance(error, HarnessError):
            raise CliError(f"서버 지표 평가에 실패했다: {error}", EXIT_FAILED) from None
        if isinstance(error, EvalConfigError):  # 장면에 필요한 사용자나 기기가 설정에 없다. 하네스를 띄운 뒤에야 알 수 있다
            raise CliError(f"평가 설정 오류: {error}", EXIT_INVALID) from None
        raise

    # 무엇을 어떤 순서로 쟀는지 보고서에 남긴다 (requirements.md §4 "입력, 시나리오, 버전, 표본 수를 보존한다")
    provenance = {"seed": seed, "metric_order": ",".join(map(str, order)), "config_file": str(eval_path)}
    rows = [{**row, **provenance} for number in sorted(outcomes) for row in outcomes[number].rows]
    sample_count = sum(row["trials"] for row in rows if isinstance(row["trials"], int))  # 빈 칸("")은 더하지 않는다
    _write_results(
        dirs.reports, name, force=args.force, rows=rows, raw={**header, "metrics": kept}, db_path=db_path,
        schema_version=EXPECTED_SCHEMA_VERSION, sample_count=sample_count, generated_at=now_utc(),
        latencies=outcomes[7].raw["latencies"] if 7 in outcomes else None, curve=None, graph_rows=rows)
    return EXIT_OK


def _parse_metrics(text: str) -> tuple[int, ...]:
    """`--metrics`의 지표 번호. 쉼표로 이은 `SERVER_METRICS`의 번호이고 겹치지 않아야 한다. 틀리면 `CliError`(종료 코드 2)다."""
    try:
        numbers = tuple(int(part) for part in text.split(","))
    except ValueError:
        raise CliError(f"--metrics는 쉼표로 이은 지표 번호다 (예: 1,3,9): {text!r}", EXIT_INVALID) from None
    unknown = sorted(set(numbers) - set(SERVER_METRICS))
    if unknown:
        raise CliError(f"--metrics의 지표 {', '.join(map(str, unknown))}은(는) evaluate-server가 재지 않는다. "
                       f"잴 수 있는 지표: {', '.join(map(str, SERVER_METRICS))}", EXIT_INVALID)
    if len(set(numbers)) != len(numbers):
        raise CliError(f"--metrics에 같은 지표가 두 번 있다: {text}", EXIT_INVALID)
    return numbers


def _require_settings(config: EvalConfig, numbers: Sequence[int]) -> None:
    """고른 지표가 읽는 횟수, 통과 기준, RSSI가 설정에 모두 있어야 한다. 빠진 것을 모아 `CliError`(종료 코드 2)로 낸다."""
    sections = (("counts", config.counts), ("acceptance", config.acceptance), ("rssi", config.rssi))
    missing: dict[tuple[str, str], list[int]] = {}  # (절, 키) → 그 키를 읽는 지표
    for number in sorted(numbers):
        for (section, values), keys in zip(sections, _READS[number]):
            for key in keys:
                if key not in values:
                    missing.setdefault((section, key), []).append(number)
    if missing:
        problems = [f"{section}.{key}: 지표 {', '.join(map(str, readers))}이(가) 읽는 값이 없다"
                    for (section, key), readers in missing.items()]
        raise CliError(f"평가 설정 오류: {'; '.join(problems)}", EXIT_INVALID)


def _run_order(numbers: Sequence[int]) -> list[int]:
    """고른 지표가 도는 순서. 서버 없이 도는 지표 9가 먼저고, 나머지는 `HARNESS_ORDER`다."""
    return [number for number in (9, *HARNESS_ORDER) if number in numbers]


def _run_metrics(config: EvalConfig, numbers: Sequence[int], seed: int,
                 kept: dict[str, Any]) -> tuple[dict[int, MetricOutcome], str]:
    """고른 지표를 돌려 (지표 번호 → 결과, 보고서 머리말에 쓸 DB 경로)를 돌려준다.

    하네스를 쓰는 지표가 있으면 아무것도 재기 전에 Mosquitto와 임시 폴더 경로부터 확인한다.
    지표 9는 서버 없이 먼저 돈다. 나머지는 임시 폴더의 하네스 하나 안에서 `HARNESS_ORDER`로 돌고, 지표 사이에 기기 상태를 정리한다.
    지표가 끝날 때마다 그 원자료를 `kept`(`m<번호>` → 원자료)에 넣어서, 도중에 실패해도 호출자가 그때까지의 것을 남길 수 있다.
    하네스 시작과 지표의 실패는 예외로 올라온다 (`HarnessError`, `EvalConfigError`). 하네스는 `with`가 끝나면 모두 끈다.
    """
    outcomes: dict[int, MetricOutcome] = {}

    def measure(number: int, run: Callable[[], MetricOutcome]) -> None:
        print(f"지표 {number} 측정 중", flush=True)  # 전체를 재는 데 15~20분이 걸린다
        outcomes[number] = run()
        kept[f"m{number}"] = outcomes[number].raw

    ordered = [number for number in HARNESS_ORDER if number in numbers]
    if ordered:  # 하네스가 뜰 수 없는 환경이면 지표 9도 재기 전에 알린다 (spec 5절 "시작 전에 오류와 종료 코드 1")
        find_mosquitto()
        temp_root = tempfile.gettempdir()
        if not temp_root.isascii():  # Mosquitto가 한글 경로의 설정 파일을 열지 못한다 (하네스 문서)
            raise CliError(f"임시 폴더 경로에 ASCII가 아닌 글자가 있다: {temp_root}. TMP나 TMPDIR을 ASCII 경로로 바꾼다",
                           EXIT_FAILED)
    if 9 in numbers:
        measure(9, lambda: run_metric9(config, random.Random(seed)))
    db_path = _NO_DB
    if ordered:
        with tempfile.TemporaryDirectory(prefix="np-eval-", ignore_cleanup_errors=True) as folder:
            workdir = Path(folder)
            with Harness(config, workdir, server_root=SERVER_ROOT) as harness:
                runners = {
                    1: lambda: run_metric1(harness, config),
                    3: lambda: run_metric3(harness, config, random.Random(seed)),
                    4: lambda: run_metric4(harness, config),
                    5: lambda: run_metric5(harness, config),
                    6: lambda: run_metric6(harness, config),
                    7: lambda: run_metric7(harness, config),
                }
                for index, number in enumerate(ordered):
                    if index:
                        _reset_devices(harness, config)
                    measure(number, runners[number])
                db_path = harness.query("PRAGMA database_list")[0]["file"]  # 하네스 임시 DB. 폴더는 곧 지워진다
    return outcomes, db_path


def _reset_devices(harness: Harness, config: EvalConfig) -> None:
    """지표가 시뮬레이터 기기에 남긴 시나리오(들리는 비콘, 구동 결과, 저장 오류)를 처음 상태로 돌린다."""
    fresh = Scenario(actuation_sec=config.actuation_sec)
    changes = {field.name: getattr(fresh, field.name) for field in dataclasses.fields(fresh)}
    for node in config.nodes:
        harness.set_scenario(node.node_id, **changes)


def _keep_raw(path: Path, header: Mapping[str, Any], kept: Mapping[str, Any], error: BaseException, *,
              force: bool) -> None:
    """측정이 도중에 끝났을 때 그때까지 끝난 지표의 원자료를 남긴다. 끝난 지표가 없으면 아무것도 쓰지 않는다.

    쓰지 못해도 경고만 하고 원래 오류를 가리지 않는다.
    """
    if not kept:
        return
    document = {"generated_at": now_utc().isoformat(), **header, "metrics": kept,
                "error": f"{type(error).__name__}: {error}"}
    try:
        _write_raw(path, document, force=force)
    except OSError as problem:  # 덮어쓰지 않는 파일이 그사이에 생긴 경우(FileExistsError)도 같다
        print(f"경고: 지금까지의 원자료를 남기지 못했다: {path} ({problem.strerror or problem})", file=sys.stderr)
        return
    print(f"지금까지의 원자료: {path}", file=sys.stderr)


# ── evaluate-reask ───────────────────────────────────────────────────


def _evaluate_reask(args: argparse.Namespace) -> int:
    place, version = _split_model_argument(args.model)
    ratios = _parse_ratios(args.ratios)
    config = load_cli_config(args.config)
    dirs = dirs_for(config.source)

    model_path = resolve_input(f"{version}.json", config.proximity.model_root / place, kind="모델")
    try:
        model = load_model(model_path)
    except (ModelFileError, OSError) as error:
        raise CliError(f"모델 파일 오류: {error}", EXIT_INVALID) from None
    if model.place_id != place:
        raise CliError(f"모델의 장소({model.place_id})와 평가할 장소({place})가 다르다", EXIT_INVALID)
    split_path = resolve_input(args.split, dirs.splits, kind="분할 결과")
    try:
        split = load_split(split_path)
    except SplitError as error:
        raise CliError(f"분할 결과 파일 오류: {error}", EXIT_INVALID) from None
    if split.place_id != place:
        raise CliError(f"분할 결과의 장소({split.place_id})와 평가할 장소({place})가 다르다", EXIT_INVALID)

    name = _report_name(args.out, f"{place}-{version}-reask-{now_utc():%Y%m%d}")
    csv_path, md_path, raw_path = _report_paths(dirs.reports, name)
    _refuse_existing([csv_path, md_path, raw_path, *_plot_paths(dirs.reports, name, ("reask",))], force=args.force)

    conn = open_read_db(config)
    try:
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        runs = load_labeled_runs(conn, split.test)  # 시험 회차만 읽는다
    except RunLoadError as error:
        raise CliError(str(error), EXIT_FAILED) from None
    finally:
        conn.close()
    try:
        samples = collect_samples(model, runs, bin_sec=config.settings.rssi_frame_sec)
        if not samples:
            raise EvaluationError("시험 회차의 표본이 없어 곡선을 만들 수 없다")
        points = reask_curve(samples, ratios)
    except EvaluationError as error:
        raise CliError(f"평가 실패: {error}", EXIT_FAILED) from None

    provenance = {"place_id": place, "model_version": model.model_version, "calib_version": model.calib_version,
                  "split_file": str(split_path)}
    rows = [{**row, **provenance} for row in reask_rows(points, len(samples))]
    generated_at = now_utc()
    raw = {"command": "evaluate-reask", "model": f"{place}/{version}", "calib_version": model.calib_version,
           "split_file": str(split_path), "ratios": ratios, "samples": len(samples),
           "curve": [dataclasses.asdict(point) for point in points]}
    # 통과 막대는 이 명령의 행이 모두 "판정 안 함"이라 그리지 않는다
    _write_results(
        dirs.reports, name, force=args.force, rows=rows, raw=raw, db_path=str(config.database.path),
        schema_version=schema_version, sample_count=len(samples), generated_at=generated_at, latencies=None,
        curve=points, graph_rows=[])
    return EXIT_OK


def _parse_ratios(text: str) -> list[float]:
    """`--ratios`의 비용 비율. 쉼표로 이은 0보다 큰 유한한 수이고 겹치지 않아야 한다. 틀리면 `CliError`(종료 코드 2)다."""
    try:
        ratios = [float(part) for part in text.split(",")]
    except ValueError:
        raise CliError(f"--ratios는 쉼표로 이은 수다 (예: 1,2,5,10): {text!r}", EXIT_INVALID) from None
    if not all(math.isfinite(ratio) and ratio > 0 for ratio in ratios):
        raise CliError(f"--ratios는 0보다 큰 유한한 수여야 한다: {text}", EXIT_INVALID)
    if len({f"{ratio:g}" for ratio in ratios}) != len(ratios):  # 행 이름 m2_reask_r{r:g}가 겹치지 않아야 한다
        raise CliError(f"--ratios에 같은 비율이 두 번 있다: {text}", EXIT_INVALID)
    return ratios


# ── 출력 ─────────────────────────────────────────────────────────────


def _report_name(out: str | None, default: str) -> str:
    name = out or default
    if is_path_argument(name):
        raise CliError(f"--out은 경로가 아니라 이름이다. /나 \\ 없이 쓴다: {name}", EXIT_INVALID)
    return name


def _report_paths(reports: Path, name: str) -> tuple[Path, Path, Path]:
    """보고서 CSV, Markdown, 원자료 JSON의 경로."""
    return reports / f"{name}.csv", reports / f"{name}.md", reports / f"{name}-raw.json"


def _plot_paths(reports: Path, name: str, kinds: Sequence[str]) -> list[Path]:
    """`write_plots`가 쓸 수 있는 그래프의 경로 (`{name}-{종류}.png`)."""
    return [reports / f"{name}-{kind}.png" for kind in kinds]


def _refuse_existing(paths: Sequence[Path], *, force: bool) -> None:
    """쓸 파일이 이미 있으면 `--force` 없이는 `CliError`(종료 코드 2)다. 하네스를 띄우거나 DB를 읽기 전에 확인한다."""
    present = [path for path in paths if path.exists()]
    if present and not force:
        raise _report_exists(present)


def _report_exists(present: Sequence[Path], kept_raw: Path | None = None) -> CliError:
    """`present`가 이미 있어 쓰지 못했다는 `CliError`(종료 코드 2). `kept_raw`는 막히기 전에 이 실행이 쓴 원자료다."""
    message = f"보고서가 이미 있다: {', '.join(str(path) for path in present)}. --force로 덮어쓴다"
    if kept_raw:
        message += f" (측정한 원자료는 {kept_raw}에 썼다)"
    return CliError(message, EXIT_INVALID)


def _make_folder(reports: Path) -> None:
    try:
        reports.mkdir(parents=True, exist_ok=True)
    except OSError as error:  # 폴더 자리에 파일이 있으면 FileExistsError다
        raise CliError(f"보고서 폴더를 만들 수 없다: {reports} ({error.strerror or error})", EXIT_INVALID) from error


def _write_raw(path: Path, document: Mapping[str, Any], *, force: bool) -> None:
    """원자료 JSON을 쓴다. `force`가 없으면 파일이 이미 있을 때 `FileExistsError`다 (확인 뒤에 만들어진 파일도 덮어쓰지 않는다)."""
    text = json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n"
    with path.open("w" if force else "x", encoding="utf-8", newline="\n") as file:
        file.write(text)


def _write_results(
    reports: Path, name: str, *, force: bool, rows: list[dict[str, Any]], raw: Mapping[str, Any], db_path: str,
    schema_version: int, sample_count: int, generated_at: datetime, latencies: Sequence[float] | None,
    curve: Sequence[CurvePoint] | None, graph_rows: Sequence[Mapping[str, Any]],
) -> None:
    """원자료, 보고서(CSV, Markdown), 그래프를 순서대로 쓰고 표와 쓴 파일을 출력한다.

    원자료를 먼저 쓴다. 보고서를 쓰다 실패해도 오래 걸려 얻은 값이 남는다. 그래프를 못 만들면 경고만 하고 보고서는 그대로 둔다.
    """
    _make_folder(reports)
    csv_path, md_path, raw_path = _report_paths(reports, name)
    try:
        _write_raw(raw_path, {"generated_at": generated_at.isoformat(), **raw}, force=force)
    except FileExistsError:  # 위의 확인 뒤에 다른 곳에서 만들었다
        raise _report_exists([path for path in (csv_path, md_path, raw_path) if path.exists()]) from None
    except OSError as error:
        raise CliError(f"보고서를 쓸 수 없다: {raw_path} ({error.strerror or error})", EXIT_INVALID) from error
    try:
        write_report(
            rows, db_path=db_path, schema_version=schema_version, sample_count=sample_count, generated_at=generated_at,
            csv_path=str(csv_path), md_path=str(md_path), force=force)
    except FileExistsError:  # 위의 확인 뒤에 다른 곳에서 만들었다. 원자료는 이미 썼다
        raise _report_exists([path for path in (csv_path, md_path) if path.exists()], raw_path) from None
    except OSError as error:
        raise CliError(f"보고서를 쓸 수 없다: {csv_path} ({error.strerror or error})", EXIT_INVALID) from error
    except ValueError as error:  # 시험 횟수가 0인 행이 있으면 신뢰구간을 낼 수 없다 (예: 지표 3에서 기기를 구동한 요청이 없다)
        raise CliError(f"보고서를 만들 수 없다: {error}. 원자료는 {raw_path}에 썼다", EXIT_FAILED) from None
    try:
        drawn = write_plots(reports, name, latencies=latencies, curve=curve, rows=graph_rows)
    except Exception as error:  # noqa: BLE001 - 그래프는 보고서의 덤이다. 못 그려도 보고서는 이미 썼다
        print(f"경고: 그래프를 만들지 못했다 ({error})", file=sys.stderr)
        drawn = []

    _print_table(rows)
    for path in (csv_path, md_path, raw_path, *drawn):
        print(f"작성: {path}")
