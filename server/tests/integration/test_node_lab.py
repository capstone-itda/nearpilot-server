"""Actual lab tool, broker, HMM, SQLite and simulated nodes; no API calls."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import socket
import time
from uuid import uuid4

import pytest

from nearpilot.shared.enums import Capability, Occupancy, ReasonCode, TrustState, Verdict
from nearpilot.shared.models import NodeAnnounce
from tools.node_lab.session import Lab, demo
from tools.node_lab.setup import broker_process, executable, initialize
from tools.node_sim.config import build_device, load_config as load_fleet


@pytest.fixture
def lab_world(tmp_path):
    try:
        executable("mosquitto")
        executable("mosquitto_passwd")
    except ValueError:
        pytest.skip("Real Mosquitto and mosquitto_passwd are required")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    folder = initialize(tmp_path / "lab", mode="sim", host="127.0.0.1", port=port,
        nodes=["light-1", "light-2"], beacon_uuid="11111111-2222-3333-4444-555555555555", major=1, minor=2)
    with ExitStack() as stack:
        process = stack.enter_context(broker_process(folder))
        time.sleep(0.2)
        assert process.poll() is None
        lab = Lab(folder, console=False)
        stack.callback(lab.close)
        lab.start()
        yield lab, stack


def test_FR06_lab_missing_beacon_blocks_command(lab_world):
    lab, _ = lab_world
    decision = lab.use("on")
    assert decision.verdict is Verdict.DENY
    assert decision.reason is ReasonCode.BEACON_NOT_SEEN
    assert not any(row["event"] == "command_result" for row in read_events(lab))


def read_events(lab):
    return [json.loads(line) for line in (lab.folder / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]


def test_FR13_lab_only_configures_listed_nodes(lab_world):
    lab, _ = lab_world
    stamp = datetime.now(timezone.utc)
    with lab.runtime.db.scope() as bound:
        bound.repo.upsert_announced_node(NodeAnnounce(
            "light-outsider", Capability.LIGHT, Occupancy.SHARED, stamp, str(uuid4()), 1, stamp))
    lab.snapshot()
    with lab.runtime.db.scope() as bound:
        outsider = next(node for node in bound.repo.nodes() if node.node_id == "light-outsider")
    assert outsider.trust_state is TrustState.DISCOVERED
    assert outsider.place_id is None


def test_FR05_FR06_FR16_FR22_lab_real_broker_command(lab_world):
    lab, stack = lab_world
    fleet = load_fleet(lab.folder / "fleet.json")
    devices = []
    for config in fleet.devices:
        device = build_device(fleet, config)
        stack.callback(device.node.store.close)
        stack.callback(device.stop)
        device.start()
        devices.append(device)
    demo(lab)  # Includes first-discovery restart and actual HMM probabilities.
    events = read_events(lab)
    assert any(event["event"] == "inventory_reloaded" for event in events)
    assert any(event["event"] == "proximity" and event["best"][0] == "light-1"
               and event["best"][1] >= lab.config.settings.threshold_light for event in events)
    outcomes = [event for event in events if event["event"] == "command_result"]
    assert [(event["action"], event["status"]) for event in outcomes] == [("on", "succeeded"), ("off", "succeeded")]
    assert sum(device.node.actuations for device in devices) == 2
    # The same external ID returns the stored outcome without a new physical execution.
    lab.use("off", request_id=outcomes[-1]["request_id"])
    assert sum(device.node.actuations for device in devices) == 2
    evidence = (lab.folder / "evidence.jsonl").read_text(encoding="utf-8")
    for path in lab.folder.glob(".env.*"):
        if path.name != ".env.broker-passwords":
            assert path.read_text(encoding="utf-8").strip() not in evidence
