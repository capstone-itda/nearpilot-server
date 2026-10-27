"""FR-09 기기 조회: find_available_devices, get_device_state.

후보 조건은 `request_use` 의 ①②③ 과 같다 (`select_candidates`, `threshold_for`). 목록에 나온 기기가
요청에서 같은 조건으로 거부되지 않게 한다. 조회만 하고 저장소를 바꾸지 않는다.
응답에는 사용자 ID, 점유 주체, 근접 확률, 장소 ID를 넣지 않는다 (FR-11, NFR-08).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from nearpilot.core.decision.pipeline import select_candidates, threshold_for
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action,
    Capability,
    DeviceState,
    ReasonCode,
    TrustState,
    Verdict,
    Visibility,
)
from nearpilot.shared.interfaces import ExecutionReadiness, PolicyChecker, ProximityEstimator, Repository
from nearpilot.shared.models import NodeInfo

log = logging.getLogger(__name__)

# 조작 가능·승인 필요를 나누는 정책 검사의 동작 (기능별 기본 동작)
_DEFAULT_ACTION: dict[Capability, Action] = {
    Capability.LOCKER: Action.OPEN,
    Capability.LIGHT: Action.ON,
    Capability.SPEAKER: Action.ON,
}

_OPERABLE = "operable"
_APPROVAL_REQUIRED = "approval_required"


class DeviceQueryService:
    def __init__(
        self,
        repo: Repository,
        proximity: ProximityEstimator,
        policy: PolicyChecker,
        settings: Settings,
        clock: Callable[[], datetime],
        readiness: ExecutionReadiness,
    ) -> None:
        self._repo = repo
        self._proximity = proximity
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
        place_nodes = [n for n in all_nodes if n.place_id == place_id]

        found: list[tuple[float, str, dict[str, Any]]] = []
        for capability in Capability:
            threshold = threshold_for(s, capability)
            if threshold is None:
                continue
            candidates = select_candidates(
                all_nodes, capability, s, now, place_id, self._readiness.can_execute)
            if not candidates:
                continue
            posteriors = dict(self._proximity.posterior(
                beacons, candidates, now, place_nodes=place_nodes).posteriors)
            for node in candidates:
                p = posteriors.get(node.node_id, 0.0)
                if p < threshold or node.device_state is not DeviceState.AVAILABLE:
                    continue
                access = self._access(user_id, node, now)
                if access is not None:
                    found.append((p, node.node_id, {
                        "node_id": node.node_id, "capability": node.capability.value,
                        "occupancy": node.occupancy.value, "access": access}))
        found.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in found]

    def _access(self, user_id: str, node: NodeInfo, now: datetime) -> str | None:
        """조작 가능·승인 필요. 숨길 기기는 None 이다. 한 기기의 오류는 그 기기만 뺀다."""
        try:
            if self._repo.current_policy(node.node_id).visibility is not Visibility.PUBLIC:
                return None
            check = self._policy.check(user_id, node.node_id, _DEFAULT_ACTION[node.capability], now)
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
            "capability": node.capability.value,
            "occupancy": node.occupancy.value,
            "state": _state_of(node.device_state),
            "online": self._online(node, now),
            "trusted": node.trust_state is TrustState.TRUSTED,
            "access_mode": policy.mode.value,
        }

    def _online(self, node: NodeInfo, now: datetime) -> bool:
        if node.last_seen_at is None:
            return False
        return now - node.last_seen_at <= timedelta(seconds=self._settings.online_ttl_sec)


def _state_of(state: DeviceState) -> str:
    """점유 단계는 점유 주체 없이 하나로 합쳐서 보여 준다."""
    if state is DeviceState.AVAILABLE:
        return "available"
    if state is DeviceState.FAULT:
        return "fault"
    return "occupied"


def _unavailable() -> dict[str, Any]:
    return {"reason_code": ReasonCode.TARGET_UNAVAILABLE.value}
