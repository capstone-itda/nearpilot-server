"""FR-13 FR-22 NFR-08 NFR-12 USB provisioning of the common node_lab firmware."""
import json

import pytest

from tools.node_lab import provision

MQTT_SECRET = "mqtt-secret-0123456789abcdef"
WIFI_SECRET = "wifi-secret-pass"


@pytest.fixture
def lab(tmp_path):
    folder = tmp_path / "lab"
    folder.mkdir()
    (folder / "lab.json").write_text(json.dumps({"nodes": ["light-1"]}), encoding="utf-8")
    (folder / "server.toml").write_text(
        '[mqtt]\nhost = "10.0.0.5"\nport = 18883\n[delivery.clock_skew_sec]\n"light-1" = 1.0\n', encoding="utf-8")
    (folder / ".env.light-1").write_text(MQTT_SECRET + "\n", encoding="utf-8")
    (folder / ".env.wifi.json").write_text(json.dumps({"ssid": "랩 핫스팟", "password": WIFI_SECRET}),
                                           encoding="utf-8")
    return folder


def values(folder, **overrides):
    options = dict(time_server="10.0.0.5", trust_sec=120, resync_sec=60) | overrides
    return provision.build_values(folder, "light-1", **options)


class FakeDevice:
    """Mirrors the firmware's line protocol, including restart after CONFIG_SAVED."""

    def __init__(self, *, reject=None, report=None):
        self.reject = reject
        self.report = report or {}
        self.staged = {}
        self.out = [b"BLE_BEACON uuid=x major=1 minor=2 rssi=-60.0 samples=3\n"]
        self.written = []

    def write(self, data):
        line = data.decode("ascii").rstrip("\n")
        self.written.append(line)
        words = line.split(" ")
        if words[0] == "CONFIG_BEGIN":
            self.out.append(f"CONFIG_READY nonce={words[1]}\n".encode())
        elif words[0] == "CONFIG_SET":
            if words[1] == self.reject:
                self.out.append(b"CONFIG_REJECTED reason=bad_value\n")
                return
            value = bytes.fromhex(words[2]).decode("utf-8")
            self.staged[words[1]] = value
            self.out.append(f"CONFIG_STAGED key={words[1]} bytes={len(value.encode())}\n".encode())
        elif words[0] == "CONFIG_COMMIT":
            self.out += [b"CONFIG_SAVED generation=1 node=light-1 restarting=1\n",
                         b"NODE_START node=light-1 config=loaded generation=1 mode=serial_only storage=1 "
                         b"reset_reason=software\n"]
        elif line == "CONFIG?":
            s = self.staged | self.report
            self.out.append((
                f"CONFIG_STATE configured=1 generation=1 node={s['node_id']} mqtt_user={s['mqtt_user']} "
                f"mqtt_host={s['mqtt_host']} mqtt_port={s['mqtt_port']} time_server={s['time_server']} "
                f"clock_max_error_sec={float(s['clock_max_error_sec']):.6f} time_trust_ms={s['time_trust_ms']} "
                f"time_resync_ms={s['time_resync_ms']} wifi_ssid_bytes={len(s['wifi_ssid'].encode())} "
                f"wifi_password_set={int(bool(s['wifi_password']))} mqtt_password_set=1\n").encode())

    def readline(self):
        return self.out.pop(0) if self.out else b""


def run(device, prepared):
    events = []
    state = provision.provision(device, prepared, timeout=0.5,
                                emit=lambda event, **data: events.append(dict(event=event, **data)))
    return state, events


def test_FR13_provision_rejects_unlisted_node(lab):
    with pytest.raises(ValueError, match="시험 목록"):
        provision.build_values(lab, "light-9", time_server="10.0.0.5", trust_sec=120, resync_sec=60)


def test_NFR08_provision_requires_password_files(lab):
    (lab / ".env.light-1").unlink()
    with pytest.raises(ValueError, match="비밀번호 파일"):
        values(lab)


def test_NFR12_provision_uses_server_clock_bound(lab):
    assert values(lab)["clock_max_error_sec"] == "1.0"
    (lab / "server.toml").write_text('[mqtt]\nhost = "10.0.0.5"\nport = 18883\n', encoding="utf-8")
    assert values(lab)["clock_max_error_sec"] == "-1"


@pytest.mark.parametrize("trust,resync", [(10, 10), (60, 120), (float("nan"), 60)])
def test_NFR12_provision_rejects_invalid_trust_window(lab, trust, resync):
    with pytest.raises(ValueError):
        values(lab, trust_sec=trust, resync_sec=resync)


def test_FR22_provision_roundtrip_verifies_readback(lab):
    prepared = values(lab)
    device = FakeDevice()
    state, events = run(device, prepared)
    assert state["node_id"] == "light-1" and state["time_trust_ms"] == 120000
    assert device.staged == prepared
    assert events[-1]["event"] == "provisioned"


def test_NFR08_provision_log_has_no_secrets(lab):
    prepared = values(lab)
    _, events = run(FakeDevice(), prepared)
    logged = json.dumps(events, ensure_ascii=False)
    for secret in (MQTT_SECRET, WIFI_SECRET, "랩 핫스팟"):
        assert secret not in logged
    assert "<28 bytes>" in logged  # length only


@pytest.mark.parametrize("device,reason", [
    (FakeDevice(reject="mqtt_host"), "거부"),
    (FakeDevice(report={"mqtt_host": "10.0.0.9"}), "mqtt_host"),
])
def test_FR22_provision_fails_on_reject_or_mismatch(lab, device, reason):
    with pytest.raises(ValueError, match=reason):
        run(device, values(lab))
    if device.reject:
        assert not any(line.startswith("CONFIG_COMMIT") for line in device.written)
