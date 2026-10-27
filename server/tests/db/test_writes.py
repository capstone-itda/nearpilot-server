"""쓰기 함수: link_request, save_decision, save_rssi, upsert_announced_node,
touch_node, set_node_session."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    Action, Capability, ExecutionKind, ExecutionStatus, Occupancy, ReasonCode, ReserveOutcome, StoreOutcome,
    Verdict,
)
from nearpilot.shared.models import (
    BeaconKey,
    Decision,
    DecisionRecord,
    NodeAnnounce,
    ReserveRequest,
    RssiFrame,
    TargetSpec,
    UseRequest,
)

from conftest import (
    insert_approval, insert_beacon, insert_node, insert_place, insert_policy, insert_request, insert_user,
)


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


def _fail_with_internal_error(repo, link, policy_version=7):
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.DENY, reason=ReasonCode.INTERNAL_ERROR, node_id="node1", p_max=0.5,
    )
    repo.save_decision(decision, DecisionRecord(settings=Settings(), policy_version=policy_version))


def test_FR18_link_request_retry_after_internal_error(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    _fail_with_internal_error(repo, link)

    retry = repo.link_request(_use_request(), "digest-1")

    assert retry.internal_request_id == link.internal_request_id
    assert retry.is_new is True
    assert retry.conflict is False
    assert retry.previous is None
    row = conn.execute(
        "SELECT verdict, reason_code, node_id, p_max, policy_ver FROM request WHERE internal_request_id = ?",
        (link.internal_request_id,),
    ).fetchone()
    assert tuple(row) == (None, None, None, None, None)


def test_NFR03_link_request_retry_concurrent_once(tmp_path):
    db_path = str(tmp_path / "retry.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn)
    insert_place(setup_conn)
    insert_node(setup_conn)
    setup_repo = SqliteRepository(setup_conn)
    link = setup_repo.link_request(_use_request(), "digest-1")
    _fail_with_internal_error(setup_repo, link)
    setup_conn.close()

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt():
        conn = connect(db_path)
        try:
            repo = SqliteRepository(conn)
            barrier.wait()
            result = repo.link_request(_use_request(), "digest-1")
            with lock:
                results.append(result)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r.is_new) == 1
    not_new = [r for r in results if not r.is_new]
    assert len(not_new) == 1
    assert not_new[0].conflict is False
    assert not_new[0].previous is None  # "처리 중"
    check_conn = connect(db_path)
    row = check_conn.execute(
        "SELECT verdict FROM request WHERE internal_request_id = ?", (link.internal_request_id,)
    ).fetchone()
    assert row["verdict"] is None
    check_conn.close()


def test_FR18_link_request_retry_different_input_conflicts(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    _fail_with_internal_error(repo, link)

    result = repo.link_request(_use_request(), "digest-2")

    assert result.conflict is True
    assert result.is_new is False
    row = conn.execute(
        "SELECT verdict, reason_code FROM request WHERE internal_request_id = ?", (link.internal_request_id,)
    ).fetchone()
    assert tuple(row) == ("DENY", "INTERNAL_ERROR")  # 비우지 않았다


def test_FR18_link_request_retry_confirmed_decision_unaffected(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.DENY, reason=ReasonCode.POLICY_DENIED, node_id="node1",
    )
    repo.save_decision(decision, DecisionRecord(settings=Settings()))

    retry = repo.link_request(_use_request(), "digest-1")

    assert retry.is_new is False
    assert retry.previous is not None
    assert retry.previous.reason == ReasonCode.POLICY_DENIED
    row = conn.execute(
        "SELECT verdict FROM request WHERE internal_request_id = ?", (link.internal_request_id,)
    ).fetchone()
    assert row["verdict"] == "DENY"


def test_FR18_link_request_retry_again_after_second_internal_error(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    _fail_with_internal_error(repo, link, policy_version=1)

    first_retry = repo.link_request(_use_request(), "digest-1")
    assert first_retry.is_new is True
    _fail_with_internal_error(repo, first_retry, policy_version=2)

    second_retry = repo.link_request(_use_request(), "digest-1")

    assert second_retry.internal_request_id == link.internal_request_id
    assert second_retry.is_new is True
    row = conn.execute(
        "SELECT verdict FROM request WHERE internal_request_id = ?", (link.internal_request_id,)
    ).fetchone()
    assert row["verdict"] is None


def test_FR22_link_request_retry_audits_previous_decision(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    _fail_with_internal_error(repo, link, policy_version=7)

    repo.link_request(_use_request(), "digest-1")

    row = conn.execute(
        "SELECT step, payload_json FROM audit_log WHERE internal_request_id = ?", (link.internal_request_id,)
    ).fetchone()
    assert row["step"] == "0"
    payload = json.loads(row["payload_json"])
    assert payload["previous_verdict"] == "DENY"
    assert payload["previous_reason_code"] == "INTERNAL_ERROR"
    assert payload["previous_node_id"] == "node1"
    assert payload["previous_policy_ver"] == 7


def test_FR19_link_request_retry_keeps_single_reservation(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    link = repo.link_request(_use_request(), "digest-1")
    reserved = repo.reserve_if_free(
        ReserveRequest(
            internal_request_id=link.internal_request_id, user_id="u1", node_id="node1",
            action=Action.OPEN, approval_id=None,
        )
    )
    assert reserved.outcome == ReserveOutcome.RESERVED
    _fail_with_internal_error(repo, link)

    retry = repo.link_request(_use_request(), "digest-1")
    assert retry.is_new is True
    retry_reserved = repo.reserve_if_free(
        ReserveRequest(
            internal_request_id=retry.internal_request_id, user_id="u1", node_id="node1",
            action=Action.OPEN, approval_id=None,
        )
    )

    assert retry_reserved.outcome == ReserveOutcome.RESERVED
    assert retry_reserved.use_id == reserved.use_id
    assert conn.execute("SELECT COUNT(*) AS c FROM use_session").fetchone()["c"] == 1


def _fail_without_node(repo, link):
    """파이프라인의 오류 거부처럼 대상 기기를 비우고 저장한다."""
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.DENY, reason=ReasonCode.INTERNAL_ERROR,
    )
    repo.save_decision(decision, DecisionRecord(settings=Settings()))


def _cancel_after_error(store, internal_request_id):
    store.cancel_not_sent(
        internal_request_id, ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR, {"source": "decision_error"}
    )


@pytest.mark.parametrize(
    ("occupancy", "node_id", "capability", "action"),
    [("rental", "node1", "locker", Action.OPEN), ("shared", "light1", "light", Action.ON)],
)
def test_FR18_retry_after_cancelled_reservation_links_new_reservation(
    conn, repo, store, occupancy, node_id, capability, action
):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id=node_id, capability=capability, occupancy=occupancy)
    insert_policy(conn, node_id=node_id, approval_mode="one_time", max_uses=1)
    insert_approval(conn, approval_id="appr1", node_id=node_id, internal_request_id="approval-req")
    link = repo.link_request(_use_request(capability=capability, action=action), "digest-1")
    reserve = ReserveRequest(
        internal_request_id=link.internal_request_id, user_id="u1", node_id=node_id, action=action,
        approval_id="appr1",
    )
    first = repo.reserve_if_free(reserve)
    _fail_without_node(repo, link)
    _cancel_after_error(store, link.internal_request_id)

    retry = repo.link_request(_use_request(capability=capability, action=action), "digest-1")
    again = repo.reserve_if_free(reserve)
    linked = store.link_command(retry.internal_request_id, node_id, action)

    assert retry.is_new is True
    assert retry.internal_request_id == link.internal_request_id
    assert again.outcome == ReserveOutcome.RESERVED
    assert again.usage_id not in (None, first.usage_id)
    if occupancy == "rental":
        assert again.use_id not in (None, first.use_id)
    assert linked.outcome == StoreOutcome.APPLIED
    assert linked.command.execution_kind == (
        ExecutionKind.RENTAL_INITIAL if occupancy == "rental" else ExecutionKind.SHARED
    )
    assert linked.command.use_id == again.use_id
    assert linked.command.usage_id == again.usage_id
    # 취소 전 행은 그대로 두고, 같은 요청의 살아 있는 예약은 하나다.
    usages = {r["usage_id"]: r["state"] for r in conn.execute("SELECT usage_id, state FROM approval_usage")}
    assert usages == {first.usage_id: "released", again.usage_id: "reserved"}
    if occupancy == "rental":
        sessions = {r["use_id"]: (r["state"], r["end_reason"]) for r in conn.execute("SELECT * FROM use_session")}
        assert sessions == {first.use_id: ("CLOSED", "cancelled"), again.use_id: ("RESERVED", None)}


def test_FR22_link_request_previous_prefers_live_session_after_retry(conn, repo, store):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    link = repo.link_request(_use_request(), "digest-1")
    reserve = ReserveRequest(
        internal_request_id=link.internal_request_id, user_id="u1", node_id="node1", action=Action.OPEN,
        approval_id=None,
    )
    repo.reserve_if_free(reserve)
    _fail_without_node(repo, link)
    _cancel_after_error(store, link.internal_request_id)
    repo.link_request(_use_request(), "digest-1")
    again = repo.reserve_if_free(reserve)
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1", verdict=Verdict.OK,
        reason=ReasonCode.OK, node_id="node1", use_id=again.use_id,
    )
    repo.save_decision(decision, DecisionRecord(settings=Settings()))

    resent = repo.link_request(_use_request(), "digest-1")

    assert resent.is_new is False
    assert resent.previous is not None
    assert resent.previous.use_id == again.use_id


def test_FR22_link_request_previous_of_rejected_retry_has_no_use_id(conn, repo, store):
    """재판정이 OK 가 아닌 판정으로 끝나면 취소된 옛 세션의 use_id 를 담지 않는다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    link = repo.link_request(_use_request(), "digest-1")
    repo.reserve_if_free(ReserveRequest(
        internal_request_id=link.internal_request_id, user_id="u1", node_id="node1", action=Action.OPEN,
        approval_id=None,
    ))
    _fail_without_node(repo, link)
    _cancel_after_error(store, link.internal_request_id)
    repo.link_request(_use_request(), "digest-1")
    busy = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1", verdict=Verdict.BUSY,
        reason=ReasonCode.DEVICE_BUSY, node_id="node1",
    )
    repo.save_decision(busy, DecisionRecord(settings=Settings()))

    resent = repo.link_request(_use_request(), "digest-1")

    assert resent.previous is not None
    assert (resent.previous.verdict, resent.previous.use_id) == (Verdict.BUSY, None)


def test_FR22_link_request_previous_keeps_cancelled_only_session(conn, repo, store):
    """세션이 하나뿐이면 취소됐어도 지금처럼 그 use_id를 담는다. 살아 있는 세션이 있을 때만 그쪽을 고른다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental")
    link = repo.link_request(_use_request(), "digest-1")
    reserved = repo.reserve_if_free(ReserveRequest(
        internal_request_id=link.internal_request_id, user_id="u1", node_id="node1", action=Action.OPEN,
        approval_id=None,
    ))
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1", verdict=Verdict.OK,
        reason=ReasonCode.OK, node_id="node1", use_id=reserved.use_id,
    )
    repo.save_decision(decision, DecisionRecord(settings=Settings()))
    _cancel_after_error(store, link.internal_request_id)

    resent = repo.link_request(_use_request(), "digest-1")

    assert resent.previous is not None
    assert resent.previous.use_id == reserved.use_id


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


def test_FR22_save_decision_stores_settings_snapshot(conn, repo):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    link = repo.link_request(_use_request(), "digest-1")
    decision = Decision(
        internal_request_id=link.internal_request_id, request_id="ext-1",
        verdict=Verdict.OK, reason=ReasonCode.OK, node_id="node1",
    )
    settings = Settings(version="42", threshold_locker=0.8)
    record = DecisionRecord(settings=settings)

    repo.save_decision(decision, record)

    row = conn.execute(
        "SELECT settings_json FROM request WHERE internal_request_id = ?", (link.internal_request_id,)
    ).fetchone()
    assert row["settings_json"] is not None
    stored = json.loads(row["settings_json"])
    assert stored["version"] == "42"
    assert stored["threshold_locker"] == 0.8


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
