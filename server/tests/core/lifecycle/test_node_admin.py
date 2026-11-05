"""기기 관리 서비스: 원격 재시작(#235)과 접속 설정 묶음(#234) (FR-12A, NFR-04, NFR-12)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.core.lifecycle.node_admin import NodeAdminService
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Capability, Occupancy, RestartOutcome
from nearpilot.shared.interfaces import NodeAdminService as NodeAdminProtocol
from nearpilot.shared.models import ProvisioningBundle

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


class Source:
    def __init__(self, bundle):
        self.bundle_value = bundle
        self.asked = []

    def bundle(self, node_id):
        self.asked.append(node_id)
        return self.bundle_value


BUNDLE = ProvisioningBundle("locker-1", "locker-1", "node-secret", "nearpilot.local", 18883,
                            Capability.LOCKER, Occupancy.RENTAL)


def test_FR12A_provisioning_returns_bundle_for_owner(caplog):
    source = Source(BUNDLE)
    admin = NodeAdminService(repository=Repository(), restarter=Restarter(), clock=lambda: NOW, provisioning=source)
    with caplog.at_level(logging.INFO, logger="nearpilot.core.lifecycle.node_admin"):
        assert admin.provisioning("owner", "locker-1") is BUNDLE
    assert source.asked == ["locker-1"]
    assert "node-secret" not in caplog.text  # 묶음을 로그에 쓰지 않는다 (NFR-08)


def test_FR12A_provisioning_rejects_non_owner():
    source = Source(BUNDLE)
    admin = NodeAdminService(repository=Repository(owner=False), restarter=Restarter(), clock=lambda: NOW,
                             provisioning=source)
    with pytest.raises(PermissionError):
        admin.provisioning("visitor", "locker-1")
    assert source.asked == []  # 소유자가 아니면 묶음을 읽지 않는다


@pytest.mark.parametrize("provisioning", [None, Source(None)])
def test_FR12A_provisioning_missing_bundle(provisioning):
    admin = NodeAdminService(repository=Repository(), restarter=Restarter(), clock=lambda: NOW,
                             provisioning=provisioning)
    with pytest.raises(LookupError):
        admin.provisioning("owner", "locker-1")
