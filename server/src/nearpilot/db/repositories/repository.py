"""`Repository`·`AccountLookup` 구현 (specs/db-repository/spec.md).

쓰기는 이 클래스 하나로만 한다 (`AGENTS.md` 의존 규칙 5). 연결은 스레드 하나당 하나를
`db/schema/connect()`로 만들어 넘긴다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from nearpilot.db.repositories._common import from_json, iso, parse_iso
from nearpilot.shared.enums import (
    AccessMode,
    Action,
    ApprovalMode,
    ApprovalStatus,
    Capability,
    DeviceState,
    EndReason,
    Occupancy,
    Role,
    SessionState,
    TrustState,
    Visibility,
)
from nearpilot.shared.models import (
    Account,
    Approval,
    BeaconKey,
    NodeInfo,
    PolicyRevision,
    RssiFrame,
    UseSession,
)


class SqliteRepository:
    """`Repository` Protocol과 `AccountLookup` Protocol을 함께 구현한다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 계정 조회 (NFR-08) ──────────────────────────────────────────

    def account_by_token_hash(self, token_hash: str) -> Account | None:
        row = self._conn.execute(
            "SELECT user_id, role FROM user WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        return Account(user_id=row["user_id"], role=Role(row["role"]))

    # ── ① 비콘 (FR-05, FR-15) ────────────────────────────────────────

    def beacons_of(self, user_id: str) -> Sequence[BeaconKey]:
        rows = self._conn.execute(
            "SELECT uuid, major, minor FROM beacon WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchall()
        return [BeaconKey(row["uuid"], row["major"], row["minor"]) for row in rows]

    def rssi_since(self, beacons: Sequence[BeaconKey], since: datetime) -> Sequence[RssiFrame]:
        keys = list(beacons)
        if not keys:
            return []
        clause = " OR ".join(["(b.uuid = ? AND b.major = ? AND b.minor = ?)"] * len(keys))
        params: list[object] = []
        for key in keys:
            params.extend([key.uuid, key.major, key.minor])
        rows = self._conn.execute(
            "SELECT r.node_id, r.rssi, r.frame_ts, r.session_id, r.seq, r.sent_at, r.received_at, "
            "b.uuid, b.major, b.minor "
            "FROM rssi_observation r JOIN beacon b ON b.beacon_id = r.beacon_id "
            f"WHERE ({clause}) AND r.received_at >= ? ORDER BY r.received_at",
            (*params, iso(since)),
        ).fetchall()
        return [
            RssiFrame(
                node_id=row["node_id"],
                beacon=BeaconKey(row["uuid"], row["major"], row["minor"]),
                rssi=row["rssi"],
                frame_ts=parse_iso(row["frame_ts"]),
                session_id=row["session_id"],
                seq=row["seq"],
                sent_at=parse_iso(row["sent_at"]),
                received_at=parse_iso(row["received_at"]),
            )
            for row in rows
        ]

    # ── ② 후보 (FR-13) ──────────────────────────────────────────────

    def nodes(self, capability: Capability | None = None) -> Sequence[NodeInfo]:
        if capability is None:
            rows = self._conn.execute("SELECT * FROM node").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM node WHERE capability = ?", (capability.value,)
            ).fetchall()
        return [self._node_from_row(row) for row in rows]

    def _node_from_row(self, row: sqlite3.Row) -> NodeInfo:
        return NodeInfo(
            node_id=row["node_id"],
            place_id=row["place_id"],
            capability=Capability(row["capability"]),
            occupancy=Occupancy(row["occupancy"]),
            trust_state=TrustState(row["trust_state"]),
            device_state=DeviceState(row["device_state"]),
            exclusive_group=row["exclusive_group"],
            last_seen_at=parse_iso(row["last_seen_at"]) if row["last_seen_at"] else None,
            release_policy=from_json(row["release_policy"]),
            anchor_params=from_json(row["anchor_params"]) if row["anchor_params"] else {},
            session_id=row["session_id"],
            session_first_seen_at=(
                parse_iso(row["session_first_seen_at"]) if row["session_first_seen_at"] else None
            ),
        )

    # ── ④ 정책 (FR-14) ──────────────────────────────────────────────

    def current_policy(self, node_id: str) -> PolicyRevision:
        row = self._conn.execute(
            "SELECT ap.policy_id, ap.current_version, pr.visibility, pr.mode, "
            "pr.approver_user_id, pr.approval_mode, pr.approval_ttl_sec, pr.max_uses "
            "FROM access_policy ap "
            "JOIN policy_revision pr ON pr.policy_id = ap.policy_id AND pr.version = ap.current_version "
            "WHERE ap.node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"기기 {node_id} 에 정책이 없다")
        return PolicyRevision(
            policy_id=row["policy_id"],
            version=row["current_version"],
            node_id=node_id,
            visibility=Visibility(row["visibility"]),
            mode=AccessMode(row["mode"]),
            approver_user_id=row["approver_user_id"],
            approval_mode=ApprovalMode(row["approval_mode"]),
            approval_ttl_sec=row["approval_ttl_sec"],
            max_uses=row["max_uses"],
        )

    def valid_approval(
        self, user_id: str, node_id: str, action: Action, policy_version: int, now: datetime
    ) -> Approval | None:
        row = self._conn.execute(
            "SELECT a.approval_id, a.node_id, a.requester_user_id, a.action, a.policy_id, "
            "a.policy_version, a.status, a.valid_until, pr.max_uses "
            "FROM approval a "
            "JOIN policy_revision pr ON pr.policy_id = a.policy_id AND pr.version = a.policy_version "
            "WHERE a.requester_user_id = ? AND a.node_id = ? AND a.action = ? AND a.policy_version = ? "
            "AND a.status = 'approved' AND a.valid_until IS NOT NULL AND a.valid_until > ? "
            "ORDER BY a.valid_until DESC, a.approval_id ASC LIMIT 1",
            (user_id, node_id, action.value, policy_version, iso(now)),
        ).fetchone()
        if row is None:
            return None
        return Approval(
            approval_id=row["approval_id"],
            node_id=row["node_id"],
            requester_user_id=row["requester_user_id"],
            action=Action(row["action"]),
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            status=ApprovalStatus(row["status"]),
            valid_until=parse_iso(row["valid_until"]),
            remaining_uses=self._remaining_uses(row["approval_id"], row["max_uses"]),
        )

    def _remaining_uses(self, approval_id: str, max_uses: int | None) -> int | None:
        if max_uses is None:
            return None
        used = self._conn.execute(
            "SELECT COUNT(*) AS c FROM approval_usage "
            "WHERE approval_id = ? AND state IN ('reserved', 'held', 'consumed')",
            (approval_id,),
        ).fetchone()["c"]
        return max(max_uses - used, 0)

    # ── 조회 (FR-21) ────────────────────────────────────────────────

    def session(self, use_id: str) -> UseSession | None:
        row = self._conn.execute("SELECT * FROM use_session WHERE use_id = ?", (use_id,)).fetchone()
        if row is None:
            return None
        return self._session_from_row(row)

    def _session_from_row(self, row: sqlite3.Row) -> UseSession:
        return UseSession(
            use_id=row["use_id"],
            internal_request_id=row["internal_request_id"],
            node_id=row["node_id"],
            state=SessionState(row["state"]),
            rental_due_at=parse_iso(row["rental_due_at"]) if row["rental_due_at"] else None,
            closed_at=parse_iso(row["closed_at"]) if row["closed_at"] else None,
            end_reason=EndReason(row["end_reason"]) if row["end_reason"] else None,
            release_policy=from_json(row["release_policy"]),
        )

    def session_owner_matches(self, use_id: str, user_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM use_session WHERE use_id = ? AND user_id = ?", (use_id, user_id)
        ).fetchone()
        return row is not None
