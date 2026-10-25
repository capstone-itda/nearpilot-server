"""FR-22 lab generation, isolation and hardware clock defaults."""
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from nearpilot.runtime import load_config
from tools.node_lab import setup


@pytest.fixture
def generated(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "executable", lambda name: name)

    def hash_passwords(args, **kwargs):
        path = Path(args[-1])
        users = [line.split(":")[0] for line in path.read_text(encoding="utf-8").splitlines()]
        path.write_text("".join(f"{user}:HASHED\n" for user in users), encoding="utf-8")
        return CompletedProcess(args, 0)

    monkeypatch.setattr(setup.subprocess, "run", hash_passwords)

    def generate(mode="sim", **kwargs):
        return setup.initialize(tmp_path / mode, mode=mode, host="127.0.0.1", port=18883,
            nodes=["light-1", "light-2"], beacon_uuid="11111111-2222-3333-4444-555555555555",
            major=1, minor=2, **kwargs)
    return generate


def test_FR22_init_refuses_existing_directory(generated):
    folder = generated()
    before = (folder / ".env.nearpilot-server").read_bytes()
    with pytest.raises(ValueError, match="이미"):
        generated()
    assert (folder / ".env.nearpilot-server").read_bytes() == before


def test_FR07_hardware_has_no_default_clock_skew(generated):
    hardware = load_config(generated("hardware") / "server.toml")
    assert not hardware.delivery.clock_skew_sec
    simulator = load_config(generated("sim") / "server.toml")
    assert dict(simulator.delivery.clock_skew_sec) == {"light-1": 0.0, "light-2": 0.0}


def test_FR16_lab_model_is_marked_synthetic_and_beacon_matches(generated):
    folder = generated()
    model = json.loads((folder / "models/node-lab/synthetic-1.json").read_text(encoding="utf-8"))
    assert model["validation"]["quality_verified"] is False
    lab = json.loads((folder / "lab.json").read_text(encoding="utf-8"))
    fleet = json.loads((folder / "fleet.json").read_text(encoding="utf-8"))
    for node in fleet["devices"]:
        assert all(node["scenario"]["beacons"][0][key] == value for key, value in lab["beacon"].items())


def test_NFR08_lab_config_does_not_embed_passwords(generated):
    folder = generated()
    public = "\n".join(path.read_text(encoding="utf-8") for path in folder.iterdir()
        if path.is_file() and not path.name.startswith(".env."))
    for name in ("nearpilot-server", "light-1", "light-2"):
        password = (folder / f".env.{name}").read_text(encoding="utf-8").strip()
        assert password not in public
        assert password not in (folder / ".env.broker-passwords").read_text(encoding="utf-8")


def test_NFR12_sim_clock_trust_rejects_remote_host(tmp_path):
    with pytest.raises(ValueError, match="localhost"):
        setup.initialize(tmp_path / "bad", mode="sim", host="192.168.1.2", port=18883,
            nodes=["light-1"], beacon_uuid="11111111-2222-3333-4444-555555555555", major=1, minor=2)
    assert not (tmp_path / "bad").exists()
