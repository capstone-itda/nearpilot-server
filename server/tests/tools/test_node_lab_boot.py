"""FR-04 FR-13 boot order and readiness logs of the node_lab firmware and console."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from nearpilot.shared.enums import Capability, DeviceState, Occupancy, TrustState
from nearpilot.shared.models import NodeInfo
from tools.node_lab.boot_check import summarize
from tools.node_lab.session import Lab

BOOT = [
    "NODE_START node=light-1 config=loaded generation=1 mode=serial_only storage=1 reset_reason=external_pin",
    "WIFI_CONNECTED ip=10.0.0.7 rssi=-50",
    "MQTT_CONNECTED session_id=s1 subscribed=cmd,safety_check",
    "ANNOUNCE_DEFERRED session_id=s1 reason=clock_unverified",
    "CLOCK_SYNCED utc=2026-10-23T00:00:00.000000Z",
    "CLOCK_TRUSTED age_ms=3",
    "ANNOUNCED session_id=s1",
    "NODE_READY session_id=s1 clock_age_ms=5 uptime_ms=4000",
]


def lines(items, start=0.0):
    return [(start + i, line) for i, line in enumerate(items)]


def test_FR04_boot_check_accepts_contract_order():
    summary = summarize(lines(BOOT))
    assert summary["ready"] and not summary["violations"]
    assert summary["reset_reason"] == "external_pin"
    assert summary["stages"]["NODE_READY"] == 7.0


def test_FR04_boot_check_requires_contract_order():
    early = [BOOT[0], BOOT[1], BOOT[2], "ANNOUNCED session_id=s1",
             "NODE_READY session_id=s1 clock_age_ms=5 uptime_ms=4000"]
    summary = summarize(lines(early))
    assert not summary["ready"]
    assert "announce_before_clock_trust" in summary["violations"]


def test_FR04_boot_check_uses_last_boot_and_tracks_recovery():
    previous = lines(BOOT)
    again = BOOT + ["NODE_NOT_READY reason=wifi_disconnected uptime_ms=9000",
                    "MQTT_DISCONNECTED session_id=s1 error=-3",
                    "WIFI_CONNECTED ip=10.0.0.7 rssi=-50",
                    "MQTT_CONNECTED session_id=s2 subscribed=cmd,safety_check",
                    "ANNOUNCED session_id=s2",
                    "NODE_READY session_id=s2 clock_age_ms=5 uptime_ms=20000"]
    summary = summarize(previous + lines(again, start=100.0))
    assert summary["ready"]
    assert summary["recoveries"] == [dict(reason="wifi_disconnected", seconds=5.0)]


def test_FR07_boot_check_reports_clock_expiry_as_open_outage():
    summary = summarize(lines(BOOT + ["CLOCK_TRUST_EXPIRED age_ms=121000 trust_ms=120000",
                                      "NODE_NOT_READY reason=clock_trust_expired uptime_ms=125000"]))
    assert not summary["ready"]
    assert summary["open_outage"]["reason"] == "clock_trust_expired"


def test_FR04_boot_check_without_config_is_not_ready():
    summary = summarize(lines([
        "NODE_START node=- config=missing generation=0 mode=serial_only storage=1 reset_reason=power_on",
        "CONFIG_MISSING usb_provisioning_required network_not_started",
        "NODE_NOT_READY reason=config_missing"]))
    assert not summary["ready"] and summary["not_ready_reasons"] == ["config_missing"]


class Evidence:
    def __init__(self):
        self.records = []

    def emit(self, event, **data):
        self.records.append(dict(event=event, **data))


def test_FR13_lab_logs_readiness_changes_only():
    lab = Lab.__new__(Lab)
    lab.evidence = Evidence()
    lab._readiness = {}
    lab.config = SimpleNamespace(settings=SimpleNamespace(online_ttl_sec=15))
    current = datetime(2026, 10, 23, tzinfo=timezone.utc)
    node = NodeInfo("light-1", "node-lab", Capability.LIGHT, Occupancy.SHARED, TrustState.TRUSTED,
                    DeviceState.AVAILABLE, None, current, session_id="s1")
    for _ in range(3):
        lab.report_readiness(node, ready=True, blockers=(), current=current)
    faulted = NodeInfo("light-1", "node-lab", Capability.LIGHT, Occupancy.SHARED, TrustState.TRUSTED,
                       DeviceState.FAULT, None, current, session_id="s1")
    lab.report_readiness(faulted, ready=False, blockers=("current_status",), current=current)
    lab.report_readiness(faulted, ready=False, blockers=("current_status",),
                         current=current + timedelta(seconds=20))
    states = [(r["ready"], r["fault"], r["online"]) for r in lab.evidence.records]
    assert states == [(True, False, True), (False, True, True), (False, True, False)]
