"""시험 회차의 근접 확률로 비용 비율별 재질문률·오작동률 곡선을 만든다 (specs/db-evaluation-server-metrics/spec.md 6절, 지표 2, NFR-06).

`reask_curve`는 `proximity.collect_samples`가 만든 표본으로 서버의 재질문 판정을 비용 비율 r마다 되풀이한다.
서버는 후보 기기의 사후확률이 임계값 미만이면 기기를 구동하지 않고 재질문한다 (FR-17). 임계값은 r마다 `r / (1 + r)`이다.
표본마다 기기 확률이 가장 큰 기기(같으면 node_id 사전순 앞)를 서버가 고를 기기로 보고, 그 확률이 임계값 미만이면 재질문,
이상이면 구동한다. `none`의 확률은 후보의 확률이 아니라서 고르는 데 쓰지 않는다. 구동한 기기가 라벨과 다르면 오작동이다.
라벨이 `none`인 표본(어느 기기 앞도 아님)은 어느 기기를 구동해도 오작동이다. 두 비율의 분모는 모두 전체 표본 수다.
`reask_rows`는 곡선을 `report.write_report`가 받는 지표 행으로 바꾼다.
DB와 파일을 열지 않고, 현재 시각과 무작위를 쓰지 않아서 같은 입력에는 같은 값이 나온다.
"같은 재질문률에서 오작동 감소"의 비교 기준이 미결이라 (requirements.md §4) 통과 여부는 판정하지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nearpilot.core.proximity.fit import NONE_LABEL
from tools.evaluation.proximity import EvaluationError, Sample, _most_probable
from tools.evaluation.rows import metric_row

_NO_LABELS = "기기 라벨 시험 회차 없음"
_UNJUDGED = "통과 여부 미판정 (같은 재질문률 비교 기준 미결)"


@dataclass(frozen=True)
class CurvePoint:
    """비용 비율 하나에서의 재질문률과 오작동률. 두 비율의 분모는 모두 `reask_curve`에 넘긴 표본 수다."""

    cost_ratio: float
    threshold: float  # r / (1 + r)
    reask_rate: float  # 기기 확률의 최댓값이 threshold 미만이라 재질문한 표본의 비율
    misoperation_rate: float  # 최댓값이 threshold 이상이라 구동했는데 고른 기기가 라벨과 다른 표본의 비율


def reask_curve(samples: Sequence[Sample], cost_ratios: Sequence[float]) -> list[CurvePoint]:
    """비용 비율마다 재질문률과 오작동률을 센다. 점은 `cost_ratios`의 순서대로다.

    모든 표본(라벨이 기기인 것과 `none`인 것)이 분모다. 기기 라벨 표본이 하나도 없으면 곡선을 만들 수 없어 빈 목록이다.
    표본이 없는 경우도 같다. 확률이 임계값과 같은 표본은 구동한다.
    `cost_ratios`가 비었거나 0보다 큰 유한한 수가 아닌 비율이 있으면 `EvaluationError`다.
    표본의 라벨이 확률을 가진 상태가 아니거나 기기의 확률이 없을 때도 `EvaluationError`다 (`compute_metrics`와 같다).
    """
    if not cost_ratios:
        raise EvaluationError("cost_ratios: 비용 비율이 하나 이상 있어야 한다")
    for ratio in cost_ratios:
        if not math.isfinite(ratio) or ratio <= 0:
            raise EvaluationError(f"cost_ratios: 0보다 큰 유한한 수여야 한다 ({ratio!r})")
    if all(sample.label == NONE_LABEL for sample in samples):
        return []

    chosen = [(*_chosen_device(sample), sample.label) for sample in samples]  # (고른 기기, 그 확률, 라벨)
    points = []
    for ratio in cost_ratios:
        threshold = ratio / (1 + ratio)
        reasked = sum(1 for _, probability, _ in chosen if probability < threshold)
        wrong = sum(1 for device, probability, label in chosen if probability >= threshold and device != label)
        points.append(CurvePoint(float(ratio), threshold, reasked / len(chosen), wrong / len(chosen)))
    return points


def reask_rows(points: Sequence[CurvePoint], trials: int) -> list[dict[str, Any]]:
    """`report.write_report`에 넘길 지표 행. `m2_reask_curve` 행 하나, 그 뒤에 비용 비율마다 `m2_reask_r{r}` 행이다.

    `trials`는 `reask_curve`에 넘긴 표본 수다. 비율 행의 값은 오작동률이고 재질문률은 `note`에 쓴다.
    `failures`는 오작동 표본 수(`오작동률 × trials`)라서 0건이면 `write_report`가 95% 신뢰구간 상한을 덧붙인다.
    통과 기준인 "같은 재질문률에서 오작동 감소"는 비교 기준이 미결이라 (requirements.md §4) `limit`과 `passed`를 비우고 note에 쓴다.
    `points`가 비었으면 (기기 라벨 시험 회차가 없다) 칸을 모두 비우고 사유만 쓴 `m2_reask_curve` 행 하나다.
    """
    if not points:
        return [metric_row("m2_reask_curve", note=_NO_LABELS)]
    rows = [
        metric_row(
            "m2_reask_curve", trials=trials,
            note=f"비용 비율 {len(points)}개의 곡선. 비율별 값은 m2_reask_r* 행. {_UNJUDGED}",
        )
    ]
    for point in points:
        rows.append(
            metric_row(
                f"m2_reask_r{point.cost_ratio:g}", value=point.misoperation_rate, trials=trials,
                failures=round(point.misoperation_rate * trials),
                note=f"재질문률 {point.reask_rate:.4f}, 확률 임계값 {point.threshold:.4f}. {_UNJUDGED}",
            )
        )
    return rows


def _chosen_device(sample: Sample) -> tuple[str, float]:
    """서버가 고를 기기와 그 확률. 기기 확률이 가장 큰 기기이고, 같으면 node_id 사전순으로 앞서는 기기다."""
    probabilities = sample.probabilities
    if sample.label not in probabilities:
        raise EvaluationError(f"회차 {sample.run_id!r}: 라벨 {sample.label!r}의 확률이 표본에 없다")
    devices = [state for state in probabilities if state != NONE_LABEL]
    if not devices:
        raise EvaluationError(f"회차 {sample.run_id!r}: 기기의 확률이 표본에 없다")
    device = _most_probable(probabilities, devices)
    return device, probabilities[device]
