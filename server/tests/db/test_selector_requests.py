"""거리 선택자의 하위 요청 행, 판정 기록, 복원 (specs/core-distance-selector/spec.md, FR-09, FR-22, #253)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import nearpilot.db.schema as schema_module
from nearpilot.db.repositories._common import iso
from nearpilot.db.repositories.dashboard_reads import SqliteDashboardReads
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import ExecutionStatus, ReasonCode, StoreOutcome, Verdict
from nearpilot.shared.models import (
    Decision, DecisionRecord, ReserveRequest, SelectorChild, SelectorRecord, TargetSpec, UseRequest,
)

from conftest import insert_node, insert_place, insert_request, insert_user


NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
SELECTOR = "near:light:1:2"


def _request(request_id="r-sel", parent_internal_request_id=None, target=SELECTOR):
    return UseRequest(
        user_id="u1", request_id=request_id, target=TargetSpec.parse(target), action="on",
        host_kind="claude", received_at=NOW, parent_internal_request_id=parent_internal_request_id,
    )


def _seed(conn):
    """계정, 장소, 공용형 조명 세 대."""
    insert_user(conn)
    insert_place(conn)
    for node_id in ("light-1", "light-2", "light-3"):
        insert_node(conn, node_id=node_id, capability="light", occupancy="shared")


def _save(repo, decision, selector=None):
    result = repo.save_decision(decision, DecisionRecord(settings=Settings(), selector=selector))
    assert result.outcome is StoreOutcome.APPLIED


def _one_device_record(node_id, children=()):
    return SelectorRecord(SELECTOR, (node_id,), {node_id: None}, (node_id,), False, children)


def _decision_context(conn, internal_request_id):
    row = conn.execute(
        "SELECT decision_context FROM request WHERE internal_request_id = ?", (internal_request_id,)
    ).fetchone()
    return row["decision_context"]


def test_FR22_selector_child_rows_link_parent(conn, repo):
    _seed(conn)
    insert_request(conn, internal_request_id="origin", request_id="r-origin")
    req = _request(parent_internal_request_id="origin")
    parent = repo.link_request(req, "digest-sel").internal_request_id

    first = repo.link_selector_child(parent, req, "light-2", "digest-sel")
    second = repo.link_selector_child(parent, req, "light-1", "digest-sel")

    assert len({parent, first, second}) == 3
    rows = conn.execute(
        "SELECT * FROM request WHERE selector_parent_id = ? ORDER BY rowid", (parent,)
    ).fetchall()
    assert [row["internal_request_id"] for row in rows] == [first, second]
    for row, node_id in zip(rows, ("light-2", "light-1")):
        assert row["request_id"] is None
        assert (row["user_id"], row["request_kind"], row["node_id"]) == ("u1", "visitor", node_id)
        assert (row["target_spec"], row["action"], row["host_kind"]) == (SELECTOR, "on", "claude")
        assert row["received_at"] == iso(NOW)
        # 재질문 연결(parent_internal_request_id)과 상위 요청 연결(selector_parent_id)은 다른 열이다
        assert (row["input_digest"], row["parent_internal_request_id"]) == ("digest-sel", "origin")
        assert row["verdict"] is None
    parent_row = conn.execute("SELECT * FROM request WHERE internal_request_id = ?", (parent,)).fetchone()
    assert (parent_row["request_id"], parent_row["selector_parent_id"]) == ("r-sel", None)

    # 하위 행은 같은 (계정, 요청 ID) 의 재전송에 끼지 않는다. 상위 행만 찾는다.
    again = repo.link_request(req, "digest-sel")
    assert (again.internal_request_id, again.is_new, again.conflict) == (parent, False, False)
    other = repo.link_request(req, "digest-other")
    assert (other.internal_request_id, other.conflict) == (parent, True)
    # 하위 행은 중복 방지 항목 수에 들지 않는다. 항목은 origin 과 상위 요청 둘뿐이라 한도 3 아래다.
    limited = SqliteRepository(conn, settings=Settings(dedup_entry_limit=3))
    assert limited.link_request(_request(request_id="r-next"), "digest-next").capacity_exceeded is False

    before = conn.execute("SELECT COUNT(*) AS n FROM request").fetchone()["n"]
    with pytest.raises(LookupError):
        repo.link_selector_child("no-such-parent", req, "light-1", "digest-sel")
    assert conn.execute("SELECT COUNT(*) AS n FROM request").fetchone()["n"] == before


def test_FR22_selector_record_round_trip(conn, repo):
    _seed(conn)
    req = _request()
    parent = repo.link_request(req, "digest-sel").internal_request_id
    first = repo.link_selector_child(parent, req, "light-2", "digest-sel")
    second = repo.link_selector_child(parent, req, "light-1", "digest-sel")
    _save(repo, Decision(first, "r-sel", Verdict.OK, ReasonCode.OK, "light-2"))
    _save(repo, Decision(
        second, "r-sel", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, "light-1", p_max=0.93))
    record = SelectorRecord(
        SELECTOR, ("light-2", "light-1", "light-3"), {"light-1": -61.5, "light-2": -55.0, "light-3": None},
        ("light-2", "light-1"), False, (first, second),
    )
    _save(repo, Decision(parent, "r-sel", Verdict.OK, ReasonCode.OK), record)

    stored = repo.selector_children(parent)

    assert stored.record == record
    assert stored.children == (
        SelectorChild(first, "light-2", Decision(first, "r-sel", Verdict.OK, ReasonCode.OK, "light-2")),
        SelectorChild(second, "light-1", Decision(
            second, "r-sel", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, "light-1", p_max=0.93)),
    )
    # 저장 형식: 거리 선택자 부분은 "selector" 키 하나이고, 정렬 값이 없는 기기는 null 이다.
    context = json.loads(_decision_context(conn, parent))
    assert set(context) == {"posteriors", "window_start", "window_end", "message", "selector"}
    assert context["selector"] == {
        "target_spec": SELECTOR, "order": ["light-2", "light-1", "light-3"],
        "values": {"light-1": -61.5, "light-2": -55.0, "light-3": None},
        "selected": ["light-2", "light-1"], "similar": False, "children": [first, second],
    }

    # 판정을 저장하지 않은 하위 요청은 decision 이 None 이다. 오류 판정은 행의 node_id 를 비워 저장하지만
    # 하위 요청의 기기는 기록에 남은 고른 기기다.
    req_b = _request(request_id="r-sel-b")
    parent_b = repo.link_request(req_b, "digest-b").internal_request_id
    waiting = repo.link_selector_child(parent_b, req_b, "light-3", "digest-b")
    errored = repo.link_selector_child(parent_b, req_b, "light-2", "digest-b")
    _save(repo, Decision(errored, "r-sel-b", Verdict.DENY, ReasonCode.INTERNAL_ERROR))
    record_b = SelectorRecord(
        SELECTOR, ("light-3", "light-2"), {"light-3": None, "light-2": None}, ("light-3", "light-2"), True,
        (waiting, errored),
    )
    _save(repo, Decision(parent_b, "r-sel-b", Verdict.OK, ReasonCode.OK), record_b)
    assert repo.selector_children(parent_b).children == (
        SelectorChild(waiting, "light-3", None),
        SelectorChild(errored, "light-2", Decision(errored, "r-sel-b", Verdict.DENY, ReasonCode.INTERNAL_ERROR)),
    )

    # 거리 선택자 기록이 없는 요청은 판정 전이든 후든, 요청이 없어도 None 이다.
    plain = repo.link_request(_request(request_id="r-plain", target="nearest:light"), "digest-plain")
    assert repo.selector_children(plain.internal_request_id) is None
    start, end = NOW - timedelta(seconds=3), NOW
    repo.save_decision(
        Decision(plain.internal_request_id, "r-plain", Verdict.OK, ReasonCode.OK, "light-1", message="안내"),
        DecisionRecord(settings=Settings(), posteriors={"light-1": 0.9}, window_start=start, window_end=end),
    )
    assert repo.selector_children(plain.internal_request_id) is None
    assert repo.selector_children("no-such-request") is None
    # 거리 선택자가 없는 판정의 decision_context 는 전과 바이트 단위로 같다.
    assert _decision_context(conn, plain.internal_request_id) == json.dumps(
        {"posteriors": {"light-1": 0.9}, "window_start": iso(start), "window_end": iso(end), "message": "안내"},
        ensure_ascii=False,
    )

    # 판정이 없는 행은 판정 기록 열에 거리 선택자가 남아 있어도 None 이다.
    conn.execute("UPDATE request SET verdict = NULL WHERE internal_request_id = ?", (parent,))
    assert repo.selector_children(parent) is None


def test_FR22_selector_child_restore_uses_parent_request_id(conn, repo, store):
    _seed(conn)
    insert_node(conn, node_id="light-r", capability="light", occupancy="rental")
    insert_request(conn, internal_request_id="origin", request_id="r-origin")
    req = _request(parent_internal_request_id="origin")
    parent = repo.link_request(req, "digest-sel").internal_request_id
    expected = UseRequest("u1", "r-sel", TargetSpec.parse(SELECTOR), "on", "claude", NOW, "origin")

    # 명령을 연결한 하위 요청: 실행 문맥의 요청이 상위 요청의 외부 ID 를 갖는다.
    linked = repo.link_selector_child(parent, req, "light-1", "digest-sel")
    _save(repo, Decision(linked, "r-sel", Verdict.OK, ReasonCode.OK, "light-1"))
    command = store.link_command(linked, "light-1", "on").command
    assert store.execution_context(command.command_id).request == expected

    # 예약만 남은 하위 요청: 복원이 요청과 판정을 상위 요청의 외부 ID 로 다시 만든다.
    reserved = repo.link_selector_child(parent, req, "light-r", "digest-sel")
    _save(repo, Decision(reserved, "r-sel", Verdict.OK, ReasonCode.OK, "light-r"))
    use_id = repo.reserve_if_free(ReserveRequest(reserved, "u1", "light-r", "on", None)).use_id

    def pending():
        bundle = next(b for b in store.restore_pending() if b.node.node_id == "light-r")
        assert [r.internal_request_id for r in bundle.requests] == [reserved]
        return bundle.requests[0]

    assert pending().request == expected
    assert pending().decision == Decision(reserved, "r-sel", Verdict.OK, ReasonCode.OK, "light-r", use_id)

    # 상위 행의 외부 ID 가 비었으면 빈 문자열이다.
    conn.execute("UPDATE request SET request_id = NULL WHERE internal_request_id = ?", (parent,))
    assert (pending().request.request_id, pending().decision.request_id) == ("", "")
    assert store.execution_context(command.command_id).request.request_id == ""


def test_FR22_selector_context_readable_by_dashboard_reads(conn, repo):
    """판정 기록에 더한 "selector" 키를 대시보드 읽기가 모르는 키로 거부하지 않는다."""
    _seed(conn)
    req = _request()
    parent = repo.link_request(req, "digest-sel").internal_request_id
    child = repo.link_selector_child(parent, req, "light-2", "digest-sel")
    record = SelectorRecord(
        SELECTOR, ("light-2", "light-1"), {"light-1": None, "light-2": -55.0}, ("light-2",), False, (child,))
    _save(repo, Decision(parent, "r-sel", Verdict.OK, ReasonCode.OK), record)

    shown = {row.internal_request_id: row for row in SqliteDashboardReads(conn).requests_after(0)}

    selector = shown[parent].decision_context["selector"]
    assert (selector["selected"], selector["children"]) == (("light-2",), (child,))
    assert selector["values"] == {"light-1": None, "light-2": -55.0}
    assert "posteriors" in shown[parent].decision_context


def test_FR22_selector_children_rejects_missing_or_foreign_child(conn, repo):
    _seed(conn)
    req_a, req_b, req_c = _request(request_id="r-a"), _request(request_id="r-b"), _request(request_id="r-c")
    a = repo.link_request(req_a, "digest-a").internal_request_id
    b = repo.link_request(req_b, "digest-b").internal_request_id
    c = repo.link_request(req_c, "digest-c").internal_request_id
    child_of_a = repo.link_selector_child(a, req_a, "light-1", "digest-a")
    ok = Verdict.OK, ReasonCode.OK
    _save(repo, Decision(a, "r-a", *ok), _one_device_record("light-1", (child_of_a,)))
    _save(repo, Decision(b, "r-b", *ok), _one_device_record("light-1", (child_of_a,)))  # 다른 상위 요청의 하위 요청
    _save(repo, Decision(c, "r-c", *ok), _one_device_record("light-1", ("no-such-child",)))  # 없는 요청

    assert [child.internal_request_id for child in repo.selector_children(a).children] == [child_of_a]
    with pytest.raises(LookupError):
        repo.selector_children(b)
    with pytest.raises(LookupError):
        repo.selector_children(c)


def test_FR22_selector_children_use_id_follows_live_session(conn, repo, store):
    _seed(conn)
    insert_node(conn, node_id="light-r", capability="light", occupancy="rental")
    req = _request()
    parent = repo.link_request(req, "digest-sel").internal_request_id
    child = repo.link_selector_child(parent, req, "light-r", "digest-sel")
    _save(repo, Decision(child, "r-sel", Verdict.OK, ReasonCode.OK, "light-r"))
    _save(repo, Decision(parent, "r-sel", Verdict.OK, ReasonCode.OK), _one_device_record("light-r", (child,)))
    reserve = ReserveRequest(child, "u1", "light-r", "on", None)

    def use_id():
        return repo.selector_children(parent).children[0].decision.use_id

    first = repo.reserve_if_free(reserve).use_id
    assert use_id() == first
    # 오류 뒤 취소하고 다시 예약하면 취소된 세션이 아니라 살아 있는 새 세션이다.
    store.cancel_not_sent(child, ExecutionStatus.PENDING, ReasonCode.INTERNAL_ERROR, {"source": "decision_error"})
    second = repo.reserve_if_free(reserve).use_id
    assert second != first
    assert use_id() == second


def test_FR22_selector_migration_0012_adds_parent_column(tmp_path, monkeypatch):
    db_path = str(tmp_path / "upgrade.db")
    files = sorted((Path(schema_module.__file__).parent / "sql").glob("*.sql"))
    assert files[11].name == "0012_selector_parent.sql"

    with monkeypatch.context() as patch:
        patch.setattr(schema_module, "_migration_files", lambda: files[:11])  # selector_parent_id 가 없던 시절
        old = connect(db_path)
    try:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 11
        assert "selector_parent_id" not in [column["name"] for column in old.execute("PRAGMA table_info(request)")]
        insert_user(old)
        insert_request(old, internal_request_id="req1", request_id="ext-1")
    finally:
        old.close()

    upgraded = connect(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == len(files)
        row = upgraded.execute("SELECT * FROM request WHERE internal_request_id = 'req1'").fetchone()
        assert (row["request_id"], row["selector_parent_id"]) == ("ext-1", None)
        indexes = {i["name"]: i for i in upgraded.execute("PRAGMA index_list(request)")}
        assert indexes["request_selector_parent"]["partial"] == 1
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.close()
