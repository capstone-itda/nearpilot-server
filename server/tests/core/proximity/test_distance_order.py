"""근접 추정의 거리 순서 (FR-09, FR-16). 모델 없이 손으로 만든 보고와 프레임으로 확인한다.

근접 참여 기기는 최근 구간(`anchor_recent_sec`) 안에 `rssi` 보고를 보낸 trusted 후보다.
정렬 값은 최근 구간 안에서 서버가 받은 그 비콘 RSSI의 평균이다. 구간은 서버 수신 시각(`received_at`)으로 자른다.
"""

from __future__ import annotations

import inspect
import math
import threading
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from nearpilot.core.proximity import HmmProximityEstimator
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import TrustState
from nearpilot.shared.interfaces import ProximityEstimator
from nearpilot.shared.models import BeaconKey, DistanceOrder, NodeInfo, RssiReport

from .conftest import BEACON, OTHER_BEACON, T0, make_frame, make_node

SETTINGS = Settings()
RECENT = timedelta(seconds=SETTINGS.anchor_recent_sec)  # 3초
ONE_US = timedelta(microseconds=1)
AT = T0 + timedelta(seconds=30)  # 거리 순서를 묻는 시각


# ── 도우미 ───────────────────────────────────────────────────────────


@pytest.fixture
def estimator() -> HmmProximityEstimator:
    return HmmProximityEstimator(None, SETTINGS)  # 모델이 없다(수집 전용). 거리 순서는 모델 파일을 쓰지 않는다.


def at(offset_sec: float) -> datetime:
    """AT에서 offset_sec초 뒤의 시각이다. 음수면 앞이다."""
    return AT + timedelta(seconds=offset_sec)


def report(estimator: HmmProximityEstimator, node_id: str, received_at: datetime, *, node: NodeInfo | None = None, seq: int = 1) -> None:
    """node_id 기기의 rssi 보고를 서버가 received_at에 받았다고 기록한다. node의 기본값은 trusted인 node_id 기기다."""
    estimator.observe_report(
        RssiReport(
            node_id=node_id, session_id=f"s-{node_id}", seq=seq,
            frame_ts=received_at, sent_at=received_at, received_at=received_at,
        ),
        node=make_node(node_id) if node is None else node,
    )


def feed(estimator: HmmProximityEstimator, node_id: str, *samples: tuple[datetime, float], beacon: BeaconKey = BEACON) -> None:
    """node_id 기기의 프레임을 (서버가 받은 시각, RSSI)마다 주어진 순서대로 넣는다. seq는 1부터 센다."""
    node = make_node(node_id)
    for seq, (received_at, rssi) in enumerate(samples, 1):
        estimator.observe(make_frame(node_id, rssi, received_at, beacon=beacon, seq=seq, received_at=received_at), node=node)


def order(estimator: HmmProximityEstimator, node_ids: list[str], *, when: datetime = AT) -> DistanceOrder:
    """node_ids 기기를 trusted 후보로 한 BEACON의 거리 순서다."""
    return estimator.distance_order(BEACON, [make_node(node_id) for node_id in node_ids], when)


# ── 정렬 값 ──────────────────────────────────────────────────────────


def test_FR16_distance_order_mean_of_recent_window(estimator):
    # 시각 순서로 넣는다. at 뒤의 프레임이 들어올 때 버퍼 자르기가 일어나도 구간 안 프레임은 남아야 한다.
    frames = [
        make_frame("light-1", -40.0, at(-4), seq=1, received_at=at(-4)),  # 구간 시작보다 이르다
        make_frame("light-1", -60.0, at(-2), seq=2, received_at=at(-2)),
        make_frame("light-1", -20.0, at(-1.5), seq=3, beacon=OTHER_BEACON, received_at=at(-1.5)),  # 다른 비콘이다
        make_frame("light-1", -70.0, at(-1), seq=4, received_at=at(-1)),
        make_frame("light-1", -30.0, at(0.5), seq=5, received_at=at(0.5)),  # at보다 늦다
    ]
    for frame in frames:
        estimator.observe(frame, node=make_node("light-1"))
    report(estimator, "light-1", at(-1))

    result = order(estimator, ["light-1"])

    assert result.nodes == ("light-1",)
    assert result.values["light-1"] == -65.0


def test_FR16_distance_order_strongest_mean_first(estimator):
    # 평균은 light-2(-61), light-1(-65), light-3(-75) 순서로 강하다.
    # 가장 강한 값은 light-1(-40)이고 마지막 값은 light-2(-62)가 앞서서, 평균이 아닌 값으로 정하면 순서가 달라진다.
    feed(estimator, "light-1", (at(-2), -40.0), (at(-1), -90.0))
    feed(estimator, "light-2", (at(-2), -60.0), (at(-1), -62.0))
    feed(estimator, "light-3", (at(-2), -75.0))
    for node_id in ("light-1", "light-2", "light-3"):
        report(estimator, node_id, at(-1))

    result = order(estimator, ["light-1", "light-2", "light-3"])

    assert result.nodes == ("light-2", "light-1", "light-3")
    assert result.values == {"light-1": -65.0, "light-2": -61.0, "light-3": -75.0}


def test_FR16_distance_order_window_bounds_use_received_at(estimator):
    report(estimator, "light-1", at(-1))
    old_ts = AT - RECENT - timedelta(seconds=5)
    frames = [  # (서버가 받은 시각, 기기 시각, RSSI)
        (AT - RECENT - ONE_US, AT, -10.0),  # 받은 시각이 구간보다 1 µs 이르다. 기기 시각이 구간 안이어도 쓰지 않는다.
        (AT - RECENT, old_ts, -50.0),  # 받은 시각이 구간 시작과 같다. 기기 시각이 구간 밖이어도 쓴다.
        (AT, old_ts, -70.0),  # 받은 시각이 at과 같다. 쓴다.
        (AT + ONE_US, AT, -20.0),  # 받은 시각이 at보다 1 µs 늦다. 기기 시각이 at이어도 쓰지 않는다.
    ]
    for seq, (received_at, frame_ts, rssi) in enumerate(frames, 1):
        estimator.observe(make_frame("light-1", rssi, frame_ts, seq=seq, received_at=received_at), node=make_node("light-1"))

    assert order(estimator, ["light-1"]).values["light-1"] == -60.0


@pytest.mark.parametrize(
    ("recent_sec", "expected"),
    [
        pytest.param(6.0, ("light-1", "light-2", "light-3"), id="six_seconds"),
        pytest.param(1.0, ("light-3",), id="one_second"),
    ],
)
def test_FR16_distance_order_window_follows_anchor_recent_sec(recent_sec, expected):
    estimator = HmmProximityEstimator(None, replace(SETTINGS, anchor_recent_sec=recent_sec))
    report(estimator, "light-1", at(-5))
    report(estimator, "light-2", at(-2))
    report(estimator, "light-3", at(-0.5))
    feed(estimator, "light-1", (at(-5), -50.0))
    feed(estimator, "light-2", (at(-2), -60.0))

    # 6초 구간이면 세 기기가 모두 서고 light-1의 5초 전 프레임도 쓴다. 1초 구간이면 light-3만 선다.
    assert order(estimator, ["light-1", "light-2", "light-3"]).nodes == expected


@pytest.mark.parametrize("bad_rssi", [math.nan, math.inf, -math.inf])
def test_FR16_distance_order_non_finite_rssi_is_missing(estimator, bad_rssi):
    # observe는 RSSI 값을 보지 않는다. 유한하지 않은 값은 posterior와 같이 결측으로 버린다.
    report(estimator, "light-1", at(-1))
    report(estimator, "light-2", at(-1))
    feed(estimator, "light-1", (at(-2), -60.0), (at(-1), bad_rssi))
    feed(estimator, "light-2", (at(-1), bad_rssi))

    result = order(estimator, ["light-1", "light-2"])

    assert result.nodes == ("light-1", "light-2")
    assert result.values == {"light-1": -60.0, "light-2": None}


# ── 근접 참여 기기 ────────────────────────────────────────────────────


def test_FR16_report_time_only_trusted(estimator):
    everyone = ["light-1", "light-2", "light-3"]

    # 신뢰하지 않는 기기의 보고와, 기기 정보가 보고와 다른 기기의 것인 경우는 기록하지 않는다. 예외도 없다.
    report(estimator, "light-1", at(-1), node=make_node("light-1", trust=TrustState.DISCOVERED))
    report(estimator, "light-1", at(-1), node=make_node("light-1", trust=TrustState.REVOKED))
    report(estimator, "light-2", at(-1), node=make_node("light-3"))
    assert order(estimator, everyone).nodes == ()

    # 같은 기기를 trusted로 기록하면 참여 기기다. 앞의 결과가 비어 있던 것은 기록하지 않았기 때문이다.
    report(estimator, "light-1", at(-1))
    assert order(estimator, everyone).nodes == ("light-1",)

    # 늦게 도착한 옛 보고(받은 시각이 더 이르다)는 마지막 보고 시각을 되돌리지 않는다.
    report(estimator, "light-3", at(-4))  # 구간 밖이다
    assert order(estimator, everyone).nodes == ("light-1",)
    report(estimator, "light-3", at(-1))
    report(estimator, "light-3", at(-5), seq=2)
    assert order(estimator, everyone).nodes == ("light-1", "light-3")


def test_FR09_selector_non_participant_excluded(estimator):
    report(estimator, "light-1", at(-4))  # 마지막 보고가 구간보다 이르다
    report(estimator, "light-3", AT - RECENT)  # 구간 시작과 같다
    report(estimator, "light-4", AT - RECENT - ONE_US)  # 구간보다 1 µs 이르다
    feed(estimator, "light-2", (at(-1), -60.0))  # 보고가 없다. 프레임만 있어서는 참여 기기가 아니다.

    result = order(estimator, ["light-1", "light-2", "light-3", "light-4"])

    assert result.nodes == ("light-3",)
    assert result.values == {"light-3": None}


def test_FR09_selector_only_candidates_participate(estimator):
    for node_id in ("light-1", "light-2", "light-3"):
        report(estimator, node_id, at(-1))  # 보고할 때는 셋 다 trusted였다

    # light-3은 후보가 아니다. light-2는 후보지만 지금은 trusted가 아니다.
    candidates = [make_node("light-2", trust=TrustState.REVOKED), make_node("light-1")]

    assert estimator.distance_order(BEACON, candidates, AT).nodes == ("light-1",)


def test_FR09_selector_empty_order_without_participants(estimator):
    assert estimator.distance_order(BEACON, [], AT) == DistanceOrder((), {})
    assert order(estimator, ["light-1", "light-2"]) == DistanceOrder((), {})  # 후보는 있지만 보고가 없다


def test_FR09_selector_restore_keeps_no_report_times(estimator):
    estimator.restore([make_frame("light-1", -60.0, at(-2), seq=1, received_at=at(-2))], [make_node("light-1")])

    # 재시작한 서버는 복원한 프레임이 있어도 다음 보고까지 그 기기를 거리 순서에 세우지 않는다.
    assert order(estimator, ["light-1"]).nodes == ()

    report(estimator, "light-1", at(-0.5))
    assert order(estimator, ["light-1"]).values == {"light-1": -60.0}


# ── 순서 ─────────────────────────────────────────────────────────────


def test_FR09_selector_empty_report_keeps_participant_last(estimator):
    for node_id in ("light-1", "light-2", "light-3"):
        report(estimator, node_id, at(-1))  # 셋 다 최근에 보고했다. light-1은 프레임 없이 보고만 했다.
    feed(estimator, "light-2", (at(-1), -95.0))  # 아주 약하게 들리지만 값이 있다
    feed(estimator, "light-3", (at(-1), -30.0), beacon=OTHER_BEACON)  # 다른 비콘만 들렸다. 이 비콘은 보지 못했다.

    result = order(estimator, ["light-1", "light-2", "light-3"])

    assert result.nodes == ("light-2", "light-1", "light-3")
    assert result.values == {"light-1": None, "light-2": -95.0, "light-3": None}


def test_FR09_selector_tie_by_node_id(estimator):
    for node_id in ("light-9", "light-3", "light-2", "light-1"):
        report(estimator, node_id, at(-1))
    feed(estimator, "light-3", (at(-2), -50.0), (at(-1), -70.0))  # 평균이 light-1과 같다. 프레임은 다르다.
    feed(estimator, "light-1", (at(-1), -60.0))
    # light-9, light-2는 프레임이 없다.

    result = order(estimator, ["light-9", "light-3", "light-2", "light-1"])

    assert result.nodes == ("light-1", "light-3", "light-2", "light-9")
    assert order(estimator, ["light-1", "light-2", "light-3", "light-9"]) == result  # 후보 순서는 결과에 영향을 주지 않는다


# ── 버퍼, 잠금, Protocol ──────────────────────────────────────────────


@pytest.mark.parametrize("frame_sec", [1.0, 2.0])
@pytest.mark.parametrize(
    ("extra", "kept"),
    [
        pytest.param(timedelta(0), True, id="exactly_retain_is_kept"),
        pytest.param(ONE_US, False, id="one_microsecond_over_is_dropped"),
    ],
)
def test_FR16_buffer_retention_covers_anchor_recent_window(frame_sec, extra, kept):
    settings = replace(SETTINGS, rssi_frame_sec=frame_sec)
    retain = timedelta(seconds=settings.anchor_recent_sec + frame_sec)  # 모델이 없어서 최근 구간이 보존 길이를 정한다
    estimator = HmmProximityEstimator(None, settings)
    # seen은 구간 시작에 받았다. later는 seen보다 retain(+extra) 늦게 받아서 at보다 뒤라 이 구간에는 쓰이지 않는다.
    seen_at = AT - RECENT
    later_at = seen_at + retain + extra
    report(estimator, "light-1", seen_at)
    estimator.observe(make_frame("light-1", -55.0, seen_at, seq=1, received_at=seen_at), node=make_node("light-1"))
    estimator.observe(make_frame("light-1", -55.0, later_at, seq=2, received_at=later_at), node=make_node("light-1"))

    assert order(estimator, ["light-1"]).values["light-1"] == (-55.0 if kept else None)


def test_FR16_distance_order_computes_outside_lock(estimator, monkeypatch):
    report(estimator, "light-1", at(-1))
    feed(estimator, "light-1", (at(-1), -60.0))

    def spy(*args, **kwargs):
        # 순서를 만드는 동안 observe와 observe_report가 잠금을 얻을 수 있어야 한다. 잠금을 쥐고 있으면 여기서 막힌다.
        estimator.observe(make_frame("light-1", -20.0, AT, seq=99, received_at=AT), node=make_node("light-1"))
        report(estimator, "light-2", AT)
        return DistanceOrder(*args, **kwargs)

    monkeypatch.setattr("nearpilot.core.proximity.estimator.DistanceOrder", spy)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(result=order(estimator, ["light-1"])), daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "distance_order가 순서를 만드는 동안 잠금을 쥐고 있다"
    assert outcome["result"].values == {"light-1": -60.0}  # 계산 중에 들어온 프레임은 이번 결과에 쓰이지 않는다
    monkeypatch.undo()
    assert order(estimator, ["light-1", "light-2"]).values == {"light-1": -40.0, "light-2": None}


def test_FR16_estimator_satisfies_distance_protocol():
    estimator = HmmProximityEstimator(None, SETTINGS)

    assert isinstance(estimator, ProximityEstimator)
    # runtime_checkable은 메서드 이름만 본다. 인자 이름과 종류도 Protocol과 같아야 한다.
    for name in ("observe_report", "distance_order"):
        actual = inspect.signature(getattr(HmmProximityEstimator, name)).parameters.values()
        declared = inspect.signature(getattr(ProximityEstimator, name)).parameters.values()
        assert [(p.name, p.kind) for p in actual] == [(p.name, p.kind) for p in declared]
