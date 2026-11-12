"""Simulator device logic, records and client wiring without a broker.

Outbound messages are checked with the server's MQTT v1 decoder.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest

from nearpilot.iot.messages import decode_message
from nearpilot.shared.config import Settings
from nearpilot.shared.descriptor import legacy_descriptor
from nearpilot.shared.enums import Occupancy
from tools.node_sim import (
    Beacon, BrokerConfig, CommandStore, ConfigError, Credentials, Scenario, SimDevice, SimNode,
    acl_sections, load_config,
)
from tools.node_sim.__main__ import main

T0 = datetime(2026, 10, 16, 3, 0, 0, tzinfo=timezone.utc)
FLEET = Path(__file__).resolve().parents[2] / "tools" / "node_sim" / "fleet.example.json"
BEACON = "11111111-2222-3333-4444-555555555555"


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(tmp_path, clock):
    def make(descriptor="locker", *, name="node", connect=True, **scenario):
        store = CommandStore(tmp_path / f"{name}.sqlite3")
        node = SimNode("locker-1", descriptor, store,
                       Scenario(**({"actuation_sec": 0.1} | scenario)), clock=clock)
        if connect:
            node.connect()
        return node
    return make


def cmd(node, command_id="command-1", *, action=None, session=None, issued=None, expires=None,
        **extra):
    action = action or ("open" if node.kind == "locker" else "on")
    data = dict(schema_version=3, node_id=node.node_id, command_id=command_id, action=action,
                target_session_id=session or node.session_id,
                issued_at=iso(issued or T0 - timedelta(seconds=0.5)),
                expires_at=iso(expires or T0 + timedelta(seconds=3))) | extra
    return json.dumps(data).encode()


def decode(message):
    kind, body = message
    return decode_message(f"nearpilot/node/{body['node_id']}/{kind}", json.dumps(body).encode(),
                          received_at=T0 + timedelta(seconds=0.1))


def states(messages):
    return [(body["state"], body.get("error_code")) for kind, body in messages if kind == "result"]


def test_FR06_duplicate_runs_once(make):
    node = make()
    first, started = node.handle_command(cmd(node))
    assert started and states(first) == [("accepted", None), ("running", None)]
    again, started = node.handle_command(cmd(node))
    assert not started and states(again) == [("running", None)]
    final = node.finish()
    assert states(final) == [("succeeded", None)]
    repeat, started = node.handle_command(cmd(node))
    assert not started and node.actuations == 1
    report, original = decode(repeat[0]), decode(final[0])
    assert (report.event_id, report.state, report.occurred_at) == (
        original.event_id, original.state, original.occurred_at)
    assert report.seq > original.seq


def test_FR06_command_conflict_keeps_record(make):
    node = make()
    node.handle_command(cmd(node))
    before = node.store.command("command-1")
    conflict, started = node.handle_command(cmd(node, expires=T0 + timedelta(seconds=9)))
    assert not started and states(conflict) == [("rejected", "COMMAND_ID_CONFLICT")]
    assert conflict[0][1]["event_id"] != before.event_id
    assert node.store.command("command-1") == before
    assert node.handle_command(cmd(node))[0][0][1]["event_id"] == before.event_id


def test_FR06_busy_rejected_without_queue(make):
    node = make()
    node.handle_command(cmd(node))
    busy, started = node.handle_command(cmd(node, "command-2"))
    assert not started and states(busy) == [("rejected", "DEVICE_BUSY")]
    assert decode(busy[0]).error_code == "DEVICE_BUSY"
    node.finish()
    again, started = node.handle_command(cmd(node, "command-2"))
    assert not started and again[0][1]["event_id"] == busy[0][1]["event_id"]
    assert node.actuations == 1


@pytest.mark.parametrize("fields,code", [
    (dict(session="99999999-9999-9999-9999-999999999999"), "SESSION_MISMATCH"),
    (dict(issued=T0 - timedelta(seconds=5), expires=T0 - timedelta(seconds=1)),
     "COMMAND_EXPIRED"),
    (dict(action="on"), "UNSUPPORTED_ACTION"),
    (dict(issued=T0 + timedelta(seconds=3)), "INVALID_MESSAGE"),
    (dict(schema_version=1), "INVALID_MESSAGE"),
    (dict(schema_version=2), "INVALID_MESSAGE"),  # cmd is v3 once the device announced v3 (#245)
    (dict(issued=T0 + timedelta(seconds=1), expires=T0 + timedelta(seconds=2)),
     "INVALID_MESSAGE"),
])
def test_FR06_expired_or_wrong_session_rejected(make, fields, code):
    node = make()
    messages, started = node.handle_command(cmd(node, **fields))
    assert not started and states(messages) == [("rejected", code)]
    assert node.actuations == 0 and node.store.command("command-1").error_code == code


def test_FR06_old_duplicate_reports_stored_result(make, clock):
    node = make()
    node.handle_command(cmd(node))
    node.finish()
    clock.now += timedelta(seconds=60)  # Expired and possibly another session.
    node.connect()
    record = node.store.command("command-1")
    resend = json.loads(record.content) | dict(schema_version=3, command_id="command-1")
    messages, started = node.handle_command(json.dumps(resend).encode())
    assert not started and states(messages) == [("succeeded", None)]
    assert messages[0][1]["event_id"] == record.event_id


def test_FR06_storage_error_prevents_actuation(make):
    node = make(storage_error=True)
    messages, started = node.handle_command(cmd(node))
    assert not started and states(messages) == [("rejected", "STORAGE_ERROR")]
    assert node.actuations == 0 and not node.busy and node.last_output is None
    node.store.fail_writes = False
    assert node.store.command("command-1") is None


def test_FR07_restart_preserves_record(make):
    node = make(name="restart")
    node.handle_command(cmd(node))
    payload = cmd(node)
    node.store.close()  # Power loss while running.
    restarted = make(name="restart", connect=False)
    messages = restarted.connect()
    assert [kind for kind, _ in messages] == ["announce", "result"]
    assert states(messages) == [("unknown", None)]
    again, started = restarted.handle_command(payload)
    assert not started and states(again) == [("unknown", None)]
    assert again[0][1]["event_id"] == messages[1][1]["event_id"]
    assert restarted.actuations == 0
    assert restarted.connect()[1:] == []  # Reported once after the restart.


@pytest.mark.parametrize("final,drop,expected", [
    ("succeeded", True, []), ("failed", False, ["failed"]), ("unknown", False, ["unknown"]),
])
def test_FR07_final_scenarios(make, final, drop, expected):
    node = make(final=final, drop_final_result=drop)
    node.handle_command(cmd(node))
    assert [s for s, _ in states(node.finish())] == expected
    assert node.store.command("command-1").state == final
    assert node.last_output is None if final != "succeeded" else node.last_output == "open"


def test_FR05_empty_and_multiple_beacons(make):
    empty = make(name="empty")
    assert decode(empty.rssi()) == ()
    node = make(name="many", beacons=[Beacon(BEACON, 1, 1, -60.5), Beacon(BEACON, 1, 2, -71)])
    frames = decode(node.rssi())
    assert [(f.beacon.minor, f.rssi) for f in frames] == [(1, -60.5), (2, -71)]
    assert decode(node.rssi())[0].seq == frames[0].seq + 1


def test_FR04_reconnect_announces_new_session(make):
    node = make(connect=False)
    first = decode(node.connect()[0])
    node.status()
    second_messages = node.connect()
    second = decode(second_messages[0])
    assert first.session_id != second.session_id
    assert (first.seq, second.seq) == (1, 1)
    assert decode(node.status()).seq == 1
    assert (second.descriptor.kind, second.occupancy.value) == ("locker", "rental")
    body = second_messages[0][1]
    assert body["schema_version"] == 3 and "capability" not in body
    # The template is the declaration a v2 simulator became on the server: the trust is kept (spec decision 10).
    assert second.descriptor.hash == legacy_descriptor("locker", Occupancy.RENTAL).hash


RING = {
    "format": "nearpilot.device/1", "kind": "light", "occupancy": "shared",
    "vendor": {"name": "nearpilot", "model": "light", "firmware": "sim"},
    "actions": [
        {"name": "on", "description": "조명을 켜고 밝기와 색을 정한다",
         "input_schema": {"type": "object", "properties": {
             "brightness": {"type": "integer", "minimum": 0, "maximum": 64},
             "color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}},
             "required": [], "additionalProperties": False},
         "result": {"kind": "signal_output"}},
        {"name": "off", "description": "조명을 끈다", "role": "deactivate",
         "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
         "result": {"kind": "signal_output"}},
    ],
}


def test_FR06_sim_accepts_close_and_arguments(make):
    """사물함 템플릿은 close(reoperate)를 받는다. 선언 객체의 인자는 스키마 안이면 받는다."""
    locker = make(name="locker")
    messages, started = locker.handle_command(cmd(locker, action="close"))
    assert started and states(messages) == [("accepted", None), ("running", None)]
    light = make(RING, name="ring")
    assert light.kind == "light" and light.occupancy == "shared"
    messages, started = light.handle_command(cmd(light, arguments={"brightness": 30, "color": "#00ff00"}))
    assert started
    light.finish()
    # Same id, other arguments: another content (FR-22).
    conflict, _ = light.handle_command(cmd(light, arguments={"brightness": 31, "color": "#00ff00"}))
    assert states(conflict) == [("rejected", "COMMAND_ID_CONFLICT")]
    same, _ = light.handle_command(cmd(light, arguments={"color": "#00ff00", "brightness": 30}))
    assert states(same) == [("succeeded", None)] and light.actuations == 1


@pytest.mark.parametrize("descriptor,fields,code", [
    ("locker", dict(action="on"), "UNSUPPORTED_ACTION"),
    ("light", dict(action="spin"), "UNSUPPORTED_ACTION"),
    ("light", dict(arguments={"brightness": 10}), "UNSUPPORTED_PARAMETER"),  # The template takes none.
    (RING, dict(arguments={"brightness": 65}), "UNSUPPORTED_PARAMETER"),
    (RING, dict(arguments={"color": "blue"}), "UNSUPPORTED_PARAMETER"),
    (RING, dict(arguments={"volume": 3}), "UNSUPPORTED_PARAMETER"),
    (RING, dict(arguments=[30]), "UNSUPPORTED_PARAMETER"),
    (RING, dict(action="off", arguments={"brightness": 1}), "UNSUPPORTED_PARAMETER"),
])
def test_FR06_sim_rejects_outside_descriptor(make, descriptor, fields, code):
    node = make(descriptor)
    messages, started = node.handle_command(cmd(node, **fields))
    assert not started and states(messages) == [("rejected", code)] and node.actuations == 0


def test_FR04_sim_descriptor_config(tmp_path):
    """기기 목록의 `descriptor`는 템플릿 이름이나 선언 객체다. 그 밖은 설정 오류다."""
    data = json.loads(FLEET.read_text(encoding="utf-8"))
    data["devices"][3]["descriptor"] = RING
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = {d.node_id: d.descriptor for d in load_config(path).devices}
    assert loaded["light-1"].action("on").properties["brightness"]["maximum"] == 64
    assert loaded["locker-1"].hash == legacy_descriptor("locker", Occupancy.RENTAL).hash
    for bad in ("door", {"kind": "light"}, None, {**RING, "occupancy": "lease"}):
        data["devices"][0]["descriptor"] = bad
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)
    data["devices"][0]["descriptor"] = "locker"
    data["devices"][0]["capability"] = "locker"  # The old key alone is not enough.
    del data["devices"][0]["descriptor"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_FR20_simulator_has_no_release_check(make):
    """반납 검사 요청과 결과를 처리하지 않는다 (2026-10-26 결정)."""
    node = make()
    assert not hasattr(node, "handle_check") and not hasattr(node.store, "check")


def test_FR07_status_reports_command_state_only(make):
    node = make()
    idle = node.status()[1]
    assert decode(node.status()).command_state.value == "idle"
    assert "physical_state" not in idle and "safe" not in idle and idle["schema_version"] == 2
    node.handle_command(cmd(node))
    assert decode(node.status()).command_state.value == "busy"
    node.finish()
    assert decode(node.status()).command_state.value == "idle"


@pytest.mark.parametrize("kind,action", [("light", "on"), ("light", "off"), ("speaker", "on")])
def test_FR07_output_completion_is_success(make, kind, action):
    """실제 상태를 관측하지 않아도 출력 완료는 성공이다. IR 무드등도 같다."""
    node = make(kind, name=kind + action)
    node.handle_command(cmd(node, action=action))
    assert states(node.finish()) == [("succeeded", None)]
    assert node.last_output == action


def test_NFR10_eight_device_scenarios():
    fleet = load_config(FLEET)
    devices = fleet.devices
    assert len(devices) == 8
    assert {d.place for d in devices} == {"place-a", "place-b"}
    by_kind = {}
    for d in devices:
        by_kind.setdefault((d.place, d.descriptor.kind), []).append(d.node_id)
    assert {k: len(v) for k, v in by_kind.items()} == {
        ("place-a", "locker"): 3, ("place-a", "light"): 3, ("place-a", "speaker"): 1,
        ("place-b", "light"): 1}
    mood = next(d for d in devices if d.place == "place-b")
    assert not hasattr(mood.scenario, "light_observable")
    assert len({d.username for d in devices}) == len({str(d.store) for d in devices}) == 8


def test_NFR10_config_rejects_invalid_fleet(tmp_path):
    data = json.loads(FLEET.read_text(encoding="utf-8"))
    data["devices"][1]["username"] = data["devices"][0]["username"]
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    data = json.loads(FLEET.read_text(encoding="utf-8"))
    del data["devices"][0]["scenario"]["actuation_sec"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    assert main(["run", "--config", str(path)], write=lambda *a, **k: None) == 2


class FakeClient:
    def __init__(self, **options):
        self.options = options
        self.published = []
        self.raw = []
        self.subscribed = []

    def reconnect_delay_set(self, minimum, maximum):
        self.backoff = (minimum, maximum)

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def connect_async(self, host, port, keepalive):
        self.address = (host, port, keepalive)

    def loop_start(self):
        return 0

    def loop_stop(self):
        return 0

    def disconnect(self):
        return 0

    def subscribe(self, topics):
        self.subscribed.append(topics)
        return 0, 1

    def publish(self, topic, payload, qos, retain):
        self.raw.append((topic, payload))
        self.published.append((topic.rsplit("/", 1)[-1], json.loads(payload), qos, retain))


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def device(tmp_path):
    secret = tmp_path / ".env.node-locker-1"
    secret.write_text("node-private-password\n", encoding="utf-8")
    node = SimNode("locker-1", "locker", CommandStore(tmp_path / "d.sqlite3"), Scenario(actuation_sec=0))
    created = SimDevice(node, BrokerConfig("127.0.0.1", 1883, 30, 1, 4),
                        Credentials("locker-1", secret),
                        Settings(rssi_frame_sec=5.0, status_report_sec=5.0, beacon_recent_sec=5.0, anchor_recent_sec=5.0),
                        client_factory=FakeClient)
    yield created
    created.stop()


def test_NFR11_simulator_v2_qos_retain(device):
    client = device._client
    assert client.options["protocol"] == mqtt.MQTTv311 and client.options["clean_session"]
    device.start()
    device._on_connect(client, None, None, 0, None)
    assert wait_for(lambda: {k for k, *_ in client.published} >= {"announce", "status", "rssi"})
    assert client.subscribed == [[("nearpilot/node/locker-1/cmd", 1)]]
    node = device.node
    device._on_message(client, None, SimpleNamespace(
        topic="nearpilot/node/locker-1/cmd", payload=cmd(node, issued=datetime.now(timezone.utc)
        - timedelta(seconds=1), expires=datetime.now(timezone.utc) + timedelta(seconds=5))))
    assert wait_for(lambda: any(b.get("state") == "succeeded" for _, b, *_ in client.published))
    expected = {"announce": (1, True), "rssi": (0, False), "status": (1, False),
                "result": (1, False)}
    seen = {kind: (qos, retain) for kind, _, qos, retain in client.published}
    assert seen == expected
    assert client.published[0][0] == "announce"


def test_NFR08_simulator_uses_node_credentials(device, caplog):
    with caplog.at_level(logging.DEBUG):
        device.start()
        device._on_connect(device._client, None, None, 5, None)  # Rejected credentials.
    assert device._client.credentials == ("locker-1", "node-private-password")
    assert "node-private-password" not in caplog.text + repr(device._credentials)
    assert not device.connected.is_set() and device._client.published == []
    fleet = load_config(FLEET)
    text = acl_sections(fleet)
    for d in fleet.devices:
        assert f"user {d.username}\ntopic write nearpilot/node/{d.node_id}/announce" in text
        assert f"topic read nearpilot/node/{d.node_id}/cmd" in text
    assert "+" not in text and "#" not in text and "topic write nearpilot/node/" in text
    assert all(line.startswith("topic read") for line in text.splitlines()
               if line.endswith("/cmd"))  # No device writes commands.
    assert "safety_check" not in text
    assert all(".env." in str(d.password_file) for d in fleet.devices)


def test_FR04_simulator_display_announce_within_limit(tmp_path):
    """동반 기기 display 선언(동작 8개)의 발표가 서버의 2048바이트 상한 안에서 받아진다 (iot-smartthings-companion).

    시뮬레이터는 펌웨어처럼 공백 없는 UTF-8 JSON 으로 발행한다. 서버 디코더가 같은 바이트를 잰다.
    """
    from nearpilot.iot.messages import decode_message
    from nearpilot.iot.smartthings.actions import ACTIONS
    from nearpilot.shared.descriptor import ANNOUNCE_MAX_BYTES

    descriptor = {"format": "nearpilot.device/1", "kind": "display", "occupancy": "shared",
                  "vendor": {"name": "nearpilot", "model": "display", "firmware": "v3"},
                  "actions": [dict(a.declaration) for a in ACTIONS.values()]}
    node_id = "d" * 64  # 가장 긴 기기 ID
    secret = tmp_path / ".env.node-display"
    secret.write_text("node-private-password\n", encoding="utf-8")
    node = SimNode(node_id, descriptor, CommandStore(tmp_path / "d.sqlite3"), Scenario(actuation_sec=0))
    device = SimDevice(node, BrokerConfig("127.0.0.1", 1883, 30, 1, 4), Credentials(node_id, secret),
                       Settings(rssi_frame_sec=5.0, status_report_sec=5.0, beacon_recent_sec=5.0,
                                anchor_recent_sec=5.0), client_factory=FakeClient)
    try:
        device.start()
        device._on_connect(device._client, None, None, 0, None)
        assert wait_for(lambda: any(t.endswith("/announce") for t, _ in device._client.raw))
        topic, payload = next((t, p) for t, p in device._client.raw if t.endswith("/announce"))
    finally:
        device.stop()
    assert isinstance(payload, bytes) and len(payload) <= ANNOUNCE_MAX_BYTES
    assert "켜기".encode("utf-8") in payload  # ASCII 이스케이프하지 않는다
    announce = decode_message(topic, payload, received_at=datetime.now(timezone.utc))
    assert [a.name for a in announce.descriptor.actions] == list(ACTIONS)
