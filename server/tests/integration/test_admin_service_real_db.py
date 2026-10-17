"""`DefaultAdminService` (specs/core-ops-cli/spec.md §6 "AdminService와 seed").

실제 SQLite(`:memory:`)에 `SqliteRepository`와 `SqliteAdminRepository`를 붙여 검사한다.
기기는 MQTT 발표로 생기고 `AdminRepository`로 만들 수 없어서 기기 행은 SQL로 직접 넣는다.
"""

from __future__ import annotations

import json

import pytest

from nearpilot.core.admin import DefaultAdminService, default_token
from nearpilot.db.repositories.admin_repository import SqliteAdminRepository
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.enums import (
    AccessMode,
    AdminWriteOutcome,
    AdminWriteReason,
    ApprovalMode,
    Role,
    TrustState,
    Visibility,
)
from nearpilot.shared.interfaces import AdminService
from nearpilot.shared.models import (
    AdminWriteResult,
    BeaconKey,
    BeaconRegistration,
    PlaceAssignment,
    PolicySettings,
)
from nearpilot.shared.tokens import hash_token

APPLIED = AdminWriteResult(AdminWriteOutcome.APPLIED, AdminWriteReason.OK)
LOCKER_RETURN = {"check_criteria": {"id": "locker-return", "version": 1}}

_TABLES = ("user", "beacon", "place", "node", "access_policy", "policy_revision", "audit_log")


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def service(conn):
    return DefaultAdminService(SqliteRepository(conn), SqliteAdminRepository(conn))


def _insert_user(conn, user_id, role="owner"):
    conn.execute(
        "INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, f"name-{user_id}", f"hash-{user_id}", role),
    )


def _insert_place(conn, place_id, owner_user_id):
    conn.execute(
        "INSERT INTO place (place_id, owner_user_id, name) VALUES (?, ?, ?)",
        (place_id, owner_user_id, f"name-{place_id}"),
    )


def _insert_node(conn, node_id, place_id):
    conn.execute(
        "INSERT INTO node (node_id, place_id, capability, occupancy, trust_state, device_state, "
        "release_policy) VALUES (?, ?, 'locker', 'rental', 'discovered', 'AVAILABLE', '{}')",
        (node_id, place_id),
    )


def _world(conn):
    """owner1: place1(node1, node2). owner2: place2(node3). 장소 없는 기기 loose. 소유자가 아닌 visitor1."""
    for user_id, role in (("owner1", "owner"), ("owner2", "owner"), ("visitor1", "visitor")):
        _insert_user(conn, user_id, role)
    _insert_place(conn, "place1", "owner1")
    _insert_place(conn, "place2", "owner2")
    _insert_node(conn, "node1", "place1")
    _insert_node(conn, "node2", "place1")
    _insert_node(conn, "node3", "place2")
    _insert_node(conn, "loose", None)


def _snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in _TABLES
    }


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def _settings(node_id, **changes):
    values = dict(
        node_id=node_id, visibility=Visibility.PRIVATE, mode=AccessMode.RESTRICTED,
        approver_user_id="owner1", approval_mode=ApprovalMode.ONE_TIME, approval_ttl_sec=3600, max_uses=None,
    )
    values.update(changes)
    return PolicySettings(**values)


def _revisions(conn, node_id):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT pr.policy_id, pr.version, pr.max_uses FROM policy_revision pr "
            "JOIN access_policy ap ON ap.policy_id = pr.policy_id WHERE ap.node_id = ? ORDER BY pr.version",
            (node_id,),
        )
    ]


def test_FR12A_default_admin_service_implements_protocol(service):
    assert isinstance(service, AdminService)


# ── FR-12A: 행위자 검사 ────────────────────────────────────────────────

NON_OWNER_CASES = {
    "trust-by-visitor": lambda s: s.set_node_trust("visitor1", "node1", TrustState.TRUSTED),
    "trust-by-other-owner": lambda s: s.set_node_trust("owner2", "node1", TrustState.TRUSTED),
    "policy-by-visitor": lambda s: s.set_policy("visitor1", _settings("node1")).result,
    "policy-by-other-owner": lambda s: s.set_policy("owner2", _settings("node1")).result,
    "release-policy-by-visitor": lambda s: s.set_release_policy("visitor1", "node1", LOCKER_RETURN),
    "release-policy-by-other-owner": lambda s: s.set_release_policy("owner2", "node1", LOCKER_RETURN),
    "trust-of-unplaced-node": lambda s: s.set_node_trust("owner1", "loose", TrustState.TRUSTED),
    "policy-of-unplaced-node": lambda s: s.set_policy("owner1", _settings("loose")).result,
    "release-policy-of-unplaced-node": lambda s: s.set_release_policy("owner1", "loose", LOCKER_RETURN),
    "assign-actor-is-not-owner": lambda s: s.assign_place(
        "owner2", PlaceAssignment("place1", "owner1", "name-place1", ("loose",))
    ),
    "assign-moves-node-of-other-owners-place": lambda s: s.assign_place(
        "owner2", PlaceAssignment("place9", "owner2", "name-place9", ("node1",))
    ),
}


@pytest.mark.parametrize("call", NON_OWNER_CASES.values(), ids=NON_OWNER_CASES.keys())
def test_FR12A_admin_rejects_non_owner(conn, service, call):
    _world(conn)
    before = _snapshot(conn)

    result = call(service)

    assert result == AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.NOT_PLACE_OWNER)
    assert result.reason == "NOT_PLACE_OWNER"  # 저장·표시되는 값이라 문자열도 고정한다
    assert _snapshot(conn) == before  # 감사 행도 늘지 않는다


def test_FR12A_admin_owner_writes_node_settings(conn, service):
    _world(conn)

    assert service.set_node_trust("owner1", "node1", TrustState.TRUSTED) == APPLIED
    assert service.set_release_policy("owner1", "node1", LOCKER_RETURN) == APPLIED

    row = conn.execute("SELECT trust_state, device_state, release_policy FROM node WHERE node_id = 'node1'").fetchone()
    assert (row["trust_state"], row["device_state"]) == ("trusted", "AVAILABLE")
    assert json.loads(row["release_policy"]) == LOCKER_RETURN
    other = conn.execute("SELECT trust_state, release_policy FROM node WHERE node_id = 'node2'").fetchone()
    assert (other["trust_state"], other["release_policy"]) == ("discovered", "{}")


def test_FR12A_admin_assign_place_by_owner(conn, service):
    _world(conn)

    # 장소 없는 기기를 자기 장소에 넣는다.
    assert service.assign_place("owner1", PlaceAssignment("place1", "owner1", "name-place1", ("loose",))) == APPLIED
    # 자기 장소의 기기를 자기 새 장소로 옮긴다.
    assert service.assign_place("owner1", PlaceAssignment("place9", "owner1", "name-place9", ("node1",))) == APPLIED

    places = {row["node_id"]: row["place_id"] for row in conn.execute("SELECT node_id, place_id FROM node")}
    assert places == {"node1": "place9", "node2": "place1", "node3": "place2", "loose": "place1"}


def test_FR12A_admin_assign_place_cannot_take_over_existing_place(conn, service):
    _world(conn)
    before = _snapshot(conn)

    result = service.assign_place("owner2", PlaceAssignment("place1", "owner2", "name-place1", ("loose",)))

    assert result == AdminWriteResult(AdminWriteOutcome.CONFLICT, AdminWriteReason.STATE_CONFLICT)
    assert _snapshot(conn) == before


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.set_node_trust("owner1", "ghost", TrustState.TRUSTED),
        lambda s: s.set_policy("owner1", _settings("ghost")).result,
        lambda s: s.set_release_policy("owner1", "ghost", LOCKER_RETURN),
        lambda s: s.assign_place("owner1", PlaceAssignment("place1", "owner1", "name-place1", ("ghost",))),
    ],
    ids=["trust", "policy", "release-policy", "assign-place"],
)
def test_FR13_admin_unknown_node_rejected(conn, service, call):
    _world(conn)
    before = _snapshot(conn)

    result = call(service)

    assert result == AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.NODE_NOT_FOUND)
    assert _snapshot(conn) == before


# ── FR-14: 정책 리비전 ────────────────────────────────────────────────


def test_FR14_admin_policy_new_revision_only_when_changed(conn, service):
    _world(conn)

    first = service.set_policy("owner1", _settings("node1"))
    assert first.result == APPLIED
    assert (first.version, first.changed) == (1, True)
    assert _revisions(conn, "node1") == [("pol-node1", 1, None)]

    audit_rows = _count(conn, "audit_log")
    same = service.set_policy("owner1", _settings("node1"))
    assert same.result == APPLIED
    assert (same.version, same.changed) == (1, False)
    assert _revisions(conn, "node1") == [("pol-node1", 1, None)]  # 리비전 행 수 그대로
    assert _count(conn, "audit_log") == audit_rows  # 쓰지 않았으므로 감사도 늘지 않는다

    changed = service.set_policy("owner1", _settings("node1", max_uses=3))
    assert changed.result == APPLIED
    assert (changed.version, changed.changed) == (2, True)
    assert _revisions(conn, "node1") == [("pol-node1", 1, None), ("pol-node1", 2, 3)]  # 버전 1은 그대로
    current = conn.execute("SELECT current_version FROM access_policy WHERE node_id = 'node1'").fetchone()
    assert current["current_version"] == 2


@pytest.mark.parametrize(
    "field_changes",
    [
        {"visibility": Visibility.PUBLIC},
        {"mode": AccessMode.OPEN},
        {"approver_user_id": "owner2"},
        {"approval_mode": ApprovalMode.TIME_WINDOW},
        {"approval_ttl_sec": 60},
        {"max_uses": 1},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_FR14_admin_policy_each_field_makes_new_revision(conn, service, field_changes):
    _world(conn)
    service.set_policy("owner1", _settings("node1"))

    changed = service.set_policy("owner1", _settings("node1", **field_changes))

    assert (changed.version, changed.changed) == (2, True)
    assert len(_revisions(conn, "node1")) == 2


def test_FR14_admin_policy_rejection_reports_current_version(conn, service):
    _world(conn)
    service.set_policy("owner1", _settings("node1"))
    revisions_before = _count(conn, "policy_revision")

    # 현재 정책과 내용이 같아도 소유자가 아니면 거부한다. 현재 버전을 함께 돌려준다.
    by_visitor = service.set_policy("visitor1", _settings("node1"))
    assert by_visitor.result == AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.NOT_PLACE_OWNER)
    assert (by_visitor.version, by_visitor.changed) == (1, False)

    # 정책이 없는 기기는 버전이 None이다.
    no_policy = service.set_policy("visitor1", _settings("node2"))
    assert no_policy.result.reason == AdminWriteReason.NOT_PLACE_OWNER
    assert (no_policy.version, no_policy.changed) == (None, False)

    ghost = service.set_policy("owner1", _settings("ghost"))
    assert ghost.result == AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.NODE_NOT_FOUND)
    assert (ghost.version, ghost.changed) == (None, False)

    # 저장소가 거부해도 현재 버전을 돌려주고 changed는 False다.
    no_approver = service.set_policy("owner1", _settings("node2", approver_user_id="no-such-user"))
    assert no_approver.result == AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.ACCOUNT_NOT_FOUND)
    assert (no_approver.version, no_approver.changed) == (None, False)
    bad_approver = service.set_policy("owner1", _settings("node1", approver_user_id="no-such-user"))
    assert bad_approver.result.reason == AdminWriteReason.ACCOUNT_NOT_FOUND
    assert (bad_approver.version, bad_approver.changed) == (1, False)

    assert _count(conn, "policy_revision") == revisions_before


def test_FR14_admin_policy_conflict_keeps_current_version(conn):
    class StaleAdmin:
        """읽은 뒤 다른 쓰기가 먼저 정책을 바꾼 상황을 만든다. 정책 쓰기만 충돌로 돌려준다."""

        def __init__(self, inner):
            self._inner = inner
            self.policy_calls = []

        def set_policy(self, revision, expected_version):
            self.policy_calls.append((revision, expected_version))
            return AdminWriteResult(AdminWriteOutcome.CONFLICT, AdminWriteReason.POLICY_VERSION_MISMATCH)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    _world(conn)
    real_admin = SqliteAdminRepository(conn)
    repository = SqliteRepository(conn)
    DefaultAdminService(repository, real_admin).set_policy("owner1", _settings("node1"))
    stale = StaleAdmin(real_admin)

    result = DefaultAdminService(repository, stale).set_policy("owner1", _settings("node1", max_uses=2))

    assert result.result == AdminWriteResult(AdminWriteOutcome.CONFLICT, AdminWriteReason.POLICY_VERSION_MISMATCH)
    assert (result.version, result.changed) == (1, False)
    (revision, expected_version), = stale.policy_calls
    assert (revision.policy_id, revision.version, revision.node_id, revision.max_uses) == ("pol-node1", 2, "node1", 2)
    assert expected_version == 1
    assert _revisions(conn, "node1") == [("pol-node1", 1, None)]


# ── FR-01: 계정과 비콘 ────────────────────────────────────────────────


def test_FR01_admin_register_account_returns_injected_token_and_stores_hash(conn):
    service = DefaultAdminService(
        SqliteRepository(conn), SqliteAdminRepository(conn), new_token=lambda: "tok-owner1"
    )

    issue = service.register_account("owner1", "소유자", Role.OWNER)

    assert issue.result == APPLIED
    assert issue.token == "tok-owner1"
    row = conn.execute("SELECT name, role, token_hash FROM user WHERE user_id = 'owner1'").fetchone()
    assert tuple(row) == ("소유자", "owner", hash_token("tok-owner1"))
    stored = json.dumps([tuple(r) for table in _TABLES for r in conn.execute(f"SELECT * FROM {table}")])
    assert "tok-owner1" not in stored  # 원문은 DB와 감사 어디에도 없다
    assert "tok-owner1" not in repr(issue)  # 로그나 오류 출력으로도 새지 않는다


def test_FR01_admin_register_account_duplicate_has_no_token(conn):
    tokens = iter(["tok-first", "tok-second"])
    service = DefaultAdminService(SqliteRepository(conn), SqliteAdminRepository(conn), new_token=lambda: next(tokens))
    service.register_account("owner1", "소유자", Role.OWNER)
    before = _snapshot(conn)

    again = service.register_account("owner1", "소유자", Role.OWNER)

    assert again.result == AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.ACCOUNT_ALREADY_EXISTS)
    assert again.token is None
    assert _snapshot(conn) == before  # 기존 해시가 새 토큰으로 바뀌지 않는다
    row = conn.execute("SELECT token_hash FROM user WHERE user_id = 'owner1'").fetchone()
    assert row["token_hash"] == hash_token("tok-first")


def test_FR01_admin_default_token_is_32_byte_urlsafe(conn):
    token = default_token()
    assert len(token) == 43  # 32바이트를 base64url로 쓴 길이
    assert token != default_token()

    service = DefaultAdminService(SqliteRepository(conn), SqliteAdminRepository(conn))  # new_token을 주입하지 않는다
    issue = service.register_account("owner1", "소유자", Role.OWNER)

    assert issue.token is not None and len(issue.token) == 43
    row = conn.execute("SELECT token_hash FROM user WHERE user_id = 'owner1'").fetchone()
    assert row["token_hash"] == hash_token(issue.token)


def test_FR01_admin_register_beacon_passes_through(conn, service):
    _insert_user(conn, "owner1")
    _insert_user(conn, "owner2")
    key = BeaconKey("UUID-1", 1, 1)

    assert service.register_beacon(BeaconRegistration("b1", "owner1", key)) == APPLIED

    other = service.register_beacon(BeaconRegistration("b2", "owner2", key))
    assert other == AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.BEACON_ALREADY_REGISTERED)
    rows = conn.execute("SELECT beacon_id, user_id, is_active FROM beacon").fetchall()
    assert [tuple(row) for row in rows] == [("b1", "owner1", 1)]  # 기존 활성 비콘을 유지한다
