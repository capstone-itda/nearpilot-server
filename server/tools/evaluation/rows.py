"""서버 지표 평가의 보고서 행 도우미 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

지표 함수는 `MetricOutcome`을 돌려준다. 행은 `report.write_report`에 넘기고, 원자료는 JSON으로 쓴다.
`metric_row`의 키 순서는 `proximity.metric_rows`의 행과 같아서 여러 지표의 행이 한 표에 섞여도 열이 어긋나지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MetricOutcome:
    """지표 하나를 잰 결과."""

    rows: list[dict[str, Any]]  # `metric_row`로 만든 보고서 행
    raw: dict[str, Any]  # JSON으로 쓸 원자료 (응답 목록, 지연 목록, 겹침 목록 등)


def metric_row(
    metric: str, *, value: float | str = "", limit: float | str = "", passed: bool | str = "",
    trials: int | str = "", failures: int | str = "", note: str = "",
) -> dict[str, Any]:
    """보고서 행 하나. 안 준 칸은 빈 문자열이고, 키 순서는 항상 같다."""
    return {
        "metric": metric, "value": value, "limit": limit, "passed": passed,
        "trials": trials, "failures": failures, "note": note,
    }
