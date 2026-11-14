"""SmartThings 기기의 보고 상태 조회 (FR-09, FR-11, #289, specs/iot-device-reported-state).

CORE가 `get_device_state`에서 부를 때만 `main` 구성요소의 전체 상태를 한 번 읽는다.
읽기 전에 `refresh.refresh`를 한 번 보낸다. 리모컨으로 바꾼 음량처럼 클라우드에 남은 옛 값을 기기 값으로 갱신한다
(spec 결정 7, 2026-11-14 실기: refresh 응답 직후의 조회가 새 값이었다).
동작 표의 보고 키(`SmartThingsAction.reported`)로 값과 관측 시각(SmartThings `timestamp`)을 담는다.
보고 상태는 참고 정보다. 판정, 허가, 실행 성공의 근거가 아니다 (FR-07, NFR-07).
로그와 결과에 `deviceId`와 토큰을 남기지 않는다 (NFR-08).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from dataclasses import replace
from threading import Lock
from typing import Any

from nearpilot.iot.smartthings.actions import ACTIONS, REFRESH_CAPABILITY, REFRESH_COMMAND
from nearpilot.iot.smartthings.transport import (
    SmartThingsStatusSource, SmartThingsTransport, SmartThingsTransportError,
)
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


class ConfirmedValues:
    """서버 명령으로 확인한 값 (spec 결정 8). 기기 하나의 게이트웨이가 기록하고 리더가 캐시 위에 겹친다.

    결과 불명이면 캐시를 믿을 수 없다고 표시한다. 다음 조회는 SmartThings를 다시 읽는다.
    시각 비교에 쓰는 단조 시계는 리더와 같은 것을 넘긴다.
    """

    def __init__(self, monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._lock = Lock()
        self._values: dict[str, tuple[ReportedValue, float]] = {}
        self._stale_at: float | None = None

    def record(self, key: str, value: Any, observed_at: datetime) -> None:
        with self._lock:
            self._values[key] = (ReportedValue(key, value, observed_at), self._monotonic())

    def invalidate(self) -> None:
        with self._lock:
            self._stale_at = self._monotonic()

    def snapshot(self) -> tuple[dict[str, tuple[ReportedValue, float]], float | None]:
        with self._lock:
            return dict(self._values), self._stale_at


class SmartThingsReportedState:
    """바인딩한 기기 하나의 보고 상태. 조회 결과를 `cache_sec()` 초 동안 재사용한다.

    조회 실패는 캐시하지 않는다 (architecture.md "보고 상태 조회").
    `refresh` 가 참이면 읽기 전에 `refresh.refresh` 를 보낸다. 실패해도 읽기를 계속한다. 캐시 안에서는 보내지 않는다.
    `confirmed` 가 있으면 캐시를 읽은 뒤 서버 명령으로 확인한 값을 겹쳐 답한다. 결과 불명이 기록되면 캐시를 버린다.
    """

    def __init__(
        self, source: SmartThingsStatusSource, device_id: str, node_id: str, *,
        cache_sec: Callable[[], float], clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic, refresh: bool = False,
        confirmed: ConfirmedValues | None = None,
    ):
        if refresh and not isinstance(source, SmartThingsTransport):
            raise ValueError("refresh 에는 명령을 보낼 수 있는 transport 가 필요하다")
        self._source = source
        self._refresh = refresh
        self._confirmed = confirmed
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
            values, stale_at = ({}, None) if self._confirmed is None else self._confirmed.snapshot()
            if (self._cached is not None and now - self._cached_at < self._cache_sec()
                    and (stale_at is None or stale_at < self._cached_at)):
                return self._overlay(self._cached, values)
            if self._refresh:
                self._send_refresh()
            try:
                status = self._source.get_component_status(self._device_id)
            except SmartThingsTransportError as error:
                log.info("SmartThings 보고 상태를 읽지 못했다: node=%s (%s)", self._node_id, error)
                return None
            state = ReportedState(self._node_id, reported_values(status), self._clock())
            self._cached, self._cached_at = state, now
            return state

    def _overlay(self, cached: ReportedState,
                 confirmed: Mapping[str, tuple[ReportedValue, float]]) -> ReportedState:
        """캐시를 읽은 뒤 확인한 값만 같은 키 자리에 바꿔 넣는다. 바뀐 값이 없으면 캐시를 그대로 돌려준다."""
        newer = {key: value for key, (value, at) in confirmed.items() if at >= self._cached_at}
        if not newer:
            return cached
        values = [newer.pop(v.key, v) for v in cached.values]
        values.extend(newer.values())
        return replace(cached, values=tuple(values))

    def _send_refresh(self) -> None:
        # refresh 의 완료 응답은 근거가 아니다. 실패하면 클라우드에 남은 값을 그대로 읽는다.
        try:
            self._source.post_command(self._device_id, REFRESH_CAPABILITY, REFRESH_COMMAND, [])  # type: ignore[attr-defined]
        except SmartThingsTransportError as error:
            log.info("SmartThings 보고 상태 새로 고침을 보내지 못했다: node=%s (%s)", self._node_id, error)


class ReportedStateRouter:
    """`shared.interfaces.ReportedStateReader`를 구현한다. 바인딩하지 않은 기기는 None 이다."""

    def __init__(self, readers: Mapping[str, SmartThingsReportedState]):
        self._readers = dict(readers)

    def read(self, node_id: str) -> ReportedState | None:
        reader = self._readers.get(node_id)
        return None if reader is None else reader.read()
