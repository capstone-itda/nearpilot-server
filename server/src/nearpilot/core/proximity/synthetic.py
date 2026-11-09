"""배치 없는 공통 모델의 합성 수집 회차 생성기 (specs/core-proximity-common-model/spec.md 6절 결정 10).

시드 하나로 정한 `random.Random`만 써서 방 여러 곳과 그 방의 수집 회차를 만든다. 같은 `SyntheticSpec`에는 같은 회차가 나온다.
방마다 기기를 벽 위 무작위 위치에 놓고 기기마다 영점 차이를 뽑는다. 방문자는 기기 앞(벽에서 안쪽으로 `front_m`)이나
모든 기기 앞에서 `none_clearance_m` 이상 떨어진 방 안에 서서, 앵커마다 칸마다 RSSI 프레임 하나를 보고한다.
RSSI는 경로손실 식에 앵커의 영점 차이와 정규분포 잡음을 더한 값이다.
좌표는 방과 회차를 만드는 데만 쓰고 반환값에 넣지 않는다 — 공통 적합은 프레임과 라벨만 본다.
값은 이전 합성 모델(`synthetic-1`)의 값과 그 값을 넓힌 범위다. 공개 데이터나 팀 기기의 값이 아니고, 실측이 아니다.
`make_synthetic_model`은 이 회차를 실측 데이터와 같은 공통 적합 함수(`fit_common_model`)에 넣어 모델을 만든다.

난수는 방마다 차례로 이 순서로 뽑는다: 가로, 세로, 기기 수, (기기마다: 벽 위 위치를 조건이 맞을 때까지, 영점 차이),
그다음 라벨(기기 1…N, `none`)마다 학습 회차들과 검증 회차들 (`none` 회차는 방문자 위치를 먼저, 그다음 프레임마다 앵커 순서로 잡음).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from nearpilot.core.proximity.common_fit import (
    NONE_LABEL,
    CommonFitConfig,
    CommonFitResult,
    LabeledRun,
    PlaceRuns,
    fit_common_model,
)
from nearpilot.shared.models import BeaconKey, RssiFrame

SYNTHETIC_SOURCE: str = "SYNTHETIC_NOT_MEASURED"  # 모델 파일 `validation.source`에 쓰는 합성 표시

_BEACON = BeaconKey("00000000-0000-4000-8000-000000000252", 1, 1)
_FIRST_RUN_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)  # 첫 회차의 시작. 회차마다 `run_bins·bin_sec + _RUN_GAP`씩 늦어진다
_RUN_GAP = timedelta(seconds=10)  # 회차 사이의 빈 시간
_RECEIVE_DELAY = timedelta(milliseconds=50)  # frame_ts에서 received_at까지
_MAX_REDRAWS = 1000  # 조건에 맞는 위치를 다시 뽑는 횟수. 이 횟수를 넘게 실패하면 ValueError다


@dataclass(frozen=True, slots=True)
class SyntheticSpec:
    """합성 생성기의 설정. 기본값이 `synthetic-2`를 만드는 값이다. 모델 파일의 `validation.generator`에 필드 전체가 들어간다."""

    seed: int = 252
    rooms: int = 6
    devices: tuple[int, int] = (3, 7)  # 방마다 기기 수 범위 (양 끝 포함)
    width_m: tuple[float, float] = (4.0, 8.0)
    depth_m: tuple[float, float] = (3.0, 6.0)
    min_gap_m: float = 1.0  # 기기 사이의 최소 거리
    front_m: float = 0.5  # 기기 앞 위치: 벽에서 방 안쪽으로
    none_clearance_m: float = 1.5  # "어느 기기 앞도 아님" 위치가 모든 기기 앞에서 떨어져야 하는 거리
    A: float = -59.0  # 1 m 기준 RSSI, dBm
    n: float = 2.0  # 경로손실 지수
    sigma: float = 4.0  # 프레임 잡음의 표준편차, dB
    min_distance_m: float = 0.5  # 이보다 가까우면 이 거리로 센다
    zero_db: tuple[float, float] = (-6.0, 6.0)  # 기기마다 영점 차이를 고르는 범위, dB
    train_runs: int = 5  # 라벨마다
    validation_runs: int = 2  # 라벨마다
    run_bins: int = 30  # 회차 하나의 칸 수
    taus: tuple[float, ...] = (0.5, 1.0, 2.0)
    stays: tuple[float, ...] = (0.8, 0.9, 0.95)
    window_secs: tuple[float, ...] = (2.5, 3.5, 5.0)
    min_bins: int = 120  # 자동 영점을 찾는 데 필요한 표본 수


@dataclass(frozen=True, slots=True)
class _Anchor:
    node_id: str
    position: tuple[float, float]  # 벽 위의 기기 위치, 미터
    front: tuple[float, float]  # 기기 앞 위치, 미터
    zero_db: float  # 이 기기가 늘 일정하게 세게(+) 또는 약하게(−) 듣는 차이


@dataclass(frozen=True, slots=True)
class _Room:
    place_id: str
    width: float
    depth: float
    anchors: tuple[_Anchor, ...]


def _on_perimeter(t: float, width: float, depth: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """둘레를 따라 t만큼 간 벽 위의 점과 그 벽의 안쪽 단위 방향. 아래 벽의 왼쪽 끝에서 시작해 반시계로 돈다."""
    if t < width:
        return (t, 0.0), (0.0, 1.0)
    t -= width
    if t < depth:
        return (width, t), (-1.0, 0.0)
    t -= depth
    if t < width:
        return (width - t, depth), (0.0, -1.0)
    t -= width
    return (0.0, depth - t), (1.0, 0.0)


def _make_room(rng: random.Random, spec: SyntheticSpec, number: int) -> _Room:
    """방 `number`(1부터)의 크기, 기기 수, 기기 위치와 영점 차이를 뽑는다. 위치는 이미 놓은 기기와 `min_gap_m` 이상 떨어져야 한다."""
    width = rng.uniform(*spec.width_m)
    depth = rng.uniform(*spec.depth_m)
    count = rng.randint(*spec.devices)
    anchors: list[_Anchor] = []
    for i in range(1, count + 1):
        for _ in range(_MAX_REDRAWS + 1):
            position, inward = _on_perimeter(rng.uniform(0.0, 2.0 * (width + depth)), width, depth)
            if all(math.dist(position, other.position) >= spec.min_gap_m for other in anchors):
                break
        else:
            raise ValueError(
                f"방 {number}: 기기 {i}의 자리를 {_MAX_REDRAWS}번 넘게 다시 뽑아도 찾지 못했다 (min_gap_m={spec.min_gap_m})"
            )
        front = (position[0] + inward[0] * spec.front_m, position[1] + inward[1] * spec.front_m)
        anchors.append(_Anchor(f"syn{number}-{i}", position, front, rng.uniform(*spec.zero_db)))
    return _Room(f"syn-room-{number}", width, depth, tuple(anchors))


def _none_position(rng: random.Random, spec: SyntheticSpec, room: _Room) -> tuple[float, float]:
    """"어느 기기 앞도 아님" 회차의 방문자 위치. 방 안에서 모든 기기 앞과 `none_clearance_m` 이상 떨어진 점을 뽑는다."""
    for _ in range(_MAX_REDRAWS + 1):
        point = (rng.uniform(0.0, room.width), rng.uniform(0.0, room.depth))
        if all(math.dist(point, anchor.front) >= spec.none_clearance_m for anchor in room.anchors):
            return point
    raise ValueError(
        f"{room.place_id}: none 회차의 자리를 {_MAX_REDRAWS}번 넘게 다시 뽑아도 찾지 못했다 (none_clearance_m={spec.none_clearance_m})"
    )


def _make_run(
    rng: random.Random,
    spec: SyntheticSpec,
    room: _Room,
    *,
    run_id: str,
    label: str,
    visitor: tuple[float, float],
    started_at: datetime,
    step: timedelta,
    seqs: dict[str, int],
) -> LabeledRun:
    """방문자가 `visitor`에 선 회차 하나. 칸 k의 프레임은 `started_at + (k + 0.5)·step`에 앵커마다 하나씩 있다."""
    frames: list[RssiFrame] = []
    for k in range(spec.run_bins):
        frame_ts = started_at + step * (k + 0.5)
        for anchor in room.anchors:
            distance = max(math.dist(visitor, anchor.position), spec.min_distance_m)
            rssi = spec.A - 10.0 * spec.n * math.log10(distance) + anchor.zero_db + rng.gauss(0.0, spec.sigma)
            seqs[anchor.node_id] += 1
            frames.append(
                RssiFrame(
                    node_id=anchor.node_id,
                    beacon=_BEACON,
                    rssi=rssi,
                    frame_ts=frame_ts,
                    session_id=f"{anchor.node_id}-s1",
                    seq=seqs[anchor.node_id],
                    sent_at=frame_ts,
                    received_at=frame_ts + _RECEIVE_DELAY,
                )
            )
    return LabeledRun(
        run_id=run_id, label=label, beacon=_BEACON, started_at=started_at, ended_at=started_at + step * spec.run_bins,
        frames=tuple(frames),
    )


def generate_places(spec: SyntheticSpec, *, bin_sec: float) -> tuple[list[PlaceRuns], list[PlaceRuns]]:
    """방마다 학습 회차 묶음과 검증 회차 묶음을 만든다. 반환값은 (학습, 검증)이고, 두 목록의 i번째가 같은 방이다.

    방 r(1부터)의 장소 ID는 `syn-room-{r}`, 기기 ID는 `syn{r}-{i}`다. 라벨마다(기기 앞, `none`) 학습 회차 `train_runs`개와
    검증 회차 `validation_runs`개를 만든다. 회차 ID는 `{장소}-{라벨}-{train|validation}-{번호}`다.
    기기 앞 회차의 방문자는 그 기기의 앞 위치에 서고, `none` 회차의 방문자는 회차마다 방 안의 무작위 위치에 선다.
    기기마다 `seq`는 1부터 모든 회차에 걸쳐 늘어난다. 회차의 시작은 모든 방에 걸쳐 `2026-01-01T00:00:00Z`부터
    회차마다 `run_bins·bin_sec + 10초`씩 늦어진다. 좌표는 반환값에 넣지 않는다.
    """
    rng = random.Random(spec.seed)
    step = timedelta(seconds=bin_sec)
    span = step * spec.run_bins + _RUN_GAP  # 회차 하나가 차지하는 시간
    seqs: dict[str, int] = defaultdict(int)
    run_count = 0

    train: list[PlaceRuns] = []
    validation: list[PlaceRuns] = []
    for number in range(1, spec.rooms + 1):
        room = _make_room(rng, spec, number)
        fronts = {anchor.node_id: anchor.front for anchor in room.anchors}
        by_kind: dict[str, list[LabeledRun]] = {"train": [], "validation": []}
        for label in (*fronts, NONE_LABEL):
            for kind, count in (("train", spec.train_runs), ("validation", spec.validation_runs)):
                for i in range(1, count + 1):
                    visitor = _none_position(rng, spec, room) if label == NONE_LABEL else fronts[label]
                    by_kind[kind].append(
                        _make_run(
                            rng, spec, room,
                            run_id=f"{room.place_id}-{label}-{kind}-{i}",
                            label=label,
                            visitor=visitor,
                            started_at=_FIRST_RUN_AT + span * run_count,
                            step=step,
                            seqs=seqs,
                        )
                    )
                    run_count += 1
        train.append(PlaceRuns(room.place_id, by_kind["train"]))
        validation.append(PlaceRuns(room.place_id, by_kind["validation"]))
    return train, validation


def _generator_fields(spec: SyntheticSpec) -> dict[str, Any]:
    """`SyntheticSpec`의 필드 전체. 모델 파일의 JSON에 쓰므로 tuple은 list로 바꾼다."""
    return {name: list(value) if isinstance(value, tuple) else value for name, value in asdict(spec).items()}


def make_synthetic_model(
    model_version: str, spec: SyntheticSpec = SyntheticSpec(), *, bin_sec: float,
) -> CommonFitResult:
    """`generate_places`의 회차로 실측과 같은 `fit_common_model`을 불러 합성 공통 모델을 만든다. 모델 파일은 쓰지 않는다.

    모델의 `validation`에 합성 표시(`source`, `quality_verified: false`)와 생성기 설정 전체(`generator`)가 들어가고,
    적합이 고른 조합의 검증 손실(`validation_loss`)이 더해진다. 배치는 쓰지 않는다.
    """
    train, validation = generate_places(spec, bin_sec=bin_sec)
    config = CommonFitConfig(
        model_version=model_version,
        taus=spec.taus,
        stays=spec.stays,
        window_secs=spec.window_secs,
        auto_zero_min_bins=spec.min_bins,
        validation={"source": SYNTHETIC_SOURCE, "quality_verified": False, "generator": _generator_fields(spec)},
    )
    return fit_common_model(train, validation, config, bin_sec=bin_sec)
