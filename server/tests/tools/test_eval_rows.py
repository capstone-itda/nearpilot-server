"""NFR-13 서버 지표 평가의 보고서 행 도우미 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

`metric_row`의 열 순서는 근접 지표의 행(`proximity`)과 같아야 여러 지표의 행이 한 표에 섞여도 열이 어긋나지 않는다.
행은 `report.write_report`가 그대로 받는다.
"""

from __future__ import annotations

import csv
import dataclasses
from datetime import datetime, timezone

import pytest

from tools.evaluation.proximity import _row
from tools.evaluation.report import write_report
from tools.evaluation.rows import MetricOutcome, metric_row

COLUMNS = ["metric", "value", "limit", "passed", "trials", "failures", "note"]
GENERATED_AT = datetime(2026, 10, 27, tzinfo=timezone.utc)


def test_NFR13_metric_row_keeps_column_order():
    full = metric_row("m", value=0.5, limit=1.0, passed=True, trials=20, failures=1, note="메모")
    empty = metric_row("m")

    assert list(full) == COLUMNS
    assert list(empty) == COLUMNS
    assert full == {"metric": "m", "value": 0.5, "limit": 1.0, "passed": True,
                    "trials": 20, "failures": 1, "note": "메모"}
    assert [empty[column] for column in COLUMNS[1:]] == [""] * 6  # 안 준 칸은 빈 문자열이다
    assert list(_row("m")) == list(empty)  # 근접 지표의 행과 같은 열이다
    with pytest.raises(TypeError):  # 값 칸은 이름으로만 준다
        metric_row("m", 0.5)


def test_NFR13_metric_rows_feed_write_report(tmp_path):
    rows = [
        metric_row("zero", trials=30, failures=0),
        metric_row("some", trials=30, failures=2),
        metric_row("unmeasured", note="미측정"),
    ]
    csv_path = tmp_path / "report.csv"

    write_report(rows, db_path="eval.db", schema_version=5, sample_count=0, generated_at=GENERATED_AT,
                 csv_path=str(csv_path), md_path=str(tmp_path / "report.md"), git_commit="abc123")

    lines = [line for line in csv_path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    table = {row["metric"]: row for row in csv.DictReader(lines)}
    assert list(table) == ["zero", "some", "unmeasured"]
    assert list(table["zero"])[:7] == COLUMNS  # 신뢰구간 상한 열은 그 뒤에 붙는다
    assert float(table["zero"]["confidence_bound_95"]) == pytest.approx(0.1157, abs=0.001)
    assert table["some"]["confidence_bound_95"] == ""  # 실패가 있으면 상한을 내지 않는다
    assert table["unmeasured"]["confidence_bound_95"] == ""  # trials가 빈 칸이면 계산하지 않는다
    assert table["unmeasured"]["note"] == "미측정"


def test_NFR13_metric_outcome_holds_rows_and_raw():
    rows = [metric_row("m")]
    outcome = MetricOutcome(rows=rows, raw={"latencies": [0.1]})

    assert outcome.rows is rows
    assert outcome.raw == {"latencies": [0.1]}
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.rows = []
