"""거리 순서 평가(지표 10)의 순수 계산 (specs/db-evaluation-distance-order/spec.md 6절, FR-09, FR-16, NFR-13).

`order_points`는 회차의 프레임으로 평가 시점마다 거리 순서를 세운다. 서버의 `distance_order`와 같은 규칙이라서
창은 서버 수신 시각(`received_at`) 기준 `[t − window, t]`이고, 정렬 값은 그 창 안 RSSI의 평균이다.
`scored_pairs`는 시점의 순서에서 기기 쌍을 골라 좌표표(`Layout`)의 정답 순서와 비교해 채점한다.
`estimate_frame_sigma`와 `estimate_static_sigma`는 학습 회차로 경계 확률 설정 σf, σs를 추정하고,
`boundary_table`과 `choose_threshold`는 검증 회차로 기준 확률을 고른다. `order_rows`는 결과를 `report.write_report`가 받는 행으로 바꾼다.
경계 확률은 판정의 `boundary_probability`를 그대로 부른다. 식을 다시 쓰지 않는다.
DB와 파일을 열지 않고, 현재 시각과 무작위를 쓰지 않아서 같은 입력에는 같은 값이 나온다.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any

from nearpilot.core.decision.pipeline import boundary_probability
from nearpilot.core.proximity.common_fit import LabeledRun, place_frames
from tools.evaluation.layout import Layout
from tools.evaluation.proximity import EvaluationError
from tools.evaluation.rows import metric_row

MIN_GAP_M = 1.0  # 지표 10은 거리 차이가 이 값(m) 이상인 쌍만 센다 (requirements.md §4)
# σs 탐색 격자: 0.00 dB부터 15.00 dB까지 0.05 dB 간격 (spec 결정 12). 탐색 범위는 측정 방법의 일부라 설정으로 두지 않는다.
STATIC_SIGMA_GRID: tuple[float, ...] = tuple(round(step * 0.05, 2) for step in range(301))
# 기준 확률 후보: 0.50부터 0.99까지 0.01 간격 (spec 결정 4)
CANDIDATES: tuple[float, ...] = tuple(round(percent / 100, 2) for percent in range(50, 100))

_PROB_FLOOR = 1e-12  # 가능도에서 log(0)을 피하려고 확률을 [1e-12, 1 − 1e-12]로 자른다
_NOT_CHOSEN = "검증 회차에서 목표 오류 비율 이하인 후보 없음"
_NOT_TESTED = "기준 확률을 고르지 못해 시험하지 않음"


@dataclass(frozen=True)
class OrderPoint:
    """평가 시점 하나의 거리 순서."""

    run_id: str
    spot: str
    at: datetime
    order: tuple[str, ...]  # 가까운 순. 못 본 기기는 끝, 같은 값은 기기 ID 순
    values: Mapping[str, float | None]  # 정렬 값(평균 dBm). 못 본 기기는 None
    counts: Mapping[str, int]  # 정렬 값에 쓴 프레임 수. 못 본 기기는 0


@dataclass(frozen=True)
class ScoredPair:
    """채점한 기기 쌍. earlier가 관측 순서에서 앞선다."""

    earlier: str
    later: str
    earlier_value: float
    later_value: float | None  # None이면 later만 못 봤다
    earlier_count: int
    later_count: int
    correct: bool  # earlier가 실제로 더 가깝다


@dataclass(frozen=True)
class StaticSigmaFit:
    """σs 추정 결과."""

    sigma_db: float
    pairs: int  # 추정에 쓴 쌍 수
    at_edge: bool  # 격자의 첫 값이나 끝 값이 골라졌다


@dataclass(frozen=True)
class BoundaryStat:
    """기준 확률 후보 하나로 센 경계 통계."""

    min_prob: float
    boundaries: int  # 채점한 경계 수
    passed: int  # 경계 확률 ≥ min_prob
    wrong: int  # passed 중 correct가 아닌 것
    pass_rate: float | None  # boundaries가 0이면 None
    error_rate: float | None  # passed가 0이면 None


# ── 시점별 거리 순서 ────────────────────────────────────────────────────


def order_points(
    run: LabeledRun, spot: str, anchors: Collection[str], *, window_sec: float, step_sec: float,
) -> list[OrderPoint]:
    """회차의 평가 시점마다 서버처럼 세운 거리 순서. 시간 순서다.

    프레임은 `place_frames(run, anchors)`다 (비콘, 앵커, 유한한 RSSI, `frame_ts`의 회차 구간, 중복을 거른다).
    시점은 `started_at + window_sec`부터 `step_sec`마다 `ended_at` 이하까지다. 시점 `t`의 창은 서버 수신 시각 기준
    `t − window_sec <= received_at <= t`이고, 창 안 프레임의 기기별 RSSI 평균이 정렬 값이다.
    정렬은 값이 큰 기기부터, 같으면 기기 ID 순서다. 창에 프레임이 없는 앵커는 값 None, 프레임 수 0으로 끝에 기기 ID 순서로 둔다.
    모든 앵커가 못 본 시점도 돌려준다 (`scored_pairs`가 쌍을 고를 때 빠진다). 시점이 없으면 빈 목록이다.
    `window_sec`나 `step_sec`가 0보다 큰 유한한 수가 아니면 `EvaluationError`다.
    """
    window = _duration(window_sec, "window_sec")
    step = _duration(step_sec, "step_sec")
    frames = sorted(place_frames(run, anchors), key=lambda frame: frame.received_at)
    received = [frame.received_at for frame in frames]
    everyone = set(anchors)

    first = run.started_at + window
    points = []
    # k번째 시점은 매번 첫 시점에서 정수 마이크로초로 센다. float 초를 더해 가면 경계의 시각이 미끄러진다.
    for k in range((run.ended_at - first) // step + 1):
        at = first + k * step
        rssis: dict[str, list[float]] = {}
        for frame in frames[bisect_left(received, at - window):bisect_right(received, at)]:
            rssis.setdefault(frame.node_id, []).append(frame.rssi)
        means = {node_id: math.fsum(values) / len(values) for node_id, values in rssis.items()}
        heard = sorted(means, key=lambda node_id: (-means[node_id], node_id))
        order = (*heard, *sorted(everyone - means.keys()))
        points.append(
            OrderPoint(
                run.run_id, spot, at, order,
                values={node_id: means.get(node_id) for node_id in order},
                counts={node_id: len(rssis.get(node_id, ())) for node_id in order},
            )
        )
    return points


def scored_pairs(point: OrderPoint, layout: Layout, *, adjacent_only: bool) -> list[ScoredPair]:
    """시점의 순서에서 고른 기기 쌍을 좌표표의 정답 순서로 채점한다. 쌍은 순서에서 앞선 기기부터 센다.

    `adjacent_only`면 순서에서 이웃한 쌍만, 아니면 모든 쌍(i < j)을 본다. `point`는 `order_points`가 만든 순서여야 한다 (못 본 기기가 끝).
    두 기기 모두 못 본 쌍은 순서가 없어서 뺀다. 서는 위치에서 두 기기까지 거리의 차이가 `MIN_GAP_M`보다 작은 쌍도 뺀다.
    한 기기만 못 본 쌍은 센다 (못 본 기기가 뒤다). `correct`는 앞선 기기가 서는 위치에 실제로 더 가깝다는 뜻이다.
    좌표표에 없는 spot이나 기기는 `KeyError`다 (부르기 전에 `check_layout_covers`로 거른다).
    """
    order = point.order
    candidates = zip(order, order[1:]) if adjacent_only else combinations(order, 2)
    pairs = []
    for earlier, later in candidates:
        earlier_value, later_value = point.values[earlier], point.values[later]
        if earlier_value is None and later_value is None:
            continue
        earlier_distance = layout.distance(point.spot, earlier)
        later_distance = layout.distance(point.spot, later)
        if abs(earlier_distance - later_distance) < MIN_GAP_M:
            continue
        pairs.append(
            ScoredPair(
                earlier, later, earlier_value, later_value, point.counts[earlier], point.counts[later],
                earlier_distance < later_distance,
            )
        )
    return pairs


# ── σ 추정 ──────────────────────────────────────────────────────────────


def estimate_frame_sigma(runs: Sequence[LabeledRun], anchors: Collection[str], *, window_sec: float) -> float:
    """프레임 하나의 RSSI 표준편차 σf(dB). 판정이 쓰는 `order_frame_sigma_db`와 같은 뜻이다.

    회차마다 `place_frames`의 프레임을 `started_at`부터 `window_sec` 길이의 겹치지 않는 구간으로 서버 수신 시각 기준으로 나눈다.
    구간·기기마다 프레임이 2개 이상이면 구간 평균에서 벗어난 제곱합과 (개수 − 1)을 더하고,
    `σf = sqrt(제곱합의 합 / 자유도의 합)`이다. 자유도의 합이 0이면 (그런 구간·기기가 없다) `EvaluationError`다.
    `window_sec`가 0보다 큰 유한한 수가 아니어도 `EvaluationError`다.
    """
    window = _duration(window_sec, "window_sec")
    squares: list[float] = []
    degrees = 0
    for run in runs:
        groups: dict[tuple[int, str], list[float]] = {}
        for frame in place_frames(run, anchors):
            slot = (frame.received_at - run.started_at) // window
            groups.setdefault((slot, frame.node_id), []).append(frame.rssi)
        for rssis in groups.values():
            if len(rssis) >= 2:
                mean = math.fsum(rssis) / len(rssis)
                squares.append(math.fsum((rssi - mean) ** 2 for rssi in rssis))
                degrees += len(rssis) - 1
    if degrees == 0:
        raise EvaluationError(
            f"σf를 추정할 수 없다: 겹치지 않는 {window_sec:g}초 구간에서 기기마다 프레임이 2개 이상인 곳이 없다"
        )
    return math.sqrt(math.fsum(squares) / degrees)


def estimate_static_sigma(pairs: Sequence[ScoredPair], frame_sigma_db: float) -> StaticSigmaFit:
    """기기별 고정 오차 σs(dB)를 `STATIC_SIGMA_GRID`에서 로그 가능도가 가장 큰 값으로 고른다.

    두 기기 모두 본 쌍(`later_value`가 None이 아님)만 쓴다. 뒤 기기만 못 본 쌍은 경계 확률이 1이라 σs와 상관이 없다.
    격자의 σs마다 로그 가능도는 `Σ log P (맞은 쌍) + log(1 − P) (틀린 쌍)`이고, P는 `boundary_probability`다.
    확률은 `[1e-12, 1 − 1e-12]`로 잘라 log(0)을 피한다. 값이 같으면 작은 σs를 고른다.
    골라진 값이 격자의 첫 값이나 끝 값이면 `at_edge`다 (모든 쌍을 맞혔거나 거의 다 틀렸다).
    쓸 쌍이 없으면 `EvaluationError`다. `frame_sigma_db`가 0보다 큰 유한한 수가 아니어도 `EvaluationError`다.
    """
    _check_frame_sigma(frame_sigma_db)
    heard = [pair for pair in pairs if pair.later_value is not None]
    if not heard:
        raise EvaluationError(f"σs를 추정할 쌍이 없다: 두 기기 모두 비콘을 본 쌍이 없다 (채점한 쌍 {len(pairs)}개)")

    best_index = 0
    best_likelihood = -math.inf
    for index, static_sigma_db in enumerate(STATIC_SIGMA_GRID):
        likelihood = math.fsum(_log_term(pair, frame_sigma_db, static_sigma_db) for pair in heard)
        if likelihood > best_likelihood:  # 같으면 앞의(작은) σs를 둔다
            best_index, best_likelihood = index, likelihood
    return StaticSigmaFit(
        STATIC_SIGMA_GRID[best_index], len(heard), best_index in (0, len(STATIC_SIGMA_GRID) - 1),
    )


# ── 기준 확률 ───────────────────────────────────────────────────────────


def boundary_table(
    pairs: Sequence[ScoredPair], frame_sigma_db: float, static_sigma_db: float,
    candidates: Sequence[float] = CANDIDATES,
) -> list[BoundaryStat]:
    """후보 기준 확률마다 경계 통계를 센다. 순서는 `candidates`의 순서다.

    쌍마다 `boundary_probability`로 경계 확률 P를 구한다 (뒤 기기만 못 봤으면 1). 후보 `min_prob`의 통과는 `P >= min_prob`이고,
    `wrong`은 통과한 쌍 중 `correct`가 아닌 수다. `pairs`는 경계가 될 수 있는 쌍(`scored_pairs(..., adjacent_only=True)`)이다.
    `frame_sigma_db`가 0보다 큰 유한한 수가 아니거나 `static_sigma_db`가 0 이상의 유한한 수가 아니면 `EvaluationError`다.
    """
    _check_frame_sigma(frame_sigma_db)
    if not math.isfinite(static_sigma_db) or static_sigma_db < 0:
        raise EvaluationError(f"static_sigma_db: 0 이상의 유한한 수여야 한다 ({static_sigma_db!r})")
    scored = [(_probability(pair, frame_sigma_db, static_sigma_db), pair.correct) for pair in pairs]

    table = []
    for min_prob in candidates:
        corrects = [correct for probability, correct in scored if probability >= min_prob]  # 통과한 쌍의 correct
        passed, wrong = len(corrects), corrects.count(False)
        table.append(
            BoundaryStat(
                min_prob, len(pairs), passed, wrong,
                passed / len(pairs) if pairs else None,
                wrong / passed if passed else None,
            )
        )
    return table


def choose_threshold(table: Sequence[BoundaryStat], max_error: float) -> float | None:
    """통과한 경계 중 틀린 비율이 `max_error` 이하인 후보 가운데 가장 작은 `min_prob`. 없으면 None이다.

    통과한 경계가 없는 후보(`error_rate`가 None)는 만족하지 않는 것으로 본다.
    """
    chosen = [stat.min_prob for stat in table if stat.error_rate is not None and stat.error_rate <= max_error]
    return min(chosen) if chosen else None


def pair_agreement(pairs: Sequence[ScoredPair]) -> tuple[int, int]:
    """(맞은 쌍 수, 전체 쌍 수)."""
    return sum(pair.correct for pair in pairs), len(pairs)


# ── 보고서 행 ───────────────────────────────────────────────────────────


def order_rows(
    *, frame_sigma_db: float, static_fit: StaticSigmaFit, agreement: tuple[int, int],
    chosen: float | None, test_stat: BoundaryStat | None, max_error: float,
) -> list[dict[str, Any]]:
    """`report.write_report`에 넘길 지표 10의 행 여섯 개. 순서는 이름 순서와 같다 (spec 결정 14).

    `m10_frame_sigma_db`, `m10_static_sigma_db`, `m10_pair_order_agreement`, `m10_boundary_min_prob`,
    `m10_boundary_pass_rate`, `m10_boundary_error_rate`다. 통과 칸은 `m10_boundary_error_rate`만 쓰고 (`max_error`와 비교),
    나머지는 비운다 — 순서 일치율의 기준은 미결이다 (#279).
    `agreement`는 `pair_agreement`의 값이고, `test_stat`은 시험 회차를 `chosen`으로 센 통계다.
    `chosen`이 None이면 (검증 회차에서 고르지 못했다) 시험 두 행은 값과 통과 칸을 비우고 이유를 쓴다.
    통과한 경계가 없으면 오류 비율 행의 값과 통과 칸을 비운다. 이때 `failures`도 비운다 —
    `write_report`는 `failures == 0`인 행에 `trials`로 신뢰구간 상한을 내는데, `trials`가 0이면 계산할 수 없다.
    """
    agreed, total = agreement
    if total:
        agreement_row = metric_row("m10_pair_order_agreement", value=agreed / total, trials=total, failures=total - agreed)
    else:
        agreement_row = metric_row("m10_pair_order_agreement", trials=0, note="채점한 쌍 없음")
    rows = [
        metric_row("m10_frame_sigma_db", value=frame_sigma_db),
        metric_row(
            "m10_static_sigma_db", value=static_fit.sigma_db, trials=static_fit.pairs,
            note="탐색 범위 끝" if static_fit.at_edge else "",
        ),
        agreement_row,
        metric_row(
            "m10_boundary_min_prob", value="" if chosen is None else chosen, note=_NOT_CHOSEN if chosen is None else "",
        ),
    ]
    if chosen is None or test_stat is None:
        rows.append(metric_row("m10_boundary_pass_rate", note=_NOT_TESTED))
        rows.append(metric_row("m10_boundary_error_rate", limit=max_error, note=_NOT_TESTED))
        return rows

    if test_stat.pass_rate is None:
        rows.append(metric_row("m10_boundary_pass_rate", trials=0, note="채점한 경계 없음"))
    else:
        rows.append(metric_row("m10_boundary_pass_rate", value=test_stat.pass_rate, trials=test_stat.boundaries))
    if test_stat.error_rate is None:
        rows.append(metric_row("m10_boundary_error_rate", limit=max_error, trials=0, note="통과한 경계 없음"))
    else:
        rows.append(
            metric_row(
                "m10_boundary_error_rate", value=test_stat.error_rate, limit=max_error,
                passed=test_stat.error_rate <= max_error, trials=test_stat.passed, failures=test_stat.wrong,
            )
        )
    return rows


# ── 도우미 ──────────────────────────────────────────────────────────────


def _duration(seconds: float, name: str) -> timedelta:
    """초를 `timedelta`로 바꾼다. 0보다 큰 유한한 수가 아니거나 1 마이크로초 단위로 0이 되면 `EvaluationError`다."""
    duration = timedelta(seconds=seconds) if math.isfinite(seconds) and seconds > 0 else timedelta(0)
    if duration <= timedelta(0):
        raise EvaluationError(f"{name}: 1 마이크로초 단위로 0보다 큰 유한한 수여야 한다 ({seconds!r})")
    return duration


def _check_frame_sigma(frame_sigma_db: float) -> None:
    """σf가 0보다 큰 유한한 수인지 본다. 서버의 `order_frame_sigma_db`도 0 초과이고, 0이면 σs가 0일 때 경계 확률이 0으로 나눈다."""
    if not math.isfinite(frame_sigma_db) or frame_sigma_db <= 0:
        raise EvaluationError(f"frame_sigma_db: 0보다 큰 유한한 수여야 한다 ({frame_sigma_db!r})")


def _probability(pair: ScoredPair, frame_sigma_db: float, static_sigma_db: float) -> float:
    """쌍의 경계 확률. 판정의 `boundary_probability`를 그대로 부른다."""
    return boundary_probability(
        pair.earlier_value, pair.earlier_count, pair.later_value, pair.later_count, frame_sigma_db, static_sigma_db,
    )


def _log_term(pair: ScoredPair, frame_sigma_db: float, static_sigma_db: float) -> float:
    """쌍 하나의 로그 가능도: 맞았으면 log P, 틀렸으면 log(1 − P). P는 [1e-12, 1 − 1e-12]로 자른다."""
    probability = min(max(_probability(pair, frame_sigma_db, static_sigma_db), _PROB_FLOOR), 1 - _PROB_FLOOR)
    return math.log(probability if pair.correct else 1 - probability)
