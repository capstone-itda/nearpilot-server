"""FR-12A 평가 리포트 읽기 (specs/core-dashboard-queries/spec.md 6절).

`tools/evaluation/report.py` 가 쓴 CSV 를 지표(`metric` 열의 값)마다 그 지표가 있는 리포트 가운데 `generated_at` 이
가장 늦은 것의 행으로 합쳐 대시보드 신뢰도의 `evaluation` 모양으로 바꾼다. 일부 지표만 잰 리포트가 가장 늦어도 다른
지표가 사라지지 않는다. `core` 는 `tools/` 를 import 하지 않아서 CSV 형식(머리 줄 `# 키: 값`, 그 뒤 `csv.DictReader` 로
읽는 표)을 여기서 직접 읽는다. 읽을 수 없는 파일은 건너뛰고 로그만 남긴다. DB 를 읽지 않고 현재 시각을 읽지 않는다.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nearpilot.core.dashboard.text import NO_SAMPLE, metric_label, metric_name

log = logging.getLogger(__name__)

_GENERATED_AT = "# generated_at:"
# 리포트가 꼭 가져야 하는 열
_COLUMNS = {"metric", "passed", "trials", "note"}
# `passed` 칸 → 통과 여부. 비면 측정하지 못한 것이다.
_PASSED = {"True": True, "False": False, "": None}


def evaluation_of(reports_dir: Path) -> dict[str, Any] | None:
    """`reports_dir/*.csv` 를 지표마다 그 지표가 있는 가장 늦은 리포트의 행으로 합쳐 `{"date", "metrics"}` 로 바꾼다.

    리포트를 `generated_at` 늦은 순(같은 시각이면 파일 경로 순서)으로 놓고, 늦은 리포트부터 아직 고르지 않은 지표의 행을
    그 리포트의 행 순서대로 붙인다. 한 리포트에 같은 지표 행이 여러 개면 모두 붙이고, 더 늦은 리포트에서 고른 지표는
    건너뛴다. `id` 는 붙인 순서의 1부터의 순번이고 `date` 는 가장 늦은 리포트의 `generated_at` 날짜다.
    폴더가 없거나 읽을 수 있는 CSV 가 없으면 `None`. 읽을 수 없는 파일은 건너뛰고 로그를 남긴다 (예외를 올리지 않는다).
    """
    reports = []
    for path in sorted(reports_dir.glob("*.csv")):
        try:
            reports.append(_read(path))
        except (OSError, ValueError, KeyError, csv.Error) as error:
            log.warning("평가 리포트를 읽을 수 없다: %s (%s)", path, error)
    if not reports:
        return None
    reports.sort(key=lambda report: report[0], reverse=True)  # 정렬은 안정적이라 같은 시각이면 파일 경로 순서가 남는다
    chosen: set[str | None] = set()  # 더 늦은 리포트에서 이미 고른 지표 (`metric` 열의 값)
    rows: list[dict[str, Any]] = []
    for _, report_rows in reports:
        rows.extend(row for key, row in report_rows if key not in chosen)
        # 같은 리포트의 같은 지표 행은 모두 쓰므로, 리포트 하나를 다 붙인 뒤에 그 지표들을 고른 것으로 친다
        chosen.update(key for key, _ in report_rows)
    return {
        "date": reports[0][0].date().isoformat(),  # 가장 늦은 리포트는 행을 모두 내므로 날짜는 그 리포트의 것이다
        "metrics": [{"id": number, **row} for number, row in enumerate(rows, start=1)],
    }


def _read(path: Path) -> tuple[datetime, list[tuple[str | None, dict[str, Any]]]]:
    """리포트 하나의 생성 시각과 행 순서대로 (`metric` 열의 값, 지표 행) 쌍들. 머리 줄이나 열이 맞지 않으면 `ValueError`."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    head = 0
    while head < len(lines) and lines[head].startswith("# "):
        head += 1
    stamp = next((line.removeprefix(_GENERATED_AT) for line in lines[:head] if line.startswith(_GENERATED_AT)), None)
    if stamp is None:
        raise ValueError("generated_at 머리 줄이 없다")
    generated_at = datetime.fromisoformat(stamp.strip())
    if generated_at.tzinfo is None:  # 순서만 비교하므로 시간대 없는 값은 UTC 로 본다
        generated_at = generated_at.replace(tzinfo=timezone.utc)

    reader = csv.DictReader(lines[head:])
    if not _COLUMNS <= set(reader.fieldnames or ()):
        raise ValueError(f"{', '.join(sorted(_COLUMNS))} 열이 있어야 한다")
    rows = [(row["metric"], _metric(row)) for row in reader]
    if not rows:  # 쓰는 중인 파일
        raise ValueError("지표 행이 없다")
    return generated_at, rows


def _metric(row: dict[str, str | None]) -> dict[str, Any]:
    passed = _PASSED[row["passed"] or ""]
    trials = row["trials"]
    metric: dict[str, Any] = {
        "name": metric_name(row["metric"]),
        "pass": passed,
        "label": metric_label(passed),
        "sample": f"{int(trials):,}" if trials else NO_SAMPLE,
    }
    if row["note"]:
        metric["sampleNote"] = row["note"]
    return metric
