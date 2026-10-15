"""저장 장애를 `StoreNotApplied`·`StoreOutcomeUnknown`으로 구분하는지 확인한다.

`ExecutionStore`의 모든 쓰기 함수가 공유하는 `immediate_transaction`(`_common.py`)의
계약이다 (specs/db-execution-store/spec.md §5).
"""

from __future__ import annotations

import pytest

from nearpilot.db.repositories.execution_store import SqliteExecutionStore
from nearpilot.db.schema import connect
from nearpilot.shared.enums import Action
from nearpilot.shared.errors import StoreNotApplied

from conftest import insert_node, insert_place, insert_request, insert_use_session, insert_user


def test_NFR03_store_not_applied_on_busy(tmp_path):
    db_path = str(tmp_path / "busy.db")
    setup_conn = connect(db_path)
    insert_user(setup_conn)
    insert_place(setup_conn)
    insert_node(setup_conn)
    insert_request(setup_conn, internal_request_id="req1")
    insert_use_session(setup_conn, use_id="use1", internal_request_id="req1", state="RESERVED")
    setup_conn.close()

    blocker = connect(db_path)
    blocker.execute("BEGIN IMMEDIATE")  # 쓰기 잠금을 쥐고 있다.
    conn = connect(db_path, busy_timeout_ms=50)
    try:
        store = SqliteExecutionStore(conn)
        with pytest.raises(StoreNotApplied):
            store.link_command("req1", "node1", Action.OPEN)
    finally:
        conn.close()
        blocker.execute("ROLLBACK")
        blocker.close()

    check_conn = connect(db_path)
    count = check_conn.execute("SELECT COUNT(*) AS n FROM node_command").fetchone()
    assert count["n"] == 0  # 아무것도 반영되지 않았다.
    check_conn.close()
