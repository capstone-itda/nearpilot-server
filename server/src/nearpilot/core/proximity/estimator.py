"""`ProximityEstimator` 구현 (specs/core-proximity-estimator/spec.md 6절, specs/core-distance-selector/spec.md 6절).

프레임을 메모리 버퍼에 쌓고, `posterior`가 버퍼의 프레임으로 순수 함수 `compute_posteriors`를 부른다.
이 모듈은 어떤 프레임을 버퍼에 둘지만 정하고 사후확률은 계산하지 않는다.
`distance_order`는 신뢰 기기의 마지막 `rssi` 보고 시각과 버퍼의 프레임으로 거리 순서를 낸다. 근접 모델 파일을 쓰지 않는다.
DB를 읽거나 쓰지 않고 현재 시각도 읽지 않는다. 버퍼와 보고 시각 표는 잠금 하나로 보호한다.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from nearpilot.core.proximity.filter import ProximityInputError, compute_posteriors
from nearpilot.core.proximity.model import ModelFileError, ModelMismatchError, ProximityModel
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import TrustState
from nearpilot.shared.models import BeaconKey, DistanceOrder, NodeInfo, ProximityResult, RssiFrame, RssiReport


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
        # 근접 참여 기기의 최근 구간. 서버 수신 시각(received_at)으로 자른다 (FR-09).
        self._recent = timedelta(seconds=settings.anchor_recent_sec)
        # 관측 구간은 frame_ts로 자르고 버퍼는 received_at으로 자른다. 늦게 도착한 프레임이 구간에 들 수 있게 칸 하나를 더 둔다.
        # 최근 구간의 프레임도 남아야 해서 두 구간 중 긴 쪽에 맞춘다. 모델이 없거나 관측 구간이 짧아도 최근 구간은 남는다.
        longest_window_sec = max((model.window_sec for model in self._models.values()), default=0.0)
        self._retain = timedelta(seconds=max(longest_window_sec, settings.anchor_recent_sec) + self._bin_sec)
        self._lock = threading.Lock()
        # 같은 (node_id, session_id, seq, beacon) 프레임은 하나만 둔다. 중복 키 집합을 따로 두지 않고 키를 버퍼의 키로 쓴다.
        self._frames: dict[tuple[str, str, int, BeaconKey], RssiFrame] = {}
        # 기기 ID → 마지막 rssi 보고의 서버 수신 시각. 신뢰 기기의 보고만 둔다. 메모리에만 두고 restore가 채우지 않는다.
        self._reports: dict[str, datetime] = {}

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
        """DB에서 읽은 프레임을 `observe`와 같은 규칙으로 버퍼에 넣는다. `nodes`에 없는 기기의 프레임은 버린다.

        마지막 보고 시각은 복원하지 않는다. 재시작하면 다음 보고까지 그 기기는 거리 순서에 서지 않는다 (FR-09).
        """
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

    def observe_report(self, report: RssiReport, *, node: NodeInfo) -> None:
        # 쓰지 않는 보고는 예외 없이 버린다. 프레임이 없는 빈 보고도 받는다.
        if report.node_id != node.node_id or node.trust_state != TrustState.TRUSTED:
            return
        with self._lock:
            # 늦게 도착한 옛 보고가 마지막 보고 시각을 되돌리지 않는다.
            last = self._reports.get(report.node_id)
            if last is None or report.received_at > last:
                self._reports[report.node_id] = report.received_at

    def distance_order(self, beacon: BeaconKey, candidates: Sequence[NodeInfo], at: datetime) -> DistanceOrder:
        since = at - self._recent
        # 복사만 잠금 안에서 한다. 계산하는 동안 observe와 observe_report가 막히지 않는다.
        with self._lock:
            frames = list(self._frames.values())
            reports = dict(self._reports)
        participants = {
            node.node_id
            for node in candidates
            if node.trust_state == TrustState.TRUSTED and node.node_id in reports and reports[node.node_id] >= since
        }

        rssis: defaultdict[str, list[float]] = defaultdict(list)
        for frame in frames:
            if (
                frame.node_id in participants
                and frame.beacon == beacon
                and since <= frame.received_at <= at
                and math.isfinite(frame.rssi)  # 유한하지 않은 RSSI는 결측과 같이 버린다
            ):
                rssis[frame.node_id].append(frame.rssi)
        means = {node_id: math.fsum(values) / len(values) for node_id, values in rssis.items()}

        # 값이 큰(가까운) 기기부터, 같으면 기기 ID 순서다. 비콘을 보지 못한 기기는 끝에 기기 ID 순서로 둔다.
        heard = sorted(means, key=lambda node_id: (-means[node_id], node_id))
        unheard = sorted(participants - means.keys())
        return DistanceOrder(nodes=(*heard, *unheard), values={**means, **dict.fromkeys(unheard)})
