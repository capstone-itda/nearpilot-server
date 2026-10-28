"""FR-04 FR-13 boot order and readiness logs of the node_lab firmware and console."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace

import pytest

from nearpilot.shared.enums import (
    Action, Capability, DeviceState, Occupancy, ReasonCode, RecoveryOutcome, RecoveryReason, RecoveryStatus,
    TrustState, Verdict,
)
from nearpilot.shared.models import Decision, NodeInfo, RecoveryApprovalResult, RecoveryReadiness
from tools.node_lab.boot_check import summarize
from tools.node_lab.session import Lab

BOOT = [
    "NODE_START node=light-1 config=loaded generation=1 mode=serial_only storage=1 reset_reason=external_pin",
    "WIFI_CONNECTED ip=10.0.0.7 rssi=-50",
    "MQTT_CONNECTED session_id=s1 subscribed=cmd",
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
                    "MQTT_CONNECTED session_id=s2 subscribed=cmd",
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

    def emit(self, event, *, show=True, **data):
        self.records.append(dict(event=event, show=show, **data))


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


def test_FR13_lab_console_thins_periodic_observations_only():
    lab = Lab.__new__(Lab)
    lab._shown_at = None
    lab.options = {"sample_sec": 1.0, "console_sec": 5.0}
    shown = [second for second in range(11) if lab.console_due(float(second))]
    assert shown == [0, 5, 10]  # 표본은 매초, 화면은 5초마다


def test_FR13_lab_console_shows_every_sample_without_console_sec():
    lab = Lab.__new__(Lab)
    lab._shown_at = None
    lab.options = {"sample_sec": 1.0}
    assert all(lab.console_due(float(second)) for second in range(5))


def test_FR13_lab_hidden_observation_still_written(tmp_path, capsys):
    from tools.node_lab.session import Evidence as FileEvidence
    evidence = FileEvidence(tmp_path / "evidence.jsonl")
    evidence.emit("node_observation", show=False, node_id="locker-1")
    evidence.emit("decision", verdict="OK")
    evidence.close()
    written = (tmp_path / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    assert [line.count('"node_observation"') + line.count('"decision"') for line in written] == [1, 1]
    printed = capsys.readouterr().out
    assert "decision" in printed and "node_observation" not in printed


class FakeCore:
    def __init__(self):
        self.requests, self.releases = [], []

    def request_use(self, request):
        self.requests.append(request)
        return Decision("", request.request_id, Verdict.OK, ReasonCode.OK, node_id="locker-1", use_id="use-1")

    def release_use(self, user_id, use_id):
        self.releases.append((user_id, use_id))
        return Decision("", "", Verdict.OK, ReasonCode.OK, use_id=use_id)


def console_lab():
    lab = Lab.__new__(Lab)
    lab.evidence = Evidence()
    lab.options = {"user_id": "lab-visitor"}
    lab.last_use_id = None
    lab._snapshot_lock = threading.RLock()
    lab.snapshot = lambda: {}
    lab.runtime = SimpleNamespace(core=FakeCore(), db=SimpleNamespace(scope=lambda: nullcontext(None)))
    return lab


def test_FR19_lab_open_targets_nearest_locker_and_release_uses_last_use():
    lab = console_lab()
    lab.use("open")
    request = lab.runtime.core.requests[-1]
    assert (request.target.capability, request.action) == (Capability.LOCKER, Action.OPEN)
    lab.release()
    assert lab.runtime.core.releases == [("lab-visitor", "use-1")]
    assert lab.last_use_id is None
    with pytest.raises(ValueError, match="use_id"):
        lab.release()


def test_FR09_lab_close_targets_nearest_locker_and_keeps_use():
    lab = console_lab()
    lab.use("open")
    lab.use("close")
    request = lab.runtime.core.requests[-1]
    assert (request.target.capability, request.action) == (Capability.LOCKER, Action.CLOSE)
    assert lab.last_use_id is not None  # 닫기는 반납이 아니다


class FakeRecovery:
    def __init__(self, status):
        self.status, self.calls = status, []

    def readiness(self, user_id, node_id):
        self.calls.append(("readiness", user_id, node_id))
        recovery_id = "rec-1" if self.status is not RecoveryStatus.NOT_READY else None
        return RecoveryReadiness(node_id, recovery_id, self.status, (), (), datetime(2026, 10, 27, tzinfo=timezone.utc))

    def approve(self, user_id, node_id, recovery_id):
        self.calls.append(("approve", user_id, node_id, recovery_id))
        readiness = self.readiness(user_id, node_id)
        return RecoveryApprovalResult(recovery_id, RecoveryOutcome.COMPLETED, RecoveryReason.OK, readiness)


def test_FR12A_lab_recover_approves_ready_fault_as_place_owner():
    lab = console_lab()
    lab.options["owner_id"] = "lab-owner"
    lab.runtime.recovery = FakeRecovery(RecoveryStatus.READY)
    result = lab.recover("locker-1")
    assert result.outcome is RecoveryOutcome.COMPLETED
    assert lab.runtime.recovery.calls[:2] == [
        ("readiness", "lab-owner", "locker-1"), ("approve", "lab-owner", "locker-1", "rec-1")]
    assert [r["event"] for r in lab.evidence.records] == ["recovery_readiness", "recovery_approval"]


def test_FR12A_lab_recover_does_not_approve_when_not_ready():
    lab = console_lab()
    lab.options["owner_id"] = "lab-owner"
    lab.runtime.recovery = FakeRecovery(RecoveryStatus.NOT_READY)
    readiness = lab.recover("locker-1")
    assert readiness.status is RecoveryStatus.NOT_READY
    assert [call[0] for call in lab.runtime.recovery.calls] == ["readiness"]


@pytest.mark.parametrize("action,target", [("open", "nearest:light"), ("close", "nearest:light"), ("on", "nearest:locker")])
def test_FR06_lab_rejects_action_for_other_capability(action, target):
    lab = console_lab()
    with pytest.raises(ValueError, match="시험 대상"):
        lab.use(action, target)
    assert not lab.runtime.core.requests
