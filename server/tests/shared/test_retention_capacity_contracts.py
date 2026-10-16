"""FR-21·FR-22의 보존기간 정리·중복 방지 한도 알림 공동 경계 검사 (#164, #165)."""

from __future__ import annotations

from datetime import datetime
from inspect import signature

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import ReasonCode, Verdict
from nearpilot.shared.interfaces import RetentionStore
from nearpilot.shared.models import CleanupReport, Decision, RequestLink


def test_FR21_FR22_retention_store_is_runtime_checkable():
    class FakeRetentionStore:
        def cleanup(self, now: datetime) -> CleanupReport:
            return CleanupReport(cleared_requests=0, deleted_sessions=0, deleted_audit_rows=0)

    assert isinstance(FakeRetentionStore(), RetentionStore)
    assert list(signature(RetentionStore.cleanup).parameters) == ["self", "now"]


def test_FR21_FR22_retention_store_rejects_wrong_signature():
    class WrongShape:
        def cleanup(self) -> CleanupReport:  # now 인자가 없다
            return CleanupReport(cleared_requests=0, deleted_sessions=0, deleted_audit_rows=0)

    # runtime_checkable Protocol은 이름만 보고 시그니처까지는 검사하지 않는다 — 메서드가 있으니
    # isinstance는 True다. 시그니처는 cleanup의 signature()로 직접 검사해야 한다.
    assert isinstance(WrongShape(), RetentionStore)
    with pytest.raises(TypeError):
        WrongShape().cleanup(datetime(2026, 1, 1))


def test_FR21_FR22_cleanup_report_fields_and_no_user_id():
    report = CleanupReport(cleared_requests=3, deleted_sessions=5, deleted_audit_rows=7)

    assert (report.cleared_requests, report.deleted_sessions, report.deleted_audit_rows) == (3, 5, 7)
    assert "user_id" not in CleanupReport.__dataclass_fields__


def test_FR18_request_link_capacity_exceeded_defaults_false():
    link = RequestLink(internal_request_id="req1", is_new=True, conflict=False)

    assert link.capacity_exceeded is False  # 기존 호출을 깨지 않는다

    exceeded = RequestLink(internal_request_id="req2", is_new=True, conflict=False, capacity_exceeded=True)
    assert exceeded.capacity_exceeded is True


def test_FR18_request_link_previous_and_capacity_exceeded_are_independent():
    previous = Decision(internal_request_id="req1", request_id="ext-1", verdict=Verdict.OK, reason=ReasonCode.OK)
    link = RequestLink(internal_request_id="req1", is_new=False, conflict=False, previous=previous)

    assert link.previous is previous
    assert link.capacity_exceeded is False


def test_NFR13_settings_retention_cleanup_interval_default_and_validation():
    assert Settings().retention_cleanup_interval_sec == 60

    with pytest.raises(ValueError):
        Settings(retention_cleanup_interval_sec=0)
    with pytest.raises(ValueError):
        Settings(retention_cleanup_interval_sec=1.5)
