"""FR-12B 모니터링 따라 읽기 (specs/core-dashboard-queries/spec.md 6절).

커밋된 DB 행을 주기마다 읽어 모니터링 이벤트로 바꾸고 `MonitorFeed.publish` 로 넘긴다. 이벤트를 만드는 일은
`events`·`queries` 의 함수가 하고, 이 파일은 "무엇이 새로 생겼거나 바뀌었는가" 만 정한다. 한 주기는 읽기 트랜잭션
하나에서 읽고, 닫은 뒤에 요청, 기기, 대여 초과, 승인, RSSI, 비콘, 감사 순으로 모은 이벤트를 낸다. 읽거나 계산하는
중에 예외가 나면 그 주기에는 아무것도 내지 않고 커서와 마지막 값을 바꾸지 않아서 다음 주기에 같은 행을 다시 읽는다
(NFR-07). DB 에 쓰지 않고 스레드나 타이머를 만들지 않는다 (주기 작업은 조립이 등록한다). 시각 기준은 주입한
`clock()` 이다.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nearpilot.core.dashboard.events import (
    approval_event,
    audit_node,
    audit_row,
    beacon_event,
    request_events,
    transition_event,
)
from nearpilot.core.dashboard.queries import approval_section, fault_of, in_progress, overdue_minutes
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import ApprovalStatus, DeviceState, Role, SessionState
from nearpilot.shared.interfaces import DashboardReadStore, MonitorFeed
from nearpilot.shared.models import (
    DashboardApproval,
    DashboardAudit,
    DashboardBeacon,
    DashboardCommand,
    DashboardNode,
    DashboardRequest,
    DashboardRssi,
    DashboardSession,
    MonitorEvent,
    Subject,
)

log = logging.getLogger(__name__)

# 요청 안에서 이벤트가 차지하는 자리 (type, step, nodeId). 같은 자리의 이벤트는 한 번만 낸다
_Slot = tuple[str, Any, Any]


@dataclass(frozen=True, slots=True)
class _Approval:
    """승인 하나의 마지막 상태. expired 는 기한이 지난 대기를 이미 알렸거나 시작할 때 이미 지났다는 뜻이다."""

    status: ApprovalStatus
    expired: bool


@dataclass(frozen=True, slots=True)
class _Memory:
    """따라 읽기가 기억하는 값. 한 주기가 끝까지 성공했을 때만 새 값으로 바꾼다."""

    request_seq: int  # 커서: 테이블마다 마지막으로 본 rowid
    audit_seq: int
    rssi_seq: int
    states: Mapping[str, DeviceState]  # 기기마다 마지막 device_state
    overdue: frozenset[str]  # 대여 초과를 이미 알린 use_id (시작할 때 이미 지난 세션 포함)
    seen: Mapping[str, bool]  # 활성 비콘이 있는 사람마다 마지막 수신 여부
    approvals: Mapping[str, _Approval]
    tracked: Mapping[str, frozenset[_Slot]]  # 추적 중인 요청마다 이미 낸 이벤트의 자리


class MonitorTail:
    """커밋된 행을 따라 읽어 모니터링 이벤트를 발행한다. 주기 작업은 조립이 돌리고 `tick()` 이 한 주기다.

    open_reads 는 읽기 트랜잭션 하나를 연 `DashboardReadStore` 를 주는 컨텍스트 관리자이고 주기마다 한 번만 연다.
    명령 상태와 판정은 같은 행을 고쳐 쓰므로 (새 rowid 가 없다) 시작 뒤에 생긴 요청은 끝날 때까지 추적하며
    새 행이 없어도 주기마다 다시 읽는다.
    """

    def __init__(
        self, *,
        open_reads: Callable[[], AbstractContextManager[DashboardReadStore]],
        feed: MonitorFeed,
        settings: Callable[[], Settings],
        clock: Callable[[], datetime],
    ) -> None:
        self._open_reads = open_reads
        self._feed = feed
        self._settings = settings
        self._clock = clock
        self._memory: _Memory | None = None

    def start(self) -> None:
        """커서(`cursors()`)와 기기 상태·비콘 수신·승인 상태·대여 초과의 지금 값을 기억한다. 발행하지 않는다.

        실패하면 예외를 올린다 (조립이 시작 실패로 다룬다). 다시 부르면 처음부터 다시 기억한다.
        """
        now, settings = self._clock(), self._settings()
        with self._open_reads() as reads:
            cursors = reads.cursors()
            nodes, sessions = reads.nodes(), reads.open_sessions()
            beacons, approvals = reads.active_beacons(), reads.approvals()
        self._memory = _Memory(
            request_seq=cursors["request"], audit_seq=cursors["audit_log"], rssi_seq=cursors["rssi_observation"],
            states={n.node_id: n.device_state for n in nodes},
            overdue=frozenset(s.use_id for s in _overdue_sessions(sessions, now)),
            seen={b.user_id: _seen(b, now, settings) for b in beacons},
            approvals={a.approval_id: _Approval(a.status, _expired(a, now)) for a in approvals},
            tracked={},
        )

    def tick(self) -> None:
        """한 주기. 예외를 올리지 않는다 (로그만, NFR-07). `start()` 전이면 아무것도 하지 않는다.

        읽기와 계산이 실패하면 그 주기에는 아무것도 내지 않고 기억도 바꾸지 않는다. 성공하면 기억을 먼저 바꾸고
        이벤트를 낸다 (낸 이벤트를 다음 주기에 다시 내지 않는다).
        """
        before = self._memory
        if before is None:
            return
        try:
            now, settings = self._clock(), self._settings()
            with self._open_reads() as reads:
                after, events = _advance(before, reads, now, settings)
        except Exception as error:  # noqa: BLE001 - 이 주기만 건너뛴다. 값에 사용자 ID 가 있을 수 있어 예외 종류만 남긴다
            log.warning("따라 읽기 실패, 다음 주기에 다시 읽는다: %s", type(error).__name__)
            return
        self._memory = after
        for event in events:
            try:
                self._feed.publish(event)
            except Exception as error:  # noqa: BLE001 - 발행의 실패는 나머지 이벤트와 다음 주기를 막지 않는다
                log.error("모니터링 이벤트 발행 중 예외: type=%s 예외=%s", event.type, type(error).__name__)


def _advance(
    memory: _Memory, reads: DashboardReadStore, now: datetime, settings: Settings,
) -> tuple[_Memory, list[MonitorEvent]]:
    """읽기 트랜잭션 하나에서 읽고 새 기억과 이번 주기에 낼 이벤트를 만든다. 이벤트는 요청부터 감사까지 순서대로다."""
    arrived = reads.requests_after(memory.request_seq)
    ids = [*memory.tracked, *(r.internal_request_id for r in arrived)]
    requests = reads.requests_by_ids(ids)
    audits, commands = _by_request(reads.audit_of(ids)), _by_request(reads.commands_of(ids))
    nodes, sessions = reads.nodes(), reads.open_sessions()
    approvals, beacons = reads.approvals(), reads.active_beacons()
    observations, recorded = reads.rssi_after(memory.rssi_seq), reads.audit_after(memory.audit_seq)
    logged_requests = {
        r.internal_request_id: r
        for r in reads.requests_by_ids({a.internal_request_id for a in recorded if a.internal_request_id})
    }
    logged_commands = {
        c.command_id: c for c in reads.commands_by_ids({a.command_id for a in recorded if a.command_id})
    }

    # 대여 초과를 이번 주기에 처음 넘긴 기기. 상태도 바뀐 기기는 그 전이가 `overdue` 를 달고 나가므로 따로 내지 않는다
    due = _overdue_sessions(sessions, now)
    changed = [n for n in nodes if memory.states.get(n.node_id) != n.device_state]
    crossed = {s.node_id for s in due if s.use_id not in memory.overdue} - {n.node_id for n in changed}
    newly_overdue = [n for n in nodes if n.node_id in crossed]
    faulty = [n.node_id for n in (*changed, *newly_overdue) if n.device_state is DeviceState.FAULT]
    evidence = reads.fault_evidence(faulty)
    roles = reads.roles(
        {r.user_id for r in requests if r.user_id} | {a.requester_user_id for a in approvals}
        | {b.user_id for b in beacons} | {o.user_id for o in observations}
    )

    names = {n.node_id: n.name for n in nodes if n.name}
    occupancy = {n.node_id: n.occupancy for n in nodes}
    overdue_nodes = {s.node_id for s in due}
    events: list[MonitorEvent] = []
    tracked: dict[str, frozenset[_Slot]] = {}
    for req in requests:
        rid = req.internal_request_id
        try:
            made = request_events(
                req, audits.get(rid, ()), commands.get(rid, ()), _subject(req.user_id, roles),
                occupancy.get(req.node_id), names,
            )
        except ValueError as error:
            # 형식이 틀린 요청 행은 그 요청만 버리고 추적을 끝낸다 (spec 5절). 고쳐지지 않는 행이 주기를 막지 않게 한다
            log.warning("따라 읽기가 형식이 틀린 요청을 버렸다: %s", type(error).__name__)
            continue
        told = memory.tracked.get(rid, frozenset())
        fresh = [event for event in made if _slot(event) not in told]
        events.extend(fresh)
        if not _finished(req, commands.get(rid, ()), made):
            tracked[rid] = told | {_slot(event) for event in fresh}
    events.extend(_transition(n, now, n.node_id in overdue_nodes, evidence) for n in changed)
    events.extend(_transition(n, now, True, evidence) for n in newly_overdue)
    approval_events, approval_states = _approval_events(memory.approvals, approvals, roles, now)
    events.extend(approval_events)
    events.extend(_rssi_events(observations, roles))
    beacon_events, seen = _beacon_events(memory.seen, beacons, roles, now, settings)
    events.extend(beacon_events)
    for a in recorded:
        row = audit_row(a, audit_node(a, logged_requests, logged_commands), names)
        events.append(MonitorEvent("audit", a.created_at, {"row": row}))

    return _Memory(
        request_seq=max((r.seq for r in arrived), default=memory.request_seq),
        audit_seq=max((a.seq for a in recorded), default=memory.audit_seq),
        rssi_seq=max((o.seq for o in observations), default=memory.rssi_seq),
        states={n.node_id: n.device_state for n in nodes},
        overdue=(memory.overdue & {s.use_id for s in sessions}) | {s.use_id for s in due},
        seen=seen, approvals=approval_states, tracked=tracked,
    ), events


def _finished(req: DashboardRequest, commands: Sequence[DashboardCommand], made: Sequence[MonitorEvent]) -> bool:
    """추적을 끝내도 되는가. 도구 호출로 낸 요청은 `mcp_result` 를 냈을 때다.

    도구 호출로 내지 않는 요청 (`request_access`, 서버 내부 요청) 은 진행 중이 아닐 때다 (`queries.in_progress`).
    서버 내부 요청은 요청 행이 먼저 저장되고 명령 행이 뒤에 붙으므로 명령이 붙은 뒤여야 한다.
    """
    kinds = {event.type for event in made}
    if "mcp_call" in kinds:
        return "mcp_result" in kinds
    return not in_progress(req, commands) and (req.kind == "visitor" or bool(commands))


def _slot(event: MonitorEvent) -> _Slot:
    return event.type, event.fields.get("step"), event.fields.get("nodeId")


def _transition(
    node: DashboardNode, at: datetime, overdue: bool, evidence: Mapping[str, DashboardAudit],
) -> MonitorEvent:
    fault = fault_of(evidence.get(node.node_id)) if node.device_state is DeviceState.FAULT else None
    return transition_event(node, at, overdue=overdue, fault=fault)


def _approval_events(
    known: Mapping[str, _Approval], approvals: Sequence[DashboardApproval], roles: Mapping[str, Role], now: datetime,
) -> tuple[list[MonitorEvent], dict[str, _Approval]]:
    """새 행은 `requested`, 상태가 `approved`·`denied` 로 바뀌면 그 값, 대기가 기한을 넘기면 `expired` 를 한 번씩 낸다.

    새 행은 대기로 시작한 것으로 보므로 처음 볼 때 이미 결정된 행은 `requested` 뒤에 결정도 낸다.
    """
    events: list[MonitorEvent] = []
    states: dict[str, _Approval] = {}
    for a in approvals:
        before = known.get(a.approval_id)
        was = ApprovalStatus.PENDING if before is None else before.status
        told = before is not None and before.expired
        expired = _expired(a, now)
        subject = _subject(a.requester_user_id, roles)
        if subject is not None:
            if before is None:
                events.append(approval_event(a, "requested", subject, a.created_at))
            if a.status is not ApprovalStatus.PENDING and a.status != was:
                events.append(approval_event(a, a.status.value, subject, now))
            if expired and not told:
                events.append(approval_event(a, "expired", subject, now))
        states[a.approval_id] = _Approval(a.status, expired or told)
    return events, states


def _rssi_events(observations: Sequence[DashboardRssi], roles: Mapping[str, Role]) -> list[MonitorEvent]:
    """사람마다 이 주기에 받은 관측을 기기별 마지막 dBm 으로 묶어 이벤트 하나씩 낸다. 마지막 관측이 이른 사람부터다."""
    levels: dict[str, dict[str, float]] = defaultdict(dict)
    last: dict[str, DashboardRssi] = {}
    for obs in observations:
        levels[obs.user_id][obs.node_id] = obs.rssi
        last[obs.user_id] = obs
    events: list[MonitorEvent] = []
    for user_id, obs in sorted(last.items(), key=lambda item: item[1].seq):
        subject = _subject(user_id, roles)
        if subject is not None:
            events.append(MonitorEvent("rssi", obs.received_at, {"rssi": levels[user_id]}, subject))
    return events


def _beacon_events(
    known: Mapping[str, bool], beacons: Sequence[DashboardBeacon], roles: Mapping[str, Role], now: datetime,
    settings: Settings,
) -> tuple[list[MonitorEvent], dict[str, bool]]:
    """수신 여부가 기억한 값과 다른 사람마다 `beacon` 을 낸다. 새로 생긴 활성 비콘의 사람도 낸다."""
    events: list[MonitorEvent] = []
    states: dict[str, bool] = {}
    for beacon in beacons:
        seen = _seen(beacon, now, settings)
        states[beacon.user_id] = seen
        subject = _subject(beacon.user_id, roles)
        if subject is not None and known.get(beacon.user_id) != seen:
            events.append(beacon_event(subject, seen, beacon.last_seen_at or now))
    return events, states


def _seen(beacon: DashboardBeacon, now: datetime, settings: Settings) -> bool:
    """마지막 관측이 `beacon_recent_sec` 안이다 (`DashboardQueryService.current_events` 와 같은 규칙)."""
    last = beacon.last_seen_at
    return last is not None and (now - last).total_seconds() <= settings.beacon_recent_sec


def _expired(a: DashboardApproval, now: datetime) -> bool:
    """대기 기한이 지난 `pending` 승인."""
    return a.status is ApprovalStatus.PENDING and approval_section(a, now) != "pending"


def _overdue_sessions(sessions: Iterable[DashboardSession], now: datetime) -> list[DashboardSession]:
    """대여 시간이 지난 ACTIVE 세션 (`DashboardQueryService.current_events` 의 `overdue` 와 같은 규칙)."""
    return [s for s in sessions if s.state is SessionState.ACTIVE and overdue_minutes(s.rental_due_at, now) > 0]


def _subject(user_id: str | None, roles: Mapping[str, Role]) -> Subject | None:
    role = None if user_id is None else roles.get(user_id)
    return None if role is None else Subject(user_id, role)


def _by_request(rows: Iterable[Any]) -> dict[str, list[Any]]:
    """요청 ID 로 묶는다. 요청에 속하지 않은 행은 뺀다. 안의 순서는 받은 순서다."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if row.internal_request_id is not None:
            grouped[row.internal_request_id].append(row)
    return dict(grouped)
