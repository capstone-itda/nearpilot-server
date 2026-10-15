"""`evidence`에 `datetime`이 있을 때 재조회 접점의 값이 달라지는지 재현한다.

`cancel_not_sent`·`record_result`는 이번 호출에서 받은 `evidence`를 그대로 돌려주도록
고쳤다. 하지만 `execution()`과 `close_result()`는 항상 DB에서 다시 읽으므로, `datetime`은
저장되는 순간 ISO 문자열로 바뀐 채로 돌아온다. 이 테스트는 그 불일치가 실제로 일어나는지
SQLite로 확인한다 (decision 이슈 초안 대기, specs/db-execution-store/spec.md §7 가정).
"""

from __future__ import annotations

from datetime import datetime, timezone

from nearpilot.shared.enums import Action, EndReason, ExecutionStatus, ReasonCode, StoreOutcome

from conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user


def test_FR19_execution_evidence_with_datetime_mismatches_after_requery(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="RESERVED")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="RESERVED")
    store.link_command("req1", "node1", Action.OPEN)
    evidence = {"source": "recheck", "confirmed_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    direct = store.cancel_not_sent("req1", ExecutionStatus.PENDING, ReasonCode.RECHECK_FAILED, evidence)
    requeried = store.execution("req1")

    # 저장 함수의 직접 반환값은 원래 객체와 같다 (이전 커밋에서 고쳤다).
    assert direct.outcome == StoreOutcome.APPLIED
    assert direct.command.evidence == evidence
    # 하지만 execution()으로 다시 조회하면 datetime이 문자열로 바뀐 채로 돌아온다.
    assert requeried.evidence != evidence
    assert requeried.evidence["confirmed_at"] == "2026-01-01T00:00:00+00:00"
    assert isinstance(evidence["confirmed_at"], datetime)


def test_FR20_close_result_evidence_with_datetime_mismatches_after_requery(store, conn):
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="node1", occupancy="rental", device_state="ACTIVE")
    insert_request(conn, internal_request_id="req1")
    insert_use_session(conn, use_id="use1", internal_request_id="req1", state="ACTIVE")
    store.begin_close("use1", EndReason.RELEASE)
    evidence = {"door_closed": True, "checked_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    direct = store.finish_close("use1", datetime(2026, 1, 1, 1, tzinfo=timezone.utc), "check1", evidence)
    requeried = store.close_result("use1")

    assert direct.outcome == StoreOutcome.APPLIED
    # close_result()는 finish_close와 별개로 항상 DB를 다시 읽는다 — release.py의
    # _matches()가 쓰는 접점이다 (355줄: completed.evidence == report.evidence).
    assert requeried is not None
    assert requeried.evidence != evidence
    assert requeried.evidence["checked_at"] == "2026-01-01T00:00:00+00:00"
