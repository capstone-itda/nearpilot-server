"""`ProximityEstimator` 구현 (specs/core-proximity-estimator/spec.md 6절).

프레임을 메모리 버퍼에 쌓고, `posterior`가 버퍼의 프레임으로 순수 함수 `compute_posteriors`를 부른다.
이 모듈은 어떤 프레임을 버퍼에 둘지만 정하고 사후확률은 계산하지 않는다.
DB를 읽거나 쓰지 않고 현재 시각도 읽지 않는다. 버퍼는 잠금 하나로 보호한다.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from nearpilot.core.proximity.filter import ProximityInputError, compute_posteriors
from nearpilot.core.proximity.model import ModelFileError, ModelMismatchError, ProximityModel
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import TrustState
from nearpilot.shared.models import BeaconKey, NodeInfo, ProximityResult, RssiFrame


class HmmProximityEstimator:
    """`ProximityEstimator` Protocol 구현. `restore`는 Protocol 밖의 재시작 복원 접점이다."""

    def __init__(self, models: Mapping[str, ProximityModel], settings: Settings) -> None:
        for place_id, model in models.items():
            if model.place_id != place_id:
                raise ModelFileError(
                    f"models의 키가 모델의 장소와 다르다 (키={place_id!r}, 모델={model.place_id!r}, 버전={model.model_version!r})"
                )
        self._models = dict(models)
        self._bin_sec = settings.rssi_frame_sec
        # 관측 구간은 frame_ts로 자르고 버퍼는 received_at으로 자른다. 늦게 도착한 프레임이 구간에 들 수 있게 칸 하나를 더 둔다.
        longest_window_sec = max((model.window_sec for model in self._models.values()), default=0.0)
        self._retain = timedelta(seconds=longest_window_sec + self._bin_sec)
        self._lock = threading.Lock()
        # 같은 (node_id, session_id, seq, beacon) 프레임은 하나만 둔다. 중복 키 집합을 따로 두지 않고 키를 버퍼의 키로 쓴다.
        self._frames: dict[tuple[str, str, int, BeaconKey], RssiFrame] = {}

    def observe(self, frame: RssiFrame, *, node: NodeInfo) -> None:
        # 쓰지 않는 프레임은 예외 없이 버린다.
        if frame.node_id != node.node_id or node.trust_state != TrustState.TRUSTED:
            return
        key = (frame.node_id, frame.session_id, frame.seq, frame.beacon)
        with self._lock:
            if key in self._frames:
                return
            self._frames[key] = frame
            # 가장 늦은 프레임은 자르는 시각보다 늦으므로 버퍼에 남는다. 버퍼의 최댓값이 지금까지 받은 가장 늦은 received_at이다.
            cutoff = max(kept.received_at for kept in self._frames.values()) - self._retain
            for stale in [k for k, kept in self._frames.items() if kept.received_at < cutoff]:
                del self._frames[stale]

    def restore(self, frames: Iterable[RssiFrame], nodes: Sequence[NodeInfo]) -> None:
        """DB에서 읽은 프레임을 `observe`와 같은 규칙으로 버퍼에 넣는다. `nodes`에 없는 기기의 프레임은 버린다."""
        by_id = {node.node_id: node for node in nodes}
        for frame in frames:
            node = by_id.get(frame.node_id)
            if node is not None:
                self.observe(frame, node=node)

    def posterior(
        self, beacons: Sequence[BeaconKey], candidates: Sequence[NodeInfo], at: datetime,
        *, place_nodes: Sequence[NodeInfo],
    ) -> ProximityResult:
        # 장소를 알아야 모델을 고를 수 있어서 여기서 먼저 본다. 나머지 입력 검사는 compute_posteriors가 한다.
        if len(beacons) != 1:
            raise ProximityInputError(f"beacons는 활성 비콘 하나여야 한다 (개수={len(beacons)})")
        place_ids = {node.place_id for node in place_nodes}
        if len(place_ids) != 1 or None in place_ids:
            raise ProximityInputError(
                f"place_nodes는 place_id가 있는 한 장소의 기기여야 한다 (place_id={sorted(map(str, place_ids))})"
            )
        (place_id,) = place_ids
        model = self._models.get(place_id)
        if model is None:
            raise ModelMismatchError(f"장소 {place_id!r}의 근접 모델이 없다 (모델이 있는 장소: {sorted(self._models)})")

        # 복사만 잠금 안에서 한다. 계산하는 동안 observe가 막히지 않는다.
        with self._lock:
            frames = list(self._frames.values())
        return compute_posteriors(model, frames, beacons[0], candidates, place_nodes, at, bin_sec=self._bin_sec)
