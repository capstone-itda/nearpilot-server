"""관측 구간 사후확률 계산 (specs/core-proximity-estimator/spec.md 6절).

프레임 목록, 모델, 비콘, 후보, 장소 전체 기기, 시각만으로 `ProximityResult`를 만드는 순수 함수다.
판정, 재현, 평가가 모두 이 함수를 쓴다. 메모리 상태, 현재 시각, 무작위를 쓰지 않는다.
`(at − window_sec, at]`의 프레임으로 처음 분포부터 HMM forward를 매번 다시 돌린다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from nearpilot.core.proximity.model import ModelMismatchError, ProximityModel
from nearpilot.shared.enums import TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, ProximityResult, RssiFrame

NONE_STATE: str = "__none__"  # "어느 기기 앞도 아님" 상태의 내부 이름. 결과의 키로 내보내지 않는다

_HALF_LOG_2PI = 0.5 * math.log(2.0 * math.pi)


class ProximityInputError(ValueError):
    """판정이 넘긴 입력의 관계가 맞지 않는다 (장소, 후보, 비콘)."""


def _check_inputs(
    model: ProximityModel,
    candidates: Sequence[NodeInfo],
    place_nodes: Sequence[NodeInfo],
    bin_sec: float,
) -> None:
    """입력끼리 맞는지 본다. 맞지 않으면 `ProximityInputError`."""
    if not bin_sec > 0:  # NaN도 거부한다
        raise ProximityInputError(f"bin_sec는 0보다 커야 한다 ({bin_sec!r})")
    if not place_nodes:
        raise ProximityInputError("place_nodes가 비어 있다")
    place_ids = {node.place_id for node in place_nodes}
    if len(place_ids) != 1 or None in place_ids:
        raise ProximityInputError(
            f"place_nodes는 place_id가 있는 한 장소의 기기여야 한다 (place_id={sorted(map(str, place_ids))})"
        )
    (place_id,) = place_ids
    if place_id != model.place_id:
        raise ProximityInputError(
            f"place_nodes의 장소가 모델의 장소와 다르다 (place_nodes={place_id!r}, 모델={model.place_id!r})"
        )
    repeated = sorted(node_id for node_id, count in Counter(node.node_id for node in place_nodes).items() if count > 1)
    if repeated:
        raise ProximityInputError(f"place_nodes에 같은 node_id가 둘 이상 있다: {repeated}")

    listed = {node.node_id: node for node in place_nodes}
    for candidate in candidates:
        if candidate.node_id not in listed:
            raise ProximityInputError(f"후보 {candidate.node_id!r}가 place_nodes에 없다")
        if candidate.place_id != place_id:
            raise ProximityInputError(
                f"후보 {candidate.node_id!r}의 장소가 place_nodes와 다르다 (후보={candidate.place_id!r}, place_nodes={place_id!r})"
            )
        if candidate.trust_state != TrustState.TRUSTED:
            raise ProximityInputError(f"후보의 trust_state가 trusted가 아니다 ({candidate.node_id!r}: {candidate.trust_state})")
        # 후보가 상태 목록에 없으면 확률을 읽을 수 없다. place_nodes와 후보의 신뢰 상태가 어긋난 경우다.
        if listed[candidate.node_id].trust_state != TrustState.TRUSTED:
            raise ProximityInputError(
                f"place_nodes에서는 trusted가 아닌 기기가 후보다 ({candidate.node_id!r}: {listed[candidate.node_id].trust_state})"
            )


def _select_frames(
    frames: Iterable[RssiFrame],
    beacon: BeaconKey,
    anchor_ids: frozenset[str],
    window_start: datetime,
    at: datetime,
) -> list[RssiFrame]:
    """쓸 프레임을 고른다. 같은 `(node_id, session_id, seq, beacon)`은 정렬 순서로 처음 하나만 남긴다."""
    usable = sorted(
        (
            frame
            for frame in frames
            if frame.beacon == beacon
            and frame.node_id in anchor_ids
            and math.isfinite(frame.rssi)  # 유한하지 않은 RSSI는 결측과 같이 버린다
            and window_start < frame.frame_ts <= at
            and frame.received_at <= at
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


def _mean_rssi_by_bin(
    selected: Sequence[RssiFrame], window_start: datetime, bin_sec: float, n_bins: int,
) -> dict[int, dict[str, float]]:
    """시간 칸 번호 → 앵커 → 평균 RSSI. 칸은 `(시작, 끝]`이고 범위 밖 번호는 양 끝 칸으로 자른다."""
    grouped: dict[int, dict[str, list[float]]] = {}
    # 칸 번호는 정수 마이크로초로 센다. bin_sec가 0.3처럼 이진 부동소수로 정확하지 않아도 경계의 프레임이 옆 칸으로 가지 않는다.
    bin_us = max(1, round(bin_sec * 1_000_000))
    for frame in selected:
        elapsed_us = (frame.frame_ts - window_start) // timedelta(microseconds=1)
        k = -(-elapsed_us // bin_us) - 1  # ceil(elapsed_us / bin_us) - 1
        k = min(max(k, 0), n_bins - 1)
        grouped.setdefault(k, {}).setdefault(frame.node_id, []).append(frame.rssi)
    return {
        k: {anchor_id: math.fsum(values) / len(values) for anchor_id, values in by_anchor.items()}
        for k, by_anchor in grouped.items()
    }


def _path_loss_mean(model: ProximityModel, anchor_id: str, x: float, y: float) -> float:
    """(x, y)에 선 방문자를 앵커가 받을 평균 RSSI. 경로손실 식이다."""
    anchor = model.nodes[anchor_id]
    distance = max(math.dist((anchor.x, anchor.y), (x, y)), model.min_distance_m)
    return anchor.A - 10.0 * anchor.n * math.log10(distance)


def _front_params(model: ProximityModel, anchor_id: str, trusted_ids: Sequence[str]) -> list[tuple[float, float]]:
    """앵커 하나가 기기 앞 상태마다 기대하는 RSSI의 (평균, 표준편차). 순서는 trusted_ids와 같다."""
    sigma = model.nodes[anchor_id].sigma
    return [
        (_path_loss_mean(model, anchor_id, model.nodes[device_id].front_x, model.nodes[device_id].front_y), sigma)
        for device_id in trusted_ids
    ]


def _none_params(model: ProximityModel, anchor_id: str) -> list[tuple[float, float]]:
    """앵커 하나가 "어느 기기 앞도 아님"의 성분마다 기대하는 RSSI의 (평균, 표준편차).

    첫 성분은 장소에서 먼 위치(`none_rssi`, `none_sigma`)이고, 나머지는 `none_points`의 위치마다 경로손실 식이다.
    """
    anchor = model.nodes[anchor_id]
    return [(anchor.none_rssi, anchor.none_sigma)] + [
        (_path_loss_mean(model, anchor_id, x, y), anchor.sigma) for x, y in model.none_points
    ]


def _log_normal(x: float, mean: float, sigma: float) -> float:
    z = (x - mean) / sigma
    return -0.5 * z * z - math.log(sigma) - _HALF_LOG_2PI


def _logsumexp(values: Sequence[float]) -> float:
    peak = max(values)
    return peak + math.log(math.fsum(math.exp(value - peak) for value in values))


def _log_transition(stay: float, k_states: int) -> list[list[float]]:
    """행 i, 열 j는 log P(i→j)다. 같은 상태에 머무를 확률이 stay이고 나머지는 다른 상태에 똑같이 나눈다."""
    if k_states == 1:
        return [[0.0]]  # 상태가 하나면 전이는 항등이다
    log_stay = math.log(stay)
    log_move = math.log((1.0 - stay) / (k_states - 1))
    return [[log_stay if i == j else log_move for j in range(k_states)] for i in range(k_states)]


def compute_posteriors(
    model: ProximityModel,
    frames: Iterable[RssiFrame],
    beacon: BeaconKey,
    candidates: Sequence[NodeInfo],
    place_nodes: Sequence[NodeInfo],
    at: datetime,
    *,
    bin_sec: float,
) -> ProximityResult:
    """`at`에서 후보마다 "그 기기 앞" 상태의 사후확률을 계산한다.

    확률은 장소 전체 상태(trusted 기기 앞 상태들과 `NONE_STATE`)로 계산하고, 후보끼리 다시 정규화하지 않는다.
    입력 관계가 맞지 않으면 `ProximityInputError`, trusted 기기가 모델에 없으면 `ModelMismatchError`.
    """
    _check_inputs(model, candidates, place_nodes, bin_sec)

    # 모델에는 있지만 trusted가 아닌 기기는 상태와 앵커에서 뺀다. 오류가 아니다.
    trusted_ids = sorted(node.node_id for node in place_nodes if node.trust_state == TrustState.TRUSTED)
    missing = [node_id for node_id in trusted_ids if node_id not in model.nodes]
    if missing:
        raise ModelMismatchError(f"모델 {model.model_version!r}에 없는 trusted 기기가 있다: {missing}")
    states = [*trusted_ids, NONE_STATE]
    k_states = len(states)

    window_start = at - timedelta(seconds=model.window_sec)
    selected = _select_frames(frames, beacon, frozenset(trusted_ids), window_start, at)
    n_bins = max(1, round(model.window_sec / bin_sec))
    observed = _mean_rssi_by_bin(selected, window_start, bin_sec, n_bins)
    fronts = {anchor_id: _front_params(model, anchor_id, trusted_ids) for anchor_id in trusted_ids}
    nones = {anchor_id: _none_params(model, anchor_id) for anchor_id in trusted_ids}
    log_n_components = math.log(1 + len(model.none_points))
    log_transition = _log_transition(model.stay, k_states)

    belief = [math.log(1.0 / k_states)] * k_states  # 처음 분포: 균등
    for k in range(n_bins):
        belief = [_logsumexp([b + row[j] for b, row in zip(belief, log_transition)]) for j in range(k_states)]
        # 값이 없는 앵커는 증거로 쓰지 않는다. 프레임이 없는 칸은 예측만 한다.
        evidence = sorted(observed.get(k, {}).items())
        if evidence:
            front_log = [
                math.fsum(_log_normal(rssi, *fronts[anchor_id][s]) for anchor_id, rssi in evidence)
                for s in range(len(trusted_ids))
            ]
            # "어느 기기 앞도 아님"은 성분들의 같은 비중 혼합이다. 칸마다 앵커들의 우도를 성분별로 곱한 뒤 평균한다.
            none_log = _logsumexp([
                math.fsum(_log_normal(rssi, *nones[anchor_id][c]) for anchor_id, rssi in evidence)
                for c in range(len(model.none_points) + 1)
            ]) - log_n_components
            belief = [b + e for b, e in zip(belief, [*front_log, none_log])]
        total = _logsumexp(belief)
        belief = [b - total for b in belief]

    # 보정: 모델 파일이 지원하는 방법은 "none"뿐이다 (`load_model`이 다른 값을 거부한다). 확률을 그대로 쓴다.
    probability = {state: math.exp(b) for state, b in zip(states, belief)}
    return ProximityResult(
        posteriors={candidate.node_id: probability[candidate.node_id] for candidate in candidates},
        window_start=window_start,
        window_end=at,
        model_version=model.model_version,
        calib_version=model.calib_version,
    )
