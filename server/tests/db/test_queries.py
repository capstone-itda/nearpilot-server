"""조회 함수: beacons_of, rssi_since, nodes, current_policy, valid_approval,
session, session_owner_matches, has_open_session, account_by_token_hash."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nearpilot.shared.enums import Action, Capability
from nearpilot.shared.models import BeaconKey

from conftest import (
    insert_approval,
    insert_beacon,
    insert_node,
    insert_place,
    insert_policy,
    insert_request,
    insert_use_session,
    insert_user,
)


def test_NFR08_account_by_token_hash(conn, repo):
    insert_user(conn, user_id="u1", token_hash="hash-1", role="owner")

    account = repo.account_by_token_hash("hash-1")

    assert account.user_id == "u1"
    assert account.role.value == "owner"
    assert repo.account_by_token_hash("missing") is None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(user)").fetchall()}
    assert "token" not in columns and "token_hash" in columns


def test_FR15_beacons_of_active_only(conn, repo):
    insert_user(conn)
    insert_beacon(conn, beacon_id="b1", uuid="UUID-1", is_active=1)
    insert_beacon(conn, beacon_id="b2", uuid="UUID-2", is_active=0)

    keys = repo.beacons_of("u1")

    assert keys == [BeaconKey("UUID-1", 1, 1)]


def test_FR05_rssi_since(conn, repo):
    insert_user(conn)
    insert_beacon(conn, beacon_id="b1", uuid="UUID-1")
    insert_place(conn)
    insert_node(conn)
    conn.execute(
        "INSERT INTO rssi_observation (obs_id, node_id, beacon_id, rssi, frame_ts, received_at, "
        "sent_at, session_id, seq) VALUES ('o1', 'node1', 'b1', -60, "
        "'2026-01-01T00:00:01+00:00', '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:00+00:00', 's1', 1)"
    )
    conn.execute(
        "INSERT INTO rssi_observation (obs_id, node_id, beacon_id, rssi, frame_ts, received_at, "
        "sent_at, session_id, seq) VALUES ('o2', 'node1', 'b1', -58, "
        "'2025-01-01T00:00:01+00:00', '2025-01-01T00:00:01+00:00', '2025-01-01T00:00:00+00:00', 's1', 2)"
    )

    since = datetime(2025, 6, 1, tzinfo=timezone.utc)
    frames = repo.rssi_since([BeaconKey("UUID-1", 1, 1)], since)

    assert len(frames) == 1
    assert frames[0].seq == 1
    assert frames[0].beacon == BeaconKey("UUID-1", 1, 1)


def test_FR13_nodes_returns_all_regardless_of_trust(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="n1", trust_state="discovered", device_state="FAULT")
    insert_node(conn, node_id="n2", capability="light", occupancy="shared")

    all_nodes = repo.nodes()
    lockers = repo.nodes(Capability.LOCKER)

    assert {n.node_id for n in all_nodes} == {"n1", "n2"}
    assert [n.node_id for n in lockers] == ["n1"]


def test_FR14_current_policy_missing(conn, repo):
    with pytest.raises(LookupError):
        repo.current_policy("missing-node")


def test_FR14_current_policy_found(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_policy(conn, max_uses=5)

    policy = repo.current_policy("node1")

    assert policy.policy_id == "policy1"
    assert policy.version == 1
    assert policy.max_uses == 5


def test_FR14_valid_approval_scope(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_policy(conn)
    insert_approval(conn, approval_id="a1", action="open", policy_version=1)

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    assert repo.valid_approval("u1", "node1", Action.OPEN, 1, now) is not None
    # 동작이 다르다
    assert repo.valid_approval("u1", "node1", Action.ON, 1, now) is None
    # 정책 버전이 다르다
    assert repo.valid_approval("u1", "node1", Action.OPEN, 2, now) is None
    # 요청자가 다르다
    assert repo.valid_approval("other", "node1", Action.OPEN, 1, now) is None


def test_FR14_valid_approval_expired(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_policy(conn)
    insert_approval(conn, approval_id="a1", valid_until="2020-01-01T00:00:00+00:00")

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    assert repo.valid_approval("u1", "node1", Action.OPEN, 1, now) is None


def test_FR14_valid_approval_latest_valid_until(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_policy(conn)
    insert_approval(
        conn, approval_id="a1", internal_request_id="r1",
        valid_until="2030-01-01T00:00:00+00:00",
    )
    insert_approval(
        conn, approval_id="a2", internal_request_id="r2",
        valid_until="2040-01-01T00:00:00+00:00",
    )

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    approval = repo.valid_approval("u1", "node1", Action.OPEN, 1, now)

    assert approval.approval_id == "a2"


def test_FR21_owner_matches(conn, repo):
    insert_user(conn, user_id="u1")
    insert_user(conn, user_id="u2", token_hash="hash-2")
    insert_place(conn)
    insert_node(conn)
    insert_request(conn, internal_request_id="req1", user_id="u1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", user_id="u1")

    assert repo.session_owner_matches("use1", "u1") is True
    assert repo.session_owner_matches("use1", "u2") is False
    assert repo.session_owner_matches("missing-use", "u1") is False


def test_FR21_session_lookup(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE")

    session = repo.session("use1")

    assert session.use_id == "use1"
    assert session.state.value == "ACTIVE"
    assert repo.session("missing") is None


def test_FR09_has_open_session(conn, repo):
    insert_user(conn, user_id="u1")
    insert_user(conn, user_id="u2", token_hash="hash-2")
    insert_place(conn)
    for state in ("RESERVED", "ACTIVE", "CLOSING", "CLOSED"):
        insert_node(conn, node_id=f"node-{state}")
        insert_request(conn, internal_request_id=f"req-{state}", request_id=f"ext-{state}")
        insert_use_session(
            conn, use_id=f"use-{state}", internal_request_id=f"req-{state}",
            node_id=f"node-{state}", state=state,
        )
    insert_node(conn, node_id="node-empty")

    # 열린 세 상태는 True, CLOSED만 있으면 False다
    assert repo.has_open_session("u1", "node-RESERVED") is True
    assert repo.has_open_session("u1", "node-ACTIVE") is True
    assert repo.has_open_session("u1", "node-CLOSING") is True
    assert repo.has_open_session("u1", "node-CLOSED") is False
    # 다른 계정의 세션, 세션이 없는 기기, 없는 기기는 본인의 열린 세션이 아니다
    assert repo.has_open_session("u2", "node-ACTIVE") is False
    assert repo.has_open_session("u1", "node-empty") is False
    assert repo.has_open_session("u1", "missing-node") is False

    # 종료된 세션이 있어도 같은 기기에 새 세션이 열리면 True다
    insert_request(conn, internal_request_id="req-again", request_id="ext-again")
    insert_use_session(
        conn, use_id="use-again", internal_request_id="req-again", node_id="node-CLOSED", state="ACTIVE",
    )
    assert repo.has_open_session("u1", "node-CLOSED") is True
