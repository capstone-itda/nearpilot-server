"""FR-22 lab generation, isolation and hardware clock defaults."""
import base64
import hashlib
import json

import pytest

from nearpilot.runtime import load_config
from tools.node_lab import setup
from tools.node_sim import load_config as load_fleet


@pytest.fixture
def generated(tmp_path):
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


def test_FR07_lab_uses_one_clock_bound(generated):
    """#236: 실기와 시뮬레이터 모두 같은 시각 오차 상한을 쓰고 기기별 표가 없다."""
    for mode in ("hardware", "sim"):
        folder = generated(mode)
        delivery = load_config(folder / "server.toml").delivery
        assert (delivery.clock_max_error_sec, delivery.clock_trust_sec, delivery.clock_resync_sec) == (0.5, 120, 60)
        assert "clock_skew_sec" not in (folder / "server.toml").read_text(encoding="utf-8")


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


def test_FR04_init_registers_locker_as_rental(tmp_path, generated):
    folder = setup.initialize(tmp_path / "locker", mode="hardware", host="10.0.0.5", port=18883,
        nodes=["locker-1", "light-1"], beacon_uuid="11111111-2222-3333-4444-555555555555", major=1, minor=2)
    fleet = json.loads((folder / "fleet.json").read_text(encoding="utf-8"))
    kinds = {d.node_id: (d.descriptor.kind, d.descriptor.occupancy.value)
             for d in load_fleet(folder / "fleet.json").devices}
    assert kinds == {"locker-1": ("locker", "rental"), "light-1": ("light", "shared")}
    assert all("capability" not in d and "occupancy" not in d for d in fleet["devices"])
    acl = (folder / "broker.acl").read_text(encoding="utf-8")
    assert "user locker-1" in acl


@pytest.mark.parametrize("nodes", [["speaker-1"], ["locker 1"], ["locker-1", "locker-1"]])
def test_FR04_init_rejects_unsupported_node_ids(tmp_path, generated, nodes):
    with pytest.raises(ValueError, match="기기 ID"):
        setup.initialize(tmp_path / "bad", mode="hardware", host="10.0.0.5", port=18883,
            nodes=nodes, beacon_uuid="11111111-2222-3333-4444-555555555555", major=1, minor=2)
    assert not (tmp_path / "bad").exists()


def test_NFR08_broker_hash_matches_mosquitto_format():
    salt = bytes(range(64))
    _, version, iterations, encoded_salt, digest = setup.broker_hash("secretpw", salt=salt).split("$")
    assert (version, iterations, base64.b64decode(encoded_salt)) == ("7", "1000", salt)
    assert base64.b64decode(digest) == hashlib.pbkdf2_hmac("sha512", b"secretpw", salt, 1000)


def test_NFR08_broker_hash_uses_random_salt():
    first, second = setup.broker_hash("secretpw"), setup.broker_hash("secretpw")
    assert first != second and "secretpw" not in first
    for hashed in (first, second):
        _, _, iterations, salt, digest = hashed.split("$")
        assert len(base64.b64decode(salt)) == 64
        assert base64.b64decode(digest) == hashlib.pbkdf2_hmac("sha512", b"secretpw", base64.b64decode(salt),
                                                               int(iterations))
