"""`nearpilot eval` (NFR-06, NFR-13, specs/core-ops-cli/spec.md 5절·6절 "eval").

실제 SQLite 파일에 `main(argv)`를 같은 프로세스에서 불러 확인한다. DB의 기기, 비콘, 회차, 프레임은 `make_collection_db`가
실제 쓰기 경로로 만들고, 모델·분할 결과·적합 설정 파일은 이 파일이 쓴다. 지표 계산의 세부는 `tests/tools/test_evaluation_proximity.py`가
확인한다. 여기서는 시험 회차만 읽는지, 보고서 파일과 표준 출력과 종료 코드가 맞는지 확인한다.

데이터는 두 가지다. 둘 다 기기 둘(locker-1, locker-2)이 있는 장소 place-a이고, 회차는 20초이며 기기마다 1초에 하나씩 RSSI를 받는다.
- `hand`: 손으로 쓴 모델 파일(`HAND_MODEL`)과, 그 모델이 확신하는 RSSI의 회차들. 값을 손으로 계산할 수 있다.
- `fitted`: `test_cli_fit.py`와 같은 학습·검증 회차를 `nearpilot fit`으로 적합한 모델과, 검증 회차와 같은 환경의 시험 회차.
`window_sec`는 .5초로 둔다. 도우미가 만드는 프레임은 수신(`received_at`)이 관측(`frame_ts`)보다 50ms 늦은데,
평가 시각은 관측 시각과 같은 순간이라서 정수 초 구간이면 마지막 칸의 프레임이 모두 빠지고 사후확률이 `stay`에서 멈춘다.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli import eval as eval_cli
from nearpilot.core.proximity.fit import AnchorLayout, PlaceLayout, model_to_dict, write_model_file
from nearpilot.core.proximity.model import NodeModel, ProximityModel, load_model
from tools.evaluation.split import SplitResult, split_to_json

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 20, 5, 0, 0, tzinfo=timezone.utc)  # 테스트가 고정하는 `now_utc()`
REPORT_NAME = "place-a-v1-20261020"  # 기본 보고서 이름: <장소>-<버전>-<NOW의 UTC 날짜>
REPORT_HEADER = "지표  값  기준  통과"
UNMEASURED_ROWS = ["switch_time  -  -  미측정", "flicker  -  -  미측정"]

LAYOUT = PlaceLayout(
    "place-a",
    {"locker-1": AnchorLayout(0.0, 0.0, 0.0, 0.5), "locker-2": AnchorLayout(4.0, 0.0, 4.0, 0.5)},
    none_points=((2.0, 3.0),),
)
# 손으로 쓴 모델: 기기마다 A = −50, n = 2, 흔들림 2 dB. 자기 앞(0.5 m)은 −43.98 dBm, 다른 기기 앞(4.03 m)은 −62.11 dBm을 기대한다.
# none은 −90 dBm이다. 아래 STATE_RSSI는 이 기대값에 가까워서 정답 상태의 확률이 1로 포화된다.
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
PER_RUN = 18  # `hand`의 회차 하나가 만드는 평가 시각: 20초 회차, window 2.5초, 칸 1초 → 2.5초부터 19.5초까지

# `fitted`의 데이터: test_cli_fit.py와 같다. 기기별 (자기 앞, 다른 기기 앞, 어느 기기 앞도 아님) 평균 RSSI(dBm)와 흔들림 폭(dB).
LEVELS = {"locker-1": (-44.0, -60.0, -80.0, 1.0), "locker-2": (-46.0, -64.0, -76.0, 2.0)}
JITTER = (-1.0, 0.0, 1.0, 0.0)  # 평균은 그대로 두고 표준편차만 0보다 크게 한다
TRAIN_LABELS = ("locker-1", "locker-1", "locker-2", "locker-2", "none", "none")
VALIDATION_LABELS = ("locker-1", "locker-2", "none")
TEST_LABELS = ("locker-1", "locker-2", "none")


def fit_file(*, max_misselection: float = 0.05, max_ece: float = 0.05, taus: str = "[0.5, 1.0, 2.0]",
             stays: str = "[0.8, 0.9]", window_secs: str = "[2.5, 3.5]") -> str:
    """적합 설정 파일 내용. `[acceptance]`가 이 명령이 읽는 절이고, 나머지는 `nearpilot fit`이 읽는다."""
    return f"""\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
min_distance_m = 0.3
taus = {taus}
stays = {stays}
window_secs = {window_secs}

[acceptance]
max_misselection = {max_misselection}
max_ece = {max_ece}
"""


def rssis(node_id: str, label: str, seconds: int, *, validation: bool = False) -> list[float]:
    """`node_id`가 `label` 회차에서 1초마다 받는 RSSI `seconds`개 (`test_cli_fit.py`와 같다).

    검증·시험 회차는 학습 회차와 환경이 달라서 기기 앞 신호는 5dB 약하고, 다른 기기 앞은 3dB, none은 8dB 세고, 흔들림은 두 배다.
    그래서 모델이 정답을 완전히 확신하지 못한다.
    """
    near, far, none, spread = LEVELS[node_id]
    if label == "none":
        mean, spread, shift = none, 2 * spread, 8.0
    elif label == node_id:
        mean, shift = near, -5.0
    else:
        mean, shift = far, 3.0
    if validation:
        mean, spread = mean + shift, 2 * spread
    return [mean + spread * JITTER[k % len(JITTER)] for k in range(seconds)]


def fitted_run(index: int, label: str, *, validation: bool, seconds: int = 20) -> tuple:
    frames = {node_id: rssis(node_id, label, seconds, validation=validation) for node_id in LAYOUT.nodes}
    return label, T0 + timedelta(seconds=60 * index), seconds, frames


def hand_run(index: int, label: str, *, looks_like: str | None = None, seconds: int = 20,
             in_progress: bool = False) -> tuple:
    """`make_collection_db`의 회차 한 건. `looks_like`는 라벨과 다른 상태의 RSSI를 쓴다 (라벨을 잘못 단 회차)."""
    frames = {node_id: [rssi] * seconds for node_id, rssi in STATE_RSSI[looks_like or label].items()}
    return label, T0 + timedelta(seconds=60 * index), None if in_progress else seconds, frames


def hand_runs() -> list[tuple]:
    """맞게 단 회차 셋(locker-1, locker-2, none)과, locker-2 앞의 RSSI에 locker-1을 라벨로 단 회차 하나."""
    return [hand_run(0, "locker-1"), hand_run(1, "locker-2"), hand_run(2, "none"),
            hand_run(3, "locker-1", looks_like="locker-2")]


def write_model(server: Path, model: ProximityModel = HAND_MODEL) -> Path:
    return write_model_file(server / "var" / "models", model)


def write_layout(server: Path) -> Path:
    path = server / "config" / "layouts" / "place-a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"place_id": "place-a", "none_points": [list(p) for p in LAYOUT.none_points],
            "nodes": {n: {"x": a.x, "y": a.y, "front_x": a.front_x, "front_y": a.front_y}
                      for n, a in LAYOUT.nodes.items()}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_fit(server: Path, name: str = "place-a.toml", text: str | None = None) -> Path:
    path = server / "config" / "fit" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fit_file() if text is None else text, encoding="utf-8")
    return path


def write_split(server: Path, train, validation, test, *, place_id: str = "place-a",
                name: str = "place-a-seed42.json") -> Path:
    """분할 결과 파일. `labels`는 eval이 읽지 않아서 비워 둔다."""
    result = SplitResult(place_id, 42, (0.6, 0.2, 0.2), tuple(train), tuple(validation), tuple(test), (), {})
    path = server / "var" / "splits" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(split_to_json(result, created_at=T0, db_path="nearpilot.db"), encoding="utf-8", newline="\n")
    return path


def build_hand(server: Path, make_collection_db, runs: list[tuple] | None = None, **db_options) -> SimpleNamespace:
    """DB에 `runs`(기본은 `hand_runs()`)를 만들고, 모두를 시험 회차로 둔 분할 결과 파일과 손으로 쓴 모델, 적합 설정 파일을 쓴다."""
    ids = make_collection_db(server, LAYOUT, hand_runs() if runs is None else runs, **db_options)
    write_model(server)
    write_fit(server)
    write_split(server, (), (), ids)
    return SimpleNamespace(test=tuple(ids))


def build_fitted(server: Path, make_collection_db, run_cli) -> SimpleNamespace:
    """학습 회차 6건, 검증 회차 3건, 시험 회차 3건을 DB에 만들고 `nearpilot fit`으로 모델 v1을 쓴다."""
    runs = [fitted_run(i, label, validation=False) for i, label in enumerate(TRAIN_LABELS)]
    runs += [fitted_run(6 + i, label, validation=True) for i, label in enumerate(VALIDATION_LABELS)]
    runs += [fitted_run(9 + i, label, validation=True) for i, label in enumerate(TEST_LABELS)]
    ids = make_collection_db(server, LAYOUT, runs)
    data = SimpleNamespace(train=tuple(ids[:6]), validation=tuple(ids[6:9]), test=tuple(ids[9:]))
    write_layout(server)
    write_fit(server)
    write_split(server, data.train, data.validation, data.test)
    code, _, err = run_cli(["fit", "place-a.json", "--split", "place-a-seed42.json", "--version", "v1"], cwd=server)
    assert (code, err) == (EXIT_OK, "")
    return data


def eval_argv(*extra: str, model: str = "place-a/v1") -> list[str]:
    return ["eval", model, "--split", "place-a-seed42.json", *extra]


def report_files(server: Path, name: str = REPORT_NAME) -> tuple[Path, Path]:
    reports = (server / "var" / "reports").resolve()
    return reports / f"{name}.csv", reports / f"{name}.md"


def read_report(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    """보고서 CSV의 머리말 주석 줄과, 지표 이름 → 행 (CSV 순서)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    comments = [line for line in lines if line.startswith("#")]
    rows = list(csv.DictReader(line for line in lines if not line.startswith("#")))
    return comments, {row["metric"]: row for row in rows}


def schema_version(server: Path) -> int:
    conn = sqlite3.connect(server / "var" / "nearpilot.db")
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eval_cli, "now_utc", lambda: NOW)


@pytest.fixture
def hand(server_dir, make_collection_db) -> SimpleNamespace:
    """`hand_runs()` 네 건이 모두 시험 회차(`col-001`~`col-004`)인 DB와 손으로 쓴 모델 v1, 이름으로 찾는 기본 위치의 입력 파일."""
    data = build_hand(server_dir, make_collection_db)
    assert data.test == ("col-001", "col-002", "col-003", "col-004")
    return data


@pytest.fixture
def fitted(server_dir, make_collection_db, run_cli) -> SimpleNamespace:
    return build_fitted(server_dir, make_collection_db, run_cli)


# ── 정상 실행 ─────────────────────────────────────────────────────────


def test_NFR06_eval_writes_report(server_dir, run_cli, fitted):
    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    csv_path, md_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert csv_path.is_file() and md_path.is_file()
    assert sorted(p.name for p in csv_path.parent.iterdir()) == [csv_path.name, md_path.name]
    comments, rows = read_report(csv_path)
    assert list(rows) == ["front_misselection_rate", "ece", "switch_time", "flicker"]  # 4행
    # fit이 쓴 모델이 시험 회차(검증 회차와 같은 환경)를 맞힌다. 회차의 평가 시각 수는 모델이 고른 window_sec에서 나온다.
    per_run = math.floor(20 - load_model(server_dir / "var" / "models" / "place-a" / "v1.json").window_sec) + 1
    assert (rows["front_misselection_rate"]["trials"], rows["front_misselection_rate"]["failures"]) == (
        str(2 * per_run), "0")
    assert rows["ece"]["trials"] == str(3 * per_run)
    assert 0.0 < float(rows["ece"]["value"]) < 1.0  # 모델이 정답을 완전히 확신하지는 않는다
    assert comments[0] == f"# db_path: {(server_dir / 'var' / 'nearpilot.db').resolve()}"
    assert comments[1] == f"# schema_version: {schema_version(server_dir)}"  # 상수가 아니라 DB의 PRAGMA user_version이다
    assert comments[3:] == [f"# sample_count: {3 * per_run}", "# generated_at: 2026-10-20T05:00:00+00:00"]
    assert out.splitlines()[-2:] == [f"작성: {csv_path}", f"작성: {md_path}"]


def test_NFR06_eval_report_values_match_hand_calculation(server_dir, run_cli, hand):
    # 회차 넷, 평가 시각 18개씩 → 표본 72개. 기기 라벨 회차는 셋(54개)이고, 그중 locker-2 앞의 RSSI에 locker-1을 달아 둔
    # 회차의 18개는 모델이 locker-2를 확신해서 오선택이다. 모든 표본의 신뢰도는 1이고 정답 비율은 54/72다.
    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    csv_path, md_path = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")  # 기준에 못 미쳐도 종료 코드는 0이다
    comments, rows = read_report(csv_path)
    rate, ece = rows["front_misselection_rate"], rows["ece"]
    assert (rate["trials"], rate["failures"]) == (str(3 * PER_RUN), str(PER_RUN))
    assert float(rate["value"]) == pytest.approx(1 / 3)
    assert (rate["limit"], rate["passed"], rate["note"]) == ("0.05", "False", "")
    assert ece["trials"] == str(4 * PER_RUN) and ece["failures"] == "" and ece["note"] == ""
    assert float(ece["value"]) == pytest.approx(0.25)
    assert (ece["limit"], ece["passed"]) == ("0.05", "False")
    assert "# sample_count: 72" in comments
    assert out.splitlines() == [
        REPORT_HEADER,
        "front_misselection_rate  0.3333  0.0500  미달",
        "ece  0.2500  0.0500  미달",
        *UNMEASURED_ROWS,
        f"작성: {csv_path}",
        f"작성: {md_path}",
    ]
    markdown = md_path.read_text(encoding="utf-8")
    assert f"- 입력 DB 경로: `{(server_dir / 'var' / 'nearpilot.db').resolve()}`" in markdown
    assert f"- 스키마 버전: {schema_version(server_dir)}" in markdown
    assert "- 표본 수: 72" in markdown and "- 생성 시각: 2026-10-20T05:00:00+00:00" in markdown
    assert "| metric | value | limit | passed | trials | failures | note |" in markdown
    assert "| " + " | ".join(["switch_time", "", "", "", "", "", "미측정"]) + " |" in markdown


def test_NFR06_eval_perfect_model_passes_with_confidence_bound(server_dir, run_cli, make_collection_db):
    # 맞게 단 회차만 있으면 오선택이 0건이다. 실패가 0건이면 보고서가 신뢰구간 상한(95%)을 덧붙인다.
    build_hand(server_dir, make_collection_db, hand_runs()[:3])

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    csv_path, _ = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    rows = read_report(csv_path)[1]
    rate, ece = rows["front_misselection_rate"], rows["ece"]
    assert (rate["trials"], rate["failures"], rate["passed"]) == (str(2 * PER_RUN), "0", "True")
    assert float(rate["value"]) == 0.0 and float(ece["value"]) == pytest.approx(0.0, abs=1e-9)
    assert float(rate["confidence_bound_95"]) == pytest.approx(1 - 0.025 ** (1 / (2 * PER_RUN)))
    assert ece["passed"] == "True" and ece["trials"] == str(3 * PER_RUN)
    assert out.splitlines()[:3] == [REPORT_HEADER, "front_misselection_rate  0.0000  0.0500  통과",
                                    "ece  0.0000  0.0500  통과"]


def test_NFR06_eval_uses_test_runs_only(server_dir, run_cli, fitted, monkeypatch):
    # 분할 파일의 학습·검증 회차를 DB에 없는 ID로 바꾼다. 읽었다면 거부된다.
    write_split(server_dir, ("col-998",), ("col-999",), fitted.test)
    asked = []
    real = eval_cli.load_labeled_runs
    monkeypatch.setattr(eval_cli, "load_labeled_runs", lambda conn, ids: asked.append(tuple(ids)) or real(conn, ids))

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert asked == [fitted.test]  # 한 번, 시험 회차만 읽는다
    assert all(path.is_file() for path in report_files(server_dir))


def test_NFR06_eval_reports_unmeasured_items(server_dir, run_cli, hand):
    assert run_cli(eval_argv(), cwd=server_dir)[0] == EXIT_OK

    rows = read_report(report_files(server_dir)[0])[1]
    for metric in ("switch_time", "flicker"):
        assert rows[metric] == {"metric": metric, "value": "", "limit": "", "passed": "", "trials": "", "failures": "",
                                "note": "미측정"}
    assert "미측정" in report_files(server_dir)[1].read_text(encoding="utf-8")


def test_NFR06_eval_limits_come_from_the_fit_file(server_dir, run_cli, hand):
    # 기준 두 값이 서로 다르고 파일 이름으로 고른다. 기본 파일은 place-a.toml(0.05)이다.
    write_fit(server_dir, "lenient.toml", fit_file(max_misselection=0.5, max_ece=0.4))
    write_fit(server_dir, "strict.toml", fit_file(max_misselection=0.0, max_ece=0.0))

    code, out, err = run_cli(eval_argv("--fit", "lenient.toml"), cwd=server_dir)
    assert (code, err) == (EXIT_OK, "")
    rows = read_report(report_files(server_dir)[0])[1]
    assert (rows["front_misselection_rate"]["limit"], rows["front_misselection_rate"]["passed"]) == ("0.5", "True")
    assert (rows["ece"]["limit"], rows["ece"]["passed"]) == ("0.4", "True")  # 1/3 <= 0.5, 0.25 <= 0.4
    assert out.splitlines()[1:3] == ["front_misselection_rate  0.3333  0.5000  통과", "ece  0.2500  0.4000  통과"]

    code, out, err = run_cli(eval_argv("--fit", "strict.toml", "--out", "strict"), cwd=server_dir)
    assert (code, err) == (EXIT_OK, "")  # 기준에 못 미쳐도 종료 코드는 0이다
    rows = read_report(report_files(server_dir, "strict")[0])[1]
    assert (rows["front_misselection_rate"]["limit"], rows["front_misselection_rate"]["passed"]) == ("0.0", "False")
    assert (rows["ece"]["limit"], rows["ece"]["passed"]) == ("0.0", "False")
    assert out.splitlines()[1:3] == ["front_misselection_rate  0.3333  0.0000  미달", "ece  0.2500  0.0000  미달"]

    # 한 지표만 미달일 수 있다: 오선택률은 0.5 이하라 통과, ECE는 0.2 이하가 아니라 미달
    write_fit(server_dir, "mixed.toml", fit_file(max_misselection=0.5, max_ece=0.2))
    assert run_cli(eval_argv("--fit", "mixed.toml", "--out", "mixed"), cwd=server_dir)[0] == EXIT_OK
    rows = read_report(report_files(server_dir, "mixed")[0])[1]
    assert (rows["front_misselection_rate"]["passed"], rows["ece"]["passed"]) == ("True", "False")


def test_NFR06_eval_without_device_label_runs(server_dir, run_cli, make_collection_db):
    # 시험 회차가 모두 none이면 오선택률을 낼 수 없다. 값과 통과 여부를 비우고 ECE만 낸다.
    build_hand(server_dir, make_collection_db, [hand_run(0, "none"), hand_run(1, "none")])

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    csv_path, _ = report_files(server_dir)
    assert (code, err) == (EXIT_OK, "")
    rows = read_report(csv_path)[1]
    assert rows["front_misselection_rate"] == {
        "metric": "front_misselection_rate", "value": "", "limit": "0.05", "passed": "", "trials": "0",
        "failures": "", "note": "기기 라벨 시험 회차 없음"}
    assert rows["ece"]["trials"] == str(2 * PER_RUN) and rows["ece"]["passed"] == "True"
    assert out.splitlines()[:3] == [REPORT_HEADER, "front_misselection_rate  -  0.0500  기기 라벨 시험 회차 없음",
                                    "ece  0.0000  0.0500  통과"]


def test_NFR06_eval_uses_frame_interval_from_settings(server_dir, run_cli, hand, write_config, monkeypatch):
    write_config(server_dir, settings={"version": "t1", "rssi_frame_sec": 0.5})
    seen = []
    real = eval_cli.collect_samples

    def spy(*args, **kwargs):
        seen.append(kwargs["bin_sec"])
        return real(*args, **kwargs)

    monkeypatch.setattr(eval_cli, "collect_samples", spy)

    code, _, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert seen == [0.5]
    # 칸이 0.5초면 평가 시각은 2.5초부터 20초까지 0.5초 간격이라 회차마다 36개다
    assert "# sample_count: 144" in read_report(report_files(server_dir)[0])[0]


def test_NFR06_eval_default_report_name_uses_utc_date(server_dir, run_cli, hand, monkeypatch):
    monkeypatch.setattr(eval_cli, "now_utc", lambda: datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc))

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    csv_path, md_path = report_files(server_dir, "place-a-v1-20261231")
    assert csv_path.is_file() and md_path.is_file()
    assert "# generated_at: 2026-12-31T23:59:59+00:00" in read_report(csv_path)[0]
    assert out.splitlines()[-2:] == [f"작성: {csv_path}", f"작성: {md_path}"]


def test_NFR06_eval_out_names_the_report(server_dir, run_cli, hand):
    code, out, err = run_cli(eval_argv("--out", "demo-run"), cwd=server_dir)

    csv_path, md_path = report_files(server_dir, "demo-run")
    assert (code, err) == (EXIT_OK, "")
    assert csv_path.is_file() and md_path.is_file()
    assert [p.name for p in sorted(csv_path.parent.iterdir())] == ["demo-run.csv", "demo-run.md"]
    assert out.splitlines()[-2:] == [f"작성: {csv_path}", f"작성: {md_path}"]


@pytest.mark.parametrize("name", ["a/b", "a\\b", "../x", "x/", "/x", "C:\\x"])
def test_NFR06_eval_out_must_be_a_name(server_dir, run_cli, hand, name):
    code, out, err = run_cli(eval_argv("--out", name), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"--out은 경로가 아니라 이름이다. /나 \\ 없이 쓴다: {name}\n"
    assert not (server_dir / "var" / "reports").exists()  # 폴더도 만들지 않는다


def test_NFR06_eval_does_not_change_the_db(server_dir, run_cli, hand):
    db_file = server_dir / "var" / "nearpilot.db"

    def counts() -> list[int]:
        conn = sqlite3.connect(db_file)
        try:
            return [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("collection_run", "rssi_observation", "node", "beacon", "user", "place")]
        finally:
            conn.close()

    before = counts()
    assert run_cli(eval_argv(), cwd=server_dir)[0] == EXIT_OK

    assert counts() == before and before[0] == 4 and before[1] == 4 * 2 * 20


# ── 거부 (종료 코드 1) ────────────────────────────────────────────────


def test_NFR13_eval_report_requires_force(server_dir, run_cli, hand, monkeypatch):
    assert run_cli(eval_argv(), cwd=server_dir)[0] == EXIT_OK
    csv_path, md_path = report_files(server_dir)
    before = (csv_path.read_bytes(), md_path.read_bytes())

    monkeypatch.setattr(eval_cli, "now_utc", lambda: NOW + timedelta(hours=1))  # 같은 날(같은 이름)의 다른 시각
    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"보고서가 이미 있다: {csv_path}, {md_path}. --force로 덮어쓴다\n"
    assert (csv_path.read_bytes(), md_path.read_bytes()) == before  # 파일이 그대로다

    code, out, err = run_cli(eval_argv("--force"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert csv_path.read_bytes() != before[0] and md_path.read_bytes() != before[1]  # 둘 다 새로 썼다
    assert "# generated_at: 2026-10-20T06:00:00+00:00" in read_report(csv_path)[0]
    assert "- 생성 시각: 2026-10-20T06:00:00+00:00" in md_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("existing", ["csv", "md"])
def test_NFR13_eval_report_requires_force_when_either_file_exists(server_dir, run_cli, hand, existing):
    csv_path, md_path = report_files(server_dir)
    present, absent = (csv_path, md_path) if existing == "csv" else (md_path, csv_path)
    present.parent.mkdir(parents=True)
    present.write_text("사람이 쓴 파일", encoding="utf-8")

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"보고서가 이미 있다: {present}. --force로 덮어쓴다\n"  # 있는 파일만 알린다
    assert present.read_text(encoding="utf-8") == "사람이 쓴 파일" and not absent.exists()

    assert run_cli(eval_argv("--force"), cwd=server_dir)[0] == EXIT_OK  # --force면 있는 쪽도 덮어쓴다
    assert csv_path.is_file() and md_path.is_file() and present.read_text(encoding="utf-8") != "사람이 쓴 파일"


def test_NFR13_eval_force_without_existing_report_just_writes(server_dir, run_cli, hand):
    code, _, err = run_cli(eval_argv("--force"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert all(path.is_file() for path in report_files(server_dir))


def test_NFR06_eval_creates_missing_report_folders(server_dir, run_cli, write_config, hand, tmp_path):
    # 설정이 다른 폴더에 있다. 보고서 폴더(그 설정 기준 var/reports)와 그 부모 var/가 아직 없다.
    # DB와 모델은 server_dir의 것을 절대 경로로 가리킨다.
    other = tmp_path / "other"
    config = write_config(
        other,
        database={"path": str(server_dir / "var" / "nearpilot.db"), "audit_jsonl": "../var/audit.jsonl"},
        proximity={"model_root": str(server_dir / "var" / "models")},
    )
    split = server_dir / "var" / "splits" / "place-a-seed42.json"
    fit = server_dir / "config" / "fit" / "place-a.toml"
    assert not (other / "var").exists()

    code, out, err = run_cli(
        ["eval", "place-a/v1", "--split", str(split), "--fit", str(fit), "--out", "far", "--config", str(config)],
        cwd=tmp_path)

    assert (code, err) == (EXIT_OK, "")
    assert (other / "var" / "reports" / "far.csv").is_file() and (other / "var" / "reports" / "far.md").is_file()


@pytest.mark.parametrize("existing", ["csv", "md"])
def test_NFR13_eval_checks_existing_report_before_reading_db(server_dir, run_cli, hand, monkeypatch, existing):
    csv_path, md_path = report_files(server_dir)
    present = csv_path if existing == "csv" else md_path
    present.parent.mkdir(parents=True)
    present.write_text("사람이 쓴 파일", encoding="utf-8")

    def must_not_read_db(config):
        raise AssertionError("보고서가 이미 있으면 DB를 읽기 전에 거부한다")

    monkeypatch.setattr(eval_cli, "open_read_db", must_not_read_db)
    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"보고서가 이미 있다: {present}. --force로 덮어쓴다\n"


def test_NFR13_eval_report_created_during_eval_is_not_overwritten(server_dir, run_cli, hand, monkeypatch):
    csv_path, md_path = report_files(server_dir)
    real = eval_cli.metric_rows

    def rows_then_other_process_writes(*args, **kwargs):
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text("다른 곳에서 만든 파일", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(eval_cli, "metric_rows", rows_then_other_process_writes)

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"보고서가 이미 있다: {csv_path}. --force로 덮어쓴다\n"
    assert csv_path.read_text(encoding="utf-8") == "다른 곳에서 만든 파일" and not md_path.exists()


def test_NFR06_eval_unreadable_runs_fail(server_dir, run_cli, make_collection_db):
    runs = [*hand_runs(), hand_run(4, "locker-1", in_progress=True)]
    data = build_hand(server_dir, make_collection_db, runs)  # 시험 회차의 마지막이 진행 중이다
    assert data.test[-1] == "col-005"

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "수집 회차를 읽을 수 없다: 진행 중인 회차 col-005\n"
    write_split(server_dir, (), (), (*data.test[:-1], "col-999"), name="ghost.json")  # 시험 회차 하나가 DB에 없다
    code, out, err = run_cli(["eval", "place-a/v1", "--split", "ghost.json"], cwd=server_dir)
    assert (code, out) == (EXIT_FAILED, "")
    assert err == "수집 회차를 읽을 수 없다: DB에 없는 회차 col-999\n"
    assert not (server_dir / "var" / "reports").exists()  # 보고서도 보고서 폴더도 만들지 않는다


def test_NFR06_eval_run_without_evaluation_times_fails(server_dir, run_cli, make_collection_db):
    # 두 번째 시험 회차가 2초라서 window_sec(2.5초)보다 짧다
    build_hand(server_dir, make_collection_db, [hand_run(0, "locker-1"), hand_run(1, "locker-2", seconds=2)])

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "평가 실패: 회차 'col-002': 구간이 window_sec(2.5초)보다 짧아 평가 시각이 없다\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_eval_label_missing_from_the_model_fails(server_dir, run_cli, make_collection_db):
    # locker-3은 DB에 있는 기기지만 모델(locker-1, locker-2)에는 없다
    build_hand(server_dir, make_collection_db, [hand_run(0, "locker-1"), hand_run(1, "locker-3", looks_like="locker-1")],
               unplaced=("locker-3",))

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "평가 실패: 회차 'col-002': 라벨 'locker-3'에 해당하는 기기가 모델에 없다 ('none'도 아니다)\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_eval_without_test_runs_fails(server_dir, run_cli, hand):
    write_split(server_dir, hand.test, (), ())  # 시험 회차가 없다

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "평가 실패: 표본이 없어 ECE를 계산할 수 없다\n"
    assert not (server_dir / "var" / "reports").exists()


# ── 입력 오류 (종료 코드 2) ───────────────────────────────────────────


@pytest.mark.parametrize("model", [
    "place-a", "place-a/v1/v2", "/v1", "place-a/", "/", "place-a\\v1", "place-a/..", "../v1", "./v1", "place-a/./v1",
    "place\\a/v1", "place-a/v\\1",
])
def test_NFR06_eval_model_argument_must_be_place_slash_version(server_dir, run_cli, model):
    code, out, err = run_cli(eval_argv(model=model), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"평가할 모델은 PLACE/VERSION 형식으로 쓴다: {model}\n"
    assert not (server_dir / "var").exists()


def test_NFR06_eval_missing_model_is_input_error(server_dir, run_cli):
    models = (server_dir / "var" / "models" / "place-a").resolve()

    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"모델 파일이 없다: {models / 'v1.json'}. {models}에 있는 파일: (없음)\n"

    write_model(server_dir)  # 다른 버전이 있으면 안내한다
    write_model(server_dir, replace(HAND_MODEL, model_version="v2"))
    code, out, err = run_cli(eval_argv(model="place-a/v9"), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"모델 파일이 없다: {models / 'v9.json'}. {models}에 있는 파일: v1.json, v2.json\n"
    code, out, err = run_cli(eval_argv(model="place-b/v1"), cwd=server_dir)  # 다른 장소의 모델은 그 장소 폴더에서 찾는다
    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"모델 파일이 없다: {models.parent / 'place-b' / 'v1.json'}.")
    assert not (server_dir / "var" / "reports").exists() and not (server_dir / "var" / "nearpilot.db").exists()


def test_NFR06_eval_rejects_invalid_model_file(server_dir, run_cli):
    path = write_model(server_dir)
    path.write_text("JSON이 아니다", encoding="utf-8")

    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"모델 파일 오류: {path.resolve()}: UTF-8 JSON으로 읽을 수 없다 (") and err.count("\n") == 1

    # 모델 파일의 장소가 폴더 이름과 다르다
    path.write_text(json.dumps(model_to_dict(replace(HAND_MODEL, place_id="place-b"))), encoding="utf-8")
    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith("모델 파일 오류: ") and "place_id는 디렉터리 이름과 같아야 한다" in err
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_eval_rejects_model_of_another_place(server_dir, run_cli, hand, monkeypatch):
    real = eval_cli.load_model
    monkeypatch.setattr(eval_cli, "load_model", lambda path: replace(real(path), place_id="place-b"))

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == "모델의 장소(place-b)와 평가할 장소(place-a)가 다르다\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_eval_split_place_mismatch_rejected(server_dir, run_cli, hand):
    write_split(server_dir, (), (), hand.test, place_id="place-b")

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == "분할 결과의 장소(place-b)와 평가할 장소(place-a)가 다르다\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_eval_checks_input_files_in_order(server_dir, run_cli):
    models = (server_dir / "var" / "models" / "place-a").resolve()
    splits = (server_dir / "var" / "splits").resolve()
    fit_dir = (server_dir / "config" / "fit").resolve()
    db_file = (server_dir / "var" / "nearpilot.db").resolve()

    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"모델 파일이 없다: {models / 'v1.json'}. {models}에 있는 파일: (없음)\n"

    write_model(server_dir)
    code, _, err = run_cli(eval_argv(), cwd=server_dir)
    assert code == EXIT_INVALID
    assert err == f"분할 결과 파일이 없다: {splits / 'place-a-seed42.json'}. {splits}에 있는 파일: (없음)\n"

    write_split(server_dir, (), (), ["col-001"])
    code, _, err = run_cli(eval_argv(), cwd=server_dir)
    assert code == EXIT_INVALID
    assert err == f"적합 설정 파일이 없다: {fit_dir / 'place-a.toml'}. {fit_dir}에 있는 파일: (없음)\n"

    write_fit(server_dir, "place-b.toml")  # 다른 장소의 설정 파일은 이 장소의 파일이 아니다
    code, _, err = run_cli(eval_argv(), cwd=server_dir)
    assert code == EXIT_INVALID and err.endswith(f"{fit_dir}에 있는 파일: place-b.toml\n")

    write_fit(server_dir)  # 파일 인자를 모두 풀고서야 DB를 연다
    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"DB 파일이 없다: {db_file}. 먼저 nearpilot serve --collect-only로 서버를 한 번 시작한다\n"
    assert not db_file.exists() and not (server_dir / "var" / "reports").exists()  # 빈 DB도 보고서 폴더도 만들지 않는다


def test_NFR06_eval_rejects_invalid_files(server_dir, run_cli, hand):
    split_file = write_split(server_dir, (), (), hand.test)
    split_file.write_text("JSON이 아니다", encoding="utf-8")
    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"분할 결과 파일 오류: {split_file.resolve()}: UTF-8 JSON으로 읽을 수 없다 (")

    write_split(server_dir, (), (), hand.test)
    write_fit(server_dir, text=fit_file().replace("[acceptance]\nmax_misselection = 0.05\nmax_ece = 0.05\n", ""))
    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == "적합 설정 파일 오류: acceptance: 필드가 없다\n"

    write_fit(server_dir, text=fit_file(max_ece=1.5))
    code, out, err = run_cli(eval_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == "적합 설정 파일 오류: acceptance.max_ece: 0 이상 1 이하여야 한다 (1.5)\n"
    assert not (server_dir / "var" / "reports").exists()


def test_NFR06_eval_unwritable_report_folder_is_input_error(server_dir, run_cli, hand):
    reports = (server_dir / "var" / "reports").resolve()
    reports.write_text("폴더 자리에 파일이 있다", encoding="utf-8")

    code, out, err = run_cli(eval_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"보고서 폴더를 만들 수 없다: {reports} (")
    assert reports.read_text(encoding="utf-8") == "폴더 자리에 파일이 있다"


def test_NFR06_eval_unwritable_report_file_is_input_error(server_dir, run_cli, hand):
    csv_path, _ = report_files(server_dir)
    csv_path.mkdir(parents=True)  # 보고서 자리에 폴더가 있다

    code, out, err = run_cli(eval_argv("--force"), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"보고서를 쓸 수 없다: {csv_path} (")
    assert csv_path.is_dir()


# ── 읽기 전용 ─────────────────────────────────────────────────────────


def test_NFR13_eval_reads_through_a_read_only_connection(server_dir, run_cli, hand, monkeypatch):
    real = eval_cli.load_labeled_runs
    seen = []

    def checking(conn, ids):
        with pytest.raises(sqlite3.OperationalError):  # 쓰기를 시도하면 sqlite가 거부한다
            conn.execute("DELETE FROM collection_run")
        seen.append(conn)
        return real(conn, ids)

    monkeypatch.setattr(eval_cli, "load_labeled_runs", checking)

    code, _, _ = run_cli(eval_argv(), cwd=server_dir)

    assert code == EXIT_OK and len(seen) == 1
    with pytest.raises(sqlite3.ProgrammingError):  # 읽고 나면 연결을 닫는다
        seen[0].execute("SELECT 1")


def test_NFR06_eval_closes_connection_on_failure(server_dir, run_cli, hand, monkeypatch):
    opened = []
    real = eval_cli.open_read_db

    def spy(config):
        conn = real(config)
        opened.append(conn)
        return conn

    def failing(conn, ids):
        raise RuntimeError("읽다가 실패")

    monkeypatch.setattr(eval_cli, "open_read_db", spy)
    monkeypatch.setattr(eval_cli, "load_labeled_runs", failing)

    with pytest.raises(RuntimeError, match="읽다가 실패"):
        run_cli(eval_argv(), cwd=server_dir)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    assert not (server_dir / "var" / "reports").exists()


# ── 명령줄 ───────────────────────────────────────────────────────────


def test_NFR06_eval_file_arguments_name_and_path(server_dir, run_cli, hand, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    config = str(server_dir / "config" / "server.toml")
    # 실행 폴더에만 있는 사본이다. 경로로 주면 이 사본을 읽고, 이름으로 주면 기본 폴더에서 찾으니 없다.
    (work / "mine-split.json").write_bytes(write_split(server_dir, (), (), hand.test[:2]).read_bytes())
    (work / "mine.toml").write_text(fit_file(max_misselection=0.5, max_ece=0.4), encoding="utf-8")

    def run(split: str, fit: str) -> tuple[int, str, str]:
        return run_cli(["eval", "place-a/v1", "--split", split, "--fit", fit, "--out", "viaw", "--config", config],
                       cwd=work)

    code, out, err = run("./mine-split.json", "./mine.toml")
    assert (code, err) == (EXIT_OK, "")
    rows = read_report(report_files(server_dir, "viaw")[0])[1]
    assert rows["ece"]["trials"] == str(2 * PER_RUN) and rows["ece"]["limit"] == "0.4"  # 사본의 시험 회차 둘, 사본의 기준
    for names, kind in ((("mine-split.json", "./mine.toml"), "분할 결과"), (("./mine-split.json", "mine.toml"), "적합 설정")):
        code, out, err = run(*names)
        assert (code, out) == (EXIT_INVALID, ""), names
        assert err.startswith(f"{kind} 파일이 없다: "), names


def test_NFR06_eval_config_option_goes_anywhere(server_dir, run_cli, hand, tmp_path):
    config = str(server_dir / "config" / "server.toml")
    work = tmp_path / "work"
    work.mkdir()

    for i, argv in enumerate((["--config", config, *eval_argv("--out", "r0")],
                              [*eval_argv("--out", "r1")[:1], "--config", config, *eval_argv("--out", "r1")[1:]],
                              [*eval_argv("--out", "r2"), "--config", config])):
        code, out, err = run_cli(argv, cwd=work)  # 실행 폴더와 상관없이 같은 설정을 읽는다
        assert (code, err) == (EXIT_OK, ""), argv
        assert report_files(server_dir, f"r{i}")[0].is_file()
    assert run_cli(eval_argv("--out", "r3"), cwd=work)[0] == EXIT_INVALID  # 쓰지 않으면 실행 폴더의 config/server.toml


def test_NFR06_eval_help_and_usage_errors(run_cli, tmp_path):
    code, out, _ = run_cli(["--help"], cwd=tmp_path)
    assert code == EXIT_OK and "eval" in out

    code, out, _ = run_cli(["eval", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(
        text in out for text in ("PLACE/VERSION", "--split", "--fit", "--out", "--force", "--config"))

    for argv in (["eval"], ["eval", "place-a/v1"], ["eval", "--split", "s.json"],
                 ["eval", "place-a/v1", "other/v2", "--split", "s.json"], ["eval", "place-a/v1", "--split", "s.json", "--bad"]):
        code, out, err = run_cli(argv, cwd=tmp_path)  # 인자 오류는 argparse가 사용법과 사유를 stderr에 쓰고 2다
        assert (code, out) == (EXIT_INVALID, ""), argv
        assert "nearpilot" in err and "eval" in err, argv
