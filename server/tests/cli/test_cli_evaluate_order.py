"""`nearpilot evaluate-order` (FR-09, FR-16, NFR-13, specs/db-evaluation-distance-order/spec.md 5절·6절).

실제 SQLite 파일에 `main(argv)`를 같은 프로세스에서 불러 확인한다. DB의 기기, 비콘, 회차, 프레임은 `make_collection_db`가 실제 쓰기 경로로 만들고,
분할 결과·적합 설정·좌표표·회차 기록 파일은 이 파일이 쓴다. 계산의 세부(순서, 쌍, σ 추정, 기준 확률)는 `tests/tools/test_eval_order.py`가 확인한다.
여기서는 명령이 학습·검증·시험 회차를 맞는 자리에 쓰는지, 입력 오류와 계산 실패의 종료 코드, 보고서·원자료·그래프 파일을 확인한다.

데이터는 기기 셋이 x축 위에 있는 장소다: locker-1(0 m), locker-2(4 m), locker-3(10 m). 서는 위치는 둘이다: "locker-1 앞"(1 m), "locker-3 앞"(9 m).
회차는 12초이고 기기마다 1초에 하나씩 RSSI를 받는다. RSSI는 기기마다 평균 `level`에 흔들림 (+1, −1, 0) × 크기를 더한 값이다.
`anchor_recent_sec`(3초)의 창에는 프레임이 3개 들어가고 흔들림의 합이 0이라서, 모든 시점의 정렬 값은 `level` 그대로다.
시점은 회차 시작 3초 뒤부터 1초마다 12초까지라 회차마다 10개이고, 한 회차의 시점은 모두 같은 순서다. 그래서 값을 손으로 센다.
흔들림은 학습 회차가 1 dB(σf가 정확히 1.0), 검증·시험 회차가 3 dB라서 다른 회차가 σf에 섞이면 값이 달라진다.
그래프를 실제로 그리는 부분은 matplotlib이 있을 때만 확인한다.
"""

from __future__ import annotations

import csv
import importlib.util
import itertools
import json
import math
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from types import SimpleNamespace

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli import evaluate
from tools.evaluation import report as report_module
from tools.evaluation.split import SplitResult, split_to_json

T0 = datetime(2026, 11, 13, 9, 0, 0, tzinfo=timezone.utc)  # 첫 회차의 시작
NOW = datetime(2026, 11, 14, 5, 0, 0, tzinfo=timezone.utc)  # 테스트가 고정하는 `now_utc()`
ORDER_NAME = "place-a-order-20261114"  # 기본 보고서 이름: <장소>-order-<NOW의 UTC 날짜>
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SECONDS = 12
WOBBLE = (1.0, -1.0, 0.0)
ROW_NAMES = [
    "m10_frame_sigma_db", "m10_static_sigma_db", "m10_pair_order_agreement",
    "m10_boundary_min_prob", "m10_boundary_pass_rate", "m10_boundary_error_rate",
]
PROVENANCE = ("place_id", "split_file", "layout_file")

NODE_XY = {"locker-1": (0.0, 0.0), "locker-2": (4.0, 0.0), "locker-3": (10.0, 0.0)}
NEAR_1, NEAR_3 = "locker-1 앞", "locker-3 앞"
SPOT_XY = {NEAR_1: (1.0, 0.0), NEAR_3: (9.0, 0.0)}  # NEAR_1에서 기기까지 1, 3, 9 m. NEAR_3에서는 9, 5, 1 m다

# 회차 이름 → (서는 위치, locker-1·2·3의 평균 RSSI, 흔들림 크기)
RUNS: dict[str, tuple[str, tuple[int, int, int], int]] = {
    "t1": (NEAR_1, (-43, -49, -67), 1),  # 학습: 순서가 모두 맞다
    "t2": (NEAR_3, (-67, -55, -43), 1),  # 학습: 순서가 모두 맞다
    "t3": (NEAR_1, (-49, -47, -67), 1),  # 학습: locker-2가 locker-1보다 2 dB 세서 그 쌍의 순서가 틀리다
    "v1": (NEAR_1, (-49, -46, -67), 3),  # 검증: locker-2가 locker-1보다 3 dB 세다 (틀린 경계가 약하다)
    "v2": (NEAR_3, (-67, -55, -43), 3),  # 검증: 순서가 모두 맞다
    "v3": (NEAR_1, (-49, -43, -67), 3),  # 검증: locker-2가 locker-1보다 6 dB 세다 (틀린 경계가 v1보다 강하다)
    "v4": (NEAR_1, (-61, -67, -43), 3),  # 검증: locker-3이 locker-1보다 18 dB 세다 (틀린 경계가 아주 강하다)
    "e1": (NEAR_1, (-43, -45, -67), 3),  # 시험: 순서가 모두 맞지만 locker-1과 locker-2는 2 dB 차이뿐이다 (약한 경계)
    "e2": (NEAR_3, (-55, -67, -43), 3),  # 시험: locker-1이 locker-2보다 12 dB 센데 locker-1이 더 멀다 (강하게 틀리다)
}
SHORT = "short"  # 2초뿐인 시험 회차. anchor_recent_sec(3초)보다 짧아 평가 시점이 없다
RUN_ID = {name: f"col-{index:03d}" for index, name in enumerate([*RUNS, SHORT], start=1)}  # make_collection_db가 붙이는 ID
TRAIN, VALIDATION, TEST = ("t1", "t2", "t3"), ("v1", "v2"), ("e1", "e2")
# 회차 기록 파일 두 개. 첫 파일에는 학습 회차와 검증 회차 v1, v2가, 둘째 파일에는 나머지가 있다
RECORD_FILES = {"plan-a-20261112.jsonl": ("t1", "t2", "t3", "v1", "v2"), "plan-a-20261113.jsonl": ("v3", "v4", "e1", "e2", SHORT)}


# ── 기대값 (알고리즘을 다시 쓰지 않고 spec의 식에서 따로 구한다) ─────────────


def pairs_of(spot: str, levels: Sequence[int]) -> list[tuple[float, bool]]:
    """회차 한 시점의 모든 쌍: (앞선 기기 − 뒤 기기의 평균 RSSI, 앞선 기기가 서는 위치에 실제로 더 가깝다).

    평균 RSSI가 센 기기가 앞선다 (같으면 기기 ID 순서).
    """
    ranked = sorted(zip(NODE_XY, levels), key=lambda item: (-item[1], item[0]))
    return [
        (early_level - late_level, math.dist(SPOT_XY[spot], NODE_XY[early]) < math.dist(SPOT_XY[spot], NODE_XY[late]))
        for (early, early_level), (late, late_level) in itertools.combinations(ranked, 2)
    ]


def train_pairs() -> list[tuple[float, bool]]:
    """학습 회차의 모든 쌍. 회차마다 시점이 10개이고 시점마다 같다."""
    return [pair for name in TRAIN for pair in pairs_of(RUNS[name][0], RUNS[name][1]) * 10]


def best_static_sigma(pairs: Sequence[tuple[float, bool]], frame_sigma: float) -> float:
    """σs의 별도 계산 (spec 6절 결정 1, 12): 0~15 dB의 0.05 dB 간격 격자에서 로그 가능도가 가장 큰 값이다.

    로그 가능도는 Σ log P (맞은 쌍) + log(1 − P) (틀린 쌍)이고 P = Φ(D / s)다. 창마다 프레임이 3개씩이라
    s² = σf² × (1/3 + 1/3) + 2 σs²다. 가능도가 같으면 작은 값이다.
    """

    def log_likelihood(sigma_s: float) -> float:
        spread = math.sqrt(frame_sigma ** 2 * 2 / 3 + 2 * sigma_s ** 2)
        total = 0.0
        for gap, correct in pairs:
            probability = min(max(NormalDist().cdf(gap / spread), 1e-12), 1 - 1e-12)
            total += math.log(probability if correct else 1 - probability)
        return total

    return max((step / 20 for step in range(301)), key=log_likelihood)


def lowest_candidate_above(probability: float) -> float:
    """확률이 `probability`인 경계가 통과하지 못하는 가장 낮은 후보 (후보는 0.50부터 0.99까지 0.01 간격이다, 결정 4)."""
    return next(percent / 100 for percent in range(50, 100) if percent / 100 > probability)


def boundary_probability(gap: float, sigma_s: float) -> float:
    """σf = 1 dB이고 프레임이 3개씩일 때 D dB 차이 경계의 확률 Φ(D / s)."""
    return NormalDist().cdf(gap / math.sqrt(2 / 3 + 2 * sigma_s ** 2))


# ── 입력 파일과 DB ────────────────────────────────────────────────────


def layout_file(*, place_id: str = "place-a", nodes: Mapping[str, tuple[float, float]] = NODE_XY,
                spots: Mapping[str, tuple[float, float]] = SPOT_XY) -> str:
    lines = [f'place_id = "{place_id}"', "", "[nodes]"]
    lines += [f'"{name}" = [{x}, {y}]' for name, (x, y) in nodes.items()]
    lines += ["", "[spots]"]
    lines += [f'"{name}" = [{x}, {y}]' for name, (x, y) in spots.items()]
    return "\n".join(lines) + "\n"


def without(points: Mapping[str, tuple[float, float]], name: str) -> dict[str, tuple[float, float]]:
    return {key: value for key, value in points.items() if key != name}


def fit_file(*, max_boundary_error: float | None = 0.10) -> str:
    """적합 설정 파일 내용. `evaluate-order`는 `[acceptance]`의 `max_boundary_error`만 쓴다. 다른 두 기준은 일부러 다른 값이다."""
    target = "" if max_boundary_error is None else f"max_boundary_error = {max_boundary_error}\n"
    return f"""\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
taus = [0.5, 1.0, 2.0]
stays = [0.8, 0.9]
window_secs = [2.5, 3.5]
auto_zero_min_bins = 120

[acceptance]
max_misselection = 0.05
max_ece = 0.07
{target}"""


def write_text(server: Path, relative: str, text: str) -> Path:
    path = server / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_layout(server: Path, text: str | None = None, name: str = "place-a.toml") -> Path:
    return write_text(server, f"config/layouts/{name}", layout_file() if text is None else text)


def write_fit(server: Path, text: str | None = None, name: str = "place-a.toml") -> Path:
    return write_text(server, f"config/fit/{name}", fit_file() if text is None else text)


def write_split(server: Path, train: Sequence[str], validation: Sequence[str], test: Sequence[str], *,
                place_id: str = "place-a", name: str = "place-a-seed42.json") -> Path:
    """분할 결과 파일. 인자는 회차 이름(`RUNS`의 키)이다. `labels`는 이 명령이 읽지 않아서 비워 둔다."""
    ids = [tuple(RUN_ID[run] for run in runs) for runs in (train, validation, test)]
    result = SplitResult(place_id, 42, (0.6, 0.2, 0.2), *ids, (), {})
    return write_text(server, f"var/splits/{name}", split_to_json(result, created_at=T0, db_path="nearpilot.db"))


def write_records(server: Path, name: str, runs: Sequence[str]) -> Path:
    """`collect run`이 쓰는 모양의 회차 기록 파일. 이 명령은 `collection_id`와 `spot`만 읽는다."""
    lines = []
    for run in runs:
        spot = RUNS[run][0] if run in RUNS else NEAR_1
        lines.append(json.dumps({
            "index": RUN_ID[run], "collection_id": RUN_ID[run], "place_id": "place-a", "label": "locker-1",
            "spot": spot, "status": "done"}, ensure_ascii=False))
    return write_text(server, f"var/records/{name}", "\n".join(lines) + "\n")


def conflicting_record(run: str) -> str:
    """`run`의 회차 ID에 다른 spot을 적은 기록 한 줄. 이미 있는 기록 파일과 합치면 spot이 서로 다르다."""
    other = NEAR_3 if RUNS[run][0] == NEAR_1 else NEAR_1
    return json.dumps({"collection_id": RUN_ID[run], "spot": other}, ensure_ascii=False) + "\n"


def remove_record_files(server: Path) -> None:
    for path in (server / "var" / "records").glob("*.jsonl"):
        path.unlink()


def build_data(server: Path, make_collection_db: Callable[..., list[str]], *, with_short: bool = False,
               extra_node_in: str | None = None, flat_train: bool = False) -> None:
    """DB에 회차를 만들고, 회차 기록 파일 둘, 좌표표, 적합 설정, 기본 분할 결과(`place-a-seed42.json`)를 쓴다.

    `extra_node_in`에 회차 이름을 주면 그 회차에만 좌표표에 없는 기기 locker-4가 나온다.
    `flat_train`이면 학습 회차의 RSSI가 흔들리지 않는다 (σf가 0이다).
    """
    runs = [(name, spot, levels, amplitude, SECONDS) for name, (spot, levels, amplitude) in RUNS.items()]
    if with_short:
        runs.append((SHORT, NEAR_1, (-43, -49, -67), 1, 2))
    rows = []
    for index, (name, spot, levels, amplitude, seconds) in enumerate(runs):
        label = "locker-1" if spot == NEAR_1 else "locker-3"
        amplitude = 0 if flat_train and name in TRAIN else amplitude
        frames = {node: [level + amplitude * WOBBLE[k % 3] for k in range(seconds)] for node, level in zip(NODE_XY, levels)}
        if name == extra_node_in:
            frames["locker-4"] = [-70.0] * seconds
        rows.append((label, T0 + timedelta(seconds=60 * index), seconds, frames))
    ids = make_collection_db(server, {"place-a": [*NODE_XY, *(["locker-4"] if extra_node_in else [])]}, rows)
    assert ids == [RUN_ID[name] for name, *_ in runs]
    for name, members in RECORD_FILES.items():
        write_records(server, name, members)
    write_text(server, "var/records/memo.txt", "JSON이 아니다\n")  # *.jsonl이 아닌 파일은 기본 기록 파일이 아니다
    write_layout(server)
    write_fit(server)
    write_split(server, TRAIN, VALIDATION, TEST)


def order_argv(*extra: str, split: str = "place-a-seed42.json", layout: str = "place-a.toml") -> list[str]:
    return ["evaluate-order", "--split", split, "--layout", layout, *extra]


def report_files(server: Path, name: str = ORDER_NAME) -> tuple[Path, Path, Path]:
    """보고서 CSV, Markdown, 원자료 JSON의 경로."""
    reports = (server / "var" / "reports").resolve()
    return reports / f"{name}.csv", reports / f"{name}.md", reports / f"{name}-raw.json"


def reports_listing(server: Path) -> list[str]:
    reports = server / "var" / "reports"
    return sorted(path.name for path in reports.iterdir()) if reports.exists() else []


def read_report(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    """보고서 CSV의 머리말 주석 줄과, 지표 이름 → 행 (CSV 순서)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = list(csv.DictReader(line for line in lines if not line.startswith("#")))
    return [line for line in lines if line.startswith("#")], {row["metric"]: row for row in rows}


def read_raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_version(server: Path) -> int:
    conn = sqlite3.connect(server / "var" / "nearpilot.db")
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def server_dir(tmp_path: Path, write_config) -> Path:
    server = tmp_path / "server"
    write_config(server, settings={"version": "t1", "anchor_recent_sec": 3.0, "rssi_frame_sec": 1.0})
    return server


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluate, "now_utc", lambda: NOW)
    monkeypatch.setattr(report_module, "resolve_git_commit", lambda: "test-commit")  # git 프로세스를 띄우지 않는다


@pytest.fixture
def data(server_dir: Path, make_collection_db) -> SimpleNamespace:
    build_data(server_dir, make_collection_db)
    return SimpleNamespace(plot_installed=importlib.util.find_spec("matplotlib") is not None)


# ── 정상 실행 ─────────────────────────────────────────────────────────


def test_FR09_evaluate_order_writes_report(server_dir, run_cli, data):
    # 학습 회차(t1·t2·t3)로 σf, σs를, 검증 회차(v1·v2)로 기준 확률을, 시험 회차(e1·e2)로 지표를 잰다
    db_file = server_dir / "var" / "nearpilot.db"
    db_before = db_file.read_bytes()

    code, out, err = run_cli(order_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    plot_path = csv_path.with_name(f"{ORDER_NAME}-order.png")
    assert (code, err) == (EXIT_OK, "")
    files = [csv_path.name, md_path.name, raw_path.name]
    if data.plot_installed:
        assert plot_path.read_bytes().startswith(PNG_SIGNATURE)
        files.append(plot_path.name)
    assert reports_listing(server_dir) == sorted(files)
    assert db_file.read_bytes() == db_before  # DB는 읽기 전용으로만 연다

    comments, rows = read_report(csv_path)
    assert list(rows) == ROW_NAMES
    # σf는 학습 회차의 흔들림 (+1, −1, 0)에서 정확히 1 dB다. 검증·시험 회차는 3 dB라서 섞이면 값이 달라진다.
    assert rows["m10_frame_sigma_db"]["value"] == "1.0"
    # σs는 학습 회차의 모든 쌍(회차 셋 × 시점 10 × 쌍 3 = 90개)의 가능도가 가장 큰 격자 값이다 (3.65 dB)
    static = rows["m10_static_sigma_db"]
    assert (static["trials"], static["note"]) == ("90", "")  # 이웃한 쌍만 썼다면 60개다
    sigma_s = float(static["value"])
    assert sigma_s == pytest.approx(best_static_sigma(train_pairs(), 1.0))
    assert 0.0 < sigma_s < 15.0
    # 기준 확률은 검증 회차의 약한 틀린 경계(v1, 3 dB 차이)가 통과하지 못하는 가장 낮은 후보다 (확률 0.717 → 0.72).
    # 그보다 낮은 후보는 그 경계를 통과시켜 통과한 40개 중 10개가 틀리다 (0.25 > 목표 0.10).
    chosen = float(rows["m10_boundary_min_prob"]["value"])
    assert chosen == lowest_candidate_above(boundary_probability(3, sigma_s))
    # 시험 회차 둘의 모든 쌍 60개 중 e2의 locker-1·locker-2 쌍(시점 10개)이 틀리다
    agreement = rows["m10_pair_order_agreement"]
    assert (agreement["trials"], agreement["failures"]) == ("60", "10")
    assert float(agreement["value"]) == pytest.approx(50 / 60)
    assert (agreement["limit"], agreement["passed"]) == ("", "")  # 순서 일치율의 기준은 미결이다 (#279)
    # 시험 회차 둘의 경계 40개 중 e1의 약한 경계(2 dB 차이, 확률 0.649) 10개가 기준 확률에 못 미쳐 통과하지 못한다.
    # 통과한 30개 중 e2의 틀린 경계 10개가 틀리다. 목표는 적합 설정의 max_boundary_error(0.10)다.
    pass_rate, error_rate = rows["m10_boundary_pass_rate"], rows["m10_boundary_error_rate"]
    assert (pass_rate["value"], pass_rate["trials"], pass_rate["passed"]) == ("0.75", "40", "")
    assert float(error_rate["value"]) == pytest.approx(1 / 3)
    assert (error_rate["trials"], error_rate["failures"]) == ("30", "10")
    assert (error_rate["limit"], error_rate["passed"]) == ("0.1", "False")
    # 무엇을 평가했는지 모든 행에 남는다: 장소, 분할 결과, 좌표표
    assert {tuple(row[key] for key in PROVENANCE) for row in rows.values()} == {(
        "place-a", str((server_dir / "var" / "splits" / "place-a-seed42.json").resolve()),
        str((server_dir / "config" / "layouts" / "place-a.toml").resolve()))}
    assert comments == [
        f"# db_path: {(server_dir / 'var' / 'nearpilot.db').resolve()}", f"# schema_version: {schema_version(server_dir)}",
        "# git_commit: test-commit", "# sample_count: 20", "# generated_at: 2026-11-14T05:00:00+00:00"]  # 시험 회차의 평가 시점 수
    assert "| m10_boundary_error_rate | 0.3333333333333333 | 0.1 | False | 30 | 10 |" in md_path.read_text(encoding="utf-8")
    assert [line.rstrip() for line in out.splitlines()] == [
        "지표  값  기준  통과", "m10_frame_sigma_db  1.0000  -", f"m10_static_sigma_db  {sigma_s:.4f}  -",
        "m10_pair_order_agreement  0.8333  -", f"m10_boundary_min_prob  {chosen:.4f}  -", "m10_boundary_pass_rate  0.7500  -",
        "m10_boundary_error_rate  0.3333  0.1000  미달",
        *[f"작성: {path}" for path in (csv_path, md_path, raw_path, *([plot_path] if data.plot_installed else []))]]

    # 시험 회차를 바꿔도 σ와 고른 값은 그대로이고 시험 쪽 값만 바뀐다 (정하는 데 시험 회차를 쓰지 않는다)
    write_split(server_dir, TRAIN, VALIDATION, ("e1",), name="test-e1.json")
    code, _, err = run_cli(order_argv("--out", "only-e1", split="test-e1.json"), cwd=server_dir)
    assert (code, err) == (EXIT_OK, "")
    rows_e1 = read_report(report_files(server_dir, "only-e1")[0])[1]
    for metric in ("m10_frame_sigma_db", "m10_static_sigma_db", "m10_boundary_min_prob"):
        assert rows_e1[metric]["value"] == rows[metric]["value"], metric
    assert (rows_e1["m10_pair_order_agreement"]["trials"], rows_e1["m10_pair_order_agreement"]["failures"]) == ("30", "0")
    assert (rows_e1["m10_boundary_pass_rate"]["value"], rows_e1["m10_boundary_pass_rate"]["trials"]) == ("0.5", "20")
    assert (rows_e1["m10_boundary_error_rate"]["value"], rows_e1["m10_boundary_error_rate"]["passed"]) == ("0.0", "True")

    # 검증 회차를 바꾸면 고른 값이 바뀐다. 틀린 경계가 6 dB 차이(v3)로 강해져서 더 높은 후보부터 통과시킨다. σ는 그대로다.
    write_split(server_dir, TRAIN, ("v3", "v2"), TEST, name="validation-v3.json")
    code, _, err = run_cli(order_argv("--out", "validation-v3", split="validation-v3.json"), cwd=server_dir)
    assert (code, err) == (EXIT_OK, "")
    rows_v3 = read_report(report_files(server_dir, "validation-v3")[0])[1]
    assert float(rows_v3["m10_boundary_min_prob"]["value"]) == lowest_candidate_above(boundary_probability(6, sigma_s))
    assert float(rows_v3["m10_boundary_min_prob"]["value"]) > chosen
    for metric in ("m10_frame_sigma_db", "m10_static_sigma_db"):
        assert rows_v3[metric]["value"] == rows[metric]["value"], metric


def test_FR09_evaluate_order_raw_file_keeps_every_candidate(server_dir, run_cli, data):
    code, _, err = run_cli(order_argv(), cwd=server_dir)

    raw = read_raw(report_files(server_dir)[2])
    assert (code, err) == (EXIT_OK, "")
    records = server_dir.resolve() / "var" / "records"
    assert {key: raw[key] for key in (
        "command", "generated_at", "place_id", "split_file", "layout_file", "fit_file", "record_files", "window_sec", "step_sec",
        "max_boundary_error", "test_points")} == {
        "command": "evaluate-order", "generated_at": "2026-11-14T05:00:00+00:00", "place_id": "place-a",
        "split_file": str(server_dir.resolve() / "var" / "splits" / "place-a-seed42.json"),
        "layout_file": str(server_dir.resolve() / "config" / "layouts" / "place-a.toml"),
        "fit_file": str(server_dir.resolve() / "config" / "fit" / "place-a.toml"),
        "record_files": [str(records / "plan-a-20261112.jsonl"), str(records / "plan-a-20261113.jsonl")],  # 이름 순
        "window_sec": 3.0, "step_sec": 1.0, "max_boundary_error": 0.1, "test_points": 20}
    assert raw["frame_sigma_db"] == 1.0
    assert raw["static_sigma"]["pairs"] == 90 and raw["static_sigma"]["at_edge"] is False
    assert raw["static_sigma"]["sigma_db"] == pytest.approx(best_static_sigma(train_pairs(), 1.0))
    assert raw["pair_agreement"] == {"agreed": 50, "total": 60}
    assert raw["chosen_min_prob"] == 0.72

    # 후보는 0.50부터 0.99까지 0.01 간격이고, 후보마다 검증·시험의 경계 수, 통과 수, 틀린 수, 통과 비율, 오류 비율이 있다
    candidates = {candidate["min_prob"]: candidate for candidate in raw["candidates"]}
    assert [candidate["min_prob"] for candidate in raw["candidates"]] == [percent / 100 for percent in range(50, 100)]
    assert set(raw["candidates"][0]) == {"min_prob", "validation", "test"}

    def stat(boundaries: int, passed: int, wrong: int) -> dict:
        return {"boundaries": boundaries, "passed": passed, "wrong": wrong, "pass_rate": passed / boundaries,
                "error_rate": wrong / passed if passed else None}

    # 0.50: 경계가 모두 통과한다. 검증 40개 중 10개(v1의 3 dB 차이)가, 시험 40개 중 10개(e2의 12 dB 차이)가 틀리다
    assert candidates[0.5]["validation"] == stat(40, 40, 10) and candidates[0.5]["test"] == stat(40, 40, 10)
    # 0.71: 검증 회차의 약한 틀린 경계(확률 0.717)를 아직 통과시킨다. 0.72부터 통과시키지 않는다
    assert candidates[0.71]["validation"] == stat(40, 40, 10)
    assert candidates[0.72]["validation"] == stat(40, 30, 0) and candidates[0.72]["test"] == stat(40, 30, 10)
    # 0.99: 확률이 0.99 이상인 경계는 18 dB(v1)와 22 dB(e1) 차이뿐이고 모두 맞다
    assert candidates[0.99]["validation"] == stat(40, 10, 0) and candidates[0.99]["test"] == stat(40, 10, 0)


def test_FR09_evaluate_order_without_chosen_threshold_still_exits_0(server_dir, run_cli, data):
    # v4의 18 dB 차이 틀린 경계(확률 0.9997)는 어느 후보로도 걸러지지 않아서, 목표 오류 비율 이하인 후보가 없다
    write_split(server_dir, TRAIN, ("v4",), TEST, name="no-choice.json")

    code, out, err = run_cli(order_argv(split="no-choice.json"), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    rows = read_report(csv_path)[1]
    assert list(rows) == ROW_NAMES and rows["m10_frame_sigma_db"]["value"] == "1.0"
    for metric, limit, note in (
        ("m10_boundary_min_prob", "", "검증 회차에서 목표 오류 비율 이하인 후보 없음"),
        ("m10_boundary_pass_rate", "", "기준 확률을 고르지 못해 시험하지 않음"),
        ("m10_boundary_error_rate", "0.1", "기준 확률을 고르지 못해 시험하지 않음"),
    ):
        row = rows[metric]
        assert (row["value"], row["passed"], row["trials"], row["failures"]) == ("", "", "", ""), metric
        assert (row["limit"], row["note"]) == (limit, note), metric
    raw = read_raw(raw_path)
    assert raw["chosen_min_prob"] is None and len(raw["candidates"]) == 50  # 시험 회차의 후보별 표는 남는다
    assert out.splitlines()[4].rstrip() == "m10_boundary_min_prob  -  -  검증 회차에서 목표 오류 비율 이하인 후보 없음"
    written = [csv_path, md_path, raw_path, *([csv_path.with_name(f"{ORDER_NAME}-order.png")] if data.plot_installed else [])]
    assert out.splitlines()[-len(written):] == [f"작성: {path}" for path in written]  # 고르지 못해도 그래프의 후보별 표는 그린다


def test_FR09_evaluate_order_target_comes_from_the_fit_file(server_dir, run_cli, data):
    # 목표 오류 비율 0.30이면 검증 회차의 오류 비율 0.25가 이하라서 가장 낮은 후보 0.50을 고른다
    lenient = write_fit(server_dir, fit_file(max_boundary_error=0.30), name="lenient.toml")

    for index, argument in enumerate(("lenient.toml", str(lenient))):  # 이름은 config/fit/에서, 경로는 실행 폴더 기준으로 찾는다
        out_name = f"lenient-{index}"
        code, _, err = run_cli(order_argv("--fit", argument, "--out", out_name), cwd=server_dir)
        assert (code, err) == (EXIT_OK, ""), argument
        csv_path, _, raw_path = report_files(server_dir, out_name)
        rows = read_report(csv_path)[1]
        assert rows["m10_boundary_min_prob"]["value"] == "0.5", argument
        error_rate = rows["m10_boundary_error_rate"]
        assert (error_rate["value"], error_rate["limit"], error_rate["passed"]) == ("0.25", "0.3", "True"), argument
        assert read_raw(raw_path)["fit_file"] == str(lenient.resolve())


@pytest.mark.parametrize(("settings", "frame_sigma", "train_pair_count", "test_point_count"), [
    pytest.param({"anchor_recent_sec": 3.0, "rssi_frame_sec": 2.0}, "1.0", "45", 10, id="step-2s"),
    pytest.param({"anchor_recent_sec": 6.0, "rssi_frame_sec": 1.0}, str(math.sqrt(0.8)), "63", 14, id="window-6s"),
])
def test_FR09_evaluate_order_reads_window_and_step_from_settings(
        server_dir, run_cli, data, write_config, settings, frame_sigma, train_pair_count, test_point_count):
    # 평가 시점은 회차 시작 window_sec 뒤부터 step_sec마다다. step 2초면 회차마다 5개(3, 5, …, 11초), window 6초면 7개(6, 7, …, 12초)다.
    # window 6초이면 구간 하나가 프레임 6개 (+1, −1, 0, +1, −1, 0)라서 편차 제곱합 4를 자유도 5로 나눈 σf = sqrt(0.8)이다.
    write_config(server_dir, settings={"version": "t1", **settings})

    code, out, err = run_cli(order_argv(), cwd=server_dir)

    csv_path, _, raw_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    comments, rows = read_report(csv_path)
    assert rows["m10_frame_sigma_db"]["value"] == frame_sigma
    assert rows["m10_static_sigma_db"]["trials"] == train_pair_count  # 학습 회차 셋 × 시점 × 쌍 3
    assert rows["m10_pair_order_agreement"]["trials"] == str(3 * test_point_count)
    assert f"# sample_count: {test_point_count}" in comments
    raw = read_raw(raw_path)
    assert (raw["window_sec"], raw["step_sec"], raw["test_points"]) == (settings["anchor_recent_sec"], settings["rssi_frame_sec"],
                                                                        test_point_count)


def test_FR09_evaluate_order_records_option(server_dir, run_cli, data):
    code, _, err = run_cli(order_argv("--out", "default"), cwd=server_dir)  # 기본은 var/records/*.jsonl 전부다
    assert (code, err) == (EXIT_OK, "")
    default_rows = read_report(report_files(server_dir, "default")[0])[1]
    records = server_dir.resolve() / "var" / "records"
    first, second = records / "plan-a-20261112.jsonl", records / "plan-a-20261113.jsonl"

    elsewhere = write_text(server_dir, "records-elsewhere/mine.jsonl", "")  # 경로로 준 파일은 var/records/ 밖이어도 된다
    elsewhere.write_bytes(first.read_bytes() + second.read_bytes())
    for out_name, arguments, used in (
        ("by-name", ["plan-a-20261112.jsonl", "plan-a-20261113.jsonl"], [first, second]),  # 이름은 var/records/에서 찾는다
        ("by-path", [str(second), str(first)], [second, first]),  # 경로는 실행 폴더 기준이다. 준 순서대로 남는다
        ("by-relative-path", ["records-elsewhere/mine.jsonl"], [elsewhere.resolve()]),  # 파일 하나에 회차가 모두 있어도 된다
    ):
        code, _, err = run_cli(order_argv("--records", *arguments, "--out", out_name), cwd=server_dir)
        assert (code, err) == (EXIT_OK, ""), out_name
        csv_path, _, raw_path = report_files(server_dir, out_name)
        rows = read_report(csv_path)[1]
        assert {m: {k: v for k, v in row.items() if k not in PROVENANCE} for m, row in rows.items()} == {
            m: {k: v for k, v in row.items() if k not in PROVENANCE} for m, row in default_rows.items()}, out_name
        assert read_raw(raw_path)["record_files"] == [str(path) for path in used], out_name


# ── 거부 (종료 코드 1, 2) ─────────────────────────────────────────────


def test_FR09_evaluate_order_rejects_place_mismatch(server_dir, run_cli, data):
    write_layout(server_dir, layout_file(place_id="place-b"))  # 분할 결과는 place-a다

    code, out, err = run_cli(order_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == "좌표표 오류: place_id가 분할 결과의 장소와 다르다 (좌표표 'place-b', 분할 결과 'place-a')\n"
    assert not (server_dir / "var" / "reports").exists()


@pytest.mark.parametrize("test_runs", [pytest.param((SHORT,), id="shorter-than-window"), pytest.param((), id="no-test-runs")])
def test_FR09_evaluate_order_without_test_samples_fails(server_dir, run_cli, make_collection_db, test_runs):
    # 시험 회차가 2초라서 anchor_recent_sec(3초)보다 짧거나, 시험 회차가 없다: 평가 시점이 하나도 없다
    build_data(server_dir, make_collection_db, with_short=True)
    write_split(server_dir, TRAIN, VALIDATION, test_runs)

    code, out, err = run_cli(order_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "평가 실패: 시험 회차에 평가 시점이 없다: 시험 회차가 없거나 회차 구간이 3초(anchor_recent_sec)보다 짧다\n"
    assert not (server_dir / "var" / "reports").exists()  # 보고서도 보고서 폴더도 만들지 않는다


def test_FR09_evaluate_order_fails_when_sigma_cannot_be_estimated(server_dir, run_cli, data):
    # 학습 회차가 없으면 σf를 추정할 수 없다. 계산 실패는 종료 코드 1이다
    write_split(server_dir, (), VALIDATION, TEST, name="no-train.json")

    code, out, err = run_cli(order_argv(split="no-train.json"), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err.startswith("평가 실패: σf를 추정할 수 없다") and err.count("\n") == 1
    assert not (server_dir / "var" / "reports").exists()


def test_FR09_evaluate_order_fails_when_frame_sigma_is_zero(server_dir, run_cli, make_collection_db):
    # 학습 회차의 프레임이 구간 안에서 모두 같은 값이면 σf가 0이라 경계 확률을 낼 수 없다. 계산 실패(종료 코드 1)다
    build_data(server_dir, make_collection_db, flat_train=True)

    code, out, err = run_cli(order_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err.startswith("평가 실패: ") and "0보다 큰 유한한 수여야 한다 (0.0)" in err and err.count("\n") == 1
    assert not (server_dir / "var" / "reports").exists()


def test_FR09_evaluate_order_unreadable_runs_fail(server_dir, run_cli, data):
    # 분할 결과의 시험 회차 하나(SHORT)가 기록 파일에는 있지만 DB에는 없다 (`data`는 SHORT를 DB에 만들지 않는다)
    write_split(server_dir, TRAIN, VALIDATION, ("e1", SHORT), name="ghost.json")

    code, out, err = run_cli(order_argv(split="ghost.json"), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"수집 회차를 읽을 수 없다: DB에 없는 회차 {RUN_ID[SHORT]}\n"
    assert not (server_dir / "var" / "reports").exists()


def test_FR09_evaluate_order_refuses_existing_report(server_dir, run_cli, data, monkeypatch):
    assert run_cli(order_argv(), cwd=server_dir)[0] == EXIT_OK
    csv_path, md_path, raw_path = report_files(server_dir)
    plot_path = csv_path.with_name(f"{ORDER_NAME}-order.png")
    present = [path for path in (csv_path, md_path, raw_path, plot_path) if path.exists()]
    before = [path.read_bytes() for path in present]

    def must_not_read_db(config):
        raise AssertionError("보고서가 이미 있으면 DB를 읽기 전에 거부한다")

    with monkeypatch.context() as patch:
        patch.setattr(evaluate, "open_read_db", must_not_read_db)
        code, out, err = run_cli(order_argv(), cwd=server_dir)  # 같은 날의 같은 이름이다
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"보고서가 이미 있다: {', '.join(map(str, present))}. --force로 덮어쓴다\n"
    assert [path.read_bytes() for path in present] == before  # 파일이 그대로다

    monkeypatch.setattr(evaluate, "now_utc", lambda: NOW + timedelta(hours=1))
    code, out, err = run_cli(order_argv("--force"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert "# generated_at: 2026-11-14T06:00:00+00:00" in read_report(csv_path)[0]  # 새로 썼다
    assert "- 생성 시각: 2026-11-14T06:00:00+00:00" in md_path.read_text(encoding="utf-8")
    assert read_raw(raw_path)["generated_at"] == "2026-11-14T06:00:00+00:00"


@pytest.mark.parametrize("existing", ["csv", "md", "raw", "plot"])
def test_FR09_evaluate_order_refuses_each_existing_file(server_dir, run_cli, data, monkeypatch, existing):
    # 그래프 `<이름>-order.png`도 이미 있는 보고서 검사에 들어간다. 그래프를 그릴 수 없는 PC에서도 사람이 둔 파일은 덮어쓰지 않는다.
    csv_path, md_path, raw_path = report_files(server_dir)
    present = {"csv": csv_path, "md": md_path, "raw": raw_path, "plot": csv_path.with_name(f"{ORDER_NAME}-order.png")}[existing]
    present.parent.mkdir(parents=True)
    present.write_text("사람이 쓴 파일", encoding="utf-8")

    def must_not_read_db(config):
        raise AssertionError("보고서가 이미 있으면 DB를 읽기 전에 거부한다")

    monkeypatch.setattr(evaluate, "open_read_db", must_not_read_db)
    code, out, err = run_cli(order_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"보고서가 이미 있다: {present}. --force로 덮어쓴다\n"  # 있는 파일만 알린다
    assert present.read_text(encoding="utf-8") == "사람이 쓴 파일" and reports_listing(server_dir) == [present.name]


INPUT_ERRORS = [
    # (준비, 덧붙일 인자, 오류 메시지에 들어 있어야 하는 글 모두). 종료 코드는 모두 2이고 보고서 폴더를 만들지 않는다
    pytest.param(None, ["--records", "plan-a-20261112.jsonl"], ["기록 파일 오류: 기록 파일에 회차 col-008, col-009가 없다"],
                 id="records-missing-runs"),
    pytest.param(lambda s: write_layout(s, layout_file(nodes=without(NODE_XY, "locker-3"), spots=without(SPOT_XY, NEAR_3))), [],
                 ["좌표표 오류: 좌표표에 없는 spot: 'locker-3 앞'; 좌표표에 없는 기기: 'locker-3'\n"], id="layout-missing-spot-and-node"),
    pytest.param(lambda s: write_layout(s, layout_file(spots=without(SPOT_XY, NEAR_1))), [],
                 ["좌표표 오류: 좌표표에 없는 spot: 'locker-1 앞'\n"], id="layout-missing-spot"),
    pytest.param(lambda s: write_layout(s, layout_file(nodes=without(NODE_XY, "locker-2"))), [],
                 ["좌표표 오류: 좌표표에 없는 기기: 'locker-2'\n"], id="layout-missing-node"),
    # "locker-3 앞"을 쓰는 회차가 시험 묶음(e2)이나 검증 묶음(v2)에만 있어도 좌표표에 없으면 거부한다
    pytest.param(lambda s: (write_layout(s, layout_file(spots=without(SPOT_XY, NEAR_3))),
                            write_split(s, ("t1", "t3"), ("v1",), ("e2",))), [],
                 ["좌표표 오류: 좌표표에 없는 spot: 'locker-3 앞'\n"], id="layout-missing-spot-of-test-run"),
    pytest.param(lambda s: (write_layout(s, layout_file(spots=without(SPOT_XY, NEAR_3))),
                            write_split(s, ("t1", "t3"), ("v2",), ("e1",))), [],
                 ["좌표표 오류: 좌표표에 없는 spot: 'locker-3 앞'\n"], id="layout-missing-spot-of-validation-run"),
    pytest.param(lambda s: write_layout(s, 'unit = "m"\n' + layout_file()), [], ["좌표표 오류: ", "unit: 모르는 필드다"],
                 id="layout-bad-file"),
    pytest.param(None, ["--layout", "nowhere.toml"], ["좌표표 파일이 없다: "], id="layout-file-not-found"),
    pytest.param(None, ["--records", "nowhere.jsonl"], ["기록 파일이 없다: "], id="records-file-not-found"),
    pytest.param(remove_record_files, [], ["기록 파일이 없다: ", "에 *.jsonl이 없다. --records로 기록 파일을 준다"], id="no-record-files"),
    pytest.param(lambda s: write_text(s, "var/records/plan-a-20261115.jsonl", conflicting_record("t1")), [],
                 ["기록 파일 오류: ", "plan-a-20261115.jsonl:1: 회차 col-001의 spot이 'locker-3 앞'이다 (",
                  "plan-a-20261112.jsonl:1에서는 'locker-1 앞')"], id="records-conflicting-spot"),
    pytest.param(lambda s: write_text(s, "var/records/plan-a-20261115.jsonl", "JSON이 아니다\n"), [],
                 ["기록 파일 오류: ", "plan-a-20261115.jsonl:1: JSON으로 읽을 수 없다 ("], id="records-bad-line"),
    pytest.param(lambda s: write_fit(s, fit_file(max_boundary_error=None)), [],
                 ["적합 설정 파일 오류: ", "acceptance.max_boundary_error: 필드가 없다"], id="fit-missing-key"),
    pytest.param(lambda s: write_fit(s, fit_file(max_boundary_error=1.5)), [],
                 ["적합 설정 파일 오류: ", "acceptance.max_boundary_error: 0 이상 1 이하여야 한다 (1.5)"], id="fit-out-of-range"),
    pytest.param(None, ["--fit", "nowhere.toml"], ["적합 설정 파일이 없다: "], id="fit-file-not-found"),
    pytest.param(lambda s: write_text(s, "var/splits/place-a-seed42.json", "JSON이 아니다"), [], ["분할 결과 파일 오류: "],
                 id="split-bad-file"),
    pytest.param(lambda s: (s / "var" / "nearpilot.db").unlink(), [], ["DB 파일이 없다: "], id="db-missing"),
    pytest.param(None, ["--out", "a/b"], ["--out은 경로가 아니라 이름이다. /나 \\ 없이 쓴다: a/b"], id="out-is-a-path"),
]


@pytest.mark.parametrize(("prepare", "extra", "messages"), INPUT_ERRORS)
def test_FR09_evaluate_order_input_errors(server_dir, run_cli, data, prepare, extra, messages):
    if prepare is not None:
        prepare(server_dir)
    layout = [] if "--layout" in extra else ["--layout", "place-a.toml"]  # 좌표표를 바꾸는 시험은 --layout을 직접 준다
    argv = ["evaluate-order", "--split", "place-a-seed42.json", *layout, *extra]

    code, out, err = run_cli(argv, cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert all(message in err for message in messages), err
    assert err.count("\n") == 1  # 사유는 한 줄이다
    assert not (server_dir / "var" / "reports").exists()  # 입력 오류는 보고서 폴더도 만들지 않는다


@pytest.mark.parametrize("run", ["t2", "v2", "e2"], ids=["train", "validation", "test"])
def test_FR09_evaluate_order_checks_devices_of_every_group(server_dir, run_cli, make_collection_db, run):
    # 좌표표에 없는 기기 locker-4가 학습·검증·시험 어느 한 묶음의 회차 하나에만 나와도 앵커에 들어가서 종료 코드 2다
    build_data(server_dir, make_collection_db, extra_node_in=run)

    code, out, err = run_cli(order_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == "좌표표 오류: 좌표표에 없는 기기: 'locker-4'\n"
    assert not (server_dir / "var" / "reports").exists()


def test_FR09_evaluate_order_test_only_device_does_not_change_choice(server_dir, run_cli, make_collection_db):
    # 시험 회차 e2에만 나온 기기 locker-4는 학습·검증 회차의 참여 기기가 아니다 (spec 6절 결정 3, 7).
    # locker-4는 검증 회차의 서는 위치 "locker-1 앞"에서 0.5 m라서, 검증 시점에 "못 본 기기"로 끼면 틀린 경계(확률 1)가 생겨
    # 기준 확률이 바뀐다. 시험 회차가 정하는 데 쓰이지 않으면 σ와 고른 값은 locker-4가 없을 때와 같다.
    build_data(server_dir, make_collection_db, extra_node_in="e2")
    write_layout(server_dir, layout_file(nodes={**NODE_XY, "locker-4": (1.0, 0.5)}))

    code, _, err = run_cli(order_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    rows = read_report(report_files(server_dir)[0])[1]
    assert rows["m10_frame_sigma_db"]["value"] == "1.0"
    sigma_s = best_static_sigma(train_pairs(), 1.0)
    assert (float(rows["m10_static_sigma_db"]["value"]), rows["m10_static_sigma_db"]["trials"]) == (pytest.approx(sigma_s), "90")
    assert float(rows["m10_boundary_min_prob"]["value"]) == lowest_candidate_above(boundary_probability(3, sigma_s))


# ── 그래프 ────────────────────────────────────────────────────────────


def test_FR09_evaluate_order_plot_failure_is_only_a_warning(server_dir, run_cli, data, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("그림을 저장할 수 없다")

    monkeypatch.setattr(evaluate, "write_order_plot", broken)

    code, out, err = run_cli(order_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert code == EXIT_OK
    assert err == "경고: 그래프를 만들지 못했다 (그림을 저장할 수 없다)\n"
    assert reports_listing(server_dir) == sorted([csv_path.name, md_path.name, raw_path.name])  # 보고서와 원자료는 쓴다
    assert out.splitlines()[-3:] == [f"작성: {path}" for path in (csv_path, md_path, raw_path)]


def test_FR09_evaluate_order_without_matplotlib_writes_report_and_warns(server_dir, run_cli, data, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "matplotlib", None)  # 가져올 수 없다. 이 PC에 있어도 없는 것으로 본다

    with caplog.at_level("WARNING", logger="tools.evaluation.plots"):
        code, out, err = run_cli(order_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert [record.getMessage() for record in caplog.records] == [
        'matplotlib이 없어 그래프 1장을 만들지 않았다. pip install -e ".[eval]"로 설치한다']
    assert reports_listing(server_dir) == sorted([csv_path.name, md_path.name, raw_path.name])
    assert out.splitlines()[-3:] == [f"작성: {path}" for path in (csv_path, md_path, raw_path)]


def test_FR09_evaluate_order_passes_both_tables_to_the_plot(server_dir, run_cli, data, monkeypatch):
    calls = []

    def fake(out_dir, name, *, validation, test, max_error, chosen):
        calls.append({"out_dir": out_dir, "name": name, "validation": validation, "test": test, "max_error": max_error,
                      "chosen": chosen})
        return None

    monkeypatch.setattr(evaluate, "write_order_plot", fake)

    code, _, err = run_cli(order_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    [call] = calls
    assert (call["out_dir"], call["name"], call["max_error"], call["chosen"]) == (
        report_files(server_dir)[0].parent, ORDER_NAME, 0.1, 0.72)
    # 검증 표와 시험 표는 후보 50개씩이고, 같은 후보 순서다. 검증은 고르는 데, 시험은 지표에 쓴다
    assert [stat.min_prob for stat in call["validation"]] == [stat.min_prob for stat in call["test"]]
    assert len(call["validation"]) == 50 and call["validation"][0].boundaries == 40 and call["test"][0].boundaries == 40
    assert call["validation"][22].min_prob == 0.72 and call["validation"][22].passed == 30 and call["test"][22].wrong == 10


# ── 명령줄 ────────────────────────────────────────────────────────────


def test_FR09_evaluate_order_help_and_usage_errors(run_cli, tmp_path):
    code, out, _ = run_cli(["--help"], cwd=tmp_path)
    assert code == EXIT_OK and "evaluate-order" in out

    code, out, _ = run_cli(["evaluate-order", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(
        text in out for text in ("--split", "--layout", "--fit", "--records", "--out", "--force", "--config"))

    for argv in (["evaluate-order"], ["evaluate-order", "--split", "s.json"], ["evaluate-order", "--layout", "l.toml"],
                 ["evaluate-order", "--split", "s.json", "--layout", "l.toml", "--records"],
                 ["evaluate-order", "--split", "s.json", "--layout", "l.toml", "--bad"]):
        code, out, err = run_cli(argv, cwd=tmp_path)  # 인자 오류는 argparse가 사용법과 사유를 stderr에 쓰고 2다
        assert (code, out) == (EXIT_INVALID, ""), argv
        assert "nearpilot" in err and "evaluate-order" in err, argv


def test_FR09_evaluate_order_default_report_name_uses_utc_date(server_dir, run_cli, data, monkeypatch):
    monkeypatch.setattr(evaluate, "now_utc", lambda: datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc))

    code, _, err = run_cli(order_argv(), cwd=server_dir)

    csv_path, md_path, raw_path = report_files(server_dir, "place-a-order-20261231")
    assert (code, err) == (EXIT_OK, "")
    assert csv_path.is_file() and md_path.is_file() and raw_path.is_file()
    assert "# generated_at: 2026-12-31T23:59:59+00:00" in read_report(csv_path)[0]
