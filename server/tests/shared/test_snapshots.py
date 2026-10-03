"""FR-19/22 snapshots must survive later changes to shared input data."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import json
import operator

import pytest

from nearpilot.shared.enums import Action, Capability, DeviceState, Occupancy, Step, TrustState
from nearpilot.shared.models import AuditEvent, NodeInfo, NodeStatus, ProximityResult, ReserveRequest


NOW = datetime(2026, 10, 1)
NODE = NodeInfo(
    "locker-1", None, Capability.LOCKER, Occupancy.RENTAL,
    TrustState.TRUSTED, DeviceState.AVAILABLE, None, None,
)
RESERVATION = ReserveRequest("r1", "u1", "locker-1", Action.OPEN, None, None)


@pytest.mark.parametrize(
    ("template", "field_name"),
    [
        (NODE, "release_policy"),
        (NODE, "anchor_params"),
        (ProximityResult({}, NOW, NOW, "m0", "c0"), "posteriors"),
        (RESERVATION, "release_policy"),
        (NodeStatus("locker-1", NOW, True), "detail"),
        (AuditEvent("e1", Step.CLOSE, {}), "payload"),
    ],
)
def test_fr19_fr22_mappings_copy_inputs_and_reject_changes(template, field_name):
    source = {"value": 1}
    model = replace(template, **{field_name: source})
    snapshot = getattr(model, field_name)
    source["value"] = 2
    assert snapshot["value"] == 1
    with pytest.raises(TypeError):
        snapshot["value"] = 3


def test_fr19_nested_release_policy_keeps_creation_values():
    source = {"conditions": [{"safe": True}]}
    reservation = replace(RESERVATION, release_policy=source)
    source["conditions"][0]["safe"] = False
    source["conditions"].append({"safe": False})
    assert reservation.release_policy["conditions"] == ({"safe": True},)
    with pytest.raises(TypeError):
        reservation.release_policy["conditions"][0]["safe"] = False
    with pytest.raises(TypeError):
        reservation.release_policy["conditions"][0] = {"safe": False}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda mapping: operator.setitem(mapping, "seconds", 1),
        lambda mapping: operator.delitem(mapping, "seconds"),
        lambda mapping: mapping.clear(),
        lambda mapping: mapping.pop("seconds"),
        lambda mapping: mapping.popitem(),
        lambda mapping: mapping.setdefault("new", 1),
        lambda mapping: mapping.update({"seconds": 1}),
        lambda mapping: operator.ior(mapping, {"seconds": 1}),
    ],
)
def test_fr19_policy_rejects_dict_mutation_methods(mutate):
    reservation = replace(RESERVATION, release_policy={"seconds": 10})
    with pytest.raises(TypeError):
        mutate(reservation.release_policy)
    assert reservation.release_policy == {"seconds": 10}


def test_fr16_posterior_result_keeps_original_best_candidate():
    probabilities = {"locker-1": 0.97, "locker-2": 0.03}
    result = ProximityResult(probabilities, NOW, NOW, "m0", "c0")
    probabilities["locker-2"] = 1.0
    assert result.best == ("locker-1", 0.97)


def test_fr22_snapshots_support_dataclass_json_and_deepcopy():
    event = AuditEvent("e1", Step.CLOSE, {"conditions": [{"safe": True}]})
    assert json.loads(json.dumps(asdict(event))) == {
        "event_id": "e1", "step": "close",
        "payload": {"conditions": [{"safe": True}]},
        "internal_request_id": None, "command_id": None,
    }
    assert deepcopy(event) == event
