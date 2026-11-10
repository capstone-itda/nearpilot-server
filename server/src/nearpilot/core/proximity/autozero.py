"""자동 영점을 찾는 순수 함수 (specs/core-proximity-common-model/spec.md 6절 결정 4).

자동 영점은 한 앵커가 다른 앵커보다 늘 일정하게 약하게 또는 세게 듣는 차이(dB)다. 장소의 관측만으로 찾고 라벨을 쓰지 않는다.
칸 하나에 1등이 아닌 앵커가 둘 이상 있으면, 그 앵커마다 `자기 값 − (1등이 아닌 앵커들 값의 평균)`이 그 앵커의 표본이다.
표본이 모델의 `auto_zero.min_bins` 이상이면 표본의 중앙값이 자동 영점이다.
서버의 근접 추정기(메모리 표본)와 적합·평가·합성(`zeros_from_frames`)이 같은 함수를 쓴다.
현재 시각, 무작위, I/O를 쓰지 않는다.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone

from nearpilot.shared.models import BeaconKey, RssiFrame

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)


def _bin_us(bin_sec: float) -> int:
    """칸 길이를 정수 마이크로초로 센다. `bin_sec`가 0.3처럼 이진 부동소수로 정확하지 않아도 경계의 프레임이 옆 칸으로 가지 않는다."""
    if not 0.0 < bin_sec < math.inf:  # NaN과 무한대도 거부한다
        raise ValueError(f"bin_sec는 0보다 큰 유한한 수여야 한다 ({bin_sec!r})")
    return max(1, round(bin_sec * 1_000_000))


def bin_index(frame_ts: datetime, bin_sec: float) -> int:
    """`frame_ts`가 든 절대 칸의 번호. UTC epoch 마이크로초를 칸 길이(마이크로초)로 나눈 몫(내림)이다.

    칸은 장소나 비콘과 상관없이 시각으로만 정한다. `bin_sec`가 0 이하이면 `ValueError`다.
    """
    return (frame_ts - _EPOCH) // _MICROSECOND // _bin_us(bin_sec)


def bin_samples(values: Mapping[str, float]) -> dict[str, float]:
    """칸 하나의 앵커별 평균 RSSI에서 앵커별 표본을 만든다.

    값이 가장 큰 앵커는 같은 값이 여럿이어도 모두 1등이고, 표본에서도 비교 기준에서도 뺀다 —
    방문자가 그 기기 앞에 있을 가능성이 큰 칸이다. 1등이 아닌 앵커가 둘 이상일 때만 표본이 있다.
    1등이 아닌 앵커 a의 표본은 `values[a] − mean(1등이 아닌 앵커들의 값)`이다 (a 자신을 포함한다).
    그래서 표본은 1등이 아닌 앵커들 안에서 가운데를 0으로 둔 차이이고, 앵커 수와 상관없이 크기가 부풀지 않는다.
    값은 보정하지 않은 평균 그대로 쓴다 (찾은 순서가 결과를 바꾸지 않게 한다).
    """
    if not values:
        return {}
    top = max(values.values())
    rest = sorted(anchor_id for anchor_id, value in values.items() if value != top)
    if len(rest) < 2:
        return {}
    center = math.fsum(values[anchor_id] for anchor_id in rest) / len(rest)
    return {anchor_id: values[anchor_id] - center for anchor_id in rest}


def zero_from_samples(samples: Sequence[float], min_bins: int) -> float | None:
    """표본이 `min_bins`개보다 적으면 None, 아니면 표본의 중앙값이다. 짝수 개면 가운데 두 값의 평균이다.

    `min_bins`는 모델 파일의 `auto_zero.min_bins`이고 1 이상이다.
    """
    if len(samples) < min_bins:
        return None
    return float(statistics.median(samples))


def zeros_from_frames(
    frames: Iterable[RssiFrame], anchors: Collection[str], *, bin_sec: float, min_bins: int,
) -> dict[str, float]:
    """프레임 전체에서 앵커별 자동 영점을 찾는다. 적합, 평가, 합성이 쓰는 오프라인 함수다.

    `anchors`에 있는 기기의 유한한 RSSI 프레임만 쓴다. 같은 `(node_id, session_id, seq, beacon)`은 정렬 순서로 처음 하나만 남긴다.
    `(beacon, 칸 번호)`로 묶어 칸마다 앵커별 평균을 내고, 칸을 `(beacon, 칸 번호)` 순서로 돌며 `bin_samples`의 표본을 앵커별로 모은다.
    표본이 `min_bins` 이상인 앵커만 결과에 든다. 표본이 모자란 앵커는 없다 (보정할 때 0 dB로 읽는다).
    """
    _bin_us(bin_sec)  # 프레임이 없어도 잘못된 칸 길이는 거부한다
    anchor_ids = frozenset(anchors)
    usable = sorted(
        (frame for frame in frames if frame.node_id in anchor_ids and math.isfinite(frame.rssi)),
        # 맨 끝의 rssi는 앞의 값이 모두 같은 프레임의 순서까지 정해서, 입력 순서가 결과를 바꾸지 못하게 한다.
        key=lambda frame: (frame.frame_ts, frame.node_id, frame.session_id, frame.seq, frame.received_at, frame.rssi),
    )

    seen: set[tuple[str, str, int, BeaconKey]] = set()
    grouped: dict[tuple[BeaconKey, int], dict[str, list[float]]] = {}
    for frame in usable:
        key = (frame.node_id, frame.session_id, frame.seq, frame.beacon)
        if key in seen:
            continue
        seen.add(key)
        bin_key = (frame.beacon, bin_index(frame.frame_ts, bin_sec))
        grouped.setdefault(bin_key, {}).setdefault(frame.node_id, []).append(frame.rssi)

    samples: dict[str, list[float]] = {}
    for bin_key in sorted(grouped, key=lambda k: (k[0].uuid, k[0].major, k[0].minor, k[1])):
        means = {anchor_id: math.fsum(values) / len(values) for anchor_id, values in grouped[bin_key].items()}
        for anchor_id, sample in bin_samples(means).items():
            samples.setdefault(anchor_id, []).append(sample)

    zeros: dict[str, float] = {}
    for anchor_id in sorted(samples):
        zero = zero_from_samples(samples[anchor_id], min_bins)
        if zero is not None:
            zeros[anchor_id] = zero
    return zeros
