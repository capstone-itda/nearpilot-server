"""기기 관리 서비스: 원격 재시작(#235, specs/iot-node-restart), 접속 설정 묶음(#234, specs/iot-ble-provisioning),
측정 영점과 영점 재탐색(#305, #307, specs/core-proximity-common-model 결정 23~25).

판정을 거치지 않는 관리 기능이다 (FR-12A, NFR-04, NFR-12). 장소 소유권만 검사하고 점유, 명령 기록, 복구 건을 바꾸지 않는다.
요청자, 기기, 결과는 서버 로그에만 남긴다. 결과에는 사용자 ID와 기기 연결 세션 ID가 없다 (NFR-08).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from nearpilot.core.proximity.autozero import measured_zero
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import RestartOutcome, TrustState, ZeroOutcome
from nearpilot.shared.interfaces import NodeRestarter, ProvisioningSource, Repository, ZeroCalibrator
from nearpilot.shared.models import NodeInfo, ProvisioningBundle, RestartResult, ZeroMeasureResult, ZeroResetResult

log = logging.getLogger(__name__)


class NodeAdminService:
    """`shared.interfaces.NodeAdminService` 구현."""

    def __init__(self, *, repository: Repository, restarter: NodeRestarter, clock: Callable[[], datetime],
                 settings: Callable[[], Settings] = Settings,
                 new_id: Callable[[], str] = lambda: str(uuid4()),
                 provisioning: ProvisioningSource | None = None,
                 calibrator: ZeroCalibrator | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._repository = repository
        self._restarter = restarter
        self._clock = clock
        self._settings = settings
        self._new_id = new_id
        self._provisioning = provisioning
        self._calibrator = calibrator
        self._sleep = sleep

    def restart(self, user_id: str, node_id: str) -> RestartResult:
        if not self._repository.is_place_owner(user_id, node_id):
            log.info("기기 재시작 거부: user=%s node=%s outcome=%s", user_id, node_id, RestartOutcome.NOT_OWNER.value)
            return RestartResult(node_id, RestartOutcome.NOT_OWNER)
        restart_id = self._new_id()
        # IOT 는 이 시각 뒤에 issued_at 을 정한다. 기기의 실제 만료는 이 값보다 늦거나 같다 (spec 6절).
        expires_at = self._clock() + timedelta(seconds=self._settings().exec_timeout_sec)
        outcome = self._restarter.restart(node_id, restart_id)
        log.info("기기 재시작 요청: user=%s node=%s restart_id=%s outcome=%s",
                 user_id, node_id, restart_id, outcome.value)
        if outcome is not RestartOutcome.SENT:
            return RestartResult(node_id, outcome)
        return RestartResult(node_id, outcome, restart_id, expires_at)

    def provisioning(self, user_id: str, node_id: str) -> ProvisioningBundle:
        """묶음을 반환한다. 비밀번호가 들어 있으므로 묶음은 로그에 쓰지 않는다 (NFR-08)."""
        if not self._repository.is_place_owner(user_id, node_id):
            log.info("기기 접속 설정 조회 거부: user=%s node=%s", user_id, node_id)
            raise PermissionError("not place owner")
        bundle = None if self._provisioning is None else self._provisioning.bundle(node_id)
        if bundle is None:
            raise LookupError("no provisioning bundle")
        log.info("기기 접속 설정 조회: user=%s node=%s", user_id, node_id)
        return bundle

    # ── 측정 영점과 영점 재탐색 (#305, #307) ───────────────────────

    def _node(self, node_id: str) -> NodeInfo | None:
        return next((node for node in self._repository.nodes() if node.node_id == node_id), None)

    def measure_zero(self, user_id: str, node_id: str) -> ZeroMeasureResult:
        """측정 시간만큼 기다린 뒤, 그 구간에 기기가 들은 소유자 활성 비콘의 RSSI로 측정 영점을 저장한다.

        소유자는 요청 전에 비콘을 거치대에 고정해 기기에서 측정 거리에 둔다. 서버는 거리를 확인하지 않는다.
        구간은 요청을 받은 시각부터 기다림이 끝난 시각까지이고 서버 수신 시각으로 자른다.
        """
        s = self._settings()
        base = {"distance_m": s.zero_measure_distance_m, "reference_dbm": s.zero_measure_reference_dbm}
        if not self._repository.is_place_owner(user_id, node_id):
            log.info("측정 영점 거부: user=%s node=%s outcome=%s", user_id, node_id, ZeroOutcome.NOT_OWNER.value)
            return ZeroMeasureResult(node_id, ZeroOutcome.NOT_OWNER, **base)
        node = self._node(node_id)
        if node is None:
            return ZeroMeasureResult(node_id, ZeroOutcome.NOT_FOUND, **base)
        if node.trust_state != TrustState.TRUSTED:
            return ZeroMeasureResult(node_id, ZeroOutcome.NOT_TRUSTED, **base)
        beacons = list(self._repository.beacons_of(user_id))
        if not beacons:
            return ZeroMeasureResult(node_id, ZeroOutcome.NO_BEACON, **base)
        started = self._clock()
        self._sleep(s.zero_measure_sec)
        ended = self._clock()
        rssis = [
            frame.rssi for frame in self._repository.rssi_since(beacons, started)
            if frame.node_id == node_id and frame.received_at <= ended
        ]
        found = measured_zero(rssis, s.zero_measure_reference_dbm, s.zero_measure_min_frames)
        if found is None:
            log.info("측정 영점: 프레임이 모자라다 user=%s node=%s frames=%d", user_id, node_id, len(rssis))
            return ZeroMeasureResult(node_id, ZeroOutcome.NOT_ENOUGH_FRAMES, frames=len(rssis), **base)
        median, zero = found
        if self._calibrator is None:
            log.error("측정 영점: 근접 추정 접점이 없다 node=%s", node_id)
            return ZeroMeasureResult(node_id, ZeroOutcome.NOT_SAVED, frames=len(rssis), median_dbm=median, **base)
        try:
            revision = self._calibrator.apply_measured_zero(node, zero, ended)
        except Exception:  # noqa: BLE001 - 저장 실패는 결과로 알린다
            log.exception("측정 영점 저장 실패: node=%s", node_id)
            return ZeroMeasureResult(node_id, ZeroOutcome.NOT_SAVED, frames=len(rssis), median_dbm=median, **base)
        log.info("측정 영점 저장: user=%s node=%s frames=%d median=%.2f zero=%.2f revision=%d",
                 user_id, node_id, len(rssis), median, zero, revision)
        return ZeroMeasureResult(node_id, ZeroOutcome.MEASURED, frames=len(rssis), median_dbm=median,
                                 zero_db=zero, revision=revision, **base)

    def reset_zero(self, user_id: str, node_id: str) -> ZeroResetResult:
        """기기 하나의 영점(측정 영점 포함)을 버리고 자동 영점을 다시 찾게 한다."""
        if not self._repository.is_place_owner(user_id, node_id):
            log.info("영점 재탐색 거부: user=%s node=%s outcome=%s", user_id, node_id, ZeroOutcome.NOT_OWNER.value)
            return ZeroResetResult(ZeroOutcome.NOT_OWNER)
        node = self._node(node_id)
        if node is None:
            return ZeroResetResult(ZeroOutcome.NOT_FOUND)
        return self._discard(user_id, [node])

    def reset_place_zeros(self, user_id: str, place_id: str) -> ZeroResetResult:
        """장소의 모든 기기(신뢰 상태와 상관없이)의 영점을 버리고 자동 영점을 다시 찾게 한다."""
        nodes = [node for node in self._repository.nodes() if node.place_id == place_id]
        if not nodes:
            return ZeroResetResult(ZeroOutcome.NOT_FOUND)
        if not self._repository.is_place_owner(user_id, nodes[0].node_id):
            log.info("영점 재탐색 거부: user=%s place=%s outcome=%s", user_id, place_id, ZeroOutcome.NOT_OWNER.value)
            return ZeroResetResult(ZeroOutcome.NOT_OWNER)
        return self._discard(user_id, nodes)

    def _discard(self, user_id: str, nodes: list[NodeInfo]) -> ZeroResetResult:
        ids = tuple(sorted(node.node_id for node in nodes))
        if self._calibrator is None:
            log.error("영점 재탐색: 근접 추정 접점이 없다 nodes=%s", ",".join(ids))
            return ZeroResetResult(ZeroOutcome.NOT_SAVED, ids)
        try:
            revision = self._calibrator.discard_zeros(nodes, self._clock())
        except Exception:  # noqa: BLE001 - 저장 실패는 결과로 알린다
            log.exception("영점 재탐색 저장 실패: nodes=%s", ",".join(ids))
            return ZeroResetResult(ZeroOutcome.NOT_SAVED, ids)
        log.info("영점 재탐색: user=%s nodes=%s revision=%d", user_id, ",".join(ids), revision)
        return ZeroResetResult(ZeroOutcome.DISCARDED, ids, revision)
