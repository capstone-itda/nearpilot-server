"""`DashboardReadStore` 구현 (specs/core-dashboard-queries/spec.md 2절·6절, FR-12A, FR-12B).

대시보드 조회 서비스와 따라 읽기가 쓰는 읽기 전용 접점이다. 쓰지 않고 트랜잭션도 열지 않는다 —
읽기 트랜잭션은 부르는 쪽이 연다. 저장된 값을 그대로 돌려주고 현재 시각을 읽지 않는다.
분류와 계산(점유, 승인 기한, 단계 결과)은 CORE 조회 서비스가 한다. 연결은 `db/schema/connect()`가
만든 것을 받는다 (`row_factory`가 `sqlite3.Row`다).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from nearpilot.db.repositories._common import from_json, iso, parse_iso
from nearpilot.shared.enums import (
    Action,
    ApprovalMode,
    ApprovalStatus,
    ApprovalUsageState,
    Capability,
    CommandOutcome,
    DeviceState,
    ExecutionStatus,
    Occupancy,
    ReasonCode,
    Role,
    SessionState,
    Step,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import (
    DashboardApproval,
    DashboardAudit,
    DashboardBeacon,
    DashboardCommand,
    DashboardNode,
    DashboardPlace,
    DashboardRequest,
    DashboardRssi,
    DashboardSession,
)

_REQUEST_COLUMNS = (
    "rowid AS seq, internal_request_id, user_id, request_kind, target_spec, action, verdict, "
    "reason_code, node_id, decision_context, received_at"
)
_COMMAND_COLUMNS = (
    "rowid AS seq, command_id, internal_request_id, node_id, action, status, use_id, "
    "send_started_at, outcome, occurred_at, evidence"
)
_AUDIT_COLUMNS = (
    "rowid AS seq, log_id, internal_request_id, command_id, step, payload_json, created_at"
)
# 따라 읽기가 커서로 잡는 표
_CURSOR_TABLES = ("request", "audit_log", "rssi_observation")
# 기기가 고장이 된 근거인 실행 결과 (`record_result` 감사 행의 outcome). `rejected` 는 기기를 격리하지 않는다
_FAULT_OUTCOMES = (CommandOutcome.UNKNOWN.value, CommandOutcome.FAILED.value)


def _time(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def _marks(values: Sequence[str]) -> str:
    return ", ".join("?" * len(values))


def _request_from_row(row: sqlite3.Row) -> DashboardRequest:
    return DashboardRequest(
        seq=row["seq"],
        internal_request_id=row["internal_request_id"],
        user_id=row["user_id"],
        kind=row["request_kind"],
        target_spec=row["target_spec"],
        action=Action(row["action"]),
        verdict=Verdict(row["verdict"]) if row["verdict"] else None,
        reason_code=ReasonCode(row["reason_code"]) if row["reason_code"] else None,
        node_id=row["node_id"],
        decision_context=from_json(row["decision_context"]) if row["decision_context"] else None,
        received_at=parse_iso(row["received_at"]),
    )


def _command_from_row(row: sqlite3.Row) -> DashboardCommand:
    return DashboardCommand(
        seq=row["seq"],
        command_id=row["command_id"],
        internal_request_id=row["internal_request_id"],
        node_id=row["node_id"],
        action=Action(row["action"]),
        status=ExecutionStatus(row["status"]),
        use_id=row["use_id"],
        send_started_at=_time(row["send_started_at"]),
        outcome=row["outcome"],
        occurred_at=_time(row["occurred_at"]),
        evidence=from_json(row["evidence"]),
    )


def _audit_from_row(row: sqlite3.Row) -> DashboardAudit:
    """모르는 `step` 값은 `Step(...)`이 `ValueError`로 알린다. 행을 조용히 버리지 않는다."""
    return DashboardAudit(
        seq=row["seq"],
        log_id=row["log_id"],
        internal_request_id=row["internal_request_id"],
        command_id=row["command_id"],
        step=Step(row["step"]),
        payload=from_json(row["payload_json"]),
        created_at=parse_iso(row["created_at"]),
    )


class SqliteDashboardReads:
    """`DashboardReadStore` Protocol 구현."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 장소·기기·계정 ─────────────────────────────────────────────

    def places(self) -> Sequence[DashboardPlace]:
        rows = self._conn.execute("SELECT place_id, name FROM place ORDER BY place_id").fetchall()
        return [DashboardPlace(place_id=row["place_id"], name=row["name"]) for row in rows]

    def nodes(self) -> Sequence[DashboardNode]:
        rows = self._conn.execute(
            "SELECT node_id, name, place_id, capability, occupancy, trust_state, device_state, "
            "last_seen_at, session_id FROM node ORDER BY node_id"
        ).fetchall()
        return [
            DashboardNode(
                node_id=row["node_id"],
                name=row["name"],
                place_id=row["place_id"],
                capability=Capability(row["capability"]),
                occupancy=Occupancy(row["occupancy"]),
                trust_state=TrustState(row["trust_state"]),
                device_state=DeviceState(row["device_state"]),
                last_seen_at=_time(row["last_seen_at"]),
                session_id=row["session_id"],
            )
            for row in rows
        ]

    def roles(self, user_ids: Iterable[str]) -> Mapping[str, Role]:
        rows = self._rows_in("SELECT user_id, role FROM user", "user_id", user_ids)
        return {row["user_id"]: Role(row["role"]) for row in rows}

    # ── 점유·승인 ───────────────────────────────────────────────────

    def open_sessions(self) -> Sequence[DashboardSession]:
        rows = self._conn.execute(
            "SELECT s.use_id, s.internal_request_id, s.user_id, s.node_id, s.state, s.reserved_at, "
            "s.rental_due_at, r.received_at "
            "FROM use_session s JOIN request r ON r.internal_request_id = s.internal_request_id "
            "WHERE s.state IN (?, ?, ?) ORDER BY s.node_id, s.reserved_at, s.use_id",
            (SessionState.RESERVED.value, SessionState.ACTIVE.value, SessionState.CLOSING.value),
        ).fetchall()
        return [
            DashboardSession(
                use_id=row["use_id"],
                internal_request_id=row["internal_request_id"],
                user_id=row["user_id"],
                node_id=row["node_id"],
                state=SessionState(row["state"]),
                reserved_at=_time(row["reserved_at"]),
                rental_due_at=_time(row["rental_due_at"]),
                received_at=parse_iso(row["received_at"]),
            )
            for row in rows
        ]

    def approvals(self) -> Sequence[DashboardApproval]:
        # 남은 횟수는 `SqliteRepository._remaining_uses`와 같은 규칙이다: max_uses 에서 예약·보류·소비한
        # 사용분 수를 빼고 0 아래로 내려가지 않는다. max_uses 가 없으면 None 이다.
        rows = self._conn.execute(
            "SELECT a.approval_id, a.node_id, a.requester_user_id, a.internal_request_id, a.action, "
            "a.status, a.created_at, a.pending_until, a.valid_until, a.used_count, "
            "pr.approval_mode, pr.max_uses, "
            "(SELECT COUNT(*) FROM approval_usage u "
            "WHERE u.approval_id = a.approval_id AND u.state IN (?, ?, ?)) AS held_count "
            "FROM approval a "
            "JOIN policy_revision pr ON pr.policy_id = a.policy_id AND pr.version = a.policy_version "
            "ORDER BY a.created_at, a.approval_id",
            (
                ApprovalUsageState.RESERVED.value, ApprovalUsageState.HELD.value,
                ApprovalUsageState.CONSUMED.value,
            ),
        ).fetchall()
        return [
            DashboardApproval(
                approval_id=row["approval_id"],
                node_id=row["node_id"],
                requester_user_id=row["requester_user_id"],
                internal_request_id=row["internal_request_id"],
                action=Action(row["action"]),
                status=ApprovalStatus(row["status"]),
                created_at=parse_iso(row["created_at"]),
                pending_until=_time(row["pending_until"]),
                valid_until=_time(row["valid_until"]),
                mode=ApprovalMode(row["approval_mode"]),
                used_count=row["used_count"],
                remaining_uses=(
                    None if row["max_uses"] is None else max(row["max_uses"] - row["held_count"], 0)
                ),
            )
            for row in rows
        ]

    # ── 요청 ─────────────────────────────────────────────────────────

    def requests(self, since: datetime | None, until: datetime | None) -> Sequence[DashboardRequest]:
        conditions: list[str] = []
        params: list[str] = []
        if since is not None:
            conditions.append("received_at >= ?")
            params.append(iso(since))
        if until is not None:
            conditions.append("received_at < ?")
            params.append(iso(until))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._conn.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM request{where} ORDER BY received_at DESC, rowid DESC", params
        ).fetchall()
        return [_request_from_row(row) for row in rows]

    def request(self, internal_request_id: str) -> DashboardRequest | None:
        row = self._conn.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM request WHERE internal_request_id = ?", (internal_request_id,)
        ).fetchone()
        return None if row is None else _request_from_row(row)

    def requests_after(self, seq: int) -> Sequence[DashboardRequest]:
        rows = self._conn.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM request WHERE rowid > ? ORDER BY rowid", (seq,)
        ).fetchall()
        return [_request_from_row(row) for row in rows]

    def requests_by_ids(self, ids: Iterable[str]) -> Sequence[DashboardRequest]:
        rows = self._rows_in(f"SELECT {_REQUEST_COLUMNS} FROM request", "internal_request_id", ids)
        return [_request_from_row(row) for row in rows]

    # ── 명령 ─────────────────────────────────────────────────────────

    def commands_of(self, internal_request_ids: Iterable[str]) -> Sequence[DashboardCommand]:
        rows = self._rows_in(
            f"SELECT {_COMMAND_COLUMNS} FROM node_command", "internal_request_id", internal_request_ids
        )
        return [_command_from_row(row) for row in rows]

    def commands_of_use(self, use_id: str) -> Sequence[DashboardCommand]:
        rows = self._conn.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM node_command WHERE use_id = ? ORDER BY rowid", (use_id,)
        ).fetchall()
        return [_command_from_row(row) for row in rows]

    def commands_by_ids(self, command_ids: Iterable[str]) -> Sequence[DashboardCommand]:
        rows = self._rows_in(f"SELECT {_COMMAND_COLUMNS} FROM node_command", "command_id", command_ids)
        return [_command_from_row(row) for row in rows]

    # ── 감사 ─────────────────────────────────────────────────────────

    def audit_recent(self, limit: int) -> Sequence[DashboardAudit]:
        # SQLite 는 음수 LIMIT 를 "제한 없음"으로 읽는다. 0 이하는 빈 결과로 둔다.
        rows = self._conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_log ORDER BY rowid DESC LIMIT ?", (max(limit, 0),)
        ).fetchall()
        return [_audit_from_row(row) for row in rows]

    def audit_after(self, seq: int) -> Sequence[DashboardAudit]:
        rows = self._conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_log WHERE rowid > ? ORDER BY rowid", (seq,)
        ).fetchall()
        return [_audit_from_row(row) for row in rows]

    def audit_of(self, internal_request_ids: Iterable[str]) -> Sequence[DashboardAudit]:
        rows = self._rows_in(
            f"SELECT {_AUDIT_COLUMNS} FROM audit_log", "internal_request_id", internal_request_ids
        )
        return [_audit_from_row(row) for row in rows]

    def fault_evidence(self, node_ids: Iterable[str]) -> Mapping[str, DashboardAudit]:
        # 행의 기기는 `events.audit_node` 와 같은 순서로 찾는다: payload 의 node_id(문자열), 요청의 node_id, 명령의 node_id.
        # 근거 행은 보통 몇 건이라 기기별 마지막 행을 파이썬에서 고른다 (rowid 순으로 읽어 뒤 행이 앞 행을 덮는다).
        unique = list(dict.fromkeys(node_ids))
        if not unique:
            return {}
        rows = self._conn.execute(
            "SELECT seq, log_id, internal_request_id, command_id, step, payload_json, created_at, fault_node FROM ("
            "SELECT a.rowid AS seq, a.log_id, a.internal_request_id, a.command_id, a.step, a.payload_json, "
            "a.created_at, COALESCE("
            "CASE WHEN json_type(a.payload_json, '$.node_id') = 'text' "
            "THEN json_extract(a.payload_json, '$.node_id') END, r.node_id, c.node_id) AS fault_node "
            "FROM audit_log a "
            "LEFT JOIN request r ON r.internal_request_id = a.internal_request_id "
            "LEFT JOIN node_command c ON c.command_id = a.command_id "
            "WHERE json_extract(a.payload_json, '$.event') = ? "
            "OR (json_extract(a.payload_json, '$.event') = ? "
            f"AND json_extract(a.payload_json, '$.outcome') IN ({_marks(_FAULT_OUTCOMES)}))"
            f") WHERE fault_node IN ({_marks(unique)}) ORDER BY seq",
            ["isolate_device", "record_result", *_FAULT_OUTCOMES, *unique],
        ).fetchall()
        return {row["fault_node"]: _audit_from_row(row) for row in rows}

    # ── 비콘 수신 ───────────────────────────────────────────────────

    def rssi_after(self, seq: int) -> Sequence[DashboardRssi]:
        rows = self._conn.execute(
            "SELECT r.rowid AS seq, r.node_id, b.user_id, r.rssi, r.received_at "
            "FROM rssi_observation r JOIN beacon b ON b.beacon_id = r.beacon_id "
            "WHERE r.rowid > ? ORDER BY r.rowid",
            (seq,),
        ).fetchall()
        return [
            DashboardRssi(
                seq=row["seq"],
                node_id=row["node_id"],
                user_id=row["user_id"],
                rssi=row["rssi"],
                received_at=parse_iso(row["received_at"]),
            )
            for row in rows
        ]

    def active_beacons(self) -> Sequence[DashboardBeacon]:
        # 최댓값을 상관 부질의로 구해 (beacon_id, received_at) 인덱스를 탄다. 관측 표 전체를 훑지 않는다.
        rows = self._conn.execute(
            "SELECT b.user_id, b.beacon_id, "
            "(SELECT MAX(r.received_at) FROM rssi_observation r WHERE r.beacon_id = b.beacon_id) AS last_seen_at "
            "FROM beacon b WHERE b.is_active = 1 ORDER BY b.user_id"
        ).fetchall()
        return [
            DashboardBeacon(
                user_id=row["user_id"], beacon_id=row["beacon_id"], last_seen_at=_time(row["last_seen_at"])
            )
            for row in rows
        ]

    # ── 커서와 연결 확인 ─────────────────────────────────────────────

    def cursors(self) -> Mapping[str, int]:
        return {
            table: self._conn.execute(f"SELECT COALESCE(MAX(rowid), 0) AS last FROM {table}").fetchone()["last"]
            for table in _CURSOR_TABLES
        }

    def ping(self) -> None:
        self._conn.execute("SELECT 1").fetchone()

    # ── 공통 ─────────────────────────────────────────────────────────

    def _rows_in(self, select: str, column: str, values: Iterable[str]) -> list[sqlite3.Row]:
        """`column`이 `values` 중 하나인 행을 rowid 순으로 읽는다.

        값이 비면 질의하지 않는다. 같은 값이 여럿이어도 행은 한 번이다.
        """
        unique = list(dict.fromkeys(values))
        if not unique:
            return []
        return self._conn.execute(
            f"{select} WHERE {column} IN ({_marks(unique)}) ORDER BY rowid", unique
        ).fetchall()
