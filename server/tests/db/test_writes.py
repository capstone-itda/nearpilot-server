"""쓰기 함수: link_request, save_decision, save_rssi, upsert_announced_node,
touch_node, set_node_session."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, Capability, Occupancy, ReasonCode, StoreOutcome, Verdict
from nearpilot.shared.models import (
    BeaconKey,
    Decision,
    DecisionRecord,
    NodeAnnounce,
    RssiFrame,
    TargetSpec,
    UseRequest,
)

from conftest import insert_beacon, insert_node, insert_place, insert_request, insert_user


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _use_request(user_id="u1", request_id="ext-1", kind="nearest", capability="locker", action=Action.OPEN):
    return UseRequest(
        user_id=user_id,
        request_id=request_id,
        target=TargetSpec.parse(f"{kind}:{capability}"),
        action=action,
        host_kind=None,
        received_at=NOW,
    )


def test_FR22_link_request_resend(conn, repo):
    insert_user(conn)
    req = _use_request()

    first = repo.link_request(req, "digest-1")
    second = repo.link_request(req, "digest-1")

    assert first.is_new is True
    assert second.is_new is False
    assert second.conflict is False
    assert second.internal_request_id == first.internal_request_id
    assert second.previous is None


def test_FR22_link_request_conflict(conn, repo):
    insert_user(conn)
    req = _use_request()

    first = repo.link_request(req, "digest-1")
    second = repo.link_request(req, "digest-2")

    assert second.conflict is True
    assert second.internal_request_id == first.internal_request_id
    row = conn.execute(
        "SELECT input_digest FROM request WHERE internal_request_id = ?", (first.internal_request_id,)
    ).fetchone()
    assert row["input_digest"] == "digest-1"


def test_FR22_link_request_other_user(conn, repo):
    insert_user(conn, user_id="u1", token_hash="hash-1")
    insert_user(conn, user_id="u2", token_hash="hash-2")

    first = repo.link_request(_use_request(user_id="u1"), "digest-1")
    second = repo.link_request(_use_request(user_id="u2"), "digest-1")

    assert second.is_new is True
    assert second.internal_request_id != first.internal_request_id


def test_FR22_link_request_after_clear(conn, repo):
    insert_user(conn)
    insert_request(conn, internal_request_id="old-req", user_id="u1", request_id=None, input_digest="old")

    result = repo.link_request(_use_request(), "digest-1")

    assert result.is_new is True
    assert result.internal_request_id != "old-req"
    old = conn.execute("SELECT request_id FROM request WHERE internal_request_id = 'old-req'").fetchone()
    assert old["request_id"] is None


def test_FR22_link_request_returns_previous_decision(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    decision = Decision(
        internal_request_id=link.internal_request_id,
        request_id="ext-1",
        verdict=Verdict.OK,
        reason=ReasonCode.OK,
        node_id="node1",
    )
    record = DecisionRecord(settings=Settings())
    repo.save_decision(decision, record)

    again = repo.link_request(_use_request(), "digest-1")

    assert again.previous is not None
    assert again.previous.verdict == Verdict.OK
    assert again.previous.node_id == "node1"


def test_FR22_save_decision_already_applied(conn, repo):
    insert_user(conn)
    link = repo.link_request(_use_request(), "digest-1")
    decision = Decision(
        internal_request_id=link.internal_request_id,
        request_id="ext-1",
        verdict=Verdict.DENY,
        reason=ReasonCode.POLICY_DENIED,
    )
    record = DecisionRecord(settings=Settings())

    first = repo.save_decision(decision, record)
    second = repo.save_decision(decision, record)

    assert first.outcome == StoreOutcome.APPLIED
    assert second.outcome == StoreOutcome.ALREADY_APPLIED


def test_FR22_save_decision_conflict(conn, repo):
    insert_user(conn)
    link = repo.link_request(_use_request(), "digest-1")
    record = DecisionRecord(settings=Settings())
    first = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.DENY, reason=ReasonCode.POLICY_DENIED,
    )
    repo.save_decision(first, record)

    second = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.OK, reason=ReasonCode.OK,
    )
    result = repo.save_decision(second, record)

    assert result.outcome == StoreOutcome.CONFLICT
    row = conn.execute(
        "SELECT verdict FROM request WHERE internal_request_id = ?", (link.internal_request_id,)
    ).fetchone()
    assert row["verdict"] == "DENY"


def _frame(node_id="node1", beacon=None, session_id="s1", seq=1, received_at=NOW):
    beacon = beacon or BeaconKey("UUID-1", 1, 1)
    return RssiFrame(
        node_id=node_id, beacon=beacon, rssi=-60.0, frame_ts=received_at,
        session_id=session_id, seq=seq, sent_at=received_at - timedelta(milliseconds=50),
        received_at=received_at,
    )


def test_FR05_save_rssi_unknown_beacon(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)

    repo.save_rssi(_frame())

    assert conn.execute("SELECT COUNT(*) AS c FROM rssi_observation").fetchone()["c"] == 0


def test_FR05_save_rssi_inactive_beacon(conn, repo):
    insert_user(conn)
    insert_beacon(conn, is_active=0)
    insert_place(conn)
    insert_node(conn)

    repo.save_rssi(_frame())

    assert conn.execute("SELECT COUNT(*) AS c FROM rssi_observation").fetchone()["c"] == 0


def test_FR05_save_rssi_unknown_node(conn, repo):
    insert_user(conn)
    insert_beacon(conn)

    repo.save_rssi(_frame(node_id="missing-node"))

    assert conn.execute("SELECT COUNT(*) AS c FROM rssi_observation").fetchone()["c"] == 0


def test_FR05_save_rssi_duplicate(conn, repo):
    insert_user(conn)
    insert_beacon(conn)
    insert_place(conn)
    insert_node(conn)

    repo.save_rssi(_frame())
    repo.save_rssi(_frame())

    assert conn.execute("SELECT COUNT(*) AS c FROM rssi_observation").fetchone()["c"] == 1


def test_FR05_save_rssi_collection_run(conn, repo):
    insert_user(conn)
    insert_beacon(conn, beacon_id="b1")
    insert_place(conn)
    insert_node(conn)
    conn.execute(
        "INSERT INTO collection_run (collection_id, beacon_id, label, started_at, ended_at) "
        "VALUES ('cr1', 'b1', '1-front', '2025-12-31T00:00:00+00:00', '2026-12-31T00:00:00+00:00')"
    )

    repo.save_rssi(_frame())

    row = conn.execute("SELECT collection_id FROM rssi_observation").fetchone()
    assert row["collection_id"] == "cr1"


def test_FR05_save_rssi_collection_run_end_exclusive(conn, repo):
    insert_user(conn)
    insert_beacon(conn, beacon_id="b1")
    insert_place(conn)
    insert_node(conn)
    conn.execute(
        "INSERT INTO collection_run (collection_id, beacon_id, label, started_at, ended_at) "
        "VALUES ('cr1', 'b1', 'none', '2025-12-31T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )

    repo.save_rssi(_frame(received_at=NOW))

    row = conn.execute("SELECT collection_id FROM rssi_observation").fetchone()
    assert row["collection_id"] is None


def test_FR05_save_rssi_trusts_regardless_of_node_trust(conn, repo):
    insert_user(conn)
    insert_beacon(conn)
    insert_place(conn)
    insert_node(conn, trust_state="discovered")

    repo.save_rssi(_frame())

    assert conn.execute("SELECT COUNT(*) AS c FROM rssi_observation").fetchone()["c"] == 1


def test_FR13_touch_node_older(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, last_seen_at="2026-06-01T00:00:00+00:00")

    repo.touch_node("node1", datetime(2026, 1, 1, tzinfo=timezone.utc))

    row = conn.execute("SELECT last_seen_at FROM node WHERE node_id = 'node1'").fetchone()
    assert row["last_seen_at"] == "2026-06-01T00:00:00+00:00"


def test_FR13_touch_node_newer(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, last_seen_at="2026-01-01T00:00:00+00:00")

    repo.touch_node("node1", datetime(2026, 6, 1, tzinfo=timezone.utc))

    row = conn.execute("SELECT last_seen_at FROM node WHERE node_id = 'node1'").fetchone()
    assert row["last_seen_at"] == "2026-06-01T00:00:00+00:00"


def test_FR13_announce_changed(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, trust_state="trusted", capability="locker", occupancy="rental")

    announce = NodeAnnounce(
        node_id="node1", capability=Capability.LOCKER, occupancy=Occupancy.SHARED,
        received_at=NOW, session_id="s1", seq=1, sent_at=NOW,
    )
    repo.upsert_announced_node(announce)

    row = conn.execute("SELECT trust_state, occupancy FROM node WHERE node_id = 'node1'").fetchone()
    assert row["trust_state"] == "discovered"
    assert row["occupancy"] == "shared"
    audit = conn.execute("SELECT step FROM audit_log").fetchone()
    assert audit["step"] == "announce"


def test_FR13_announce_unchanged_keeps_trust(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, trust_state="trusted", capability="locker", occupancy="rental")

    announce = NodeAnnounce(
        node_id="node1", capability=Capability.LOCKER, occupancy=Occupancy.RENTAL,
        received_at=NOW, session_id="s1", seq=1, sent_at=NOW,
    )
    repo.upsert_announced_node(announce)

    row = conn.execute("SELECT trust_state FROM node WHERE node_id = 'node1'").fetchone()
    assert row["trust_state"] == "trusted"
    assert conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"] == 0


def test_FR04_announce_new_node_is_discovered(conn, repo):
    announce = NodeAnnounce(
        node_id="brand-new", capability=Capability.LIGHT, occupancy=Occupancy.SHARED,
        received_at=NOW, session_id="s1", seq=1, sent_at=NOW,
    )

    repo.upsert_announced_node(announce)

    row = conn.execute("SELECT trust_state, device_state FROM node WHERE node_id = 'brand-new'").fetchone()
    assert row["trust_state"] == "discovered"
    assert row["device_state"] == "AVAILABLE"


def test_FR04_set_node_session_keeps_first_seen_for_same_session(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)

    repo.set_node_session("node1", "sess-1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    repo.set_node_session("node1", "sess-1", datetime(2026, 6, 1, tzinfo=timezone.utc))

    row = conn.execute("SELECT session_first_seen_at FROM node WHERE node_id = 'node1'").fetchone()
    assert row["session_first_seen_at"] == "2026-01-01T00:00:00+00:00"


def test_FR04_set_node_session_new_session_updates_first_seen(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)

    repo.set_node_session("node1", "sess-1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    repo.set_node_session("node1", "sess-2", datetime(2026, 6, 1, tzinfo=timezone.utc))

    row = conn.execute(
        "SELECT session_id, session_first_seen_at FROM node WHERE node_id = 'node1'"
    ).fetchone()
    assert row["session_id"] == "sess-2"
    assert row["session_first_seen_at"] == "2026-06-01T00:00:00+00:00"
