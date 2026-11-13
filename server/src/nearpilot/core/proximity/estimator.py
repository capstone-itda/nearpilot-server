"""`ProximityEstimator` 구현 (specs/core-proximity-common-model/spec.md 5절·6절, specs/core-distance-selector/spec.md 6절).

프레임을 메모리 버퍼에 쌓고, `posterior`가 버퍼의 프레임으로 순수 함수 `compute_posteriors`를 부른다.
이 모듈은 어떤 프레임을 버퍼에 둘지, 어떤 기기가 근접 참여 기기인지, 기기별 자동 영점이 무엇인지만 정하고 사후확률은 계산하지 않는다.
자동 영점은 장소의 관측만으로 찾는다. 칸(절대 칸)이 닫힐 때마다 `bin_samples`의 표본을 모으고, 표본이 모델의 `auto_zero_min_bins`
이상이면 `zero_from_samples`로 값을 내서 `AutoZeroStore`에 저장한다. 찾은 값은 `vendor`(모델, 펌웨어)가 바뀔 때까지 고정한다.
`vendor`는 자동 영점을 버리는 조건으로만 쓰고 판정 분기에는 쓰지 않는다.
`distance_order`는 신뢰 기기의 마지막 `rssi` 보고 시각과 버퍼의 프레임으로 거리 순서를 낸다. 근접 모델 파일을 쓰지 않는다.
현재 시각을 읽지 않는다. 자동 영점 행의 `recorded_at`은 그 행을 만든 프레임이나 보고의 `received_at`이다.
버퍼, 보고 시각 표, 자동 영점 상태는 잠금 하나로 보호하고, 저장소 쓰기는 잠금 밖에서 한다 (결정 15).
저장소 예외는 `observe`와 `observe_report` 밖으로 올리지 않는다 (IOT가 인계 실패로 보고 다시 인계한다).
"""

from __future__ import annotations

import logging
import math
import threading
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nearpilot.core.proximity.autozero import bin_index, bin_samples, zero_from_samples
from nearpilot.core.proximity.common_model import CommonModel, ModelMismatchError
from nearpilot.core.proximity.posterior import ProximityInputError, compute_posteriors
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import TrustState
from nearpilot.shared.interfaces import AutoZeroStore
from nearpilot.shared.models import (
    AutoZero, AutoZeroState, BeaconKey, DistanceOrder, NodeInfo, ProximityResult, RssiFrame, RssiReport,
)

log = logging.getLogger(__name__)

# 기기 선언의 (vendor.model, vendor.firmware). 선언이 없으면 (None, None)이다.
_VendorKey = tuple[str | None, str | None]


def _vendor_key(node: NodeInfo) -> _VendorKey:
    if node.descriptor is None:
        return (None, None)
    return (node.descriptor.vendor.model, node.descriptor.vendor.firmware)


@dataclass(frozen=True, slots=True)
class _Write:
    """잠금 밖에서 할 저장소 쓰기 하나. `zero_db`가 None이면 버림 행이다."""

    node_id: str
    zero_db: float | None
    vendor: _VendorKey
    recorded_at: datetime


@dataclass(slots=True)
class _Track:
    """한 `(장소, 비콘)`의 칸 상태. 지금까지 가장 큰 칸 번호와, 아직 닫지 않은 칸의 앵커별 RSSI다."""

    latest: int | None = None
    open_bins: dict[int, dict[str, list[float]]] = field(default_factory=dict)


class HmmProximityEstimator:
    """`ProximityEstimator` Protocol 구현. `restore`는 Protocol 밖의 재시작 복원 접점이다.

    `model`이 None이면 수집 전용이다. 프레임과 보고를 받고 `distance_order`는 동작하지만, 자동 영점 표본을 모으지 않고
    `posterior`는 `ModelMismatchError`다. `zeros`는 시작할 때 DB에서 읽은 자동 영점 상태(버림 행 포함)이고,
    `store`가 None이면 자동 영점을 DB에 쓰지 않고 개정 번호를 메모리에서만 올린다.
    """

    def __init__(
        self, model: CommonModel | None, settings: Settings, *,
        zeros: AutoZeroState | None = None, store: AutoZeroStore | None = None,
    ) -> None:
        self._model = model
        self._store = store
        self._bin_sec = settings.rssi_frame_sec
        # 근접 참여 기기의 최근 구간. 서버 수신 시각(received_at)으로 자른다 (FR-09).
        self._recent = timedelta(seconds=settings.anchor_recent_sec)
        # 관측 구간은 frame_ts로 자르고 버퍼는 received_at으로 자른다. 늦게 도착한 프레임이 구간에 들 수 있게 칸 하나를 더 둔다.
        # 최근 구간의 프레임도 남아야 해서 두 구간 중 긴 쪽에 맞춘다. 모델이 없거나 관측 구간이 짧아도 최근 구간은 남는다.
        window_sec = 0.0 if model is None else model.window_sec
        self._retain = timedelta(seconds=max(window_sec, settings.anchor_recent_sec) + self._bin_sec)
        self._lock = threading.Lock()
        # 같은 (node_id, session_id, seq, beacon) 프레임은 하나만 둔다. 중복 키 집합을 따로 두지 않고 키를 버퍼의 키로 쓴다.
        self._frames: dict[tuple[str, str, int, BeaconKey], RssiFrame] = {}
        # 기기 ID → 마지막 rssi 보고의 서버 수신 시각. 신뢰 기기의 보고만 둔다. 메모리에만 두고 restore가 채우지 않는다.
        self._reports: dict[str, datetime] = {}
        # 개정 번호와 기기별 자동 영점 행. 버림 행(zero_db가 None)도 둔다. 자동 영점이 있는 기기는 zero_db가 None이 아닌 행이 있는 기기다.
        self._revision = 0 if zeros is None else zeros.revision
        self._zeros: dict[str, AutoZero] = {} if zeros is None else dict(zeros.zeros)
        # 자동 영점 표본 상태. 메모리에만 두고, 찾기 전에 서버를 다시 시작하면 처음부터 모은다.
        self._tracks: dict[tuple[str, BeaconKey], _Track] = {}
        self._samples: dict[str, list[float]] = {}
        self._vendor_seen: dict[str, _VendorKey] = {}  # 기기 ID → 마지막으로 본 vendor 열쇠. 모으던 표본의 vendor다
        self._saving: set[str] = set()  # 저장소에 쓰는 중인 기기. 같은 기기를 두 번 저장하지 않고 그동안 표본도 더하지 않는다

    @property
    def calib_version(self) -> str:
        """지금 알고 있는 최신 개정 번호의 10진 문자열. 개정이 없으면 `"0"`이다."""
        with self._lock:
            return str(self._revision)

    def observe(self, frame: RssiFrame, *, node: NodeInfo) -> None:
        # 쓰지 않는 프레임은 예외 없이 버린다.
        if frame.node_id != node.node_id or node.trust_state != TrustState.TRUSTED:
            return
        key = (frame.node_id, frame.session_id, frame.seq, frame.beacon)
        writes: list[_Write] = []
        with self._lock:
            self._check_vendor(node, frame.received_at, writes)
            if key not in self._frames:
                self._frames[key] = frame
                # 가장 늦은 프레임은 자르는 시각보다 늦으므로 버퍼에 남는다. 버퍼의 최댓값이 지금까지 받은 가장 늦은 received_at이다.
                cutoff = max(kept.received_at for kept in self._frames.values()) - self._retain
                for stale in [k for k, kept in self._frames.items() if kept.received_at < cutoff]:
                    del self._frames[stale]
                self._collect(frame, node, writes)
        self._flush(writes)

    def restore(self, frames: Iterable[RssiFrame], nodes: Sequence[NodeInfo]) -> None:
        """DB에서 읽은 프레임을 `observe`와 같은 규칙으로 버퍼에 넣는다. `nodes`에 없는 기기의 프레임은 버린다.

        자동 영점 표본도 `observe`와 같이 모은다. 마지막 보고 시각은 복원하지 않는다.
        재시작하면 다음 보고까지 그 기기는 거리 순서에 서지 않는다 (FR-09).
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
        # 입력 관계를 먼저 본다. 나머지 입력 검사는 compute_posteriors가 한다.
        if len(beacons) != 1:
            raise ProximityInputError(f"beacons는 활성 비콘 하나여야 한다 (개수={len(beacons)})")
        place_ids = {node.place_id for node in place_nodes}
        if len(place_ids) != 1 or None in place_ids:
            raise ProximityInputError(
                f"place_nodes는 place_id가 있는 한 장소의 기기여야 한다 (place_id={sorted(map(str, place_ids))})"
            )
        model = self._model
        if model is None:
            raise ModelMismatchError("근접 모델 없이 시작했다 (수집 전용)")

        since = at - self._recent
        # 복사만 잠금 안에서 한다. 계산하는 동안 observe가 막히지 않는다.
        with self._lock:
            frames = list(self._frames.values())
            reports = dict(self._reports)
            zeros = {node_id: row.zero_db for node_id, row in self._zeros.items() if row.zero_db is not None}
            revision = self._revision
        # 근접 참여 기기는 최근 구간에 보고한 trusted 기기다. distance_order와 같은 기준이다.
        participants = {
            node.node_id
            for node in place_nodes
            if node.trust_state == TrustState.TRUSTED and node.node_id in reports and reports[node.node_id] >= since
        }
        return compute_posteriors(
            model, frames, beacons[0], candidates, place_nodes, at,
            bin_sec=self._bin_sec, participants=participants, zeros=zeros, calib_version=str(revision),
        )

    def observe_report(self, report: RssiReport, *, node: NodeInfo) -> None:
        # 쓰지 않는 보고는 예외 없이 버린다. 프레임이 없는 빈 보고도 받는다.
        if report.node_id != node.node_id or node.trust_state != TrustState.TRUSTED:
            return
        writes: list[_Write] = []
        with self._lock:
            self._check_vendor(node, report.received_at, writes)
            # 늦게 도착한 옛 보고가 마지막 보고 시각을 되돌리지 않는다.
            last = self._reports.get(report.node_id)
            if last is None or report.received_at > last:
                self._reports[report.node_id] = report.received_at
        self._flush(writes)

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
        # 프레임 수는 평균을 낸 리스트의 길이다. 정렬 값과 같은 프레임을 센다. 비콘을 보지 못한 기기는 0이다 (경계 확률의 N, #278).
        counts = {**{node_id: len(values) for node_id, values in rssis.items()}, **dict.fromkeys(unheard, 0)}
        return DistanceOrder(nodes=(*heard, *unheard), values={**means, **dict.fromkeys(unheard)}, counts=counts)

    # ── 자동 영점 (잠금 안에서 부른다) ──────────────────────────────

    def _has_zero(self, node_id: str) -> bool:
        row = self._zeros.get(node_id)
        return row is not None and row.zero_db is not None

    def _check_vendor(self, node: NodeInfo, received_at: datetime, writes: list[_Write]) -> None:
        """신뢰 기기의 `vendor`가 바뀌었는지 보고, 바뀌었으면 자동 영점을 버린다. 재부팅(`session_id`)은 보지 않는다."""
        key = _vendor_key(node)
        previous = self._vendor_seen.get(node.node_id)
        self._vendor_seen[node.node_id] = key
        if previous is not None and previous != key:
            # 모으던 표본의 vendor가 지금과 다르다. 표본만 지우고 DB에는 쓰지 않는다.
            self._samples.pop(node.node_id, None)
        row = self._zeros.get(node.node_id)
        if row is not None and row.zero_db is not None and (row.vendor_model, row.vendor_firmware) != key:
            # 이 판단 뒤 바로 unzeroed다. 쓰기가 실패해도 메모리의 자동 영점은 지운 채로 둔다.
            del self._zeros[node.node_id]
            self._samples.pop(node.node_id, None)
            self._saving.add(node.node_id)
            writes.append(_Write(node.node_id, None, key, received_at))

    def _collect(self, frame: RssiFrame, node: NodeInfo, writes: list[_Write]) -> None:
        """새 프레임을 칸에 더하고, 닫힌 칸에서 표본을 모아 찾은 자동 영점의 쓰기를 예약한다."""
        model = self._model
        if model is None or node.place_id is None or not math.isfinite(frame.rssi):
            return
        k = bin_index(frame.frame_ts, self._bin_sec)
        track = self._tracks.setdefault((node.place_id, frame.beacon), _Track())
        if track.latest is not None and k <= track.latest - 2:
            return  # 이미 닫은 칸의 늦은 프레임이다. 표본이 아니고, 사후확률 버퍼에는 이미 들어 있다.
        track.open_bins.setdefault(k, {}).setdefault(node.node_id, []).append(frame.rssi)
        if track.latest is None or k > track.latest:
            track.latest = k

        # 가장 큰 칸보다 2 이상 작은 열린 칸을 번호 순서로 닫는다.
        grown: set[str] = set()
        for closing in sorted(number for number in track.open_bins if number <= track.latest - 2):
            means = {
                anchor_id: math.fsum(values) / len(values)
                for anchor_id, values in track.open_bins.pop(closing).items()
            }
            for anchor_id, sample in bin_samples(means).items():
                if self._has_zero(anchor_id) or anchor_id in self._saving:
                    continue
                self._samples.setdefault(anchor_id, []).append(sample)
                grown.add(anchor_id)

        for anchor_id in sorted(grown):
            zero = zero_from_samples(self._samples[anchor_id], model.auto_zero_min_bins)
            if zero is None:
                continue
            self._saving.add(anchor_id)
            writes.append(_Write(anchor_id, zero, self._vendor_seen[anchor_id], frame.received_at))

    # ── 저장소 쓰기 (잠금 밖) ───────────────────────────────────────

    def _flush(self, writes: list[_Write]) -> None:
        """예약한 쓰기를 순서대로 한다. 저장소 예외는 올리지 않는다 (IOT가 인계 실패로 본다)."""
        for write in writes:
            saved: int | None = None  # 저장소가 돌려준 개정 번호. 저장소가 없으면 None이고 메모리에서 올린다
            failed = False
            if self._store is not None:
                try:
                    saved = self._store.save_auto_zero(
                        write.node_id, write.zero_db, write.vendor[0], write.vendor[1], write.recorded_at,
                    )
                except Exception:
                    failed = True
                    kind = "버림" if write.zero_db is None else "값"
                    log.warning("자동 영점 저장에 실패했다: node=%s 종류=%s", write.node_id, kind, exc_info=True)
            with self._lock:
                self._saving.discard(write.node_id)
                if failed:
                    # 찾은 값은 기억하지 않고 표본을 남긴다. 다음에 표본이 늘 때 다시 저장한다. 버림은 이미 메모리에서 지웠다.
                    continue
                revision = self._revision + 1 if saved is None else saved
                self._revision = max(self._revision, revision)
                self._zeros[write.node_id] = AutoZero(
                    node_id=write.node_id, zero_db=write.zero_db, vendor_model=write.vendor[0],
                    vendor_firmware=write.vendor[1], revision=revision, recorded_at=write.recorded_at,
                )
                if write.zero_db is not None:
                    self._samples.pop(write.node_id, None)
