"""검증한 기기 보고를 상태 관리에 인계한다 (FR-04, FR-05, FR-07, FR-13)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import logging
from threading import Lock
from typing import Any

from nearpilot.core.lifecycle.execution import ExecutionService
from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.core.lifecycle.restore import RestoreService
from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.shared.enums import Action, DeviceState, ExecutionStatus, SessionState, StoreOutcome
from nearpilot.shared.errors import StoreError
from nearpilot.shared.interfaces import ExecutionStore, ProximityEstimator, Repository
from nearpilot.shared.models import (
    ExecutionContext, ExecutionResult, NodeAnnounce, NodeInfo, NodeResult, NodeStatus,
    RestoreBundle, RssiFrame, SafetyCheckResult,
)

_log = logging.getLogger(__name__)


@dataclass
class _Fault:
    evidence: dict[str, Any]
    links: tuple[tuple[str, ...], tuple[str, ...], str | None] | None = None


@dataclass
class _RssiDelivery:
    frame: RssiFrame
    saved: bool = False
    observed: bool = False
    light_observed: bool = False
    lock: Any = field(default_factory=Lock)



def _action(context: ExecutionContext) -> Action:
    """서버 내부 요청에는 방문자 요청이 없다 (#86)."""
    return context.internal_request.action if context.request is None else context.request.action


class NodeEventService:
    """NodeEventSink의 CORE 구현. 입력의 연결·최신성 검사는 IOT가 맡는다.

    lock_for는 실행·반납·복원·복구와 같은 기기별 재진입 잠금이다.
    context_for는 명령 ID로 원래 ExecutionContext를 조회한다. 재시작 뒤에도
    원래 설정을 반환해야 한다. 문맥을 찾지 못하면 결과를 전달 완료로 보지 않는다.
    """

    def __init__(
        self, *, repository: Repository, store: ExecutionStore,
        proximity: ProximityEstimator, execution: ExecutionService,
        release: ReleaseService, restore: RestoreService,
        recovery: FaultRecoveryService, shared_light: SharedLightService,
        context_for: Callable[[str], ExecutionContext | None],
        lock_for: Callable[[str], AbstractContextManager[Any]],
    ) -> None:
        self._repository, self._store = repository, store
        self._proximity, self._execution = proximity, execution
        self._release, self._restore = release, restore
        self._recovery, self._shared_light = recovery, shared_light
        self._context_for, self._lock_for = context_for, lock_for
        self._faults: dict[str, _Fault] = {}
        self._statuses: dict[str, NodeStatus] = {}
        self._rssi: dict[tuple[Any, ...], _RssiDelivery] = {}
        self._rssi_done: dict[tuple[Any, ...], RssiFrame] = {}
        self._status_done: dict[str, NodeStatus] = {}
        self._index_lock = Lock()

    def on_announce(self, announce: NodeAnnounce) -> None:
        with self._lock_for(announce.node_id):
            old = self._node(announce.node_id, required=False)
            if old is not None and (
                old.capability != announce.capability or old.occupancy != announce.occupancy
            ):
                occupied = old.device_state in (
                    DeviceState.RESERVED, DeviceState.ACTIVE, DeviceState.CLOSING,
                )
                if not occupied:
                    bundle = self._bundle(announce.node_id)
                    occupied = bundle is not None and any(
                        s.session.state != SessionState.CLOSED for s in bundle.sessions
                    )
                if occupied:
                    self._faults.setdefault(announce.node_id, _Fault({
                        "source": "announce_changed", "session_id": announce.session_id,
                        "seq": announce.seq, "received_at": announce.received_at,
                        "previous_capability": old.capability.value,
                        "previous_occupancy": old.occupancy.value,
                        "capability": announce.capability.value,
                        "occupancy": announce.occupancy.value,
                    }))
            # 발표 저장과 격리는 별개 쓰기다. 점유 중 변경은 먼저 격리해야
            # 발표 저장 직후 재시작해도 변경 전 점유의 연결을 잃지 않는다.
            if announce.node_id in self._faults:
                self._isolate(announce.node_id)
            self._repository.upsert_announced_node(announce)

    def on_rssi(self, frame: RssiFrame) -> None:
        key = (frame.node_id, frame.session_id, frame.seq, frame.beacon)
        stream = (frame.node_id, frame.beacon)
        with self._index_lock:
            done = self._rssi_done.get(stream)
            if (key not in self._rssi and done is not None
                and done.session_id == frame.session_id and done.seq >= frame.seq):
                if done.seq == frame.seq and done != frame:
                    raise ValueError("같은 RSSI 사건의 내용이 다르다")
                return
            delivery = self._rssi.setdefault(key, _RssiDelivery(frame))
        with delivery.lock:
            if delivery.frame != frame:
                raise ValueError("같은 RSSI 사건의 내용이 다르다")
            error: Exception | None = None
            with self._lock_for(frame.node_id):
                node = self._node(frame.node_id)
                if node.session_id != frame.session_id:
                    raise StoreError("RSSI의 현재 기기 연결을 확인하지 못했다")
                if not delivery.saved:
                    self._repository.save_rssi(frame)
                    delivery.saved = True
                self._repository.touch_node(frame.node_id, frame.received_at)
                node = self._node(frame.node_id)
                if not delivery.observed:
                    try:
                        self._proximity.observe(frame, node=node)
                    except Exception as caught:
                        _log.exception("RSSI 근접 관측 인계 실패: %s", frame.node_id)
                        error = caught
                    else:
                        delivery.observed = True
            # observe는 같은 장소의 여러 조명 잠금을 잡는다. 앵커 잠금은 먼저 푼다.
            if not delivery.light_observed:
                try:
                    self._shared_light.observe(frame)
                except Exception as caught:
                    _log.exception("RSSI 이탈 관측 인계 실패: %s", frame.node_id)
                    if error is None:
                        error = caught
                else:
                    delivery.light_observed = True
            if error is not None:
                raise error
            with self._index_lock:
                done = self._rssi_done.get(stream)
                if done is None or done.session_id != frame.session_id or done.seq < frame.seq:
                    self._rssi_done[stream] = frame
                self._rssi.pop(key, None)

    def on_status(self, status: NodeStatus) -> None:
        with self._lock_for(status.node_id):
            done = self._status_done.get(status.node_id)
            if done is not None and done.session_id == status.session_id and done.seq >= status.seq:
                if done.seq == status.seq and done != status:
                    raise ValueError("같은 상태 사건의 내용이 다르다")
                return
            if status.physical.sensor_fault is True:
                self._faults.setdefault(status.node_id, _Fault({
                    "source": "sensor_fault", "session_id": status.session_id,
                    "seq": status.seq, "observed_at": status.observed_at,
                    "received_at": status.received_at,
                }))
            if status.node_id in self._faults:
                self._isolate(status.node_id)
            self._repository.set_node_session(status.node_id, status.session_id, status.received_at)
            self._repository.touch_node(status.node_id, status.received_at)
            with self._index_lock:
                self._statuses[status.node_id] = status
            self._restore.on_status(status)
            self._shared_light.on_status(status)
            self._status_done[status.node_id] = status

    def on_result(self, result: NodeResult) -> None:
        with self._lock_for(result.node_id):
            context = self._context_for(result.command_id)
            if context is None:
                raise StoreError("명령의 원래 실행 문맥이 없다")
            command = self._store.execution(context.internal_request_id)
            if command is None or (
                command.command_id != result.command_id
                or command.node_id != result.node_id or context.node_id != result.node_id
                or command.internal_request_id != context.internal_request_id
                or command.action != _action(context) or command.use_id != context.use_id
                or command.usage_id != context.usage_id
                or command.execution_kind != context.execution_kind
            ):
                raise StoreError("결과의 명령과 실행 문맥 연결이 다르다")
            self._execution.on_result(context, result)
            saved = self._store.execution(context.internal_request_id)
            if saved is None or saved.command_id != result.command_id:
                raise StoreError("인계한 결과의 저장 기록을 확인하지 못했다")
            if saved.status == ExecutionStatus.SUCCEEDED:
                self._shared_light.on_execution_result(context, ExecutionResult(
                    internal_request_id=context.internal_request_id, status=saved.status,
                    command_id=saved.command_id, use_id=saved.use_id,
                ))

    def on_safety_check_result(self, result: SafetyCheckResult) -> None:
        with self._lock_for(result.node_id):
            self._release.on_safety_check_result(result)

    def latest_status(self, node_id: str) -> NodeStatus | None:
        """내부 물리 관측 조회다. 반납 안전이나 재사용 허가가 아니다."""
        with self._lock_for(node_id):
            with self._index_lock:
                return self._statuses.get(node_id)

    def _isolate(self, node_id: str) -> None:
        fault = self._faults[node_id]
        result = self._store.isolate_device(node_id, fault.evidence)
        if result.outcome == StoreOutcome.CONFLICT or self._node(node_id).device_state != DeviceState.FAULT:
            raise StoreError("기기 격리의 저장을 확인하지 못했다")
        if fault.links is None:
            fault.links = self._links(self._bundle(node_id))
        request_ids, command_ids, use_id = fault.links
        self._recovery.record_isolated(node_id, internal_request_ids=request_ids,
                                       command_ids=command_ids, use_id=use_id)
        del self._faults[node_id]

    def _node(self, node_id: str, *, required: bool = True) -> NodeInfo | None:
        nodes = [node for node in self._repository.nodes() if node.node_id == node_id]
        if len(nodes) > 1 or (required and not nodes):
            raise StoreError("기기 조회의 연결을 확인하지 못했다")
        return nodes[0] if nodes else None

    def _bundle(self, node_id: str) -> RestoreBundle | None:
        bundles = [bundle for bundle in self._store.restore_pending() if bundle.node.node_id == node_id]
        if len(bundles) > 1:
            raise StoreError("기기의 복원 묶음이 중복이다")
        return bundles[0] if bundles else None

    @staticmethod
    def _links(bundle: RestoreBundle | None) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
        if bundle is None:
            return (), (), None
        node_id = bundle.node.node_id
        if (any(c.node_id != node_id for c in bundle.commands)
            or any(s.session.node_id != node_id for s in bundle.sessions)
            or any(r.node_id != node_id for r in bundle.recoveries)):
            raise StoreError("복원 묶음의 기기 연결이 다르다")
        active = [r for r in bundle.recoveries if r.completion is None]
        if len(active) > 1:
            raise StoreError("미완료 복구 건이 둘 이상이다")
        if active:
            record = active[0]
            return record.internal_request_ids, record.command_ids, record.use_id
        sessions = [s.session for s in bundle.sessions if s.session.state != SessionState.CLOSED]
        use_ids = {s.use_id for s in sessions}
        use_ids.update(c.use_id for c in bundle.commands if c.use_id is not None)
        if len(use_ids) > 1:
            raise StoreError("기기의 점유 세션 연결이 둘 이상이다")
        request_ids = tuple(dict.fromkeys([
            *(r.internal_request_id for r in bundle.requests),
            *(c.internal_request_id for c in bundle.commands),
            *(s.internal_request_id for s in sessions),
        ]))
        return request_ids, tuple(c.command_id for c in bundle.commands), next(iter(use_ids), None)
