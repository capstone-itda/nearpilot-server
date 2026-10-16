"""적합용 합성 수집 회차 생성기 (specs/core-proximity-model-fit/spec.md 8절).

`synthetic.py`와 같은 방식으로 수집 회차 하나의 `LabeledRun`을 만든다. 회차는 라벨 하나(기기 앞 또는 `none`)이고,
앵커마다 1초에 한 번 보고한다. 평균은 모델의 경로손실 식(`none`이면 `none_rssi`)이고 잡음은 정규분포이며 프레임은 확률로 빠진다.
회차마다 앵커별 오프셋을 한 번 뽑아 모든 프레임에 더할 수 있다 — 같은 자리에 선 회차의 위치 오차를 흉내 낸다.
난수는 시드 하나로 정해져서 같은 인자에는 같은 회차가 나온다. 모델 파일처럼 테스트용 합성 값이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy

from nearpilot.core.proximity import NONE_LABEL, AnchorLayout, LabeledRun, PlaceLayout, ProximityModel
from nearpilot.shared.models import BeaconKey

from .conftest import BEACON, make_frame
from .synthetic import ANCHOR_SPACING, FRAME_OFFSET, RECEIVE_DELAY
from .test_filter import front_rssi


def layout_of(model: ProximityModel) -> PlaceLayout:
    """모델의 배치와 `none_points`를 옮긴다. 모델의 `A`, `n` 같은 관측 값은 옮기지 않는다."""
    return PlaceLayout(
        place_id=model.place_id,
        nodes={
            node_id: AnchorLayout(x=node.x, y=node.y, front_x=node.front_x, front_y=node.front_y)
            for node_id, node in model.nodes.items()
        },
        none_points=model.none_points,
    )


def make_run(
    model: ProximityModel,
    label: str,
    *,
    run_id: str,
    seconds: int,
    start: datetime,
    seed: int,
    noise_sigma: float,
    run_offset_sigma: float = 0.0,
    drop_prob: float = 0.0,
    beacon: BeaconKey = BEACON,
) -> LabeledRun:
    """label(node_id 또는 `NONE_LABEL`)에 seconds초 동안 서 있는 방문자의 수집 회차를 만든다.

    초 t(0부터)마다 앵커를 `sorted(model.nodes)` 순서로 프레임 하나씩 만든다. 평균 RSSI는 `front_rssi`와 같다.
    RSSI는 평균에 앵커별 회차 오프셋 N(0, run_offset_sigma)과 프레임별 잡음 N(0, noise_sigma)를 더한 값이다.
    각 프레임은 확률 drop_prob로 빠진다. 난수는 오프셋(앵커 순서), 그다음 프레임마다 잡음과 탈락 순서로 뽑는다.
    잡음과 탈락 난수는 프레임마다 항상 뽑아서, 프레임이 빠져도 다른 프레임의 값이 바뀌지 않는다.
    """
    rng = numpy.random.default_rng(seed)
    anchor_ids = sorted(model.nodes)
    device_id = None if label == NONE_LABEL else label
    offsets = {anchor_id: float(rng.normal(0.0, run_offset_sigma)) for anchor_id in anchor_ids}

    frames = []
    for t in range(seconds):
        for order, anchor_id in enumerate(anchor_ids):
            noise = float(rng.normal(0.0, noise_sigma))
            dropped = rng.random() < drop_prob
            if dropped:
                continue
            frame_ts = start + timedelta(seconds=t) + order * ANCHOR_SPACING + FRAME_OFFSET
            frames.append(
                make_frame(
                    anchor_id,
                    front_rssi(model, anchor_id, device_id) + offsets[anchor_id] + noise,
                    frame_ts,
                    beacon=beacon,
                    seq=t + 1,  # 앵커별 순번이다. 빠진 프레임의 순번은 비워 둔다
                    received_at=frame_ts + RECEIVE_DELAY,
                )
            )
    return LabeledRun(
        run_id=run_id,
        label=label,
        beacon=beacon,
        started_at=start,
        ended_at=start + timedelta(seconds=seconds),
        frames=tuple(frames),
    )
