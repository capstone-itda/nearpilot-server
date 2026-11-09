"""자동 영점을 찾는 순수 함수 (FR-16). 손으로 만든 칸 값과 프레임으로 확인한다."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.proximity.autozero import bin_index, bin_samples, zero_from_samples, zeros_from_frames
from nearpilot.shared.models import BeaconKey, RssiFrame

from .conftest import BEACON, OTHER_BEACON, T0, make_frame

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ANCHORS = ("a", "b", "c")
THREE = {"a": -50.0, "b": -60.0, "c": -70.0}  # a가 1등. 1등이 아닌 b, c의 평균 -65에서 b = 5, c = -5
THREE_SAMPLES = {"b": 5.0, "c": -5.0}


def frames_for(
    bins: Sequence[Mapping[str, float]],
    *,
    beacon: BeaconKey = BEACON,
    start: datetime = T0,
    first_seq: int = 1,
    offset_sec: float = 0.5,
) -> list[RssiFrame]:
    """칸 k의 {앵커: RSSI}마다 프레임 하나를 만든다. 프레임은 `start + k초 + offset_sec`에 있어서 `bin_sec=1.0`의 칸 하나에 든다.

    `start`는 정수 초여야 칸이 초 단위로 떨어진다. 앵커마다 `seq`는 `first_seq + k`다.
    """
    frames = []
    for k, values in enumerate(bins):
        ts = start + timedelta(seconds=k + offset_sec)
        for node_id, rssi in values.items():
            frames.append(make_frame(node_id, rssi, ts, beacon=beacon, seq=first_seq + k))
    return frames


def find_zeros(frames, anchors=ANCHORS, *, min_bins: int = 20) -> dict[str, float]:
    return zeros_from_frames(frames, anchors, bin_sec=1.0, min_bins=min_bins)


def weak_device_bins(rng: random.Random, n_bins: int = 60) -> list[dict[str, float]]:
    """앵커 다섯. c만 늘 6 dB 약하게 듣는다. 방문자는 a 앞과 b 앞을 번갈아 선다 (앞 -55, 옆 -72, 잡음 1.5 dB)."""
    bins = []
    for k in range(n_bins):
        front = "a" if k % 2 == 0 else "b"
        bins.append({
            node_id: (-55.0 if node_id == front else -72.0) + (-6.0 if node_id == "c" else 0.0) + rng.gauss(0.0, 1.5)
            for node_id in ("a", "b", "c", "d", "e")
        })
    return bins


# ── 표본에서 자동 영점 ──────────────────────────────────────────────


def test_FR16_auto_zero_needs_min_bins():
    assert zero_from_samples([-6.0] * 19, 20) is None
    assert zero_from_samples([-6.0] * 20, 20) == -6.0
    assert zero_from_samples([], 20) is None
    assert zero_from_samples([float(i) for i in range(20)], 20) == 9.5  # 짝수 개: 가운데 두 값 9, 10의 평균

    # 프레임으로도 같다. 칸 19개는 찾지 않고 20개는 찾는다.
    bins = [THREE] * 20
    assert find_zeros(frames_for(bins[:19])) == {}
    assert find_zeros(frames_for(bins)) == THREE_SAMPLES


def test_FR16_auto_zero_is_median_of_samples():
    assert zero_from_samples([-6.0], 1) == -6.0
    assert zero_from_samples([3.0, 1.0, 2.0], 3) == 2.0  # 홀수 개: 가운데 값
    assert zero_from_samples([1.0, 2.0, 3.0, 10.0], 4) == 2.5  # 짝수 개: 가운데 두 값의 평균
    assert zero_from_samples([1.0, 2.0, 3.0], 2) == 2.0  # min_bins를 넘어도 표본을 모두 쓴다
    assert zero_from_samples([-6.0] * 19 + [40.0], 20) == -6.0  # 평균과 달리 큰 이상치에 흔들리지 않는다
    assert zero_from_samples([40.0] + [-6.0] * 19, 20) == zero_from_samples([-6.0] * 19 + [40.0], 20)  # 순서와 상관없다
    assert type(zero_from_samples([1, 2, 3], 3)) is float  # 정수 표본도 float로 돌려준다


# ── 칸 하나의 표본 ──────────────────────────────────────────────────


def test_FR16_auto_zero_bin_needs_two_anchors():
    # 1등이 아닌 앵커가 둘 이상이어야 표본이 있다
    assert bin_samples({"a": -50.0}) == {}
    assert bin_samples({}) == {}
    assert bin_samples({"a": -50.0, "b": -60.0}) == {}

    # 프레임으로도 같다. 30칸은 세 앵커가 모두 듣고, 그 뒤 100칸은 c만 듣는다. c의 표본은 앞의 30칸뿐이다.
    together = [THREE] * 30
    alone = [{"c": -70.0}] * 100
    frames = frames_for(together) + frames_for(alone, start=T0 + timedelta(seconds=30), first_seq=31)
    assert find_zeros(frames, min_bins=30) == THREE_SAMPLES
    assert find_zeros(frames, min_bins=31) == {}  # 혼자 들은 칸이 표본이었다면 c는 130개를 모았다


def test_FR16_auto_zero_excludes_top_bins():
    # 1등(a)은 표본도 아니고 비교 기준에도 들지 않는다. 1등 값이 아무리 커도 b, c의 표본은 같다
    assert bin_samples(THREE) == THREE_SAMPLES
    assert bin_samples({**THREE, "a": -20.0}) == THREE_SAMPLES
    # 같은 값의 1등은 모두 빠진다. 남은 앵커가 하나면 표본이 없다
    assert bin_samples({"a": -50.0, "b": -50.0, "c": -60.0}) == {}
    assert bin_samples({"a": -50.0, "b": -50.0, "c": -60.0, "d": -70.0}) == {"c": 5.0, "d": -5.0}
    assert bin_samples({"a": -50.0, "b": -50.0}) == {}

    # 프레임으로도 같다. a는 늘 1등이라 표본이 하나도 없어서 결과에 없다.
    assert find_zeros(frames_for([THREE] * 25), min_bins=1) == THREE_SAMPLES
    assert find_zeros(frames_for([{"a": -50.0, "b": -50.0, "c": -60.0, "d": -70.0}] * 25), "abcd", min_bins=1) == {"c": 5.0, "d": -5.0}


def test_FR16_auto_zero_bin_sample_uses_mean_of_non_top_anchors():
    # 기준은 1등이 아닌 앵커들(자기 포함)의 평균이다. 표본의 합은 0이다
    four = bin_samples({"a": -50.0, "b": -55.0, "c": -65.0, "d": -80.0})
    assert four == pytest.approx({"b": 35.0 / 3, "c": 5.0 / 3, "d": -40.0 / 3})
    assert math.fsum(four.values()) == pytest.approx(0.0, abs=1e-12)
    five = {"a": -50.0, "b": -60.0, "c": -62.0, "d": -66.0, "e": -70.0}
    assert bin_samples(five) == {"b": 4.5, "c": 2.5, "d": -1.5, "e": -5.5}


def test_FR16_auto_zero_bin_samples_ignore_input_order():
    five = {"a": -50.0, "b": -60.0, "c": -62.0, "d": -66.0, "e": -70.0}
    before = dict(five)
    reversed_five = dict(reversed(list(five.items())))

    assert bin_samples(reversed_five) == bin_samples(five)
    assert list(bin_samples(reversed_five)) == list(bin_samples(five)) == ["b", "c", "d", "e"]  # 키 순서도 앵커 ID 순서다
    assert five == before  # 입력은 바뀌지 않는다


# ── 칸 번호 ─────────────────────────────────────────────────────────


def test_FR16_auto_zero_bin_index_counts_absolute_bins():
    epoch_s = int(T0.timestamp())  # T0는 정수 초다
    assert bin_index(EPOCH, 1.0) == 0
    assert bin_index(T0, 1.0) == epoch_s
    assert bin_index(T0 + timedelta(microseconds=999_999), 1.0) == epoch_s  # 칸은 [시작, 끝)이다
    assert bin_index(T0 + timedelta(seconds=1), 1.0) == epoch_s + 1
    assert bin_index(T0 - timedelta(microseconds=1), 1.0) == epoch_s - 1
    assert bin_index(EPOCH - timedelta(microseconds=1), 1.0) == -1  # 내림이다
    assert bin_index(T0, 3.5) == epoch_s * 1_000_000 // 3_500_000
    assert bin_index(T0 + timedelta(seconds=1), 1) == epoch_s + 1  # int도 받는다


def test_FR16_auto_zero_bin_index_counts_integer_microseconds():
    # 0.9 // 0.3은 부동소수에서 2.0이다. 정수 마이크로초로 세면 경계의 프레임이 옆 칸으로 가지 않는다.
    assert bin_index(EPOCH + timedelta(microseconds=900_000), 0.3) == 3
    assert bin_index(EPOCH + timedelta(microseconds=899_999), 0.3) == 2
    assert bin_index(EPOCH + timedelta(microseconds=5), 1e-9) == 5  # 1 마이크로초보다 짧은 칸은 1 마이크로초다


def test_FR16_auto_zero_bin_index_ignores_utc_offset():
    kst = timezone(timedelta(hours=9))

    assert bin_index(T0.astimezone(kst), 1.0) == bin_index(T0, 1.0)
    assert bin_index((T0 + timedelta(seconds=0.4)).astimezone(kst), 1.0) == bin_index(T0, 1.0)


@pytest.mark.parametrize("bin_sec", [0.0, -1.0, math.nan, math.inf])
def test_FR16_auto_zero_rejects_bad_bin_sec(bin_sec):
    with pytest.raises(ValueError):
        bin_index(T0, bin_sec)
    with pytest.raises(ValueError):  # 프레임이 없어도 칸 길이는 검사한다
        zeros_from_frames([], ANCHORS, bin_sec=bin_sec, min_bins=1)


# ── 프레임 전체에서 자동 영점 ───────────────────────────────────────


def test_FR16_auto_zero_from_frames_finds_offset_of_weak_device():
    # c만 6 dB 약하다. 자동 영점은 1등이 아닌 앵커들 안에서 가운데를 0으로 둔 값이라, 기기끼리의 차이가 6 dB다.
    anchors = ("a", "b", "c", "d", "e")
    zeros = find_zeros(frames_for(weak_device_bins(random.Random(16))), anchors)

    for node_id in ("a", "b", "d", "e"):
        assert zeros[node_id] - zeros["c"] == pytest.approx(6.0, abs=1.0)


def test_FR16_auto_zero_from_frames_three_anchors_follow_the_formula():
    # 앵커 셋, c만 6 dB 약하다. 1등은 기준에서 빠지므로 1등 값(앞 -55)이 c의 표본에 섞이지 않는다.
    #   a 앞 칸: 1등이 아닌 b=-72, c=-78의 평균 -75 → b: 3, c: -3
    #   b 앞 칸: 1등이 아닌 a=-72, c=-78의 평균 -75 → a: 3, c: -3
    # 보정하면 a, b, c가 같은 값이 된다 (-72-3 = -78+3). 차이가 부풀지 않는다.
    bins = [{"a": -55.0, "b": -72.0, "c": -78.0}, {"a": -72.0, "b": -55.0, "c": -78.0}] * 15

    # a와 b는 1등이 아닌 15칸에서만 표본을 모은다
    assert find_zeros(frames_for(bins), min_bins=15) == {"a": 3.0, "b": 3.0, "c": -3.0}
    assert find_zeros(frames_for(bins)) == {"c": -3.0}  # min_bins 20에는 a와 b의 표본이 모자란다


def test_FR16_auto_zero_from_frames_averages_frames_of_a_bin():
    # c가 한 칸에서 프레임 둘(-78, -80)을 들으면 그 평균 -79가 칸 값이다.
    frames = frames_for([{"a": -50.0, "b": -60.0, "c": -78.0}])
    frames.append(make_frame("c", -80.0, T0 + timedelta(seconds=0.75), seq=100))

    # 1등이 아닌 b=-60, c=-79의 평균 -69.5 → b: 9.5, c: -9.5
    assert find_zeros(frames, min_bins=1) == {"b": 9.5, "c": -9.5}


def test_FR16_auto_zero_from_frames_ignores_unusable_frames():
    base = frames_for([{"a": -50.0, "b": -60.0, "c": -72.0}] * 25)
    expected = find_zeros(base)
    assert expected == {"b": 6.0, "c": -6.0}  # 1등이 아닌 b=-60, c=-72의 평균 -66

    # anchors 밖 기기. 모든 칸에서 가장 센 값이라 앵커로 쓰이면 a가 1등에서 내려와 결과가 바뀐다.
    outsider = frames_for([{"x": -30.0}] * 25)
    # 유한하지 않은 RSSI. 같은 칸의 c 프레임으로 들어가면 칸 평균이 NaN이나 무한대가 된다.
    non_finite = [
        make_frame("c", value, T0 + timedelta(seconds=k + 0.25), seq=1000 + 3 * k + i)
        for k in range(25)
        for i, value in enumerate((math.nan, math.inf, -math.inf))
    ]
    # 같은 (node_id, session_id, seq, beacon)을 더 늦게 받은 중복. 값이 30 dB 세서 쓰이면 c의 칸 평균이 올라간다.
    duplicates = [
        replace(frame, rssi=frame.rssi + 30.0, received_at=frame.received_at + timedelta(milliseconds=5))
        for frame in base
        if frame.node_id == "c"
    ]

    assert find_zeros([*base, *outsider]) == expected
    assert find_zeros([*base, *non_finite]) == expected
    assert find_zeros([*base, *duplicates]) == expected
    assert find_zeros([*duplicates, *outsider, *non_finite, *base]) == expected


def test_FR16_auto_zero_from_frames_keeps_first_duplicate_in_sort_order():
    # 같은 키의 프레임 둘 중 정렬 순서로 먼저인 것을 쓴다. 입력 순서와 상관없다.
    base = frames_for([{"a": -50.0, "b": -60.0, "c": -72.0}] * 25)
    earlier = [
        replace(frame, rssi=frame.rssi - 10.0, received_at=frame.received_at - timedelta(milliseconds=5))
        for frame in base
        if frame.node_id == "c"
    ]
    originals = [frame for frame in base if frame.node_id != "c"]

    # c의 칸 값은 -82다. 1등이 아닌 b=-60, c=-82의 평균 -71 → b: 11, c: -11
    assert find_zeros([*base, *earlier]) == {"b": 11.0, "c": -11.0}
    assert find_zeros([*earlier, *base]) == {"b": 11.0, "c": -11.0}
    assert find_zeros([*earlier, *originals]) == {"b": 11.0, "c": -11.0}  # 원래 프레임이 없어도 같다


def test_FR16_auto_zero_from_frames_groups_bins_by_beacon():
    # 비콘 둘이 같은 시각에 따로 들린다. 칸은 비콘마다 묶고, 표본은 앵커별로 하나에 모은다.
    first = frames_for([THREE] * 20, beacon=BEACON)  # b: 5, c: -5
    # 1등이 아닌 b=-60, c=-76의 평균 -68 → b: 8, c: -8
    second = frames_for([{"a": -50.0, "b": -60.0, "c": -76.0}] * 20, beacon=OTHER_BEACON)

    # 표본 40개. b는 5 스무 개와 8 스무 개, c는 -5 스무 개와 -8 스무 개의 중앙값이다.
    assert find_zeros([*first, *second], min_bins=40) == {"b": 6.5, "c": -6.5}
    assert find_zeros(first, min_bins=40) == {}  # 한 비콘만으로는 표본이 20개다


def test_FR16_auto_zero_from_frames_bins_are_absolute_seconds():
    # 같은 칸(T0 ~ T0+1초)에 든 세 앵커는 표본을 낸다
    inside = [
        make_frame("a", -50.0, T0 + timedelta(seconds=0.1), seq=1),
        make_frame("b", -60.0, T0 + timedelta(seconds=0.5), seq=1),
        make_frame("c", -70.0, T0 + timedelta(seconds=0.9), seq=1),
    ]
    assert find_zeros(inside, min_bins=1) == THREE_SAMPLES

    # 칸 경계를 넘으면 칸마다 앵커가 하나뿐이다
    straddling = [
        make_frame("a", -50.0, T0 + timedelta(seconds=0.9), seq=1),
        make_frame("b", -60.0, T0 + timedelta(seconds=1.1), seq=1),
        make_frame("c", -70.0, T0 + timedelta(seconds=2.1), seq=1),
    ]
    assert find_zeros(straddling, min_bins=1) == {}

    # 칸은 [시작, 끝)이다. T0는 앞 칸이 아니라 T0에서 시작하는 칸에 든다.
    edge = [
        make_frame("a", -50.0, T0, seq=1),
        make_frame("b", -60.0, T0 + timedelta(microseconds=999_999), seq=1),
        make_frame("d", -70.0, T0 + timedelta(seconds=0.5), seq=1),
        make_frame("c", -70.0, T0 + timedelta(seconds=1), seq=1),
    ]
    assert find_zeros(edge, "abcd", min_bins=1) == {"b": 5.0, "d": -5.0}


def test_FR16_auto_zero_from_frames_ignores_input_order():
    rng = random.Random(7)
    anchors = ("a", "b", "c", "d", "e")
    base = frames_for(weak_device_bins(rng))
    duplicates = [replace(frame, rssi=frame.rssi + 5.0) for frame in base[::7]]  # 값만 다른 중복. received_at은 같다
    frames = [*base, *duplicates]
    expected = find_zeros(frames, anchors)

    for _ in range(5):
        shuffled = list(frames)
        rng.shuffle(shuffled)
        assert find_zeros(shuffled, anchors) == expected
    assert find_zeros(iter(frames), anchors) == expected  # 한 번만 읽는 iterable도 된다


def test_FR16_auto_zero_from_frames_accepts_any_collection_of_anchors():
    frames = frames_for([THREE] * 5)
    expected = find_zeros(frames, min_bins=1)

    assert expected == THREE_SAMPLES
    for anchors in (["a", "b", "c"], ("a", "b", "c"), {"a", "b", "c"}, frozenset("abc"), dict.fromkeys("abc")):
        assert find_zeros(frames, anchors, min_bins=1) == expected
    assert find_zeros(frames, ["a"], min_bins=1) == {}  # 앵커가 하나뿐이면 칸마다 앵커가 하나다
    assert find_zeros(frames, ["a", "b"], min_bins=1) == {}  # c는 앵커가 아니라 1등이 아닌 앵커가 b 하나다
    assert find_zeros(frames, [], min_bins=1) == {}
    assert find_zeros([], ANCHORS, min_bins=1) == {}
