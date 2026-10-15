"""`tests/core/proximity/` 공통 도우미 (specs/core-proximity-estimator/spec.md).

일반 함수와 상수로 두어서 테스트 모듈이 `from .conftest import ...`로 가져다 쓴다.
`fixtures/models/`의 모델 파일은 테스트용 합성 값이다. 운영 값이 아니다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.shared.enums import Capability, DeviceState, Occupancy, TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, RssiFrame

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("11111111-2222-3333-4444-555555555555", 1, 1)
OTHER_BEACON = BeaconKey(BEACON.uuid, 1, 2)
MODEL_ROOT = Path(__file__).resolve().parent / "fixtures" / "models"
PLACE_A_NODES = ("locker-1", "locker-2", "locker-3", "light-1", "light-2", "speaker-1")

_CAPABILITY_BY_PREFIX = {
    "locker": Capability.LOCKER,
    "light": Capability.LIGHT,
    "mood": Capability.LIGHT,
    "speaker": Capability.SPEAKER,
}


def make_node(
    node_id: str,
    place_id: str = "place-a",
    *,
    trust: TrustState = TrustState.TRUSTED,
    capability: Capability | None = None,
    device_state: DeviceState = DeviceState.AVAILABLE,
) -> NodeInfo:
    """capability가 None이면 node_id 접두어로 정한다 (locker→LOCKER, light/mood→LIGHT, speaker→SPEAKER).

    occupancy는 LOCKER면 RENTAL, 그 외 SHARED. exclusive_group은 None, last_seen_at은 T0.
    """
    if capability is None:
        capability = _CAPABILITY_BY_PREFIX[node_id.partition("-")[0]]
    occupancy = Occupancy.RENTAL if capability == Capability.LOCKER else Occupancy.SHARED
    return NodeInfo(
        node_id=node_id,
        place_id=place_id,
        capability=capability,
        occupancy=occupancy,
        trust_state=trust,
        device_state=device_state,
        exclusive_group=None,
        last_seen_at=T0,
    )


def place_a_nodes(**overrides: TrustState) -> list[NodeInfo]:
    """PLACE_A_NODES 순서의 NodeInfo 목록. overrides로 node_id별 trust를 바꾼다.

    node_id에 하이픈이 있어서 `place_a_nodes(**{"locker-2": TrustState.REVOKED})`로 쓴다.
    """
    unknown = sorted(set(overrides) - set(PLACE_A_NODES))
    if unknown:
        raise ValueError(f"place-a에 없는 기기다: {unknown}")
    return [
        make_node(node_id, trust=overrides.get(node_id, TrustState.TRUSTED))
        for node_id in PLACE_A_NODES
    ]


def make_frame(
    node_id: str,
    rssi: float,
    frame_ts: datetime,
    *,
    beacon: BeaconKey = BEACON,
    seq: int = 1,
    session_id: str | None = None,
    received_at: datetime | None = None,
) -> RssiFrame:
    """session_id 기본값은 f"s-{node_id}". sent_at은 frame_ts. received_at 기본값은 frame_ts + 50 ms."""
    return RssiFrame(
        node_id=node_id,
        beacon=beacon,
        rssi=rssi,
        frame_ts=frame_ts,
        session_id=session_id if session_id is not None else f"s-{node_id}",
        seq=seq,
        sent_at=frame_ts,
        received_at=received_at if received_at is not None else frame_ts + timedelta(milliseconds=50),
    )


@pytest.fixture
def model_root() -> Path:
    return MODEL_ROOT
