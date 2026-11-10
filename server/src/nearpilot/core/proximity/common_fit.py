"""배치 없는 공통 근접 모델의 적합과 검증 손실 (specs/core-proximity-common-model/spec.md 6절 결정 11, 12).

장소마다 라벨이 붙은 수집 회차 묶음(`PlaceRuns`)을 받아 공통 모델 하나를 만드는 순수 함수다. 좌표와 배치 파일은 쓰지 않는다.
장소의 앵커는 그 장소 회차의 프레임에 나온 기기와 라벨 기기다. 장소마다 모든 회차의 프레임에서 자동 영점을 먼저 찾고
(`autozero.zeros_from_frames`, 라벨은 쓰지 않는다), 학습 회차의 칸 평균을 자동 영점으로 보정한 값에서 역할(`front`, `other`, `none`)별
정규분포를 구한다. 평균과 모표준편차는 `math.fsum`의 닫힌 식이라서 같은 입력에는 입력 순서와 상관없이 같은 값이 나온다.
검증 회차로는 τ, `stay`, `window_sec`의 조합마다 정답 상태의 평균 로그 손실을 `compute_posteriors`로 재고,
손실이 가장 작은 조합으로 `CommonModel`을 만든다. DB, 현재 시각, 무작위를 쓰지 않고 모델 파일도 쓰지 않는다
(파일 쓰기는 `common_model.write_common_model_file`).
`NONE_LABEL`, `FitError`, `LabeledRun`, `GridLoss`는 옛 `fit.py`와 같은 이름이라서, 옛 모듈이 여기서 가져다 내보낸다.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from nearpilot.core.proximity.autozero import bin_index, zeros_from_frames
from nearpilot.core.proximity.common_model import CommonModel, RoleParams
from nearpilot.core.proximity.posterior import compute_posteriors
from nearpilot.shared.enums import DeviceState, Occupancy, TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, RssiFrame

NONE_LABEL: str = "none"  # 수집 라벨 "어느 기기 앞도 아님"

_CALIB_VERSION = "fit"  # 평가가 `compute_posteriors`에 넘기는 보정 버전. 결과에 적힐 뿐 손실 계산에는 쓰지 않는다


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
class GridLoss:
    """후보 조합 하나의 검증 손실."""

    tau: float
    stay: float
    window_sec: float
    loss: float  # 검증 평균 로그 손실. 정답 확률이 0 이하로 내려간 시각이 있으면 무한대다


@dataclass(frozen=True, slots=True)
class PlaceRuns:
    """장소 하나의 수집 회차 묶음. 학습 묶음과 검증 묶음이 따로 있고, 같은 장소면 `place_id`가 같다."""

    place_id: str
    runs: tuple[LabeledRun, ...]  # list를 받아도 tuple로 바꾼다

    def __post_init__(self) -> None:
        object.__setattr__(self, "runs", tuple(self.runs))


@dataclass(frozen=True, slots=True)
class CommonFitConfig:
    """공통 적합 설정. 버전과 후보 값은 코드 상수가 아니라 호출자가 정한다. 값의 범위는 `fit_common_model`이 검사한다."""

    model_version: str
    taus: tuple[float, ...]  # 후보 우도 온도 τ. list를 받아도 tuple로 바꾼다
    stays: tuple[float, ...]  # 후보 stay (한 시간 칸에서 같은 상태에 머무를 확률)
    window_secs: tuple[float, ...]  # 후보 관측 구간 길이, 초
    auto_zero_min_bins: int  # 자동 영점을 찾는 데 필요한 표본 수. 모델 파일의 `auto_zero.min_bins`가 된다
    validation: Mapping[str, Any] = field(default_factory=dict)  # 모델 파일의 `validation`에 더할 값

    def __post_init__(self) -> None:
        object.__setattr__(self, "taus", tuple(self.taus))
        object.__setattr__(self, "stays", tuple(self.stays))
        object.__setattr__(self, "window_secs", tuple(self.window_secs))


@dataclass(frozen=True, slots=True)
class CommonFitResult:
    """`fit_common_model`의 결과. `losses`는 후보 순서(taus → stays → window_secs 중첩 순)다."""

    model: CommonModel
    losses: tuple[GridLoss, ...]
    zeros: Mapping[str, Mapping[str, float]]  # place_id → node_id → 자동 영점. 찾지 못한 기기는 없다


# ── 입력 검사 ───────────────────────────────────────────────────────
# 옛 `fit.py`의 도우미와 같은 검사다. 옛 모듈이 이 모듈을 import해서 거꾸로 import할 수 없어 따로 둔다.


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


def _bin_sec(value: object) -> float:
    """시간 칸 길이를 검사한다. `timedelta`로 바꿨을 때 0이 되는 값(0.5 µs 미만)도 거부한다."""
    bin_sec = _positive_number(value, "bin_sec")
    if timedelta(seconds=bin_sec) <= timedelta(0):
        raise FitError(f"bin_sec: 1 마이크로초 단위로 0보다 커야 한다 ({value!r})")
    return bin_sec


def _is_path_component(value: object) -> bool:
    """경로 조각 하나인가. 모델 파일 경로 `{root}/{model_version}.json`이 루트 밖으로 나가지 않게 한다."""
    return isinstance(value, str) and value not in ("", ".", "..") and "/" not in value and "\\" not in value


def _candidates(values: Sequence[object], name: str, *, upper: float = math.inf) -> tuple[float, ...]:
    """후보 목록이 비어 있지 않고 값이 모두 범위 안인지 본다. 값은 float로 바꿔 돌려준다."""
    if not values:
        raise FitError(f"{name}: 후보가 하나 이상 있어야 한다")
    return tuple(_positive_number(value, name, upper=upper) for value in values)


def _by_place(groups: Sequence[PlaceRuns], name: str) -> dict[str, PlaceRuns]:
    """장소 ID → 묶음. 같은 목록에 같은 장소가 둘 이상이면 `FitError`다."""
    by_place: dict[str, PlaceRuns] = {}
    for group in groups:
        if group.place_id in by_place:
            raise FitError(f"{name} 목록에 같은 장소가 둘 이상 있다: {group.place_id!r}")
        by_place[group.place_id] = group
    return by_place


# ── 앵커, 프레임, 자동 영점 ─────────────────────────────────────────


def place_frames(run: LabeledRun, anchors: Collection[str]) -> list[RssiFrame]:
    """회차에서 적합과 평가에 쓸 프레임을 정렬 순서로 고른다.

    비콘이 회차와 같고, 기기가 `anchors`에 있고,
    RSSI가 유한하고, `started_at <= frame_ts < ended_at`인 프레임만 남긴다.
    같은 `(node_id, session_id, seq, beacon)`은 정렬 순서로 처음 하나만 쓴다.
    `received_at`으로는 거르지 않는다. 평가 때는 `compute_posteriors`가 판정과 같은 규칙으로 늦은 프레임을 뺀다.
    """
    usable = sorted(
        (
            frame
            for frame in run.frames
            if frame.beacon == run.beacon
            and frame.node_id in anchors
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


def place_anchors(*groups: PlaceRuns) -> frozenset[str]:
    """같은 장소의 묶음들의 앵커. 회차 프레임에 나온 `node_id`와 `NONE_LABEL`이 아닌 라벨의 합집합이다 (spec 결정 11).

    배치 파일 없이 정한다. 프레임은 비콘, 구간, RSSI로 거르지 않고 모두 본다.
    보고를 멈춘 라벨 기기도 앵커로 남는다 — 평가가 쓰는 근접 참여 기기의 근사다 (결정 11, 17).
    """
    anchors: set[str] = set()
    for group in groups:
        for run in group.runs:
            anchors.update(frame.node_id for frame in run.frames)
            if run.label != NONE_LABEL:
                anchors.add(run.label)
    return frozenset(anchors)


def place_zeros(groups: Sequence[PlaceRuns], *, bin_sec: float, min_bins: int) -> dict[str, float]:
    """장소의 자동 영점. 같은 장소 묶음들의 모든 회차 프레임에서 찾는다 (spec 결정 4). 라벨은 쓰지 않는다.

    앵커는 `place_anchors(*groups)`이고, 프레임은 각 회차의 `place_frames`를 모두 모은 것이다.
    표본이 `min_bins`보다 적은 앵커는 결과에 없다 (보정할 때 0 dB로 읽는다).
    """
    anchors = place_anchors(*groups)
    frames = [frame for group in groups for run in group.runs for frame in place_frames(run, anchors)]
    return zeros_from_frames(frames, anchors, bin_sec=bin_sec, min_bins=min_bins)


# ── 검증 손실 ───────────────────────────────────────────────────────


def validation_log_loss(
    model: CommonModel, places: Sequence[PlaceRuns], zeros: Mapping[str, Mapping[str, float]], *, bin_sec: float,
) -> float:
    """모델이 검증 회차의 정답 상태에 준 확률의 평균 로그 손실 −log P(정답)을 낸다.

    회차마다 평가 시각은 `started_at + window_sec`부터 `bin_sec` 간격으로 `ended_at` 이하까지다.
    장소마다 그 장소 앵커의 `trusted` 기기를 후보와 `place_nodes`로, 앵커 전부를 `participants`로 넘기고,
    `zeros[place_id]`를 자동 영점으로 쓴다 (없는 장소는 모든 기기가 영점 없음이다).
    정답이 `NONE_LABEL`이면 확률은 1 − (후보 확률의 합)이다. 확률이 0 이하이면 그 시각의 손실은 무한대다.
    시각 손실을 회차마다 평균한 뒤 회차끼리 평균한다 — 회차가 독립 단위이고, 모든 장소의 회차를 한데 모은다.
    회차가 없거나 평가 시각이 없는 회차가 있으면 `FitError`다.
    """
    bin_sec = _bin_sec(bin_sec)
    if not any(place.runs for place in places):
        raise FitError("검증 회차가 없다")
    window = timedelta(seconds=model.window_sec)
    step = timedelta(seconds=bin_sec)

    run_losses: list[float] = []
    for place in places:
        anchors = place_anchors(place)
        # 근접 추정은 node_id, place_id, trust_state만 읽는다. 나머지 필드는 자리 채움 값이다.
        nodes = [
            NodeInfo(
                node_id=node_id,
                place_id=place.place_id,
                kind="light",
                occupancy=Occupancy.SHARED,
                trust_state=TrustState.TRUSTED,
                device_state=DeviceState.AVAILABLE,
                exclusive_group=None,
                last_seen_at=None,
            )
            for node_id in sorted(anchors)
        ]
        place_zero = zeros.get(place.place_id, {})
        for run in place.runs:
            frames = place_frames(run, anchors)
            # k번째 시각은 매번 첫 시각에서 정수 마이크로초로 센다. float 초를 더해 가면 경계의 시각이 미끄러진다.
            first = run.started_at + window
            times = [first + k * step for k in range((run.ended_at - first) // step + 1)]
            if not times:
                raise FitError(f"회차 {run.run_id!r}: 구간이 window_sec({model.window_sec:g}초)보다 짧아 평가 시각이 없다")

            time_losses: list[float] = []
            for at in times:
                posteriors = compute_posteriors(
                    model, frames, run.beacon, nodes, nodes, at,
                    bin_sec=bin_sec, participants=anchors, zeros=place_zero, calib_version=_CALIB_VERSION,
                ).posteriors
                p = 1.0 - math.fsum(posteriors.values()) if run.label == NONE_LABEL else posteriors[run.label]
                time_losses.append(-math.log(p) if p > 0 else math.inf)
            run_losses.append(math.fsum(time_losses) / len(time_losses))
    return math.fsum(run_losses) / len(run_losses)


# ── 적합 ────────────────────────────────────────────────────────────


def _check_labels(place_id: str, groups: Sequence[PlaceRuns]) -> None:
    """회차의 라벨 기기가 장소의 프레임에 한 번도 나오지 않으면 `FitError`다. 라벨을 잘못 쓴 회차를 막는다.

    `place_anchors`는 라벨 기기도 앵커로 치지만, 적합은 한 번도 보고하지 않은 기기를 라벨로 받지 않는다.
    """
    heard = {frame.node_id for group in groups for run in group.runs for frame in run.frames}
    for group in groups:
        for run in group.runs:
            if run.label != NONE_LABEL and run.label not in heard:
                raise FitError(
                    f"회차 {run.run_id!r}: 라벨 {run.label!r}가 장소 {place_id!r}의 앵커가 아니다"
                    f" (이 장소 회차의 프레임에 나온 기기가 아니고 {NONE_LABEL!r}도 아니다)"
                )


def _role_samples(
    group: PlaceRuns, anchors: Collection[str], zero: Mapping[str, float], bin_sec: float,
) -> dict[str, list[float]]:
    """학습 회차의 칸 평균을 자동 영점으로 보정한 역할별 표본 (spec 결정 12).

    회차마다 `place_frames`를 `bin_index`의 칸으로 묶어 앵커별 평균을 내고, 영점으로 보정한다 (`r − z`, 영점이 없으면 0).
    라벨이 기기 i이면 앵커 i의 값은 `front`, 다른 앵커의 값은 `other`이고, 라벨이 `NONE_LABEL`이면 모든 값이 `none`이다.
    칸에 값이 없는 앵커는 표본이 없다.
    """
    samples: dict[str, list[float]] = {"front": [], "other": [], "none": []}
    for run in group.runs:
        bins: dict[int, dict[str, list[float]]] = {}
        for frame in place_frames(run, anchors):
            bins.setdefault(bin_index(frame.frame_ts, bin_sec), {}).setdefault(frame.node_id, []).append(frame.rssi)
        for by_anchor in bins.values():
            for anchor_id, values in by_anchor.items():
                if run.label == NONE_LABEL:
                    role = "none"
                else:
                    role = "front" if anchor_id == run.label else "other"
                samples[role].append(math.fsum(values) / len(values) - zero.get(anchor_id, 0.0))
    return samples


def _role_params(samples: Sequence[float], role: str) -> RoleParams:
    """표본의 평균과 모표준편차. 표본이 2개 미만이거나 표준편차가 0이면 `FitError`다."""
    if len(samples) < 2:
        raise FitError(f"역할 {role!r}: 표본 {len(samples)}개를 받았다 (2개 이상이어야 한다)")
    mean = math.fsum(samples) / len(samples)
    sigma = math.sqrt(math.fsum((value - mean) ** 2 for value in samples) / len(samples))
    if sigma == 0:
        raise FitError(f"역할 {role!r}: 표준편차가 0이다 (표본 {len(samples)}개가 모두 {mean!r}이다)")
    return RoleParams(mean=mean, sigma=sigma)


def fit_common_model(
    train: Sequence[PlaceRuns],
    validation: Sequence[PlaceRuns],
    config: CommonFitConfig,
    *,
    bin_sec: float,  # Settings.rssi_frame_sec
) -> CommonFitResult:
    """학습 회차로 역할별 값을 적합하고, 검증 회차로 τ·`stay`·`window_sec` 조합을 골라 `CommonModel`을 만든다.

    장소마다(학습과 검증에 나온 모든 장소) 앵커와 자동 영점을 정한다. 자동 영점은 그 장소의 학습·검증 회차 전부로 찾는다.
    역할 표본(`front`, `other`, `none`)은 학습 회차의 보정한 칸 평균이다 (`_role_samples`). 평균과 모표준편차를 `math.fsum`으로 낸다.
    자동 영점이 없는 기기의 영점 범위 Δ(`auto_zero_bound_db`)는 학습 장소마다 찾은 자동 영점 전체의 절댓값 최댓값이다.
    학습 장소에서 찾은 자동 영점이 하나도 없으면(기기가 1~2대인 장소뿐이다) Δ를 정할 수 없어서 `None`(모름)이다. 적합은 실패하지 않는다.
    Δ는 검증 손실 계산에 쓰지 않는다.
    조합은 taus → stays → window_secs 중첩 순으로 재고 (`validation_log_loss`), 손실이 가장 작은 조합을 고른다.
    같으면 먼저 나온 조합이다. 모든 손실이 무한대이면 고를 근거가 없다.
    모델의 `validation`은 `config.validation`에 `validation_loss`(고른 조합의 손실)를 더한 것이다.
    `calibration_method`는 τ가 1.0이면 `none`, 아니면 `temperature`다. 모델 파일은 쓰지 않는다.
    입력이 잘못됐거나 고를 수 없으면 `FitError`다.
    """
    # 설정 검사는 적합보다 먼저 한다.
    if not _is_path_component(config.model_version):
        raise FitError(f"model_version: 경로 구분자가 없는 비어 있지 않은 이름이어야 한다 ({config.model_version!r})")
    taus = _candidates(config.taus, "taus")
    stays = _candidates(config.stays, "stays", upper=1.0)
    window_secs = _candidates(config.window_secs, "window_secs")
    min_bins = config.auto_zero_min_bins
    if isinstance(min_bins, bool) or not isinstance(min_bins, int) or min_bins < 1:
        raise FitError(f"auto_zero_min_bins: 1 이상의 정수여야 한다 ({min_bins!r})")
    bin_sec = _bin_sec(bin_sec)

    if not train:
        raise FitError("학습 회차 묶음이 없다")
    train_by_place = _by_place(train, "학습")
    validation_by_place = _by_place(validation, "검증")
    if not any(group.runs for group in validation):
        raise FitError("검증 회차가 없다")

    # 장소마다 앵커와 자동 영점. 자동 영점은 그 장소의 학습·검증 회차 전부로 찾는다.
    anchors_of: dict[str, frozenset[str]] = {}
    zeros: dict[str, dict[str, float]] = {}
    for place_id in sorted(train_by_place.keys() | validation_by_place.keys()):
        groups = [by_place[place_id] for by_place in (train_by_place, validation_by_place) if place_id in by_place]
        _check_labels(place_id, groups)
        anchors_of[place_id] = place_anchors(*groups)
        zeros[place_id] = place_zeros(groups, bin_sec=bin_sec, min_bins=min_bins)

    samples: dict[str, list[float]] = {"front": [], "other": [], "none": []}
    for place_id, group in train_by_place.items():
        for role, values in _role_samples(group, anchors_of[place_id], zeros[place_id], bin_sec).items():
            samples[role].extend(values)
    front = _role_params(samples["front"], "front")
    other = _role_params(samples["other"], "other")
    none = _role_params(samples["none"], "none")

    # Δ: 학습 장소에서 찾은 자동 영점의 절댓값 최댓값. 공통 값 하나이고 검증 손실에는 쓰지 않는다.
    # 찾은 영점이 하나도 없으면 모른다(None). 그 모델은 자동 영점이 없는 기기의 보수적 사후확률이 0이다.
    found = [abs(zero) for place_id in train_by_place for zero in zeros[place_id].values()]
    bound_db = max(found) if found else None

    models: list[CommonModel] = []
    losses: list[GridLoss] = []
    for tau in taus:
        for stay in stays:
            for window_sec in window_secs:
                model = CommonModel(
                    model_version=config.model_version,
                    window_sec=window_sec,
                    stay=stay,
                    calibration_method="none" if tau == 1.0 else "temperature",
                    calibration_tau=tau,
                    front=front,
                    other=other,
                    none=none,
                    auto_zero_min_bins=min_bins,
                    auto_zero_bound_db=bound_db,
                    validation={},
                )
                models.append(model)
                loss = validation_log_loss(model, validation, zeros, bin_sec=bin_sec)
                losses.append(GridLoss(tau=tau, stay=stay, window_sec=window_sec, loss=loss))

    best = min(range(len(losses)), key=lambda i: losses[i].loss)  # 손실이 같으면 먼저 나온 조합이다
    if math.isinf(losses[best].loss):
        raise FitError(
            f"후보 {len(losses)}개의 검증 손실이 모두 무한대다. 정답 상태의 확률이 어느 조합에서도 0보다 크지 않아 고를 근거가 없다"
        )
    model = replace(models[best], validation={**config.validation, "validation_loss": losses[best].loss})
    return CommonFitResult(model=model, losses=tuple(losses), zeros=zeros)
