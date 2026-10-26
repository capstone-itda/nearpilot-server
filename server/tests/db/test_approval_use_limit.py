"""승인 한 건의 유효 횟수 한도 (FR-14, FR-19).

one_time 은 max_uses 와 상관없이 한도가 1이고, time_window 는 max_uses 다 (NULL 이면 무제한).
남은 횟수를 읽는 세 곳(valid_approval, approvals_in_scope, 대시보드)과 사용분 예약의 횟수 검사가
같은 규칙을 쓴다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nearpilot.db.repositories.dashboard_reads import SqliteDashboardReads
from nearpilot.shared.enums import Action, ReserveOutcome
from nearpilot.shared.models import ReserveRequest

from conftest import (
    insert_approval,
    insert_approval_usage,
    insert_node,
    insert_place,
    insert_policy,
    insert_request,
    insert_user,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _approved(conn, approval_mode, max_uses):
    """승인된 승인 한 건(appr1)과 그 정책 리비전을 넣는다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn)
    insert_policy(conn, approval_mode=approval_mode, max_uses=max_uses)
    insert_approval(conn, approval_id="appr1", pending_until="2026-01-01T00:10:00+00:00")


def _remaining(conn, repo):
    """남은 횟수를 읽는 세 곳의 값: (valid_approval, approvals_in_scope, 대시보드)."""
    valid = repo.valid_approval("u1", "node1", Action.OPEN, 1, NOW)
    [record] = repo.approvals_in_scope("u1", "node1")
    [dashboard] = SqliteDashboardReads(conn).approvals()
    return valid.remaining_uses, record.remaining_uses, dashboard.remaining_uses


@pytest.mark.parametrize("max_uses", [None, 1, 3])
def test_FR14_remaining_uses_one_time(conn, repo, max_uses):
    _approved(conn, "one_time", max_uses)
    assert _remaining(conn, repo) == (1, 1, 1)

    insert_approval_usage(conn, "usage1", "appr1", "req-usage1", state="reserved")
    assert _remaining(conn, repo) == (0, 0, 0)

    conn.execute("UPDATE approval_usage SET state = 'released' WHERE usage_id = 'usage1'")
    assert _remaining(conn, repo) == (1, 1, 1)  # 반환한 사용분은 세지 않는다


@pytest.mark.parametrize("max_uses, before, after", [(None, None, None), (3, 3, 2)])
def test_FR14_remaining_uses_time_window_follows_max_uses(conn, repo, max_uses, before, after):
    _approved(conn, "time_window", max_uses)
    assert _remaining(conn, repo) == (before,) * 3

    insert_approval_usage(conn, "usage1", "appr1", "req-usage1", state="consumed")
    assert _remaining(conn, repo) == (after,) * 3


def _shared_light(conn, approval_mode, max_uses):
    """승인된 승인(appr1)이 있는 공용형 조명과 요청 행 req1~req3 을 넣는다."""
    insert_user(conn)
    insert_place(conn)
    insert_node(conn, node_id="light1", capability="light", occupancy="shared")
    insert_policy(conn, node_id="light1", approval_mode=approval_mode, max_uses=max_uses)
    insert_approval(conn, approval_id="appr1", node_id="light1")
    for internal_request_id in ("req1", "req2", "req3"):
        insert_request(conn, internal_request_id=internal_request_id, request_id=f"ext-{internal_request_id}",
                       node_id="light1")


def _reserve_outcomes(repo, count):
    return [
        repo.reserve_if_free(ReserveRequest(
            internal_request_id=f"req{n}", user_id="u1", node_id="light1", action=Action.ON, approval_id="appr1",
        )).outcome
        for n in range(1, count + 1)
    ]


@pytest.mark.parametrize("max_uses", [None, 1, 3])
def test_FR14_reserve_one_time_allows_one_usage(conn, repo, max_uses):
    _shared_light(conn, "one_time", max_uses)

    assert _reserve_outcomes(repo, 2) == [ReserveOutcome.RESERVED, ReserveOutcome.APPROVAL_EXHAUSTED]
    assert conn.execute("SELECT COUNT(*) AS c FROM approval_usage").fetchone()["c"] == 1


@pytest.mark.parametrize("max_uses, outcomes", [
    (None, [ReserveOutcome.RESERVED] * 3),  # NULL 은 무제한이다
    (2, [ReserveOutcome.RESERVED, ReserveOutcome.RESERVED, ReserveOutcome.APPROVAL_EXHAUSTED]),
])
def test_FR14_reserve_time_window_follows_max_uses(conn, repo, max_uses, outcomes):
    _shared_light(conn, "time_window", max_uses)

    assert _reserve_outcomes(repo, 3) == outcomes
