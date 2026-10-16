"""근접 모델의 관측 값 적합 (specs/core-proximity-model-fit/spec.md 6절).

수집 회차(라벨이 붙은 RSSI 프레임 묶음)로 앵커마다 `A`, `n`, `sigma`, `none_rssi`, `none_sigma`를 구하는 순수 함수다.
기기 앞 표본은 경로손실 식 `rssi = A − 10·n·log10(d)`에 최소제곱으로 맞추고, `none` 표본은 평균과 표준편차를 낸다.
최소제곱과 표준편차는 `math.fsum`의 닫힌 식이라서 같은 입력에는 같은 값이 나온다. DB, 현재 시각, 무작위를 쓰지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from nearpilot.shared.models import BeaconKey, RssiFrame

NONE_LABEL: str = "none"  # 수집 라벨 "어느 기기 앞도 아님"


class FitError(ValueError):
    """적합 입력이 잘못됐거나, 입력으로 모델 값을 정할 수 없다."""


@dataclass(frozen=True, slots=True)
class LabeledRun:
    """수집 회차 하나. 구간은 `started_at <= frame_ts < ended_at`이다."""

    run_id: str
    label: str  # node_id 또는 NONE_LABEL
    beacon: BeaconKey
    started_at: datetime
    ended_at: datetime
    frames: tuple[RssiFrame, ...]  # list를 받아도 tuple로 바꾼다

    def __post_init__(self) -> None:
        object.__setattr__(self, "frames", tuple(self.frames))


@dataclass(frozen=True, slots=True)
class AnchorLayout:
    """기기 하나의 배치. 단위는 미터다."""

    x: float  # 앵커(기기) 위치
    y: float
    front_x: float  # 기기 앞 위치
    front_y: float


@dataclass(frozen=True, slots=True)
class PlaceLayout:
    """장소 하나의 배치. `nodes`의 기기가 모두 앵커다."""

    place_id: str
    nodes: Mapping[str, AnchorLayout]  # node_id → 배치
    # "어느 기기 앞도 아님" 상태의 대표 위치 (x, y), 미터. 적합에는 쓰지 않고 모델 파일에 그대로 옮긴다
    none_points: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", dict(self.nodes))
        object.__setattr__(self, "none_points", tuple((float(x), float(y)) for x, y in self.none_points))


@dataclass(frozen=True, slots=True)
class ObservationParams:
    """앵커 하나의 적합 결과. 모델 파일의 기기 항목 중 관측에서 나오는 값이다."""

    A: float  # 1 m 기준 RSSI, dBm
    n: float  # 경로손실 지수
    sigma: float  # 기기 앞 상태의 RSSI 표준편차, dB
    none_rssi: float  # "어느 기기 앞도 아님" 상태의 RSSI 평균, dBm
    none_sigma: float  # "어느 기기 앞도 아님" 상태의 RSSI 표준편차, dB


def run_frames(run: LabeledRun, layout: PlaceLayout) -> list[RssiFrame]:
    """회차에서 적합과 평가에 쓸 프레임을 정렬 순서로 고른다.

    근접 추정기와 같은 규칙이다. 비콘이 회차와 같고, 앵커가 배치에 있고, RSSI가 유한하고,
    `started_at <= frame_ts < ended_at`인 프레임만 남긴다. 같은 `(node_id, session_id, seq, beacon)`은 정렬 순서로 처음 하나만 쓴다.
    `received_at`으로는 거르지 않는다. 평가 때는 `compute_posteriors`가 판정과 같은 규칙으로 늦은 프레임을 뺀다.
    """
    usable = sorted(
        (
            frame
            for frame in run.frames
            if frame.beacon == run.beacon
            and frame.node_id in layout.nodes
            and math.isfinite(frame.rssi)  # 유한하지 않은 RSSI는 결측과 같이 버린다
            and run.started_at <= frame.frame_ts < run.ended_at
        ),
        # 맨 끝의 rssi는 앞의 값이 모두 같은 프레임의 순서까지 정해서, 입력 순서가 결과를 바꾸지 못하게 한다.
        key=lambda frame: (frame.frame_ts, frame.node_id, frame.session_id, frame.seq, frame.received_at, frame.rssi),
    )
    seen: set[tuple[str, str, int, BeaconKey]] = set()
    selected: list[RssiFrame] = []
    for frame in usable:
        key = (frame.node_id, frame.session_id, frame.seq, frame.beacon)
        if key not in seen:
            seen.add(key)
            selected.append(frame)
    return selected


def fit_observation_params(
    layout: PlaceLayout, runs: Sequence[LabeledRun], *, min_distance_m: float,
) -> dict[str, ObservationParams]:
    """학습 회차로 앵커마다 `ObservationParams`를 적합한다. 결과는 앵커 ID 정렬 순이다.

    기기 앞 회차(라벨이 `NONE_LABEL`이 아닌 회차)로 `A`, `n`, `sigma`를, `none` 회차로 `none_rssi`, `none_sigma`를 구한다.
    입력이 잘못됐거나 값을 정할 수 없으면 `FitError`다. 앵커마다 앞 표본, `n`·`sigma`, none 표본, `none_sigma` 순으로 본다.
    """
    for run in runs:
        if run.label != NONE_LABEL and run.label not in layout.nodes:
            raise FitError(
                f"회차 {run.run_id!r}: 라벨 {run.label!r}에 해당하는 기기가 배치에 없다 ({NONE_LABEL!r}도 아니다)"
            )
    front_runs = [run for run in runs if run.label != NONE_LABEL]
    none_runs = [run for run in runs if run.label == NONE_LABEL]
    missing = [kind for kind, found in (("기기 앞", front_runs), (NONE_LABEL, none_runs)) if not found]
    if missing:
        raise FitError(f"학습 회차에 {' 회차와 '.join(missing)} 회차가 없다")

    # 회차의 프레임은 한 번만 고른다. 앵커마다 그 앵커의 프레임만 쓴다.
    front_frames = [(layout.nodes[run.label], run_frames(run, layout)) for run in front_runs]
    none_frames = [frame for run in none_runs for frame in run_frames(run, layout)]

    params: dict[str, ObservationParams] = {}
    for anchor_id in sorted(layout.nodes):
        anchor = layout.nodes[anchor_id]

        distances: list[float] = []
        rssis: list[float] = []
        for front, frames in front_frames:
            distance = max(math.dist((anchor.x, anchor.y), (front.front_x, front.front_y)), min_distance_m)
            for frame in frames:
                if frame.node_id == anchor_id:
                    distances.append(distance)
                    rssis.append(frame.rssi)
        if len(rssis) < 3 or len(set(distances)) < 2:
            raise FitError(
                f"앵커 {anchor_id!r}: 기기 앞 회차에서 프레임 {len(rssis)}개, 서로 다른 거리 {len(set(distances))}개를 받았다"
                " (프레임 3개 이상, 서로 다른 거리 2개 이상이어야 한다)"
            )

        # rssi = A + n·u, u = −10·log10(d). 최소제곱의 닫힌 식이다.
        us = [-10.0 * math.log10(distance) for distance in distances]
        u_mean = math.fsum(us) / len(us)
        r_mean = math.fsum(rssis) / len(rssis)
        du = [u - u_mean for u in us]
        n = math.fsum(a * (r - r_mean) for a, r in zip(du, rssis)) / math.fsum(a * a for a in du)
        intercept = r_mean - n * u_mean
        residuals = [r - (intercept + n * u) for u, r in zip(us, rssis)]
        sigma = math.sqrt(math.fsum(e * e for e in residuals) / (len(rssis) - 2))
        if n <= 0 or sigma == 0:
            raise FitError(
                f"앵커 {anchor_id!r}: 적합한 값을 모델 파일이 받지 않는다 (n={n!r}, sigma={sigma!r}). n과 sigma는 0보다 커야 한다"
            )

        none_rssis = [frame.rssi for frame in none_frames if frame.node_id == anchor_id]
        if len(none_rssis) < 2:
            raise FitError(f"앵커 {anchor_id!r}: none 회차에서 프레임 {len(none_rssis)}개를 받았다 (2개 이상이어야 한다)")
        none_rssi = math.fsum(none_rssis) / len(none_rssis)
        deviations = [r - none_rssi for r in none_rssis]
        none_sigma = math.sqrt(math.fsum(e * e for e in deviations) / (len(none_rssis) - 1))
        if none_sigma == 0:
            raise FitError(
                f"앵커 {anchor_id!r}: none_sigma가 0이다 (none_sigma={none_sigma!r}, none_rssi={none_rssi!r})."
                " none 회차의 RSSI가 모두 같다"
            )

        params[anchor_id] = ObservationParams(A=intercept, n=n, sigma=sigma, none_rssi=none_rssi, none_sigma=none_sigma)
    return params
