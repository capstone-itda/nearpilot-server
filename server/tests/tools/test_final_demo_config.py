"""Final demo configuration: seven devices, isolation, credentials and existing parsers."""
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from nearpilot.cli.seed import load_seed_file
from nearpilot.core.proximity.model import load_models
from nearpilot.runtime import load_config
from nearpilot.shared.enums import AccessMode, Role
from tools import final_demo
from tools.node_sim.config import load_config as load_fleet

EXPECTED = {"locker-1": ("locker", "rental"), "locker-2": ("locker", "rental"),
            "locker-3": ("locker", "rental"), "light-1": ("light", "shared"),
            "light-2": ("light", "shared"), "light-3": ("light", "shared"),
            "speaker-1": ("speaker", "shared")}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(final_demo, "executable", lambda name: name)

    def hash_passwords(args, **kwargs):
        path = Path(args[-1])
        names = [line.split(":")[0] for line in path.read_text(encoding="utf-8").splitlines()]
        path.write_text("".join(f"{name}:HASHED\n" for name in names), encoding="utf-8")
        return CompletedProcess(args, 0)

    monkeypatch.setattr(final_demo.subprocess, "run", hash_passwords)

    def generate(mode="hardware"):
        return final_demo.prepare(tmp_path / mode, host="127.0.0.1" if mode == "sim" else "10.106.106.199", mode=mode)
    return generate


def test_FR04_NFR10_final_demo_inventory(prepared):
    folder = prepared()
    fleet = load_fleet(folder / "fleet.json")
    assert {d.node_id: (d.capability, d.occupancy) for d in fleet.devices} == EXPECTED
    options = json.loads((folder / "lab.json").read_text(encoding="utf-8"))
    assert set(options["nodes"]) == set(EXPECTED)
    acl = (folder / "broker.acl").read_text(encoding="utf-8")
    for node in EXPECTED:
        block = acl.split(f"user {node}\n")[1].split("\n\n")[0]
        assert f"topic write nearpilot/node/{node}/announce" in block
        assert f"topic read nearpilot/node/{node}/cmd" in block
        assert "+" not in block and "#" not in block


def test_FR01_FR13_FR14_final_demo_seed(prepared):
    seed = load_seed_file(prepared() / "seed.toml")
    assert {(a.user_id, a.role) for a in seed.accounts} == {("demo-owner", Role.OWNER), ("demo-visitor", Role.VISITOR)}
    assert set(seed.places[0].nodes) == set(EXPECTED)
    assert set(n.node_id for n in seed.nodes) == set(EXPECTED)
    assert set(p.node_id for p in seed.policies) == set(EXPECTED)
    assert all(p.mode is AccessMode.OPEN for p in seed.policies)


def test_FR12A_final_demo_http(prepared):
    cfg = load_config(prepared() / "server.toml")
    assert cfg.dashboard is not None
    assert cfg.http.host == "0.0.0.0"
    assert cfg.http.public_hosts == ("10.106.106.199:8000",)
    assert cfg.database.path.parent == cfg.source.parent


def test_FR16_FR17_NFR12_final_demo_clock_and_model(prepared):
    for mode in ("hardware", "sim"):
        cfg = load_config(prepared(mode) / "server.toml")
        assert dict(cfg.delivery.clock_skew_sec) == (dict.fromkeys(EXPECTED, 0.0) if mode == "sim" else {})
        model = load_models(cfg.proximity.model_root, cfg.proximity.versions)["final-demo"]
        assert set(model.nodes) == set(EXPECTED)
        assert model.validation["source"] == "SYNTHETIC_NOT_MEASURED"
        assert model.validation["quality_verified"] is False
        assert cfg.settings.threshold_speaker is None
        assert cfg.settings.threshold_locker == 0.95
        assert cfg.settings.threshold_light == 0.75


def test_FR22_final_demo_preserves_existing_directory(prepared):
    folder = prepared()
    before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="이미"):
        prepared()
    assert before == {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_NFR08_final_demo_credentials(prepared):
    folder = prepared()
    public = "\n".join(p.read_text(encoding="utf-8-sig") for p in folder.rglob("*")
                       if p.is_file() and not p.name.startswith(".env."))
    passwords = [(folder / f".env.{name}").read_text(encoding="utf-8").strip()
                 for name in ("nearpilot-server", *EXPECTED)]
    assert len(set(passwords)) == 8
    assert all(password not in public for password in passwords)
    hashed = (folder / ".env.broker-passwords").read_text(encoding="utf-8")
    assert all(password not in hashed for password in passwords)


@pytest.mark.parametrize("host,mode,mqtt_port,http_port", [
    ("bad\naddress", "hardware", 18884, 8000), ("0.0.0.0", "hardware", 18884, 8000),
    ("10.106.106.199", "sim", 18884, 8000), ("127.0.0.1", "sim", 0, 8000),
    ("127.0.0.1", "sim", 18884, 18884),
])
def test_NFR12_final_demo_rejects_invalid_network(tmp_path, host, mode, mqtt_port, http_port):
    folder = tmp_path / "invalid"
    with pytest.raises(ValueError):
        final_demo.prepare(folder, host=host, mode=mode, mqtt_port=mqtt_port, http_port=http_port)
    assert not folder.exists()
