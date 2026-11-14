"""SmartThings 기기의 보고 상태 조회 (FR-09, FR-11, #289, specs/iot-device-reported-state).

CORE가 `get_device_state`에서 부를 때만 `main` 구성요소의 전체 상태를 한 번 읽는다.
동작 표의 보고 키(`SmartThingsAction.reported`)로 값과 관측 시각(SmartThings `timestamp`)을 담는다.
보고 상태는 참고 정보다. 판정, 허가, 실행 성공의 근거가 아니다 (FR-07, NFR-07).
로그와 결과에 `deviceId`와 토큰을 남기지 않는다 (NFR-08).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from nearpilot.iot.smartthings.actions import ACTIONS
from nearpilot.iot.smartthings.transport import SmartThingsStatusSource, SmartThingsTransportError
from nearpilot.shared.models import ReportedState, ReportedValue

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(raw: Any) -> datetime | None:
    """SmartThings `timestamp`(ISO 8601, `Z`)를 UTC 시각으로 읽는다. 읽지 못하면 None 이다."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def reported_values(status: Mapping[str, Any]) -> tuple[ReportedValue, ...]:
    """구성요소 상태에서 보고 상태를 고른다.

    동작의 `requires` capability가 모두 응답에 있는 동작만 담는다. 선언 합성과 같은 규칙이다.
    같은 키(`on`·`off`의 `power`)는 한 번만 담는다. 속성이 없으면 그 키를 뺀다.
    """
    values: list[ReportedValue] = []
    seen: set[str] = set()
    for action in ACTIONS.values():
        target = action.reported
        if target is None or target.key in seen:
            continue
        if not all(isinstance(status.get(capability), Mapping) for capability in action.requires):
            continue
        attributes = status.get(target.capability)
        entry = attributes.get(target.attribute) if isinstance(attributes, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        seen.add(target.key)
        values.append(ReportedValue(target.key, entry.get("value"), _timestamp(entry.get("timestamp"))))
    return tuple(values)


class SmartThingsReportedState:
    """바인딩한 기기 하나의 보고 상태. 조회 결과를 `cache_sec()` 초 동안 재사용한다.

    조회 실패는 캐시하지 않는다 (architecture.md "보고 상태 조회").
    """

    def __init__(
        self, source: SmartThingsStatusSource, device_id: str, node_id: str, *,
        cache_sec: Callable[[], float], clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._source = source
        self._device_id = device_id
        self._node_id = node_id
        self._cache_sec = cache_sec
        self._clock = clock
        self._monotonic = monotonic
        self._lock = Lock()
        self._cached: ReportedState | None = None
        self._cached_at = 0.0

    def read(self) -> ReportedState | None:
        with self._lock:
            now = self._monotonic()
            if self._cached is not None and now - self._cached_at < self._cache_sec():
                return self._cached
            try:
                status = self._source.get_component_status(self._device_id)
            except SmartThingsTransportError as error:
                log.info("SmartThings 보고 상태를 읽지 못했다: node=%s (%s)", self._node_id, error)
                return None
            state = ReportedState(self._node_id, reported_values(status), self._clock())
            self._cached, self._cached_at = state, now
            return state


class ReportedStateRouter:
    """`shared.interfaces.ReportedStateReader`를 구현한다. 바인딩하지 않은 기기는 None 이다."""

    def __init__(self, readers: Mapping[str, SmartThingsReportedState]):
        self._readers = dict(readers)

    def read(self, node_id: str) -> ReportedState | None:
        reader = self._readers.get(node_id)
        return None if reader is None else reader.read()
