"""묶음 요청의 하위 요청 행, 판정 기록, 복원, 외부 request_id 보존 (specs/core-request-batch/spec.md, FR-09, FR-22, #284)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.db.repositories._common import iso
from nearpilot.db.repositories.retention import SqliteRetentionStore
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import ReasonCode, StoreOutcome, Verdict
from nearpilot.shared.models import (
    BatchChildren, BatchItemChildren, BatchItemRecord, BatchRecord, Decision, DecisionRecord, ReserveRequest,
    SelectorChild, SelectorRecord, TargetSpec, UseItem, UseRequest,
)

from conftest import insert_audit_log, insert_node, insert_node_command, insert_place, insert_use_session, insert_user


NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
AFTER_DEDUP_TTL = NOW + timedelta(seconds=Settings().request_dedup_ttl_sec + 60)  # 중복 보장기간이 지난 시각
SELECTOR = "near:light:1:2"
LIGHTS = UseItem(TargetSpec.parse(SELECTOR), "on")
SPEAKER = UseItem(TargetSpec.parse("speaker-1"), "off")
NEAREST_LOCKER = UseItem(TargetSpec.parse("near:locker:1:1"), "open")
LOCKER_1 = UseItem(TargetSpec.parse("locker-1"), "open")
LOCKER_2 = UseItem(TargetSpec.parse("locker-2"), "open")


def _batch_request(*items, request_id="r-batch"):
    return UseRequest("u1", request_id, TargetSpec.parse("batch:"), "batch", None, NOW, items=items)


def _item_request(item, request_id="r-batch"):
    return UseRequest("u1", request_id, item.target, item.action, None, NOW, arguments=item.arguments)


def _parent(repo, *items, request_id="r-batch"):
    """묶음 상위 요청 행을 만들고 `internal_request_id`를 반환한다."""
    return repo.link_request(_batch_request(*items, request_id=request_id), f"digest-{request_id}").internal_request_id


def _child(repo, parent, item, node_id, request_id="r-batch"):
    """항목의 하위 요청 행을 만든다. 기기를 모르면 `node_id`는 `None`이다."""
    return repo.link_selector_child(parent, _item_request(item, request_id), node_id, f"digest-{request_id}")


def _seed(conn):
    """계정, 장소, 공용형 조명 셋과 스피커 하나, 대여형 사물함 둘."""
    insert_user(conn)
    insert_place(conn)
    for node_id in ("light-1", "light-2", "light-3"):
        insert_node(conn, node_id=node_id, capability="light", occupancy="shared")
    insert_node(conn, node_id="speaker-1", capability="speaker", occupancy="shared")
    for node_id in ("locker-1", "locker-2"):
        insert_node(conn, node_id=node_id, capability="locker", occupancy="rental")


def _save(repo, decision, selector=None, batch=None):
    result = repo.save_decision(decision, DecisionRecord(settings=Settings(), selector=selector, batch=batch))
    assert result.outcome is StoreOutcome.APPLIED


def _row(conn, internal_request_id):
    return conn.execute("SELECT * FROM request WHERE internal_request_id = ?", (internal_request_id,)).fetchone()


def _external_id(conn, internal_request_id):
    return _row(conn, internal_request_id)["request_id"]


def test_FR22_batch_record_round_trip(conn, repo):
    _seed(conn)
    play = UseItem(TargetSpec.parse("speaker-1"), "play", {"volume": 3, "tracks": ["morning", "news"]})
    parent = _parent(repo, LIGHTS, play)

    # 상위 요청 행: 대상은 묶음 표기이고 동작은 "batch" 다. 기기가 없고 외부 ID 는 상위 요청만 갖는다.
    parent_row = _row(conn, parent)
    assert (parent_row["request_id"], parent_row["target_spec"], parent_row["action"]) == ("r-batch", "batch:", "batch")
    assert (parent_row["node_id"], parent_row["selector_parent_id"]) == (None, None)

    # 거리 선택자 항목의 하위 행은 고른 기기를 갖고, 명시 지정 항목의 하위 행은 기기를 모른다.
    first = _child(repo, parent, LIGHTS, "light-2")
    second = _child(repo, parent, LIGHTS, "light-1")
    third = _child(repo, parent, play, None)
    assert len({parent, first, second, third}) == 4
    rows = [_row(conn, iid) for iid in (first, second, third)]
    assert [(row["node_id"], row["target_spec"], row["action"]) for row in rows] == [
        ("light-2", SELECTOR, "on"), ("light-1", SELECTOR, "on"), (None, "speaker-1", "play"),
    ]
    for row in rows:
        assert (row["request_id"], row["selector_parent_id"], row["verdict"]) == (None, parent, None)
        assert (row["user_id"], row["input_digest"]) == ("u1", "digest-r-batch")

    # 판정을 저장하면 행의 기기는 판정의 node_id 가 된다.
    _save(repo, Decision(first, "r-batch", Verdict.OK, ReasonCode.OK, "light-2"))
    _save(repo, Decision(second, "r-batch", Verdict.OK, ReasonCode.OK, "light-1"))
    assert _row(conn, third)["node_id"] is None
    _save(repo, Decision(
        third, "r-batch", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, "speaker-1", p_max=0.93))
    assert _row(conn, third)["node_id"] == "speaker-1"

    selector = SelectorRecord(
        SELECTOR, ("light-2", "light-1", "light-3"), {"light-1": -61.5, "light-2": -55.0, "light-3": None},
        ("light-2", "light-1"), False, (first, second),
    )
    record = BatchRecord((
        BatchItemRecord(SELECTOR, "on", None, (first, second), selector),
        BatchItemRecord("speaker-1", "play", {"volume": 3, "tracks": ["morning", "news"]}, (third,)),
    ))
    _save(repo, Decision(parent, "r-batch", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED), batch=record)

    stored = repo.batch_children(parent)

    assert stored == BatchChildren(record, (
        BatchItemChildren(record.items[0], (
            SelectorChild(first, "light-2", Decision(first, "r-batch", Verdict.OK, ReasonCode.OK, "light-2")),
            SelectorChild(second, "light-1", Decision(second, "r-batch", Verdict.OK, ReasonCode.OK, "light-1")),
        )),
        BatchItemChildren(record.items[1], (
            SelectorChild(third, "speaker-1", Decision(
                third, "r-batch", Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_REQUIRED, "speaker-1", p_max=0.93)),
        )),
    ))
    # 저장 형식: 묶음 부분은 "batch" 키 하나다. 거리 선택자 항목만 "selector" 를 갖고 다른 항목은 null 이다.
    context = json.loads(_row(conn, parent)["decision_context"])
    assert set(context) == {"posteriors", "window_start", "window_end", "message", "batch"}
    assert context["batch"] == {"items": [
        {
            "target_spec": SELECTOR, "action": "on", "arguments": None, "children": [first, second],
            "selector": {
                "target_spec": SELECTOR, "order": ["light-2", "light-1", "light-3"],
                "values": {"light-1": -61.5, "light-2": -55.0, "light-3": None},
                "selected": ["light-2", "light-1"], "similar": False, "children": [first, second],
                "counts": {}, "boundary_probability": None,
            },
        },
        {
            "target_spec": "speaker-1", "action": "play", "arguments": {"volume": 3, "tracks": ["morning", "news"]},
            "children": [third], "selector": None,
        },
    ]}

    # 상위 행의 외부 ID 가 비었으면 하위 판정의 request_id 는 빈 문자열이다.
    conn.execute("UPDATE request SET request_id = NULL WHERE internal_request_id = ?", (parent,))
    shown = repo.batch_children(parent)
    assert {child.decision.request_id for item in shown.items for child in item.children} == {""}

    # 판정 전의 하위 요청은 decision 이 None 이다. node_id 는 행이 알면 그 값이고, 모르거나 판정이 기기 없이
    # 끝났으면 "" 다.
    parent_b = _parent(repo, LIGHTS, play, request_id="r-batch-b")
    waiting = _child(repo, parent_b, LIGHTS, "light-3", "r-batch-b")
    errored = _child(repo, parent_b, LIGHTS, "light-2", "r-batch-b")
    unknown = _child(repo, parent_b, play, None, "r-batch-b")
    _save(repo, Decision(errored, "r-batch-b", Verdict.DENY, ReasonCode.INTERNAL_ERROR))
    selector_b = SelectorRecord(
        SELECTOR, ("light-3", "light-2"), {"light-3": None, "light-2": None}, ("light-3", "light-2"), True,
        (waiting, errored),
    )
    record_b = BatchRecord((
        BatchItemRecord(SELECTOR, "on", None, (waiting, errored), selector_b),
        BatchItemRecord("speaker-1", "play", {"volume": 3, "tracks": ["morning", "news"]}, (unknown,)),
    ))
    _save(repo, Decision(parent_b, "r-batch-b", Verdict.DENY, ReasonCode.INTERNAL_ERROR), batch=record_b)
    assert [item.children for item in repo.batch_children(parent_b).items] == [
        (
            SelectorChild(waiting, "light-3", None),
            SelectorChild(errored, "", Decision(errored, "r-batch-b", Verdict.DENY, ReasonCode.INTERNAL_ERROR)),
        ),
        (SelectorChild(unknown, "", None),),
    ]

    # OK 하위 판정의 use_id 는 그 하위 요청의 점유 세션이다.
    parent_c = _parent(repo, LOCKER_1, request_id="r-batch-c")
    rental = _child(repo, parent_c, LOCKER_1, None, "r-batch-c")
    _save(repo, Decision(rental, "r-batch-c", Verdict.OK, ReasonCode.OK, "locker-1"))
    use_id = repo.reserve_if_free(ReserveRequest(rental, "u1", "locker-1", "open", None)).use_id
    record_c = BatchRecord((BatchItemRecord("locker-1", "open", None, (rental,)),))
    _save(repo, Decision(parent_c, "r-batch-c", Verdict.OK, ReasonCode.OK), batch=record_c)
    assert use_id is not None
    assert repo.batch_children(parent_c).items[0].children[0].decision.use_id == use_id

    # 거리 선택자 기록과 묶음 기록을 함께 담으면 둘 다 저장한다. 서로를 덮어쓰지 않는다.
    both = _parent(repo, LIGHTS, request_id="r-both")
    selector_both = SelectorRecord(SELECTOR, ("light-1",), {"light-1": None}, ("light-1",), False)
    record_both = BatchRecord((BatchItemRecord(SELECTOR, "on", None, (), selector_both),))
    _save(repo, Decision(both, "r-both", Verdict.ASK, ReasonCode.ORDER_UNCERTAIN), selector_both, record_both)
    assert {"selector", "batch"} <= set(json.loads(_row(conn, both)["decision_context"]))
    assert repo.selector_children(both).record == selector_both
    assert repo.batch_children(both).record == record_both

    # 기록이 가리키는 하위 행이 없거나 다른 상위 요청의 것이면 LookupError 다.
    other = _parent(repo, SPEAKER, request_id="r-other")
    foreign = _child(repo, other, SPEAKER, None, "r-other")
    for index, missing in enumerate(("no-such-child", foreign)):
        broken = _parent(repo, SPEAKER, request_id=f"r-broken-{index}")
        _save(repo, Decision(broken, f"r-broken-{index}", Verdict.OK, ReasonCode.OK),
              batch=BatchRecord((BatchItemRecord("speaker-1", "off", None, (missing,)),)))
        with pytest.raises(LookupError):
            repo.batch_children(broken)

    # 묶음 기록이 없는 요청은 판정 전이든 후든, 요청이 없어도 None 이다.
    plain = repo.link_request(_item_request(SPEAKER, "r-plain"), "digest-plain").internal_request_id
    assert repo.batch_children(plain) is None
    _save(repo, Decision(plain, "r-plain", Verdict.OK, ReasonCode.OK, "speaker-1"))
    assert repo.batch_children(plain) is None
    assert "batch" not in json.loads(_row(conn, plain)["decision_context"])
    assert repo.batch_children("no-such-request") is None
    # 판정이 없는 행은 판정 기록 열에 묶음 기록이 남아 있어도 None 이다.
    conn.execute("UPDATE request SET verdict = NULL WHERE internal_request_id = ?", (parent,))
    assert repo.batch_children(parent) is None


def test_FR22_batch_child_restore_uses_item_target(conn, repo, store):
    _seed(conn)
    parent = _parent(repo, SPEAKER, LOCKER_1, LOCKER_2)

    # 명령을 연결한 하위 요청: 실행 문맥의 요청이 항목의 대상·동작과 상위 요청의 외부 ID 를 갖는다.
    linked = _child(repo, parent, SPEAKER, None)
    _save(repo, Decision(linked, "r-batch", Verdict.OK, ReasonCode.OK, "speaker-1"))
    command = store.link_command(linked, "speaker-1", "off").command
    assert store.execution_context(command.command_id).request == _item_request(SPEAKER)

    # 예약만 남은 하위 요청: 복원이 요청과 판정을 항목의 대상·동작과 상위 요청의 외부 ID 로 다시 만든다.
    reserved = _child(repo, parent, LOCKER_1, None)
    _save(repo, Decision(reserved, "r-batch", Verdict.OK, ReasonCode.OK, "locker-1"))
    use_id = repo.reserve_if_free(ReserveRequest(reserved, "u1", "locker-1", "open", None)).use_id

    def pending(node_id, internal_request_id):
        bundle = next(b for b in store.restore_pending() if b.node.node_id == node_id)
        assert [r.internal_request_id for r in bundle.requests] == [internal_request_id]
        return bundle.requests[0]

    assert pending("locker-1", reserved).request == _item_request(LOCKER_1)
    assert pending("locker-1", reserved).decision == Decision(
        reserved, "r-batch", Verdict.OK, ReasonCode.OK, "locker-1", use_id)

    # 판정을 저장하기 전에 멈춘 하위 요청: 행의 기기와 판정이 비어 있어도 남은 예약 세션이 기기를 알려 준다.
    unsaved = _child(repo, parent, LOCKER_2, None)
    repo.reserve_if_free(ReserveRequest(unsaved, "u1", "locker-2", "open", None))
    assert _row(conn, unsaved)["node_id"] is None
    assert pending("locker-2", unsaved).request == _item_request(LOCKER_2)
    assert pending("locker-2", unsaved).decision is None

    # 상위 행의 외부 ID 가 비었으면 빈 문자열이다.
    conn.execute("UPDATE request SET request_id = NULL WHERE internal_request_id = ?", (parent,))
    restored = pending("locker-1", reserved)
    assert (restored.request.request_id, restored.decision.request_id) == ("", "")
    assert pending("locker-2", unsaved).request.request_id == ""
    assert store.execution_context(command.command_id).request.request_id == ""


def _settled_batch(conn, repo):
    """판정을 마친 묶음 상위 요청 하나와 하위 요청 둘. 감사 행은 모두 NOW 다.

    첫 항목은 거리 선택자이고 둘째 항목은 명시 지정이라, 둘째 하위 행은 판정이 채우기 전까지 기기를 모른다.
    """
    _seed(conn)
    parent = _parent(repo, NEAREST_LOCKER, LOCKER_2)
    first = _child(repo, parent, NEAREST_LOCKER, "locker-1")
    second = _child(repo, parent, LOCKER_2, None)
    _save(repo, Decision(first, "r-batch", Verdict.OK, ReasonCode.OK, "locker-1"))
    _save(repo, Decision(second, "r-batch", Verdict.OK, ReasonCode.OK, "locker-2"))
    _save(repo, Decision(parent, "r-batch", Verdict.OK, ReasonCode.OK))
    for iid in (parent, first, second):
        insert_audit_log(conn, internal_request_id=iid, created_at=iso(NOW))
    return parent, (first, second)


@pytest.mark.parametrize("in_progress", ["active_session", "pending_command"])
def test_FR22_batch_parent_request_id_kept_while_child_in_progress(conn, repo, in_progress):
    parent, (first, second) = _settled_batch(conn, repo)
    insert_use_session(conn, use_id="use-1", internal_request_id=first, node_id="locker-1",
                       state="CLOSED", end_reason="release", closed_at=iso(NOW))
    if in_progress == "active_session":  # 명시 지정 항목의 기기를 아직 쓰고 있다
        insert_use_session(conn, use_id="use-2", internal_request_id=second, node_id="locker-2", state="ACTIVE")
    else:  # 명시 지정 항목의 명령이 아직 끝나지 않았다
        insert_use_session(conn, use_id="use-2", internal_request_id=second, node_id="locker-2",
                           state="CLOSED", end_reason="release", closed_at=iso(NOW))
        insert_node_command(conn, command_id="cmd-2", internal_request_id=second, node_id="locker-2",
                            status="pending")
    store = SqliteRetentionStore(conn, Settings())

    report = store.cleanup(AFTER_DEDUP_TTL)

    assert report.cleared_requests == 0
    assert _external_id(conn, parent) == "r-batch"
    assert _parent(repo, NEAREST_LOCKER, LOCKER_2) == parent  # 같은 요청 ID 의 재전송은 같은 상위 요청을 찾는다

    # 하위 요청이 끝나면 외부 ID 를 비운다.
    if in_progress == "active_session":
        conn.execute("UPDATE use_session SET state = 'CLOSED', end_reason = 'release', closed_at = ? "
                     "WHERE use_id = 'use-2'", (iso(NOW),))
    else:
        conn.execute("UPDATE node_command SET status = 'succeeded' WHERE command_id = 'cmd-2'")
    report = store.cleanup(AFTER_DEDUP_TTL)
    assert report.cleared_requests == 1
    assert _external_id(conn, parent) is None
