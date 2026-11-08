"""거리 선택자 상위 요청의 외부 request_id 보존 (FR-22, specs/core-distance-selector/spec.md 결정 22).

세션·명령은 하위 요청 행에 붙는다. 하위 요청이 진행 중이면 상위 요청의 외부 ID 를 비우지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import (
    insert_audit_log, insert_node, insert_node_command, insert_place, insert_use_session, insert_user,
)

from nearpilot.db.repositories._common import iso
from nearpilot.db.repositories.retention import SqliteRetentionStore
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import ReasonCode, StoreOutcome, Verdict
from nearpilot.shared.models import Decision, DecisionRecord, SelectorRecord, TargetSpec, UseRequest

T = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
SPEC = "near:locker:1:2"


def _selector(conn, repo):
    """판정을 마친 상위 요청 하나와 하위 요청 둘. 감사 행은 모두 T 다."""
    insert_user(conn)
    insert_place(conn)
    for node_id in ("locker-1", "locker-2"):
        insert_node(conn, node_id=node_id, capability="locker", occupancy="rental")
    req = UseRequest(user_id="u1", request_id="r-sel", target=TargetSpec.parse(SPEC), action="open",
                     host_kind="claude", received_at=T, parent_internal_request_id=None)
    parent = repo.link_request(req, "dg").internal_request_id
    children = tuple(repo.link_selector_child(parent, req, n, "dg") for n in ("locker-1", "locker-2"))
    settings = Settings()
    for child, node_id in zip(children, ("locker-1", "locker-2")):
        saved = repo.save_decision(Decision(child, "r-sel", Verdict.OK, ReasonCode.OK, node_id),
                                   DecisionRecord(settings=settings))
        assert saved.outcome is StoreOutcome.APPLIED
    record = SelectorRecord(SPEC, ("locker-1", "locker-2"), {"locker-1": -50.0, "locker-2": -60.0},
                            ("locker-1", "locker-2"), False, children)
    saved = repo.save_decision(Decision(parent, "r-sel", Verdict.OK, ReasonCode.OK),
                               DecisionRecord(settings=settings, selector=record))
    assert saved.outcome is StoreOutcome.APPLIED
    for iid in (parent, *children):
        insert_audit_log(conn, internal_request_id=iid, created_at=iso(T))
    return req, parent, children


def _external_id(conn, iid):
    return conn.execute("SELECT request_id FROM request WHERE internal_request_id = ?", (iid,)).fetchone()[0]


@pytest.mark.parametrize("in_progress", ["active_session", "pending_command"])
def test_FR22_selector_parent_request_id_kept_while_child_in_progress(conn, repo, in_progress):
    req, parent, (first, second) = _selector(conn, repo)
    insert_use_session(conn, use_id="use-1", internal_request_id=first, node_id="locker-1",
                       state="CLOSED", end_reason="RETURNED", closed_at=iso(T))
    if in_progress == "active_session":  # 두 번째 기기를 아직 쓰고 있다
        insert_use_session(conn, use_id="use-2", internal_request_id=second, node_id="locker-2", state="ACTIVE")
    else:  # 두 번째 기기의 명령이 아직 끝나지 않았다
        insert_use_session(conn, use_id="use-2", internal_request_id=second, node_id="locker-2",
                           state="CLOSED", end_reason="RETURNED", closed_at=iso(T))
        insert_node_command(conn, command_id="cmd-2", internal_request_id=second, node_id="locker-2",
                            status="pending")

    report = SqliteRetentionStore(conn, Settings()).cleanup(T + timedelta(minutes=21))

    assert report.cleared_requests == 0
    assert _external_id(conn, parent) == "r-sel"
    again = repo.link_request(req, "dg")  # 같은 요청 ID 의 재전송은 같은 상위 요청을 찾는다
    assert (again.is_new, again.internal_request_id) == (False, parent)


def test_FR22_selector_parent_request_id_cleared_after_latest_child(conn, repo):
    """하위 요청이 모두 끝나면 비운다. 기준 시각은 상위·하위 요청 중 가장 늦게 끝난 것이다."""
    _, parent, (first, second) = _selector(conn, repo)
    for use_id, child, node_id in (("use-1", first, "locker-1"), ("use-2", second, "locker-2")):
        insert_use_session(conn, use_id=use_id, internal_request_id=child, node_id=node_id,
                           state="CLOSED", end_reason="RETURNED", closed_at=iso(T))
    insert_audit_log(conn, internal_request_id=second, created_at=iso(T + timedelta(minutes=5)))
    store = SqliteRetentionStore(conn, Settings())

    store.cleanup(T + timedelta(minutes=21))  # 상위 요청은 T 에 끝났지만 두 번째 하위 요청은 T+5분이다
    assert _external_id(conn, parent) == "r-sel"

    store.cleanup(T + timedelta(minutes=26))
    assert _external_id(conn, parent) is None
