"""재시작 복원과 현재 연결 확인 (FR-19, FR-20, NFR-04).

can_execute는 판정과 실행 서비스에 ExecutionReadiness로 제공한다 (#82).
예약 대기를 넘긴 미전송 예약은 RESERVATION_EXPIRED로 취소한다 (#78, #81).
호출자는 기기별 공통 재진입 잠금과 검증된 현재 연결 보고를 제공한다.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from nearpilot.core.lifecycle.fault_recovery import FaultRecoveryService
from nearpilot.core.lifecycle.release import ReleaseService
from nearpilot.core.lifecycle.shared_light import SharedLightService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    ActuatorState, Capability, CommandOutcome, DeviceState, ExecutionStatus,
    Occupancy, ReasonCode, SessionState, StoreOutcome,
)
from nearpilot.shared.errors import StoreError, StoreOutcomeUnknown
from nearpilot.shared.interfaces import ExecutionStore, Repository
from nearpilot.shared.models import CommandRecord, NodeInfo, NodeStatus, RecoveryRecord, RestoreBundle


@dataclass
class _Restoring:
    bundle: RestoreBundle
    settings: Settings
    status: NodeStatus | None = None
    completed_connection: str | None = None
    blockers: tuple[str, ...] = ("current_status",)
    recoveries: dict[str, RecoveryRecord] = field(default_factory=dict)
    # 만료 취소의 근거. 저장 재시도에서 같은 내용을 쓴다.
    cancels: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)


class RestoreService:
    """서버 시작 때 restore(), 이후 tick()을 호출한다.

    can_execute는 ExecutionReadiness 구현이다. 판정의 ②와 실행 서비스의 전송 직전 검사가 쓴다.
    on_status는 Repository의 현재 연결과 마지막 수신을 저장한 뒤 호출한다.
    lock_for는 실행·반납·복구·관측에 쓰는 동일한 기기별 재진입 잠금이다.
    """

    def __init__(
        self, *, repository: Repository, store: ExecutionStore,
        release: ReleaseService, recovery: FaultRecoveryService,
        shared_light: SharedLightService,
        settings: Callable[[], Settings], clock: Callable[[], datetime],
        lock_for: Callable[[str], AbstractContextManager[Any]],
    ) -> None:
        self._repository = repository
        self._store = store
        self._release = release
        self._recovery = recovery
        self._shared_light = shared_light
        self._settings = settings
        self._clock = clock
        self._lock_for = lock_for
        self._started_at = clock()
        self._states: dict[str, _Restoring] = {}
        self._loaded = False
        self._index_lock = Lock()
        self._load_lock = Lock()

    def restore(self) -> None:
        """조회 전체를 확인한 뒤 공개한다. 실패하면 모든 기기의 차단을 유지한다."""
        with self._load_lock:
            if not self._loaded:
                settings = self._settings()
                inventory = tuple(self._repository.nodes())
                nodes = {node.node_id: node for node in inventory}
                if len(nodes) != len(inventory):
                    raise StoreError("기기 조회가 중복이다")
                bundles = tuple(self._store.restore_pending())
                by_node: dict[str, RestoreBundle] = {}
                for bundle in bundles:
                    self._validate_bundle(bundle)
                    node_id = bundle.node.node_id
                    if node_id in by_node:
                        raise StoreError("기기의 복원 묶음이 중복이다")
                    by_node[node_id] = bundle
                    nodes[node_id] = bundle.node
                states = {}
                for node_id, node in nodes.items():
                    bundle = by_node.get(node_id, RestoreBundle(node, self._started_at))
                    states[node_id] = _Restoring(
                        bundle, settings, recoveries={r.recovery_id: r for r in bundle.recoveries},
                    )
                with self._index_lock:
                    self._states = states
                    self._loaded = True
        self._drive()

    def tick(self) -> None:
        """실패한 저장·인계와 남은 복원을 재시도한다. 새 명령은 보내지 않는다."""
        self.restore()

    def on_status(self, report: NodeStatus) -> None:
        """검증을 마친 현재 연결의 보고만 받는다. 온라인 시각은 갱신하지 않는다."""
        with self._lock_for(report.node_id):
            state = self._state(report.node_id)
            if state is None:
                return
            node = self._node(report.node_id)
            if node is None or not self._fresh(report, node, state.settings):
                return
            previous = state.status
            if previous == report:
                # 같은 보고의 재인계도 실패한 저장과 인계를 재시도한다.
                self._process(state)
                return
            if previous is not None and previous.session_id == report.session_id and (
                report.seq <= previous.seq or report.observed_at <= previous.observed_at
                or report.received_at < previous.received_at
            ):
                return
            state.status = report
            self._process(state)

    def is_restored(self, node_id: str) -> bool:
        """복원과 현재 연결 확인을 끝냈는지 읽는다. CLOSING은 실행 가능과 다르다."""
        with self._lock_for(node_id):
            state = self._state(node_id)
            if state is None or state.completed_connection is None:
                return False
            node = self._node(node_id)
            return (
                node is not None and node.device_state != DeviceState.FAULT
                and node.session_id == state.completed_connection
                and state.status is not None and self._fresh(state.status, node, state.settings)
                and self._normal(state.status)
            )

    def can_execute(self, node_id: str) -> bool:
        """현재 연결·온라인·장애를 다시 검사한다. 호출자는 전송 직전에도 확인한다."""
        with self._lock_for(node_id):
            if not self.is_restored(node_id):
                return False
            node = self._node(node_id)
            # 복원 완료 뒤 새 요청이 만든 RESERVED도 실행 인계의 정상 상태다.
            return node is not None and node.device_state not in (DeviceState.CLOSING, DeviceState.FAULT)

    def blockers(self, node_id: str) -> tuple[str, ...]:
        """상태 관리 내부 진단이다. 호스트 응답과 공동 사유 코드가 아니다."""
        with self._lock_for(node_id):
            state = self._state(node_id)
            if state is None:
                return ("restore_not_loaded",)
            if state.completed_connection is not None and not self.is_restored(node_id):
                return ("current_status",)
            return state.blockers

    def _drive(self) -> None:
        with self._index_lock:
            states = tuple(self._states.values())
        first_error: Exception | None = None
        for state in states:
            with self._lock_for(state.bundle.node.node_id):
                try:
                    self._process(state)
                except Exception as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error

    def _process(self, state: _Restoring) -> None:
        node_id = state.bundle.node.node_id
        node = self._node(node_id)
        if node is None:
            state.completed_connection = None
            state.blockers = ("missing_node",)
            return
        # 복원 완료 뒤의 정상 운영 명령을 재시작 당시 명령으로 다루지 않는다.
        if state.completed_connection == node.session_id and state.completed_connection is not None:
            if self._is_light(node) and state.status is not None:
                try:
                    self._shared_light.on_status(state.status)
                except Exception:
                    state.completed_connection = None
                    state.blockers = ("restoring",)
                    raise
            return
        state.completed_connection = None
        state.blockers = ("restoring",)
        commands = tuple(self._restore_command(command) for command in state.bundle.commands)
        node = self._node(node_id)
        if node is None:
            raise StoreError("복원 중인 기기를 확인하지 못했다")
        recoveries = self._current_recoveries(state)
        completed_commands = {command_id for r in recoveries if r.completion is not None for command_id in r.command_ids}
        failed = tuple(c for c in commands if c.status in (ExecutionStatus.FAILED, ExecutionStatus.UNKNOWN)
                       and c.command_id not in completed_commands)
        active_recoveries = tuple(r for r in recoveries if r.completion is None)
        if node.device_state == DeviceState.FAULT:
            self._record_fault(state, commands, active_recoveries)
            state.blockers = ("fault_recovery",)
            return
        if failed or active_recoveries:
            state.blockers = ("fault_recovery",)
            return
        completed_requests = {request_id for r in recoveries if r.completion is not None
                              for request_id in r.internal_request_ids}
        pending = [c for c in commands if c.status == ExecutionStatus.PENDING and c.command_id not in completed_commands]
        linked = {c.internal_request_id for c in commands}
        unlinked = [r for r in state.bundle.requests
                    if r.internal_request_id not in linked | completed_requests | state.cancelled]
        if pending or unlinked:
            reservations = [(c.internal_request_id, c.reserved_at) for c in pending]
            reservations += [(r.internal_request_id, r.reserved_at) for r in unlinked]
            if any(at is None for _, at in reservations):
                raise StoreError("예약 시각을 확인하지 못했다")
            deadline = timedelta(seconds=state.settings.reservation_wait_sec)
            waiting = False
            for request_id, at in reservations:
                if self._clock() >= at + deadline:
                    self._cancel_expired(state, request_id, at, linked=request_id in linked)
                else:
                    waiting = True
            if waiting:
                state.blockers = ("reservation_pending",)
                return
            # 취소가 기기와 세션 상태를 바꿨으므로 다음 확인은 현재 기록으로 다시 한다.
            self._process(state)
            return
        # 요청·명령이 없는 사용분도 자동 반환하지 않는다.
        request_ids = linked | {r.internal_request_id for r in state.bundle.requests}
        if any(usage.internal_request_id not in request_ids for usage in state.bundle.approval_usages):
            state.blockers = ("unlinked_approval_usage",)
            return
        sessions = []
        for saved in state.bundle.sessions:
            current = self._repository.session(saved.session.use_id)
            if current is None or current.node_id != node_id:
                raise StoreError("점유 세션의 현재 연결을 확인하지 못했다")
            sessions.append(current)
        if node.device_state == DeviceState.RESERVED or any(s.state == SessionState.RESERVED for s in sessions):
            state.blockers = ("reservation_pending",)
            return
        report = state.status
        if report is None or not self._fresh(report, node, state.settings) or not self._normal(report):
            state.blockers = ("current_status",)
            return
        for session in sessions:
            if session.state == SessionState.CLOSING:
                self._release.resume_close(session.use_id)
        # 그 사이 기기가 격리됐으면 초기 참여와 실행 차단 해제를 보류한다.
        node = self._node(node_id)
        if node is None or node.device_state == DeviceState.FAULT:
            state.blockers = ("fault_recovery",)
            return
        if self._is_light(node):
            self._shared_light.resume(node_id)
            self._shared_light.on_status(report)
        state.completed_connection = node.session_id
        state.blockers = ()

    def _cancel_expired(self, state: _Restoring, request_id: str, reserved_at: datetime, *,
                        linked: bool) -> None:
        """예약 대기를 넘긴 미전송 예약을 취소한다. 명령이 없는 요청도 같은 함수로 취소한다 (#81)."""
        evidence = state.cancels.setdefault(request_id, {
            "source": "restore_reservation_expired", "reason": ReasonCode.RESERVATION_EXPIRED.value,
            "reserved_at": reserved_at.isoformat(), "confirmed_at": self._clock().isoformat(),
        })
        state.blockers = ("reservation_expired",)
        try:
            saved = self._store.cancel_not_sent(request_id, ExecutionStatus.PENDING,
                                                ReasonCode.RESERVATION_EXPIRED, evidence)
        except StoreOutcomeUnknown:
            current = self._store.execution(request_id) if linked else None
            if current is None or current.status != ExecutionStatus.NOT_SENT or current.evidence != evidence:
                raise
        else:
            if saved.outcome == StoreOutcome.CONFLICT:
                raise StoreError("예약 만료 취소가 충돌했다")
            if linked:
                current = saved.command or self._store.execution(request_id)
                if current is None or current.status != ExecutionStatus.NOT_SENT:
                    raise StoreError("예약 만료 취소를 확인하지 못했다")
        if not linked:
            state.cancelled.add(request_id)

    def _restore_command(self, saved: CommandRecord) -> CommandRecord:
        current = self._store.execution(saved.internal_request_id)
        self._same_command(saved, current)
        if current.status != ExecutionStatus.IN_PROGRESS:
            return current
        evidence = {"source": "server_restart", "restore_started_at": self._started_at.isoformat()}
        try:
            result = self._store.record_result(
                current.command_id, CommandOutcome.UNKNOWN, self._started_at, None, evidence,
            )
        except StoreOutcomeUnknown:
            confirmed = self._store.execution(saved.internal_request_id)
            self._same_command(saved, confirmed)
            if confirmed.status == ExecutionStatus.IN_PROGRESS:
                raise
            return confirmed
        confirmed = result.command
        if confirmed is None or result.outcome == StoreOutcome.CONFLICT:
            confirmed = self._store.execution(saved.internal_request_id)
        self._same_command(saved, confirmed)
        if confirmed.status == ExecutionStatus.IN_PROGRESS:
            raise StoreError("불명 결과 저장을 확인하지 못했다")
        return confirmed

    def _current_recoveries(self, state: _Restoring) -> tuple[RecoveryRecord, ...]:
        records = []
        for saved in state.recoveries.values():
            current = self._store.recovery(saved.recovery_id)
            if current is None or any(
                getattr(current, name) != getattr(saved, name)
                for name in ("recovery_id", "node_id", "internal_request_ids", "command_ids", "use_id", "created_at")
            ):
                raise StoreError("복구 건의 현재 연결을 확인하지 못했다")
            records.append(current)
        return tuple(records)

    def _record_fault(
        self, state: _Restoring, commands: tuple[CommandRecord, ...],
        recoveries: tuple[RecoveryRecord, ...],
    ) -> None:
        if len(recoveries) > 1:
            raise StoreError("미완료 복구 건이 둘 이상이다")
        if recoveries:
            record = recoveries[0]
            failed = [c for c in commands if c.status in (ExecutionStatus.FAILED, ExecutionStatus.UNKNOWN)]
            if any(c.command_id not in record.command_ids or c.internal_request_id not in record.internal_request_ids
                   or (c.use_id is not None and c.use_id != record.use_id) for c in failed):
                raise StoreError("불명 명령이 기존 복구 건에 연결되지 않았다")
            request_ids, command_ids, use_id = record.internal_request_ids, record.command_ids, record.use_id
        else:
            request_ids = tuple(dict.fromkeys([
                *(r.internal_request_id for r in state.bundle.requests),
                *(c.internal_request_id for c in commands),
                *(s.session.internal_request_id for s in state.bundle.sessions),
            ]))
            command_ids = tuple(c.command_id for c in commands)
            use_ids = {s.session.use_id for s in state.bundle.sessions if s.session.state != SessionState.CLOSED}
            use_ids.update(c.use_id for c in commands if c.use_id is not None)
            if len(use_ids) > 1:
                raise StoreError("기기의 점유 세션 연결이 둘 이상이다")
            use_id = next(iter(use_ids), None)
        record = self._recovery.record_isolated(
            state.bundle.node.node_id, internal_request_ids=request_ids,
            command_ids=command_ids, use_id=use_id,
        )
        state.recoveries[record.recovery_id] = record

    def _fresh(self, report: NodeStatus, node: NodeInfo, settings: Settings) -> bool:
        now = self._clock()
        ttl = timedelta(seconds=settings.online_ttl_sec)
        return (
            bool(node.session_id) and report.node_id == node.node_id and report.session_id == node.session_id
            and self._started_at <= report.received_at <= now <= report.received_at + ttl
            and node.last_seen_at is not None and report.received_at <= node.last_seen_at <= now
            and now <= node.last_seen_at + ttl
        )

    @staticmethod
    def _normal(report: NodeStatus) -> bool:
        return report.physical.sensor_fault is False and report.physical.actuator_state == ActuatorState.IDLE

    @staticmethod
    def _is_light(node: NodeInfo) -> bool:
        return node.capability == Capability.LIGHT and node.occupancy == Occupancy.SHARED

    @staticmethod
    def _same_command(expected: CommandRecord, actual: CommandRecord | None) -> None:
        if actual is None or any(
            getattr(expected, name) != getattr(actual, name)
            for name in ("command_id", "internal_request_id", "node_id", "action", "use_id", "execution_kind")
        ):
            raise StoreError("명령의 대상 연결을 확인하지 못했다")

    @staticmethod
    def _validate_bundle(bundle: RestoreBundle) -> None:
        node_id = bundle.node.node_id
        if (
            any(c.node_id != node_id for c in bundle.commands)
            or any(s.session.node_id != node_id for s in bundle.sessions)
            or any(r.node_id != node_id for r in bundle.recoveries)
        ):
            raise StoreError("복원 묶음의 기기 연결이 다르다")

    def _node(self, node_id: str) -> NodeInfo | None:
        nodes = [node for node in self._repository.nodes() if node.node_id == node_id]
        if len(nodes) > 1:
            raise StoreError("기기 조회가 중복이다")
        return nodes[0] if nodes else None

    def _state(self, node_id: str) -> _Restoring | None:
        with self._index_lock:
            return self._states.get(node_id) if self._loaded else None
