"""SmartThings 온라인 하트비트 (FR-13, specs/iot-smartthings-adapter).

`smartthings_poll_sec`마다 호출자(상태 관리의 주기 작업)가 `poll_once()`를 부른다.
조회에 성공하면 `NodeEventSink.on_status()`를 불러 `last_seen_at`을 갱신시킨다.
실패하면 부르지 않는다 — 유효하지 않은 응답으로 온라인 시각을 갱신하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol

from nearpilot.iot.smartthings.transport import SmartThingsTransport, SmartThingsTransportError
from nearpilot.shared.enums import CommandState
from nearpilot.shared.models import NodeStatus


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _StatusSink(Protocol):
    def on_status(self, status: NodeStatus) -> None: ...


class SmartThingsHeartbeat:
    def __init__(
        self, transport: SmartThingsTransport, device_id: str, node_id: str, session_id: str,
        sink: _StatusSink, *, clock: Callable[[], datetime] = _utc_now,
    ):
        self._transport = transport
        self._device_id = device_id
        self._node_id = node_id
        self._session_id = session_id
        self._sink = sink
        self._clock = clock
        self._seq = 0

    def poll_once(self) -> NodeStatus | None:
        """상태를 한 번 조회한다. 성공하면 `on_status`를 부르고 그 값을 반환한다. 실패하면 `None`."""
        try:
            self._transport.get_attribute(self._device_id, "switch", "switch")
        except SmartThingsTransportError:
            return None

        self._seq += 1
        now = self._clock()
        status = NodeStatus(
            node_id=self._node_id, received_at=now, session_id=self._session_id, seq=self._seq,
            sent_at=now, observed_at=now, command_state=CommandState.IDLE,
        )
        self._sink.on_status(status)
        return status
