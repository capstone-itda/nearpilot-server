"""Regression checks for requirements.md section 1.2 operational settings."""

import pytest

from nearpilot.shared.config import Settings


NUMERIC_FIELDS = (
    "threshold_locker", "threshold_light", "rssi_frame_sec",
    "leave_absent_sec", "leave_grace_sec", "allocation_window_ms",
    "beacon_recent_sec", "online_ttl_sec", "status_report_sec",
    "reservation_wait_sec", "exec_timeout_sec",
    "rental_duration_sec", "approval_ttl_sec", "approval_pending_ttl_sec",
    "use_history_retention_days", "audit_retention_days",
    "request_dedup_ttl_sec", "dedup_entry_limit",
)

INTEGER_FIELDS = (
    "allocation_window_ms", "approval_ttl_sec", "approval_pending_ttl_sec",
    "use_history_retention_days", "audit_retention_days",
    "request_dedup_ttl_sec", "dedup_entry_limit",
)


@pytest.mark.parametrize("name", NUMERIC_FIELDS)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "3"])
def test_fr17_fr20_fr23_settings_reject_invalid_numbers(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize("name", INTEGER_FIELDS)
@pytest.mark.parametrize("value", [0.5, 2.0])
def test_fr14_fr23_integer_settings_reject_floats(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize("name", NUMERIC_FIELDS)
def test_fr17_fr20_fr23_required_settings_reject_none(name):
    with pytest.raises(ValueError):
        Settings(**{name: None})


def test_fr20_settings_allow_finite_integer_times_and_zero_grace():
    settings = Settings(exec_timeout_sec=3, leave_grace_sec=0, allocation_window_ms=0)
    assert settings.exec_timeout_sec == 3
    assert settings.leave_grace_sec == 0


def test_fr14_fr15_fr20_prd7_time_settings_have_defaults():
    settings = Settings()
    assert settings.beacon_recent_sec >= settings.rssi_frame_sec
    assert all(getattr(settings, name) > 0 for name in NUMERIC_FIELDS[6:])


def test_fr13_fr15_fr19_fr20_time_settings_match_initial_baseline():
    settings = Settings()
    assert settings.beacon_recent_sec == 3.0
    assert settings.online_ttl_sec == 15.0
    assert settings.status_report_sec == 1.0
    assert settings.reservation_wait_sec == 10.0
    assert settings.exec_timeout_sec == 3.0
    assert settings.rental_duration_sec == 3600.0


def test_fr03_fr14_approval_time_settings_match_initial_baseline():
    settings = Settings()
    assert settings.approval_ttl_sec == 3600
    assert settings.approval_pending_ttl_sec == 600


def test_fr21_fr22_retention_and_dedup_settings_match_initial_baseline():
    settings = Settings()
    assert settings.use_history_retention_days == 7
    assert settings.audit_retention_days == 30
    assert settings.request_dedup_ttl_sec == 1200
    assert settings.dedup_entry_limit == 100_000


@pytest.mark.parametrize(
    "name",
    [
        "status_report_sec", "reservation_wait_sec", "approval_pending_ttl_sec",
        "use_history_retention_days", "audit_retention_days",
        "request_dedup_ttl_sec", "dedup_entry_limit",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_fr03_fr19_fr21_fr22_new_settings_reject_non_positive(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


def test_fr13_online_ttl_is_not_shorter_than_status_report_period():
    with pytest.raises(ValueError):
        Settings(online_ttl_sec=0.5)
    assert Settings(online_ttl_sec=1.0).online_ttl_sec == 1.0
