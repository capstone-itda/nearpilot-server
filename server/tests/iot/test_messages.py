"""MQTT v1 contract examples and invalid input boundaries."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nearpilot.iot.messages import (
    MessageValidationError, decode_message, encode_command, encode_safety_check,
)
from nearpilot.shared.enums import (
    Action, ActuatorState, Capability, NodeResultState, Occupancy, SafetyCheckOutcome,
)
from nearpilot.shared.models import (
    BeaconKey, CheckCriteria, NodeAnnounce, NodeCommand, NodeResult, NodeStatus,
    PhysicalState, RssiFrame, SafetyCheckRequest, SafetyCheckResult,
)

SESSION = "12345678-1234-1234-1234-123456789abc"
EVENT = "87654321-4321-4321-4321-cba987654321"
BEACON = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STAMP = "2026-10-15T03:00:00.123Z"
SENT = datetime.fromisoformat(STAMP)
RECEIVED = SENT + timedelta(seconds=1)
COMMON = dict(node_id="locker-1", session_id=SESSION, seq=7, sent_at=SENT, received_at=RECEIVED)


def body(kind):
    data = dict(schema_version=1, node_id="locker-1", session_id=SESSION, seq=7, sent_at=STAMP)
    data.update({
        "announce": dict(capability="locker", occupancy="rental"),
        "rssi": dict(frame_ts=STAMP, observations=[dict(uuid=BEACON, major=0, minor=65535, rssi=-62.5)]),
        "status": dict(observed_at=STAMP, physical_state=dict(
            door_closed=True, light_on=None, actuator_state="idle", sensor_fault=False)),
        "result": dict(command_id="command-1", event_id=EVENT, state="succeeded", occurred_at=STAMP),
        "safety_check_result": dict(check_id="check-1", event_id=EVENT,
            criteria_id="locker-return", criteria_version=1, checked_at=STAMP, outcome="passed",
            evidence={key: dict(value=True, observed_at=STAMP) for key in ("door_closed", "locked")}),
    }[kind])
    return data


def decode(kind, data=None):
    return decode_message(f"nearpilot/node/locker-1/{kind}",
        json.dumps(body(kind) if data is None else data).encode(), received_at=RECEIVED)


def test_FR04_announce_to_shared_model():
    assert decode("announce") == NodeAnnounce(**COMMON,
        capability=Capability.LOCKER, occupancy=Occupancy.RENTAL)


@pytest.mark.parametrize("capability", ["locker", "light", "speaker"])
@pytest.mark.parametrize("occupancy", ["rental", "shared"])
def test_FR04_all_capabilities_and_occupancies(capability, occupancy):
    data = body("announce") | dict(capability=capability, occupancy=occupancy)
    report = decode("announce", data)
    assert report.capability == Capability(capability)
    assert report.occupancy == Occupancy(occupancy)


def test_FR05_batch_preserves_metadata():
    data = body("rssi")
    data["observations"].append(dict(uuid=BEACON, major=65535, minor=0, rssi=-80))
    assert decode("rssi", data) == (
        RssiFrame(**COMMON, beacon=BeaconKey(BEACON, 0, 65535), rssi=-62.5, frame_ts=SENT),
        RssiFrame(**COMMON, beacon=BeaconKey(BEACON, 65535, 0), rssi=-80, frame_ts=SENT),
    )


def test_FR05_empty_observations_no_fake_frame():
    assert decode("rssi", body("rssi") | dict(observations=[])) == ()


@pytest.mark.parametrize("field,value", [
    ("uuid", BEACON.upper()), ("uuid", BEACON.replace("-", "")), ("uuid", "bad"),
    ("major", True), ("major", -1), ("major", 65536), ("major", 1.0),
    ("minor", False), ("minor", -1), ("minor", 65536),
    ("rssi", True), ("rssi", "-60"), ("rssi", None),
    ("rssi", float("nan")), ("rssi", float("inf")), ("rssi", -float("inf")),
])
def test_FR05_invalid_rssi_batch_rejected(field, value):
    data = body("rssi")
    data["observations"].append(data["observations"][0] | {field: value})
    with pytest.raises(MessageValidationError):
        decode("rssi", data)


def test_FR05_duplicate_beacon_rejects_complete_batch():
    data = body("rssi")
    data["observations"].append(data["observations"][0] | dict(rssi=-99))
    with pytest.raises(MessageValidationError):
        decode("rssi", data)


@pytest.mark.parametrize("observations", [None, {}, "", [None], [1]])
def test_FR05_invalid_observations_rejected(observations):
    with pytest.raises(MessageValidationError):
        decode("rssi", body("rssi") | dict(observations=observations))


def test_FR07_unobserved_light_is_none():
    assert decode("status") == NodeStatus(**COMMON, observed_at=SENT,
        physical=PhysicalState(True, None, ActuatorState.IDLE, False))


@pytest.mark.parametrize("value", [True, False, None])
def test_FR07_nullable_observations_preserved(value):
    data = body("status")
    for key in ("door_closed", "light_on", "sensor_fault"):
        data["physical_state"][key] = value
    report = decode("status", data)
    assert report.physical.door_closed is value
    assert report.physical.light_on is value
    assert report.physical.sensor_fault is value


def test_FR07_extra_fields_not_safety_evidence():
    data = body("status") | dict(safe=True, received_at="forged", detail={"safe": True})
    data["physical_state"]["safe"] = True
    report = decode("status", data)
    assert report == decode("status")
    assert not hasattr(report, "safe")
    assert report.detail == {}
    assert report.received_at is RECEIVED


@pytest.mark.parametrize("state", list(NodeResultState))
def test_FR07_result_event_preserved(state):
    data = body("result") | dict(state=state, error_code="DEVICE_BUSY" if state == "rejected" else "device-detail")
    assert decode("result", data) == NodeResult(**COMMON, command_id="command-1", event_id=EVENT,
        state=state, occurred_at=SENT, error_code=data["error_code"])


@pytest.mark.parametrize("error", [None, "", "NOT_SENT", "timeout", True, 1])
def test_FR07_rejected_requires_reject_code(error):
    with pytest.raises(MessageValidationError):
        decode("result", body("result") | dict(state="rejected", error_code=error))


@pytest.mark.parametrize("kind", ["announce", "rssi", "status", "result", "safety_check_result"])
@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("seq", True), ("seq", 0), ("seq", 1.0), ("seq", "1"),
    ("node_id", ""), ("node_id", 1), ("session_id", "bad"),
    ("sent_at", "2026-10-15T03:00:00"), ("sent_at", "2026-10-15T04:00:00+01:00"),
    ("sent_at", "2026-10-15 03:00:00Z"), ("sent_at", "2026-02-30T00:00:00Z"),
])
def test_FR07_invalid_payload_rejected(kind, field, value):
    with pytest.raises(MessageValidationError):
        decode(kind, body(kind) | {field: value})


@pytest.mark.parametrize("kind", ["announce", "rssi", "status", "result", "safety_check_result"])
def test_NFR11_every_required_field_is_required(kind):
    for key in body(kind):
        data = body(kind)
        del data[key]
        with pytest.raises(MessageValidationError):
            decode(kind, data)


@pytest.mark.parametrize("kind,field,value", [
    ("announce", "capability", "gate"), ("announce", "occupancy", "exclusive"),
    ("rssi", "frame_ts", "yesterday"), ("status", "observed_at", None),
    ("status", "physical_state", {}), ("result", "state", "timeout"),
    ("result", "command_id", ""), ("result", "event_id", "bad"),
    ("result", "occurred_at", "today"), ("result", "error_code", 1),
    ("safety_check_result", "criteria_version", True),
    ("safety_check_result", "criteria_version", 0),
    ("safety_check_result", "criteria_id", ""),
    ("safety_check_result", "check_id", None),
    ("safety_check_result", "outcome", "rejected"),
    ("safety_check_result", "checked_at", "today"),
    ("safety_check_result", "event_id", "bad"),
])
def test_NFR11_invalid_topic_specific_fields(kind, field, value):
    with pytest.raises(MessageValidationError):
        decode(kind, body(kind) | {field: value})


@pytest.mark.parametrize("field", ["door_closed", "light_on", "actuator_state", "sensor_fault"])
def test_FR07_physical_fields_are_required(field):
    data = body("status")
    del data["physical_state"][field]
    with pytest.raises(MessageValidationError):
        decode("status", data)


@pytest.mark.parametrize("field,value", [
    ("door_closed", 1), ("light_on", "true"), ("sensor_fault", 0),
    ("actuator_state", None), ("actuator_state", "AVAILABLE"),
])
def test_FR07_physical_types_are_strict(field, value):
    data = body("status")
    data["physical_state"][field] = value
    with pytest.raises(MessageValidationError):
        decode("status", data)


@pytest.mark.parametrize("raw", [b"\xff", b"[]", b"null", b"1", b"{} garbage", b"{", b'"text"',
    b'{"schema_version":1,"schema_version":1}', b'{"extra": NaN}', b'{"extra": 1e999}'])
def test_NFR11_invalid_json_rejected(raw):
    with pytest.raises(MessageValidationError):
        decode_message("nearpilot/node/locker-1/status", raw, received_at=RECEIVED)


@pytest.mark.parametrize("topic", ["other/node/locker-1/announce", "nearpilot/node//announce",
    "nearpilot/node/+/announce", "nearpilot/node/#/announce", "nearpilot/node/locker-1/result/extra",
    "nearpilot/node/locker-1/cmd", "nearpilot/node/locker-1/safety_check", "nearpilot/node/locker-1/unknown"])
def test_NFR08_invalid_report_topic(topic):
    with pytest.raises(MessageValidationError):
        decode_message(topic, json.dumps(body("announce")), received_at=RECEIVED)


def test_NFR08_topic_payload_identity_mismatch():
    with pytest.raises(MessageValidationError):
        decode("announce", body("announce") | dict(node_id="other-node"))


@pytest.mark.parametrize("received", [SENT.replace(tzinfo=None), SENT.astimezone(timezone(timedelta(hours=9))), None])
def test_NFR11_received_time_requires_utc(received):
    with pytest.raises(MessageValidationError):
        decode_message("nearpilot/node/locker-1/announce", json.dumps(body("announce")), received_at=received)


@pytest.mark.parametrize("criteria_id,items", [("locker-return", ("door_closed", "locked")),
    ("light-return", ("actuator_idle",)), ("speaker-return", ("actuator_idle",))])
@pytest.mark.parametrize("outcome", list(SafetyCheckOutcome))
def test_FR20_check_result_preserves_criteria_and_evidence(criteria_id, items, outcome):
    data = body("safety_check_result") | dict(criteria_id=criteria_id, outcome=outcome,
        evidence={key: dict(value=outcome == "passed", observed_at=STAMP) for key in items})
    assert decode("safety_check_result", data) == SafetyCheckResult(**COMMON,
        check_id="check-1", event_id=EVENT, criteria=CheckCriteria(criteria_id, 1),
        checked_at=SENT, outcome=outcome, evidence=data["evidence"])


@pytest.mark.parametrize("evidence", [{}, {"door_closed": {"value": True, "observed_at": STAMP}},
    {key: {"value": 1, "observed_at": STAMP} for key in ("door_closed", "locked")},
    {key: {"value": True} for key in ("door_closed", "locked")},
    {key: {"value": True, "observed_at": "yesterday"} for key in ("door_closed", "locked")},
    None, []])
@pytest.mark.parametrize("outcome", list(SafetyCheckOutcome))
def test_FR20_missing_evidence_never_passes(evidence, outcome):
    with pytest.raises(MessageValidationError):
        decode("safety_check_result", body("safety_check_result") | dict(evidence=evidence, outcome=outcome))


def test_FR20_values_and_unrecognized_criteria_are_left_to_core():
    data = body("safety_check_result")
    data["evidence"]["locked"]["value"] = False
    assert decode("safety_check_result", data).outcome == SafetyCheckOutcome.PASSED
    data.update(criteria_id="future-return", criteria_version=2, evidence={})
    report = decode("safety_check_result", data)
    assert report.criteria == CheckCriteria("future-return", 2)
    assert report.evidence == {}


def command():
    return NodeCommand("command-1", "locker-1", Action.OPEN, SESSION, SENT, RECEIVED)


def check():
    return SafetyCheckRequest("check-1", "locker-1", SESSION, CheckCriteria("locker-return", 1), SENT, RECEIVED)


@pytest.mark.parametrize("action", list(Action))
def test_FR06_encode_preserves_command(action):
    data = json.loads(encode_command(replace(command(), action=action)))
    assert datetime.fromisoformat(data.pop("issued_at")) == SENT
    assert datetime.fromisoformat(data.pop("expires_at")) == RECEIVED
    assert data == dict(
        schema_version=1, node_id="locker-1", command_id="command-1", action=action,
        target_session_id=SESSION)


def test_FR20_encode_preserves_check():
    assert json.loads(encode_safety_check(check())) == dict(schema_version=1, node_id="locker-1",
        check_id="check-1", target_session_id=SESSION, criteria_id="locker-return", criteria_version=1,
        issued_at="2026-10-15T03:00:00.123000Z", expires_at="2026-10-15T03:00:01.123000Z")


def test_NFR08_outbound_allowlist():
    assert set(json.loads(encode_command(command()))) == {
        "schema_version", "node_id", "command_id", "action", "target_session_id", "issued_at", "expires_at"}
    assert set(json.loads(encode_safety_check(check()))) == {
        "schema_version", "node_id", "check_id", "target_session_id", "criteria_id", "criteria_version", "issued_at", "expires_at"}


@pytest.mark.parametrize("field,value", [("node_id", ""), ("node_id", "other/node"),
    ("target_session_id", "bad"), ("issued_at", SENT.replace(tzinfo=None)), ("expires_at", None)])
@pytest.mark.parametrize("factory,encoder", [(command, encode_command), (check, encode_safety_check)])
def test_NFR11_invalid_outbound_fields_rejected(factory, encoder, field, value):
    with pytest.raises(MessageValidationError):
        encoder(replace(factory(), **{field: value}))


def test_NFR11_all_v1_message_shapes():
    assert isinstance(decode("announce"), NodeAnnounce)
    assert isinstance(decode("rssi")[0], RssiFrame)
    assert isinstance(decode("status"), NodeStatus)
    assert isinstance(decode("result"), NodeResult)
    assert isinstance(decode("safety_check_result"), SafetyCheckResult)
    assert json.loads(encode_command(command()))["schema_version"] == 1
    assert json.loads(encode_safety_check(check()))["schema_version"] == 1
