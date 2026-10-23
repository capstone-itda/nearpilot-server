"""Read-only ESP32 clock measurement over USB; run with PlatformIO's Python."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time
from uuid import uuid4


SAMPLE = re.compile(r"CLOCK_SAMPLE nonce=([0-9a-f]{32}) unix_us=(\d+) synced=([01]) age_ms=(\d+) uptime_ms=(\d+)")


def offset_interval(start_ns, end_ns, elapsed_ns, device_us, *, max_rtt_sec, jump_tolerance_sec):
    """Device sampled its clock after host send and before host receive."""
    elapsed = elapsed_ns / 1e9
    if not 0 <= elapsed <= max_rtt_sec:
        raise ValueError("round_trip_limit")
    if abs((end_ns - start_ns) / 1e9 - elapsed) > jump_tolerance_sec:
        raise ValueError("host_clock_jump")
    if end_ns < start_ns:
        raise ValueError("host_clock_jump")
    return ((device_us * 1000 - end_ns) / 1e9, (device_us * 1000 - start_ns) / 1e9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--max-rtt", type=float, required=True)
    parser.add_argument("--jump-tolerance", type=float, required=True)
    parser.add_argument("--max-sync-age", type=float, required=True)
    parser.add_argument("--margin", type=float, required=True)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    if min(args.duration, args.interval, args.max_rtt, args.jump_tolerance, args.max_sync_age) <= 0 or args.margin < 0:
        parser.error("durations must be positive; margin must be nonnegative")
    import serial  # Optional diagnostic dependency, bundled with PlatformIO.
    records = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output, serial.Serial(args.port, 115200, timeout=0.1) as port:
        def emit(record):
            record["host_unix_ns"] = time.time_ns()
            output.write(json.dumps(record, allow_nan=False) + "\n")
            output.flush()
            if record["event"] != "serial":
                print(json.dumps(record), flush=True)
        emit(dict(event="measurement_started", settings=vars(args) | {"output": str(args.output)}))
        if args.reset:
            port.dtr = False
            port.rts = True
            time.sleep(0.15)
            port.rts = False
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            nonce = uuid4().hex
            start = time.time_ns()
            started = time.monotonic_ns()
            port.write(f"CLOCK? {nonce}\n".encode("ascii"))
            received = False
            while (time.monotonic_ns() - started) / 1e9 <= args.max_rtt:
                line = port.readline().decode("ascii", errors="replace").strip()
                end = time.time_ns()
                elapsed = time.monotonic_ns() - started
                if not line:
                    continue
                match = SAMPLE.fullmatch(line)
                if not match or match[1] != nonce:
                    if line.startswith(("CLOCK_SYNCED", "NODE_START", "COMMAND_", "MQTT_CONNECTED")):
                        emit(dict(event="serial", line=line))
                    continue
                received = True
                try:
                    low, high = offset_interval(start, end, elapsed, int(match[2]),
                        max_rtt_sec=args.max_rtt, jump_tolerance_sec=args.jump_tolerance)
                    if match[3] != "1" or int(match[4]) > args.max_sync_age * 1000:
                        raise ValueError("clock_not_synchronized_or_stale")
                except ValueError as error:
                    emit(dict(event="sample_rejected", reason=str(error)))
                    break
                record = dict(event="clock_sample", nonce=nonce, offset_low_sec=low, offset_high_sec=high,
                    rtt_sec=elapsed / 1e9, sync_age_ms=int(match[4]), uptime_ms=int(match[5]))
                records.append(record)
                emit(record)
                break
            if not received:
                emit(dict(event="sample_rejected", reason="response_timeout"))
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
        if not records:
            raise RuntimeError("No valid synchronized samples; do not enable clock trust")
        observed = max(max(abs(item["offset_low_sec"]), abs(item["offset_high_sec"])) for item in records)
        emit(dict(event="measurement_summary", valid_samples=len(records), observed_bound_sec=observed,
            proposed_lab_bound_sec=observed + args.margin, margin_sec=args.margin,
            min_sync_age_ms=min(item["sync_age_ms"] for item in records),
            max_sync_age_ms=max(item["sync_age_ms"] for item in records),
            scope="empirical bound for this lab run; not a production guarantee"))


if __name__ == "__main__":
    main()
