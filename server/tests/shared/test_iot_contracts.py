"""#19·#31·#32·#37·#38 기기·IOT shared 계약 검사."""

from dataclasses import fields
from datetime import datetime, timedelta

import pytest

from nearpilot.shared.enums import (
    Action,
    CommandState,
    Capability,
    CommandOutcome,
    DeviceState,
    EndReason,
    NodeResultState,
    Occupancy,
    ReasonCode,
    RejectCode,
    SessionState,
    TrustState,
)
from nearpilot.shared.interfaces import (
    ExecutionStore,
    NodeCommandGateway,
    NodeEventSink,
    Repository,
)
from nearpilot.shared.models import (
    BeaconKey,
    CloseResult,
    CommandResult,
    NodeAnnounce,
    NodeCommand,
    NodeInfo,
    NodeResult,
    NodeStatus,
    RssiFrame,
    UseSession,
)

NOW = datetime(2026, 10, 13)
REPORT_FIELDS = {"node_id", "session_id", "seq", "sent_at", "received_at"}
RESULT = NodeResult(
    "locker-1", "sess-1", 3, NOW, NOW, "cmd-1", "evt-1", NodeResultState.SUCCEEDED, NOW,
)


@pytest.mark.parametrize(
    "model", [NodeAnnounce, RssiFrame, NodeStatus, NodeResult]
)
def test_fr04_fr05_fr07_device_reports_share_common_field_names(model):
    assert REPORT_FIELDS <= {f.name for f in fields(model)}


@pytest.mark.parametrize(
    "model",
    [NodeAnnounce, RssiFrame, NodeStatus, NodeResult, NodeCommand,
     CloseResult],
)
def test_nfr08_device_models_have_no_user_fields(model):
    names = {f.name for f in fields(model)}
    assert not names & {"user_id", "occupant", "occupant_user_id", "request_id", "use_id"} - (
        {"use_id"} if model is CloseResult else set()
    )


def test_fr05_rssi_frame_keeps_observed_and_received_times_apart():
    frame = RssiFrame(
        "locker-1", BeaconKey("u", 1, 2), -60.0, NOW, "sess-1", 7, NOW, NOW + timedelta(seconds=1),
    )
    assert frame.frame_ts != frame.received_at
    assert (frame.session_id, frame.seq) == ("sess-1", 7)


def test_fr07_node_result_state_matches_mqtt_values():
    assert {s.value for s in NodeResultState} == {
        "accepted", "running", "succeeded", "failed", "unknown", "rejected",
    }


def test_fr06_reject_codes_include_device_busy_and_differ_from_reason_code():
    assert {c.value for c in RejectCode} == {
        "SESSION_MISMATCH", "COMMAND_EXPIRED", "CLOCK_UNVERIFIED", "UNSUPPORTED_ACTION",
        "COMMAND_ID_CONFLICT", "INVALID_MESSAGE", "STORAGE_ERROR", "DEVICE_BUSY", "UNSUPPORTED_PARAMETER",
    }
    assert RejectCode.DEVICE_BUSY is not ReasonCode.DEVICE_BUSY


def test_fr07_node_result_carries_device_state_and_error_code_unchanged():
    rejected = NodeResult(
        "locker-1", "sess-1", 4, NOW, NOW, "cmd-2", "evt-2",
        NodeResultState.REJECTED, NOW, RejectCode.DEVICE_BUSY,
    )
    assert rejected.state is NodeResultState.REJECTED
    assert rejected.error_code == "DEVICE_BUSY"


def test_fr07_server_made_command_result_has_no_device_event_id():
    timeout = CommandResult("cmd-1", CommandOutcome.UNKNOWN, reported_at=NOW)
    reported = CommandResult("cmd-1", CommandOutcome.SUCCEEDED, RESULT, NOW)
    assert timeout.report is None
    assert reported.report.event_id == "evt-1"
    assert "event_id" not in {f.name for f in fields(CommandResult)}


def test_fr07_status_and_command_result_do_not_judge_safety():
    assert "safe" not in {f.name for f in fields(NodeStatus)}
    assert "safe" not in {f.name for f in fields(CommandResult)}
    status = NodeStatus(
        "locker-1", NOW, "sess-1", 1, NOW, NOW,
        CommandState.UNKNOWN,
    )
    assert {f.name for f in fields(NodeStatus)} >= {"command_state"}
    assert not {"physical", "door_closed", "light_on", "sensor_fault"} & {f.name for f in fields(NodeStatus)}
    assert {s.value for s in CommandState} == {"idle", "busy", "unknown"}


def test_fr06_node_command_targets_a_session_and_expires():
    command = NodeCommand(
        "cmd-1", "locker-1", Action.OPEN, "sess-1", NOW, NOW + timedelta(seconds=3),
    )
    assert command.target_session_id == "sess-1"
    assert command.issued_at < command.expires_at


def test_fr20_close_result_is_logical_close_without_check_evidence():
    record = CloseResult("use-1", NOW, EndReason.RELEASE)
    assert {f.name for f in fields(CloseResult)} == {"use_id", "closed_at", "end_reason"}
    assert record.end_reason == EndReason.RELEASE


def test_fr20_use_session_has_no_check_criteria():
    session = UseSession("use-1", "ir-1", "locker-1", SessionState.CLOSING,
                         release_policy={"check_criteria": {"id": "locker-door", "version": 1}})
    assert not hasattr(session, "check_criteria")


def test_fr04_node_info_exposes_current_session():
    node = NodeInfo(
        "locker-1", None, Capability.LOCKER, Occupancy.RENTAL,
        TrustState.TRUSTED, DeviceState.AVAILABLE, None, None,
    )
    assert node.session_id is None
    assert node.session_first_seen_at is None


def test_fr04_fr13_repository_has_announce_touch_and_session_contracts():
    for name in ("upsert_announced_node", "touch_node", "set_node_session", "save_rssi"):
        assert hasattr(Repository, name)


def test_fr20_execution_store_has_close_result():
    assert hasattr(ExecutionStore, "close_result")


def test_fr07_fr20_fake_sink_and_gateways_satisfy_protocols():
    class FakeSink:
        def on_announce(self, announce): ...

        def on_rssi(self, frame): ...

        def on_status(self, status): ...

        def on_result(self, result): ...

    class OldSink:
        def on_announce(self, announce): ...

        def on_rssi(self, frame): ...

        def on_status(self, status): ...

    class FakeGateway:
        def send(self, command, timeout_sec):
            return CommandResult(command.command_id, CommandOutcome.SUCCEEDED, RESULT, NOW)

    assert isinstance(FakeSink(), NodeEventSink)
    assert not isinstance(OldSink(), NodeEventSink)
    assert isinstance(FakeGateway(), NodeCommandGateway)
