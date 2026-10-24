"""FR-12A 평가 리포트 읽기 (specs/core-dashboard-queries/spec.md 6절).

`tools/evaluation/report.py` 가 쓴 CSV 가운데 `generated_at` 이 가장 늦은 것 하나를 대시보드 신뢰도의
`evaluation` 모양으로 바꾼다. `core` 는 `tools/` 를 import 하지 않아서 CSV 형식(머리 줄 `# 키: 값`, 그 뒤
`csv.DictReader` 로 읽는 표)을 여기서 직접 읽는다. 읽을 수 없는 파일은 건너뛰고 로그만 남긴다.
DB 를 읽지 않고 현재 시각을 읽지 않는다.
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
    """`reports_dir/*.csv` 가운데 `generated_at` 이 가장 늦은 리포트 하나를 `{"date", "metrics"}` 로 바꾼다.

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
    generated_at, metrics = max(reports, key=lambda report: report[0])
    return {"date": generated_at.date().isoformat(), "metrics": metrics}


def _read(path: Path) -> tuple[datetime, list[dict[str, Any]]]:
    """리포트 하나의 생성 시각과 지표 행. 머리 줄이나 열이 맞지 않으면 `ValueError`."""
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
    metrics = [_metric(number, row) for number, row in enumerate(reader, start=1)]
    if not metrics:  # 쓰는 중인 파일
        raise ValueError("지표 행이 없다")
    return generated_at, metrics


def _metric(number: int, row: dict[str, str | None]) -> dict[str, Any]:
    passed = _PASSED[row["passed"] or ""]
    trials = row["trials"]
    metric: dict[str, Any] = {
        "id": number,
        "name": metric_name(row["metric"]),
        "pass": passed,
        "label": metric_label(passed),
        "sample": f"{int(trials):,}" if trials else NO_SAMPLE,
    }
    if row["note"]:
        metric["sampleNote"] = row["note"]
    return metric
