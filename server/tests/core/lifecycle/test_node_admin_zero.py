"""기기 관리 서비스의 측정 영점(#307)과 영점 재탐색(#305) (FR-16, spec 결정 23, 24). 저장소와 근접 추정은 fake다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.core.lifecycle.node_admin import NodeAdminService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import DeviceState, Occupancy, TrustState, ZeroOutcome
from nearpilot.shared.models import BeaconKey, NodeInfo, RssiFrame

NOW = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("11111111-2222-3333-4444-555555555555", 1, 2)
SETTINGS = Settings(zero_measure_sec=10.0, zero_measure_reference_dbm=-44.0, zero_measure_min_frames=3,
                    zero_measure_distance_m=0.5)


def node(node_id: str, place_id: str = "place-a", trust: TrustState = TrustState.TRUSTED) -> NodeInfo:
    return NodeInfo(node_id=node_id, place_id=place_id, kind="light", occupancy=Occupancy.SHARED,
                    trust_state=trust, device_state=DeviceState.AVAILABLE, exclusive_group=None, last_seen_at=NOW)


def frame(node_id: str, rssi: float, received_at: datetime, seq: int = 1) -> RssiFrame:
    return RssiFrame(node_id=node_id, beacon=BEACON, rssi=rssi, frame_ts=received_at, session_id="s",
                     seq=seq, sent_at=received_at, received_at=received_at)


class Clock:
    def __init__(self) -> None:
        self.now = NOW
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class Repository:
    def __init__(self, nodes, *, owner=True, beacons=(BEACON,), frames=()):
        self._nodes = list(nodes)
        self.owner = owner
        self.beacons = list(beacons)
        self.frames = list(frames)
        self.since: list[datetime] = []

    def is_place_owner(self, user_id, node_id):
        return self.owner and any(n.node_id == node_id for n in self._nodes)

    def nodes(self, kind=None):
        return self._nodes

    def beacons_of(self, user_id):
        return self.beacons

    def rssi_since(self, beacons, since):
        self.since.append(since)
        return [f for f in self.frames if f.received_at >= since and f.beacon in beacons]


class Calibrator:
    def __init__(self, error: Exception | None = None) -> None:
        self.measured: list[tuple[str, float, datetime]] = []
        self.discarded: list[tuple[list[str], datetime]] = []
        self.error = error

    def apply_measured_zero(self, node, zero_db, recorded_at):
        if self.error:
            raise self.error
        self.measured.append((node.node_id, zero_db, recorded_at))
        return 8

    def discard_zeros(self, nodes, recorded_at):
        if self.error:
            raise self.error
        self.discarded.append(([n.node_id for n in nodes], recorded_at))
        return 9


def service(repo, calibrator=None, clock=None):
    clock = clock or Clock()
    return NodeAdminService(repository=repo, restarter=None, clock=clock, settings=lambda: SETTINGS,
                            calibrator=calibrator, sleep=clock.sleep), clock


def in_window(node_id="light-1"):
    """측정 구간(NOW ~ NOW+10초)의 프레임 넷과 구간 밖 프레임, 다른 기기의 프레임."""
    return [
        frame(node_id, -48.0, NOW + timedelta(seconds=1), 1),
        frame(node_id, -50.0, NOW + timedelta(seconds=2), 2),
        frame(node_id, -46.0, NOW + timedelta(seconds=3), 3),
        frame(node_id, -90.0, NOW + timedelta(seconds=10), 4),  # 구간 끝도 넣는다
        frame(node_id, -20.0, NOW + timedelta(seconds=11), 5),  # 구간 뒤
        frame(node_id, -10.0, NOW - timedelta(seconds=1), 6),  # 구간 앞
        frame("light-2", -30.0, NOW + timedelta(seconds=2), 7),  # 다른 기기
    ]


def test_FR16_measure_zero_saves_measured_row():
    repo = Repository([node("light-1")], frames=in_window())
    calibrator = Calibrator()
    admin, clock = service(repo, calibrator)
    result = admin.measure_zero("owner", "light-1")
    assert clock.slept == [10.0] and repo.since == [NOW]
    # 구간 안 그 기기의 값 −48, −50, −46, −90 의 중앙값 −49, 영점 −49 − (−44) = −5
    assert (result.outcome, result.frames, result.median_dbm, result.zero_db, result.revision) == (
        ZeroOutcome.MEASURED, 4, -49.0, -5.0, 8)
    assert (result.distance_m, result.reference_dbm) == (0.5, -44.0)
    assert calibrator.measured == [("light-1", -5.0, NOW + timedelta(seconds=10))]


def test_FR16_measure_zero_not_owner():
    repo = Repository([node("light-1")], owner=False, frames=in_window())
    calibrator = Calibrator()
    admin, clock = service(repo, calibrator)
    result = admin.measure_zero("visitor", "light-1")
    assert result.outcome is ZeroOutcome.NOT_OWNER
    assert clock.slept == [] and calibrator.measured == []  # 기다리지 않는다


def test_FR16_measure_zero_requires_trusted_and_beacon():
    calibrator = Calibrator()
    admin, clock = service(Repository([node("light-1", trust=TrustState.DISCOVERED)]), calibrator)
    assert admin.measure_zero("owner", "light-1").outcome is ZeroOutcome.NOT_TRUSTED
    admin, clock = service(Repository([node("light-1")], beacons=()), calibrator)
    assert admin.measure_zero("owner", "light-1").outcome is ZeroOutcome.NO_BEACON
    assert clock.slept == [] and calibrator.measured == []


def test_FR16_measure_zero_not_enough_frames():
    repo = Repository([node("light-1")], frames=in_window()[:2])
    calibrator = Calibrator()
    admin, _ = service(repo, calibrator)
    result = admin.measure_zero("owner", "light-1")
    assert (result.outcome, result.frames, result.zero_db) == (ZeroOutcome.NOT_ENOUGH_FRAMES, 2, None)
    assert calibrator.measured == []


def test_FR16_measure_zero_store_failure():
    admin, _ = service(Repository([node("light-1")], frames=in_window()), Calibrator(OSError("locked")))
    result = admin.measure_zero("owner", "light-1")
    assert (result.outcome, result.median_dbm, result.zero_db, result.revision) == (
        ZeroOutcome.NOT_SAVED, -49.0, None, None)
    admin, _ = service(Repository([node("light-1")], frames=in_window()), None)
    assert admin.measure_zero("owner", "light-1").outcome is ZeroOutcome.NOT_SAVED


def test_FR16_reset_zero_single_node():
    calibrator = Calibrator()
    admin, _ = service(Repository([node("light-1"), node("light-2")]), calibrator)
    result = admin.reset_zero("owner", "light-2")
    assert (result.outcome, result.nodes, result.revision) == (ZeroOutcome.DISCARDED, ("light-2",), 9)
    assert calibrator.discarded == [(["light-2"], NOW)]
    admin, _ = service(Repository([node("light-1")], owner=False), calibrator)
    assert admin.reset_zero("visitor", "light-1").outcome is ZeroOutcome.NOT_OWNER


def test_FR16_reset_place_zeros_all_nodes():
    calibrator = Calibrator()
    nodes = [node("light-2"), node("light-1", trust=TrustState.DISCOVERED), node("light-9", place_id="place-b")]
    admin, _ = service(Repository(nodes), calibrator)
    result = admin.reset_place_zeros("owner", "place-a")
    assert (result.outcome, result.nodes, result.revision) == (ZeroOutcome.DISCARDED, ("light-1", "light-2"), 9)
    assert sorted(calibrator.discarded[0][0]) == ["light-1", "light-2"]
    admin, _ = service(Repository(nodes, owner=False), calibrator)
    assert admin.reset_place_zeros("visitor", "place-a").outcome is ZeroOutcome.NOT_OWNER


def test_FR16_reset_place_zeros_unknown_place():
    calibrator = Calibrator()
    admin, _ = service(Repository([node("light-1")]), calibrator)
    assert admin.reset_place_zeros("owner", "place-x").outcome is ZeroOutcome.NOT_FOUND
    admin, _ = service(Repository([node("light-1")]), Calibrator(OSError("locked")))
    assert admin.reset_place_zeros("owner", "place-a").outcome is ZeroOutcome.NOT_SAVED
    assert calibrator.discarded == []
