"""FR-16·FR-17·FR-18의 비정규화 후보 확률과 명시 지정 설정 검사."""

from datetime import datetime
from inspect import signature

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.interfaces import ProximityEstimator
from nearpilot.shared.models import ProximityResult

NOW = datetime(2026, 10, 13)


def test_FR16_candidate_projection_preserves_outside_probability_mass():
    result = ProximityResult({'locker-1': 0.2, 'locker-2': 0.1}, NOW, NOW, 'm1', 'c1')
    assert result.posteriors == {'locker-1': 0.2, 'locker-2': 0.1}
    assert result.best == ('locker-1', 0.2)
    assert sum(result.posteriors.values()) < 1


@pytest.mark.parametrize('values', [{'a': 0.8, 'b': 0.4}, {'a': -0.1}, {'a': 1.1},
                                   {'a': float('nan')}, {'a': float('inf')}, {'a': True}, {'a': '0.2'}])
def test_FR16_invalid_probability_distribution_is_rejected(values):
    with pytest.raises(ValueError):
        ProximityResult(values, NOW, NOW, 'm1', 'c1')


def test_FR16_empty_projection_and_rounding_are_supported():
    assert ProximityResult({}, NOW, NOW, 'm1', 'c1').best is None
    assert ProximityResult({'a': 0.1, 'b': 0.2, 'c': 0.7}, NOW, NOW, 'm1', 'c1').best == ('c', 0.7)


@pytest.mark.parametrize('name,general', [('threshold_explicit_locker', 'threshold_locker'),
                                         ('threshold_explicit_light', 'threshold_light')])
def test_FR17_explicit_threshold_is_unconfigured_until_experiment(name, general):
    settings = Settings()
    assert getattr(settings, name) is None
    configured = Settings(**{name: 0.5})
    assert 0 < getattr(configured, name) < getattr(configured, general)


@pytest.mark.parametrize('name', ['threshold_explicit_locker', 'threshold_explicit_light'])
@pytest.mark.parametrize('value', [True, '0.5', float('nan'), float('inf'), -1, 0, 1])
def test_FR17_explicit_threshold_rejects_invalid_or_nonlower_values(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize('name,general', [('threshold_explicit_locker', 'threshold_locker'),
                                         ('threshold_explicit_light', 'threshold_light')])
def test_FR17_explicit_threshold_cannot_equal_general_threshold(name, general):
    with pytest.raises(ValueError):
        Settings(**{name: getattr(Settings(), general)})


def test_FR16_FR18_proximity_boundary_requires_place_and_trust_information():
    posterior = signature(ProximityEstimator.posterior)
    assert posterior.parameters['place_nodes'].kind.name == 'KEYWORD_ONLY'
    assert posterior.parameters['place_nodes'].default is signature(ProximityEstimator.posterior).empty
    assert not {'target', 'explicit', 'target_node_id'} & posterior.parameters.keys()
    observe = signature(ProximityEstimator.observe)
    assert observe.parameters['node'].kind.name == 'KEYWORD_ONLY'
