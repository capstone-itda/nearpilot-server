"""Final demo configuration: seven devices, isolation, credentials and existing parsers."""
import json
import re
import shutil

import pytest

from nearpilot.cli.seed import load_seed_file
from nearpilot.core.proximity.common_model import load_model_version
from nearpilot.runtime import load_config
from nearpilot.shared.enums import AccessMode, Role
from tools import final_demo
from tools.node_sim.config import load_config as load_fleet

EXPECTED = {"locker-1": ("locker", "rental"), "locker-2": ("locker", "rental"),
            "locker-3": ("locker", "rental"), "light-1": ("light", "shared"),
            "light-2": ("light", "shared"), "light-3": ("light", "shared"),
            "speaker-1": ("speaker", "shared")}


@pytest.fixture
def prepared(tmp_path):
    def generate(mode="hardware"):
        return final_demo.prepare(tmp_path / mode, host="127.0.0.1" if mode == "sim" else "10.106.106.199", mode=mode)
    return generate


def test_FR04_NFR10_final_demo_inventory(prepared):
    folder = prepared()
    fleet = load_fleet(folder / "fleet.json")
    assert {d.node_id: (d.descriptor.kind, d.descriptor.occupancy.value) for d in fleet.devices} == EXPECTED
    options = json.loads((folder / "lab.json").read_text(encoding="utf-8"))
    assert set(options["nodes"]) == set(EXPECTED)
    acl = (folder / "broker.acl").read_text(encoding="utf-8")
    for node in EXPECTED:
        block = acl.split(f"user {node}\n")[1].split("\n\n")[0]
        assert f"topic write nearpilot/node/{node}/announce" in block
        assert f"topic read nearpilot/node/{node}/cmd" in block
        assert f"topic write nearpilot/node/{node}/time_request" in block  # #236
        assert f"topic read nearpilot/node/{node}/time" in block
        assert "+" not in block and "#" not in block
    server = acl.split("user nearpilot-server\n")[1].split("\n\n")[0]
    assert "topic read nearpilot/node/+/time_request" in server
    assert "topic write nearpilot/node/+/time" in server


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
        # 모든 기기에 같은 시각 오차 상한을 쓴다. 기기별 실측 표는 없다 (#236)
        assert (cfg.delivery.clock_max_error_sec, cfg.delivery.clock_trust_sec,
                cfg.delivery.clock_resync_sec) == (0.5, 120, 60)
        # 서버는 공통 모델 하나를 읽는다. 장소와 기기 정보가 없는 합성 모델이다 (FR-16)
        model = load_model_version(cfg.proximity.model_root, cfg.proximity.model_version)
        assert model.model_version == cfg.proximity.model_version == "synthetic-2"
        assert model.validation["source"] == "SYNTHETIC_NOT_MEASURED"
        assert model.validation["quality_verified"] is False
        assert cfg.settings.version == "final-demo-2"
        assert cfg.settings.threshold_speaker == 0.8
        assert cfg.settings.threshold_locker == 0.8
        assert cfg.settings.threshold_light == 0.8


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
    assert [line.split(":")[1].startswith("$7$") for line in hashed.splitlines()] == [True] * 8


def test_NFR12_final_demo_without_mosquitto_passwd(tmp_path, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if "mosquitto" in name else real_which(name, *a, **k))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "no-programs"))
    folder = final_demo.prepare(tmp_path / "demo", host="10.106.106.199")
    users = [line.split(":")[0] for line in (folder / ".env.broker-passwords").read_text(encoding="utf-8").splitlines()]
    assert users == ["nearpilot-server", *EXPECTED]


@pytest.mark.parametrize("mode,expected", [
    # 기기는 MQTT로 시각을 맞춘다 (#236). 실제 기기는 nearpilot.local을 mDNS로 찾는다 (#234)
    ("hardware", {("HTTP", "TCP", 8000), ("MQTT", "TCP", 18883), ("mDNS", "UDP", 5353)}),
    ("sim", {("HTTP", "TCP", 8000), ("MQTT", "TCP", 18883)}),
])
def test_NFR12_final_demo_firewall_rules(prepared, mode, expected):
    script = (prepared(mode) / "firewall.ps1").read_text(encoding="utf-8-sig")
    assert "-RemoteAddress LocalSubnet" in script and "@RULES@" not in script
    assert "#Requires -RunAsAdministrator" in script
    rules = {(name, protocol, int(port)) for name, protocol, port in
             re.findall(r"@\{Name='(\w+)'; Protocol='(\w+)'; Port=(\d+)\}", script)}
    assert rules == expected


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


@pytest.mark.parametrize("mode", ["hardware", "sim"])
def test_NFR12_final_demo_provisioning_only_for_hardware(prepared, mode):
    """#234: 실제 기기 구성만 nearpilot.local을 알리고 묶음을 준다. sim은 같은 PC라 알리지 않는다."""
    folder = prepared(mode)
    config = load_config(folder / "server.toml")
    if mode == "sim":
        assert config.provisioning is None
        return
    assert (config.provisioning.fleet, config.provisioning.mdns_name, config.provisioning.mdns_address) == (
        folder / "fleet.json", "nearpilot", "10.106.106.199")

