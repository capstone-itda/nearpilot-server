"""`Repository.is_place_owner` (specs/db-contract-followups/spec.md, #84)."""

from __future__ import annotations

from conftest import insert_node, insert_place, insert_user


def test_FR12A_is_place_owner_true(repo, conn):
    insert_user(conn, user_id="owner1")
    insert_place(conn, place_id="place1", owner_user_id="owner1")
    insert_node(conn, node_id="node1", place_id="place1")

    assert repo.is_place_owner("owner1", "node1") is True


def test_FR12A_is_place_owner_false(repo, conn):
    insert_user(conn, user_id="owner1")
    insert_user(conn, user_id="other1", token_hash="hash-other1")
    insert_place(conn, place_id="place1", owner_user_id="owner1")
    insert_node(conn, node_id="node1", place_id="place1")

    assert repo.is_place_owner("other1", "node1") is False


def test_FR12A_is_place_owner_unplaced(repo, conn):
    insert_user(conn, user_id="owner1")
    insert_node(conn, node_id="node1", place_id=None)

    assert repo.is_place_owner("owner1", "node1") is False


def test_FR12A_is_place_owner_missing_node(repo, conn):
    insert_user(conn, user_id="owner1")

    assert repo.is_place_owner("owner1", "no-such-node") is False
