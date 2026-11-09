"""SmartThings `NodeCommandGateway`: 전송과 상태 조회 확인 (FR-06·FR-07, specs/iot-smartthings-adapter).

`send()`는 명령을 보낸 뒤 상태를 다시 조회해서 한 호출 안에서 결과를 확정한다.
`NodeEventSink.on_result`는 쓰지 않는다 — 서버가 만든 결과이므로 `CommandResult.report`는
항상 `None`이다. 접수 응답만으로 성공을 확정하지 않는다(FR-07).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from nearpilot.iot.smartthings.actions import ACTIONS
from nearpilot.iot.smartthings.transport import SmartThingsTransport, SmartThingsTransportError
from nearpilot.shared.enums import CommandOutcome
from nearpilot.shared.models import CommandResult, NodeCommand


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Waiter:
    command: NodeCommand
    result: CommandResult | None = None
    callers: int = 0


class SmartThingsCommandGateway:
    """`shared.interfaces.NodeCommandGateway`를 구현한다."""

    def __init__(
        self, transport: SmartThingsTransport, device_id: str, *,
        poll_interval_sec: float = 2.0,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._transport = transport
        self._device_id = device_id
        self._poll_interval_sec = poll_interval_sec
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._condition = threading.Condition()
        self._known: dict[str, NodeCommand] = {}
        self._waiters: dict[str, _Waiter] = {}

    def send(self, command: NodeCommand, timeout_sec: float) -> CommandResult:
        if type(timeout_sec) not in (int, float) or not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError("Invalid command timeout")

        action = ACTIONS.get(command.action)
        if action is None:
            return self._result(command, CommandOutcome.NOT_SENT)

        with self._condition:
            known = self._known.get(command.command_id)
            if known is not None and known != command:
                raise ValueError("Command ID reused with different content")
            self._known[command.command_id] = command
            waiter = self._waiters.get(command.command_id)
            owner = waiter is None
            if owner:
                waiter = self._waiters[command.command_id] = _Waiter(command=command)
            waiter.callers += 1
        try:
            if owner:
                result = self._run(command, action, timeout_sec)
                with self._condition:
                    waiter.result = result
                    self._condition.notify_all()
                return result
            with self._condition:
                while waiter.result is None:
                    self._condition.wait()
                return waiter.result
        finally:
            with self._condition:
                waiter.callers -= 1
                if waiter.callers == 0 and self._waiters.get(command.command_id) is waiter:
                    del self._waiters[command.command_id]

    def _run(self, command: NodeCommand, action, timeout_sec: float) -> CommandResult:
        try:
            self._transport.post_command(self._device_id, action.capability, action.command, command.arguments)
        except SmartThingsTransportError:
            return self._result(command, CommandOutcome.NOT_SENT)

        expected = action.expected_value(command.arguments)
        deadline = self._monotonic() + timeout_sec
        while True:
            try:
                actual = self._transport.get_attribute(self._device_id, action.capability, action.confirm_attribute)
            except SmartThingsTransportError:
                actual = None
            if actual == expected:
                return self._result(command, CommandOutcome.SUCCEEDED)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._result(command, CommandOutcome.UNKNOWN)
            self._sleep(min(self._poll_interval_sec, remaining))

    def _result(self, command: NodeCommand, outcome: CommandOutcome) -> CommandResult:
        return CommandResult(command.command_id, outcome, report=None, reported_at=self._clock())
