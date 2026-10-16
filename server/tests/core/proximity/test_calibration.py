"""우도 온도 보정 (FR-16). `compute_posteriors`가 칸마다 증거의 로그우도를 τ로 나누는지 합성 모델로 확인한다."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from nearpilot.core.proximity import ProximityModel
from nearpilot.shared.models import RssiFrame

from .test_filter import AT, MODEL_A, MODEL_B, NODES_B, NOISE, close, compute, frame_at, normal_pdf, standing_frames

TAUS = (1.0, 2.0, 4.0)


def with_tau(tau: float, model: ProximityModel = MODEL_A) -> ProximityModel:
    """model의 보정을 우도 온도 tau로 바꾼다."""
    return replace(model, calibration_method="temperature", calibration_tau=tau)


def noisy_moving_frames() -> list[RssiFrame]:
    """locker-2 앞에 6초 있다가 light-1 앞으로 옮긴 방문자의 프레임. 손으로 고른 오차를 더해서 어느 기대값과도 다르다."""
    frames = standing_frames(MODEL_A, "locker-2", count=6, last_ts=AT - timedelta(seconds=4)) + standing_frames(
        MODEL_A, "light-1", count=4, first_seq=7,
    )
    return [replace(frame, rssi=frame.rssi + NOISE[i % len(NOISE)]) for i, frame in enumerate(frames)]


def test_FR16_temperature_one_equals_none():
    tau_one = with_tau(1.0)

    # 근삿값이 아니라 같은 값이어야 한다. x / 1.0 == x이므로 계산 식 하나로 두 방법을 다룬다.
    for frames in (standing_frames(MODEL_A, "locker-2"), noisy_moving_frames()):
        assert compute(frames, model=tau_one).posteriors == compute(frames).posteriors


def test_FR16_temperature_tempers_evidence():
    # 증거를 적게 써서 확률이 1로 포화하지 않게 한다. 앵커 하나의 프레임 둘이다.
    frames = standing_frames(MODEL_A, "locker-2", count=2, anchors=["locker-2"])
    prior = 1 / 7  # 처음 분포: trusted 기기 여섯 개와 "어느 기기 앞도 아님"

    results = [compute(frames, model=with_tau(tau)) for tau in TAUS]

    probabilities = [result.posteriors["locker-2"] for result in results]
    assert probabilities[0] < 0.999  # 전제: τ = 1에서 포화하지 않아야 τ의 영향이 보인다
    for result in results:
        others = [p for node_id, p in result.posteriors.items() if node_id != "locker-2"]
        assert result.posteriors["locker-2"] > max(others)  # 최대 확률 기기는 τ와 관계없이 locker-2다
    assert probabilities[0] > probabilities[1] > probabilities[2] > prior  # τ가 클수록 처음 분포에 가깝다

    # 관측이 없으면 τ가 바꿀 증거가 없다.
    unobserved = compute([])
    for tau in TAUS:
        assert compute([], model=with_tau(tau)) == unobserved


@pytest.mark.parametrize("tau", TAUS)
def test_FR16_temperature_divides_evidence_log_likelihood(tau):
    """칸마다 두 상태의 우도를 1/τ제곱해서 forward에 넣는다. 확률 공간의 forward를 직접 적어 구현과 따로 정한다.

    상태는 mood-1 앞과 "어느 기기 앞도 아님" 둘이다. 마지막 두 칸에 프레임이 하나씩 있고, 앞의 칸은 기기 앞의
    기대값, 뒤의 칸은 none_points 위치의 기대값이다. "어느 기기 앞도 아님"의 우도는 먼 위치와 none_points의
    혼합이고, 혼합 전체를 한 번 나눈다. 칸마다 나누지 않고 마지막 확률에 온도를 쓰거나, 혼합의 성분마다
    따로 나누면 τ > 1에서 값이 달라진다.
    """
    model = replace(MODEL_B, none_points=((0.0, 2.5),))
    front_mean = -59.0 - 20.0 * math.log10(0.5)
    point_mean = -59.0 - 20.0 * math.log10(2.5)  # 앵커 (0, 0)에서 (0, 2.5)까지
    frames = [frame_at(8.5, front_mean, seq=1), frame_at(9.5, point_mean, seq=2)]  # 칸 8, 칸 9

    result = compute(frames, model=with_tau(tau, model), place_nodes=NODES_B)

    belief = 0.5  # mood-1 앞일 확률. 처음 분포가 균등하고 프레임이 없는 앞의 칸은 예측만 하므로 그대로다
    for rssi in (front_mean, point_mean):
        belief = belief * model.stay + (1 - belief) * (1 - model.stay)  # 예측: 상태가 둘이라 나머지는 모두 다른 상태로 간다
        front = normal_pdf(rssi, front_mean, 4.0) ** (1 / tau)
        none = ((normal_pdf(rssi, -90.0, 6.0) + normal_pdf(rssi, point_mean, 4.0)) / 2) ** (1 / tau)
        belief = belief * front / (belief * front + (1 - belief) * none)
    assert result.posteriors["mood-1"] == close(belief)
