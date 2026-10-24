"""Inject one device's identity and network settings into the node firmware over USB.

Works with the product firmware (firmware/) and the node_lab firmware (firmware/node_lab/).

Run with PlatformIO's Python (pyserial). Secrets are read from the lab folder's .env files,
sent hex-encoded to the device and never written to the log or the console.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import time
import tomllib
from uuid import uuid4

KEYS = ("node_id", "mqtt_user", "mqtt_password", "wifi_ssid", "wifi_password",
        "mqtt_host", "mqtt_port", "time_server", "clock_max_error_sec", "time_trust_ms", "time_resync_ms")
# Required by the product firmware (firmware/); node_lab firmware answers unknown_key and goes on without them.
DEVICE_KEYS = ("capability", "occupancy")
UNKNOWN_KEY = "CONFIG_REJECTED reason=unknown_key"
# MQTT v1 announce values (architecture.md §5 MQTT 3절); this script runs without the server package.
CAPABILITIES = frozenset({"locker", "light", "speaker"})
OCCUPANCIES = frozenset({"rental", "shared"})
SECRETS = frozenset({"mqtt_password", "wifi_password"})
STATE = re.compile(
    r"CONFIG_STATE configured=([01]) generation=(\d+) node=(\S*) mqtt_user=(\S*) mqtt_host=(\S*) "
    r"mqtt_port=(\d+) time_server=(\S*) clock_max_error_sec=(-?[\d.]+) time_trust_ms=(\d+) "
    r"time_resync_ms=(\d+) wifi_ssid_bytes=(\d+) wifi_password_set=([01]) mqtt_password_set=([01])")
# lwIP SNTP polls no faster than 15 s; the firmware applies the same window.
TRUST_WINDOW_MS = (15_000, 86_400_000)


def build_values(folder: Path, node: str, *, time_server: str, trust_sec: float, resync_sec: float,
                 wifi_file: Path | None = None) -> dict[str, str]:
    """Collect values from the lab folder so server and device share the same clock bound."""
    folder = folder.resolve()
    options = json.loads((folder / "lab.json").read_text(encoding="utf-8"))
    if node not in options["nodes"]:
        raise ValueError("시험 목록에 없는 기기다. lab.json의 nodes를 확인한다")
    with (folder / "server.toml").open("rb") as stream:
        server = tomllib.load(stream)
    # The device announces what the server registered for it, never a value typed at the console.
    fleet = json.loads((folder / "fleet.json").read_text(encoding="utf-8"))
    device = next((item for item in fleet.get("devices", []) if item.get("node_id") == node), None)
    if device is None or device.get("capability") not in CAPABILITIES or device.get("occupancy") not in OCCUPANCIES:
        raise ValueError("fleet.json에 이 기기의 기능·점유 방식이 없거나 계약 밖의 값이다")
    password_path = folder / f".env.{node}"
    wifi_path = wifi_file or folder / ".env.wifi.json"
    if not password_path.is_file() or not wifi_path.is_file():
        raise ValueError("기기 비밀번호 파일 또는 Wi-Fi 파일이 없다")
    wifi = json.loads(wifi_path.read_text(encoding="utf-8"))
    if not isinstance(wifi.get("ssid"), str) or not isinstance(wifi.get("password"), str):
        raise ValueError("Wi-Fi 파일은 ssid와 password 문자열을 담는다")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", time_server):
        raise ValueError("시각원 주소가 올바르지 않다")
    windows = []
    for value in (trust_sec, resync_sec):
        millis = round(value * 1000)
        if not math.isfinite(value) or not TRUST_WINDOW_MS[0] <= millis <= TRUST_WINDOW_MS[1]:
            raise ValueError("시각 신뢰 기간과 재동기화 간격은 15초~1일이다")
        windows.append(millis)
    if windows[1] > windows[0]:
        raise ValueError("재동기화 간격은 시각 신뢰 기간보다 길 수 없다")
    skew = server.get("delivery", {}).get("clock_skew_sec", {}).get(node)
    mqtt = server["mqtt"]
    return dict(node_id=node, mqtt_user=node,
                mqtt_password=password_path.read_text(encoding="utf-8").strip(),
                wifi_ssid=wifi["ssid"], wifi_password=wifi["password"],
                mqtt_host=str(mqtt["host"]), mqtt_port=str(int(mqtt["port"])), time_server=time_server,
                # Unmeasured devices get -1: the firmware then never trusts its clock.
                clock_max_error_sec="-1" if skew is None else repr(float(skew)),
                time_trust_ms=str(windows[0]), time_resync_ms=str(windows[1]),
                capability=device["capability"], occupancy=device["occupancy"])


def public_view(values: dict[str, str]) -> dict[str, object]:
    return {key: (f"<{len(value.encode('utf-8'))} bytes>" if key in SECRETS or key == "wifi_ssid" else value)
            for key, value in values.items()}


def parse_state(line: str) -> dict[str, object] | None:
    match = STATE.fullmatch(line)
    if not match:
        return None
    names = ("configured", "generation", "node_id", "mqtt_user", "mqtt_host", "mqtt_port", "time_server",
             "clock_max_error_sec", "time_trust_ms", "time_resync_ms", "wifi_ssid_bytes",
             "wifi_password_set", "mqtt_password_set")
    raw = dict(zip(names, match.groups()))
    return {key: (float(value) if key == "clock_max_error_sec" else
                  value if key in ("node_id", "mqtt_user", "mqtt_host", "time_server") else int(value))
            for key, value in raw.items()}


def mismatches(values: dict[str, str], state: dict[str, object]) -> list[str]:
    expected = dict(configured=1, node_id=values["node_id"], mqtt_user=values["mqtt_user"],
        mqtt_host=values["mqtt_host"], mqtt_port=int(values["mqtt_port"]), time_server=values["time_server"],
        time_trust_ms=int(values["time_trust_ms"]), time_resync_ms=int(values["time_resync_ms"]),
        wifi_ssid_bytes=len(values["wifi_ssid"].encode("utf-8")),
        wifi_password_set=int(bool(values["wifi_password"])), mqtt_password_set=1)
    wrong = [key for key, value in expected.items() if state[key] != value]
    if abs(state["clock_max_error_sec"] - float(values["clock_max_error_sec"])) > 1e-6:
        wrong.append("clock_max_error_sec")
    return wrong


class ResponseTimeout(ValueError):
    pass


class Session:
    """Line protocol over a pyserial-like port: write(bytes), readline() -> bytes."""

    def __init__(self, port, *, timeout: float, emit):
        self.port = port
        self.timeout = timeout
        self.emit = emit

    def send(self, line: str) -> None:
        self.port.write((line + "\n").encode("ascii"))

    def expect(self, accept, *, what: str, tolerate: str | None = None, timeout: float | None = None) -> str:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            line = self.port.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if tolerate is not None and line == tolerate:
                return line
            if line.startswith("CONFIG_REJECTED"):
                self.emit("device_rejected", line=line)
                raise ValueError(f"기기가 설정을 거부했다: {line}")
            if line.startswith(("NODE_START", "CONFIG_SAVED", "CONFIG_MISSING")):
                self.emit("device_line", line=line)
            if accept(line):
                return line
        raise ResponseTimeout(f"기기 응답 시간 초과: {what}")


def begin(session: Session, nonce: str, *, timeout: float, interval: float) -> None:
    """Opening the port resets the board on Windows; a BEGIN sent while it boots is lost, so resend it."""
    deadline = time.monotonic() + timeout
    while True:
        session.send(f"CONFIG_BEGIN {nonce}")
        try:
            session.expect(lambda line: line == f"CONFIG_READY nonce={nonce}", what="CONFIG_READY",
                           timeout=min(interval, max(deadline - time.monotonic(), 0.0)))
            return
        except ResponseTimeout:
            if time.monotonic() >= deadline:
                raise


def provision(port, values: dict[str, str], *, timeout: float, emit,
              begin_interval: float = 1.0) -> dict[str, object]:
    session = Session(port, timeout=timeout, emit=emit)
    nonce = uuid4().hex
    begin(session, nonce, timeout=timeout, interval=begin_interval)
    for key in KEYS + DEVICE_KEYS:
        encoded = values[key].encode("utf-8")
        session.send(f"CONFIG_SET {key} {encoded.hex()}")
        reply = session.expect(lambda line: line == f"CONFIG_STAGED key={key} bytes={len(encoded)}", what=key,
                               tolerate=UNKNOWN_KEY if key in DEVICE_KEYS else None)
        if reply == UNKNOWN_KEY:
            # node_lab firmware: it keeps the session and announces its fixed capability.
            emit("key_unsupported", key=key)
            continue
        emit("staged", key=key, bytes=len(encoded))
    session.send(f"CONFIG_COMMIT {nonce}")
    saved = session.expect(lambda line: line.startswith("CONFIG_SAVED "), what="CONFIG_SAVED")
    session.expect(lambda line: line.startswith("NODE_START ") and " config=loaded " in line, what="restart")
    session.send("CONFIG?")
    state = parse_state(session.expect(lambda line: parse_state(line) is not None, what="CONFIG_STATE"))
    wrong = mismatches(values, state)
    if wrong:
        emit("readback_mismatch", keys=wrong)
        raise ValueError(f"다시 읽은 설정이 다르다: {', '.join(wrong)}")
    emit("provisioned", saved=saved, generation=state["generation"], values=public_view(values))
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True, help="시험 폴더 (lab.json, server.toml, .env.*)")
    parser.add_argument("--node", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--time-server", required=True)
    parser.add_argument("--time-trust-sec", type=float, required=True)
    parser.add_argument("--time-resync-sec", type=float, required=True)
    parser.add_argument("--wifi-file", type=Path, help="기본값: 시험 폴더의 .env.wifi.json")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--begin-interval", type=float, default=1.0,
                        help="부팅 중 잃은 CONFIG_BEGIN을 다시 보내는 간격(초)")
    args = parser.parse_args()
    values = build_values(args.dir, args.node, time_server=args.time_server, trust_sec=args.time_trust_sec,
                          resync_sec=args.time_resync_sec, wifi_file=args.wifi_file)
    import serial  # Optional diagnostic dependency, bundled with PlatformIO.
    with (args.dir / "provision.jsonl").open("a", encoding="utf-8") as log, \
            serial.Serial(args.port, 115200, timeout=0.2) as port:
        def emit(event, **data):
            record = dict(at=time.time(), event=event, node_id=args.node, **data)
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            print(json.dumps(record, ensure_ascii=False), flush=True)
        emit("provision_started", port=args.port, values=public_view(values))
        try:
            provision(port, values, timeout=args.timeout, emit=emit, begin_interval=args.begin_interval)
        except ValueError as error:
            emit("provision_failed", reason=str(error))
            raise SystemExit(1)


if __name__ == "__main__":
    main()
