"""shared 계약의 기본 성질 검사."""

from dataclasses import fields
from datetime import datetime

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Capability, TargetKind, Verdict
from nearpilot.shared.interfaces import ProximityEstimator
from nearpilot.shared.models import Decision, ProximityResult, TargetSpec


def test_fr18_verdict_values():
    assert {v.value for v in Verdict} == {"OK", "BUSY", "ASK", "DENY", "NEED_APPROVAL"}


@pytest.mark.parametrize(
    ("raw", "kind", "node_id"),
    [
        ("nearest:locker", TargetKind.NEAREST, None),
        ("any:locker", TargetKind.ANY, None),
        ("locker-1", TargetKind.EXPLICIT, "locker-1"),
    ],
)
def test_target_spec_parse(raw, kind, node_id):
    spec = TargetSpec.parse(raw)
    assert (spec.kind, spec.capability, spec.node_id) == (kind, Capability.LOCKER, node_id)


@pytest.mark.parametrize("raw", ["locker", "door:locker", "nearest:door", "locker-x"])
def test_target_spec_parse_rejects(raw):
    with pytest.raises(ValueError):
        TargetSpec.parse(raw)


def test_fr11_decision_has_no_user_fields():
    names = {f.name for f in fields(Decision)}
    assert not names & {"user_id", "occupant", "occupant_user_id"}


def test_proximity_best_is_deterministic_on_tie():
    t = datetime(2026, 10, 1)
    r = ProximityResult({"locker-2": 0.5, "locker-1": 0.5}, t, t, "m0", "c0")
    assert r.best == ("locker-1", 0.5)


def test_settings_defaults_and_validation():
    s = Settings()
    assert (s.threshold_locker, s.threshold_light) == (0.95, 0.75)
    with pytest.raises(ValueError):
        Settings(threshold_locker=1.5)
    with pytest.raises(ValueError):
        Settings(leave_absent_sec=0.5)


def test_fake_satisfies_protocol():
    class FakeProximity:
        def observe(self, frame): ...

        def posterior(self, beacons, candidates, at):
            return ProximityResult({"locker-1": 0.97}, at, at, "fake", "fake")

    assert isinstance(FakeProximity(), ProximityEstimator)
