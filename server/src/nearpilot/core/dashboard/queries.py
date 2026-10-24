"""FR-11·FR-12A·FR-12B 대시보드 조회 서비스 (specs/core-dashboard-queries/spec.md 5절·6절).

`DashboardQueries` 9개와 `MonitorSnapshot.current_events` 를 구현한다. DB 는 `DashboardReadStore` 로 읽고, 값은
`steps`·`text`·`events` 의 함수로 만든다. 메서드 하나는 읽기 트랜잭션 하나(`open_reads()` 한 번)에서 읽어서 한 응답의
값이 같은 순간이다. 사람은 `Subject` 로만 담고, 자유 문자열은 코드와 값으로 만든 문구다 (FR-11, NFR-08).
쓰지 않고 이벤트를 발행하지 않는다. 시각 기준은 주입한 `clock()` 이다.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from nearpilot.core.dashboard.evaluation import evaluation_of
from nearpilot.core.dashboard.events import (
    approval_event,
    audit_node,
    audit_row,
    beacon_event,
    request_events,
    target_of,
    transition_event,
)
from nearpilot.core.dashboard.steps import proximity, request_steps, request_tool, responded_at
from nearpilot.core.dashboard.text import (
    NO_MODEL,
    NO_SIGNAL,
    OVERDUE,
    TIMELINE_RESERVED,
    fault_cause,
    node_name,
    outcome,
    session_note,
    timeline_command,
)
from nearpilot.core.lifecycle.shared_light import SharedHolder
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    ApprovalStatus,
    CommandOutcome,
    DeviceState,
    ExecutionStatus,
    ReasonCode,
    SessionState,
    Step,
    Verdict,
)
from nearpilot.shared.interfaces import DashboardReadStore
from nearpilot.shared.models import (
    DashboardApproval,
    DashboardAudit,
    DashboardCommand,
    DashboardNode,
    DashboardRequest,
    MonitorEvent,
    Subject,
)

_RUNNING = (ExecutionStatus.PENDING, ExecutionStatus.IN_PROGRESS)  # 아직 끝나지 않은 명령
_PERIOD = re.compile(r"([0-9]{2}):([0-9]{2})~([0-9]{2}):([0-9]{2})")


@dataclass(frozen=True, slots=True)
class ModelVersions:
    """장소의 근접 모델과 보정 버전 (서버가 시작할 때 읽은 모델)."""

    model_version: str
    calib_version: str


class DashboardQueryService:
    """`DashboardQueries` 와 `MonitorSnapshot` 구현.

    open_reads 는 읽기 트랜잭션 하나를 연 `DashboardReadStore` 를 주는 컨텍스트 관리자다. 메서드 하나는 한 번만 연다.
    models·mqtt_ready·holders 는 서버가 시작하기 전에도 불릴 수 있고, 읽기 트랜잭션 밖에서 부른다.
    started_at 은 서버가 시작한 시각이고 tz 는 이력 기간의 "오늘"을 자르는 시간대다 (없으면 서버 지역 시간대).
    """

    def __init__(
        self, *,
        open_reads: Callable[[], AbstractContextManager[DashboardReadStore]],
        settings: Callable[[], Settings],
        weak_rssi_dbm: float,
        models: Callable[[], Mapping[str, ModelVersions]],
        mqtt_ready: Callable[[], bool],
        holders: Callable[[], Sequence[SharedHolder]],
        reports_dir: Path,
        clock: Callable[[], datetime],
        started_at: Callable[[], datetime],
        tz: tzinfo | None = None,
    ) -> None:
        self._open_reads = open_reads
        self._settings = settings
        self._weak_rssi_dbm = weak_rssi_dbm
        self._models = models
        self._mqtt_ready = mqtt_ready
        self._holders = holders
        self._reports_dir = reports_dir
        self._clock = clock
        self._started_at = started_at
        self._tz = tz

    # ── DashboardQueries ─────────────────────────────────────────────

    def places(self) -> Mapping[str, Any]:
        """장소마다 그 장소의 기기를 node_id 순으로 담는다. 장소 없는 기기는 뺀다."""
        models = self._models()
        with self._open_reads() as reads:
            places, nodes = reads.places(), reads.nodes()
        names = _names(nodes)
        rows = []
        for place in places:
            version = models.get(place.place_id)
            rows.append({
                "placeId": place.place_id,
                "placeName": place.name,
                "modelVersion": NO_MODEL if version is None else version.model_version,
                "calibrationVersion": NO_MODEL if version is None else version.calib_version,
                "devices": [
                    {
                        "nodeId": node.node_id, "name": node_name(names, node.node_id),
                        "capability": node.capability, "occupancy": node.occupancy,
                    }
                    for node in nodes if node.place_id == place.place_id
                ],
            })
        return {"weakRssiDbm": self._weak_rssi_dbm, "places": rows}

    def uses(self) -> Sequence[Mapping[str, Any]]:
        """대여형은 끝나지 않은 점유 세션, 공용형은 공용 조명의 유지 대상자. 대여형이 먼저다."""
        now, holders = self._clock(), self._holders()
        with self._open_reads() as reads:
            nodes = {node.node_id: node for node in reads.nodes()}
            sessions = reads.open_sessions()
            shared = [holder for holder in holders if holder.node_id in nodes]
            roles = reads.roles({s.user_id for s in sessions} | {h.user_id for h in shared})
        rows = [
            _use_row(Subject(s.user_id, roles[s.user_id]), nodes[s.node_id], s.state,
                     s.reserved_at or s.received_at, overdue_minutes(s.rental_due_at, now))
            for s in sessions
        ]
        rows.extend(
            _use_row(Subject(h.user_id, roles[h.user_id]), nodes[h.node_id], SessionState.ACTIVE,
                     h.last_received_at, 0.0)
            for h in shared
        )
        return rows

    def approvals(self) -> Mapping[str, Any]:
        """대기, 유효, 끝남으로 나눈다. 안의 순서는 저장소 순서(만든 시각, 승인 ID)다."""
        now = self._clock()
        with self._open_reads() as reads:
            approvals = reads.approvals()
            roles = reads.roles({a.requester_user_id for a in approvals})
        sections: dict[str, list[dict[str, Any]]] = {"pending": [], "active": [], "ended": []}
        for a in approvals:
            section = approval_section(a, now)
            row: dict[str, Any] = {
                "subject": Subject(a.requester_user_id, roles[a.requester_user_id]), "nodeId": a.node_id,
                "mode": a.mode, "requestedAt": a.created_at,
            }
            if section == "pending" and a.pending_until is not None:
                row["remainingMinutes"] = _remaining_minutes(a.pending_until, now)
            if section == "active" and a.valid_until is not None:
                row["remainingMinutes"] = _remaining_minutes(a.valid_until, now)
            if a.valid_until is not None:
                row["validUntil"] = a.valid_until
            row["uses"] = a.used_count
            if section == "ended":
                row["endReason"] = _end_reason(a, now)
            sections[section].append(row)
        return sections

    def audit(self, limit: int) -> Sequence[Mapping[str, Any]]:
        """최근 감사 행 limit 개. 최근 것부터."""
        with self._open_reads() as reads:
            rows = reads.audit_recent(limit)
            requests = {
                r.internal_request_id: r
                for r in reads.requests_by_ids({a.internal_request_id for a in rows if a.internal_request_id})
            }
            commands = {c.command_id: c for c in reads.commands_by_ids({a.command_id for a in rows if a.command_id})}
            names = _names(reads.nodes())
        return [audit_row(a, audit_node(a, requests, commands), names) for a in rows]

    def history(self, period: str, verdict: Verdict | None, node_id: str | None) -> Sequence[Mapping[str, Any]]:
        """기간 안의 방문자 `request_use` 가운데 판정이 저장된 요청. 최근 것부터.

        period 는 `today` 또는 `HH:MM~HH:MM` 이고 그 밖이면 `ValueError` 다.
        """
        since, until = self._period(period)
        with self._open_reads() as reads:
            requests = [
                r for r in reads.requests(since, until)
                if r.kind == "visitor" and r.verdict is not None
                and (verdict is None or r.verdict == verdict) and (node_id is None or r.node_id == node_id)
            ]
            ids = [r.internal_request_id for r in requests]
            audits, commands = _by_request(reads.audit_of(ids)), _by_request(reads.commands_of(ids))
            roles = reads.roles({r.user_id for r in requests})
        rows = []
        for req in requests:
            rid = req.internal_request_id
            if request_tool(audits.get(rid, ())) != "request_use":
                continue
            text, code = outcome(req, commands.get(rid, ()))
            row: dict[str, Any] = {
                "subject": Subject(req.user_id, roles[req.user_id]), "requestId": rid, "at": req.received_at,
                "target": target_of(req.target_spec), "action": req.action, "verdict": req.verdict,
            }
            if req.node_id is not None:
                row["nodeId"] = req.node_id
            row["outcome"] = text
            if code is not None:
                row["code"] = code
            rows.append(row)
        return rows

    def request(self, request_id: str) -> Mapping[str, Any] | None:
        """방문자 `request_use` 한 건의 판정 흐름. 없거나 그 밖의 요청이면 `None`."""
        with self._open_reads() as reads:
            req = reads.request(request_id)
            if req is None or req.kind != "visitor":
                return None
            audits = reads.audit_of([request_id])
            if request_tool(audits) != "request_use":
                return None
            commands = reads.commands_of([request_id])
            nodes = reads.nodes()
            role = reads.roles([req.user_id])[req.user_id]
            approvals = reads.approvals() if req.node_id is not None else ()
        occupancy = next((node.occupancy for node in nodes if node.node_id == req.node_id), None)
        steps = request_steps(req, audits, occupancy, _names(nodes))
        result: dict[str, Any] = {
            "subject": Subject(req.user_id, role), "id": request_id, "tool": "request_use",
            "target": target_of(req.target_spec), "action": req.action, "at": req.received_at,
            "steps": [{"result": s.result, **({} if s.detail is None else {"detail": s.detail})} for s in steps],
        }
        if req.verdict is not None:
            result["verdict"] = req.verdict
        if req.reason_code is not None:
            result["reason"] = req.reason_code
        if req.node_id is not None:
            result["nodeId"] = req.node_id
        near = proximity(audits)
        if near is not None:
            result["posteriors"], result["threshold"] = near
        if commands:
            result["execution"] = max(commands, key=lambda c: c.seq).status
        earlier = [
            a for a in approvals
            if a.requester_user_id == req.user_id and a.node_id == req.node_id and a.action == req.action
            and a.created_at < req.received_at
        ]
        if earlier:
            access = max(earlier, key=lambda a: (a.created_at, a.approval_id))
            result["accessRequestedAt"] = access.created_at
            if access.pending_until is not None:
                result["accessExpiredAt"] = access.pending_until
        answered = _answered_at(audits)
        if req.verdict is not None and answered is not None:
            result["respondedAt"] = answered
        return result

    def trust(self) -> Mapping[str, Any]:
        """서버가 시작한 뒤의 판정 수, 판정 값별 수, 지연, 이중 점유와 최근 평가 리포트."""
        since = self._started_at()
        with self._open_reads() as reads:
            requests = [
                r for r in reads.requests(since, None) if r.kind == "visitor" and r.verdict is not None
            ]
            audits = _by_request(reads.audit_of([r.internal_request_id for r in requests]))
            sessions = reads.open_sessions()
        decided = [r for r in requests if request_tool(audits.get(r.internal_request_id, ())) == "request_use"]
        counts = dict.fromkeys(Verdict, 0)
        latencies = []
        for req in decided:
            counts[req.verdict] += 1
            answered = _answered_at(audits[req.internal_request_id])
            if answered is not None:
                latencies.append((answered - req.received_at).total_seconds())
        report: dict[str, Any] = {
            "live": {
                "since": since,
                "decisions": len(decided),
                "doubleOccupancy": sum(1 for count in Counter(s.node_id for s in sessions).values() if count > 1),
                "latencyMedianSec": statistics.median(latencies) if latencies else None,
                "latencyP95Sec": _nearest_rank(latencies, 95) if latencies else None,
                "counts": counts,
            },
        }
        evaluation = evaluation_of(self._reports_dir)  # 파일을 읽는 동안 읽기 트랜잭션을 잡지 않는다
        if evaluation is not None:
            report["evaluation"] = evaluation
        return report

    def rental(self, node_id: str) -> Mapping[str, Any] | None:
        """그 기기의 끝나지 않은 대여 세션 하나와 시각 순 타임라인. 없으면 `None`."""
        now = self._clock()
        with self._open_reads() as reads:
            session = next((s for s in reads.open_sessions() if s.node_id == node_id), None)
            if session is None:
                return None
            commands = reads.commands_of_use(session.use_id)
            role = reads.roles([session.user_id])[session.user_id]
        started = session.reserved_at or session.received_at
        timeline = [(started, TIMELINE_RESERVED)]
        for cmd in commands:
            text = timeline_command(cmd)
            if text is not None:
                timeline.append((cmd.occurred_at or cmd.send_started_at or started, text))
        due = session.rental_due_at
        if due is not None and now > due:
            timeline.append((due, OVERDUE))
        timeline.sort(key=lambda item: item[0])
        items = [{"at": at, "text": text} for at, text in timeline]
        items[-1]["now"] = True
        return {
            "subject": Subject(session.user_id, role), "nodeId": node_id, "startedAt": started,
            "overdueMinutes": overdue_minutes(due, now), "timeline": items,
        }

    def health(self) -> Mapping[str, Any]:
        """기기 연결, 활성 비콘의 최근 수신, 서버 상태. DB 점검이 실패해도 나머지 키는 만든다."""
        now, settings = self._clock(), self._settings()
        models, mqtt = self._models(), self._mqtt_ready()
        with self._open_reads() as reads:
            try:
                reads.ping()
                database = True
            except sqlite3.Error:
                database = False
            places, nodes, beacons = reads.places(), reads.nodes(), reads.active_beacons()
            roles = reads.roles({b.user_id for b in beacons})
        ttl = timedelta(seconds=settings.online_ttl_sec)
        beacon_rows = []
        for beacon in beacons:
            # 조회 중에 새로 저장된 관측이 `now` 보다 늦을 수 있다
            last = None if beacon.last_seen_at is None else max(0.0, (now - beacon.last_seen_at).total_seconds())
            row: dict[str, Any] = {"subject": Subject(beacon.user_id, roles[beacon.user_id]), "lastSeenSec": last}
            if last is None or last > settings.beacon_recent_sec:
                row["note"] = NO_SIGNAL
            beacon_rows.append(row)
        versions = [models.get(p.place_id) for p in places]
        model = " / ".join(
            NO_MODEL if v is None else f"{v.model_version} · {v.calib_version}" for v in versions
        )
        return {
            "devices": [
                {"nodeId": n.node_id, "connected": _connected(n, now, ttl), "trust": n.trust_state} for n in nodes
            ],
            "beacons": beacon_rows,
            "server": {"database": database, "mqtt": mqtt, "model": model or NO_MODEL},
        }

    # ── MonitorSnapshot ──────────────────────────────────────────────

    def current_events(self) -> Sequence[MonitorEvent]:
        """지금 상태를 이벤트로 돌려준다. 기기, 비콘, 승인 대기, 진행 중인 요청 순이다."""
        now, settings, started = self._clock(), self._settings(), self._started_at()
        with self._open_reads() as reads:
            nodes = reads.nodes()
            sessions = reads.open_sessions()
            beacons = reads.active_beacons()
            waiting = [a for a in reads.approvals() if approval_section(a, now) == "pending"]
            evidence = reads.fault_evidence([n.node_id for n in nodes if n.device_state is DeviceState.FAULT])
            candidates = sorted(reads.requests(started, None), key=lambda r: (r.received_at, r.seq))
            commands = _by_request(reads.commands_of([r.internal_request_id for r in candidates]))
            running = [r for r in candidates if in_progress(r, commands.get(r.internal_request_id, ()))]
            audits = _by_request(reads.audit_of([r.internal_request_id for r in running]))
            roles = reads.roles(
                {b.user_id for b in beacons} | {a.requester_user_id for a in waiting}
                | {r.user_id for r in running if r.user_id is not None}
            )
        names = _names(nodes)
        occupancy = {n.node_id: n.occupancy for n in nodes}
        overdue = {
            s.node_id for s in sessions if s.state is SessionState.ACTIVE and overdue_minutes(s.rental_due_at, now) > 0
        }

        events = [
            transition_event(
                n, now, overdue=n.node_id in overdue,
                fault=fault_of(evidence.get(n.node_id)) if n.device_state is DeviceState.FAULT else None,
            )
            for n in nodes
        ]
        for beacon in beacons:
            seen = (
                beacon.last_seen_at is not None
                and (now - beacon.last_seen_at).total_seconds() <= settings.beacon_recent_sec
            )
            subject = Subject(beacon.user_id, roles[beacon.user_id])
            events.append(beacon_event(subject, seen, beacon.last_seen_at or now))
        for a in waiting:
            events.append(approval_event(a, "requested", Subject(a.requester_user_id, roles[a.requester_user_id]),
                                         a.created_at))
        for req in running:
            rid = req.internal_request_id
            subject = None if req.user_id is None else Subject(req.user_id, roles[req.user_id])
            events.extend(request_events(
                req, audits.get(rid, ()), commands.get(rid, ()), subject, occupancy.get(req.node_id), names,
            ))
        return events

    # ── 내부 ─────────────────────────────────────────────────────────

    def _period(self, period: str) -> tuple[datetime, datetime | None]:
        """이력 기간 → [시작, 끝). `today` 는 오늘 0시부터이고 끝이 없다. 시각은 서버 시간대의 오늘이다."""
        today = self._clock().astimezone(self._tz).date()
        if period == "today":
            return self._at(today, time(0, 0)), None
        match = _PERIOD.fullmatch(period)
        if match is None:
            raise ValueError(f"period 는 today 또는 HH:MM~HH:MM 이어야 한다: {period!r}")
        first_hour, first_minute, last_hour, last_minute = map(int, match.groups())
        start, end = time(first_hour, first_minute), time(last_hour, last_minute)  # 범위 밖이면 ValueError
        if end <= start:
            raise ValueError(f"period 의 끝이 시작보다 늦어야 한다: {period!r}")
        return self._at(today, start), self._at(today, end)

    def _at(self, day: date, at: time) -> datetime:
        naive = datetime.combine(day, at)
        return naive.replace(tzinfo=self._tz) if self._tz is not None else naive.astimezone()


def fault_of(row: DashboardAudit | None) -> ReasonCode:
    """기기의 마지막 고장 근거 감사 행 → `fault.cause` (spec 6절). 근거 행이 없으면 `TARGET_UNAVAILABLE`.

    `rejected` 결과는 기기를 격리하지 않으므로 근거가 아니다 (`DashboardReadStore.fault_evidence` 가 고르지 않는다).
    """
    if row is None:
        return ReasonCode.TARGET_UNAVAILABLE
    event, result = row.payload.get("event"), row.payload.get("outcome")
    if event == "record_result":
        if result == CommandOutcome.UNKNOWN.value:
            return ReasonCode.EXEC_UNKNOWN
        if result == CommandOutcome.FAILED.value:
            return ReasonCode.EXEC_FAILED
    if event == "isolate_device":
        evidence = row.payload.get("evidence")
        return fault_cause(evidence if isinstance(evidence, Mapping) else {})
    return ReasonCode.TARGET_UNAVAILABLE


def _names(nodes: Iterable[DashboardNode]) -> dict[str, str]:
    """이름이 있는 기기만. 없는 기기는 `node_name` 이 ID 를 쓴다."""
    return {node.node_id: node.name for node in nodes if node.name}


def _by_request(rows: Iterable[Any]) -> dict[str, list[Any]]:
    """요청 ID 로 묶는다. 요청에 속하지 않은 행은 뺀다. 안의 순서는 받은 순서다."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if row.internal_request_id is not None:
            grouped[row.internal_request_id].append(row)
    return dict(grouped)


def overdue_minutes(due: datetime | None, now: datetime) -> float:
    """대여 만료 시각부터 흐른 분. `ReleaseService` 와 같이 0 이상이다."""
    return 0.0 if due is None else max(0.0, (now - due).total_seconds() / 60)


def _remaining_minutes(deadline: datetime, now: datetime) -> int:
    """기한까지 남은 분. 정수로 내리고 0 이상이다."""
    return max(0, int((deadline - now).total_seconds() // 60))


def _answered_at(audits: Sequence[DashboardAudit]) -> datetime | None:
    """판정을 돌려준 시각. 반납 완료 행(`CLOSE`)은 대여 요청의 ID 를 달고 반납할 때 붙으므로 뺀다 (NFR-01)."""
    return responded_at([a for a in audits if a.step is not Step.CLOSE])


def _nearest_rank(values: Sequence[float], percent: int) -> float:
    """가장 가까운 순위 방식의 백분위수. values 는 비어 있지 않다."""
    ordered = sorted(values)
    return ordered[(len(ordered) * percent + 99) // 100 - 1]


def _connected(node: DashboardNode, now: datetime, ttl: timedelta) -> bool:
    """판정 ②와 같은 규칙: 세션이 있고 마지막 수신이 `online_ttl_sec` 안이다."""
    return bool(node.session_id) and node.last_seen_at is not None and now - node.last_seen_at <= ttl


def in_progress(req: DashboardRequest, commands: Sequence[DashboardCommand]) -> bool:
    """진행 중인 요청: 방문자 요청은 판정이 아직 없거나, 어느 요청이든 끝나지 않은 명령이 있다.

    서버 내부 요청은 판정 값을 저장하지 않으므로(`verdict` 가 늘 비어 있다) 판정이 없다는 이유로는 진행 중이라 보지 않는다.
    """
    return (req.kind == "visitor" and req.verdict is None) or any(c.status in _RUNNING for c in commands)


def approval_section(a: DashboardApproval, now: datetime) -> str:
    """`pending`: 대기 기한 전. `active`: 유효 기한 전이고 남은 횟수가 있음. 그 밖은 `ended`."""
    if a.status is ApprovalStatus.PENDING and a.pending_until is not None and now < a.pending_until:
        return "pending"
    if (
        a.status is ApprovalStatus.APPROVED and a.valid_until is not None and now < a.valid_until
        and (a.remaining_uses is None or a.remaining_uses > 0)
    ):
        return "active"
    return "ended"


def _end_reason(a: DashboardApproval, now: datetime) -> ReasonCode:
    """끝난 승인의 사유. `RepositoryPolicyChecker` 가 승인 필요 사유를 고르는 규칙과 같다."""
    if a.status is ApprovalStatus.DENIED:
        return ReasonCode.APPROVAL_REJECTED
    if a.status is ApprovalStatus.PENDING:
        return ReasonCode.APPROVAL_REQUEST_EXPIRED
    if a.valid_until is not None and now >= a.valid_until:
        return ReasonCode.APPROVAL_EXPIRED
    return ReasonCode.APPROVAL_EXHAUSTED


def _use_row(
    subject: Subject, node: DashboardNode, session: SessionState, started_at: datetime, overdue: float,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "subject": subject, "nodeId": node.node_id, "occupancy": node.occupancy, "session": session,
    }
    note = session_note(session, node.device_state, overdue)
    if note is not None:
        row["sessionNote"] = note
    row["startedAt"] = started_at
    if overdue > 0:
        row["overdueMinutes"] = overdue
    return row
