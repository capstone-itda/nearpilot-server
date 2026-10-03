"""Regression checks for PRD section 2 operational settings."""

import pytest

from nearpilot.shared.config import Settings


NUMERIC_FIELDS = (
    "threshold_locker", "threshold_light", "rssi_frame_sec",
    "leave_absent_sec", "leave_grace_sec", "allocation_window_ms",
    "beacon_recent_sec", "online_ttl_sec", "exec_timeout_sec",
    "rental_timeout_sec", "approval_ttl_sec",
)


@pytest.mark.parametrize("name", NUMERIC_FIELDS)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "3"])
def test_fr17_fr20_fr23_settings_reject_invalid_numbers(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize("name", ["allocation_window_ms", "approval_ttl_sec"])
@pytest.mark.parametrize("value", [0.5, 2.0])
def test_fr14_fr23_integer_settings_reject_floats(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize("name", NUMERIC_FIELDS[:6])
def test_fr17_fr20_fr23_required_settings_reject_none(name):
    with pytest.raises(ValueError):
        Settings(**{name: None})


def test_fr20_settings_allow_finite_integer_times_and_zero_grace():
    settings = Settings(exec_timeout_sec=3, leave_grace_sec=0, allocation_window_ms=0)
    assert settings.exec_timeout_sec == 3
    assert settings.leave_grace_sec == 0


def test_fr14_fr20_undecided_settings_remain_optional():
    settings = Settings()
    assert all(getattr(settings, name) is None for name in NUMERIC_FIELDS[6:])
