"""`Repository`·`AccountLookup` 구현 (specs/db-repository/spec.md).

쓰기는 이 클래스 하나로만 한다 (`AGENTS.md` 의존 규칙 5). 연결은 스레드 하나당 하나를
`db/schema/connect()`로 만들어 넘긴다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from nearpilot.db.repositories._common import (
    from_json, immediate_transaction, iso, new_id, parse_iso, settings_to_json, to_json,
)
from nearpilot.shared.enums import (
    AccessMode,
    Action,
    ApprovalDecisionReason,
    ApprovalMode,
    ApprovalStatus,
    ApprovalUsageState,
    Capability,
    DeviceState,
    EndReason,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    Role,
    SessionState,
    Step,
    StoreConflictReason,
    StoreOutcome,
    TargetKind,
    TrustState,
    Verdict,
    Visibility,
)
from nearpilot.shared.errors import StoreError
from nearpilot.shared.models import (
    Account,
    Approval,
    ApprovalDecisionRequest,
    ApprovalDecisionResult,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalRequestResult,
    BeaconKey,
    Decision,
    DecisionRecord,
    NodeAnnounce,
    NodeInfo,
    PolicyRevision,
    RequestLink,
    ReserveRequest,
    ReserveResult,
    RssiFrame,
    StoreResult,
    TargetSpec,
    UseRequest,
    UseSession,
)


_EXHAUSTED = object()  # 승인 남은 횟수가 0일 때 _reserve_usage()가 돌려주는 표지값


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
            "AND a.status = ? AND a.valid_until IS NOT NULL AND a.valid_until > ? "
            "ORDER BY a.valid_until DESC, a.approval_id ASC LIMIT 1",
            (user_id, node_id, action.value, policy_version, ApprovalStatus.APPROVED.value, iso(now)),
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

    def approvals_in_scope(self, user_id: str, node_id: str) -> Sequence[ApprovalRecord]:
        """요청자·기기의 승인 이력을 모두 반환한다 (#60, FR-03, FR-14).

        시각·동작·정책 버전으로 거르지 않는다. `core/policy`가 승인 필요 사유를 고르는 데
        쓴다. 저장소를 바꾸지 않는다.
        """
        rows = self._conn.execute(
            "SELECT a.approval_id, a.action, a.policy_version, a.status, a.created_at, "
            "a.pending_until, a.valid_until, pr.max_uses "
            "FROM approval a "
            "JOIN policy_revision pr ON pr.policy_id = a.policy_id AND pr.version = a.policy_version "
            "WHERE a.requester_user_id = ? AND a.node_id = ? "
            "ORDER BY a.created_at",
            (user_id, node_id),
        ).fetchall()
        return [
            ApprovalRecord(
                approval_id=row["approval_id"],
                action=Action(row["action"]),
                policy_version=row["policy_version"],
                status=ApprovalStatus(row["status"]),
                created_at=parse_iso(row["created_at"]),
                pending_until=parse_iso(row["pending_until"]),
                valid_until=parse_iso(row["valid_until"]) if row["valid_until"] else None,
                remaining_uses=self._remaining_uses(row["approval_id"], row["max_uses"]),
            )
            for row in rows
        ]

    def _remaining_uses(self, approval_id: str, max_uses: int | None) -> int | None:
        if max_uses is None:
            return None
        used = self._conn.execute(
            "SELECT COUNT(*) AS c FROM approval_usage "
            "WHERE approval_id = ? AND state IN (?, ?, ?)",
            (
                approval_id, ApprovalUsageState.RESERVED.value, ApprovalUsageState.HELD.value,
                ApprovalUsageState.CONSUMED.value,
            ),
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

    def is_place_owner(self, user_id: str, node_id: str) -> bool:
        """기기가 속한 장소의 소유 계정인지만 본다. 소유 계정 ID는 반환하지 않는다 (#84, FR-11).

        기기가 없거나 장소에 배치되지 않았으면 `False`다 — `JOIN`이 그 경우 행을 못 찾는다.
        """
        try:
            row = self._conn.execute(
                "SELECT p.owner_user_id FROM node n JOIN place p ON p.place_id = n.place_id "
                "WHERE n.node_id = ?",
                (node_id,),
            ).fetchone()
        except sqlite3.Error as error:
            raise StoreError(str(error)) from error
        return row is not None and row["owner_user_id"] == user_id

    # ── ⓪ 중복 (FR-22) ──────────────────────────────────────────────

    def link_request(self, req: UseRequest, input_digest: str) -> RequestLink:
        target_spec = self._serialize_target(req.target)
        internal_request_id = new_id()
        try:
            self._conn.execute(
                "INSERT INTO request (internal_request_id, user_id, request_id, input_digest, "
                "parent_internal_request_id, node_id, target_spec, action, host_kind, received_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    internal_request_id, req.user_id, req.request_id, input_digest,
                    req.parent_internal_request_id, target_spec, req.action.value,
                    req.host_kind, iso(req.received_at),
                ),
            )
        except sqlite3.IntegrityError:
            return self._link_existing(req, input_digest)
        return RequestLink(internal_request_id=internal_request_id, is_new=True, conflict=False)

    def _serialize_target(self, target: TargetSpec) -> str:
        if target.kind == TargetKind.EXPLICIT and target.node_id:
            return target.node_id
        return f"{target.kind.value}:{target.capability.value}"

    def _link_existing(self, req: UseRequest, input_digest: str) -> RequestLink:
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT internal_request_id, input_digest, verdict, reason_code, node_id, p_max, "
                "policy_ver FROM request WHERE user_id = ? AND request_id = ?",
                (req.user_id, req.request_id),
            ).fetchone()
            if row["input_digest"] != input_digest:
                return RequestLink(internal_request_id=row["internal_request_id"], is_new=False, conflict=True)

            internal_request_id = row["internal_request_id"]
            if row["verdict"] == Verdict.DENY.value and row["reason_code"] == ReasonCode.INTERNAL_ERROR.value:
                retried = self._retry_internal_error(internal_request_id, input_digest, row)
                return RequestLink(
                    internal_request_id=internal_request_id, is_new=retried, conflict=False, previous=None
                )

            previous = None
            if row["verdict"] is not None:
                use_row = self._conn.execute(
                    "SELECT use_id FROM use_session WHERE internal_request_id = ?",
                    (internal_request_id,),
                ).fetchone()
                previous = Decision(
                    internal_request_id=internal_request_id,
                    request_id=req.request_id,
                    verdict=Verdict(row["verdict"]),
                    reason=ReasonCode(row["reason_code"]),
                    node_id=row["node_id"],
                    use_id=use_row["use_id"] if use_row else None,
                    p_max=row["p_max"],
                )
            return RequestLink(
                internal_request_id=internal_request_id, is_new=False, conflict=False, previous=previous
            )

    def _retry_internal_error(
        self, internal_request_id: str, input_digest: str, row: sqlite3.Row
    ) -> bool:
        """`INTERNAL_ERROR`로 끝난 요청을 같은 키·같은 입력으로 다시 보내면 판정 칸을 비워

        새 요청처럼 재판정하게 한다 (#76). 비운 뒤 `True`, 다른 재전송이 먼저 비웠으면
        `False`를 반환한다(그 쪽은 `verdict IS NULL`인 "처리 중"으로 떨어진다).
        """
        updated = self._conn.execute(
            "UPDATE request SET verdict = NULL, reason_code = NULL, node_id = NULL, p_max = NULL, "
            "model = NULL, calib = NULL, policy_ver = NULL, config_ver = NULL, decision_context = NULL, "
            "settings_json = NULL "
            "WHERE internal_request_id = ? AND input_digest = ? AND verdict = ? AND reason_code = ?",
            (internal_request_id, input_digest, Verdict.DENY.value, ReasonCode.INTERNAL_ERROR.value),
        )
        if updated.rowcount == 0:
            return False
        self._conn.execute(
            "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
            "payload_json, created_at) VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (
                new_id(), new_id(), internal_request_id, Step.DEDUP.value,
                to_json(
                    {
                        "event": "link_request_retry",
                        "previous_verdict": row["verdict"],
                        "previous_reason_code": row["reason_code"],
                        "previous_node_id": row["node_id"],
                        "previous_p_max": row["p_max"],
                        "previous_policy_ver": row["policy_ver"],
                    }
                ),
                iso(datetime.now(timezone.utc)),
            ),
        )
        return True

    def save_decision(self, decision: Decision, record: DecisionRecord) -> StoreResult:
        row = self._conn.execute(
            "SELECT verdict, reason_code, node_id, p_max, model, calib, policy_ver, config_ver "
            "FROM request WHERE internal_request_id = ?",
            (decision.internal_request_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"요청 {decision.internal_request_id} 가 없다")
        if row["verdict"] is not None:
            if self._decision_matches(row, decision, record):
                return StoreResult(outcome=StoreOutcome.ALREADY_APPLIED)
            return StoreResult(outcome=StoreOutcome.CONFLICT, conflict_reason=StoreConflictReason.CONTENT_CONFLICT)
        decision_context = to_json(
            {
                "posteriors": dict(record.posteriors),
                "window_start": iso(record.window_start) if record.window_start else None,
                "window_end": iso(record.window_end) if record.window_end else None,
                "message": decision.message,
            }
        )
        updated = self._conn.execute(
            "UPDATE request SET verdict = ?, reason_code = ?, node_id = ?, p_max = ?, model = ?, "
            "calib = ?, policy_ver = ?, config_ver = ?, decision_context = ?, settings_json = ? "
            "WHERE internal_request_id = ? AND verdict IS NULL",
            (
                decision.verdict.value, decision.reason.value, decision.node_id, decision.p_max,
                record.model_version, record.calib_version, record.policy_version, record.settings.version,
                decision_context, settings_to_json(record.settings), decision.internal_request_id,
            ),
        )
        if updated.rowcount == 0:
            return self.save_decision(decision, record)
        return StoreResult(outcome=StoreOutcome.APPLIED)

    @staticmethod
    def _decision_matches(row: sqlite3.Row, decision: Decision, record: DecisionRecord) -> bool:
        return (
            row["verdict"] == decision.verdict.value
            and row["reason_code"] == decision.reason.value
            and row["node_id"] == decision.node_id
            and row["p_max"] == decision.p_max
            and row["model"] == record.model_version
            and row["calib"] == record.calib_version
            and row["policy_ver"] == record.policy_version
            and row["config_ver"] == record.settings.version
        )

    # ── ① 비콘 저장 (FR-05) ──────────────────────────────────────────

    def save_rssi(self, frame: RssiFrame) -> None:
        beacon_row = self._conn.execute(
            "SELECT beacon_id, is_active FROM beacon WHERE uuid = ? AND major = ? AND minor = ?",
            (frame.beacon.uuid, frame.beacon.major, frame.beacon.minor),
        ).fetchone()
        if beacon_row is None or not beacon_row["is_active"]:
            return
        node_row = self._conn.execute("SELECT 1 FROM node WHERE node_id = ?", (frame.node_id,)).fetchone()
        if node_row is None:
            return
        beacon_id = beacon_row["beacon_id"]
        with immediate_transaction(self._conn):
            # 구간은 started_at <= frame_ts < ended_at다(시작 포함, 끝 미만). 회차 찾기와 관측
            # 저장을 같은 트랜잭션에서 한다 (architecture.md §4 "RSSI 수집 회차").
            collection_row = self._conn.execute(
                "SELECT collection_id FROM collection_run "
                "WHERE beacon_id = ? AND started_at <= ? AND (ended_at IS NULL OR ended_at > ?)",
                (beacon_id, iso(frame.frame_ts), iso(frame.frame_ts)),
            ).fetchone()
            collection_id = collection_row["collection_id"] if collection_row else None
            self._conn.execute(
                "INSERT OR IGNORE INTO rssi_observation "
                "(obs_id, node_id, beacon_id, rssi, frame_ts, received_at, sent_at, session_id, seq, "
                "collection_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(), frame.node_id, beacon_id, frame.rssi, iso(frame.frame_ts),
                    iso(frame.received_at), iso(frame.sent_at), frame.session_id, frame.seq,
                    collection_id,
                ),
            )

    # ── ② 발표와 기기 연결 (FR-04, FR-13) ─────────────────────────────

    def upsert_announced_node(self, announce: NodeAnnounce) -> None:
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT capability, occupancy FROM node WHERE node_id = ?", (announce.node_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, "
                    "device_state, exclusive_group, release_policy) "
                    "VALUES (?, NULL, ?, ?, ?, ?, NULL, '{}')",
                    (
                        announce.node_id, announce.capability.value, announce.occupancy.value,
                        TrustState.DISCOVERED.value, DeviceState.AVAILABLE.value,
                    ),
                )
                return
            if row["capability"] == announce.capability.value and row["occupancy"] == announce.occupancy.value:
                return
            self._conn.execute(
                "UPDATE node SET capability = ?, occupancy = ?, trust_state = ? "
                "WHERE node_id = ?",
                (announce.capability.value, announce.occupancy.value, TrustState.DISCOVERED.value, announce.node_id),
            )
            self._conn.execute(
                "INSERT INTO audit_log (log_id, event_id, internal_request_id, command_id, step, "
                "payload_json, created_at) VALUES (?, ?, NULL, NULL, ?, ?, ?)",
                (
                    new_id(),
                    new_id(),
                    Step.ANNOUNCE.value,
                    to_json(
                        {
                            "node_id": announce.node_id,
                            "previous_capability": row["capability"],
                            "previous_occupancy": row["occupancy"],
                            "capability": announce.capability.value,
                            "occupancy": announce.occupancy.value,
                        }
                    ),
                    iso(announce.received_at),
                ),
            )

    def touch_node(self, node_id: str, seen_at: datetime) -> None:
        self._conn.execute(
            "UPDATE node SET last_seen_at = ? "
            "WHERE node_id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (iso(seen_at), node_id, iso(seen_at)),
        )

    def set_node_session(self, node_id: str, session_id: str, first_seen_at: datetime) -> None:
        self._conn.execute(
            "UPDATE node SET session_id = ?, session_first_seen_at = ? "
            "WHERE node_id = ? AND (session_id IS NULL OR session_id != ?)",
            (session_id, iso(first_seen_at), node_id, session_id),
        )

    # ── 승인 신청과 결정 (FR-03, FR-14) ────────────────────────────────

    def create_approval_request(self, req: ApprovalRequest) -> ApprovalRequestResult:
        with immediate_transaction(self._conn):
            existing = self._conn.execute(
                "SELECT approval_id, created_at, pending_until FROM approval "
                "WHERE requester_user_id = ? AND node_id = ? AND action = ? AND policy_version = ? "
                "AND status = ? AND pending_until > ? "
                "ORDER BY created_at DESC LIMIT 1",
                (
                    req.requester_user_id, req.node_id, req.action.value, req.policy_version,
                    ApprovalStatus.PENDING.value, iso(req.created_at),
                ),
            ).fetchone()
            if existing is not None:
                return ApprovalRequestResult(
                    approval_id=existing["approval_id"],
                    status=ApprovalStatus.PENDING,
                    created_at=parse_iso(existing["created_at"]),
                    pending_until=parse_iso(existing["pending_until"]),
                    is_new=False,
                )
            approval_id = new_id()
            pending_until = req.created_at + timedelta(seconds=req.settings.approval_pending_ttl_sec)
            self._conn.execute(
                "INSERT INTO approval (approval_id, node_id, requester_user_id, internal_request_id, "
                "approved_by_user_id, action, policy_id, policy_version, status, created_at, "
                "pending_until, valid_until, used_count) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, 0)",
                (
                    approval_id, req.node_id, req.requester_user_id, req.internal_request_id,
                    req.action.value, req.policy_id, req.policy_version, ApprovalStatus.PENDING.value,
                    iso(req.created_at), iso(pending_until),
                ),
            )
            return ApprovalRequestResult(
                approval_id=approval_id,
                status=ApprovalStatus.PENDING,
                created_at=req.created_at,
                pending_until=pending_until,
                is_new=True,
            )

    def decide_approval(self, req: ApprovalDecisionRequest) -> ApprovalDecisionResult:
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT a.status, a.valid_until, a.approved_by_user_id, a.policy_id, a.policy_version, "
                "a.pending_until, a.node_id, pr.approval_ttl_sec, ap.current_version "
                "FROM approval a "
                "JOIN policy_revision pr ON pr.policy_id = a.policy_id AND pr.version = a.policy_version "
                "JOIN access_policy ap ON ap.policy_id = a.policy_id "
                "WHERE a.approval_id = ?",
                (req.approval_id,),
            ).fetchone()
            if row is None:
                return ApprovalDecisionResult(
                    applied=False, reason=ApprovalDecisionReason.NOT_FOUND, status=None, valid_until=None
                )
            if row["status"] != ApprovalStatus.PENDING.value:
                current_status = ApprovalStatus(row["status"])
                if current_status == req.decision and row["approved_by_user_id"] == req.decided_by_user_id:
                    return ApprovalDecisionResult(
                        applied=False,
                        reason=ApprovalDecisionReason.ALREADY_APPLIED,
                        status=current_status,
                        valid_until=parse_iso(row["valid_until"]) if row["valid_until"] else None,
                    )
                return ApprovalDecisionResult(
                    applied=False,
                    reason=ApprovalDecisionReason.ALREADY_DECIDED,
                    status=current_status,
                    valid_until=parse_iso(row["valid_until"]) if row["valid_until"] else None,
                )
            if row["pending_until"] is not None and iso(req.decided_at) > row["pending_until"]:
                return ApprovalDecisionResult(
                    applied=False, reason=ApprovalDecisionReason.REQUEST_EXPIRED, status=None, valid_until=None
                )
            if row["policy_version"] != row["current_version"]:
                return ApprovalDecisionResult(
                    applied=False,
                    reason=ApprovalDecisionReason.POLICY_VERSION_MISMATCH,
                    status=None,
                    valid_until=None,
                )
            valid_until = None
            if req.decision == ApprovalStatus.APPROVED and row["approval_ttl_sec"] is not None:
                valid_until = req.decided_at + timedelta(seconds=row["approval_ttl_sec"])
            self._conn.execute(
                "UPDATE approval SET status = ?, approved_by_user_id = ?, valid_until = ? "
                "WHERE approval_id = ? AND status = ?",
                (
                    req.decision.value, req.decided_by_user_id, iso(valid_until) if valid_until else None,
                    req.approval_id, ApprovalStatus.PENDING.value,
                ),
            )
            return ApprovalDecisionResult(
                applied=True, reason=ApprovalDecisionReason.OK, status=req.decision, valid_until=valid_until
            )

    # ── ⑤⑥ 원자적 예약 (FR-19, NFR-03) ────────────────────────────────

    def reserve_if_free(self, req: ReserveRequest) -> ReserveResult:
        with immediate_transaction(self._conn):
            node = self._conn.execute(
                "SELECT occupancy, device_state, exclusive_group FROM node WHERE node_id = ?",
                (req.node_id,),
            ).fetchone()
            if node is None:
                raise LookupError(f"기기 {req.node_id} 가 없다")
            request_row = self._conn.execute(
                "SELECT received_at FROM request WHERE internal_request_id = ?",
                (req.internal_request_id,),
            ).fetchone()
            if request_row is None:
                raise LookupError(f"요청 {req.internal_request_id} 가 없다")
            reserved_at_iso = request_row["received_at"]

            # 같은 internal_request_id의 기존 예약은 재조작 검사보다 먼저 본다. 최초 대여 요청이
            # 이미 ACTIVE가 된 뒤 재전송되어도 REOPERATING이 아니라 기존 예약을 그대로 반환한다.
            existing_session = self._conn.execute(
                "SELECT use_id, reserved_at FROM use_session WHERE internal_request_id = ?",
                (req.internal_request_id,),
            ).fetchone()
            if existing_session is not None:
                usage_id = self._lookup_usage(req.approval_id, req.internal_request_id)
                return ReserveResult(
                    outcome=ReserveOutcome.RESERVED, use_id=existing_session["use_id"], usage_id=usage_id,
                    reserved_at=parse_iso(existing_session["reserved_at"]),
                )

            own_active = self._conn.execute(
                "SELECT use_id FROM use_session WHERE node_id = ? AND user_id = ? AND state = ?",
                (req.node_id, req.user_id, SessionState.ACTIVE.value),
            ).fetchone()
            if own_active is not None:
                # 세션은 ACTIVE로 보존돼도 기기가 FAULT로 격리됐으면(isolate_device) 재조작을
                # 통과시키지 않는다. RESERVED·CLOSING·FAULT는 DEVICE_BUSY다 (interfaces.py).
                if node["device_state"] != DeviceState.ACTIVE.value:
                    return ReserveResult(outcome=ReserveOutcome.DEVICE_BUSY)
                usage_id = self._reserve_usage(req.approval_id, req.internal_request_id, reserved_at_iso)
                if usage_id is _EXHAUSTED:
                    return ReserveResult(outcome=ReserveOutcome.APPROVAL_EXHAUSTED)
                return ReserveResult(
                    outcome=ReserveOutcome.REOPERATING, use_id=own_active["use_id"], usage_id=usage_id,
                    reserved_at=parse_iso(reserved_at_iso) if usage_id is not None else None,
                )

            if node["occupancy"] == Occupancy.SHARED.value:
                if req.approval_id is None:
                    return ReserveResult(outcome=ReserveOutcome.RESERVED)
                usage_id = self._reserve_usage(req.approval_id, req.internal_request_id, reserved_at_iso)
                if usage_id is _EXHAUSTED:
                    return ReserveResult(outcome=ReserveOutcome.APPROVAL_EXHAUSTED)
                return ReserveResult(
                    outcome=ReserveOutcome.RESERVED, usage_id=usage_id, reserved_at=parse_iso(reserved_at_iso)
                )

            if node["device_state"] != DeviceState.AVAILABLE.value:
                return ReserveResult(outcome=ReserveOutcome.DEVICE_BUSY)

            if node["exclusive_group"] is not None:
                busy = self._conn.execute(
                    "SELECT 1 FROM use_session s JOIN node n ON n.node_id = s.node_id "
                    "WHERE n.exclusive_group = ? AND s.user_id = ? "
                    "AND s.state IN (?, ?, ?)",
                    (
                        node["exclusive_group"], req.user_id, SessionState.RESERVED.value,
                        SessionState.ACTIVE.value, SessionState.CLOSING.value,
                    ),
                ).fetchone()
                if busy is not None:
                    return ReserveResult(outcome=ReserveOutcome.USER_BUSY)

            usage_id = self._reserve_usage(req.approval_id, req.internal_request_id, reserved_at_iso)
            if usage_id is _EXHAUSTED:
                return ReserveResult(outcome=ReserveOutcome.APPROVAL_EXHAUSTED)

            use_id = new_id()
            self._conn.execute(
                "INSERT INTO use_session (use_id, internal_request_id, user_id, node_id, state, "
                "release_policy, reserved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    use_id, req.internal_request_id, req.user_id, req.node_id, SessionState.RESERVED.value,
                    to_json(req.release_policy), reserved_at_iso,
                ),
            )
            self._conn.execute(
                "UPDATE node SET device_state = ? WHERE node_id = ?",
                (DeviceState.RESERVED.value, req.node_id),
            )
            return ReserveResult(
                outcome=ReserveOutcome.RESERVED, use_id=use_id, usage_id=usage_id,
                reserved_at=parse_iso(reserved_at_iso),
            )

    def _lookup_usage(self, approval_id: str | None, internal_request_id: str) -> str | None:
        if approval_id is None:
            return None
        row = self._conn.execute(
            "SELECT usage_id FROM approval_usage WHERE approval_id = ? AND internal_request_id = ?",
            (approval_id, internal_request_id),
        ).fetchone()
        return row["usage_id"] if row else None

    def _reserve_usage(
        self, approval_id: str | None, internal_request_id: str, reserved_at_iso: str
    ) -> str | None:
        """승인 사용분을 예약한다. 이미 예약했으면 기존 사용분을 그대로 반환한다(멱등).

        남은 횟수가 0이면 `_EXHAUSTED`를 반환한다. `approval_id` 가 `None` 이면 예약하지 않는다.
        """
        if approval_id is None:
            return None
        existing = self._conn.execute(
            "SELECT usage_id FROM approval_usage WHERE approval_id = ? AND internal_request_id = ?",
            (approval_id, internal_request_id),
        ).fetchone()
        if existing is not None:
            return existing["usage_id"]
        policy_row = self._conn.execute(
            "SELECT pr.max_uses FROM approval a "
            "JOIN policy_revision pr ON pr.policy_id = a.policy_id AND pr.version = a.policy_version "
            "WHERE a.approval_id = ?",
            (approval_id,),
        ).fetchone()
        max_uses = policy_row["max_uses"] if policy_row else None
        if max_uses is not None:
            used = self._conn.execute(
                "SELECT COUNT(*) AS c FROM approval_usage "
                "WHERE approval_id = ? AND state IN (?, ?, ?)",
                (
                    approval_id, ApprovalUsageState.RESERVED.value, ApprovalUsageState.HELD.value,
                    ApprovalUsageState.CONSUMED.value,
                ),
            ).fetchone()["c"]
            if used >= max_uses:
                return _EXHAUSTED
        usage_id = new_id()
        self._conn.execute(
            "INSERT INTO approval_usage (usage_id, approval_id, internal_request_id, command_id, state, "
            "reserved_at) VALUES (?, ?, ?, NULL, ?, ?)",
            (usage_id, approval_id, internal_request_id, ApprovalUsageState.RESERVED.value, reserved_at_iso),
        )
        return usage_id
