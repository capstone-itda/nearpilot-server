"""시험 회차로 공통 근접 모델의 정면 오선택률과 ECE를 계산한다 (specs/core-ops-cli/spec.md 6절 "eval", 지표 8의 일부,
specs/core-proximity-common-model/spec.md 6절 결정 4, 11, 13).

`collect_samples`는 시험 회차를 장소 하나의 회차로 보고 앵커와 자동 영점을 정한 뒤 (라벨은 쓰지 않는다),
시험 회차의 평가 시각마다 모델의 사후확률을 읽어 표본으로 만든다. `compute_metrics`는 표본에서 오선택률과 ECE를 센다.
`metric_rows`는 그 값을 `report.write_report`가 받는 지표 행으로 바꾼다.
평가 시각과 후보를 만드는 법은 `core/proximity/common_fit.py`의 `validation_log_loss`와 같다. DB와 파일을 열지 않고,
현재 시각과 무작위를 쓰지 않아서 같은 입력에는 같은 값이 나온다.
이동 뒤 대상 전환 시간과 정지 시 깜빡임 감소는 정의가 미결이라 계산하지 않고 보고서에 "미측정"으로 쓴다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from nearpilot.core.proximity.common_fit import (
    NONE_LABEL, LabeledRun, PlaceRuns, place_anchors, place_frames, place_zeros,
)
from nearpilot.core.proximity.common_model import CommonModel
from nearpilot.core.proximity.files import AcceptanceSettings
from nearpilot.core.proximity.posterior import compute_posteriors
from nearpilot.shared.enums import DeviceState, Occupancy, TrustState
from nearpilot.shared.models import NodeInfo

AUTO_ZERO_CALIB = "auto-zero"  # 평가 결과의 보정 버전. 평가는 시험 회차에서 찾은 자동 영점을 쓴다
_EVAL_PLACE = "eval"  # 시험 회차를 한 장소로 볼 때 붙이는 이름. 공통 모델에는 장소가 없어서 어디에도 쓰이지 않는다


class EvaluationError(ValueError):
    """평가 입력이 잘못됐거나, 입력으로 지표를 계산할 수 없다."""


@dataclass(frozen=True)
class Sample:
    """평가 시각 하나. 모델이 그 시각에 상태마다 준 확률과 정답 라벨이다."""

    run_id: str
    label: str  # node_id 또는 NONE_LABEL
    probabilities: Mapping[str, float]  # node_id → 사후확률, NONE_LABEL → 1 − 합 (0 미만이면 0)


@dataclass(frozen=True)
class Metrics:
    """시험 표본의 지표."""

    misselection_rate: float | None  # 기기 라벨 표본이 없으면 None
    misselection_trials: int  # 기기 라벨 표본 수
    misselection_failures: int  # 그중 가장 큰 기기가 라벨과 다른 표본 수
    ece: float
    ece_samples: int  # 모든 표본 수


def collect_samples(model: CommonModel, runs: Sequence[LabeledRun], *, bin_sec: float) -> list[Sample]:
    """시험 회차의 평가 시각마다 모델의 사후확률을 읽어 표본을 만든다. 순서는 회차 순서, 시각 순서다.

    회차 전부를 장소 하나의 `PlaceRuns`로 본다. 앵커는 `place_anchors`이고, 자동 영점은 시험 회차 전부의 프레임에서
    라벨 없이 `model.auto_zero_min_bins`로 찾는다 (`place_zeros`). 찾지 못한 앵커는 보정하지 않는다 (`unzeroed`).
    회차마다 평가 시각은 `started_at + window_sec`부터 `bin_sec` 간격으로 `ended_at` 이하까지다.
    시각마다 앵커 전부를 후보와 근접 참여 기기로 `compute_posteriors`에 넘기고, 보정 버전은 `AUTO_ZERO_CALIB`다.
    `NONE_LABEL`의 확률은 1 − (기기 확률의 합)이고 0보다 작으면 0이다.
    `bin_sec`가 잘못됐거나 평가 시각이 없는 회차가 있으면 `EvaluationError`다.
    """
    if not math.isfinite(bin_sec) or bin_sec <= 0:
        raise EvaluationError(f"bin_sec: 0보다 큰 유한한 수여야 한다 ({bin_sec!r})")
    step = timedelta(seconds=bin_sec)
    if step <= timedelta(0):
        raise EvaluationError(f"bin_sec: 1 마이크로초 단위로 0보다 커야 한다 ({bin_sec!r})")

    group = PlaceRuns(_EVAL_PLACE, runs)
    anchors = place_anchors(group)
    zeros = place_zeros([group], bin_sec=bin_sec, min_bins=model.auto_zero_min_bins)
    # 근접 추정은 node_id, place_id, trust_state만 읽는다. 나머지 필드는 자리 채움 값이다 (`validation_log_loss`와 같다).
    nodes = [
        NodeInfo(
            node_id=node_id,
            place_id=_EVAL_PLACE,
            kind="light",
            occupancy=Occupancy.SHARED,
            trust_state=TrustState.TRUSTED,
            device_state=DeviceState.AVAILABLE,
            exclusive_group=None,
            last_seen_at=None,
        )
        for node_id in sorted(anchors)
    ]
    window = timedelta(seconds=model.window_sec)

    samples: list[Sample] = []
    for run in runs:
        frames = place_frames(run, anchors)
        # k번째 시각은 매번 첫 시각에서 정수 마이크로초로 센다. float 초를 더해 가면 경계의 시각이 미끄러진다.
        first = run.started_at + window
        times = [first + k * step for k in range((run.ended_at - first) // step + 1)]
        if not times:
            raise EvaluationError(
                f"회차 {run.run_id!r}: 구간이 window_sec({model.window_sec:g}초)보다 짧아 평가 시각이 없다"
            )
        for at in times:
            posteriors = compute_posteriors(
                model, frames, run.beacon, nodes, nodes, at,
                bin_sec=bin_sec, participants=anchors, zeros=zeros, calib_version=AUTO_ZERO_CALIB,
            ).posteriors
            none_probability = max(0.0, 1.0 - math.fsum(posteriors.values()))
            samples.append(Sample(run.run_id, run.label, {**posteriors, NONE_LABEL: none_probability}))
    return samples


def compute_metrics(samples: Sequence[Sample], *, bins: int = 10) -> Metrics:
    """표본에서 정면 오선택률과 ECE를 센다.

    오선택률은 라벨이 기기인 표본만 본다. 기기 확률 중 가장 큰 기기(같으면 node_id 사전순 앞)가 라벨과 다르면 실패이고,
    `실패 / 표본`이다. 기기 라벨 표본이 없으면 `None`이다.
    ECE는 모든 표본으로 센다. 전체 상태(기기들과 `NONE_LABEL`) 중 확률이 가장 큰 상태(같으면 사전순 앞)가 예측이고 그 확률이 신뢰도다.
    신뢰도를 `bins`개의 같은 폭 구간으로 나눠(1.0은 마지막 구간) `Σ (구간 표본 수 / 전체) × |구간 정확도 − 구간 평균 신뢰도|`를 낸다.
    표본이 없거나, 표본의 라벨이 확률을 가진 상태가 아니거나, `bins`가 1보다 작으면 `EvaluationError`다.
    """
    if bins < 1:
        raise EvaluationError(f"bins: 1 이상이어야 한다 ({bins!r})")
    if not samples:
        raise EvaluationError("표본이 없어 ECE를 계산할 수 없다")

    trials = 0
    failures = 0
    confidences: list[list[float]] = [[] for _ in range(bins)]
    corrects: list[list[float]] = [[] for _ in range(bins)]
    for sample in samples:
        probabilities = sample.probabilities
        if sample.label not in probabilities:
            raise EvaluationError(f"회차 {sample.run_id!r}: 라벨 {sample.label!r}의 확률이 표본에 없다")
        if sample.label != NONE_LABEL:
            trials += 1
            devices = [state for state in probabilities if state != NONE_LABEL]
            if _most_probable(probabilities, devices) != sample.label:
                failures += 1

        predicted = _most_probable(probabilities, probabilities)
        confidence = probabilities[predicted]
        index = min(int(confidence * bins), bins - 1)  # 신뢰도 1.0은 마지막 구간에 넣는다
        confidences[index].append(confidence)
        corrects[index].append(1.0 if predicted == sample.label else 0.0)

    total = len(samples)
    ece = math.fsum(
        len(confidence) / total * abs(math.fsum(correct) / len(correct) - math.fsum(confidence) / len(confidence))
        for confidence, correct in zip(confidences, corrects)
        if confidence
    )
    return Metrics(
        misselection_rate=None if trials == 0 else failures / trials,
        misselection_trials=trials,
        misselection_failures=failures,
        ece=ece,
        ece_samples=total,
    )


def _most_probable(probabilities: Mapping[str, float], states: Iterable[str]) -> str:
    """`states` 중 확률이 가장 큰 상태. 같으면 사전순으로 앞서는 상태다."""
    return min(states, key=lambda state: (-probabilities[state], state))


def metric_rows(metrics: Metrics, acceptance: AcceptanceSettings) -> list[dict[str, Any]]:
    """`report.write_report`에 넘길 지표 행 네 개. 키 순서는 `metric, value, limit, passed, trials, failures, note`다.

    측정하지 않는 항목(`switch_time`, `flicker`)은 값을 비우고 `note`에 "미측정"이라고 쓴다.
    기기 라벨 표본이 없으면 오선택률 행의 값과 통과 여부를 비운다. 이때 `failures`도 비운다 —
    `write_report`는 `failures == 0`인 행에 `trials`로 신뢰구간 상한을 내는데, `trials`가 0이면 계산할 수 없다.
    """
    rate = metrics.misselection_rate
    if rate is None:
        misselection = _row(
            "front_misselection_rate", limit=acceptance.max_misselection, trials=metrics.misselection_trials,
            note="기기 라벨 시험 회차 없음",
        )
    else:
        misselection = _row(
            "front_misselection_rate", value=rate, limit=acceptance.max_misselection,
            passed=rate <= acceptance.max_misselection, trials=metrics.misselection_trials,
            failures=metrics.misselection_failures,
        )
    return [
        misselection,
        _row(
            "ece", value=metrics.ece, limit=acceptance.max_ece, passed=metrics.ece <= acceptance.max_ece,
            trials=metrics.ece_samples,
        ),
        _row("switch_time", note="미측정"),
        _row("flicker", note="미측정"),
    ]


def _row(
    metric: str, *, value: float | str = "", limit: float | str = "", passed: bool | str = "",
    trials: int | str = "", failures: int | str = "", note: str = "",
) -> dict[str, Any]:
    """보고서 행 하나. 안 준 칸은 빈 문자열이고, 키 순서는 항상 같다."""
    return {
        "metric": metric, "value": value, "limit": limit, "passed": passed,
        "trials": trials, "failures": failures, "note": note,
    }
