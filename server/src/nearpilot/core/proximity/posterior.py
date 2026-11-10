"""공통 모델의 관측 구간 사후확률 계산 (specs/core-proximity-common-model/spec.md 6절 결정 1~3).

프레임 목록, 공통 모델, 비콘, 후보, 장소 전체 기기, 시각, 근접 참여 기기, 자동 영점만으로 `ProximityResult`를 만드는 순수 함수다.
판정, 재현, 평가가 모두 이 함수를 쓴다. 메모리 상태, 현재 시각, 무작위를 쓰지 않는다.
`(at − window_sec, at]`의 프레임으로 처음 분포부터 HMM forward를 매번 다시 돌린다.
상태는 근접 참여 기기 앞 상태들과 "어느 기기 앞도 아님"이다. 관측은 자동 영점으로 보정한 칸 평균 RSSI이고,
기기 i 앞 상태에서 앵커 i는 `front`, 다른 앵커는 `other`, "어느 기기 앞도 아님"에서는 모든 앵커가 `none`을 따른다.
보정은 우도 온도다. 칸마다 증거의 로그우도를 `model.calibration_tau`로 나눈다.
자동 영점이 없는 후보(`unzeroed`)는 영점 0 dB로 계산한 사후확률(`posteriors`) 말고 보수적 사후확률(`conservative`)도 낸다.
영점이 `±model.auto_zero_bound_db`(Δ) 안의 어느 값일 수도 있다고 보고, 그 기기에 가장 불리한 영점으로 같은 계산을 한 번 더 한다.
Δ를 모르는 모델(`None`)은 다시 계산하지 않고 보수적 사후확률을 0으로 둔다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from nearpilot.core.proximity.common_model import CommonModel, RoleParams
from nearpilot.shared.enums import TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, ProximityResult, RssiFrame

NONE_STATE: str = "__none__"  # "어느 기기 앞도 아님" 상태의 내부 이름. 결과의 키로 내보내지 않는다

_HALF_LOG_2PI = 0.5 * math.log(2.0 * math.pi)


class ProximityInputError(ValueError):
    """판정이 넘긴 입력의 관계가 맞지 않는다 (장소, 후보, 비콘)."""


def _check_inputs(candidates: Sequence[NodeInfo], place_nodes: Sequence[NodeInfo], bin_sec: float) -> None:
    """입력끼리 맞는지 본다. 맞지 않으면 `ProximityInputError`.

    공통 모델에는 장소가 없어서 `place_nodes`의 장소를 모델과 비교하지 않는다.
    """
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
        # place_nodes와 후보의 신뢰 상태가 어긋난 경우다. 어느 쪽이 맞는지 알 수 없어서 거부한다.
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


def _log_normal(x: float, role: RoleParams) -> float:
    z = (x - role.mean) / role.sigma
    return -0.5 * z * z - math.log(role.sigma) - _HALF_LOG_2PI


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


def _anchor_probabilities(
    model: CommonModel,
    anchor_ids: Sequence[str],
    observed: Mapping[int, Mapping[str, float]],
    n_bins: int,
    zeros: Mapping[str, float],
) -> dict[str, float]:
    """칸 평균 RSSI(`observed`, 보정 전)를 `zeros`로 보정해 처음 분포부터 HMM forward를 돌리고, 앵커마다 그 기기 앞 상태의 확률을 낸다.

    상태는 `anchor_ids`의 순서대로 기기 앞 상태들이고 마지막이 `NONE_STATE`다. `zeros`에 없는 앵커는 0 dB로 보정한다.
    `NONE_STATE`의 확률은 결과에 넣지 않는다.
    """
    k_states = len(anchor_ids) + 1
    log_transition = _log_transition(model.stay, k_states)

    belief = [math.log(1.0 / k_states)] * k_states  # 처음 분포: 균등
    for k in range(n_bins):
        belief = [_logsumexp([b + row[j] for b, row in zip(belief, log_transition)]) for j in range(k_states)]
        # 값이 없는 앵커는 증거로 쓰지 않는다. 증거가 하나도 없는 칸은 예측만 한다.
        evidence = [(anchor_id, rssi - zeros.get(anchor_id, 0.0)) for anchor_id, rssi in sorted(observed.get(k, {}).items())]
        if evidence:
            # 기기 i 앞: 앵커 i는 front, 다른 앵커는 other. i가 이 칸에 없으면 other 항만 남는다.
            front_log = [
                math.fsum(
                    _log_normal(r, model.front if anchor_id == device_id else model.other) for anchor_id, r in evidence
                )
                for device_id in anchor_ids
            ]
            none_log = math.fsum(_log_normal(r, model.none) for _, r in evidence)
            # 보정: 증거의 로그우도를 τ로 나눠 더한다. "none"은 τ = 1.0이라 나눠도 값이 그대로다.
            belief = [b + e / model.calibration_tau for b, e in zip(belief, [*front_log, none_log])]
        total = _logsumexp(belief)
        belief = [b - total for b in belief]

    # 앵커 상태에서만 읽는다. 마지막 NONE_STATE의 확률은 zip이 버린다.
    return dict(zip(anchor_ids, (math.exp(b) for b in belief)))


def compute_posteriors(
    model: CommonModel,
    frames: Iterable[RssiFrame],
    beacon: BeaconKey,
    candidates: Sequence[NodeInfo],
    place_nodes: Sequence[NodeInfo],
    at: datetime,
    *,
    bin_sec: float,
    participants: Collection[str],
    zeros: Mapping[str, float],
    calib_version: str,
) -> ProximityResult:
    """`at`에서 후보마다 "그 기기 앞" 상태의 사후확률을 계산한다.

    상태 앵커는 `participants` 중 `place_nodes`의 trusted 기기다. `participants`에 다른 기기가 있어도 오류가 아니고 무시한다.
    확률은 상태 전체(상태 앵커 앞 상태들과 `NONE_STATE`)로 계산하고, 후보끼리 다시 정규화하지 않는다.
    `posteriors`에는 상태 앵커인 후보만 든다. `unzeroed`는 그중 `zeros`에 없는 기기다.
    `zeros`는 기기별 자동 영점(dB)이고, 없는 기기는 0 dB로 보정한다. `calib_version`은 결과에 그대로 적는다.
    `conservative`는 `unzeroed` 기기 i마다 한 번 더 계산한 보수적 사후확률이다. 자동 영점이 있는 앵커는 그대로 두고,
    i는 영점 `+Δ`, i가 아닌 자동 영점 없는 상태 앵커(후보가 아니어도)는 모두 `−Δ`로 보정해 같은 HMM·관측 구간·τ로 계산한다
    (Δ는 `model.auto_zero_bound_db`). i가 실제로 세게 듣는 기기이고 다른 기기들은 약하게 듣는다고 보는 쪽이 i에게 가장 불리하다.
    값은 `min(posteriors[i], 그 계산의 i 확률)`이라 `posteriors[i]` 이하이고, Δ가 0이면 같다.
    Δ를 모르면(`model.auto_zero_bound_db`가 `None`) 영점이 어느 값일지 알 수 없어서 다시 계산하지 않고 0.0이다 (어떤 임계값도 넘지 못한다).
    입력 관계가 맞지 않으면 `ProximityInputError`다.
    """
    _check_inputs(candidates, place_nodes, bin_sec)

    trusted_ids = {node.node_id for node in place_nodes if node.trust_state == TrustState.TRUSTED}
    anchor_ids = sorted(trusted_ids.intersection(participants))

    window_start = at - timedelta(seconds=model.window_sec)
    selected = _select_frames(frames, beacon, frozenset(anchor_ids), window_start, at)
    n_bins = max(1, round(model.window_sec / bin_sec))
    observed = _mean_rssi_by_bin(selected, window_start, bin_sec, n_bins)

    # 후보의 확률은 앵커 상태에서만 읽는다. NONE_STATE의 확률은 어느 후보에도 더하지 않는다.
    probability = _anchor_probabilities(model, anchor_ids, observed, n_bins, zeros)
    posteriors = {
        candidate.node_id: probability[candidate.node_id] for candidate in candidates if candidate.node_id in probability
    }
    unzeroed = frozenset(node_id for node_id in posteriors if node_id not in zeros)

    bound = model.auto_zero_bound_db
    conservative: dict[str, float] = {}
    for device_id in sorted(unzeroed):
        if bound is None:  # Δ를 모른다. 다시 계산하지 않고 어떤 임계값도 넘지 못하게 0으로 둔다
            conservative[device_id] = 0.0
            continue
        worst = dict(zeros)
        for anchor_id in anchor_ids:
            if anchor_id not in zeros:
                worst[anchor_id] = bound if anchor_id == device_id else -bound
        worst_probability = _anchor_probabilities(model, anchor_ids, observed, n_bins, worst)[device_id]
        conservative[device_id] = min(posteriors[device_id], worst_probability)

    return ProximityResult(
        posteriors=posteriors,
        window_start=window_start,
        window_end=at,
        model_version=model.model_version,
        calib_version=calib_version,
        unzeroed=unzeroed,
        conservative=conservative,
    )
