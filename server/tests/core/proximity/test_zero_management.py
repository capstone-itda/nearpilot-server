"""측정 영점(#307), 영점 재탐색(#305), 거리 순서의 영점 보정(#306) — 근접 추정기 쪽 (spec 결정 23~25).

자동 영점 시험(`test_auto_zero_estimator.py`)의 장면과 도우미를 쓴다. c는 늘 6 dB 약하게 듣는다.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from nearpilot.core.proximity import HmmProximityEstimator
from nearpilot.core.proximity.autozero import measured_zero
from nearpilot.shared.enums import ZeroSource
from nearpilot.shared.interfaces import ZeroCalibrator
from nearpilot.shared.models import AutoZero

from .conftest import BEACON, T0, make_frame
from .test_auto_zero_estimator import (
    MIN_BINS, MODEL, SETTINGS, A, B, C, FakeStore, anchors, feed, make_report, vendor_of, with_vendor,
)


class SourceStore(FakeStore):
    """출처(`source`)를 받는 메모리 저장소. 자동 영점 쓰기는 `source` 없이 부른다 (기존 fake와 같은 호출)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sources: list[ZeroSource] = []
        self.hold: threading.Event | None = None  # 설정하면 자동 영점 쓰기가 이 신호까지 멈춘다
        self.held = threading.Event()  # 자동 영점 쓰기가 멈춰 기다리는 중이다

    def save_auto_zero(self, node_id, zero_db, vendor_model, vendor_firmware, recorded_at,
                       *, source: ZeroSource = ZeroSource.AUTO) -> int:
        if source is ZeroSource.AUTO and self.hold is not None:
            self.held.set()
            self.hold.wait(5)
        revision = super().save_auto_zero(node_id, zero_db, vendor_model, vendor_firmware, recorded_at)
        self.sources.append(source)
        row = self.rows[-1]
        self.rows[-1] = AutoZero(row.node_id, row.zero_db, row.vendor_model, row.vendor_firmware,
                                 row.revision, row.recorded_at, source)
        return revision


def estimator(store=None, **kwargs) -> HmmProximityEstimator:
    return HmmProximityEstimator(MODEL, SETTINGS, store=store, **kwargs)


# ── 측정 영점 계산 ──────────────────────────────────────────────────


def test_FR16_measured_zero_median_minus_reference():
    assert measured_zero([-50.0, -48.0, -46.0, float("nan"), -80.0], -44.0, 3) == (-49.0, -5.0)
    assert measured_zero([-40.0, -42.0], -44.0, 2) == (-41.0, 3.0)  # 짝수 개는 가운데 두 값의 평균
    assert measured_zero([-40.0, float("inf")], -44.0, 2) is None  # 유한한 값이 모자라다
    with pytest.raises(ValueError):
        measured_zero([-40.0], -44.0, 0)
    with pytest.raises(ValueError):
        measured_zero([-40.0], float("nan"), 1)


# ── 근접 추정기 ─────────────────────────────────────────────────────


def test_FR16_measured_zero_blocks_auto_zero():
    store = SourceStore()
    est = estimator(store)
    assert isinstance(est, ZeroCalibrator)
    nodes = anchors()
    revision = est.apply_measured_zero(nodes[C], -2.5, T0)
    assert revision == 1 and store.sources == [ZeroSource.MEASURED]
    assert store.rows[-1].zero_db == -2.5 and store.rows[-1].source is ZeroSource.MEASURED
    assert (store.rows[-1].vendor_model, store.rows[-1].vendor_firmware) == vendor_of(nodes[C])
    feed(est, nodes, range(MIN_BINS * 3))
    # c는 측정 영점이 있어서 자동 영점을 찾지 않는다. 행은 측정 영점 하나뿐이다
    assert [row.source for row in store.rows_of(C)] == [ZeroSource.MEASURED]
    assert est.calib_version == str(store.revision)


def test_FR16_vendor_change_discards_measured_zero():
    store = SourceStore()
    est = estimator(store)
    nodes = anchors("v1")
    est.apply_measured_zero(nodes[C], -2.5, T0)
    changed = {**nodes, C: with_vendor(nodes[C], "v2")}
    feed(est, changed, range(MIN_BINS + 3))
    rows = store.rows_of(C)
    assert rows[1].zero_db is None and rows[1].source is ZeroSource.AUTO  # 버림 행
    assert rows[2].zero_db is not None and rows[2].source is ZeroSource.AUTO  # 다시 찾은 자동 영점
    assert (rows[2].vendor_model, rows[2].vendor_firmware) == ("locker", "v2")


def test_FR16_late_auto_zero_write_keeps_measured_zero():
    """자동 영점 쓰기가 멈춘 동안 측정을 요청해도, 끝난 뒤 DB와 메모리의 c 영점은 측정 영점이다."""
    store = SourceStore()
    store.hold = threading.Event()
    est = estimator(store)
    nodes = anchors()
    feeder = threading.Thread(target=feed, args=(est, nodes, range(MIN_BINS + 3)))
    feeder.start()
    assert store.held.wait(5)  # 칸 하나에서 찾은 b, c의 자동 영점 쓰기가 예약됐고 첫 쓰기가 멈췄다
    measure = threading.Thread(target=est.apply_measured_zero, args=(nodes[C], -1.0, T0))
    measure.start()
    store.hold.set()
    feeder.join(5)
    measure.join(5)
    assert store.rows_of(C)[-1].source is ZeroSource.MEASURED  # 재시작해도 측정 영점을 쓴다
    assert [row.source for row in store.rows_of(C)].count(ZeroSource.MEASURED) == 1
    at = T0 + timedelta(seconds=MIN_BINS + 60)  # 장면의 프레임이 최근 구간 밖으로 나간 뒤다
    est.observe_report(make_report(C, at), node=nodes[C])
    est.observe(make_frame(C, -70.0, at, seq=10_000, received_at=at), node=nodes[C])
    assert est.distance_order(BEACON, [nodes[C]], at).values[C] == pytest.approx(-69.0)


def test_FR16_reset_zero_discards_and_refinds():
    store = SourceStore()
    est = estimator(store)
    nodes = anchors()
    feed(est, nodes, range(MIN_BINS + 3))
    found = store.rows_of(C)
    assert len(found) == 1 and found[0].zero_db is not None
    revision = est.discard_zeros([nodes[C]], T0 + timedelta(minutes=1))
    rows = store.rows_of(C)
    assert rows[-1].zero_db is None and rows[-1].source is ZeroSource.AUTO and revision == rows[-1].revision
    assert est.calib_version == str(revision)
    # 다시 처음부터 표본을 모아 찾는다
    feed(est, nodes, range(MIN_BINS + 3, 2 * MIN_BINS + 6), first_seq=10_000)
    assert store.rows_of(C)[-1].zero_db is not None


def test_FR16_reset_zero_discards_measured_zero():
    store = SourceStore()
    est = estimator(store)
    nodes = anchors()
    est.apply_measured_zero(nodes[A], 1.0, T0)
    est.discard_zeros([nodes[A]], T0)
    assert [(row.zero_db, row.source) for row in store.rows_of(A)] == [
        (1.0, ZeroSource.MEASURED), (None, ZeroSource.AUTO)]


def test_FR16_reset_zero_without_zero_writes_nothing():
    store = SourceStore()
    est = estimator(store)
    nodes = anchors()
    feed(est, nodes, range(MIN_BINS // 2))  # 표본이 모자라 아직 찾지 않았다
    assert est.discard_zeros(list(nodes.values()), T0) == 0
    assert store.rows == []
    # 모으던 표본을 지웠으므로 처음부터 MIN_BINS 칸을 다시 모아야 찾는다
    feed(est, nodes, range(MIN_BINS // 2, MIN_BINS // 2 + MIN_BINS - 1), first_seq=10_000)
    assert store.rows_of(C) == []
    feed(est, nodes, range(MIN_BINS // 2 + MIN_BINS - 1, MIN_BINS // 2 + MIN_BINS + 3), first_seq=10_000)
    assert len(store.rows_of(C)) == 1


def test_FR16_zero_management_raises_store_errors():
    store = SourceStore()
    est = estimator(store)
    nodes = anchors()
    store.failures = 1
    with pytest.raises(OSError):
        est.apply_measured_zero(nodes[A], 1.0, T0)
    assert est.calib_version == "0" and store.rows == []
    est.apply_measured_zero(nodes[A], 1.0, T0)
    store.failures = 1
    with pytest.raises(OSError):
        est.discard_zeros([nodes[A]], T0)
    # 버림 저장이 실패해도 메모리의 영점은 지운다 (vendor 변경의 버림과 같다)
    at = T0 + timedelta(seconds=1)
    est.observe_report(make_report(A, at), node=nodes[A])
    est.observe(make_frame(A, -60.0, at, received_at=at), node=nodes[A])
    assert est.distance_order(BEACON, [nodes[A]], at).values[A] == pytest.approx(-60.0)


# ── 거리 순서 (#306) ────────────────────────────────────────────────


def test_FR09_distance_order_subtracts_zero():
    store = SourceStore()
    est = estimator(store)
    nodes = anchors()
    at = T0 + timedelta(seconds=2)
    for node_id in (A, B, C):
        est.observe_report(make_report(node_id, at), node=nodes[node_id])
    # 보정 전: b가 a보다 2 dB 크다. b는 측정 영점이 +4 dB다 (세게 듣는 기기)
    for seq, (node_id, rssi) in enumerate(((A, -60.0), (A, -62.0), (B, -59.0), (C, -70.0))):
        est.observe(make_frame(node_id, rssi, at, seq=seq + 1, received_at=at), node=nodes[node_id])
    before = est.distance_order(BEACON, list(nodes.values()), at)
    assert before.nodes == (B, A, C) and before.calib_version == "0"
    revision = est.apply_measured_zero(nodes[B], 4.0, at)
    after = est.distance_order(BEACON, list(nodes.values()), at)
    assert after.nodes == (A, B, C)
    assert dict(after.values) == {A: pytest.approx(-61.0), B: pytest.approx(-63.0), C: pytest.approx(-70.0)}
    assert dict(after.counts) == {A: 2, B: 1, C: 1}  # 프레임 수는 그대로다
    assert after.calib_version == str(revision)
