"""공용 조명의 유지 대상자와 꺼짐 후보 (FR-20, NFR-12).

실제 꺼짐 명령의 저장·전송과 활성화는 선행 계약을 기다린다.
호출자는 검증된 관측과 저장을 확인한 실행 결과만 인계한다.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, Capability, DeviceState, ExecutionKind, ExecutionStatus, Occupancy,
    TrustState, Verdict,
)
from nearpilot.shared.interfaces import PolicyChecker, Repository
from nearpilot.shared.models import (
    ExecutionContext, ExecutionResult, NodeInfo, NodeStatus, RssiFrame,
)


@dataclass
class _Holder:
    settings: Settings
    last_received_at: datetime
    last_observed_at: datetime | None = None


@dataclass
class _Light:
    settings: Settings
    holders: dict[str, _Holder] = field(default_factory=dict)
    successes: dict[str, tuple[ExecutionContext, ExecutionResult]] = field(default_factory=dict)
    resumed_at: datetime | None = None
    resumed_session_id: str | None = None
    status: NodeStatus | None = None
    status_after: datetime | None = None


class SharedLightService:
    """메모리 상태만 관리한다. tick 반환은 실행 허가나 명령이 아니다.

    lock_for는 실행·관측·복구와 같은 기기별 재진입 잠금이다.
    can_request_off도 후보 조회다. 실제 전송과의 원자성은 후속 계약이 정한다.
    """

    def __init__(
        self, *, repository: Repository, policy: PolicyChecker,
        settings: Callable[[], Settings], clock: Callable[[], datetime],
        lock_for: Callable[[str], AbstractContextManager[Any]],
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._settings = settings
        self._clock = clock
        self._lock_for = lock_for
        self._lights: dict[str, _Light] = {}
        self._index_lock = Lock()

    def on_execution_result(self, context: ExecutionContext, result: ExecutionResult) -> None:
        """저장을 확인한 성공만 인계한다. 같은 성공은 타이머를 바꾸지 않는다."""
        if context.internal_request_id != result.internal_request_id or context.use_id != result.use_id:
            raise ValueError("실행 결과의 요청 또는 점유 연결이 다르다")
        if (
            context.execution_kind != ExecutionKind.SHARED
            or result.status != ExecutionStatus.SUCCEEDED
        ):
            return
        if context.request.action not in (Action.ON, Action.OFF):
            raise ValueError("공용 조명의 동작이 아니다")
        if not result.command_id:
            raise ValueError("성공한 실행의 명령 ID가 필요하다")
        with self._lock_for(context.node_id):
            node = self._node(context.node_id)
            if node is None or not self._is_light(node):
                return
            light = self._light(context.node_id)
            previous = light.successes.get(context.internal_request_id)
            if previous is not None:
                if previous != (context, result):
                    raise ValueError("같은 성공 인계의 내용이 다르다")
                return
            now = self._clock()
            if context.request.action == Action.OFF:
                light.holders.clear()
                # 뒤늦은 켜짐 status로 같은 꺼짐을 다시 요청하지 않는다.
                light.status_after = now
            elif self._policy.check(
                context.request.user_id, context.node_id, Action.ON, now,
            ).verdict == Verdict.OK:
                old = light.holders.get(context.request.user_id)
                light.holders[context.request.user_id] = _Holder(
                    context.settings, now, None if old is None else old.last_observed_at,
                )
            light.successes[context.internal_request_id] = (context, result)

    def resume(self, node_id: str) -> None:
        """복원과 현재 기기 확인 뒤 호출한다. 같은 연결의 재인계는 대기를 연장하지 않는다."""
        with self._lock_for(node_id):
            node = self._node(node_id)
            if node is None or not self._is_light(node) or not node.session_id:
                raise ValueError("현재 연결을 확인한 공용 조명이 필요하다")
            light = self._light(node_id)
            if light.resumed_at is not None and light.resumed_session_id == node.session_id:
                return
            light.settings = self._settings()
            light.resumed_at = self._clock()
            light.resumed_session_id = node.session_id
            light.status = None
            light.status_after = None

    def on_status(self, status: NodeStatus) -> None:
        """IOT가 검증한 보고를 보존한다. Repository의 온라인 시각을 바꾸지 않는다."""
        with self._lock_for(status.node_id):
            node = self._node(status.node_id)
            if node is None or not self._is_light(node) or status.session_id != node.session_id:
                return
            if status.received_at > self._clock():
                return
            light = self._light(status.node_id)
            if light.status_after is not None and status.received_at <= light.status_after:
                return
            old = light.status
            if old is not None and old.session_id == status.session_id and (
                status.seq <= old.seq or status.observed_at <= old.observed_at
                or status.received_at < old.received_at
            ):
                return
            light.status = status

    def observe(self, frame: RssiFrame) -> None:
        """IOT가 검증한 관측을 같은 장소의 기존 유지 대상자에게만 반영한다."""
        for node_id in self._node_ids():
            with self._lock_for(node_id):
                nodes = {node.node_id: node for node in self._repository.nodes()}
                light_node, anchor = nodes.get(node_id), nodes.get(frame.node_id)
                if (
                    light_node is None or anchor is None or light_node.place_id is None
                    or light_node.place_id != anchor.place_id or frame.session_id != anchor.session_id
                    or frame.received_at > self._clock()
                ):
                    continue
                light = self._light(node_id)
                updates = []
                for user_id, holder in light.holders.items():
                    if frame.beacon not in self._repository.beacons_of(user_id):
                        continue
                    deadline = holder.last_received_at + timedelta(
                        seconds=holder.settings.leave_absent_sec + holder.settings.leave_grace_sec,
                    )
                    if frame.received_at >= deadline:
                        # 유예가 끝난 뒤 관측만으로 다시 참여할 수 없다.
                        continue
                    if holder.last_observed_at is None or frame.frame_ts > holder.last_observed_at:
                        updates.append(holder)
                # 비콘 조회가 실패하면 어느 유지 대상자의 타이머도 바꾸지 않는다.
                for holder in updates:
                    holder.last_observed_at = frame.frame_ts
                    holder.last_received_at = max(holder.last_received_at, frame.received_at)

    def tick(self) -> tuple[str, ...]:
        """이탈을 검사하고 꺼짐 후보를 반환한다. 명령을 만들거나 보내지 않는다."""
        return tuple(node_id for node_id in self._node_ids() if self.can_request_off(node_id))

    def can_request_off(self, node_id: str) -> bool:
        """현재 유지 대상자·연결·온라인·보고를 다시 검사한다. 실행 허가가 아니다."""
        with self._lock_for(node_id):
            with self._index_lock:
                light = self._lights.get(node_id)
            if light is None:
                return False
            node = self._node(node_id)
            if node is None or not self._is_light(node):
                return False
            now = self._clock()
            self._expire_holders(node_id, light, now)
            if light.holders or light.resumed_at is None:
                return False
            initial_wait = light.settings.leave_absent_sec + light.settings.leave_grace_sec
            if now < light.resumed_at + timedelta(seconds=initial_wait):
                return False
            status = light.status
            if (
                node.trust_state != TrustState.TRUSTED or node.device_state != DeviceState.AVAILABLE
                or not node.session_id or node.session_id != light.resumed_session_id
                or node.last_seen_at is None or status is None or status.session_id != node.session_id
                or status.received_at < light.resumed_at or status.physical.light_on is not True
                or (light.status_after is not None and status.received_at <= light.status_after)
                or status.physical.sensor_fault is not False
            ):
                return False
            ttl = timedelta(seconds=light.settings.online_ttl_sec)
            return (
                node.last_seen_at <= now <= node.last_seen_at + ttl
                and status.received_at <= now <= status.received_at + ttl
            )

    def _expire_holders(self, node_id: str, light: _Light, now: datetime) -> None:
        expired = []
        for user_id, holder in light.holders.items():
            allowed = self._policy.check(user_id, node_id, Action.ON, now).verdict == Verdict.OK
            deadline = holder.last_received_at + timedelta(
                seconds=holder.settings.leave_absent_sec + holder.settings.leave_grace_sec,
            )
            if not allowed or now >= deadline:
                expired.append(user_id)
        # 조회 오류를 권한 만료로 바꾸지 않는다. 모든 검사를 마친 뒤 목록을 바꾼다.
        for user_id in expired:
            del light.holders[user_id]

    def _light(self, node_id: str) -> _Light:
        with self._index_lock:
            light = self._lights.get(node_id)
            if light is None:
                light = _Light(self._settings())
                self._lights[node_id] = light
            return light

    def _node(self, node_id: str) -> NodeInfo | None:
        return next((node for node in self._repository.nodes() if node.node_id == node_id), None)

    def _node_ids(self) -> tuple[str, ...]:
        with self._index_lock:
            return tuple(self._lights)

    @staticmethod
    def _is_light(node: NodeInfo) -> bool:
        return node.capability == Capability.LIGHT and node.occupancy == Occupancy.SHARED
