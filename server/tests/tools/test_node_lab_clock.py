"""NFR-12 clock measurement must include transport delay and reject jumps."""
import pytest

from tools.node_lab.clock_probe import offset_interval


def test_NFR12_clock_interval_includes_usb_latency():
    # Host: 100.000 send, 100.020 receive. Device sampled 100.015 in between.
    low, high = offset_interval(100_000_000_000, 100_020_000_000, 20_000_000, 100_015_000,
        max_rtt_sec=0.5, jump_tolerance_sec=0.01)
    assert low == pytest.approx(-0.005)
    assert high == pytest.approx(0.015)


@pytest.mark.parametrize("end,elapsed,reason", [
    (102_020_000_000, 20_000_000, "host_clock_jump"),
    (99_990_000_000, 20_000_000, "host_clock_jump"),
    (100_600_000_000, 600_000_000, "round_trip_limit"),
])
def test_NFR12_clock_measurement_rejects_invalid_interval(end, elapsed, reason):
    with pytest.raises(ValueError, match=reason):
        offset_interval(100_000_000_000, end, elapsed, 100_015_000,
            max_rtt_sec=0.5, jump_tolerance_sec=0.01)
