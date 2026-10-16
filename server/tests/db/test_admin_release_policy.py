"""`AdminRepository.set_release_policy` (specs/core-ops-cli/spec.md §6, FR-13, FR-22)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason

from conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user

LOCKER_RETURN = {"check_criteria": {"id": "locker-return", "version": 1}}
LOCKER_RETURN_V2 = {"check_criteria": {"id": "locker-return", "version": 2}}


def _setup(conn):
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    insert_place(conn, owner_user_id="owner1")
    insert_node(conn, node_id="node1")


def _release_policy(conn, node_id="node1"):
    row = conn.execute("SELECT release_policy FROM node WHERE node_id = ?", (node_id,)).fetchone()
    return json.loads(row["release_policy"])


def _node_without_release_policy(conn, node_id="node1"):
    row = conn.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
    return {key: row[key] for key in row.keys() if key != "release_policy"}


def _audit_rows(conn):
    return [(row["step"], json.loads(row["payload_json"])) for row in conn.execute("SELECT * FROM audit_log")]


def test_FR22_set_release_policy_audited(admin, conn):
    _setup(conn)
    before = _node_without_release_policy(conn)

    result = admin.set_release_policy("node1", LOCKER_RETURN)

    assert result.outcome == AdminWriteOutcome.APPLIED
    assert result.reason == AdminWriteReason.OK
    assert _release_policy(conn) == LOCKER_RETURN
    assert _node_without_release_policy(conn) == before  # release_policy 외에는 바뀌지 않는다
    assert _audit_rows(conn) == [
        ("admin", {"event": "set_release_policy", "node_id": "node1", "release_policy": LOCKER_RETURN})
    ]

    # 감사 INSERT가 실패하면 node 값도 바뀌지 않는다 (한 트랜잭션).
    conn.execute(
        "CREATE TRIGGER audit_down BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'audit down'); END"
    )
    with pytest.raises(sqlite3.DatabaseError):
        admin.set_release_policy("node1", LOCKER_RETURN_V2)

    assert not conn.in_transaction
    assert _release_policy(conn) == LOCKER_RETURN
    assert len(_audit_rows(conn)) == 1


def test_FR22_set_release_policy_keeps_session_value(admin, conn):
    _setup(conn)
    insert_request(conn, internal_request_id="req1", user_id="owner1", node_id="node1")
    insert_use_session(
        conn, use_id="use1", internal_request_id="req1", user_id="owner1", node_id="node1", state="ACTIVE"
    )
    conn.execute("UPDATE use_session SET release_policy = ? WHERE use_id = 'use1'", (json.dumps(LOCKER_RETURN),))

    admin.set_release_policy("node1", LOCKER_RETURN_V2)

    session_row = conn.execute("SELECT state, release_policy FROM use_session WHERE use_id = 'use1'").fetchone()
    assert session_row["state"] == "ACTIVE"
    assert json.loads(session_row["release_policy"]) == LOCKER_RETURN  # 생성 당시 값을 소급하지 않는다
    assert _release_policy(conn) == LOCKER_RETURN_V2


def test_FR13_set_release_policy_unknown_node(admin, conn):
    _setup(conn)

    result = admin.set_release_policy("no-such-node", LOCKER_RETURN)

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.NODE_NOT_FOUND
    assert _audit_rows(conn) == []
    assert _release_policy(conn) == {}

    # Mapping이 아닌 값은 빈 종료 정책으로 조용히 바뀌지 않고 TypeError다.
    for value in (None, "", "{}", [], ["check_criteria"], 1):
        with pytest.raises(TypeError):
            admin.set_release_policy("node1", value)
    assert _audit_rows(conn) == []
    assert _release_policy(conn) == {}
