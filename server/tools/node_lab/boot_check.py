"""Watch node_lab boot and reconnect logs over USB; run with PlatformIO's Python (pyserial).

The check accepts only the contract order: Wi-Fi, trusted clock and MQTT subscription,
then the announce of that connection, then NODE_READY. Device readiness is not execution permission.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time

STAGES = {
    "NODE_START": re.compile(r"NODE_START node=(?P<node>\S+) config=(?P<config>\S+) .*reset_reason=(?P<reset>\S+)"),
    "CONFIG_MISSING": re.compile(r"CONFIG_MISSING\b.*"),
    "WIFI_CONNECTED": re.compile(r"WIFI_CONNECTED ip=(?P<ip>\S+).*"),
    "WIFI_DISCONNECTED": re.compile(r"WIFI_DISCONNECTED reason=(?P<reason>\d+)"),
    "WIFI_RETRY": re.compile(r"WIFI_RETRY .*"),
    "CLOCK_SOURCE": re.compile(r"CLOCK_SOURCE(_UNSET)? .*"),
    "CLOCK_SYNCED": re.compile(r"CLOCK_SYNCED .*"),
    "CLOCK_TRUSTED": re.compile(r"CLOCK_TRUSTED .*"),
    "CLOCK_TRUST_EXPIRED": re.compile(r"CLOCK_TRUST_EXPIRED .*"),
    "MQTT_CONNECTED": re.compile(r"MQTT_CONNECTED session_id=(?P<session>\S+).*"),
    "MQTT_CONNECT_FAILED": re.compile(r"MQTT_CONNECT_FAILED .*"),
    "MQTT_SUBSCRIBE_FAILED": re.compile(r"MQTT_SUBSCRIBE_FAILED .*"),
    "MQTT_DISCONNECTED": re.compile(r"MQTT_DISCONNECTED session_id=(?P<session>\S+).*"),
    "ANNOUNCE_DEFERRED": re.compile(r"ANNOUNCE_DEFERRED session_id=(?P<session>\S+).*"),
    "ANNOUNCED": re.compile(r"ANNOUNCED session_id=(?P<session>\S+)"),
    "NODE_READY": re.compile(r"NODE_READY session_id=(?P<session>\S+).*"),
    "NODE_NOT_READY": re.compile(r"NODE_NOT_READY reason=(?P<reason>\S+).*"),
    "COMMAND_RESULT": re.compile(r"COMMAND_RESULT .*"),
}


def parse(line: str) -> tuple[str, dict[str, str]] | None:
    for name, pattern in STAGES.items():
        match = pattern.fullmatch(line)
        if match:
            return name, match.groupdict()
    return None


def summarize(lines: list[tuple[float, str]]) -> dict[str, object]:
    """lines: (host seconds, serial line). Evaluates the last boot in the capture."""
    events = [(at, *parsed) for at, line in lines if (parsed := parse(line))]
    starts = [i for i, (_, name, _) in enumerate(events) if name == "NODE_START"]
    if not starts:
        return dict(ready=False, violations=["no_boot_observed"], stages={}, recoveries=[])
    events = events[starts[-1]:]
    origin = events[0][0]
    stages: dict[str, float] = {}
    violations: list[str] = []
    trusted = False
    connected: str | None = None
    announced: str | None = None
    ready = False
    outage: tuple[float, str] | None = None
    recoveries = []
    reasons: list[str] = []
    for at, name, fields in events:
        stages.setdefault(name, round(at - origin, 3))
        if name == "CLOCK_TRUSTED":
            trusted = True
        elif name == "CLOCK_TRUST_EXPIRED":
            trusted = False
        elif name == "MQTT_CONNECTED":
            connected, announced = fields["session"], None
        elif name == "MQTT_DISCONNECTED":
            connected = announced = None
        elif name == "ANNOUNCED":
            if not trusted:
                violations.append("announce_before_clock_trust")
            if fields["session"] != connected:
                violations.append("announce_for_other_connection")
            announced = fields["session"]
        elif name == "NODE_READY":
            if fields["session"] != announced or not trusted:
                violations.append("ready_before_announce_or_trust")
            if outage:
                recoveries.append(dict(reason=outage[1], seconds=round(at - outage[0], 3)))
                outage = None
            ready = True
        elif name == "NODE_NOT_READY":
            reasons.append(fields["reason"])
            if ready and outage is None:
                outage = (at, fields["reason"])
            ready = False
    start = events[0][2]
    return dict(ready=ready and not violations, violations=sorted(set(violations)), stages=stages,
                node=start.get("node"), config=start.get("config"), reset_reason=start.get("reset"),
                not_ready_reasons=sorted(set(reasons)), recoveries=recoveries,
                open_outage=dict(reason=outage[1], since=round(outage[0] - origin, 3)) if outage else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, required=True, help="관찰 시간(초)")
    parser.add_argument("--reset", action="store_true", help="USB 신호로 EN 재시작을 대신한다")
    parser.add_argument("--stop-when-ready", action="store_true")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("duration must be positive")
    import serial  # Optional diagnostic dependency, bundled with PlatformIO.
    lines: list[tuple[float, str]] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output, serial.Serial(args.port, 115200, timeout=0.2) as port:
        def emit(record):
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
        emit(dict(event="boot_check_started", at=time.time(), settings=vars(args) | {"output": str(args.output)}))
        if args.reset:
            port.dtr = False
            port.rts = True
            time.sleep(0.15)
            port.rts = False
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            line = port.readline().decode("utf-8", errors="replace").strip()
            if not line or not parse(line):
                continue  # Keep only known stage lines; the firmware prints no secrets in them.
            at = time.time()
            lines.append((at, line))
            emit(dict(event="serial", at=at, line=line))
            print(line, flush=True)
            if args.stop_when_ready and line.startswith("NODE_READY"):
                break
        summary = summarize(lines)
        emit(dict(event="boot_check_summary", at=time.time(), **summary))
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    raise SystemExit(0 if summary["ready"] else 1)


if __name__ == "__main__":
    main()
