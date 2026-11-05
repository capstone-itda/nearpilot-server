"""기기 관리 서비스: 원격 재시작 (FR-12A, NFR-04, #235, specs/iot-node-restart/spec.md)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.lifecycle.node_admin import NodeAdminService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import RestartOutcome
from nearpilot.shared.interfaces import NodeAdminService as NodeAdminProtocol

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)


class Repository:
    def __init__(self, owner: bool = True):
        self.owner = owner
        self.asked = []

    def is_place_owner(self, user_id, node_id):
        self.asked.append((user_id, node_id))
        return self.owner


class Restarter:
    def __init__(self, outcome=RestartOutcome.SENT):
        self.outcome = outcome
        self.calls = []

    def restart(self, node_id, restart_id):
        self.calls.append((node_id, restart_id))
        return self.outcome


def service(repository, restarter, ids=("r-1", "r-2")):
    pending = list(ids)
    return NodeAdminService(repository=repository, restarter=restarter, clock=lambda: NOW,
                            settings=lambda: Settings(exec_timeout_sec=3.0), new_id=lambda: pending.pop(0))


def test_FR12A_restart_sends_new_id_for_owner(caplog):
    restarter = Restarter()
    admin = service(Repository(), restarter)
    assert isinstance(admin, NodeAdminProtocol)
    with caplog.at_level(logging.INFO, logger="nearpilot.core.lifecycle.node_admin"):
        first = admin.restart("owner", "locker-1")
        second = admin.restart("owner", "locker-1")
    assert (first.outcome, first.restart_id, first.expires_at) == (
        RestartOutcome.SENT, "r-1", NOW + timedelta(seconds=3))
    assert second.restart_id == "r-2"  # 요청마다 새 ID다
    assert restarter.calls == [("locker-1", "r-1"), ("locker-1", "r-2")]
    # 서버 로그에 요청자, 기기, 결과를 남긴다.
    assert "user=owner node=locker-1 restart_id=r-1 outcome=SENT" in caplog.text


def test_FR12A_restart_rejects_non_owner(caplog):
    restarter = Restarter()
    with caplog.at_level(logging.INFO, logger="nearpilot.core.lifecycle.node_admin"):
        result = service(Repository(owner=False), restarter).restart("visitor", "locker-1")
    assert (result.outcome, result.restart_id, result.expires_at) == (RestartOutcome.NOT_OWNER, None, None)
    assert restarter.calls == []
    assert "NOT_OWNER" in caplog.text


@pytest.mark.parametrize("outcome", [RestartOutcome.NODE_OFFLINE, RestartOutcome.NOT_SENT])
def test_NFR04_restart_failure_has_no_id(outcome):
    result = service(Repository(), Restarter(outcome)).restart("owner", "locker-1")
    assert (result.outcome, result.restart_id, result.expires_at) == (outcome, None, None)
