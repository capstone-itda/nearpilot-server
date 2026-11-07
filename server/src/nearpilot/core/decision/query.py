"""FR-09 기기 조회: find_available_devices, get_device_state.

등록 비콘의 최근 수신으로 장소를 확인하고, 같은 장소의 사용 가능한 공개 기기를 조회한다.
기기별 근접 확률은 실행 판정에서 검사한다. 조회만 하고 저장소를 바꾸지 않는다.
응답에는 사용자 ID, 점유 주체, 근접 확률, 장소 ID를 넣지 않는다 (FR-11, NFR-08).
기기마다 종류와 동작 목록(이름, 설명, 인자 스키마)을 담는다. 제작사 정보와 선언 해시는 넣지 않는다 (FR-09, FR-11).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from nearpilot.core.decision.pipeline import select_candidates
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import capped_schema
from nearpilot.shared.enums import (
    DeviceState,
    ReasonCode,
    TrustState,
    Verdict,
    Visibility,
)
from nearpilot.shared.interfaces import ExecutionReadiness, PolicyChecker, Repository
from nearpilot.shared.models import NodeInfo

log = logging.getLogger(__name__)

_OPERABLE = "operable"
_APPROVAL_REQUIRED = "approval_required"


class DeviceQueryService:
    def __init__(
        self,
        repo: Repository,
        policy: PolicyChecker,
        settings: Settings,
        clock: Callable[[], datetime],
        readiness: ExecutionReadiness,
    ) -> None:
        self._repo = repo
        self._policy = policy
        self._settings = settings
        self._clock = clock
        self._readiness = readiness

    # ── find_available_devices ──────────────────────────────────────

    def find_available_devices(self, user_id: str) -> list[dict[str, Any]]:
        s, now = self._settings, self._clock()
        beacons = self._repo.beacons_of(user_id)
        if len(beacons) > 1:
            raise ValueError("활성 비콘이 둘 이상이다")
        if not beacons:
            return []
        frames = self._repo.rssi_since(beacons, now - timedelta(seconds=s.beacon_recent_sec))
        if not frames:
            return []
        all_nodes = self._repo.nodes()
        by_node = {n.node_id: n for n in all_nodes}
        anchors = {f.node_id for f in frames
                   if f.node_id in by_node and by_node[f.node_id].trust_state is TrustState.TRUSTED}
        if not anchors:
            return []
        places = {by_node[a].place_id for a in anchors}
        if len(places) != 1 or None in places:
            raise ValueError("비콘을 본 기기의 장소가 하나로 정해지지 않는다")
        place_id = places.pop()
        found: list[dict[str, Any]] = []
        for node in select_candidates(all_nodes, None, s, now, place_id, self._readiness.can_execute):
            if node.device_state is not DeviceState.AVAILABLE:
                continue
            access = self._access(user_id, node, now)
            if access is not None:
                found.append({
                    "node_id": node.node_id, "kind": node.kind,
                    "occupancy": node.occupancy.value, "access": access,
                    "actions": self._actions(node)})
        found.sort(key=lambda item: item["node_id"])
        return found

    def _access(self, user_id: str, node: NodeInfo, now: datetime) -> str | None:
        """조작 가능·승인 필요. 숨길 기기는 None 이다. 한 기기의 오류는 그 기기만 뺀다.

        정책 검사의 동작은 기기가 선언한 첫 번째 역할 없는 동작이다. 없으면 숨긴다 (spec core-device-actions 결정 9).
        """
        action = _first_plain_action(node)
        if action is None:
            return None
        try:
            if self._repo.current_policy(node.node_id).visibility is not Visibility.PUBLIC:
                return None
            check = self._policy.check(user_id, node.node_id, action, now)
        except Exception:  # noqa: BLE001 - 알 수 없는 오류는 보여 주지 않는다
            log.exception("기기 정책 조회 중 예외: %s", node.node_id)
            return None
        if check.verdict is Verdict.OK:
            return _OPERABLE
        if check.verdict is Verdict.NEED_APPROVAL:
            return _APPROVAL_REQUIRED
        return None

    # ── get_device_state ────────────────────────────────────────────

    def get_device_state(self, user_id: str, node_id: str) -> Mapping[str, Any]:
        now = self._clock()
        node = next((n for n in self._repo.nodes() if n.node_id == node_id), None)
        if node is None:
            return _unavailable()
        policy = self._repo.current_policy(node_id)
        if policy.visibility is not Visibility.PUBLIC and policy.approver_user_id != user_id:
            if not self._repo.has_open_session(user_id, node_id):  # 소유자가 아니어도 점유 중인 본인은 본다
                return _unavailable()  # 없는 기기와 구분하지 않는다
        return {
            "node_id": node.node_id,
            "kind": node.kind,
            "occupancy": node.occupancy.value,
            "state": _state_of(node.device_state),
            "online": self._online(node, now),
            "trusted": node.trust_state is TrustState.TRUSTED,
            "access_mode": policy.mode.value,
            "actions": self._actions(node),
        }

    def _actions(self, node: NodeInfo) -> list[dict[str, Any]]:
        """기기 선언의 동작 목록. 인자 스키마의 상한은 선언 값과 `param_max` 중 작은 값이다 (FR-09).

        역할, 제작사 정보, 선언 해시는 넣지 않는다 (FR-11). 선언이 없으면 빈 목록이다.
        """
        if node.descriptor is None:
            return []
        return [
            {"name": spec.name, "description": spec.description,
             "input_schema": capped_schema(spec, self._settings.param_caps(node.kind, spec.name))}
            for spec in node.descriptor.actions
        ]

    def _online(self, node: NodeInfo, now: datetime) -> bool:
        if node.last_seen_at is None:
            return False
        return now - node.last_seen_at <= timedelta(seconds=self._settings.online_ttl_sec)


def _first_plain_action(node: NodeInfo) -> str | None:
    """역할이 없는 첫 동작. 새 점유를 시작하는 일반 동작이다 (FR-09)."""
    if node.descriptor is None:
        return None
    return next((spec.name for spec in node.descriptor.actions if spec.role is None), None)


def _state_of(state: DeviceState) -> str:
    """점유 단계는 점유 주체 없이 하나로 합쳐서 보여 준다."""
    if state is DeviceState.AVAILABLE:
        return "available"
    if state is DeviceState.FAULT:
        return "fault"
    return "occupied"


def _unavailable() -> dict[str, Any]:
    return {"reason_code": ReasonCode.TARGET_UNAVAILABLE.value}
