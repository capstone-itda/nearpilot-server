"""근접 모델의 관측 값 적합과 검증 손실 선택 (specs/core-proximity-model-fit/spec.md 6절).

수집 회차(라벨이 붙은 RSSI 프레임 묶음)로 앵커마다 `A`, `n`, `sigma`, `none_rssi`, `none_sigma`를 구하는 순수 함수다.
기기 앞 표본은 경로손실 식 `rssi = A − 10·n·log10(d)`에 최소제곱으로 맞추고, `none` 표본은 평균과 표준편차를 낸다.
최소제곱과 표준편차는 `math.fsum`의 닫힌 식이라서 같은 입력에는 같은 값이 나온다. DB, 현재 시각, 무작위를 쓰지 않는다.
검증 회차로는 τ, `stay`, `window_sec`의 조합마다 정답 상태의 평균 로그 손실을 `compute_posteriors`로 재고,
손실이 가장 작은 조합으로 `ProximityModel`을 만든다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from nearpilot.core.proximity.filter import compute_posteriors
from nearpilot.core.proximity.model import NodeModel, ProximityModel
from nearpilot.shared.enums import Capability, DeviceState, Occupancy, TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, RssiFrame

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


@dataclass(frozen=True, slots=True)
class FitConfig:
    """적합 설정. 버전과 후보 값은 코드 상수가 아니라 호출자가 정한다. 값의 범위는 `fit_model`이 검사한다."""

    model_version: str
    calib_version: str
    min_distance_m: float
    taus: tuple[float, ...]  # 후보 우도 온도 τ. list를 받아도 tuple로 바꾼다
    stays: tuple[float, ...]  # 후보 stay (한 시간 칸에서 같은 상태에 머무를 확률)
    window_secs: tuple[float, ...]  # 후보 관측 구간 길이, 초

    def __post_init__(self) -> None:
        object.__setattr__(self, "taus", tuple(self.taus))
        object.__setattr__(self, "stays", tuple(self.stays))
        object.__setattr__(self, "window_secs", tuple(self.window_secs))


@dataclass(frozen=True, slots=True)
class GridLoss:
    """후보 조합 하나의 검증 손실."""

    tau: float
    stay: float
    window_sec: float
    loss: float  # 검증 평균 로그 손실. 정답 확률이 0 이하로 내려간 시각이 있으면 무한대다


@dataclass(frozen=True, slots=True)
class FitResult:
    """`fit_model`의 결과. `losses`는 후보 순서(taus → stays → window_secs 중첩 순)다."""

    model: ProximityModel
    losses: tuple[GridLoss, ...]


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


def _positive_number(value: object, name: str, *, upper: float = math.inf) -> float:
    """bool이 아닌 수가 0보다 크고 upper보다 작은지 본다. NaN과 무한대는 범위 밖이다. float로 돌려주고, 어기면 `FitError`다."""
    number = math.nan
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:  # float로 바꿀 수 없을 만큼 큰 int
            pass
    if not 0.0 < number < upper:
        requirement = "0보다 큰 유한한 수" if upper == math.inf else f"0보다 크고 {upper:g}보다 작은 수"
        raise FitError(f"{name}: {requirement}여야 한다 ({value!r:.40})")  # 큰 int가 메시지를 채우지 않게 자른다
    return number


def _candidates(values: Sequence[object], name: str, *, upper: float = math.inf) -> tuple[float, ...]:
    """후보 목록이 비어 있지 않고 값이 모두 범위 안인지 본다. 값은 float로 바꿔 돌려준다."""
    if not values:
        raise FitError(f"{name}: 후보가 하나 이상 있어야 한다")
    return tuple(_positive_number(value, name, upper=upper) for value in values)


def _check_label(run: LabeledRun, layout: PlaceLayout) -> None:
    if run.label != NONE_LABEL and run.label not in layout.nodes:
        raise FitError(
            f"회차 {run.run_id!r}: 라벨 {run.label!r}에 해당하는 기기가 배치에 없다 ({NONE_LABEL!r}도 아니다)"
        )


def validation_log_loss(
    model: ProximityModel, layout: PlaceLayout, runs: Sequence[LabeledRun], *, bin_sec: float,
) -> float:
    """모델이 검증 회차의 정답 상태에 준 확률의 평균 로그 손실 −log P(정답)을 낸다.

    회차마다 평가 시각은 `started_at + window_sec`부터 `bin_sec` 간격으로 `ended_at` 이하까지다.
    시각마다 `compute_posteriors`에 배치의 모든 기기를 후보로 넘긴다. 정답이 `NONE_LABEL`이면 확률은
    1 − (후보 확률의 합)이다. 확률이 0 이하이면 그 시각의 손실은 무한대다. 시각 손실을 회차마다 평균한 뒤
    회차끼리 평균한다 — 회차가 독립 단위다. 회차가 없거나, 평가 시각이 없는 회차가 있거나,
    라벨이 배치에 없으면 `FitError`다.
    """
    bin_sec = _positive_number(bin_sec, "bin_sec")
    if not runs:
        raise FitError("검증 회차가 없다")
    # 근접 추정은 node_id, place_id, trust_state만 읽는다. 나머지 필드는 자리 채움 값이다.
    nodes = [
        NodeInfo(
            node_id=node_id,
            place_id=layout.place_id,
            capability=Capability.LIGHT,
            occupancy=Occupancy.SHARED,
            trust_state=TrustState.TRUSTED,
            device_state=DeviceState.AVAILABLE,
            exclusive_group=None,
            last_seen_at=None,
        )
        for node_id in layout.nodes
    ]
    window = timedelta(seconds=model.window_sec)
    step = timedelta(seconds=bin_sec)

    run_losses: list[float] = []
    for run in runs:
        _check_label(run, layout)
        frames = run_frames(run, layout)
        # k번째 시각은 매번 첫 시각에서 정수 마이크로초로 센다. float 초를 더해 가면 경계의 시각이 미끄러진다.
        first = run.started_at + window
        times = [first + k * step for k in range((run.ended_at - first) // step + 1)]
        if not times:
            raise FitError(f"회차 {run.run_id!r}: 구간이 window_sec({model.window_sec:g}초)보다 짧아 평가 시각이 없다")

        time_losses: list[float] = []
        for at in times:
            posteriors = compute_posteriors(model, frames, run.beacon, nodes, nodes, at, bin_sec=bin_sec).posteriors
            p = 1.0 - math.fsum(posteriors.values()) if run.label == NONE_LABEL else posteriors[run.label]
            time_losses.append(-math.log(p) if p > 0 else math.inf)
        run_losses.append(math.fsum(time_losses) / len(time_losses))
    return math.fsum(run_losses) / len(run_losses)


def fit_model(
    layout: PlaceLayout,
    train: Sequence[LabeledRun],
    validation: Sequence[LabeledRun],
    config: FitConfig,
    *,
    bin_sec: float,  # Settings.rssi_frame_sec
) -> FitResult:
    """학습 회차로 관측 값을 적합하고, 검증 회차로 τ·`stay`·`window_sec` 조합을 골라 `ProximityModel`을 만든다.

    설정, 회차 ID, 검증 회차의 라벨과 길이를 먼저 검사한다. 조합은 taus → stays → window_secs 중첩 순으로 재고
    (`validation_log_loss`), 손실이 가장 작은 조합을 고른다. 같으면 먼저 나온 조합이다. 모든 손실이 무한대이면 고를 근거가 없다.
    입력이 잘못됐거나 고를 수 없으면 `FitError`다. 고른 모델은 τ = 1이어도 `calibration_method`가 `temperature`다.
    모델 파일은 쓰지 않는다.
    """
    # 설정 검사는 적합보다 먼저 한다. 적합은 min_distance_m를 검사하지 않아서, 0 이하이면 log10(0)에서 막힌다.
    for name, value in (("model_version", config.model_version), ("calib_version", config.calib_version)):
        if not isinstance(value, str) or not value:
            raise FitError(f"{name}: 비어 있지 않은 문자열이어야 한다 ({value!r})")
    min_distance_m = _positive_number(config.min_distance_m, "min_distance_m")
    taus = _candidates(config.taus, "taus")
    stays = _candidates(config.stays, "stays", upper=1.0)
    window_secs = _candidates(config.window_secs, "window_secs")
    bin_sec = _positive_number(bin_sec, "bin_sec")

    counts = Counter(run.run_id for run in (*train, *validation))
    repeated = sorted(run_id for run_id, count in counts.items() if count > 1)
    if repeated:
        raise FitError(f"회차 ID가 겹친다 (학습과 검증을 합쳐 한 번씩만 쓸 수 있다): {repeated}")
    for run in validation:
        _check_label(run, layout)
    if not validation:
        raise FitError("검증 회차가 없다")
    longest = timedelta(seconds=max(window_secs))
    for run in validation:
        if run.started_at + longest > run.ended_at:
            raise FitError(
                f"검증 회차 {run.run_id!r}: 구간이 가장 긴 window_sec({max(window_secs):g}초)보다 짧아 평가 시각이 없다"
            )

    params = fit_observation_params(layout, train, min_distance_m=min_distance_m)
    nodes = {
        node_id: NodeModel(
            node_id=node_id,
            x=layout.nodes[node_id].x,
            y=layout.nodes[node_id].y,
            front_x=layout.nodes[node_id].front_x,
            front_y=layout.nodes[node_id].front_y,
            A=params[node_id].A,
            n=params[node_id].n,
            sigma=params[node_id].sigma,
            none_rssi=params[node_id].none_rssi,
            none_sigma=params[node_id].none_sigma,
        )
        for node_id in sorted(layout.nodes)
    }

    models: list[ProximityModel] = []
    losses: list[GridLoss] = []
    for tau in taus:
        for stay in stays:
            for window_sec in window_secs:
                model = ProximityModel(
                    place_id=layout.place_id,
                    model_version=config.model_version,
                    calib_version=config.calib_version,
                    window_sec=window_sec,
                    stay=stay,
                    min_distance_m=min_distance_m,
                    calibration_method="temperature",
                    calibration_tau=tau,
                    nodes=nodes,
                    validation={},
                    none_points=layout.none_points,
                )
                models.append(model)
                loss = validation_log_loss(model, layout, validation, bin_sec=bin_sec)
                losses.append(GridLoss(tau=tau, stay=stay, window_sec=window_sec, loss=loss))

    best = min(range(len(losses)), key=lambda i: losses[i].loss)  # 손실이 같으면 먼저 나온 조합이다
    if math.isinf(losses[best].loss):
        raise FitError(
            f"후보 {len(losses)}개의 검증 손실이 모두 무한대다. 정답 상태의 확률이 어느 조합에서도 0보다 크지 않아 고를 근거가 없다"
        )

    report = {
        "criterion": "log_loss",
        "loss": losses[best].loss,
        "grid": [
            {
                "tau": grid.tau,
                "stay": grid.stay,
                "window_sec": grid.window_sec,
                "loss": grid.loss if math.isfinite(grid.loss) else None,  # 표준 JSON에는 무한대가 없다
            }
            for grid in losses
        ],
        "train_runs": [run.run_id for run in train],
        "validation_runs": [run.run_id for run in validation],
    }
    return FitResult(model=replace(models[best], validation=report), losses=tuple(losses))
