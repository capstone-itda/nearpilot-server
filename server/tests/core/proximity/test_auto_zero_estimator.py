"""근접 추정기의 자동 영점 (FR-16, FR-22, spec 결정 4~6, 15). 메모리 fake `AutoZeroStore`와 합성 공통 모델로 확인한다.

방문자가 a, b 앞을 번갈아 서고 c는 늘 6 dB 약하게 듣는다. 칸마다 1등이 아닌 기기가 둘 이상이라서 모든 칸이 c의 표본이 된다.
칸 k는 `k`초 `T0 + k초 + 0.5초`의 프레임이 든다. 칸 k는 칸 k + 2의 프레임이 오면 닫힌다.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nearpilot.core.proximity import HmmProximityEstimator
from nearpilot.core.proximity.autozero import bin_index, zeros_from_frames
from nearpilot.core.proximity.common_model import CommonModel, ModelMismatchError, load_common_model
from nearpilot.core.proximity.posterior import compute_posteriors
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import VendorInfo, legacy_descriptor
from nearpilot.shared.enums import Occupancy, TrustState
from nearpilot.shared.interfaces import AutoZeroStore
from nearpilot.shared.models import AutoZero, AutoZeroState, BeaconKey, NodeInfo, ProximityResult, RssiFrame, RssiReport

from .conftest import BEACON, OTHER_BEACON, T0, make_frame, make_node

MODEL = load_common_model(Path(__file__).resolve().parent / "fixtures" / "common" / "syn-common-1.json")
MIN_BINS = MODEL.auto_zero_min_bins  # 20
SETTINGS = Settings()  # rssi_frame_sec 1.0, anchor_recent_sec 3.0
A, B, C = "locker-1", "locker-2", "locker-3"
ANCHORS = (A, B, C)
C_OFFSET = -6.0  # c는 늘 6 dB 약하게 듣는다. 칸마다 b와 c(또는 a와 c)의 평균에서 c는 −3 dB다


# ── 도우미 ───────────────────────────────────────────────────────────


class FakeStore:
    """메모리 `AutoZeroStore`. 쓴 행을 순서대로 모으고, 정해 둔 횟수만큼 저장을 실패시킨다."""

    def __init__(self, *, revision: int = 0, delay_sec: float = 0.0) -> None:
        self.revision = revision
        self.rows: list[AutoZero] = []
        self.attempts: list[tuple[str, float | None, str | None, str | None, datetime]] = []
        self.failures = 0  # 앞으로 실패시킬 저장 횟수
        self.delay_sec = delay_sec
        self.in_flight: dict[str, int] = {}  # 기기별로 지금 저장 중인 호출 수
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def auto_zeros(self, revision: int | None = None) -> AutoZeroState:
        limit = self.revision if revision is None else revision
        latest: dict[str, AutoZero] = {}
        for row in self.rows:
            if row.revision <= limit:
                latest[row.node_id] = row
        return AutoZeroState(revision=limit, zeros=latest)

    def save_auto_zero(
        self, node_id: str, zero_db: float | None, vendor_model: str | None, vendor_firmware: str | None,
        recorded_at: datetime,
    ) -> int:
        with self._lock:
            self.attempts.append((node_id, zero_db, vendor_model, vendor_firmware, recorded_at))
            self.in_flight[node_id] = self.in_flight.get(node_id, 0) + 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight[node_id])
        try:
            if self.delay_sec:
                time.sleep(self.delay_sec)  # 쓰는 동안 다른 스레드가 같은 기기를 다시 저장하려 하면 in_flight가 2가 된다
            with self._lock:
                if self.failures > 0:
                    self.failures -= 1
                    raise OSError("database is locked")
                self.revision += 1
                self.rows.append(AutoZero(node_id, zero_db, vendor_model, vendor_firmware, self.revision, recorded_at))
                return self.revision
        finally:
            with self._lock:
                self.in_flight[node_id] -= 1

    def rows_of(self, node_id: str) -> list[AutoZero]:
        return [row for row in self.rows if row.node_id == node_id]


def with_vendor(node: NodeInfo, firmware: str, model: str = "locker") -> NodeInfo:
    """기기 선언의 vendor를 (model, firmware)로 바꾼 NodeInfo."""
    base = legacy_descriptor("locker", Occupancy.RENTAL)
    return replace(node, descriptor=replace(base, vendor=VendorInfo("nearpilot", model, firmware)))


def anchors(firmware: str = "v1", place_id: str = "place-a") -> dict[str, NodeInfo]:
    """a, b, c. 선언의 vendor는 (locker, firmware)다."""
    return {node_id: with_vendor(make_node(node_id, place_id), firmware) for node_id in ANCHORS}


def vendor_of(node: NodeInfo) -> tuple[str, str]:
    return (node.descriptor.vendor.model, node.descriptor.vendor.firmware)


def ts_of(k: int) -> datetime:
    return T0 + timedelta(seconds=k + 0.5)


def bin_frames(
    k: int, nodes: dict[str, NodeInfo], *, beacon: BeaconKey = BEACON, session: str = "s1",
    rng: random.Random | None = None, c_rssi: float | None = None, first_seq: int = 1,
) -> list[RssiFrame]:
    """칸 k의 프레임. 앵커마다 하나이고, a(짝수 칸)와 b(홀수 칸) 앞에 선 방문자를 듣는다. c는 C_OFFSET만큼 약하다."""
    front = A if k % 2 == 0 else B
    frames = []
    for node_id in nodes:
        rssi = (MODEL.front.mean if node_id == front else MODEL.other.mean) + (C_OFFSET if node_id == C else 0.0)
        if rng is not None:
            rssi += rng.gauss(0.0, 1.5)
        if node_id == C and c_rssi is not None:
            rssi = c_rssi
        frames.append(make_frame(node_id, rssi, ts_of(k), beacon=beacon, seq=first_seq + k, session_id=session))
    return frames


def feed(
    estimator: HmmProximityEstimator, nodes: dict[str, NodeInfo], bins: range | list[int], **kwargs,
) -> list[RssiFrame]:
    """칸을 순서대로 넣는다. 넣은 프레임을 돌려준다."""
    sent = []
    for k in bins:
        for frame in bin_frames(k, nodes, **kwargs):
            estimator.observe(frame, node=nodes[frame.node_id])
            sent.append(frame)
    return sent


def make_report(node_id: str, received_at: datetime, *, session: str = "s1", seq: int = 1) -> RssiReport:
    return RssiReport(node_id, session, seq, received_at, received_at, received_at)


def report_all(estimator: HmmProximityEstimator, nodes: dict[str, NodeInfo], at: datetime, **kwargs) -> None:
    """판정 시각 직전에 모든 기기가 빈 보고를 보냈다."""
    for node in nodes.values():
        estimator.observe_report(make_report(node.node_id, at - timedelta(seconds=0.5), **kwargs), node=node)


def ask(estimator: HmmProximityEstimator, nodes: dict[str, NodeInfo], at: datetime) -> ProximityResult:
    """모든 기기가 후보이고 보고했다고 보고 묻는다."""
    report_all(estimator, nodes, at)
    return estimator.posterior([BEACON], list(nodes.values()), at, place_nodes=list(nodes.values()))


def new_estimator(store: FakeStore | None = None, *, model: CommonModel | None = MODEL, zeros: AutoZeroState | None = None):
    return HmmProximityEstimator(model, SETTINGS, zeros=zeros, store=store)


def zeroed_c(store: FakeStore | None = None, firmware: str = "v1") -> tuple[HmmProximityEstimator, FakeStore, dict[str, NodeInfo]]:
    """칸 0~21을 넣어 c의 자동 영점을 찾은 추정기. c의 표본 20개(칸 0~19)가 칸 21의 프레임으로 닫힌다."""
    store = FakeStore() if store is None else store
    nodes = anchors(firmware)
    estimator = new_estimator(store)
    feed(estimator, nodes, range(MIN_BINS + 2))
    return estimator, store, nodes


AT_22 = T0 + timedelta(seconds=MIN_BINS + 2)  # 칸 21 바로 뒤의 판정 시각


# ── 찾아서 저장한다 ──────────────────────────────────────────────────


def test_FR16_auto_zero_saved_with_revision():
    store = FakeStore()
    assert isinstance(store, AutoZeroStore)  # fake가 Protocol을 지킨다
    nodes = anchors()
    estimator = new_estimator(store)
    assert estimator.calib_version == "0"  # 개정이 없으면 "0"이다

    feed(estimator, nodes, range(MIN_BINS + 1))  # 칸 20까지: 닫힌 칸은 0~18이라 c의 표본이 19개다
    assert store.rows == [] and store.attempts == []
    assert ask(estimator, nodes, T0 + timedelta(seconds=MIN_BINS + 1)).unzeroed == set(ANCHORS)  # 아직 아무도 영점이 없다

    feed(estimator, nodes, [MIN_BINS + 1])  # 칸 21의 첫 프레임이 칸 19를 닫는다: c의 표본 20개
    assert len(store.rows) == 1
    (row,) = store.rows
    assert (row.node_id, row.revision, row.vendor_model, row.vendor_firmware) == (C, 1, "locker", "v1")
    assert row.zero_db == pytest.approx(-3.0)  # c − (1등이 아닌 기기들의 평균)의 중앙값
    assert row.recorded_at == ts_of(MIN_BINS + 1) + timedelta(milliseconds=50)  # 칸 21의 프레임의 received_at. 현재 시각이 아니다

    result = ask(estimator, nodes, AT_22)
    assert result.calib_version == "1" == estimator.calib_version
    assert result.unzeroed == {A, B}  # c는 자동 영점이 있다. a, b는 표본이 10개뿐이다
    assert set(result.posteriors) == set(ANCHORS)


def test_FR16_auto_zero_is_calibrated_in_posterior():
    # 저장한 영점으로 보정한 확률을 낸다: 보정하지 않은 같은 입력과 결과가 다르다.
    estimator, store, nodes = zeroed_c()
    frames = [frame for k in range(MIN_BINS + 2) for frame in bin_frames(k, nodes)]
    plain = compute_posteriors(
        MODEL, frames, BEACON, list(nodes.values()), list(nodes.values()), AT_22, bin_sec=1.0,
        participants=list(ANCHORS), zeros={}, calib_version="1",
    )
    calibrated = compute_posteriors(
        MODEL, frames, BEACON, list(nodes.values()), list(nodes.values()), AT_22, bin_sec=1.0,
        participants=list(ANCHORS), zeros={C: store.rows[0].zero_db}, calib_version="1",
    )

    result = ask(estimator, nodes, AT_22)

    assert result == calibrated
    assert result.posteriors != plain.posteriors


def test_FR16_auto_zero_late_frame_ignored():
    store = FakeStore()
    nodes = anchors()
    estimator = new_estimator(store)
    sent = feed(estimator, nodes, range(MIN_BINS + 1))  # L = 20. 칸 0~18이 닫혔다. c의 표본 19개
    at = T0 + timedelta(seconds=MIN_BINS + 1)
    before = ask(estimator, nodes, at)

    # 칸 L − 2 = 18의 프레임이 늦게 왔다. 세 기기의 값이 모두 있어서, 표본으로 쓰면 c의 20번째 표본이 된다.
    late = bin_frames(MIN_BINS - 2, nodes, first_seq=1000, c_rssi=-120.0)
    for frame in late:
        estimator.observe(frame, node=nodes[frame.node_id])
    assert store.rows == [] and store.attempts == []  # 표본이 늘지 않았다

    # 그 프레임은 사후확률 버퍼에는 있다.
    after = ask(estimator, nodes, at)
    plain = compute_posteriors(
        MODEL, [*sent, *late], BEACON, list(nodes.values()), list(nodes.values()), at, bin_sec=1.0,
        participants=list(ANCHORS), zeros={}, calib_version="0",
    )
    assert after == plain and after != before

    # 늦은 프레임은 표본을 바꾸지 않았다: 칸 19가 닫히면 c의 표본이 정확히 20개가 된다.
    feed(estimator, nodes, [MIN_BINS + 1])
    (row,) = store.rows
    assert (row.node_id, row.zero_db) == (C, pytest.approx(-3.0))


def test_FR16_auto_zero_out_of_order_frame_in_open_bin_counts():
    # 칸 L − 1은 아직 열려 있다. 칸 L의 프레임이 먼저 와도 칸 L − 1의 프레임은 표본이 된다.
    store = FakeStore()
    nodes = anchors()
    estimator = new_estimator(store)
    feed(estimator, nodes, range(MIN_BINS - 1))  # 칸 0~18. 닫힌 칸은 0~16이다
    bin19 = bin_frames(MIN_BINS - 1, nodes)
    for frame in bin19:  # 칸 19는 c의 프레임만 늦게 온다
        if frame.node_id != C:
            estimator.observe(frame, node=nodes[frame.node_id])
    feed(estimator, nodes, [MIN_BINS, MIN_BINS + 1])  # 칸 20, 21의 프레임이 칸 19까지 닫는다. 칸 19에는 c의 값이 없었다
    assert store.rows == []  # c의 표본은 19개(칸 0~18)다. 칸 19는 앵커가 a, b뿐이라 표본이 아니다

    estimator = new_estimator(store)
    feed(estimator, nodes, range(MIN_BINS - 1))
    for frame in bin19:
        if frame.node_id != C:
            estimator.observe(frame, node=nodes[frame.node_id])
    feed(estimator, nodes, [MIN_BINS])  # 칸 20이 먼저 온다. L = 20이라 칸 19(L − 1)는 열려 있다
    (c_frame,) = [frame for frame in bin19 if frame.node_id == C]
    estimator.observe(c_frame, node=nodes[C])  # 늦게 온 c의 칸 19 프레임이 열린 칸에 들어간다
    feed(estimator, nodes, [MIN_BINS + 1])

    assert [row.node_id for row in store.rows] == [C]  # 칸 0~19의 표본 20개


def test_FR16_auto_zero_without_store_counts_revision_in_memory():
    nodes = anchors()
    estimator = new_estimator(None)  # 저장소가 없다
    feed(estimator, nodes, range(MIN_BINS + 2))

    result = ask(estimator, nodes, AT_22)

    assert estimator.calib_version == "1" and result.calib_version == "1"
    assert result.unzeroed == {A, B}

    # 버림도 개정을 올린다.
    estimator.observe_report(make_report(C, AT_22), node=with_vendor(nodes[C], "v2"))
    assert estimator.calib_version == "2"
    assert ask(estimator, {**nodes, C: with_vendor(nodes[C], "v2")}, AT_22).unzeroed == set(ANCHORS)


def test_FR16_auto_zero_skips_nodes_without_place_and_non_finite_rssi():
    # 장소가 없는 기기의 프레임은 표본이 아니다.
    store = FakeStore()
    nodes = {node_id: replace(node, place_id=None) for node_id, node in anchors().items()}
    estimator = new_estimator(store)
    feed(estimator, nodes, range(MIN_BINS + 5))
    assert store.rows == []

    # 유한하지 않은 RSSI는 표본이 아니다. c가 칸마다 NaN이면 1등이 아닌 기기가 하나뿐이라 아무도 표본이 없다.
    for bad in (math.nan, math.inf, -math.inf):
        store = FakeStore()
        nodes = anchors()
        estimator = new_estimator(store)
        feed(estimator, nodes, range(MIN_BINS + 5), c_rssi=bad)
        assert store.rows == [], bad


def test_FR16_auto_zero_tracks_each_place_and_beacon_separately():
    store = FakeStore()
    place_a = anchors()
    # 장소 b의 기기 셋. bin_frames는 a, b, c 이름으로 프레임을 만들므로 장소 b의 이름으로 바꿔 넣는다.
    place_b = {name: make_node(name, "place-b") for name in ("mood-1", "mood-2", "mood-3")}
    rename = dict(zip(ANCHORS, place_b))
    estimator = new_estimator(store)

    # 다른 비콘의 프레임이 아주 늦은 칸에 먼저 와도 이 비콘의 칸을 닫거나 늦은 칸으로 만들지 않는다.
    for frame in bin_frames(1000, place_a, beacon=OTHER_BEACON):
        estimator.observe(frame, node=place_a[frame.node_id])
    for k in range(MIN_BINS + 2):
        for frame in bin_frames(k, place_a):
            estimator.observe(frame, node=place_a[frame.node_id])
            name = rename[frame.node_id]
            estimator.observe(replace(frame, node_id=name, session_id=f"s-{name}"), node=place_b[name])

    # 장소 a와 장소 b가 각자 c를 찾는다. 같은 칸 번호를 쓰지만 서로의 칸을 닫거나 섞지 않는다.
    assert sorted(row.node_id for row in store.rows) == ["locker-3", "mood-3"]
    assert store.rows_of("locker-3")[0].vendor_firmware == "v1"
    assert store.rows_of("mood-3")[0].vendor_firmware is None  # 선언이 없는 기기의 vendor는 (None, None)이다


# ── 버림과 유지 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("via", ["report", "frame"])
def test_FR16_vendor_change_discards_auto_zero(via):
    estimator, store, nodes = zeroed_c()
    (found,) = store.rows
    changed = with_vendor(nodes[C], "v2")
    discard_at = AT_22 + timedelta(seconds=1)

    if via == "report":
        estimator.observe_report(make_report(C, discard_at, session="s2"), node=changed)
    else:
        estimator.observe(make_frame(C, -78.0, discard_at, seq=1, session_id="s2", received_at=discard_at), node=changed)

    discard = store.rows[-1]
    assert len(store.rows) == 2
    assert (discard.node_id, discard.zero_db, discard.vendor_model, discard.vendor_firmware) == (C, None, "locker", "v2")
    assert discard.revision == found.revision + 1 and discard.recorded_at == discard_at  # 버림 행도 받은 시각이다
    nodes = {**nodes, C: changed}
    result = ask(estimator, nodes, discard_at + timedelta(seconds=1))
    assert result.unzeroed == set(ANCHORS) and result.calib_version == "2"

    # 모으던 표본은 지웠고 다시 모은다: 15칸으로는 찾지 않고, 충분히 쌓이면 새 자동 영점을 찾는다.
    feed(estimator, nodes, range(MIN_BINS + 2, MIN_BINS + 2 + 15))
    assert len(store.rows) == 2
    feed(estimator, nodes, range(MIN_BINS + 2 + 15, MIN_BINS + 2 + 30))
    again = store.rows_of(C)[2:]  # a, b도 그동안 표본 20개가 쌓여 자기 자동 영점을 찾았다
    assert [(row.zero_db is None, row.vendor_firmware) for row in again] == [(False, "v2")]
    assert again[0].zero_db == pytest.approx(-3.0)
    final = ask(estimator, nodes, T0 + timedelta(seconds=MIN_BINS + 2 + 30))
    assert final.calib_version == str(store.revision) and C not in final.unzeroed


def test_FR16_vendor_model_change_also_discards():
    estimator, store, nodes = zeroed_c()

    estimator.observe_report(make_report(C, AT_22), node=with_vendor(nodes[C], "v1", model="locker-2"))

    assert [(row.zero_db, row.vendor_model, row.vendor_firmware) for row in store.rows[1:]] == [(None, "locker-2", "v1")]


def test_FR16_descriptor_removed_discards_auto_zero():
    # 선언이 없으면 vendor는 (None, None)이다. 선언이 있던 기기의 자동 영점은 다른 vendor의 것이라 버린다.
    estimator, store, nodes = zeroed_c()

    estimator.observe_report(make_report(C, AT_22), node=replace(nodes[C], descriptor=None))

    assert [(row.zero_db, row.vendor_model, row.vendor_firmware) for row in store.rows[1:]] == [(None, None, None)]


def test_FR16_vendor_change_while_collecting_drops_samples_without_writing():
    store = FakeStore()
    nodes = anchors()
    estimator = new_estimator(store)
    feed(estimator, nodes, range(15))  # c의 표본이 12개쯤 쌓였다
    estimator.observe_report(make_report(C, ts_of(15)), node=with_vendor(nodes[C], "v2"))
    assert store.attempts == []  # 영점이 없던 기기의 vendor가 바뀌어도 DB에 쓰지 않는다

    changed = {**nodes, C: with_vendor(nodes[C], "v2")}
    feed(estimator, changed, range(15, MIN_BINS + 2))  # 바뀐 뒤 쌓인 표본은 7칸뿐이다
    assert store.rows == []
    feed(estimator, changed, range(MIN_BINS + 2, 15 + MIN_BINS + 3))
    assert [row.node_id for row in store.rows] == [C]  # 20칸이 쌓이면 찾는다
    assert store.rows[0].vendor_firmware == "v2"


def test_FR16_reboot_keeps_auto_zero():
    estimator, store, nodes = zeroed_c()
    rebooted = replace(nodes[C], session_id="s2")  # 새 연결, 같은 vendor
    after = AT_22 + timedelta(seconds=1)

    estimator.observe(make_frame(C, -78.0, after, seq=1, session_id="s2", received_at=after), node=rebooted)
    estimator.observe_report(make_report(C, after, session="s2", seq=1), node=rebooted)
    assert len(store.rows) == 1  # 행이 늘지 않는다

    result = ask(estimator, {**nodes, C: rebooted}, after + timedelta(seconds=1))
    assert C not in result.unzeroed and result.calib_version == "1"


def test_FR16_declaration_change_without_vendor_change_keeps_auto_zero():
    # 동작 선언만 바뀌고 vendor가 같으면 유지한다.
    estimator, store, nodes = zeroed_c()
    base = nodes[C].descriptor
    changed = replace(nodes[C], descriptor=replace(base, kind="locker-x", actions=base.actions[:1]))

    estimator.observe_report(make_report(C, AT_22), node=changed)

    assert len(store.rows) == 1
    assert C not in ask(estimator, {**nodes, C: changed}, AT_22 + timedelta(seconds=1)).unzeroed


# ── 서버를 다시 시작한다 ─────────────────────────────────────────────


def stored_state(store: FakeStore) -> AutoZeroState:
    return store.auto_zeros()


def test_FR16_restart_loads_auto_zero():
    found_store = FakeStore(revision=4)
    nodes = anchors()
    saved = [
        AutoZero(B, None, "locker", "v1", 4, T0),  # 버림 행: 자동 영점이 없다
        AutoZero(C, -3.0, "locker", "v1", 5, T0),
    ]
    found_store.rows.extend(saved)
    found_store.revision = 5
    state = found_store.auto_zeros()
    assert state.revision == 5 and set(state.zeros) == {B, C}

    estimator = new_estimator(found_store, zeros=state)

    assert estimator.calib_version == "5"
    result = ask(estimator, nodes, AT_22)
    assert result.calib_version == "5"
    assert result.unzeroed == {A, B}  # c는 처음부터 자동 영점이 있다. 버림 행의 b와 행이 없는 a는 없다
    assert found_store.attempts == []  # 같은 vendor의 관측은 아무것도 쓰지 않는다

    # 저장 행의 vendor와 다른 vendor의 첫 관측은 버림 행을 쓴다.
    estimator.observe_report(make_report(C, AT_22), node=with_vendor(nodes[C], "v2"))
    assert [(row.node_id, row.zero_db, row.vendor_firmware, row.revision) for row in found_store.rows[2:]] == [
        (C, None, "v2", 6),
    ]
    assert estimator.calib_version == "6"
    assert C in ask(estimator, {**nodes, C: with_vendor(nodes[C], "v2")}, AT_22).unzeroed


def test_FR16_restart_restore_frames_count_as_samples():
    # restore가 넘긴 프레임도 표본이 된다 (결정 4).
    store = FakeStore()
    nodes = anchors()
    frames = [frame for k in range(MIN_BINS + 2) for frame in bin_frames(k, nodes)]
    estimator = new_estimator(store)

    estimator.restore(frames, list(nodes.values()))

    assert [row.node_id for row in store.rows] == [C]


def test_FR16_restart_discards_when_vendor_changed_during_restore():
    # 재시작 때 DB의 자동 영점이 옛 vendor의 것이면, restore가 첫 프레임에서 버린다.
    store = FakeStore(revision=3)
    store.rows.append(AutoZero(C, -3.0, "locker", "v1", 3, T0))
    nodes = anchors("v2")
    estimator = new_estimator(store, zeros=store.auto_zeros())

    estimator.restore(bin_frames(0, nodes), list(nodes.values()))

    assert [(row.node_id, row.zero_db, row.vendor_firmware) for row in store.rows[1:]] == [(C, None, "v2")]


# ── 저장이 실패한다 ──────────────────────────────────────────────────


def test_FR16_auto_zero_store_failure_retries(caplog):
    store = FakeStore()
    store.failures = 1
    nodes = anchors()
    estimator = new_estimator(store)

    with caplog.at_level(logging.WARNING, logger="nearpilot.core.proximity.estimator"):
        feed(estimator, nodes, range(MIN_BINS + 2))  # 예외 없이 끝난다

    assert len(store.attempts) == 1 and store.rows == []
    assert any("자동 영점 저장에 실패" in record.getMessage() for record in caplog.records)
    result = ask(estimator, nodes, AT_22)
    assert C in result.unzeroed and result.calib_version == "0"  # 영점을 기억하지 않는다

    # 표본이 남아 있다. 다음 칸에서 표본이 늘면 다시 저장한다.
    feed(estimator, nodes, [MIN_BINS + 2])
    assert len(store.attempts) == 2
    (row,) = store.rows
    assert (row.node_id, row.zero_db) == (C, pytest.approx(-3.0))
    assert C not in ask(estimator, nodes, AT_22 + timedelta(seconds=1)).unzeroed

    # 저장 중 표시는 풀렸고, 다시 저장하지 않는다.
    feed(estimator, nodes, range(MIN_BINS + 3, MIN_BINS + 8))
    assert len(store.attempts) == 2


def test_FR16_auto_zero_store_failure_does_not_retry_without_new_samples():
    store = FakeStore()
    store.failures = 100
    nodes = anchors()
    estimator = new_estimator(store)
    feed(estimator, nodes, range(MIN_BINS + 2))
    assert len(store.attempts) == 1

    # 같은 칸의 프레임이 더 와도 표본이 늘지 않으면 다시 쓰지 않는다. 칸이 닫힐 때만 늘어난다.
    estimator.observe_report(make_report(C, AT_22), node=nodes[C])
    estimator.observe(make_frame(C, -70.0, ts_of(MIN_BINS + 1), seq=500, received_at=AT_22), node=nodes[C])
    assert len(store.attempts) == 1


def test_FR16_discard_store_failure_drops_zero(caplog):
    estimator, store, nodes = zeroed_c()
    store.failures = 1
    changed = with_vendor(nodes[C], "v2")

    with caplog.at_level(logging.WARNING, logger="nearpilot.core.proximity.estimator"):
        estimator.observe_report(make_report(C, AT_22), node=changed)  # 예외 없이 끝난다

    assert len(store.rows) == 1 and len(store.attempts) == 2  # 버림 행은 쓰지 못했다
    assert any("자동 영점 저장에 실패" in record.getMessage() for record in caplog.records)
    nodes = {**nodes, C: changed}
    result = ask(estimator, nodes, AT_22 + timedelta(seconds=1))
    assert C in result.unzeroed  # 메모리의 자동 영점은 지웠다
    assert result.calib_version == "1"  # 개정은 그대로다

    # 이미 지웠으므로 같은 vendor의 관측이 다시 버리려 하지 않는다.
    estimator.observe_report(make_report(C, AT_22 + timedelta(seconds=2), seq=2), node=changed)
    assert len(store.attempts) == 2

    # 서버를 다시 시작하면 DB의 옛 자동 영점(vendor v1)과 지금의 vendor가 달라서 다시 버린다.
    restarted = new_estimator(store, zeros=store.auto_zeros())
    restarted.observe_report(make_report(C, AT_22 + timedelta(seconds=3)), node=changed)
    assert [(row.node_id, row.zero_db, row.vendor_firmware) for row in store.rows[1:]] == [(C, None, "v2")]
    assert C in ask(restarted, nodes, AT_22 + timedelta(seconds=4)).unzeroed


def test_FR16_store_failure_never_escapes_observe():
    # 어떤 예외든 observe와 observe_report 밖으로 올리지 않는다 (IOT가 인계 실패로 보고 다시 인계한다).
    class Broken:
        def auto_zeros(self, revision=None):
            raise AssertionError("추정기는 시작할 때 받은 상태만 쓴다")

        def save_auto_zero(self, *args):
            raise RuntimeError("secret internal detail")

    nodes = anchors()
    estimator = HmmProximityEstimator(MODEL, SETTINGS, store=Broken())
    feed(estimator, nodes, range(MIN_BINS + 5))
    estimator.observe_report(make_report(C, AT_22), node=with_vendor(nodes[C], "v2"))

    assert C in ask(estimator, nodes, AT_22 + timedelta(seconds=1)).unzeroed


# ── 수집 전용 ────────────────────────────────────────────────────────


def test_FR16_collect_only_no_model():
    store = FakeStore()
    nodes = anchors()
    estimator = new_estimator(store, model=None)

    feed(estimator, nodes, range(MIN_BINS + 2))
    report_all(estimator, nodes, AT_22)

    assert store.rows == [] and store.attempts == []  # 표본을 모으지 않는다
    with pytest.raises(ModelMismatchError):
        estimator.posterior([BEACON], list(nodes.values()), AT_22, place_nodes=list(nodes.values()))
    order = estimator.distance_order(BEACON, list(nodes.values()), AT_22)
    assert order.nodes == (B, A, C)  # 거리 순서는 동작한다: 최근 3칸(19~21)의 평균 RSSI가 큰 순서
    assert estimator.calib_version == "0"
    # 더 많은 칸을 넣어도 표본을 모으지 않는다.
    feed(estimator, nodes, range(MIN_BINS + 2, MIN_BINS + 40))
    assert store.attempts == []


# ── 적합과 같은 자동 영점 ────────────────────────────────────────────


def test_FR16_fit_uses_runtime_auto_zero():
    rng = random.Random(7)
    store = FakeStore()
    nodes = anchors()
    estimator = new_estimator(store)
    sent: list[RssiFrame] = []
    found_at: dict[str, int] = {}  # 기기 → 저장이 일어난 칸 m

    for k in range(70):
        if k in (30, 31):
            continue  # 칸이 비는 구간이 있어도 같다
        sent.extend(feed(estimator, nodes, [k], rng=rng))
        for row in store.rows:
            found_at.setdefault(row.node_id, k)

    assert set(found_at) == set(ANCHORS)  # a, b는 표본이 늦게 20개가 된다
    assert len(set(found_at.values())) > 1
    for row in store.rows:
        m = found_at[row.node_id]
        last_closed = bin_index(ts_of(m - 2), 1.0)  # 절대 칸 번호로 센 칸 m − 2
        closed = [frame for frame in sent if bin_index(frame.frame_ts, 1.0) <= last_closed]  # 칸 번호 ≤ m − 2인 프레임
        offline = zeros_from_frames(closed, ANCHORS, bin_sec=1.0, min_bins=MIN_BINS)
        assert offline[row.node_id] == row.zero_db, (row.node_id, m)


# ── 판정에 쓰인 개정 번호 ────────────────────────────────────────────


def test_FR22_calib_version_recorded():
    """판정 파이프라인(다른 계층은 fake)과 이 추정기로 근접 지정 판정을 하면 저장한 판정 기록의 `calib_version`이 자동 영점 개정이다."""
    from nearpilot.core.decision.pipeline import DecisionPipeline
    from nearpilot.shared.enums import DeviceState, ReasonCode, ReserveOutcome, StoreOutcome, Verdict
    from nearpilot.shared.models import (
        PolicyCheckResult, RequestLink, ReserveResult, StoreResult, TargetSpec, UseRequest,
    )

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=T0.tzinfo)
    descriptor = legacy_descriptor("locker", Occupancy.RENTAL)
    nodes = [
        NodeInfo(
            node_id=node_id, place_id="p", kind="locker", occupancy=Occupancy.RENTAL, trust_state=TrustState.TRUSTED,
            device_state=DeviceState.AVAILABLE, exclusive_group="g", last_seen_at=now - timedelta(seconds=1),
            descriptor=descriptor,
        )
        for node_id in ANCHORS
    ]

    class Repo:
        def __init__(self):
            self.records = []

        def link_request(self, req, input_digest):
            return RequestLink("i1", True, False)

        def beacons_of(self, user_id):
            return [BEACON]

        def rssi_since(self, beacons, since):
            return [RssiFrame(node.node_id, BEACON, -60.0, now, "s1", 1, now, now) for node in nodes]

        def nodes(self, capability=None):
            return nodes

        def reserve_if_free(self, req):
            return ReserveResult(ReserveOutcome.RESERVED, use_id="use-1")

        def save_decision(self, decision, record):
            self.records.append(record)
            return StoreResult(StoreOutcome.APPLIED)

    class Policy:
        def check(self, user_id, node_id, action, now, *, internal_request_id=None, reoperate=False):
            return PolicyCheckResult(Verdict.OK, ReasonCode.OK, 1)

    class Ready:
        def can_execute(self, node_id):
            return True

    class Audit:
        def append(self, event):
            pass

    # 자동 영점 개정 3을 미리 넣은 상태로 시작한 추정기. 세 기기 모두 자동 영점이 있다.
    store = FakeStore(revision=3)
    store.rows.extend(
        AutoZero(node_id, 0.0, descriptor.vendor.model, descriptor.vendor.firmware, revision, T0)
        for revision, node_id in enumerate(ANCHORS, start=1)
    )
    settings = Settings(version="v1", threshold_locker=0.5)
    estimator = HmmProximityEstimator(MODEL, settings, zeros=store.auto_zeros(), store=store)
    for node in nodes:
        for i in range(3):  # 방문자가 locker-1 앞에 서 있다
            ts = now - timedelta(seconds=2 - i)
            rssi = MODEL.front.mean if node.node_id == A else MODEL.other.mean
            estimator.observe(make_frame(node.node_id, rssi, ts, seq=i + 1, received_at=ts), node=node)
        estimator.observe_report(make_report(node.node_id, now - timedelta(seconds=0.5)), node=node)

    repo = Repo()
    ids = iter(range(10_000))
    pipe = DecisionPipeline(repo, estimator, Policy(), Audit(), settings, Ready(), new_id=lambda: f"e{next(ids)}")
    decision = pipe.decide(UseRequest(
        user_id="u1", request_id="r1", target=TargetSpec.parse("nearest:locker"), action="open", host_kind="claude",
        received_at=now,
    ))

    assert (decision.verdict, decision.node_id) == (Verdict.OK, A)
    (record,) = repo.records
    assert record.calib_version == "3"
    assert record.model_version == MODEL.model_version


# ── 동시성 ───────────────────────────────────────────────────────────


class BlockingStore(FakeStore):
    """첫 저장이 `release`될 때까지 막히는 fake. 막힌 동안 다른 스레드가 추정기를 쓰는 상황을 만든다. 두 번째 저장부터는 막지 않는다."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0  # 저장 호출 수. 막힌 호출도 센다 (attempts는 막힌 호출이 풀린 뒤에 늘어난다)

    def save_auto_zero(self, node_id, zero_db, vendor_model, vendor_firmware, recorded_at) -> int:
        with self._lock:
            self.calls += 1
            block = self.calls == 1
        if block:
            self.entered.set()
            self.release.wait(10)
        return super().save_auto_zero(node_id, zero_db, vendor_model, vendor_firmware, recorded_at)


def test_FR16_auto_zero_not_saved_again_while_saving():
    # 결정 15: 저장소 쓰기는 잠금 밖에서 한다. 쓰는 동안 다른 스레드가 관측을 넣어도 막히지 않고, 같은 기기를 다시 저장하지 않는다.
    store = BlockingStore()
    nodes = anchors()
    estimator = new_estimator(store)
    feed(estimator, nodes, range(MIN_BINS + 1))  # 칸 0~20. 칸 0~18이 닫혀 c의 표본이 19개다
    worker = threading.Thread(target=feed, args=(estimator, nodes, [MIN_BINS + 1]), daemon=True)
    worker.start()  # 칸 21의 첫 프레임이 칸 19를 닫아 표본이 20개가 되고, 저장이 시작되어 막힌다
    assert store.entered.wait(10)

    feed(estimator, nodes, range(MIN_BINS + 2, MIN_BINS + 6))  # 막힌 동안 칸 22~25가 와서 칸 20~23이 닫힌다. 잠금이 잡혀 있으면 여기서 멈춘다
    assert store.calls == 1  # 표본이 더 쌓였어도 같은 기기를 다시 저장하지 않았다

    store.release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert store.calls == 1 and [(row.node_id, row.revision) for row in store.rows] == [(C, 1)]
    assert estimator.calib_version == "1"
    assert C not in ask(estimator, nodes, T0 + timedelta(seconds=MIN_BINS + 6)).unzeroed


def test_FR16_concurrent_auto_zero_saves_each_node_once():
    # 기기마다 스레드 하나가 칸을 같이 넘어가며 넣는다. 저장이 느려도 같은 기기를 두 번 저장하지 않고 예외도 없다.
    store = FakeStore(delay_sec=0.002)
    nodes = anchors()
    estimator = new_estimator(store)
    n_bins = 75
    start = threading.Barrier(3, timeout=30)
    errors: list[BaseException] = []

    def run(node_id: str) -> None:
        try:
            start.wait()
            for k in range(n_bins):
                (frame,) = [f for f in bin_frames(k, nodes) if f.node_id == node_id]
                estimator.observe(frame, node=nodes[node_id])
                estimator.observe_report(make_report(node_id, frame.received_at, seq=k + 1), node=nodes[node_id])
                estimator.posterior([BEACON], list(nodes.values()), frame.received_at, place_nodes=list(nodes.values()))
                # 칸을 같이 넘어간다. 한 기기가 앞서 달리면 다른 기기의 프레임이 늦은 프레임이 된다.
                sync.wait()
        except BaseException as error:  # noqa: BLE001 - 스레드의 예외를 모아 주 스레드에서 확인한다
            errors.append(error)
            sync.abort()

    sync = threading.Barrier(3, timeout=30)
    threads = [threading.Thread(target=run, args=(node_id,), daemon=True) for node_id in ANCHORS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors and not any(thread.is_alive() for thread in threads)
    assert store.max_in_flight == 1  # 같은 기기를 동시에 저장하지 않았다
    assert all(len(store.rows_of(node_id)) <= 1 for node_id in ANCHORS)  # 찾은 뒤에는 다시 저장하지 않는다
    assert len(store.rows_of(C)) == 1
    revisions = [row.revision for row in store.rows]
    assert revisions == sorted(set(revisions)) and estimator.calib_version == str(store.revision)
