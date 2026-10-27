"""FR-12A 평가 리포트 읽기. 리포트는 `tools/evaluation/report.py` 의 `_write_csv` 와 같은 모양(머리 줄 다섯 개 뒤에
`csv.DictWriter` 표)으로 쓴다. `core` 시험은 `tools` 를 import 하지 않는다."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from nearpilot.core.dashboard.evaluation import evaluation_of

COLUMNS = ["metric", "value", "limit", "passed", "trials", "failures", "note"]
# `tools/evaluation/proximity.py` 의 `metric_rows` 가 만드는 행 네 개
ROWS = [
    {"metric": "front_misselection_rate", "value": 0.02, "limit": 0.05, "passed": True, "trials": 2100,
     "failures": 42, "note": ""},
    {"metric": "ece", "value": 0.08, "limit": 0.05, "passed": False, "trials": 2100, "failures": "", "note": ""},
    {"metric": "switch_time", "value": "", "limit": "", "passed": "", "trials": "", "failures": "",
     "note": "미측정"},
    {"metric": "flicker", "value": "", "limit": "", "passed": "", "trials": "", "failures": "", "note": "미측정"},
]
METRICS = [
    {"id": 1, "name": "앞 기기 오선택률", "pass": True, "label": "통과", "sample": "2,100"},
    {"id": 2, "name": "보정 오차", "pass": False, "label": "✕ 미통과", "sample": "2,100"},
    {"id": 3, "name": "전환 시간", "pass": None, "label": "미측정", "sample": "—", "sampleNote": "미측정"},
    {"id": 4, "name": "흔들림", "pass": None, "label": "미측정", "sample": "—", "sampleNote": "미측정"},
]


def write_report(path: Path, generated_at: str = "2026-10-20T01:02:03.456789+00:00", rows=ROWS,
                 columns=COLUMNS) -> Path:
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write("# db_path: var/nearpilot.db\n")
        file.write("# schema_version: 6\n")
        file.write("# git_commit: 0123abc\n")
        file.write("# sample_count: 2100\n")
        file.write(f"# generated_at: {generated_at}\n")
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def rows_of(*trials_by_metric: tuple[str, int]) -> list[dict]:
    """지표 이름과 시행 수만 다른 통과 행들. 어느 리포트의 행인지는 시행 수로 알아본다."""
    return [{"metric": metric, "passed": True, "trials": trials, "note": ""} for metric, trials in trials_by_metric]


def test_FR12A_평가_리포트가_없으면_evaluation을_뺀다(tmp_path):
    assert evaluation_of(tmp_path / "missing") is None  # 폴더가 없다

    reports = tmp_path / "reports"
    reports.mkdir()
    assert evaluation_of(reports) is None  # 비었다

    (reports / "report.md").write_text("- 표본 수: 10\n", encoding="utf-8")
    assert evaluation_of(reports) is None  # CSV 만 본다


def test_FR12A_평가_리포트의_지표_행(tmp_path):
    # 실제 리포트에는 신뢰구간 상한과 출처 열이 더 있다
    columns = [*COLUMNS, "confidence_bound_95", "place_id", "model_version"]
    rows = [{**row, "confidence_bound_95": 0.0017, "place_id": "lab", "model_version": "m1"} for row in ROWS]
    write_report(tmp_path / "lab-m1-20261020.csv", rows=rows, columns=columns)

    # date 는 generated_at 에 쓰인 날짜다
    assert evaluation_of(tmp_path) == {"date": "2026-10-20", "metrics": METRICS}


def test_FR12A_평가_리포트의_표본과_비고(tmp_path):
    rows = [
        {"metric": "front_misselection_rate", "passed": "", "trials": 0, "note": "기기 라벨 시험 회차 없음"},
        {"metric": "ece", "passed": True, "trials": 12, "note": ""},
        {"metric": "new_metric", "passed": False, "trials": 1234567, "note": ""},  # 모르는 지표는 이름을 그대로 쓴다
    ]
    write_report(tmp_path / "report.csv", rows=rows, columns=["metric", "passed", "trials", "note"])

    assert evaluation_of(tmp_path)["metrics"] == [
        {"id": 1, "name": "앞 기기 오선택률", "pass": None, "label": "미측정", "sample": "0",
         "sampleNote": "기기 라벨 시험 회차 없음"},
        {"id": 2, "name": "보정 오차", "pass": True, "label": "통과", "sample": "12"},
        {"id": 3, "name": "new_metric", "pass": False, "label": "✕ 미통과", "sample": "1,234,567"},
    ]


def test_FR12A_평가는_가장_늦은_리포트를_읽는다(tmp_path):
    new_rows = [{**ROWS[0], "trials": 50}]
    old_rows = [{**ROWS[0], "trials": 10}]
    # 파일 이름 순서(a < z)와 만든 순서(최근 수정)가 생성 시각 순서와 반대다
    write_report(tmp_path / "a-new.csv", "2026-10-20T01:00:00+00:00", new_rows)
    write_report(tmp_path / "z-old.csv", "2026-10-18T01:00:00+00:00", old_rows)

    result = evaluation_of(tmp_path)

    assert result["date"] == "2026-10-20"
    assert [metric["sample"] for metric in result["metrics"]] == ["50"]


@pytest.mark.parametrize(
    ("chain", "expected"),
    [
        # 늦은 리포트는 ece 하나만 다시 쟀다: ece 는 늦은 값이 앞에 오고, 나머지는 이른 리포트의 값과 순서다
        (
            [rows_of(("ece", 3000)), ROWS],
            [(1, "보정 오차", "3,000"), (2, "앞 기기 오선택률", "2,100"), (3, "전환 시간", "—"), (4, "흔들림", "—")],
        ),
        # 한 리포트에 같은 지표 행이 여러 개면 그 리포트의 행 순서대로 모두 나온다. 이른 리포트의 같은 지표 행은 나오지 않는다
        (
            [rows_of(("ece", 100), ("front_misselection_rate", 50), ("ece", 200)), ROWS],
            [
                (1, "보정 오차", "100"),
                (2, "앞 기기 오선택률", "50"),
                (3, "보정 오차", "200"),
                (4, "전환 시간", "—"),
                (5, "흔들림", "—"),
            ],
        ),
        # 이른 리포트에 같은 지표 행이 여럿이어도 더 늦은 리포트가 고른 지표면 모두 나오지 않는다
        (
            [rows_of(("ece", 3000)), rows_of(("ece", 10), ("flicker", 20), ("ece", 30))],
            [(1, "보정 오차", "3,000"), (2, "흔들림", "20")],
        ),
        # 다음으로 늦은 리포트에도 없는 지표는 그다음 리포트에서 가져온다
        (
            [
                rows_of(("ece", 3000)),
                rows_of(("flicker", 20), ("switch_time", 30)),
                rows_of(("front_misselection_rate", 40), ("ece", 41), ("flicker", 42)),
            ],
            [(1, "보정 오차", "3,000"), (2, "흔들림", "20"), (3, "전환 시간", "30"), (4, "앞 기기 오선택률", "40")],
        ),
    ],
)
def test_FR12A_평가는_지표마다_가장_늦은_리포트를_쓴다(tmp_path, chain, expected):
    # chain 은 늦은 리포트가 앞이다. 파일 이름 순서가 생성 시각 순서와 반대다
    for number, rows in enumerate(chain):
        write_report(tmp_path / f"report-{len(chain) - number}.csv", f"2026-10-{20 - number}T01:00:00+00:00", rows)

    result = evaluation_of(tmp_path)

    assert result["date"] == "2026-10-20"  # 가장 늦은 리포트의 날짜
    assert [(metric["id"], metric["name"], metric["sample"]) for metric in result["metrics"]] == expected


def test_FR12A_평가는_생성_시각이_같으면_파일_경로_순서로_정한다(tmp_path, monkeypatch):
    stamp = "2026-10-20T01:00:00+00:00"
    write_report(tmp_path / "b.csv", stamp, rows_of(("flicker", 20), ("ece", 21)))
    write_report(tmp_path / "a.csv", stamp, rows_of(("ece", 10)))
    # 파일 시스템의 나열 순서(b → a)가 아니라 경로 순서(a < b)에서 앞서는 쪽을 더 늦은 리포트로 본다
    glob = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter(sorted(glob(self, pattern), reverse=True)))

    metrics = evaluation_of(tmp_path)["metrics"]

    # 지표마다 합치므로 b 에만 있는 지표도 나온다
    assert [(metric["name"], metric["sample"]) for metric in metrics] == [("보정 오차", "10"), ("흔들림", "20")]


@pytest.mark.parametrize(
    ("stamps", "date"),
    [
        # +09:00 의 10-20 00:30 은 UTC 로 10-19 15:30 이라 문자열로는 더 크지만 더 이르다
        (("2026-10-20T00:30:00+09:00", "2026-10-19T20:00:00+00:00"), "2026-10-19"),
        # 시간대 없는 값은 UTC 로 본다
        (("2026-10-19T20:00:00+00:00", "2026-10-21T09:00:00"), "2026-10-21"),
        (("2026-10-21T09:00:00", "2026-10-19T20:00:00+00:00"), "2026-10-21"),
    ],
)
def test_FR12A_평가는_생성_시각의_순간으로_비교한다(tmp_path, stamps, date):
    for number, stamp in enumerate(stamps):
        write_report(tmp_path / f"report-{number}.csv", stamp)

    assert evaluation_of(tmp_path)["date"] == date


def test_FR12A_읽을_수_없는_리포트는_건너뛴다(tmp_path, caplog):
    good = write_report(tmp_path / "good.csv", "2026-10-19T01:00:00+00:00")
    later = "2026-10-21T01:00:00+00:00"  # 읽을 수 있었다면 이 파일들이 이긴다
    broken = {
        "no-head.csv": "metric,passed,trials,note\nece,True,10,\n",
        "no-columns.csv": f"# generated_at: {later}\nname,value\nece,1\n",
        "bad-stamp.csv": "# generated_at: yesterday\nmetric,passed,trials,note\nece,True,10,\n",
        "bad-passed.csv": f"# generated_at: {later}\nmetric,passed,trials,note\nece,maybe,10,\n",
        "bad-trials.csv": f"# generated_at: {later}\nmetric,passed,trials,note\nece,True,many,\n",
        "no-rows.csv": f"# generated_at: {later}\nmetric,passed,trials,note\n",
        "empty.csv": "",
    }
    for name, text in broken.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "not-utf8.csv").write_bytes(b"# generated_at: " + later.encode() + b"\n\xff\xfe\n")
    (tmp_path / "folder.csv").mkdir()

    with caplog.at_level(logging.WARNING, logger="nearpilot.core.dashboard.evaluation"):
        result = evaluation_of(tmp_path)

    assert result == {"date": "2026-10-19", "metrics": METRICS}
    assert len(caplog.records) == len(broken) + 2  # 파일마다 한 줄이다
    assert all(record.levelno == logging.WARNING for record in caplog.records)
    assert str(good) not in caplog.text

    good.unlink()
    assert evaluation_of(tmp_path) is None  # 읽을 수 있는 것이 없으면 뺀다
