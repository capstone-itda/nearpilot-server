"""NFR-02 합성 경로 생성기 (specs/core-proximity-estimator/spec.md 8절).

경로를 따라 기기 앞에 서 있는 방문자가 앵커마다 1초에 한 번 보고하는 RSSI 프레임을 만든다.
평균은 모델의 경로손실 식이고 잡음은 정규분포이며 프레임은 확률로 빠진다. 난수는 시드 하나로 정해져서
같은 인자에는 같은 프레임이 나온다. 모델 파일처럼 테스트용 합성 값이다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy

from nearpilot.core.proximity import ProximityModel
from nearpilot.shared.models import BeaconKey, RssiFrame

from .conftest import BEACON, T0, make_frame
from .test_filter import front_rssi

ANCHOR_SPACING = timedelta(milliseconds=10)  # 앵커 순번이 하나 늘 때마다 프레임 시각이 늦어지는 간격
FRAME_OFFSET = timedelta(milliseconds=500)  # 초가 시작하고 첫 프레임이 찍히기까지의 시간
RECEIVE_DELAY = timedelta(milliseconds=50)  # frame_ts에서 received_at까지


@dataclass(frozen=True)
class Segment:
    """경로의 한 구간. 방문자는 seconds초 동안 node_id 기기 앞에 서 있다."""

    node_id: str
    seconds: int  # 정수 초


def generate_frames(
    model: ProximityModel,
    path: Sequence[Segment],
    *,
    seed: int,
    noise_sigma: float,
    drop_prob: float,
    start: datetime = T0,
    beacon: BeaconKey = BEACON,
) -> list[RssiFrame]:
    """path를 따라 서 있는 방문자의 RSSI 프레임을 시각 순서로 돌려준다.

    경로 전체의 초 t(0부터)마다 앵커를 `sorted(model.nodes)` 순서로 프레임 하나씩 만든다.
    평균 RSSI는 그 초에 서 있는 기기 앞 위치에서 앵커까지의 경로손실 식이고, 잡음은 N(0, noise_sigma)다.
    각 프레임은 확률 drop_prob로 빠진다. 잡음과 탈락 난수는 프레임마다 항상 뽑아서,
    프레임이 빠져도 다른 프레임의 값이 바뀌지 않는다.
    """
    rng = numpy.random.default_rng(seed)
    anchor_ids = sorted(model.nodes)
    standing = [segment.node_id for segment in path for _ in range(segment.seconds)]  # 초 t에 서 있는 기기

    frames = []
    for t, device_id in enumerate(standing):
        for order, anchor_id in enumerate(anchor_ids):
            noise = float(rng.normal(0.0, noise_sigma))
            dropped = rng.random() < drop_prob
            if dropped:
                continue
            frame_ts = start + timedelta(seconds=t) + order * ANCHOR_SPACING + FRAME_OFFSET
            frames.append(
                make_frame(
                    anchor_id,
                    front_rssi(model, anchor_id, device_id) + noise,
                    frame_ts,
                    beacon=beacon,
                    seq=t + 1,  # 앵커별 순번이다. 빠진 프레임의 순번은 비워 둔다
                    received_at=frame_ts + RECEIVE_DELAY,
                )
            )
    return frames
