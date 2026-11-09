"""NFR-02 합성 경로 생성기 (specs/core-proximity-common-model/spec.md, NFR-02 시험).

경로를 따라 기기 앞에 서 있는 방문자가 앵커마다 1초에 한 번 보고하는 RSSI 프레임을 만든다.
공통 모델의 관측 식을 그대로 쓴다. 서 있는 기기의 앵커는 `front`, 다른 앵커는 `other`의 평균을 따르고,
잡음은 각 역할의 `sigma`를 표준편차로 하는 정규분포이며 프레임은 확률로 빠진다. 난수는 시드 하나로 정해져서
같은 인자에는 같은 프레임이 나온다. 모델 파일처럼 테스트용 합성 값이다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy

from nearpilot.core.proximity.common_model import CommonModel
from nearpilot.shared.models import BeaconKey, RssiFrame

from .conftest import BEACON, PLACE_A_NODES, T0, make_frame

ANCHOR_SPACING = timedelta(milliseconds=10)  # 앵커 순번이 하나 늘 때마다 프레임 시각이 늦어지는 간격
FRAME_OFFSET = timedelta(milliseconds=500)  # 초가 시작하고 첫 프레임이 찍히기까지의 시간
RECEIVE_DELAY = timedelta(milliseconds=50)  # frame_ts에서 received_at까지


@dataclass(frozen=True)
class Segment:
    """경로의 한 구간. 방문자는 seconds초 동안 node_id 기기 앞에 서 있다."""

    node_id: str
    seconds: int  # 정수 초


def generate_frames(
    model: CommonModel,
    path: Sequence[Segment],
    *,
    seed: int,
    drop_prob: float,
    noise_scale: float = 1.0,
    start: datetime = T0,
    beacon: BeaconKey = BEACON,
) -> list[RssiFrame]:
    """path를 따라 서 있는 방문자의 RSSI 프레임을 시각 순서로 돌려준다.

    경로 전체의 초 t(0부터)마다 place-a의 앵커(`PLACE_A_NODES`)를 `sorted` 순서로 프레임 하나씩 만든다.
    그 초에 서 있는 기기의 앵커는 평균 `model.front.mean`, 다른 앵커는 `model.other.mean`이고,
    잡음은 N(0, 그 역할의 sigma × noise_scale)이다. noise_scale 0은 평균 그대로의 프레임이다.
    각 프레임은 확률 drop_prob로 빠진다. 잡음과 탈락 난수는 프레임마다 항상 뽑아서,
    프레임이 빠져도 다른 프레임의 값이 바뀌지 않는다.
    """
    rng = numpy.random.default_rng(seed)
    anchors = sorted(PLACE_A_NODES)
    standing = [segment.node_id for segment in path for _ in range(segment.seconds)]  # 초 t에 서 있는 기기

    frames = []
    for t, device_id in enumerate(standing):
        for order, anchor_id in enumerate(anchors):
            role = model.front if anchor_id == device_id else model.other
            noise = float(rng.normal(0.0, role.sigma * noise_scale))
            dropped = rng.random() < drop_prob
            if dropped:
                continue
            frame_ts = start + timedelta(seconds=t) + order * ANCHOR_SPACING + FRAME_OFFSET
            frames.append(
                make_frame(
                    anchor_id,
                    role.mean + noise,
                    frame_ts,
                    beacon=beacon,
                    seq=t + 1,  # 앵커별 순번이다. 빠진 프레임의 순번은 비워 둔다
                    received_at=frame_ts + RECEIVE_DELAY,
                )
            )
    return frames
