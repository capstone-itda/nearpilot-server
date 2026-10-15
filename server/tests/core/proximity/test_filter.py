"""관측 구간 사후확률 계산 (FR-16, FR-22). 합성 모델 파일과 손으로 만든 프레임으로 확인한다."""

from __future__ import annotations

import itertools
import math
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from nearpilot.core.proximity import (
    NONE_STATE,
    ModelMismatchError,
    ProximityInputError,
    ProximityModel,
    compute_posteriors,
    load_model,
)
from nearpilot.shared.enums import DeviceState, TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, ProximityResult, RssiFrame

from .conftest import BEACON, MODEL_ROOT, OTHER_BEACON, PLACE_A_NODES, T0, make_frame, make_node, place_a_nodes

MODEL_A = load_model(MODEL_ROOT / "place-a" / "syn-a-1.json")
MODEL_B = load_model(MODEL_ROOT / "place-b" / "syn-b-1.json")
NODES_B = [make_node("mood-1", "place-b")]
BIN_SEC = 1.0
AT = T0 + timedelta(seconds=30)  # 판정 시각
WINDOW_START = AT - timedelta(seconds=MODEL_A.window_sec)  # place-a와 place-b의 window_sec가 같다
EXTREME_RSSI = -30.0  # 어느 상태에서도 거의 나오지 않는 값. 쓰이면 결과가 크게 달라진다


# ── 도우미 ───────────────────────────────────────────────────────────


def front_rssi(model: ProximityModel, anchor_id: str, device_id: str | None) -> float:
    """모델 식으로 계산한 평균 RSSI (잡음 없음). device_id 앞에 선 방문자를 anchor_id가 받는 값이다.

    device_id가 None이면 "어느 기기 앞도 아님"이다.
    """
    anchor = model.nodes[anchor_id]
    if device_id is None:
        return anchor.none_rssi
    front = model.nodes[device_id]
    distance = max(math.hypot(anchor.x - front.front_x, anchor.y - front.front_y), model.min_distance_m)
    return anchor.A - 10.0 * anchor.n * math.log10(distance)


def standing_frames(
    model: ProximityModel,
    device_id: str | None,
    *,
    count: int = 10,
    last_ts: datetime = AT,
    beacon: BeaconKey = BEACON,
    anchors: list[str] | None = None,
    first_seq: int = 1,
) -> list[RssiFrame]:
    """device_id 앞에 count초 서 있는 프레임. 앵커마다 1초 간격이고 마지막 프레임의 frame_ts가 last_ts다.

    received_at이 frame_ts와 같아서 frame_ts == at인 프레임도 쓸 수 있다.
    """
    frames = []
    for anchor_id in model.nodes if anchors is None else anchors:
        for i in range(count):
            ts = last_ts - timedelta(seconds=count - 1 - i)
            frames.append(
                make_frame(
                    anchor_id,
                    front_rssi(model, anchor_id, device_id),
                    ts,
                    beacon=beacon,
                    seq=first_seq + i,
                    received_at=ts,
                )
            )
    return frames


def frame_at(
    offset_sec: float, rssi: float, *, node_id: str = "mood-1", seq: int = 1, session_id: str | None = None,
    window_sec: float = MODEL_B.window_sec,
) -> RssiFrame:
    """관측 구간 시작에서 offset_sec초 뒤의 프레임. received_at이 frame_ts와 같다."""
    ts = AT - timedelta(seconds=window_sec) + timedelta(seconds=offset_sec)
    return make_frame(node_id, rssi, ts, seq=seq, session_id=session_id, received_at=ts)


def compute(
    frames,
    *,
    model: ProximityModel = MODEL_A,
    beacon: BeaconKey = BEACON,
    candidates: list[NodeInfo] | None = None,
    place_nodes: list[NodeInfo] | None = None,
    at: datetime = AT,
    bin_sec: float = BIN_SEC,
) -> ProximityResult:
    """기본값은 place-a의 전체 기기를 후보로 하는 호출이다."""
    nodes = place_a_nodes() if place_nodes is None else place_nodes
    return compute_posteriors(
        model, frames, beacon, nodes if candidates is None else candidates, nodes, at, bin_sec=bin_sec,
    )


def close(expected: float):
    return pytest.approx(expected, rel=1e-9, abs=1e-300)


def normal_pdf(x: float, mean: float, sigma: float) -> float:
    return math.exp(-0.5 * ((x - mean) / sigma) ** 2) / (sigma * math.sqrt(2.0 * math.pi))


def point_rssi(model: ProximityModel, anchor_id: str, x: float, y: float) -> float:
    """(x, y)에 선 방문자를 anchor_id가 받는 평균 RSSI (잡음 없음)."""
    anchor = model.nodes[anchor_id]
    distance = max(math.hypot(anchor.x - x, anchor.y - y), model.min_distance_m)
    return anchor.A - 10.0 * anchor.n * math.log10(distance)


def none_density(model: ProximityModel, readings: dict[str, float]) -> float:
    """한 칸의 {앵커: RSSI}가 "어느 기기 앞도 아님"에서 나올 밀도. 먼 위치와 none_points의 같은 비중 혼합이다."""
    far = math.prod(
        normal_pdf(rssi, model.nodes[a].none_rssi, model.nodes[a].none_sigma) for a, rssi in readings.items()
    )
    points = [
        math.prod(normal_pdf(rssi, point_rssi(model, a, x, y), model.nodes[a].sigma) for a, rssi in readings.items())
        for x, y in model.none_points
    ]
    return (far + sum(points)) / (1 + len(points))


def reference_posteriors(
    model: ProximityModel, trusted_ids: list[str], observed: dict[int, dict[str, float]], n_bins: int,
) -> dict[str, float]:
    """확률 공간의 HMM forward. 구현(로그 공간)과 따로 쓴 기준값이다.

    observed는 칸 번호 → {앵커: 평균 RSSI}다. 상태는 trusted_ids와 "어느 기기 앞도 아님"(None)이고,
    반환값은 trusted 기기마다의 확률이다.
    """
    states = [*trusted_ids, None]
    k_states = len(states)
    move = (1.0 - model.stay) / (k_states - 1)
    belief = [1.0 / k_states] * k_states
    for k in range(n_bins):
        belief = [
            sum(belief[i] * (model.stay if i == j else move) for i in range(k_states)) for j in range(k_states)
        ]
        readings = observed.get(k, {})
        if readings:
            for j, state in enumerate(states[:-1]):
                for anchor_id, rssi in readings.items():
                    belief[j] *= normal_pdf(rssi, front_rssi(model, anchor_id, state), model.nodes[anchor_id].sigma)
            belief[-1] *= none_density(model, readings)
        total = sum(belief)
        belief = [b / total for b in belief]
    return dict(zip(trusted_ids, belief))


# ── 후보와 상태 공간 ───────────────────────────────────────────────────


@pytest.mark.parametrize("device_id", PLACE_A_NODES)
def test_FR16_front_position_selects_device(device_id):
    result = compute(standing_frames(MODEL_A, device_id))

    best_id, best_probability = result.best
    assert best_id == device_id
    others = [probability for node_id, probability in result.posteriors.items() if node_id != device_id]
    assert best_probability > max(others)  # 이름 순 동률 때문이 아니라 확률이 가장 크다
    assert all(type(probability) is float for probability in result.posteriors.values())


def test_FR16_candidate_sum_at_most_one():
    nodes = place_a_nodes()
    frames = standing_frames(MODEL_A, "light-1")
    outside = [n for n in nodes if n.node_id in ("locker-1", "locker-3", "light-2")]  # light-1은 후보 밖이다
    inside = [n for n in nodes if n.node_id not in ("locker-1", "locker-3", "light-2")]  # light-1이 후보다

    whole = compute(frames, candidates=nodes)
    first = compute(frames, candidates=outside)
    second = compute(frames, candidates=inside)

    for part in (first, second):
        assert sum(part.posteriors.values()) <= 1.0
        for node_id, probability in part.posteriors.items():
            assert probability == pytest.approx(whole.posteriors[node_id])  # 후보 구성이 확률을 바꾸지 않는다
    # 후보끼리 다시 정규화했다면 두 부분의 합이 각각 1이 되어 전체 합을 넘는다.
    assert sum(first.posteriors.values()) + sum(second.posteriors.values()) == pytest.approx(
        sum(whole.posteriors.values())
    )


def test_FR16_outside_state_does_not_raise_candidates():
    lockers = [n for n in place_a_nodes() if n.node_id.startswith("locker")]

    result = compute(standing_frames(MODEL_A, "light-1"), candidates=lockers)

    assert set(result.posteriors) == {"locker-1", "locker-2", "locker-3"}
    assert all(probability < 0.05 for probability in result.posteriors.values())


def test_FR16_empty_candidates():
    result = compute(standing_frames(MODEL_A, "locker-1"), candidates=[])

    assert result.posteriors == {}
    assert result.best is None
    assert (result.model_version, result.calib_version) == ("syn-a-1", "none-1")
    assert (result.window_start, result.window_end) == (WINDOW_START, AT)


@pytest.mark.parametrize(
    ("model", "nodes", "n_states"),
    [
        pytest.param(MODEL_A, place_a_nodes(), 7, id="place_a"),
        pytest.param(MODEL_B, NODES_B, 2, id="place_b"),
    ],
)
def test_FR16_no_observation_returns_initial_distribution(model, nodes, n_states):
    result = compute([], model=model, place_nodes=nodes)

    assert result.posteriors == pytest.approx({node.node_id: 1 / n_states for node in nodes})
    assert NONE_STATE not in result.posteriors
    assert (result.window_start, result.window_end) == (AT - timedelta(seconds=model.window_sec), AT)
    assert (result.model_version, result.calib_version) == (model.model_version, model.calib_version)


@pytest.mark.parametrize("with_candidates", [True, False], ids=["with_candidates", "empty_candidates"])
def test_FR16_trusted_node_missing_from_model_raises(with_candidates):
    nodes = [*place_a_nodes(), make_node("locker-9")]  # 모델 파일에 없는 trusted 기기
    candidates = place_a_nodes() if with_candidates else []

    with pytest.raises(ModelMismatchError, match="locker-9"):
        compute(standing_frames(MODEL_A, "locker-1"), candidates=candidates, place_nodes=nodes)


def test_FR16_untrusted_model_node_is_excluded():
    # speaker-1은 모델에 있지만 trusted가 아니다. locker-9는 모델에도 없고 trusted도 아니다. 둘 다 오류가 아니다.
    nodes = [*place_a_nodes(**{"speaker-1": TrustState.DISCOVERED}), make_node("locker-9", trust=TrustState.DISCOVERED)]
    candidates = [node for node in nodes if node.trust_state == TrustState.TRUSTED]
    anchors = [node.node_id for node in candidates]

    # 관측이 없으면 상태는 trusted 5개와 "어느 기기 앞도 아님"이라 6개다.
    empty = compute([], candidates=candidates, place_nodes=nodes)
    assert empty.posteriors == pytest.approx({node_id: 1 / 6 for node_id in anchors})

    # 앵커가 아닌 기기의 프레임은 아무리 강해도 쓰지 않는다.
    seen = standing_frames(MODEL_A, "locker-1", anchors=anchors)
    ignored = standing_frames(MODEL_A, None, anchors=["speaker-1"]) + [
        make_frame(node_id, EXTREME_RSSI, AT - timedelta(seconds=i), seq=i + 1, received_at=AT - timedelta(seconds=i))
        for node_id in ("speaker-1", "locker-9", "locker-7")  # locker-7은 어디에도 없는 기기다
        for i in range(10)
    ]
    baseline = compute(seen, candidates=candidates, place_nodes=nodes)
    assert baseline.best[0] == "locker-1"
    assert compute(seen + ignored, candidates=candidates, place_nodes=nodes) == baseline


def test_FR16_other_beacon_frames_are_ignored():
    mine = standing_frames(MODEL_A, "locker-1")
    other = standing_frames(MODEL_A, "light-2", beacon=OTHER_BEACON)

    baseline = compute(mine)
    assert baseline.best[0] == "locker-1"
    assert compute(mine + other) == baseline
    # 다른 비콘의 프레임만 있으면 관측이 없는 것과 같다.
    assert compute(other).posteriors == pytest.approx({node_id: 1 / 7 for node_id in PLACE_A_NODES})
    # 비콘 인자를 바꾸면 그 비콘의 프레임을 고른다. 모든 프레임을 버리는 것이 아니다.
    assert compute(mine + other, beacon=OTHER_BEACON).best[0] == "light-2"


def test_FR16_fault_and_offline_nodes_stay_in_state_space():
    nodes = place_a_nodes()
    nodes[1] = make_node("locker-2", device_state=DeviceState.FAULT)
    nodes[4] = replace(make_node("light-2"), last_seen_at=None)  # 한 번도 보고하지 않은 오프라인 기기

    result = compute([], place_nodes=nodes)
    assert result.posteriors == pytest.approx({node_id: 1 / 7 for node_id in PLACE_A_NODES})

    # FAULT 기기의 앞 상태도 있고, 그 기기의 프레임도 증거로 쓴다.
    assert compute(standing_frames(MODEL_A, "locker-2"), place_nodes=nodes).best[0] == "locker-2"
    assert compute(standing_frames(MODEL_A, "light-2"), place_nodes=nodes).best[0] == "light-2"


def test_FR16_no_trusted_nodes_leaves_only_outside_state():
    # trusted 기기가 없으면 상태는 "어느 기기 앞도 아님" 하나뿐이다. 전이는 항등이고 후보는 있을 수 없다.
    nodes = place_a_nodes(**{node_id: TrustState.DISCOVERED for node_id in PLACE_A_NODES})

    result = compute(standing_frames(MODEL_A, "locker-1"), candidates=[], place_nodes=nodes)

    assert result.posteriors == {}
    assert (result.window_start, result.window_end) == (WINDOW_START, AT)
    with pytest.raises(ProximityInputError, match="trusted"):
        compute([], candidates=nodes[:1], place_nodes=nodes)


MISMATCH_CASES = [
    pytest.param({"place_nodes": [], "candidates": []}, "place_nodes가 비어", id="empty_place_nodes"),
    pytest.param({"place_nodes": [*place_a_nodes(), make_node("mood-1", "place-b")]}, "한 장소", id="two_places"),
    pytest.param(
        {"place_nodes": [replace(node, place_id=None) for node in place_a_nodes()], "candidates": []},
        "한 장소",
        id="place_id_none",
    ),
    pytest.param(
        {"place_nodes": [make_node("mood-1", "place-b")], "candidates": []}, "모델의 장소", id="place_differs_from_model",
    ),
    pytest.param({"candidates": [make_node("locker-9")]}, "place_nodes에 없다", id="candidate_not_in_place_nodes"),
    pytest.param(
        {"candidates": [make_node("locker-1", "place-b")]}, "장소가 place_nodes와 다르다", id="candidate_in_other_place",
    ),
    pytest.param(
        {
            "place_nodes": place_a_nodes(**{"speaker-1": TrustState.DISCOVERED}),
            "candidates": [make_node("speaker-1", trust=TrustState.DISCOVERED)],
        },
        "후보의 trust_state",
        id="candidate_not_trusted",
    ),
    pytest.param(
        {"candidates": [make_node("locker-1", trust=TrustState.REVOKED)]},
        "후보의 trust_state",
        id="candidate_revoked_but_place_nodes_trusted",
    ),
    pytest.param(
        {
            "place_nodes": place_a_nodes(**{"locker-1": TrustState.REVOKED}),
            "candidates": [make_node("locker-1")],
        },
        "place_nodes에서는",
        id="candidate_trusted_but_place_nodes_revoked",
    ),
    pytest.param(
        {"place_nodes": [*place_a_nodes(), make_node("locker-1")]}, "같은 node_id", id="duplicate_node_id",
    ),
    pytest.param(
        # 모델에 없는 trusted 기기(ModelMismatchError)가 있어도 입력 관계 검사가 먼저다.
        {"place_nodes": [*place_a_nodes(), make_node("locker-9")], "candidates": [], "bin_sec": 0.0},
        "bin_sec",
        id="input_check_comes_before_model_check",
    ),
    pytest.param({"bin_sec": 0.0}, "bin_sec", id="bin_sec_zero"),
    pytest.param({"bin_sec": -1.0}, "bin_sec", id="bin_sec_negative"),
    pytest.param({"bin_sec": math.nan}, "bin_sec", id="bin_sec_nan"),
]


@pytest.mark.parametrize(("arguments", "message"), MISMATCH_CASES)
def test_FR16_place_relation_mismatch_raises(arguments, message):
    # 사례마다 어긴 규칙이 하나뿐이고, 오류 메시지로 그 규칙 때문에 거부했는지 확인한다.
    with pytest.raises(ProximityInputError, match=message):
        compute(standing_frames(MODEL_A, "locker-1"), **arguments)


# ── 재현 (FR-22) ──────────────────────────────────────────────────────


def test_FR22_same_input_same_output():
    # 마지막 세 칸에는 locker-2 앞 프레임과 light-1 앞 프레임이 섞여 있다.
    frames = standing_frames(MODEL_A, "locker-2") + standing_frames(
        MODEL_A, "light-1", count=3, last_ts=AT - timedelta(seconds=0.5), anchors=["locker-1", "light-2"], first_seq=50,
    )
    # 마지막 칸에 같은 키의 재전송이 더 늦은 received_at과 다른 값으로 들어 있다.
    # 정렬하지 않으면 입력 순서가 어느 쪽을 쓸지 정하고, 마지막 칸이라 결과도 달라진다.
    original = frames[-1]
    frames.append(replace(original, rssi=EXTREME_RSSI, received_at=original.received_at + timedelta(seconds=0.2)))
    third = len(frames) // 3
    permutations = [
        frames[::-1],
        frames[1::2] + frames[0::2],
        frames[third:] + frames[:third],
    ]

    baseline = compute(frames)
    assert baseline.best[0] == "locker-2"
    # 재전송이 원본보다 먼저 도착했다면 재전송을 쓴다. 이 사례에서 어느 쪽을 쓰느냐가 결과를 바꾼다.
    resent_first = [*frames[:-1], replace(frames[-1], received_at=original.received_at - timedelta(seconds=1))]
    assert compute(resent_first) != baseline
    for permutation in permutations:
        assert compute(permutation) == baseline
    assert compute(iter(permutations[1])) == baseline  # 한 번만 읽을 수 있는 입력도 같다
    assert compute(frames) == baseline  # 불러도 상태가 쌓이지 않는다


def test_FR22_late_frame_is_excluded():
    base = standing_frames(MODEL_A, "locker-1")
    baseline = compute(base)
    last = AT - timedelta(seconds=0.5)

    late = make_frame("locker-1", EXTREME_RSSI, last, seq=99, received_at=AT + timedelta(milliseconds=1))
    assert compute([*base, late]) == baseline

    on_time = make_frame("locker-1", EXTREME_RSSI, last, seq=99, received_at=AT)  # received_at == at는 쓴다
    assert compute([*base, on_time]) != baseline


def test_FR22_window_bounds():
    one_us = timedelta(microseconds=1)
    unobserved = compute([])

    def only(frame_ts: datetime) -> ProximityResult:
        """프레임 하나만 있는 구간. 쓰이면 처음 분포에서 벗어나고, 쓰이지 않으면 관측이 없는 것과 같다."""
        return compute([make_frame("locker-1", -55.0, frame_ts, received_at=AT)])

    assert only(WINDOW_START) == unobserved  # frame_ts == window_start는 쓰지 않는다
    assert only(WINDOW_START + one_us) != unobserved
    assert only(AT) != unobserved  # frame_ts == at는 쓴다
    assert only(AT + one_us) == unobserved  # received_at이 at이어도 frame_ts가 at보다 뒤면 쓰지 않는다


def test_FR16_duplicate_frame_is_used_once():
    base = standing_frames(MODEL_A, "locker-1")
    # 마지막 칸의 프레임이다. 칸 평균이 달라지면 결과가 달라진다.
    frame_ts = AT - timedelta(seconds=0.5)
    original = make_frame("locker-2", -60.0, frame_ts, seq=99, received_at=AT - timedelta(seconds=0.4))
    baseline = compute([*base, original])

    assert compute([*base, original, original]) == baseline

    # 재전송은 값이 달라도 정렬 순서로 먼저인 프레임(더 이른 received_at)만 쓴다. 입력 순서와 상관없다.
    resent = replace(original, rssi=EXTREME_RSSI, received_at=AT - timedelta(seconds=0.1))
    assert compute([*base, original, resent]) == baseline
    assert compute([*base, resent, original]) == baseline

    # seq가 다르면 다른 프레임이다.
    assert compute([*base, original, replace(resent, seq=100)]) != baseline

    # received_at까지 같으면 rssi가 작은 쪽이 먼저다. 입력 순서와 상관없다.
    tie = replace(original, rssi=-61.0)
    assert compute([*base, original, tie]) == compute([*base, tie, original])


# ── 계산식 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("front_y", "mean"),
    [
        pytest.param(0.5, -59.0 - 20.0 * math.log10(0.5), id="front_0.5m"),
        pytest.param(0.1, -59.0 - 20.0 * math.log10(0.3), id="front_0.1m_is_clamped_to_min_distance"),
    ],
)
def test_FR16_front_state_mean_follows_path_loss_formula(front_y, mean):
    """기기 앞 상태의 평균은 A − 10·n·log10(max(거리, min_distance_m))다. 평균을 직접 적어 구현과 따로 정한다."""
    # none_points를 비워서 "어느 기기 앞도 아님"을 먼 위치 성분 하나로 둔다. 기기 앞 상태의 평균만 확인한다.
    model = replace(MODEL_B, nodes={"mood-1": replace(MODEL_B.nodes["mood-1"], front_y=front_y)}, none_points=())
    rssi = -50.0

    # 프레임 하나가 마지막 칸에 있고 처음 분포가 균등하다. 사후확률은 두 상태의 밀도 비다.
    front = normal_pdf(rssi, mean, 4.0)
    none = normal_pdf(rssi, -90.0, 6.0)
    result = compute([frame_at(10.0, rssi)], model=model, place_nodes=NODES_B)

    assert result.posteriors["mood-1"] == close(front / (front + none))


def _varied(model: ProximityModel) -> ProximityModel:
    """기기마다 A, n, sigma, none_*가 다르고 전이, 구간, 거리 하한도 바꾼 모델.

    앵커의 값을 써야 할 곳에 다른 기기의 값이나 고정 값을 쓰면 결과가 달라진다.
    """
    nodes = {
        node_id: replace(
            node,
            A=node.A + i,
            n=node.n + 0.1 * i,
            sigma=node.sigma + 0.5 * i,
            none_rssi=node.none_rssi + 2.0 * i,
            none_sigma=node.none_sigma + 0.5 * i,
        )
        for i, (node_id, node) in enumerate(model.nodes.items())
    }
    return replace(model, nodes=nodes, stay=0.7, window_sec=8.0, min_distance_m=0.6)


NOISE = (2.5, -3.0, 1.0, -1.5, 4.0, -2.0, 0.5, 3.0)  # 손으로 고른 오차(dB)


def _walk(model: ProximityModel, first: str | None, second: str | None) -> dict[int, dict[str, float]]:
    """칸 → {앵커: 평균 RSSI}. 앞 절반은 first 앞, 뒤 절반은 second 앞이다.

    손으로 고른 오차를 더하고, 칸마다 일부 앵커는 보고하지 않으며, 가운데 칸은 프레임이 없다.
    """
    n_bins = round(model.window_sec / BIN_SEC)
    observed = {}
    for k in range(n_bins):
        if k == n_bins // 2:
            continue
        device = first if k < n_bins // 2 else second
        present = [anchor_id for j, anchor_id in enumerate(model.nodes) if (j + k) % 3 != 0]
        observed[k] = {
            anchor_id: front_rssi(model, anchor_id, device) + NOISE[(j + k) % len(NOISE)]
            for j, anchor_id in enumerate(present)
        }
    return observed


def _frames_of(model: ProximityModel, observed: dict[int, dict[str, float]]) -> list[RssiFrame]:
    """칸의 가운데에 프레임 하나씩. 칸 2의 첫 앵커만 평균이 같은 프레임 둘로 나눈다."""
    window_start = AT - timedelta(seconds=model.window_sec)
    seqs = itertools.count(1)
    frames = []
    for k, by_anchor in observed.items():
        for j, (anchor_id, rssi) in enumerate(by_anchor.items()):
            middle = window_start + timedelta(seconds=(k + 0.5) * BIN_SEC)
            if k == 2 and j == 0:
                for shift, value in ((-0.25, rssi - 1.5), (0.25, rssi + 1.5)):
                    frames.append(make_frame(anchor_id, value, middle + timedelta(seconds=shift), seq=next(seqs)))
            else:
                frames.append(make_frame(anchor_id, rssi, middle, seq=next(seqs)))
    return frames


@pytest.mark.parametrize(
    ("model", "nodes", "path"),
    [
        pytest.param(MODEL_A, place_a_nodes(), ("light-1", "locker-2"), id="place_a"),
        pytest.param(_varied(MODEL_A), place_a_nodes(), ("locker-3", None), id="place_a_varied_parameters"),
        pytest.param(MODEL_B, NODES_B, ("mood-1", None), id="place_b"),
    ],
)
def test_FR16_forward_matches_reference(model, nodes, path):
    observed = _walk(model, *path)
    frames = _frames_of(model, observed)
    n_bins = round(model.window_sec / BIN_SEC)

    result = compute(frames, model=model, place_nodes=nodes)

    expected = reference_posteriors(model, sorted(model.nodes), observed, n_bins)
    assert set(result.posteriors) == set(expected)
    for node_id, probability in expected.items():
        assert result.posteriors[node_id] == close(probability), node_id


@pytest.mark.parametrize(
    ("bin_sec", "offset", "expected_bin", "n_bins"),
    [
        pytest.param(1.0, 10.0, 9, 10, id="last_frame_at_at"),
        pytest.param(1.0, 0.001, 0, 10, id="just_after_window_start"),
        pytest.param(1.0, 3.0, 2, 10, id="bin_end_is_closed"),
        pytest.param(1.0, 3.001, 3, 10, id="just_after_bin_end"),
        pytest.param(3.0, 3.0, 0, 3, id="bin_end_is_closed_for_3s_bins"),
        pytest.param(3.0, 4.0, 1, 3, id="middle_bin_for_3s_bins"),
        pytest.param(3.0, 10.0, 2, 3, id="remainder_goes_to_last_bin"),
        pytest.param(3.0, 1.0, 0, 3, id="10_over_3_rounds_down"),
        pytest.param(6.0, 3.0, 0, 2, id="10_over_6_rounds_up"),
        pytest.param(6.0, 10.0, 1, 2, id="last_bin_ends_at_at"),
        pytest.param(7.0, 9.0, 0, 1, id="one_bin_clips_index"),
        pytest.param(20.0, 5.0, 0, 1, id="bin_longer_than_window"),
        pytest.param(0.3, 2.1, 6, 33, id="inexact_binary_bin_end_is_closed"),
        pytest.param(0.7, 2.1, 2, 14, id="inexact_binary_bin_end_is_closed_0_7"),
    ],
)
def test_FR16_time_bins_are_right_closed_and_clipped(bin_sec, offset, expected_bin, n_bins):
    rssi = -55.0
    result = compute([frame_at(offset, rssi)], model=MODEL_B, place_nodes=NODES_B, bin_sec=bin_sec)

    def posterior_if_in_bin(k: int) -> dict[str, float]:
        return reference_posteriors(MODEL_B, ["mood-1"], {k: {"mood-1": rssi}}, n_bins)

    assert result.posteriors["mood-1"] == close(posterior_if_in_bin(expected_bin)["mood-1"])
    # 칸을 잘못 고르면 결과가 달라지는 사례여야 한다.
    wrong = [posterior_if_in_bin(k)["mood-1"] for k in range(n_bins) if k != expected_bin]
    assert all(probability != close(result.posteriors["mood-1"]) for probability in wrong)


def test_FR16_frames_of_different_nodes_are_not_duplicates():
    # 두 기기가 같은 session_id와 seq를 쓸 수 있다. node_id가 다르면 다른 프레임이다.
    ts = AT - timedelta(seconds=0.5)  # 마지막 칸
    readings = {"locker-1": -52.0, "locker-2": -63.0}
    frames = [
        make_frame(node_id, rssi, ts, seq=1, session_id="shared", received_at=ts) for node_id, rssi in readings.items()
    ]

    result = compute(frames)

    expected = reference_posteriors(MODEL_A, sorted(MODEL_A.nodes), {9: readings}, 10)
    for node_id, probability in expected.items():
        assert result.posteriors[node_id] == close(probability), node_id


def test_FR16_log_space_does_not_underflow():
    # σ가 아주 작은 모델에서 기대값과 4 dB 어긋난 프레임이다. 두 상태의 밀도가 모두 0으로 언더플로한다(exp(-792), exp(-84050)).
    # 로그 공간에서는 두 로그 우도의 차이로 사후확률이 정해진다.
    sharp = {"mood-1": replace(MODEL_B.nodes["mood-1"], sigma=0.1, none_sigma=0.1)}
    model = replace(MODEL_B, nodes=sharp)
    assert normal_pdf(-49.0, -59.0 - 20.0 * math.log10(0.5), 0.1) == 0.0  # 확률 공간에서는 구분되지 않는 입력이다

    result = compute([frame_at(10.0, -49.0)], model=model, place_nodes=NODES_B)

    assert result.posteriors["mood-1"] == pytest.approx(1.0)


def test_FR16_same_bin_frames_are_averaged():
    # 같은 칸에서 같은 앵커의 서로 다른 연결이 같은 seq로 보고했다. 중복이 아니라서 둘 다 평균에 든다.
    two = [frame_at(4.4, -50.0, session_id="s-1"), frame_at(4.6, -70.0, session_id="s-2")]
    mean = [frame_at(4.5, -60.0)]

    result = compute(two, model=MODEL_B, place_nodes=NODES_B)

    assert result == compute(mean, model=MODEL_B, place_nodes=NODES_B)
    assert result != compute(two[:1], model=MODEL_B, place_nodes=NODES_B)


@pytest.mark.parametrize("bad_rssi", [math.nan, math.inf, -math.inf])
def test_FR16_non_finite_rssi_is_treated_as_missing(bad_rssi):
    # 유한하지 않은 RSSI는 결측과 같다. 결과는 그 프레임이 없을 때와 같다.
    good = [frame_at(4.5, -55.0)]
    bad = [*good, frame_at(9.5, bad_rssi, seq=2)]

    assert compute(bad, model=MODEL_B, place_nodes=NODES_B) == compute(good, model=MODEL_B, place_nodes=NODES_B)


def test_FR16_none_state_is_mixture_of_far_and_none_points():
    """"어느 기기 앞도 아님"의 밀도는 먼 위치 성분과 none_points 성분들의 같은 비중 평균이다. 값을 직접 적는다."""
    model = replace(MODEL_B, none_points=((0.0, 2.5),))
    rssi = -67.0
    point_mean = -59.0 - 20.0 * math.log10(2.5)  # 앵커 (0, 0)에서 (0, 2.5)까지
    front = normal_pdf(rssi, -59.0 - 20.0 * math.log10(0.5), 4.0)
    none = (normal_pdf(rssi, -90.0, 6.0) + normal_pdf(rssi, point_mean, 4.0)) / 2

    result = compute([frame_at(10.0, rssi)], model=model, place_nodes=NODES_B)

    assert result.posteriors["mood-1"] == close(front / (front + none))


@pytest.mark.parametrize(
    ("x", "y"),
    [
        pytest.param(2.0, 1.5, id="1m_behind_front"),
        pytest.param(2.0, 3.0, id="2_5m_behind_front"),
        pytest.param(2.3, 2.7, id="between_none_points"),
        pytest.param(4.4, 2.0, id="behind_light_2"),
        pytest.param(2.0, 6.0, id="far_behind"),
        pytest.param(10.0, 0.5, id="outside_place"),
    ],
)
def test_FR16_standing_away_from_fronts_keeps_candidates_low(x, y):
    """기기 앞이 아닌 위치에 10초 서 있으면 후보 확률의 합이 0.05 미만이다 (NFR-04, FR-16).

    none_points가 없던 모델은 (2, 3)에서 locker-1을 0.99로 골랐다.
    """
    frames = [
        make_frame(anchor_id, point_rssi(MODEL_A, anchor_id, x, y), AT - timedelta(seconds=9 - t, milliseconds=500), seq=t + 1)
        for t in range(10)
        for anchor_id in PLACE_A_NODES
    ]

    result = compute(frames)

    assert sum(result.posteriors.values()) < 0.05, dict(result.posteriors)
