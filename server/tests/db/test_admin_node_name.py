"""`AdminRepository.set_node_name` (specs/core-dashboard-queries/spec.md 6절 "기기 이름", FR-12A, FR-22)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason, Capability, Occupancy, TrustState
from nearpilot.shared.interfaces import AdminRepository
from nearpilot.shared.models import NodeAnnounce

from conftest import insert_node, insert_place, insert_user


def _setup(conn):
    insert_user(conn, user_id="owner1", token_hash="hash-owner1", role="owner")
    insert_place(conn, owner_user_id="owner1")
    insert_node(conn, node_id="node1")


def _name(conn, node_id="node1"):
    return conn.execute("SELECT name FROM node WHERE node_id = ?", (node_id,)).fetchone()["name"]


def _node_without_name(conn, node_id="node1"):
    row = conn.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
    return {key: row[key] for key in row.keys() if key != "name"}


def _audit_rows(conn):
    return [(row["step"], json.loads(row["payload_json"])) for row in conn.execute("SELECT * FROM audit_log")]


def _announce(node_id, capability, occupancy):
    now = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
    return NodeAnnounce(node_id=node_id, capability=capability, occupancy=occupancy, received_at=now,
                        session_id="session-1", seq=1, sent_at=now)


def test_FR12A_set_node_name_이름_저장과_감사_한_행(admin, conn):
    _setup(conn)
    before = _node_without_name(conn)
    assert isinstance(admin, AdminRepository)
    assert _name(conn) is None  # 이름을 정하지 않은 기기는 NULL 이다

    result = admin.set_node_name("node1", "사물함 1")

    assert result.outcome == AdminWriteOutcome.APPLIED
    assert result.reason == AdminWriteReason.OK
    assert _name(conn) == "사물함 1"
    assert _node_without_name(conn) == before  # name 외에는 바뀌지 않는다
    assert _audit_rows(conn) == [
        ("admin", {"event": "set_node_name", "node_id": "node1", "name": "사물함 1"})
    ]

    # 다시 정하면 덮어쓰고 감사 행이 하나 더 는다
    assert admin.set_node_name("node1", "사물함 A").outcome == AdminWriteOutcome.APPLIED
    assert _name(conn) == "사물함 A"
    assert len(_audit_rows(conn)) == 2

    # 감사 INSERT가 실패하면 이름도 바뀌지 않는다 (한 트랜잭션).
    conn.execute(
        "CREATE TRIGGER audit_down BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'audit down'); END"
    )
    with pytest.raises(sqlite3.DatabaseError):
        admin.set_node_name("node1", "사물함 B")

    assert not conn.in_transaction
    assert _name(conn) == "사물함 A"
    assert len(_audit_rows(conn)) == 2


def test_FR13_set_node_name_없는_기기는_NODE_NOT_FOUND(admin, conn):
    _setup(conn)

    result = admin.set_node_name("no-such-node", "사물함 1")

    assert result.outcome == AdminWriteOutcome.REJECTED
    assert result.reason == AdminWriteReason.NODE_NOT_FOUND
    assert _audit_rows(conn) == []
    assert _name(conn) is None


@pytest.mark.parametrize("name", ["", " ", "   ", "\t\n"])
def test_FR12A_set_node_name_빈_이름은_ValueError(admin, conn, name):
    """`AdminWriteReason`에 입력 오류 값이 없어 빈 이름은 사유 값이 아니라 `ValueError`다."""
    _setup(conn)
    assert admin.set_node_name("node1", "사물함 1").outcome == AdminWriteOutcome.APPLIED

    with pytest.raises(ValueError):
        admin.set_node_name("node1", name)
    with pytest.raises(ValueError):  # 없는 기기여도 이름을 먼저 본다
        admin.set_node_name("no-such-node", name)

    assert _name(conn) == "사물함 1"
    assert len(_audit_rows(conn)) == 1  # 처음 저장한 한 행뿐이다
    assert not conn.in_transaction


def test_FR12A_기기_발표는_이름을_지우지_않는다(admin, repo, conn):
    _setup(conn)
    admin.set_node_name("node1", "사물함 1")

    # 같은 기능을 다시 발표해도, 기능이 바뀌어 신뢰가 되돌아가도 이름은 그대로다
    repo.upsert_announced_node(_announce("node1", Capability.LOCKER, Occupancy.RENTAL))
    assert _name(conn) == "사물함 1"
    repo.upsert_announced_node(_announce("node1", Capability.LIGHT, Occupancy.SHARED))
    row = conn.execute("SELECT name, capability, trust_state FROM node WHERE node_id = 'node1'").fetchone()
    assert (row["name"], row["capability"], row["trust_state"]) == (
        "사물함 1", Capability.LIGHT.value, TrustState.DISCOVERED.value)

    # 발표로 새로 생긴 기기는 이름이 없다
    repo.upsert_announced_node(_announce("node2", Capability.LOCKER, Occupancy.RENTAL))
    assert _name(conn, "node2") is None
