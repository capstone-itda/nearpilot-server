"""`nearpilot evaluate-server`, `nearpilot evaluate-reask` (NFR-13, NFR-06, specs/db-evaluation-server-metrics/spec.md 5절·6절).

명령은 같은 프로세스에서 `main(argv)`로 부른다. 실제 Mosquitto, 서버, 시뮬레이터는 띄우지 않는다 (그 시험은 `tests/tools/test_eval_*.py`가
실제 프로세스로 한다). `evaluate-server`는 `Harness`와 지표 함수를 가짜로 바꿔서 명령이 하는 일을 확인한다: 설정과 인자 검사,
출력 경로 확인, 지표 순서와 시드, 지표 사이의 기기 정리, 보고서·원자료·그래프 쓰기, 실패 때의 종료 코드와 남는 파일.
`evaluate-reask`는 `test_cli_eval.py`처럼 실제 SQLite 파일의 합성 회차와 손으로 쓴 모델을 읽는다. 곡선 계산은 `test_eval_reask.py`가 확인한다.
그래프를 실제로 그리는 시험은 matplotlib이 있을 때만 돈다 (`pytest.importorskip`).
"""

from __future__ import annotations

import csv
import dataclasses
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli import evaluate
from nearpilot.core.dashboard.evaluation import evaluation_of
from nearpilot.core.proximity.fit import AnchorLayout, PlaceLayout, write_model_file
from nearpilot.core.proximity.model import NodeModel, ProximityModel
from tools.evaluation import report as report_module
from tools.evaluation.db import EXPECTED_SCHEMA_VERSION
from tools.evaluation.harness import HarnessError
from tools.evaluation.rows import MetricOutcome, metric_row
from tools.evaluation.server_config import EvalConfigError, load_eval_config
from tools.evaluation.split import SplitResult, split_to_json
from tools.node_sim import Scenario

NOW = datetime(2026, 10, 27, 5, 0, 0, tzinfo=timezone.utc)  # 테스트가 고정하는 `now_utc()`
SERVER_NAME = "server-metrics-20261027"  # evaluate-server의 기본 보고서 이름
REASK_NAME = "place-a-v1-reask-20261027"  # evaluate-reask의 기본 보고서 이름
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "eval" / "server-metrics.example.toml"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# 평가 설정: 사용자 둘, 기기 둘. 모든 지표가 읽는 키가 다 있고 횟수는 작다. 지표 함수는 가짜라서 값은 쓰이지 않는다.
EVAL_TOML = """\
seed = 42
actuation_sec = 0.1
startup_timeout_sec = 30.0
model_root = "../../var/models"
model_version = "v1"

[beacon]
uuid = "11111111-2222-3333-4444-555555555555"
major = 1

[[users]]
user_id = "owner"
role = "owner"
minor = 1

[[users]]
user_id = "visitor-1"
role = "visitor"
minor = 2

[[nodes]]
node_id = "locker-1"
capability = "locker"
occupancy = "rental"

[[nodes]]
node_id = "light-1"
capability = "light"
occupancy = "shared"

[counts]
m1_utterances = 3
m3_requests = 4
m3_retransmits = 2
m3_clients = 2
m4_each = 2
m5_each = 2
m6_each = 2
m7_server_requests = 3
m9_sets = 5

[acceptance]
m1_max_wrong_rate = 0.025
m3_max_double_occupancy = 0
m3_max_duplicate_execution = 1
m4_max_cross_user_malfunction = 0
m4_min_success_rate = 0.95
m5_max_out_of_scope = 0
m5_max_untrusted_candidates = 0
m5_min_valid_approval_success_rate = 0.95
m6_max_auto_release = 0
m6_max_foreign_release = 0
m6_max_release_during_fault = 0
m6_max_unapproved_recovery = 0
m6_min_recovery_success_rate = 0.95
m7_max_median_sec = 1.0
m7_max_p95_sec = 2.0
m9_min_match_rate = 1.0

[rssi]
near = -53.0
far = -62.0
absent = -90.0
"""

# 지표 번호 → 가짜 지표 함수가 돌려주는 결과. 행은 통과 칸이 True, False, 빈 값인 것이 섞여 있다.
OUTCOMES = {
    1: MetricOutcome(
        [metric_row("m1_wrong_device_rate", value=0.0, limit=0.025, passed=True, trials=3, failures=0, note="시뮬레이터 측정")],
        {"attempts": [1, 2, 3]}),
    3: MetricOutcome(
        [metric_row("m3_double_occupancy", value="", limit=0, passed=True, trials=7, failures=0),
         metric_row("m3_duplicate_execution", value=1, limit=1, passed=True, trials=4, failures=0),
         metric_row("m3_lock_errors", value=0, note="따로 센다")],
        {"responses": ["가"], "overlaps": []}),
    4: MetricOutcome(
        [metric_row("m4_cross_user_malfunction", value=0, limit=0, passed=True, trials=2, failures=0),
         metric_row("m4_success_rate", value=0.5, limit=0.95, passed=False, trials=2, failures=1)],
        {"attempts": []}),
    5: MetricOutcome([metric_row("m5_out_of_scope", value=0, limit=0, passed=True, trials=12, failures=0)], {"skipped": {}}),
    6: MetricOutcome([metric_row("m6_auto_release", value=0, limit=0, passed=True, trials=4, failures=0)], {"waits": {}}),
    7: MetricOutcome(
        [metric_row("m7_server_latency_median", value=0.12, limit=1.0, passed=True, trials=3),
         metric_row("m7_host_latency", note="미측정: LLM 호스트 실행 필요")],
        {"latencies": [0.1, 0.12, 0.2]}),
    9: MetricOutcome(
        [metric_row("m9_allocation_match", value=1.0, limit=1.0, passed=True, trials=5, failures=0)], {"sets": []}),
}
RUN_ORDER = [9, 1, 4, 5, 6, 7, 3]  # 지표 9는 서버 없이 먼저, 나머지는 1·4·5·6·7·3
ROW_NAMES = [  # 보고서의 행은 지표 번호순이다
    "m1_wrong_device_rate", "m3_double_occupancy", "m3_duplicate_execution", "m3_lock_errors",
    "m4_cross_user_malfunction", "m4_success_rate", "m5_out_of_scope", "m6_auto_release",
    "m7_server_latency_median", "m7_host_latency", "m9_allocation_match",
]
SAMPLE_COUNT = 3 + (7 + 4) + (2 + 2) + 12 + 4 + 3 + 5  # 행의 trials 합. 빈 칸은 더하지 않는다


# ── 도우미 ────────────────────────────────────────────────────────────


def write_eval(server: Path, text: str = EVAL_TOML, name: str = "server-metrics.toml") -> Path:
    path = server / "config" / "eval" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def server_argv(*extra: str, config: str = "server-metrics.toml") -> list[str]:
    return ["evaluate-server", config, *extra]


def report_files(server: Path, name: str = SERVER_NAME) -> tuple[Path, Path, Path]:
    """보고서 CSV, Markdown, 원자료 JSON의 경로."""
    reports = (server / "var" / "reports").resolve()
    return reports / f"{name}.csv", reports / f"{name}.md", reports / f"{name}-raw.json"


def read_report(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """보고서 CSV의 머리말 주석 줄과 지표 행."""
    lines = path.read_text(encoding="utf-8").splitlines()
    table = [line for line in lines if not line.startswith("#")]
    return [line for line in lines if line.startswith("#")], list(csv.DictReader(table))


def read_raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def reports_listing(server: Path) -> list[str]:
    reports = server / "var" / "reports"
    return sorted(path.name for path in reports.iterdir()) if reports.exists() else []


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluate, "now_utc", lambda: NOW)
    monkeypatch.setattr(report_module, "resolve_git_commit", lambda: "test-commit")  # git 프로세스를 띄우지 않는다


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`Harness`와 지표 함수를 가짜로 바꾼다.

    `events`는 불린 일을 순서대로 쌓는다: ("mosquitto", "find"), ("harness", "init", 작업 폴더, server_root), ("harness", "enter"),
    ("harness", "exit"), ("metric", 번호, 시드로 받은 무작위 수 또는 None), ("reset", 기기, 바꾼 시나리오 필드), ("query", SQL).
    `fail[번호]`에 예외를 넣으면 그 지표가 예외를 낸다. `start_error`를 넣으면 하네스가 시작에 실패하고,
    `mosquitto_error`를 넣으면 Mosquitto를 찾지 못한다. 이 PC에 Mosquitto가 있는지와 무관하게 돈다.
    """
    run = SimpleNamespace(events=[], fail={}, start_error=None, mosquitto_error=None)

    def find_mosquitto():
        run.events.append(("mosquitto", "find"))
        if run.mosquitto_error is not None:
            raise run.mosquitto_error
        return Path("mosquitto"), Path("mosquitto_passwd")

    class FakeHarness:
        def __init__(self, config, workdir, *, server_root):
            self.config, self.workdir = config, workdir
            run.events.append(("harness", "init", workdir, server_root))

        def __enter__(self):
            run.events.append(("harness", "enter"))
            if run.start_error is not None:
                raise run.start_error
            return self

        def __exit__(self, *exc):
            run.events.append(("harness", "exit"))

        def set_scenario(self, node_id, **changes):
            run.events.append(("reset", node_id, changes))

        def query(self, sql, *params):
            run.events.append(("query", sql))
            return [{"file": str(self.workdir / "np.db")}]

    def measure(number, rng=None):
        run.events.append(("metric", number, None if rng is None else rng.random()))
        if number in run.fail:
            raise run.fail[number]
        return OUTCOMES[number]

    monkeypatch.setattr(evaluate, "Harness", FakeHarness)
    monkeypatch.setattr(evaluate, "find_mosquitto", find_mosquitto)
    for number in (1, 4, 5, 6, 7):
        monkeypatch.setattr(evaluate, f"run_metric{number}", lambda harness, config, number=number: measure(number))
    monkeypatch.setattr(evaluate, "run_metric3", lambda harness, config, rng: measure(3, rng))
    monkeypatch.setattr(evaluate, "run_metric9", lambda config, rng: measure(9, rng))
    return run


@pytest.fixture
def fake_plots(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """`write_plots`를 대신한다. 불린 인자를 쌓고, 데이터가 있는 그래프마다 가짜 PNG를 쓴다 (matplotlib 없이도 돈다)."""
    calls: list[dict] = []

    def fake(out_dir, name, *, latencies, curve, rows):
        calls.append({"out_dir": out_dir, "name": name, "latencies": latencies, "curve": curve, "rows": rows})
        drawn = (("latency", latencies), ("reask", curve), ("pass", rows))
        paths = [out_dir / f"{name}-{kind}.png" for kind, data in drawn if data]
        for path in paths:
            path.write_bytes(b"png")
        return paths

    monkeypatch.setattr(evaluate, "write_plots", fake)
    return calls


def kinds(run: SimpleNamespace) -> list[tuple]:
    """`events`에서 일의 종류와 이름만 남긴다 (작업 폴더, 무작위 수, 시나리오 값은 뺀다)."""
    return [event[:2] for event in run.events]


def workdir_of(run: SimpleNamespace) -> Path:
    """하네스가 받은 작업 폴더."""
    [workdir] = [event[2] for event in run.events if event[:2] == ("harness", "init")]
    return workdir


# ── evaluate-server: 정상 실행 ────────────────────────────────────────


def test_NFR13_evaluate_server_writes_report(server_dir, run_cli, fake_run, fake_plots):
    eval_path = write_eval(server_dir).resolve()

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert reports_listing(server_dir) == sorted(
        [csv_path.name, md_path.name, raw_path.name, f"{SERVER_NAME}-latency.png", f"{SERVER_NAME}-pass.png"])
    comments, rows = read_report(csv_path)
    # 대시보드가 읽는 열이 있고, 행은 지표 번호순이다 (도는 순서가 아니다)
    assert {"metric", "passed", "trials", "note"} <= set(rows[0])
    assert [row["metric"] for row in rows] == ROW_NAMES
    assert rows[0] == {**rows[0], "value": "0.0", "limit": "0.025", "passed": "True", "trials": "3", "failures": "0",
                       "note": "시뮬레이터 측정"}
    assert [row["passed"] for row in rows] == ["True", "True", "True", "", "True", "False", "True", "True", "True", "", "True"]
    # 실패 0건인 행에는 write_report가 신뢰구간 상한을 덧붙인다
    assert float(rows[0]["confidence_bound_95"]) == pytest.approx(1 - 0.025 ** (1 / 3))
    # 무엇을, 어떤 시드와 순서로 쟀는지 모든 행에 남는다
    assert {(row["seed"], row["metric_order"], row["config_file"]) for row in rows} == {("42", "9,1,4,5,6,7,3", str(eval_path))}
    # 머리말: DB 경로는 하네스의 임시 DB, 표본 수는 행의 trials 합이다
    workdir = next(event[2] for event in fake_run.events if event[:2] == ("harness", "init"))
    assert comments == [
        f"# db_path: {workdir / 'np.db'}", f"# schema_version: {EXPECTED_SCHEMA_VERSION}", "# git_commit: test-commit",
        f"# sample_count: {SAMPLE_COUNT}", "# generated_at: 2026-10-27T05:00:00+00:00"]
    assert SAMPLE_COUNT == 42
    assert not workdir.exists()  # 임시 폴더는 지운다
    markdown = md_path.read_text(encoding="utf-8")
    assert f"- 입력 DB 경로: `{workdir / 'np.db'}`" in markdown and "- 표본 수: 42" in markdown
    assert "| m4_success_rate | 0.5 | 0.95 | False | 2 | 1 |" in markdown

    # 원자료: 시드, 도는 순서, 지표마다의 원자료. 키 순서가 도는 순서다
    raw = read_raw(raw_path)
    assert (raw["command"], raw["seed"], raw["order"], raw["config_file"]) == ("evaluate-server", 42, RUN_ORDER, str(eval_path))
    assert raw["generated_at"] == "2026-10-27T05:00:00+00:00" and "error" not in raw
    assert list(raw["metrics"]) == [f"m{number}" for number in RUN_ORDER]
    assert raw["metrics"] == {f"m{number}": OUTCOMES[number].raw for number in RUN_ORDER}

    # 그래프는 지표 7의 지연과 모든 행으로 그린다. 곡선은 없다
    [call] = fake_plots
    assert (call["out_dir"].resolve(), call["name"], call["latencies"], call["curve"]) == (
        csv_path.parent, SERVER_NAME, [0.1, 0.12, 0.2], None)
    assert [row["metric"] for row in call["rows"]] == ROW_NAMES

    # 대시보드는 `var/reports/`의 CSV를 읽는다
    evaluation = evaluation_of(csv_path.parent)
    assert evaluation["date"] == "2026-10-27"
    assert [metric["pass"] for metric in evaluation["metrics"]] == [
        True, True, True, None, True, False, True, True, True, None, True]

    assert out.splitlines()[:7] == [f"지표 {number} 측정 중" for number in RUN_ORDER]
    assert out.splitlines()[7:11] == [
        "지표  값  기준  통과", "m1_wrong_device_rate  0.0000  0.0250  통과", "m3_double_occupancy  -  0.0000  통과",
        "m3_duplicate_execution  1.0000  1.0000  통과"]
    assert "m4_success_rate  0.5000  0.9500  미달" in out.splitlines()
    assert "m7_host_latency  -  -  미측정: LLM 호스트 실행 필요" in out.splitlines()
    assert out.splitlines()[-5:] == [f"작성: {path}" for path in (
        csv_path, md_path, raw_path, csv_path.parent / f"{SERVER_NAME}-latency.png", csv_path.parent / f"{SERVER_NAME}-pass.png")]


def test_NFR13_evaluate_server_runs_metrics_in_order_and_resets_devices_between(server_dir, run_cli, fake_run, fake_plots):
    write_eval(server_dir)

    # 지표 번호를 어떤 순서로 줘도 도는 순서는 같다: 서버 없이 도는 9가 먼저, 하네스 안에서는 부하를 거는 3이 마지막이다
    code, out, err = run_cli(server_argv("--metrics", "3,7,1,9,6,5,4"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    reset = [("reset", "locker-1"), ("reset", "light-1")]  # 지표 사이마다 모든 기기를 정리한다. 처음과 마지막에는 하지 않는다
    assert kinds(fake_run) == [
        ("mosquitto", "find"),  # 아무것도 재기 전에 하네스를 띄울 수 있는지부터 본다
        ("metric", 9), ("harness", "init"), ("harness", "enter"),
        ("metric", 1), *reset, ("metric", 4), *reset, ("metric", 5), *reset, ("metric", 6), *reset, ("metric", 7), *reset,
        ("metric", 3), ("query", "PRAGMA database_list"), ("harness", "exit")]
    # 정리는 시나리오를 처음 상태(구동 성공, 비콘 없음)로 되돌린다
    fresh = Scenario(actuation_sec=0.1)
    changes = [event[2] for event in fake_run.events if event[0] == "reset"]
    assert len(changes) == 10
    assert all(dataclasses.replace(Scenario(0.5, final="failed", drop_final_result=True, result_delay_sec=9.0,
                                            storage_error=True, beacons=()), **change) == fresh for change in changes)
    assert read_raw(report_files(server_dir)[2])["order"] == RUN_ORDER


def test_NFR13_evaluate_server_runs_harness_in_this_checkout(server_dir, run_cli, fake_run, fake_plots):
    write_eval(server_dir)

    assert run_cli(server_argv("--metrics", "1"), cwd=server_dir)[0] == EXIT_OK

    [(_, _, workdir, server_root)] = [event for event in fake_run.events if event[:2] == ("harness", "init")]
    # 하네스는 이 checkout의 server/에서 코드와 서버 설정 예시를 읽는다
    assert server_root == evaluate.SERVER_ROOT
    assert (server_root / "config" / "server.example.toml").is_file()
    assert Path(evaluate.__file__).resolve() == server_root / "src" / "nearpilot" / "cli" / "evaluate.py"
    assert workdir.name.startswith("np-eval-") and str(workdir).isascii()


@pytest.mark.parametrize(("argv", "seed"), [([], 42), (["--seed", "7"], 7), (["--seed", "-3"], -3)])
def test_NFR13_evaluate_server_seeds_metrics_3_and_9(server_dir, run_cli, fake_run, fake_plots, argv, seed):
    write_eval(server_dir)  # 설정 파일의 seed는 42다

    code, _, err = run_cli(server_argv(*argv), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    # 무작위 입력을 받는 지표 3과 9는 각자 `random.Random(시드)`를 받는다. 어느 지표를 같이 재는지가 입력을 바꾸지 않는다
    draws = {event[1]: event[2] for event in fake_run.events if event[0] == "metric" and event[2] is not None}
    assert draws == {3: random.Random(seed).random(), 9: random.Random(seed).random()}
    csv_path, _, raw_path = report_files(server_dir)
    assert {row["seed"] for row in read_report(csv_path)[1]} == {str(seed)}
    assert read_raw(raw_path)["seed"] == seed


def test_NFR13_evaluate_server_metric_9_alone_needs_no_harness(server_dir, run_cli, fake_run, fake_plots):
    # 지표 9는 서버 없이 잰다. 다른 지표가 읽는 횟수와 통과 기준, RSSI가 설정에 없어도 된다
    text = "\n".join(line for line in EVAL_TOML.splitlines()
                     if not line.startswith(("m1_", "m3_", "m4_", "m5_", "m6_", "m7_", "near", "far", "absent")))
    write_eval(server_dir, text)

    code, out, err = run_cli(server_argv("--metrics", "9"), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert kinds(fake_run) == [("metric", 9)]  # 하네스를 만들지 않았다
    comments, rows = read_report(csv_path)
    assert [row["metric"] for row in rows] == ["m9_allocation_match"]
    assert rows[0]["metric_order"] == "9"
    assert comments[0] == "# db_path: 없음 (서버를 띄우지 않았다)" and comments[3] == "# sample_count: 5"
    assert [call["latencies"] for call in fake_plots] == [None]  # 지표 7이 없어서 지연 그래프는 없다
    assert reports_listing(server_dir) == sorted([csv_path.name, md_path.name, raw_path.name, f"{SERVER_NAME}-pass.png"])


def test_NFR13_evaluate_server_only_the_chosen_metrics_run(server_dir, run_cli, fake_run, fake_plots):
    write_eval(server_dir)

    code, _, err = run_cli(server_argv("--metrics", "7,1"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert [event[1] for event in fake_run.events if event[0] == "metric"] == [1, 7]
    rows = read_report(report_files(server_dir)[0])[1]
    assert [row["metric"] for row in rows] == ["m1_wrong_device_rate", "m7_server_latency_median", "m7_host_latency"]
    assert {row["metric_order"] for row in rows} == {"1,7"}
    assert fake_plots[0]["latencies"] == [0.1, 0.12, 0.2]


def test_NFR13_evaluate_server_report_time_is_when_the_run_ended(server_dir, run_cli, fake_run, fake_plots, monkeypatch):
    # 대시보드는 generated_at이 가장 늦은 보고서를 읽는다. 오래 걸린 측정은 시작한 시각이 아니라 끝난 시각이다.
    write_eval(server_dir)
    ticks = iter([NOW, NOW + timedelta(minutes=20)])  # 기본 이름의 날짜, 보고서 생성 시각
    monkeypatch.setattr(evaluate, "now_utc", lambda: next(ticks))

    code, _, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    csv_path, _, raw_path = report_files(server_dir)
    assert "# generated_at: 2026-10-27T05:20:00+00:00" in read_report(csv_path)[0]
    assert read_raw(raw_path)["generated_at"] == "2026-10-27T05:20:00+00:00"


def test_NFR13_evaluate_server_out_names_the_report(server_dir, run_cli, fake_run, fake_plots):
    write_eval(server_dir)

    code, out, err = run_cli(server_argv("--out", "demo-run", "--seed", "1"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert reports_listing(server_dir) == ["demo-run-latency.png", "demo-run-pass.png", "demo-run-raw.json", "demo-run.csv",
                                           "demo-run.md"]


@pytest.mark.parametrize("config", ["server-metrics.toml", "config/eval/server-metrics.toml", "ABSOLUTE"])
def test_NFR13_evaluate_server_config_is_a_name_or_a_path(server_dir, run_cli, fake_run, fake_plots, config):
    # 이름이면 설정 파일 옆 eval/ 폴더에서 찾고, 경로면 실행 폴더 기준이다 (다른 명령의 파일 인자와 같다)
    path = write_eval(server_dir)
    argument = str(path) if config == "ABSOLUTE" else config

    code, _, err = run_cli(server_argv("--metrics", "9", config=argument), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert read_report(report_files(server_dir)[0])[1][0]["config_file"] == str(path.resolve())


def test_NFR13_evaluate_server_example_config_is_accepted(server_dir, run_cli, fake_run, fake_plots):
    # 저장소의 예시 설정으로 모든 지표를 잴 수 있다 (하네스는 가짜다)
    code, _, err = run_cli(server_argv(config=str(EXAMPLE)), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert [event[1] for event in fake_run.events if event[0] == "metric"] == RUN_ORDER


def test_NFR13_evaluate_server_required_settings_match_the_example_config():
    # 하네스를 띄우기 전에 확인하는 키 표가 예시 설정과 어긋나지 않는다. 지표 모듈이 읽는 키를 바꾸면서 표를 놓치면 여기서 잡힌다.
    config = load_eval_config(EXAMPLE)
    reads = [set().union(*(evaluate._READS[number][section] for number in evaluate.SERVER_METRICS)) for section in range(3)]

    assert set(config.counts) == reads[0]
    # 지표 7의 호스트 쪽 기준은 이 도구가 재지 않는다 (예시에 표의 행을 보존하려고 둔 값)
    assert {key for key in config.acceptance if not key.startswith("m7_host_")} == reads[1]
    assert reads[2] <= set(config.rssi)  # 예시의 absent는 지표 모듈이 읽지 않는다


def test_NFR13_evaluate_server_plot_failure_is_only_a_warning(server_dir, run_cli, fake_run, monkeypatch):
    write_eval(server_dir)

    def broken(*args, **kwargs):
        raise RuntimeError("그림을 저장할 수 없다")

    monkeypatch.setattr(evaluate, "write_plots", broken)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert code == EXIT_OK
    assert err == "경고: 그래프를 만들지 못했다 (그림을 저장할 수 없다)\n"
    assert reports_listing(server_dir) == sorted([csv_path.name, md_path.name, raw_path.name])  # 보고서와 원자료는 쓴다
    assert out.splitlines()[-3:] == [f"작성: {path}" for path in (csv_path, md_path, raw_path)]


def test_NFR13_evaluate_server_without_matplotlib_writes_report_and_warns(server_dir, run_cli, fake_run, monkeypatch, caplog):
    write_eval(server_dir)
    monkeypatch.setitem(sys.modules, "matplotlib", None)  # 가져올 수 없다. 이 PC에 있어도 없는 것으로 본다

    with caplog.at_level("WARNING", logger="tools.evaluation.plots"):
        code, out, err = run_cli(server_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert [record.getMessage() for record in caplog.records] == [
        'matplotlib이 없어 그래프 2장을 만들지 않았다. pip install -e ".[eval]"로 설치한다']
    assert reports_listing(server_dir) == sorted([csv_path.name, md_path.name, raw_path.name])
    assert out.splitlines()[-3:] == [f"작성: {path}" for path in (csv_path, md_path, raw_path)]


def test_NFR13_evaluate_server_writes_graphs(server_dir, run_cli, fake_run):
    pytest.importorskip("matplotlib")
    write_eval(server_dir)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    reports = report_files(server_dir)[0].parent
    assert (code, err) == (EXIT_OK, "")
    for kind in ("latency", "pass"):
        assert (reports / f"{SERVER_NAME}-{kind}.png").read_bytes().startswith(PNG_SIGNATURE)
        assert f"작성: {reports / f'{SERVER_NAME}-{kind}.png'}" in out.splitlines()
    assert not (reports / f"{SERVER_NAME}-reask.png").exists()  # 곡선은 evaluate-reask가 그린다


# ── evaluate-server: 설정과 인자 오류 (종료 코드 2, 서버를 띄우기 전) ───


BAD_CONFIGS = [
    pytest.param("not toml = [", "TOML로 읽을 수 없다", id="not-toml"),
    pytest.param("unknown_key = 1\n" + EVAL_TOML, "unknown_key: 알 수 없는 키", id="unknown-key"),
    pytest.param(EVAL_TOML.replace("m3_clients = 2\n", "m3_clients = 0\n"), "counts.m3_clients: 1 이상의 정수여야 한다 (0)",
                 id="count-below-one"),
    pytest.param(EVAL_TOML.replace("m1_max_wrong_rate = 0.025", "m1_max_wrong_rate = 2.5"),
                 "acceptance.m1_max_wrong_rate: 1 이하여야 한다 (2.5)", id="rate-above-one"),
    pytest.param(EVAL_TOML.replace("m3_clients = 2\n", ""), "counts.m3_clients: 지표 3이(가) 읽는 값이 없다", id="missing-count"),
    pytest.param(EVAL_TOML.replace("m7_max_p95_sec = 2.0\n", ""), "acceptance.m7_max_p95_sec: 지표 7이(가) 읽는 값이 없다",
                 id="missing-acceptance"),
    pytest.param(EVAL_TOML.replace("near = -53.0\n", ""), "rssi.near: 지표 1, 3, 4, 5, 6, 7이(가) 읽는 값이 없다",
                 id="missing-rssi"),
    pytest.param(EVAL_TOML.replace("m9_sets = 5\n", "").replace("m6_max_auto_release = 0\n", ""),
                 "acceptance.m6_max_auto_release: 지표 6이(가) 읽는 값이 없다; counts.m9_sets: 지표 9이(가) 읽는 값이 없다",
                 id="missing-several"),  # 지표 번호순으로 모두 알린다
]


@pytest.mark.parametrize(("text", "message"), BAD_CONFIGS)
def test_NFR13_evaluate_server_rejects_bad_config(server_dir, run_cli, fake_run, fake_plots, text, message):
    write_eval(server_dir, text)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith("평가 설정 오류: ") and message in err and err.count("\n") == 1
    assert fake_run.events == []  # 하네스도 지표도 시작하지 않았다
    assert not (server_dir / "var").exists()  # 보고서 폴더도 만들지 않는다


def test_NFR13_evaluate_server_checks_only_the_chosen_metrics_settings(server_dir, run_cli, fake_run, fake_plots):
    write_eval(server_dir, EVAL_TOML.replace("m3_clients = 2\n", ""))

    code, out, err = run_cli(server_argv("--metrics", "1,4,5"), cwd=server_dir)  # 지표 3의 횟수는 이 지표들이 읽지 않는다

    assert (code, err) == (EXIT_OK, "")
    assert [event[1] for event in fake_run.events if event[0] == "metric"] == [1, 4, 5]


def test_NFR13_evaluate_server_missing_config_file_is_input_error(server_dir, run_cli, fake_run):
    code, out, err = run_cli(server_argv(), cwd=server_dir)
    eval_dir = (server_dir / "config" / "eval").resolve()
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"평가 설정 파일이 없다: {eval_dir / 'server-metrics.toml'}. {eval_dir}에 있는 파일: (없음)\n"

    write_eval(server_dir, name="other.toml")
    code, out, err = run_cli(server_argv(config="config/eval/nowhere.toml"), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"평가 설정 파일이 없다: {eval_dir / 'nowhere.toml'}\n"
    assert fake_run.events == [] and not (server_dir / "var").exists()


@pytest.mark.parametrize(("metrics", "message"), [
    ("", "--metrics는 쉼표로 이은 지표 번호다 (예: 1,3,9): ''"),
    ("a", "--metrics는 쉼표로 이은 지표 번호다 (예: 1,3,9): 'a'"),
    ("1,,3", "--metrics는 쉼표로 이은 지표 번호다 (예: 1,3,9): '1,,3'"),
    ("2", "--metrics의 지표 2은(는) evaluate-server가 재지 않는다. 잴 수 있는 지표: 1, 3, 4, 5, 6, 7, 9"),
    ("1,8,0,2", "--metrics의 지표 0, 2, 8은(는) evaluate-server가 재지 않는다. 잴 수 있는 지표: 1, 3, 4, 5, 6, 7, 9"),
    ("1,3,1", "--metrics에 같은 지표가 두 번 있다: 1,3,1"),
])
def test_NFR13_evaluate_server_rejects_bad_metrics(server_dir, run_cli, fake_run, metrics, message):
    write_eval(server_dir)

    code, out, err = run_cli(server_argv("--metrics", metrics), cwd=server_dir)

    assert (code, out, err) == (EXIT_INVALID, "", message + "\n")
    assert fake_run.events == [] and not (server_dir / "var").exists()


@pytest.mark.parametrize("name", ["a/b", "a\\b", "../x", "x/", "/x", "C:\\x"])
def test_NFR13_evaluate_server_out_must_be_a_name(server_dir, run_cli, fake_run, name):
    # 보고서는 `var/reports/` 안에만 쓴다. 경로를 주면 다른 곳에 쓰거나 폴더 밖을 가리키게 되므로 서버를 띄우기 전에 거부한다.
    write_eval(server_dir)

    code, out, err = run_cli(server_argv("--out", name), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"--out은 경로가 아니라 이름이다. /나 \\ 없이 쓴다: {name}\n"
    assert fake_run.events == [] and not (server_dir / "var").exists()


# ── evaluate-server: 이미 있는 보고서 (종료 코드 2, 서버를 띄우기 전) ───


def existing_path(server: Path, which: str) -> Path:
    csv_path, md_path, raw_path = report_files(server)
    return {"csv": csv_path, "md": md_path, "raw": raw_path, "latency": csv_path.with_name(f"{SERVER_NAME}-latency.png"),
            "pass": csv_path.with_name(f"{SERVER_NAME}-pass.png")}[which]


@pytest.mark.parametrize("existing", ["csv", "md", "raw", "latency", "pass"])
def test_NFR13_evaluate_server_existing_report_requires_force(server_dir, run_cli, fake_run, fake_plots, existing):
    write_eval(server_dir)
    present = existing_path(server_dir, existing)
    present.parent.mkdir(parents=True)
    present.write_text("사람이 쓴 파일", encoding="utf-8")

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"보고서가 이미 있다: {present}. --force로 덮어쓴다\n"  # 있는 파일만 알린다
    assert fake_run.events == []  # 하네스도 지표도 시작하지 않았다
    assert present.read_text(encoding="utf-8") == "사람이 쓴 파일" and reports_listing(server_dir) == [present.name]

    code, out, err = run_cli(server_argv("--force"), cwd=server_dir)  # --force면 있는 쪽도 덮어쓴다
    assert (code, err) == (EXIT_OK, "")
    assert present.read_bytes() != "사람이 쓴 파일".encode()
    assert len(reports_listing(server_dir)) == 5


def test_NFR13_evaluate_server_existing_reports_are_all_listed(server_dir, run_cli, fake_run):
    write_eval(server_dir)
    csv_path, md_path, raw_path = report_files(server_dir)
    csv_path.parent.mkdir(parents=True)
    for path in (csv_path, raw_path, md_path):
        path.write_text("사람이 쓴 파일", encoding="utf-8")

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"보고서가 이미 있다: {csv_path}, {md_path}, {raw_path}. --force로 덮어쓴다\n"
    assert fake_run.events == []


def test_NFR13_evaluate_server_graph_of_a_metric_not_measured_does_not_block(server_dir, run_cli, fake_run, fake_plots):
    # 지표 7을 재지 않으면 지연 그래프를 쓰지 않으므로, 같은 이름의 옛 지연 그래프가 있어도 막지 않는다
    write_eval(server_dir)
    old = existing_path(server_dir, "latency")
    old.parent.mkdir(parents=True)
    old.write_text("옛 그래프", encoding="utf-8")

    code, _, err = run_cli(server_argv("--metrics", "1"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert old.read_text(encoding="utf-8") == "옛 그래프"


@pytest.mark.parametrize("created", ["csv", "md"])
def test_NFR13_evaluate_server_report_created_during_run_is_not_overwritten(server_dir, run_cli, fake_run, fake_plots,
                                                                           monkeypatch, created):
    # 측정은 오래 걸린다. 그동안 다른 곳에서 같은 이름의 보고서를 만들었으면 덮어쓰지 않는다.
    write_eval(server_dir)
    csv_path, md_path, raw_path = report_files(server_dir)
    other = {"csv": csv_path, "md": md_path}[created]
    real = evaluate.run_metric7

    def other_process_writes(harness, config):
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("다른 곳에서 만든 파일", encoding="utf-8")
        return real(harness, config)

    monkeypatch.setattr(evaluate, "run_metric7", other_process_writes)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, out.count("작성:")) == (EXIT_INVALID, 0)
    # 막힌 파일만 알린다. 오래 걸려 잰 값이니 원자료는 쓰고 그 사실을 알린다 (나머지 보고서와 그래프는 쓰지 않는다)
    assert err == f"보고서가 이미 있다: {other}. --force로 덮어쓴다 (측정한 원자료는 {raw_path}에 썼다)\n"
    assert other.read_text(encoding="utf-8") == "다른 곳에서 만든 파일"
    assert reports_listing(server_dir) == sorted([other.name, raw_path.name])
    assert list(read_raw(raw_path)["metrics"]) == [f"m{number}" for number in RUN_ORDER]
    assert ("harness", "exit") in fake_run.events


def test_NFR13_evaluate_server_raw_created_during_run_is_not_overwritten(server_dir, run_cli, fake_run, fake_plots, monkeypatch):
    # 원자료를 먼저 쓴다. 그 이름의 파일이 측정 중에 생겼으면 덮어쓰지 않고, 아무것도 쓰지 않는다.
    write_eval(server_dir)
    raw_path = report_files(server_dir)[2]
    real = evaluate.run_metric7

    def other_process_writes(harness, config):
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text("다른 곳에서 만든 파일", encoding="utf-8")
        return real(harness, config)

    monkeypatch.setattr(evaluate, "run_metric7", other_process_writes)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, out.count("작성:")) == (EXIT_INVALID, 0)
    assert err == f"보고서가 이미 있다: {raw_path}. --force로 덮어쓴다\n"
    assert raw_path.read_text(encoding="utf-8") == "다른 곳에서 만든 파일" and reports_listing(server_dir) == [raw_path.name]


@pytest.mark.parametrize("failing", ["_write_raw", "write_report"])
def test_NFR13_evaluate_server_unwritable_report_is_exit_2(server_dir, run_cli, fake_run, fake_plots, monkeypatch, failing):
    write_eval(server_dir)
    csv_path, _, raw_path = report_files(server_dir)

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(evaluate, failing, denied)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    # 원자료를 먼저 쓰므로, 보고서를 쓰다 막혀도 원자료는 남는다. 원자료부터 막히면 아무것도 없다
    blocked, listing = (raw_path, []) if failing == "_write_raw" else (csv_path, [raw_path.name])
    assert (code, out.count("작성:")) == (EXIT_INVALID, 0)
    assert err == f"보고서를 쓸 수 없다: {blocked} (Permission denied)\n"
    assert reports_listing(server_dir) == listing


def test_NFR13_evaluate_server_zero_trial_row_fails_after_keeping_raw(server_dir, run_cli, fake_run, fake_plots, monkeypatch):
    # 지표 3에서 기기를 구동한 요청이 하나도 없으면 시험 횟수 0, 실패 0인 행이 나온다. write_report가 그 행의 신뢰구간을 내지 못한다.
    # 앞 단계의 행이라 여기서 고치지 않는다. 명령은 트레이스백 대신 사유를 알리고, 오래 걸려 얻은 원자료는 남긴다.
    write_eval(server_dir)
    empty = MetricOutcome([metric_row("m3_duplicate_execution", value=0, limit=1, passed=True, trials=0, failures=0)],
                          {"responses": []})
    monkeypatch.setattr(evaluate, "run_metric3", lambda harness, config, rng: empty)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    raw_path = report_files(server_dir)[2]
    assert (code, out.count("작성:")) == (EXIT_FAILED, 0)
    assert err == f"보고서를 만들 수 없다: trials 는 1 이상이어야 한다: 0. 원자료는 {raw_path}에 썼다\n"
    assert reports_listing(server_dir) == [raw_path.name] and fake_plots == []
    assert list(read_raw(raw_path)["metrics"]) == [f"m{number}" for number in RUN_ORDER]


# ── evaluate-server: 실행 실패 ────────────────────────────────────────


def test_NFR13_evaluate_server_harness_failure_is_exit_1(server_dir, run_cli, fake_run, fake_plots):
    write_eval(server_dir)
    fake_run.start_error = HarnessError("mosquitto 실행 파일이 없다 (찾지 못했다). PATH에 두거나 MOSQUITTO_EXECUTABLE에 경로를 준다")

    code, out, err = run_cli(server_argv("--metrics", "1,4"), cwd=server_dir)

    assert code == EXIT_FAILED
    assert err == "서버 지표 평가에 실패했다: mosquitto 실행 파일이 없다 (찾지 못했다). PATH에 두거나 MOSQUITTO_EXECUTABLE에 경로를 준다\n"
    assert kinds(fake_run) == [("mosquitto", "find"), ("harness", "init"), ("harness", "enter")]  # 지표를 하나도 돌리지 않았다
    assert reports_listing(server_dir) == [] and fake_plots == []  # 끝난 지표가 없으니 남길 원자료도 없다
    assert not workdir_of(fake_run).exists()


@pytest.mark.parametrize(("error", "code", "message"), [
    pytest.param(HarnessError("serve이(가) 끝났다 (종료 코드 1)\n로그 끝"), EXIT_FAILED,
                 "서버 지표 평가에 실패했다: serve이(가) 끝났다 (종료 코드 1)\n로그 끝\n", id="harness-error"),
    pytest.param(EvalConfigError("users: 방문자(role = visitor)가 3명 이상 있어야 한다 (지표 4의 장면에 선다)"), EXIT_INVALID,
                 "평가 설정 오류: users: 방문자(role = visitor)가 3명 이상 있어야 한다 (지표 4의 장면에 선다)\n",
                 id="config-error"),
    pytest.param(KeyboardInterrupt(), 130, "Ctrl+C로 멈췄다.\n", id="ctrl-c"),
])
def test_NFR13_evaluate_server_failure_keeps_raw_so_far(server_dir, run_cli, fake_run, fake_plots, error, code, message):
    write_eval(server_dir)
    fake_run.fail[5] = error  # 지표 9, 1, 4가 끝난 뒤 지표 5가 도중에 실패한다

    got_code, out, err = run_cli(server_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert got_code == code
    assert err == f"지금까지의 원자료: {raw_path}\n{message}"
    # 하네스를 끈다 (임시 폴더도 지운다). 지표 5 뒤의 지표는 돌리지 않는다
    assert kinds(fake_run)[-2:] == [("metric", 5), ("harness", "exit")]
    assert not workdir_of(fake_run).exists()
    # 보고서(CSV, Markdown)와 그래프는 쓰지 않고, 끝난 지표의 원자료와 실패 사유만 남긴다
    assert reports_listing(server_dir) == [raw_path.name] and fake_plots == []
    raw = read_raw(raw_path)
    assert (raw["command"], raw["seed"], raw["order"]) == ("evaluate-server", 42, RUN_ORDER)
    assert list(raw["metrics"]) == ["m9", "m1", "m4"]
    assert raw["metrics"] == {f"m{number}": OUTCOMES[number].raw for number in (9, 1, 4)}
    assert raw["error"] == f"{type(error).__name__}: {error}"
    assert raw["generated_at"] == "2026-10-27T05:00:00+00:00"
    assert "작성:" not in out


def test_NFR13_evaluate_server_failure_after_start_keeps_metric_9(server_dir, run_cli, fake_run, fake_plots):
    # 서버 없이 도는 지표 9는 하네스 시작 전에 끝난다. 하네스가 못 뜨면 그 원자료만 남는다.
    write_eval(server_dir)
    fake_run.start_error = HarnessError("broker이(가) 30초 안에 포트를 열지 않았다")

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    _, _, raw_path = report_files(server_dir)
    assert code == EXIT_FAILED and reports_listing(server_dir) == [raw_path.name]
    assert list(read_raw(raw_path)["metrics"]) == ["m9"]
    assert err.startswith(f"지금까지의 원자료: {raw_path}\n서버 지표 평가에 실패했다: broker이(가)")


def test_NFR13_evaluate_server_failure_does_not_hide_the_error_when_raw_cannot_be_written(server_dir, run_cli, fake_run,
                                                                                       monkeypatch):
    write_eval(server_dir)
    fake_run.fail[4] = HarnessError("서버가 끝났다")
    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(evaluate, "_write_raw", denied)

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    _, _, raw_path = report_files(server_dir)
    assert code == EXIT_FAILED
    assert err == (f"경고: 지금까지의 원자료를 남기지 못했다: {raw_path} (Permission denied)\n"
                   "서버 지표 평가에 실패했다: 서버가 끝났다\n")


def test_NFR13_evaluate_server_requires_mosquitto_before_measuring(server_dir, run_cli, fake_run, fake_plots):
    # spec 5절: Mosquitto 실행 파일이 없으면 시작 전에 오류와 종료 코드 1이다. 서버 없이 도는 지표 9도 재지 않고, 원자료도 남기지 않는다.
    write_eval(server_dir)
    fake_run.mosquitto_error = HarnessError("mosquitto 실행 파일이 없다 (찾지 못했다). PATH에 두거나 MOSQUITTO_EXECUTABLE에 경로를 준다")

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "서버 지표 평가에 실패했다: mosquitto 실행 파일이 없다 (찾지 못했다). PATH에 두거나 MOSQUITTO_EXECUTABLE에 경로를 준다\n"
    assert kinds(fake_run) == [("mosquitto", "find")]
    assert reports_listing(server_dir) == [] and fake_plots == []


def test_NFR13_evaluate_server_temp_folder_must_be_ascii(server_dir, run_cli, fake_run, tmp_path, monkeypatch):
    # Mosquitto가 한글 경로의 설정 파일을 열지 못한다 (하네스 문서). 지표 9도 재기 전에, 하네스를 띄우기 전에 알린다.
    write_eval(server_dir)
    korean = tmp_path / "임시"
    korean.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(korean))

    code, out, err = run_cli(server_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"임시 폴더 경로에 ASCII가 아닌 글자가 있다: {korean}. TMP나 TMPDIR을 ASCII 경로로 바꾼다\n"
    assert kinds(fake_run) == [("mosquitto", "find")]  # 지표도 하네스도 시작하지 않았다
    assert list(korean.iterdir()) == [] and reports_listing(server_dir) == []


def test_NFR13_evaluate_commands_are_registered(run_cli, tmp_path):
    code, out, err = run_cli(["evaluate-server", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and "EVAL_TOML" in out and "--metrics" in out and "--seed" in out and "--force" in out
    code, out, err = run_cli(["evaluate-reask", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and "PLACE/VERSION" in out and "--split" in out and "--ratios" in out and "--force" in out
    code, out, err = run_cli(["evaluate-server"], cwd=tmp_path)  # 설정 파일 인자가 빠졌다
    assert code == EXIT_INVALID and "EVAL_TOML" in err


# ── evaluate-reask ────────────────────────────────────────────────────

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
LAYOUT = PlaceLayout(
    "place-a",
    {"locker-1": AnchorLayout(0.0, 0.0, 0.0, 0.5), "locker-2": AnchorLayout(4.0, 0.0, 4.0, 0.5)},
    none_points=((2.0, 3.0),),
)
# 손으로 쓴 모델 (`test_cli_eval.py`와 같다): 기기마다 A = −50, n = 2, 흔들림 2 dB. 아래 STATE_RSSI는 이 모델이 확신하는 RSSI라서
# 정답 상태의 확률이 1로 포화된다. 확률이 0 아니면 1이라 곡선의 값을 손으로 센다.
HAND_MODEL = ProximityModel(
    place_id="place-a", model_version="v1", calib_version="c1", window_sec=2.5, stay=0.9, min_distance_m=0.3,
    calibration_method="temperature", calibration_tau=1.0, validation={},
    nodes={
        node_id: NodeModel(node_id, a.x, a.y, a.front_x, a.front_y, A=-50.0, n=2.0, sigma=2.0, none_rssi=-90.0,
                           none_sigma=2.0)
        for node_id, a in LAYOUT.nodes.items()
    },
)
STATE_RSSI = {  # 방문자가 선 상태 → 앵커마다 받는 RSSI
    "locker-1": {"locker-1": -44.0, "locker-2": -62.0},
    "locker-2": {"locker-1": -62.0, "locker-2": -44.0},
    "none": {"locker-1": -90.0, "locker-2": -90.0},
}
PER_RUN = 18  # 20초 회차, window 2.5초, 칸 1초 → 평가 시각은 2.5초부터 19.5초까지


def hand_run(index: int, label: str, *, looks_like: str | None = None, seconds: int = 20, in_progress: bool = False) -> tuple:
    """`make_collection_db`의 회차 한 건. `looks_like`는 라벨과 다른 상태의 RSSI를 쓴다 (라벨을 잘못 단 회차)."""
    frames = {node_id: [rssi] * seconds for node_id, rssi in STATE_RSSI[looks_like or label].items()}
    return label, T0 + timedelta(seconds=60 * index), None if in_progress else seconds, frames


def hand_runs() -> list[tuple]:
    """맞게 단 회차 둘(locker-1, locker-2), none 회차, locker-2 앞의 RSSI에 locker-1을 라벨로 단 회차 하나."""
    return [hand_run(0, "locker-1"), hand_run(1, "locker-2"), hand_run(2, "none"), hand_run(3, "locker-1", looks_like="locker-2")]


def write_model(server: Path, model: ProximityModel = HAND_MODEL) -> Path:
    return write_model_file(server / "var" / "models", model)


def write_split(server: Path, train, validation, test, *, place_id: str = "place-a", name: str = "place-a-seed42.json") -> Path:
    result = SplitResult(place_id, 42, (0.6, 0.2, 0.2), tuple(train), tuple(validation), tuple(test), (), {})
    path = server / "var" / "splits" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(split_to_json(result, created_at=T0, db_path="nearpilot.db"), encoding="utf-8", newline="\n")
    return path


def build_hand(server: Path, make_collection_db, runs: list[tuple] | None = None) -> tuple[str, ...]:
    """DB에 `runs`(기본은 `hand_runs()`)를 만들고, 모두를 시험 회차로 둔 분할 결과 파일과 손으로 쓴 모델을 쓴다. 시험 회차 ID를 돌려준다."""
    ids = make_collection_db(server, LAYOUT, hand_runs() if runs is None else runs)
    write_model(server)
    write_split(server, (), (), ids)
    return tuple(ids)


def reask_argv(*extra: str, model: str = "place-a/v1") -> list[str]:
    return ["evaluate-reask", model, "--split", "place-a-seed42.json", *extra]


def schema_version(server: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(server / "var" / "nearpilot.db")
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def hand(server_dir, make_collection_db) -> tuple[str, ...]:
    """`hand_runs()` 네 건이 모두 시험 회차(`col-001`~`col-004`)인 DB와 손으로 쓴 모델 v1, 이름으로 찾는 기본 위치의 분할 결과."""
    ids = build_hand(server_dir, make_collection_db)
    assert ids == ("col-001", "col-002", "col-003", "col-004")
    return ids


def test_NFR06_evaluate_reask_writes_report(server_dir, run_cli, hand, fake_plots):
    # 회차 넷, 평가 시각 18개씩 → 표본 72개. none 회차는 기기 확률이 모두 0이라 모든 비용 비율에서 재질문(18개)이고,
    # locker-2 앞의 RSSI에 locker-1을 단 회차는 모델이 locker-2를 확신해서 구동하는 오작동(18개)이다. 비용 비율과 상관없이 둘 다 18/72다.
    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir, REASK_NAME)
    assert (code, err) == (EXIT_OK, "")
    assert reports_listing(server_dir) == sorted([csv_path.name, md_path.name, raw_path.name, f"{REASK_NAME}-reask.png"])
    comments, rows = read_report(csv_path)
    assert {"metric", "passed", "trials", "note"} <= set(rows[0])  # 대시보드가 읽는 열
    assert [row["metric"] for row in rows] == ["m2_reask_curve", "m2_reask_r1", "m2_reask_r2", "m2_reask_r5", "m2_reask_r10"]
    curve = rows[0]
    assert (curve["value"], curve["limit"], curve["passed"], curve["trials"], curve["failures"]) == ("", "", "", "72", "")
    for row, threshold in zip(rows[1:], ("0.5000", "0.6667", "0.8333", "0.9091")):  # 확률 임계값 r / (1 + r)
        assert (row["value"], row["limit"], row["passed"], row["trials"], row["failures"]) == ("0.25", "", "", "72", "18")
        assert f"재질문률 0.2500, 확률 임계값 {threshold}" in row["note"] and "미판정" in row["note"]
    # 무엇을 평가했는지 모든 행에 남는다 (eval과 같다)
    assert {(row["place_id"], row["model_version"], row["calib_version"], row["split_file"]) for row in rows} == {
        ("place-a", "v1", "c1", str((server_dir / "var" / "splits" / "place-a-seed42.json").resolve()))}
    assert comments == [
        f"# db_path: {(server_dir / 'var' / 'nearpilot.db').resolve()}", f"# schema_version: {schema_version(server_dir)}",
        "# git_commit: test-commit", "# sample_count: 72", "# generated_at: 2026-10-27T05:00:00+00:00"]
    raw = read_raw(raw_path)
    assert (raw["command"], raw["model"], raw["calib_version"], raw["ratios"], raw["samples"]) == (
        "evaluate-reask", "place-a/v1", "c1", [1.0, 2.0, 5.0, 10.0], 72)
    assert raw["curve"] == [{"cost_ratio": float(r), "threshold": r / (1 + r), "reask_rate": 0.25, "misoperation_rate": 0.25}
                            for r in (1, 2, 5, 10)]
    # 그래프는 곡선만 그린다. 통과 막대는 이 명령의 행이 모두 판정 안 함이라 그리지 않는다
    [call] = fake_plots
    assert (call["name"], call["latencies"], call["rows"]) == (REASK_NAME, None, [])
    assert [point.cost_ratio for point in call["curve"]] == [1.0, 2.0, 5.0, 10.0]
    assert out.splitlines()[0] == "지표  값  기준  통과"
    png_path = csv_path.parent / f"{REASK_NAME}-reask.png"
    assert out.splitlines()[-4:] == [f"작성: {path}" for path in (csv_path, md_path, raw_path, png_path)]
    assert [metric["pass"] for metric in evaluation_of(csv_path.parent)["metrics"]] == [None] * 5  # 대시보드가 읽는다


def test_NFR06_evaluate_reask_ratios_come_from_the_option(server_dir, run_cli, hand, fake_plots):
    code, out, err = run_cli(reask_argv("--ratios", "3,0.5", "--out", "demo"), cwd=server_dir)

    csv_path, _, raw_path = report_files(server_dir, "demo")
    assert (code, err) == (EXIT_OK, "")
    rows = read_report(csv_path)[1]
    assert [row["metric"] for row in rows] == ["m2_reask_curve", "m2_reask_r3", "m2_reask_r0.5"]  # 준 순서다
    assert "확률 임계값 0.7500" in rows[1]["note"] and "확률 임계값 0.3333" in rows[2]["note"]
    assert read_raw(raw_path)["ratios"] == [3.0, 0.5]


def test_NFR06_evaluate_reask_without_device_label_writes_blank_row(server_dir, run_cli, make_collection_db, fake_plots):
    # 시험 회차가 모두 none이면 곡선을 만들 수 없다. 행을 비우고 사유를 note에 쓴다 (종료 코드 0).
    build_hand(server_dir, make_collection_db, [hand_run(0, "none"), hand_run(1, "none")])

    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    csv_path, _, raw_path = report_files(server_dir, REASK_NAME)
    assert (code, err) == (EXIT_OK, "")
    comments, rows = read_report(csv_path)
    assert [{key: row[key] for key in ("metric", "value", "limit", "passed", "trials", "failures", "note")} for row in rows] == [
        {"metric": "m2_reask_curve", "value": "", "limit": "", "passed": "", "trials": "", "failures": "",
         "note": "기기 라벨 시험 회차 없음"}]
    assert "# sample_count: 36" in comments
    assert read_raw(raw_path)["curve"] == [] and fake_plots[0]["curve"] == []
    assert reports_listing(server_dir) == sorted([csv_path.name, csv_path.with_suffix(".md").name, raw_path.name])


def test_NFR06_evaluate_reask_uses_test_runs_only(server_dir, run_cli, hand, fake_plots, monkeypatch):
    write_split(server_dir, ("col-998",), ("col-999",), hand)  # 학습·검증 회차는 DB에 없는 ID다. 읽었다면 거부된다
    asked = []
    real = evaluate.load_labeled_runs
    monkeypatch.setattr(evaluate, "load_labeled_runs", lambda conn, ids: asked.append(tuple(ids)) or real(conn, ids))

    code, _, err = run_cli(reask_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert asked == [hand]  # 한 번, 시험 회차만 읽는다


def test_NFR13_evaluate_reask_existing_report_requires_force(server_dir, run_cli, hand, fake_plots, monkeypatch):
    csv_path, md_path, raw_path = report_files(server_dir, REASK_NAME)
    png_path = csv_path.with_name(f"{REASK_NAME}-reask.png")
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("사람이 쓴 파일", encoding="utf-8")
    png_path.write_text("사람이 쓴 파일", encoding="utf-8")

    def must_not_read_db(config):
        raise AssertionError("보고서가 이미 있으면 DB를 읽기 전에 거부한다")

    monkeypatch.setattr(evaluate, "open_read_db", must_not_read_db)
    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"보고서가 이미 있다: {csv_path}, {png_path}. --force로 덮어쓴다\n"
    assert csv_path.read_text(encoding="utf-8") == "사람이 쓴 파일" and not md_path.exists() and not raw_path.exists()

    monkeypatch.undo()
    monkeypatch.setattr(evaluate, "now_utc", lambda: NOW)
    monkeypatch.setattr(report_module, "resolve_git_commit", lambda: "test-commit")
    code, out, err = run_cli(reask_argv("--force"), cwd=server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert csv_path.read_text(encoding="utf-8") != "사람이 쓴 파일" and md_path.is_file() and raw_path.is_file()


@pytest.mark.parametrize("created", ["csv", "md"])
def test_NFR13_evaluate_reask_report_created_during_run_is_not_overwritten(server_dir, run_cli, hand, fake_plots, monkeypatch,
                                                                          created):
    csv_path, md_path, raw_path = report_files(server_dir, REASK_NAME)
    other = {"csv": csv_path, "md": md_path}[created]
    real = evaluate.reask_curve

    def curve_then_other_process_writes(*args, **kwargs):
        other.parent.mkdir(parents=True)
        other.write_text("다른 곳에서 만든 파일", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(evaluate, "reask_curve", curve_then_other_process_writes)

    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"보고서가 이미 있다: {other}. --force로 덮어쓴다 (측정한 원자료는 {raw_path}에 썼다)\n"
    assert other.read_text(encoding="utf-8") == "다른 곳에서 만든 파일"
    assert reports_listing(server_dir) == sorted([other.name, raw_path.name])


@pytest.mark.parametrize("ratios", ["", "a", "1,,2", "0", "-1", "1,0", "nan", "inf", "1,1", "1,1.0", "1,1.0000001"])
def test_NFR06_evaluate_reask_rejects_bad_ratios(server_dir, run_cli, hand, fake_plots, ratios):
    code, out, err = run_cli(reask_argv("--ratios", ratios), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith("--ratios") and err.count("\n") == 1
    assert not (server_dir / "var" / "reports").exists()  # DB를 읽거나 폴더를 만들기 전에 거부한다


@pytest.mark.parametrize("model", ["place-a", "place-a/v1/v2", "/v1", "place-a/", "place-a\\v1", "../v1", "place-a/.."])
def test_NFR06_evaluate_reask_model_argument_must_be_place_slash_version(server_dir, run_cli, model):
    code, out, err = run_cli(reask_argv(model=model), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"평가할 모델은 PLACE/VERSION 형식으로 쓴다: {model}\n"
    assert not (server_dir / "var").exists()


def test_NFR06_evaluate_reask_input_errors_are_exit_2(server_dir, run_cli, make_collection_db):
    models = (server_dir / "var" / "models" / "place-a").resolve()
    code, out, err = run_cli(reask_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"모델 파일이 없다: {models / 'v1.json'}. {models}에 있는 파일: (없음)\n"

    build_hand(server_dir, make_collection_db)
    code, out, err = run_cli(["evaluate-reask", "place-a/v1", "--split", "nowhere.json"], cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith("분할 결과 파일이 없다: ")

    other = write_split(server_dir, (), (), ("col-001",), place_id="place-b", name="other.json")  # 다른 장소의 분할 결과
    code, out, err = run_cli(["evaluate-reask", "place-a/v1", "--split", str(other)], cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == "분할 결과의 장소(place-b)와 평가할 장소(place-a)가 다르다\n"

    other.write_text("JSON이 아니다", encoding="utf-8")
    code, out, err = run_cli(["evaluate-reask", "place-a/v1", "--split", str(other)], cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "") and err.startswith("분할 결과 파일 오류: ")

    (models / "v1.json").write_text("JSON이 아니다", encoding="utf-8")  # build_hand가 쓴 모델을 망가뜨린다
    code, out, err = run_cli(reask_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "") and err.startswith("모델 파일 오류: ")
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_evaluate_reask_missing_db_is_exit_2(server_dir, run_cli, fake_plots):
    # 모델과 분할 결과는 있는데 DB가 없다. 오타 난 --config가 가리키는 곳에 빈 DB를 만들지 않는다.
    write_model(server_dir)
    write_split(server_dir, (), (), ("col-001",))

    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    db_path = (server_dir / "var" / "nearpilot.db").resolve()
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"DB 파일이 없다: {db_path}. 먼저 nearpilot serve --collect-only로 서버를 한 번 시작한다\n"
    assert not db_path.exists() and not (server_dir / "var" / "reports").exists()


@pytest.mark.parametrize("name", ["a/b", "a\\b", "../x"])
def test_NFR06_evaluate_reask_out_must_be_a_name(server_dir, run_cli, hand, name):
    code, out, err = run_cli(reask_argv("--out", name), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"--out은 경로가 아니라 이름이다. /나 \\ 없이 쓴다: {name}\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_evaluate_reask_unreadable_runs_fail(server_dir, run_cli, make_collection_db):
    data = build_hand(server_dir, make_collection_db, [*hand_runs(), hand_run(4, "locker-1", in_progress=True)])
    assert data[-1] == "col-005"  # 시험 회차의 마지막이 진행 중이다

    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "수집 회차를 읽을 수 없다: 진행 중인 회차 col-005\n"
    assert not (server_dir / "var" / "reports").exists()  # 보고서도 보고서 폴더도 만들지 않는다


def test_NFR06_evaluate_reask_run_without_evaluation_times_fails(server_dir, run_cli, make_collection_db):
    # 두 번째 시험 회차가 2초라서 window_sec(2.5초)보다 짧다
    build_hand(server_dir, make_collection_db, [hand_run(0, "locker-1"), hand_run(1, "locker-2", seconds=2)])

    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "평가 실패: 회차 'col-002': 구간이 window_sec(2.5초)보다 짧아 평가 시각이 없다\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_evaluate_reask_without_test_runs_fails(server_dir, run_cli, hand):
    write_split(server_dir, hand, (), ())  # 시험 회차가 없다

    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "평가 실패: 시험 회차의 표본이 없어 곡선을 만들 수 없다\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR13_evaluate_reask_writes_graph(server_dir, run_cli, hand):
    pytest.importorskip("matplotlib")

    code, out, err = run_cli(reask_argv(), cwd=server_dir)

    reports = report_files(server_dir, REASK_NAME)[0].parent
    assert (code, err) == (EXIT_OK, "")
    assert (reports / f"{REASK_NAME}-reask.png").read_bytes().startswith(PNG_SIGNATURE)
    assert not (reports / f"{REASK_NAME}-pass.png").exists() and not (reports / f"{REASK_NAME}-latency.png").exists()
    assert out.splitlines()[-1] == f"작성: {reports / f'{REASK_NAME}-reask.png'}"
