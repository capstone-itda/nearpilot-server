"""`AdminRepository` 구현 (specs/db-admin-repository/spec.md).

관리 기능, 시드 스크립트, 수집 도구가 쓰는 쓰기 접점이다. `SqliteRepository`와 같은 연결을
받는다 — DB 쓰기 경로는 하나다 (`AGENTS.md` 의존 규칙 5). 인증·권한 검사는 하지 않는다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from nearpilot.db.repositories._common import immediate_transaction, iso, new_id, parse_iso, to_json
from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason, Step, TrustState
from nearpilot.shared.models import (
    AccountRegistration,
    AdminWriteResult,
    BeaconKey,
    BeaconRegistration,
    PlaceAssignment,
    PolicyRevision,
)


class SqliteAdminRepository:
    """`AdminRepository` Protocol 구현."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def register_account(self, req: AccountRegistration) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            try:
                self._conn.execute(
                    "INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
                    (req.user_id, req.name, req.token_hash, req.role.value),
                )
            except sqlite3.IntegrityError:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.ACCOUNT_ALREADY_EXISTS
                )
            self._append_audit({"event": "register_account", "user_id": req.user_id, "role": req.role.value})
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def register_beacon(self, req: BeaconRegistration) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            user_row = self._conn.execute(
                "SELECT 1 FROM user WHERE user_id = ?", (req.user_id,)
            ).fetchone()
            if user_row is None:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.ACCOUNT_NOT_FOUND
                )

            by_id = self._conn.execute(
                "SELECT user_id, uuid, major, minor FROM beacon WHERE beacon_id = ?", (req.beacon_id,)
            ).fetchone()
            if by_id is not None:
                same = (
                    by_id["user_id"] == req.user_id
                    and by_id["uuid"] == req.beacon.uuid
                    and by_id["major"] == req.beacon.major
                    and by_id["minor"] == req.beacon.minor
                )
                if not same:
                    # 같은 beacon_id가 다른 계정이나 다른 키로 바뀌는 등록은 거부한다 (spec §3 결정).
                    return AdminWriteResult(
                        outcome=AdminWriteOutcome.CONFLICT, reason=AdminWriteReason.STATE_CONFLICT
                    )
                self._activate_beacon(req)
                self._append_audit(
                    {
                        "event": "register_beacon", "beacon_id": req.beacon_id, "user_id": req.user_id,
                        "is_active": req.is_active,
                    }
                )
                return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

            by_key = self._conn.execute(
                "SELECT user_id FROM beacon WHERE uuid = ? AND major = ? AND minor = ?",
                (req.beacon.uuid, req.beacon.major, req.beacon.minor),
            ).fetchone()
            if by_key is not None:
                if by_key["user_id"] != req.user_id:
                    return AdminWriteResult(
                        outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.BEACON_ALREADY_REGISTERED
                    )
                # 같은 계정이 같은 키를 다른 beacon_id로 중복 등록하는 것을 막는다 (spec §3 결정).
                return AdminWriteResult(outcome=AdminWriteOutcome.CONFLICT, reason=AdminWriteReason.STATE_CONFLICT)

            if req.is_active:
                self._conn.execute(
                    "UPDATE beacon SET is_active = 0 WHERE user_id = ?", (req.user_id,)
                )
            self._conn.execute(
                "INSERT INTO beacon (beacon_id, user_id, uuid, major, minor, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    req.beacon_id, req.user_id, req.beacon.uuid, req.beacon.major, req.beacon.minor,
                    1 if req.is_active else 0,
                ),
            )
            self._append_audit(
                {
                    "event": "register_beacon", "beacon_id": req.beacon_id, "user_id": req.user_id,
                    "is_active": req.is_active,
                }
            )
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def set_node_trust(self, node_id: str, trust_state: TrustState) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            updated = self._conn.execute(
                "UPDATE node SET trust_state = ? WHERE node_id = ?", (trust_state.value, node_id)
            )
            if updated.rowcount == 0:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.NODE_NOT_FOUND
                )
            self._append_audit({"event": "set_node_trust", "node_id": node_id, "trust_state": trust_state.value})
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def assign_place(self, req: PlaceAssignment) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            owner_row = self._conn.execute(
                "SELECT 1 FROM user WHERE user_id = ?", (req.owner_user_id,)
            ).fetchone()
            if owner_row is None:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.ACCOUNT_NOT_FOUND
                )

            # 연결할 기기가 모두 있는지 먼저 본다 — 하나라도 없으면 아무것도 바꾸지 않는다 (원자적).
            for node_id in req.node_ids:
                node_row = self._conn.execute("SELECT 1 FROM node WHERE node_id = ?", (node_id,)).fetchone()
                if node_row is None:
                    return AdminWriteResult(
                        outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.NODE_NOT_FOUND
                    )

            existing = self._conn.execute(
                "SELECT owner_user_id, name FROM place WHERE place_id = ?", (req.place_id,)
            ).fetchone()
            if existing is not None:
                if existing["owner_user_id"] != req.owner_user_id or existing["name"] != req.name:
                    return AdminWriteResult(
                        outcome=AdminWriteOutcome.CONFLICT, reason=AdminWriteReason.STATE_CONFLICT
                    )
            else:
                self._conn.execute(
                    "INSERT INTO place (place_id, owner_user_id, name) VALUES (?, ?, ?)",
                    (req.place_id, req.owner_user_id, req.name),
                )

            for node_id in req.node_ids:
                self._conn.execute("UPDATE node SET place_id = ? WHERE node_id = ?", (req.place_id, node_id))

            self._append_audit(
                {
                    "event": "assign_place", "place_id": req.place_id, "owner_user_id": req.owner_user_id,
                    "node_ids": list(req.node_ids),
                }
            )
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def set_policy(self, revision: PolicyRevision, expected_version: int | None) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            node_row = self._conn.execute(
                "SELECT 1 FROM node WHERE node_id = ?", (revision.node_id,)
            ).fetchone()
            if node_row is None:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.NODE_NOT_FOUND
                )
            if revision.approver_user_id is not None:
                approver_row = self._conn.execute(
                    "SELECT 1 FROM user WHERE user_id = ?", (revision.approver_user_id,)
                ).fetchone()
                if approver_row is None:
                    return AdminWriteResult(
                        outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.ACCOUNT_NOT_FOUND
                    )

            existing = self._conn.execute(
                "SELECT policy_id, current_version FROM access_policy WHERE node_id = ?", (revision.node_id,)
            ).fetchone()
            current_version = existing["current_version"] if existing is not None else None
            if current_version != expected_version:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.CONFLICT, reason=AdminWriteReason.POLICY_VERSION_MISMATCH
                )
            expected_new_version = 1 if current_version is None else current_version + 1
            if revision.version != expected_new_version:
                # 기대 버전은 맞지만 새 리비전 번호가 현재+1이 아니다 (spec §5 결정).
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.CONFLICT, reason=AdminWriteReason.STATE_CONFLICT
                )

            if existing is None:
                # policy_revision.policy_id가 access_policy를 참조하므로 먼저 만든다.
                self._conn.execute(
                    "INSERT INTO access_policy (policy_id, node_id, current_version) VALUES (?, ?, ?)",
                    (revision.policy_id, revision.node_id, revision.version),
                )

            self._conn.execute(
                "INSERT INTO policy_revision (policy_id, version, visibility, mode, approver_user_id, "
                "approval_ttl_sec, approval_mode, max_uses) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.policy_id, revision.version, revision.visibility.value, revision.mode.value,
                    revision.approver_user_id, revision.approval_ttl_sec, revision.approval_mode.value,
                    revision.max_uses,
                ),
            )
            if existing is not None:
                self._conn.execute(
                    "UPDATE access_policy SET current_version = ? WHERE node_id = ?",
                    (revision.version, revision.node_id),
                )
            self._append_audit(
                {
                    "event": "set_policy", "policy_id": revision.policy_id, "node_id": revision.node_id,
                    "version": revision.version,
                }
            )
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def set_release_policy(self, node_id: str, release_policy: Mapping[str, Any]) -> AdminWriteResult:
        """`node.release_policy`만 바꾼다. 점유 세션은 생성 당시 값을 보존하므로 소급하지 않는다.

        `Mapping`이 아니면 `TypeError`다 — `to_json`은 빈 값을 `{}`로 바꾸므로 `None`이나 빈 목록이
        조용히 빈 종료 정책이 되지 않게 먼저 막는다.
        """
        if not isinstance(release_policy, Mapping):
            raise TypeError(f"release_policy 는 Mapping 이어야 한다: {type(release_policy).__name__}")
        value = to_json(release_policy)
        with immediate_transaction(self._conn):
            updated = self._conn.execute(
                "UPDATE node SET release_policy = ? WHERE node_id = ?", (value, node_id)
            )
            if updated.rowcount == 0:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.NODE_NOT_FOUND
                )
            self._append_audit(
                {"event": "set_release_policy", "node_id": node_id, "release_policy": dict(release_policy)}
            )
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def start_collection(
        self, collection_id: str, beacon: BeaconKey, label: str, started_at: datetime
    ) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            beacon_row = self._conn.execute(
                "SELECT beacon_id FROM beacon WHERE uuid = ? AND major = ? AND minor = ? AND is_active = ?",
                (beacon.uuid, beacon.major, beacon.minor, 1),
            ).fetchone()
            if beacon_row is None:
                # 비활성 비콘도 등록되지 않은 비콘과 같은 사유로 거부한다 (#138).
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.BEACON_NOT_FOUND
                )
            beacon_id = beacon_row["beacon_id"]

            if label != "none":
                node_row = self._conn.execute("SELECT 1 FROM node WHERE node_id = ?", (label,)).fetchone()
                if node_row is None:
                    return AdminWriteResult(
                        outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.NODE_NOT_FOUND
                    )

            existing = self._conn.execute(
                "SELECT 1 FROM collection_run WHERE collection_id = ?", (collection_id,)
            ).fetchone()
            if existing is not None:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.CONFLICT, reason=AdminWriteReason.STATE_CONFLICT
                )

            # 진행 중이거나(ended_at IS NULL) started_at 이후에 끝난 회차와 겹친다.
            # 끝 시각과 다음 시작 시각이 같으면 겹치지 않는다 (spec §3 결정).
            overlap = self._conn.execute(
                "SELECT 1 FROM collection_run WHERE beacon_id = ? AND (ended_at IS NULL OR ended_at > ?)",
                (beacon_id, iso(started_at)),
            ).fetchone()
            if overlap is not None:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.COLLECTION_OVERLAP
                )

            self._conn.execute(
                "INSERT INTO collection_run (collection_id, beacon_id, label, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (collection_id, beacon_id, label, iso(started_at)),
            )
            self._append_audit(
                {"event": "start_collection", "collection_id": collection_id, "beacon_id": beacon_id}
            )
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def end_collection(self, collection_id: str, ended_at: datetime) -> AdminWriteResult:
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT started_at, ended_at FROM collection_run WHERE collection_id = ?", (collection_id,)
            ).fetchone()
            if row is None:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.COLLECTION_NOT_FOUND
                )
            if row["ended_at"] is not None:
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.CONFLICT, reason=AdminWriteReason.STATE_CONFLICT
                )
            if ended_at <= parse_iso(row["started_at"]):
                return AdminWriteResult(
                    outcome=AdminWriteOutcome.REJECTED, reason=AdminWriteReason.INVALID_INTERVAL
                )

            self._conn.execute(
                "UPDATE collection_run SET ended_at = ? WHERE collection_id = ?",
                (iso(ended_at), collection_id),
            )
            self._append_audit({"event": "end_collection", "collection_id": collection_id})
            return AdminWriteResult(outcome=AdminWriteOutcome.APPLIED, reason=AdminWriteReason.OK)

    def _activate_beacon(self, req: BeaconRegistration) -> None:
        """같은 `beacon_id`의 재등록을 `is_active`대로 반영한다. 먼저 다른 비콘을 끈 뒤 이 비콘을

        켠다 — `beacon_one_active_per_user` 부분 UNIQUE 인덱스가 중간에도 위반되지 않게 한다.
        """
        if req.is_active:
            self._conn.execute(
                "UPDATE beacon SET is_active = 0 WHERE user_id = ? AND beacon_id != ?",
                (req.user_id, req.beacon_id),
            )
        self._conn.execute(
            "UPDATE beacon SET is_active = ? WHERE beacon_id = ?",
            (1 if req.is_active else 0, req.beacon_id),
        )

    def _append_audit(self, payload: Mapping[str, Any]) -> None:
        """감사 payload에 토큰 원문·`token_hash`를 넣지 않는다 (NFR-08, #29)."""
        self._conn.execute(
            "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
            "payload_json, created_at) VALUES (?, ?, NULL, NULL, ?, ?, ?)",
            (new_id(), new_id(), Step.ADMIN.value, to_json(payload), iso(datetime.now(timezone.utc))),
        )
