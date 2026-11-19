"""근접 지정의 전체 상태 범위와 RSSI 하한 설정 (FR-16, FR-17, #312)."""

from __future__ import annotations

import math

import pytest

from nearpilot.shared.config import Settings, settings_from_data, settings_to_data


def test_FR16_proximity_scope_settings_default_to_current_rule():
    settings = Settings()
    assert (settings.proximity_state_scope, settings.proximity_rssi_floor_dbm) == ("place", None)


@pytest.mark.parametrize("kw", [
    {"proximity_state_scope": "all"},
    {"proximity_rssi_floor_dbm": math.nan},
    {"proximity_rssi_floor_dbm": True},
    {"proximity_rssi_floor_dbm": "-52"},
])
def test_FR16_proximity_scope_settings_reject_bad_values(kw):
    with pytest.raises(ValueError):
        Settings(**kw)


def test_FR22_proximity_scope_settings_survive_snapshot():
    settings = Settings(proximity_state_scope="kind", proximity_rssi_floor_dbm=-52.0)
    assert settings_from_data(settings_to_data(settings)) == settings
