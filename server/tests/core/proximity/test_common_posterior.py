"""공통 모델의 관측 구간 사후확률 계산 (FR-16). 합성 공통 모델 파일과 손으로 만든 프레임으로 확인한다."""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nearpilot.core.proximity.common_model import CommonModel, RoleParams, load_common_model
from nearpilot.core.proximity.posterior import NONE_STATE, ProximityInputError, compute_posteriors
from nearpilot.shared.enums import TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, ProximityResult, RssiFrame

from .conftest import BEACON, OTHER_BEACON, T0, make_frame, make_node

MODEL = load_common_model(Path(__file__).resolve().parent / "fixtures" / "common" / "syn-common-1.json")
BIN_SEC = 1.0
AT = T0 + timedelta(seconds=30)  # 판정 시각
WINDOW_START = AT - timedelta(seconds=MODEL.window_sec)
LOCKERS = ("locker-1", "locker-2", "locker-3")
ALL_IDS = (*LOCKERS, "light-1", "light-2", "speaker-1")


# ── 도우미 ───────────────────────────────────────────────────────────


def nodes_of(
    ids: Sequence[str] = LOCKERS, place_id: str = "place-a", *, revoked: Collection[str] = (),
) -> list[NodeInfo]:
    """ids의 기기. 기본은 모두 trusted이고, revoked에 든 기기는 신뢰 해제 상태다."""
    return [
        make_node(node_id, place_id, trust=TrustState.REVOKED if node_id in revoked else TrustState.TRUSTED)
        for node_id in ids
    ]


def ideal_rssi(anchor_id: str, device_id: str | None, model: CommonModel = MODEL) -> float:
    """device_id 앞에 선 방문자를 anchor_id가 듣는 평균 RSSI (잡음 없음, 보정한 값). device_id가 None이면 "어느 기기 앞도 아님"이다."""
    if device_id is None:
        return model.none.mean
    return model.front.mean if anchor_id == device_id else model.other.mean


def standing_frames(
    device_id: str | None,
    anchors: Sequence[str] = LOCKERS,
    *,
    offsets: Mapping[str, float] | None = None,
    count: int = 3,
    last_ts: datetime = AT,
    beacon: BeaconKey = BEACON,
    first_seq: int = 1,
) -> list[RssiFrame]:
    """device_id 앞에 count초 서 있는 프레임. 앵커마다 1초 간격이고 마지막 프레임의 frame_ts가 last_ts다.

    received_at이 frame_ts와 같아서 frame_ts == at인 프레임도 쓸 수 있다.
    offsets는 앵커별 영점(dB)이다. 보정 전 RSSI는 이상값에 영점을 더한 값이다 (보정은 `r − 영점`).
    """
    offsets = offsets or {}
    frames = []
    for anchor_id in anchors:
        for i in range(count):
            ts = last_ts - timedelta(seconds=count - 1 - i)
            rssi = ideal_rssi(anchor_id, device_id) + offsets.get(anchor_id, 0.0)
            frames.append(make_frame(anchor_id, rssi, ts, beacon=beacon, seq=first_seq + i, received_at=ts))
    return frames


def frames_from_bins(
    raw_bins: Mapping[int, Mapping[str, Sequence[float]]],
    *,
    bin_sec: float = BIN_SEC,
    beacon: BeaconKey = BEACON,
) -> list[RssiFrame]:
    """칸 k의 {앵커: 보정 전 RSSI 목록}으로 프레임을 만든다.

    칸 k는 `(window_start + k·bin_sec, window_start + (k+1)·bin_sec]`이다. 목록의 j번째 프레임은 칸 끝에서 0.1칸씩 j번 앞선 시각에 있고
    (첫 프레임은 칸 끝이다), received_at이 frame_ts와 같다.
    """
    seq = itertools.count(1)
    frames = []
    for k, by_anchor in raw_bins.items():
        for anchor_id, values in by_anchor.items():
            for j, value in enumerate(values):
                ts = WINDOW_START + (k + 1) * timedelta(seconds=bin_sec) - j * timedelta(seconds=bin_sec * 0.1)
                frames.append(make_frame(anchor_id, value, ts, beacon=beacon, seq=next(seq), received_at=ts))
    return frames


def compute(
    frames,
    *,
    model: CommonModel = MODEL,
    beacon: BeaconKey = BEACON,
    place_nodes: list[NodeInfo] | None = None,
    candidates: list[NodeInfo] | None = None,
    participants: Collection[str] | None = None,
    zeros: Mapping[str, float] | None = None,
    at: datetime = AT,
    bin_sec: float = BIN_SEC,
    calib_version: str = "7",
) -> ProximityResult:
    """기본값은 place-a의 locker 셋이 모두 trusted, 후보, 근접 참여 기기이고 자동 영점이 하나도 없는 호출이다."""
    nodes = nodes_of() if place_nodes is None else place_nodes
    return compute_posteriors(
        model,
        frames,
        beacon,
        nodes if candidates is None else candidates,
        nodes,
        at,
        bin_sec=bin_sec,
        participants=[node.node_id for node in nodes] if participants is None else participants,
        zeros={} if zeros is None else zeros,
        calib_version=calib_version,
    )


def normal_pdf(x: float, mean: float, sigma: float) -> float:
    return math.exp(-0.5 * ((x - mean) / sigma) ** 2) / (sigma * math.sqrt(2.0 * math.pi))


def reference_posteriors(
    model: CommonModel, anchor_ids: Sequence[str], observed: Mapping[int, Mapping[str, float]], n_bins: int,
) -> tuple[dict[str, float], float]:
    """확률 공간의 HMM forward. 구현(로그 공간)과 따로 쓴 기준값이다.

    observed는 칸 번호 → {앵커: 보정한 평균 RSSI}다. 상태는 anchor_ids의 기기 앞 상태들과 "어느 기기 앞도 아님"이다.
    기기마다의 확률과 "어느 기기 앞도 아님"의 확률을 돌려준다.
    """
    k_states = len(anchor_ids) + 1
    belief = [1.0 / k_states] * k_states
    for k in range(n_bins):
        if k_states > 1:
            move = (1.0 - model.stay) / (k_states - 1)
            belief = [sum(belief[i] * (model.stay if i == j else move) for i in range(k_states)) for j in range(k_states)]
        evidence = observed.get(k, {})
        if evidence:
            likelihood = []
            for state in range(k_states):
                density = 1.0
                for anchor_id, rssi in evidence.items():
                    if state == k_states - 1:
                        role = model.none
                    elif anchor_ids[state] == anchor_id:
                        role = model.front
                    else:
                        role = model.other
                    density *= normal_pdf(rssi, role.mean, role.sigma)
                likelihood.append(density)
            peak = max(likelihood)  # 값이 너무 작아져도 같은 비율로 키워서 정규화 뒤 결과가 같다
            belief = [b * (density / peak) ** (1.0 / model.calibration_tau) for b, density in zip(belief, likelihood)]
        total = sum(belief)
        belief = [b / total for b in belief]
    return dict(zip(anchor_ids, belief)), belief[-1]


def close(expected: Mapping[str, float]):
    return pytest.approx(dict(expected), rel=1e-9, abs=1e-300)


def check_against_reference(
    raw_bins: Mapping[int, Mapping[str, Sequence[float]]],
    *,
    model: CommonModel = MODEL,
    bin_sec: float = BIN_SEC,
    ids: Sequence[str] = LOCKERS,
    revoked: Collection[str] = (),
    participants: Collection[str] | None = None,
    zeros: Mapping[str, float] | None = None,
    candidate_ids: Sequence[str] | None = None,
) -> ProximityResult:
    """같은 관측으로 `compute_posteriors`와 기준 계산을 돌려 후보의 확률이 같은지 본다. 결과를 돌려준다."""
    nodes = nodes_of(ids, revoked=revoked)
    participants = list(ids) if participants is None else participants
    zeros = {} if zeros is None else zeros
    trusted = [node_id for node_id in ids if node_id not in revoked]
    chosen = trusted if candidate_ids is None else candidate_ids

    result = compute(
        frames_from_bins(raw_bins, bin_sec=bin_sec),
        model=model,
        place_nodes=nodes,
        candidates=[node for node in nodes if node.node_id in chosen],
        participants=participants,
        zeros=zeros,
        bin_sec=bin_sec,
    )

    anchors = sorted(set(trusted) & set(participants))
    observed = {
        k: {
            anchor_id: math.fsum(values) / len(values) - zeros.get(anchor_id, 0.0)
            for anchor_id, values in by_anchor.items()
            if anchor_id in anchors
        }
        for k, by_anchor in raw_bins.items()
    }
    expected, _ = reference_posteriors(model, anchors, observed, round(model.window_sec / bin_sec))
    assert dict(result.posteriors) == close({node_id: expected[node_id] for node_id in anchors if node_id in chosen})
    return result


# ── 전체 상태는 근접 참여 기기다 ────────────────────────────────────


def test_FR16_full_state_is_participants():
    nodes = nodes_of()  # trusted 셋
    participants = {"locker-1", "locker-2"}  # locker-3은 보고하지 않았다
    frames = standing_frames("locker-1")  # 셋 모두의 프레임이 있다

    result = compute(frames, place_nodes=nodes, candidates=nodes, participants=participants)

    assert set(result.posteriors) == participants  # locker-3은 후보여도 결과에 없다

    # 비참여 기기의 프레임은 쓰지 않는다. 그 프레임을 빼도 결과가 같다.
    without = [frame for frame in frames if frame.node_id != "locker-3"]
    assert compute(without, place_nodes=nodes, candidates=nodes, participants=participants) == result
    # 상태는 참여 기기 둘과 "어느 기기 앞도 아님" 셋이다. 셋 모두를 상태로 쓰면 이 값이 나오지 않는다.
    heard = {"locker-1": ideal_rssi("locker-1", "locker-1"), "locker-2": ideal_rssi("locker-2", "locker-1")}
    expected, _ = reference_posteriors(MODEL, ["locker-1", "locker-2"], {k: heard for k in range(3)}, 3)
    assert dict(result.posteriors) == close(expected)


def test_FR16_no_participant_empty_posteriors():
    frames = standing_frames("locker-1")

    # 참여 기기가 없으면 결과가 빈 표다. 예외가 아니다.
    for participants in ((), set(), ["locker-9"]):  # locker-9는 장소에 없는 기기다
        result = compute(frames, participants=participants)
        assert dict(result.posteriors) == {}
        assert result.unzeroed == frozenset()
        assert (result.model_version, result.calib_version) == ("syn-common-1", "7")
        assert (result.window_start, result.window_end) == (WINDOW_START, AT)

    # 참여한 기기가 모두 trusted가 아니어도 같다
    revoked = nodes_of(revoked=LOCKERS)
    assert dict(compute(frames, place_nodes=revoked, candidates=[]).posteriors) == {}
    # 후보가 없어도 오류가 아니다
    assert dict(compute(frames, candidates=[]).posteriors) == {}


def test_FR16_untrusted_participant_is_not_a_state():
    nodes = nodes_of(revoked={"locker-3"})
    frames = standing_frames("locker-1")  # locker-3도 보고했지만 신뢰가 해제됐다

    result = compute(frames, place_nodes=nodes, candidates=nodes_of(("locker-1", "locker-2")))

    assert set(result.posteriors) == {"locker-1", "locker-2"}
    without = [frame for frame in frames if frame.node_id != "locker-3"]
    assert result == compute(without, place_nodes=nodes_of(("locker-1", "locker-2")))  # 상태도 둘뿐이다


def test_FR16_participants_outside_place_are_ignored():
    frames = standing_frames("locker-1")
    expected = compute(frames, participants=LOCKERS)

    # 장소에 없는 기기와 장소의 다른 종류 기기를 더해도 오류가 아니고 결과가 같다
    assert compute(frames, participants=[*LOCKERS, "locker-9", "light-1"]) == expected
    assert compute([*frames, *standing_frames("locker-1", ("locker-9",))], participants=[*LOCKERS, "locker-9"]) == expected


# ── 후보를 다시 정규화하지 않는다 ───────────────────────────────────


def test_FR16_posteriors_not_renormalized():
    # 어느 기기 앞인지 애매한 값이다. 상태 넷(locker 셋 앞, 없음)이 확률을 나눠 가지고, 절반쯤은 "어느 기기 앞도 아님"에 있다.
    raw = {k: {"locker-1": [-67.5], "locker-2": [-68.0], "locker-3": [-74.0]} for k in range(3)}
    full = check_against_reference(raw)
    _, p_none = reference_posteriors(MODEL, list(LOCKERS), {k: {a: v[0] for a, v in raw[k].items()} for k in raw}, 3)

    assert 0.05 < p_none < 0.95  # "어느 기기 앞도 아님"이 확률을 꽤 가졌다
    assert sum(full.posteriors.values()) < 1.0
    # "어느 기기 앞도 아님"의 확률은 어느 후보에도 더하지 않는다
    assert math.fsum(full.posteriors.values()) + p_none == pytest.approx(1.0, rel=1e-12)

    # 일부 후보만 넘겨도 확률은 전체 계산과 같다. 후보끼리 다시 나누면 하나뿐인 후보는 1이 된다.
    for node_id in LOCKERS:
        subset = check_against_reference(raw, candidate_ids=[node_id])
        assert dict(subset.posteriors) == {node_id: full.posteriors[node_id]}
        assert subset.posteriors[node_id] < 0.95
    pair = check_against_reference(raw, candidate_ids=["locker-1", "locker-2"])
    assert dict(pair.posteriors) == {node_id: full.posteriors[node_id] for node_id in ("locker-1", "locker-2")}
    assert sum(pair.posteriors.values()) < 1.0


def test_FR16_none_evidence_leaves_candidates_almost_nothing():
    result = compute(standing_frames(None))

    assert sum(result.posteriors.values()) < 1e-3  # 확률 대부분은 "어느 기기 앞도 아님"에 있다. 후보에 더하지 않는다


# ── 자동 영점 ────────────────────────────────────────────────────────


def test_FR16_unzeroed_marks_candidates():
    nodes = nodes_of()
    frames = standing_frames("locker-1")

    # 자동 영점이 없는 참여 후보가 unzeroed에 있다. 영점 0.0도 영점이다.
    result = compute(frames, place_nodes=nodes, candidates=nodes, zeros={"locker-1": 0.0, "locker-3": 2.0})
    assert result.unzeroed == frozenset({"locker-2"})
    assert set(result.conservative) == {"locker-2"}  # 보수적 사후확률의 기기는 unzeroed와 같다
    assert compute(frames, zeros={}).unzeroed == frozenset(LOCKERS)
    assert compute(frames, zeros={node_id: 0.0 for node_id in LOCKERS}).unzeroed == frozenset()

    # unzeroed는 결과의 후보 중에서만 고른다. 비참여 후보, 후보가 아닌 기기, 장소 밖 기기의 영점은 영향이 없다.
    partial = compute(
        frames, place_nodes=nodes, candidates=nodes_of(("locker-1", "locker-2")),
        participants={"locker-1", "locker-3"}, zeros={"locker-3": 1.0, "locker-9": 1.0},
    )
    assert set(partial.posteriors) == {"locker-1"}
    assert partial.unzeroed == frozenset({"locker-1"})

    # 영점은 보정 전 RSSI에서 빠진다. locker-2가 영점 +10 dB로 늘 10 dB 세게 듣는다면,
    # 그 값에서 영점을 뺀 확률은 영점 없이 10 dB 약하게 들은 프레임의 확률과 같다.
    raw = standing_frames("locker-1", offsets={"locker-2": 10.0})
    plain = standing_frames("locker-1")
    corrected = compute(raw, zeros={"locker-2": 10.0})
    assert dict(corrected.posteriors) == close(compute(plain).posteriors)
    assert corrected.unzeroed == frozenset({"locker-1", "locker-3"})
    # 영점을 빼지 않으면 locker-2의 보정 전 값이 센 그대로 들어가서 확률이 달라진다
    assert compute(raw, zeros={}).posteriors["locker-2"] > compute(plain).posteriors["locker-2"]


@pytest.mark.parametrize("zero_db", [10.0, -6.0, 0.5])
@pytest.mark.parametrize("device_id", LOCKERS)
def test_FR16_zero_shifts_only_that_device(device_id, zero_db):
    # 기기마다 늘 일정하게 다르게 들어도, 그 영점을 알면 영점 없는 기기와 같은 확률이 나온다
    ideal = compute(standing_frames("locker-2"))
    raw = standing_frames("locker-2", offsets={device_id: zero_db})

    assert dict(compute(raw, zeros={device_id: zero_db}).posteriors) == close(ideal.posteriors)
    assert dict(compute(raw, zeros={}).posteriors) != close(ideal.posteriors)  # 영점을 모르면 다르다


# ── 자동 영점이 없는 기기의 보수적 사후확률 ─────────────────────────

HOT_MODEL = replace(MODEL, calibration_method="temperature", calibration_tau=8.0)  # 증거를 약하게 해서 확률이 0이나 1에 붙지 않게 한다
# 칸마다 값이 크게 어긋난 관측이다. 이 관측에서는 가장 불리한 영점으로 다시 계산한 확률이 오히려 사후확률보다 커진다 (locker-1)
ODD_MODEL = replace(MODEL, calibration_method="temperature", calibration_tau=2.0)
ODD_BINS = {
    0: {"locker-1": [-41.6], "locker-2": [-75.1], "locker-3": [-64.6]},
    1: {"locker-1": [-81.7], "locker-2": [-89.4], "locker-3": [-59.6]},
    2: {"locker-1": [-55.0], "locker-2": [-86.4], "locker-3": [-81.7]},
}


def test_FR16_conservative_posterior_is_lower_bound():
    frames = standing_frames("locker-1")
    result = compute(frames, model=HOT_MODEL, zeros={"locker-3": 2.0})

    # 보수적 사후확률은 자동 영점이 없는 후보마다 있고, 사후확률보다 크지 않다
    assert result.unzeroed == frozenset({"locker-1", "locker-2"})
    assert set(result.conservative) == result.unzeroed
    for node_id in result.unzeroed:
        assert 0.0 <= result.conservative[node_id] <= result.posteriors[node_id]
    assert result.conservative["locker-1"] < result.posteriors["locker-1"]  # Δ가 0보다 크면 실제로 낮아진다
    # 자동 영점이 모두 있으면 보수적 사후확률이 없다
    assert dict(compute(frames, model=HOT_MODEL, zeros={node_id: 0.5 for node_id in LOCKERS}).conservative) == {}

    # Δ = 0이면 어떤 영점이든 0이라서 사후확률과 같다
    exact = compute(frames, model=replace(HOT_MODEL, auto_zero_bound_db=0.0))
    assert dict(exact.conservative) == close(exact.posteriors)

    # Δ를 키우면 커지지 않는다. 사후확률은 Δ와 상관없다
    results = [
        compute(frames, model=replace(HOT_MODEL, auto_zero_bound_db=bound)) for bound in (0.0, 1.0, 3.0, 6.0, 12.0)
    ]
    assert all(r.posteriors == results[0].posteriors for r in results)
    for node_id in LOCKERS:
        values = [r.conservative[node_id] for r in results]
        assert all(later <= earlier for earlier, later in zip(values, values[1:]))
        assert values[-1] < values[0]

    # 관측이 어긋나서 다시 계산한 확률이 사후확률보다 커져도 보수적 사후확률은 사후확률 이하다
    odd_frames = frames_from_bins(ODD_BINS)
    odd = compute(odd_frames, model=ODD_MODEL)
    worst = {"locker-1": 6.0, "locker-2": -6.0, "locker-3": -6.0}
    assert compute(odd_frames, model=ODD_MODEL, zeros=worst).posteriors["locker-1"] > odd.posteriors["locker-1"]
    assert odd.conservative["locker-1"] == odd.posteriors["locker-1"]
    assert all(odd.conservative[node_id] <= odd.posteriors[node_id] for node_id in odd.unzeroed)


@pytest.mark.parametrize(
    ("model", "zeros", "candidate_ids"),
    [
        pytest.param(HOT_MODEL, {}, LOCKERS, id="no_zero_found"),
        pytest.param(HOT_MODEL, {"locker-3": 2.0}, LOCKERS, id="one_zero_found"),
        pytest.param(HOT_MODEL, {}, ("locker-1", "locker-2"), id="non_candidate_anchor"),
        pytest.param(MODEL, {"locker-2": -3.0}, LOCKERS, id="no_temperature"),
    ],
)
def test_FR16_conservative_posterior_uses_worst_offsets(model, zeros, candidate_ids):
    # locker-1과 locker-2 사이에서 애매한 관측이다
    raw = {k: {"locker-1": [-58.0], "locker-2": [-67.0], "locker-3": [-75.0]} for k in range(3)}
    frames = frames_from_bins(raw)
    candidates = [node for node in nodes_of() if node.node_id in candidate_ids]
    bound = model.auto_zero_bound_db
    result = compute(frames, model=model, candidates=candidates, zeros=zeros)
    assert result.unzeroed == frozenset(candidate_ids) - set(zeros)

    for device_id in result.unzeroed:
        # 영점을 찾은 앵커는 그대로다. device_id는 +Δ이고, 영점이 없는 다른 상태 앵커는 후보가 아니어도 −Δ다
        worst = {**zeros, **{a: (bound if a == device_id else -bound) for a in LOCKERS if a not in zeros}}
        # 그 영점을 직접 넣은 명목 계산과 같다
        explicit = compute(frames, model=model, candidates=candidates, zeros=worst).posteriors[device_id]
        assert result.conservative[device_id] == pytest.approx(
            min(result.posteriors[device_id], explicit), rel=1e-9, abs=1e-300)
        assert result.conservative[device_id] < result.posteriors[device_id]
        # 따로 쓴 기준 계산과도 같다
        observed = {k: {a: raw[k][a][0] - worst[a] for a in LOCKERS} for k in raw}
        reference = reference_posteriors(model, list(LOCKERS), observed, 3)[0][device_id]
        assert explicit == pytest.approx(reference, rel=1e-9, abs=1e-300)


def test_FR16_single_device_strong_signal_passes_conservatively():
    # 기기 한 대뿐인 장소다. 그 기기 앞의 강한 신호(모델의 front 평균 근처)는 자동 영점이 없어도 보수적 사후확률이 0.95 이상이다
    nodes = nodes_of(("locker-1",))
    threshold = 0.95

    def run(rssi: float, zeros: Mapping[str, float] | None = None) -> ProximityResult:
        frames = frames_from_bins({k: {"locker-1": [rssi]} for k in range(3)})
        return compute(frames, place_nodes=nodes, candidates=nodes, zeros=zeros)

    for rssi in (MODEL.front.mean, MODEL.front.mean + 2.0):
        result = run(rssi)
        assert result.unzeroed == frozenset({"locker-1"})
        assert result.conservative["locker-1"] >= threshold
        assert result.threshold_probability("locker-1") == result.conservative["locker-1"]

    # 신호가 약하면 사후확률은 0.95를 넘어도 보수적 사후확률은 넘지 못한다. 영점을 알면 사후확률로 넘는다
    weak = run(MODEL.front.mean - 5.0)
    assert weak.posteriors["locker-1"] >= threshold > weak.conservative["locker-1"]
    known = run(MODEL.front.mean - 5.0, zeros={"locker-1": 0.0})
    assert known.unzeroed == frozenset()
    assert known.threshold_probability("locker-1") == known.posteriors["locker-1"] >= threshold


# ── 확률의 값 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("device_id", LOCKERS)
def test_FR16_front_device_has_highest_posterior(device_id):
    result = compute(standing_frames(device_id))

    assert result.best is not None
    best_id, best_p = result.best
    assert best_id == device_id
    assert best_p > 0.99


def test_FR16_no_evidence_is_uniform():
    # 증거가 없으면 처음 분포(균등)가 그대로다. 상태는 참여 기기와 "어느 기기 앞도 아님"이다.
    assert dict(compute([]).posteriors) == close({node_id: 1 / 4 for node_id in LOCKERS})
    assert dict(compute([], participants={"locker-1", "locker-2"}).posteriors) == close({"locker-1": 1 / 3, "locker-2": 1 / 3})
    assert dict(compute([], participants={"locker-1"}).posteriors) == close({"locker-1": 1 / 2})


def test_FR16_temperature_weakens_evidence():
    frames = standing_frames("locker-1")
    plain = compute(frames)
    one = compute(frames, model=replace(MODEL, calibration_method="temperature", calibration_tau=1.0))
    hot = compute(frames, model=replace(MODEL, calibration_method="temperature", calibration_tau=8.0))

    assert dict(one.posteriors) == close(plain.posteriors)  # τ = 1은 보정하지 않은 것과 같다
    assert hot.posteriors["locker-1"] < plain.posteriors["locker-1"]  # 증거의 로그우도를 τ로 나눠서 증거가 약해진다
    assert hot.posteriors["locker-2"] > plain.posteriors["locker-2"]
    assert hot.best is not None and hot.best[0] == "locker-1"  # 순서는 그대로다
    # 기준 계산과 같다
    observed = {k: {node_id: ideal_rssi(node_id, "locker-1") for node_id in LOCKERS} for k in range(3)}
    hot_model = replace(MODEL, calibration_method="temperature", calibration_tau=8.0)
    assert dict(hot.posteriors) == close(reference_posteriors(hot_model, list(LOCKERS), observed, 3)[0])


@pytest.mark.parametrize(
    ("raw_bins", "zeros"),
    [
        pytest.param(
            {0: {"locker-1": [-56.0], "locker-2": [-71.0], "locker-3": [-73.0]},
             1: {"locker-1": [-54.0], "locker-2": [-70.0], "locker-3": [-72.5]},
             2: {"locker-1": [-57.0], "locker-2": [-72.0], "locker-3": [-70.0]}},
            {},
            id="visitor_at_locker_1",
        ),
        pytest.param(
            {0: {"locker-1": [-55.0], "locker-2": [-72.0], "locker-3": [-72.0]},
             1: {"locker-1": [-55.0], "locker-2": [-72.0], "locker-3": [-72.0]},
             2: {"locker-1": [-72.0], "locker-2": [-55.0], "locker-3": [-72.0]}},
            {},
            id="moves_to_locker_2",
        ),
        pytest.param(
            {0: {"locker-1": [-55.0], "locker-2": [-72.0], "locker-3": [-72.0]},
             2: {"locker-1": [-80.0], "locker-2": [-80.0], "locker-3": [-80.0]}},
            {},
            id="no_frames_in_middle_bin",
        ),
        pytest.param(
            {0: {"locker-1": [-50.0, -60.0], "locker-2": [-72.0]},
             1: {"locker-3": [-70.0, -74.0, -66.0]},
             2: {"locker-1": [-58.0], "locker-2": [-69.0, -75.0]}},
            {},
            id="two_frames_in_a_bin_and_missing_anchors",
        ),
        pytest.param(
            {0: {"locker-1": [-80.0], "locker-2": [-79.0], "locker-3": [-81.0]},
             1: {"locker-1": [-82.0], "locker-2": [-78.0], "locker-3": [-80.0]},
             2: {"locker-1": [-79.0], "locker-2": [-80.0], "locker-3": [-83.0]}},
            {},
            id="nobody_in_front",
        ),
        pytest.param(
            {0: {"locker-1": [-45.0], "locker-2": [-72.0], "locker-3": [-70.0]},
             1: {"locker-1": [-44.0], "locker-2": [-71.0], "locker-3": [-69.0]},
             2: {"locker-1": [-46.0], "locker-2": [-73.0], "locker-3": [-68.0]}},
            {"locker-1": 10.0, "locker-3": -2.0},
            id="with_zeros",
        ),
    ],
)
@pytest.mark.parametrize(
    "model",
    [
        pytest.param(MODEL, id="no_calibration"),
        pytest.param(replace(MODEL, calibration_method="temperature", calibration_tau=0.5), id="tau_0_5"),
        pytest.param(replace(MODEL, calibration_method="temperature", calibration_tau=3.0, stay=0.6), id="tau_3_stay_0_6"),
    ],
)
def test_FR16_posteriors_match_reference_forward(raw_bins, zeros, model):
    check_against_reference(raw_bins, model=model, zeros=zeros)


@pytest.mark.parametrize("bin_sec", [0.5, 0.3])
def test_FR16_posteriors_follow_bin_sec(bin_sec):
    # 칸이 짧아지면 칸 수가 늘어난다 (window_sec / bin_sec). 방문자가 중간에 locker-1에서 locker-2로 옮긴다.
    n_bins = round(MODEL.window_sec / bin_sec)
    raw = {}
    for k in range(n_bins):
        at_first = k < n_bins // 2
        raw[k] = {
            "locker-1": [-55.0 if at_first else -72.0],
            "locker-2": [-72.0 if at_first else -55.0],
            "locker-3": [-72.0],
        }

    result = check_against_reference(raw, bin_sec=bin_sec)

    assert result.best is not None and result.best[0] == "locker-2"


@pytest.mark.parametrize("seed", range(12))
def test_FR16_posteriors_match_reference_for_random_scenarios(seed):
    rng = random.Random(seed)
    ids = tuple(rng.sample(ALL_IDS, rng.randint(2, 6)))
    revoked = set(rng.sample(ids, rng.randint(0, 1)))
    participants = rng.sample(ids, rng.randint(0, len(ids)))  # 비참여 기기와 신뢰 해제 기기도 보고한다
    bin_sec = rng.choice([1.0, 0.5, 0.3])
    model = replace(
        MODEL,
        stay=rng.choice([0.6, 0.9, 0.97]),
        calibration_method="temperature",
        calibration_tau=rng.choice([0.5, 1.0, 2.0]),
        front=RoleParams(-56.0, 3.5),
        other=RoleParams(-71.0, 5.0),
        none=RoleParams(-79.0, 7.0),
    )
    raw: dict[int, dict[str, list[float]]] = {}
    for k in range(round(model.window_sec / bin_sec)):
        if rng.random() < 0.25:
            continue  # 프레임이 없는 칸
        raw[k] = {
            node_id: [rng.uniform(-85.0, -50.0) for _ in range(rng.randint(1, 3))] for node_id in ids if rng.random() < 0.7
        }
    zeros = {node_id: rng.uniform(-8.0, 8.0) for node_id in ids if rng.random() < 0.5}
    candidate_ids = [node_id for node_id in ids if node_id not in revoked and rng.random() < 0.7]

    result = check_against_reference(
        raw, model=model, bin_sec=bin_sec, ids=ids, revoked=revoked, participants=participants, zeros=zeros,
        candidate_ids=candidate_ids,
    )

    anchors = {node_id for node_id in participants if node_id not in revoked}
    assert set(result.posteriors) == {node_id for node_id in candidate_ids if node_id in anchors}
    assert result.unzeroed == frozenset(node_id for node_id in result.posteriors if node_id not in zeros)


# ── 프레임 선택 ──────────────────────────────────────────────────────

PARTICIPANTS = {"locker-1", "locker-2"}
USED_NOTHING = [
    pytest.param(make_frame("locker-1", -55.0, AT, beacon=OTHER_BEACON, received_at=AT), id="other_beacon"),
    pytest.param(make_frame("locker-1", -55.0, WINDOW_START, received_at=WINDOW_START), id="at_window_start"),
    pytest.param(make_frame("locker-1", -55.0, WINDOW_START - timedelta(seconds=1), received_at=AT), id="before_window"),
    pytest.param(
        make_frame("locker-1", -55.0, AT + timedelta(milliseconds=1), received_at=AT + timedelta(seconds=1)), id="after_at",
    ),
    pytest.param(make_frame("locker-1", -55.0, AT, received_at=AT + timedelta(milliseconds=1)), id="received_after_at"),
    pytest.param(make_frame("locker-1", math.nan, AT, received_at=AT), id="nan"),
    pytest.param(make_frame("locker-1", math.inf, AT, received_at=AT), id="infinite"),
    pytest.param(make_frame("locker-1", -math.inf, AT, received_at=AT), id="negative_infinite"),
    pytest.param(make_frame("locker-3", -55.0, AT, received_at=AT), id="not_participant"),
    pytest.param(make_frame("locker-9", -55.0, AT, received_at=AT), id="not_in_place"),
]
USED = [
    pytest.param(make_frame("locker-1", -55.0, AT, received_at=AT), id="at_at"),
    pytest.param(
        make_frame("locker-1", -55.0, WINDOW_START + timedelta(microseconds=1), received_at=AT), id="just_after_window_start",
    ),
    pytest.param(make_frame("locker-1", -55.0, AT, received_at=AT, session_id="s-other"), id="other_session"),
]


@pytest.mark.parametrize("frame", USED_NOTHING)
def test_FR16_posteriors_skip_unusable_frames(frame):
    expected = compute([], participants=PARTICIPANTS)

    assert compute([frame], participants=PARTICIPANTS) == expected  # 쓰지 않은 프레임은 증거 없음과 같다


@pytest.mark.parametrize("frame", USED)
def test_FR16_posteriors_use_frames_in_window(frame):
    uniform = compute([], participants=PARTICIPANTS)
    result = compute([frame], participants=PARTICIPANTS)

    assert result.posteriors["locker-1"] > uniform.posteriors["locker-1"]  # (window_start, at]의 프레임은 증거다


def test_FR16_posteriors_count_duplicate_frame_once():
    base = standing_frames("locker-1")
    expected = compute(base)
    # 같은 (node_id, session_id, seq, beacon)을 1 ms 늦게 받았고 값이 다르다. 처음 하나만 쓴다.
    # at 뒤에 받은 것은 중복 제거 전에 빠지므로, at 이전에 받은 중복만 남겨서 중복 제거를 본다.
    duplicates = [
        replace(frame, rssi=-80.0, received_at=frame.received_at + timedelta(milliseconds=1)) for frame in base
        if frame.received_at + timedelta(milliseconds=1) <= AT
    ]

    assert duplicates  # at 이전에 받은 프레임이 있다
    assert compute([*duplicates, *base]) == expected
    assert compute([*base, *duplicates]) == expected


def test_FR16_posteriors_ignore_input_order_and_collection_types():
    rng = random.Random(3)
    frames = [*standing_frames("locker-2"), *standing_frames("locker-2", first_seq=100, last_ts=AT - timedelta(seconds=1))]
    expected = compute(frames)

    for _ in range(5):
        shuffled = list(frames)
        rng.shuffle(shuffled)
        assert compute(shuffled) == expected
    assert compute(iter(frames)) == expected  # 한 번만 읽는 iterable도 된다
    for participants in (LOCKERS, list(LOCKERS), set(LOCKERS), frozenset(LOCKERS), dict.fromkeys(LOCKERS)):
        assert compute(frames, participants=participants) == expected


# ── 입력 검사 ────────────────────────────────────────────────────────

INVALID_INPUTS = [
    pytest.param({"bin_sec": 0.0}, "bin_sec", id="bin_sec_zero"),
    pytest.param({"bin_sec": -1.0}, "bin_sec", id="bin_sec_negative"),
    pytest.param({"bin_sec": math.nan}, "bin_sec", id="bin_sec_nan"),
    pytest.param({"place_nodes": [], "candidates": []}, "비어 있다", id="no_place_nodes"),
    pytest.param(
        {"place_nodes": [make_node("locker-1", "place-a"), make_node("locker-2", "place-b")], "candidates": []},
        "한 장소",
        id="two_places",
    ),
    pytest.param({"place_nodes": [make_node("locker-1", None)], "candidates": []}, "한 장소", id="no_place_id"),
    pytest.param(
        {"place_nodes": [make_node("locker-1"), make_node("locker-1")], "candidates": []}, "같은 node_id", id="duplicate_node_id",
    ),
    pytest.param({"candidates": [make_node("locker-9")]}, "place_nodes에 없다", id="candidate_not_in_place_nodes"),
    pytest.param(
        {"candidates": [make_node("locker-1", "place-b")]}, "장소가 place_nodes와 다르다", id="candidate_in_other_place",
    ),
    pytest.param(
        {"candidates": [make_node("locker-1", trust=TrustState.REVOKED)]}, "trusted가 아니다", id="candidate_not_trusted",
    ),
    pytest.param(
        {
            "candidates": [make_node("locker-1")],
            "place_nodes": [make_node("locker-1", trust=TrustState.DISCOVERED), make_node("locker-2")],
        },
        "place_nodes에서는",
        id="place_nodes_disagree_on_trust",
    ),
]


@pytest.mark.parametrize(("kwargs", "message"), INVALID_INPUTS)
def test_FR16_posteriors_reject_inconsistent_inputs(kwargs, message):
    with pytest.raises(ProximityInputError, match=message):
        compute(standing_frames("locker-1"), **kwargs)


# ── 결과와 이름 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("place_id", ["place-a", "place-b", "rooftop-7"])
def test_FR16_model_has_no_place(place_id):
    # 모델에 장소가 없어서 같은 모델이 어느 장소의 기기에도 쓰인다. 장소가 모델과 다르다는 오류가 없다.
    frames = standing_frames("locker-2")
    result = compute(frames, place_nodes=nodes_of(place_id=place_id))

    assert result.best is not None and result.best[0] == "locker-2"
    assert result == compute(frames)


def test_FR16_result_carries_versions_and_window():
    result = compute(standing_frames("locker-1"), calib_version="12")

    assert result.model_version == "syn-common-1"
    assert result.calib_version == "12"  # 판정에 적용한 자동 영점 개정. 인자 값 그대로다
    assert (result.window_start, result.window_end) == (AT - timedelta(seconds=3.0), AT)
    assert compute(standing_frames("locker-1"), calib_version="0").calib_version == "0"


def test_FR16_node_named_like_none_state_is_not_confused_with_it():
    # 기기 ID가 "어느 기기 앞도 아님"의 내부 이름과 같아도, 그 기기의 확률은 "그 기기 앞" 상태의 확률이다
    odd = make_node(NONE_STATE, capability="light")
    nodes = [make_node("locker-1"), odd]

    result = compute(standing_frames(None, ("locker-1", NONE_STATE)), place_nodes=nodes, participants=["locker-1", NONE_STATE])

    assert set(result.posteriors) == {"locker-1", NONE_STATE}
    assert result.posteriors[NONE_STATE] < 0.01  # 모두 none 값을 들었다. "어느 기기 앞도 아님"의 확률(거의 1)이 아니다.


def test_FR16_posterior_public_names():
    assert NONE_STATE == "__none__"
    assert issubclass(ProximityInputError, ValueError)
