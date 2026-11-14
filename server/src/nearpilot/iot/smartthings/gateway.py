"""SmartThings `NodeCommandGateway`: 전송과 상태 조회 확인 (FR-06·FR-07, specs/iot-smartthings-companion).

`send()`는 명령을 보낸 뒤 상태를 다시 조회해서 한 호출 안에서 결과를 확정한다.
`NodeEventSink.on_result`는 쓰지 않는다 — 서버가 만든 결과이므로 `CommandResult.report`는
항상 `None`이고, 성공 근거는 `CommandResult.confirmation`이다. 접수 응답과 `refresh`의 완료 응답은
성공 근거가 아니다. 상태 조회 값만 근거다 (FR-07).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from nearpilot.iot.smartthings.actions import (
    ACTIONS, CHANGED, REFRESH_CAPABILITY, REFRESH_COMMAND, Confirm, Plan,
)
from nearpilot.iot.smartthings.reported import ConfirmedValues
from nearpilot.iot.smartthings.transport import (
    SmartThingsMaybeSent, SmartThingsTransport, SmartThingsTransportError,
)
from nearpilot.shared.enums import CommandOutcome
from nearpilot.shared.models import CommandResult, NodeCommand

log = logging.getLogger(__name__)

_NO_VALUE = object()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Waiter:
    command: NodeCommand
    result: CommandResult | None = None
    callers: int = 0


class SmartThingsCommandGateway:
    """`shared.interfaces.NodeCommandGateway`를 구현한다. 기기(`deviceId`) 하나를 맡는다.

    `deviceId`는 이 객체 밖으로 내보내지 않는다. 로그와 결과에는 `node_id`·`command_id`만 쓴다 (NFR-08).
    """

    def __init__(
        self, transport: SmartThingsTransport, device_id: str, *,
        poll_interval_sec: float,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        confirmed: ConfirmedValues | None = None,
    ):
        if type(poll_interval_sec) not in (int, float) or not math.isfinite(poll_interval_sec) \
                or poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec 는 양수여야 한다")
        self._transport = transport
        self._device_id = device_id
        self._poll_interval_sec = poll_interval_sec
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._confirmed_values = confirmed
        self._condition = threading.Condition()
        self._known: dict[str, NodeCommand] = {}
        self._waiters: dict[str, _Waiter] = {}
        # 상대 명령(changed)의 결과. 같은 command_id 로 다시 보내지 않는다 (spec 결정 6). 프로세스 메모리에만 둔다.
        self._relative: dict[str, CommandResult] = {}

    def send(self, command: NodeCommand, timeout_sec: float) -> CommandResult:
        if type(timeout_sec) not in (int, float) or not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError("Invalid command timeout")

        action = ACTIONS.get(command.action)
        plan = None if action is None else action.plan(command.arguments)
        if plan is None:
            return self._result(command, CommandOutcome.NOT_SENT)

        with self._condition:
            known = self._known.get(command.command_id)
            if known is not None and known != command:
                raise ValueError("Command ID reused with different content")
            self._known[command.command_id] = command
            done = self._relative.get(command.command_id)
            if done is not None:
                return done
            waiter = self._waiters.get(command.command_id)
            owner = waiter is None
            if owner:
                waiter = self._waiters[command.command_id] = _Waiter(command=command)
            waiter.callers += 1
        try:
            if owner:
                result = self._run(command, plan, timeout_sec)
                self._remember(command, result)
                with self._condition:
                    if plan.confirm.method == CHANGED:
                        self._relative[command.command_id] = result
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

    # ── 한 번의 전송과 확인 ──────────────────────────────────────────

    def _run(self, command: NodeCommand, plan: Plan, timeout_sec: float) -> CommandResult:
        deadline = self._monotonic() + timeout_sec

        if plan.mode_name is not None:
            plan = self._with_mode_name(plan)
            if plan is None:
                log.info("SmartThings 모드 대응표에서 이름을 찾지 못해 보내지 않는다: node=%s command=%s",
                         command.node_id, command.command_id)
                return self._result(command, CommandOutcome.NOT_SENT)

        before: Any = _NO_VALUE
        if plan.confirm.method == CHANGED:
            if plan.refresh:
                self._refresh()
            before = self._read(plan.confirm)
            if before is None:
                # 비교할 값이 없으면 성공을 확인할 수 없다 (spec 결정 5)
                log.info("SmartThings 전송 전 값을 읽지 못해 보내지 않는다: node=%s command=%s",
                         command.node_id, command.command_id)
                return self._result(command, CommandOutcome.NOT_SENT)

        if self._monotonic() >= deadline:
            return self._result(command, CommandOutcome.NOT_SENT)
        try:
            self._transport.post_command(self._device_id, plan.capability, plan.command, list(plan.arguments))
        except SmartThingsMaybeSent:
            log.warning("SmartThings 명령 응답이 불명이다. 확인 조회를 계속한다: node=%s command=%s",
                        command.node_id, command.command_id)
        except SmartThingsTransportError as error:
            log.warning("SmartThings 명령을 보내지 못했다: node=%s command=%s (%s)",
                        command.node_id, command.command_id, error)
            return self._result(command, CommandOutcome.NOT_SENT)

        if plan.refresh:
            # 전송 직후의 refresh 는 기기가 적용하기 전이라 옛 값을 읽는다. 한 간격 기다려 호출 한도를 아낀다 (spec 결정 25)
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._sleep(min(self._poll_interval_sec, remaining))
        while True:
            if plan.refresh:
                self._refresh()
            actual = self._read(plan.confirm)
            if self._confirmed(plan.confirm, actual, before):
                confirmation = {"source": "smartthings_state", "capability": plan.confirm.capability,
                                "attribute": plan.confirm.attribute, "value": actual}
                if before is not _NO_VALUE:
                    confirmation["before"] = before
                return self._result(command, CommandOutcome.SUCCEEDED, confirmation)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                # 이상값 진단용으로 마지막 관측 값과 기대값을 남긴다. deviceId 는 남기지 않는다 (NFR-08).
                log.warning("SmartThings 상태를 확인하지 못했다: node=%s command=%s %s.%s 관측=%r 기대=%s",
                            command.node_id, command.command_id, plan.confirm.capability,
                            plan.confirm.attribute, actual,
                            "변화" if plan.confirm.method == CHANGED else repr(plan.confirm.expected))
                return self._result(command, CommandOutcome.UNKNOWN)
            self._sleep(min(self._poll_interval_sec, remaining))

    def _remember(self, command: NodeCommand, result: CommandResult) -> None:
        """확인한 값을 보고 상태에 넘긴다 (reported-state spec 결정 8). 결과 불명이면 보고 상태 캐시를 버린다.

        확인 속성이 보고 키의 속성과 같을 때만 기록한다. 보고 상태는 실행 성공의 근거가 아니다 (FR-07).
        """
        if self._confirmed_values is None:
            return
        if result.outcome is CommandOutcome.UNKNOWN:
            self._confirmed_values.invalidate()
            return
        action = ACTIONS.get(command.action)
        target = None if action is None else action.reported
        confirmation = result.confirmation
        if (result.outcome is CommandOutcome.SUCCEEDED and target is not None and confirmation is not None
                and confirmation.get("capability") == target.capability
                and confirmation.get("attribute") == target.attribute):
            self._confirmed_values.record(target.key, confirmation.get("value"), result.reported_at)

    def _with_mode_name(self, plan: Plan) -> Plan | None:
        """대응표에서 모드 ID 의 이름을 골라 인자와 기대값에 넣는다. 대응표가 없으면 refresh 뒤 한 번 더 읽는다."""
        mode = plan.mode_name
        table = self._get(plan.capability, mode.attribute)
        if table is None:
            self._refresh()
            table = self._get(plan.capability, mode.attribute)
        name = _mode_name(table, mode.mode_id)
        if name is None:
            return None
        confirm = replace(plan.confirm, expected=tuple(dict.fromkeys((*plan.confirm.expected, name))))
        return replace(plan, arguments=(name,), confirm=confirm)

    @staticmethod
    def _confirmed(confirm: Confirm, actual: Any, before: Any) -> bool:
        if actual is None:
            return False
        if confirm.method == CHANGED:
            return actual != before
        return any(_same_value(actual, value) for value in confirm.expected)

    def _refresh(self) -> None:
        # refresh 의 완료 응답은 성공 근거가 아니다. 실패해도 조회를 계속한다 (spec 결정 4).
        try:
            self._transport.post_command(self._device_id, REFRESH_CAPABILITY, REFRESH_COMMAND, [])
        except SmartThingsTransportError:
            pass

    def _read(self, confirm: Confirm) -> Any:
        return self._get(confirm.capability, confirm.attribute)

    def _get(self, capability: str, attribute: str) -> Any:
        try:
            return self._transport.get_attribute(self._device_id, capability, attribute)
        except SmartThingsTransportError:
            return None

    def _result(self, command: NodeCommand, outcome: CommandOutcome,
                confirmation: Mapping[str, Any] | None = None) -> CommandResult:
        return CommandResult(command.command_id, outcome, report=None, reported_at=self._clock(),
                             confirmation=confirmation)


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)  # bool 은 숫자로 보지 않는다


def _same_value(actual: Any, expected: Any) -> bool:
    """FR-07 "요청한 값과 같다". 숫자는 표현(15·15.0)이 달라도 값이 같으면 같다. 나머지는 타입까지 같아야 한다."""
    if _is_number(actual) and _is_number(expected):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _mode_name(table: Any, mode_id: str) -> str | None:
    """`[{"id", "name"}, ...]` 대응표에서 ID 의 이름. 형식이 다르거나 없으면 None (spec 가정)."""
    if not isinstance(table, (list, tuple)):
        return None
    for item in table:
        if isinstance(item, Mapping) and item.get("id") == mode_id:
            name = item.get("name")
            return name if isinstance(name, str) and name else None
    return None
