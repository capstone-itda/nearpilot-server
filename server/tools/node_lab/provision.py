"""Inject one device's identity and network settings into the product firmware (firmware/) over USB.

The firmware takes its clock from the server over MQTT (#236), so no clock value is injected.
The node_lab firmware (firmware/node_lab/) still needs the old clock keys and is not supported.

Run with PlatformIO's Python (pyserial). Secrets are read from the lab folder's .env files,
sent hex-encoded to the device and never written to the log or the console.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time
import tomllib
from uuid import uuid4

# Connection and Wi-Fi only: the firmware declares its kind and occupancy itself (#245).
KEYS = ("node_id", "mqtt_user", "mqtt_password", "wifi_ssid", "wifi_password", "mqtt_host", "mqtt_port")
SECRETS = frozenset({"mqtt_password", "wifi_password"})
STATE = re.compile(
    r"CONFIG_STATE configured=([01]) generation=(\d+) node=(\S*) mqtt_user=(\S*) mqtt_host=(\S*) "
    r"mqtt_port=(\d+) wifi_ssid_bytes=(\d+) wifi_password_set=([01]) mqtt_password_set=([01])")


def build_values(folder: Path, node: str, *, wifi_file: Path | None = None) -> dict[str, str]:
    """Collect values from the lab folder: the server's broker address, the device's account and Wi-Fi."""
    folder = folder.resolve()
    options = json.loads((folder / "lab.json").read_text(encoding="utf-8"))
    if node not in options["nodes"]:
        raise ValueError("시험 목록에 없는 기기다. lab.json의 nodes를 확인한다")
    with (folder / "server.toml").open("rb") as stream:
        server = tomllib.load(stream)
    # Only devices the lab registered get an account; the firmware's declaration says what it is.
    fleet = json.loads((folder / "fleet.json").read_text(encoding="utf-8"))
    if not any(item.get("node_id") == node for item in fleet.get("devices", [])):
        raise ValueError("fleet.json에 이 기기가 없다")
    password_path = folder / f".env.{node}"
    wifi_path = wifi_file or folder / ".env.wifi.json"
    if not password_path.is_file() or not wifi_path.is_file():
        raise ValueError("기기 비밀번호 파일 또는 Wi-Fi 파일이 없다")
    wifi = json.loads(wifi_path.read_text(encoding="utf-8"))
    if not isinstance(wifi.get("ssid"), str) or not isinstance(wifi.get("password"), str):
        raise ValueError("Wi-Fi 파일은 ssid와 password 문자열을 담는다")
    mqtt = server["mqtt"]
    # With [provisioning] the device finds the broker by mDNS, the same host the app's BLE setup injects (#234).
    provisioning = server.get("provisioning")
    host = f'{provisioning["mdns_name"]}.local' if isinstance(provisioning, dict) else str(mqtt["host"])
    return dict(node_id=node, mqtt_user=node,
                mqtt_password=password_path.read_text(encoding="utf-8").strip(),
                wifi_ssid=wifi["ssid"], wifi_password=wifi["password"],
                mqtt_host=host, mqtt_port=str(int(mqtt["port"])))


def public_view(values: dict[str, str]) -> dict[str, object]:
    return {key: (f"<{len(value.encode('utf-8'))} bytes>" if key in SECRETS or key == "wifi_ssid" else value)
            for key, value in values.items()}


def parse_state(line: str) -> dict[str, object] | None:
    match = STATE.fullmatch(line)
    if not match:
        return None
    names = ("configured", "generation", "node_id", "mqtt_user", "mqtt_host", "mqtt_port", "wifi_ssid_bytes",
             "wifi_password_set", "mqtt_password_set")
    raw = dict(zip(names, match.groups()))
    return {key: (value if key in ("node_id", "mqtt_user", "mqtt_host") else int(value))
            for key, value in raw.items()}


def mismatches(values: dict[str, str], state: dict[str, object]) -> list[str]:
    expected = dict(configured=1, node_id=values["node_id"], mqtt_user=values["mqtt_user"],
        mqtt_host=values["mqtt_host"], mqtt_port=int(values["mqtt_port"]),
        wifi_ssid_bytes=len(values["wifi_ssid"].encode("utf-8")),
        wifi_password_set=int(bool(values["wifi_password"])), mqtt_password_set=1)
    return [key for key, value in expected.items() if state[key] != value]


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
    for key in KEYS:
        encoded = values[key].encode("utf-8")
        session.send(f"CONFIG_SET {key} {encoded.hex()}")
        session.expect(lambda line: line == f"CONFIG_STAGED key={key} bytes={len(encoded)}", what=key)
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


def run(folder: Path, node: str, port_name: str, values: dict[str, str], *, timeout: float,
        begin_interval: float, open_port=None) -> bool:
    """Provision one device over `port_name`, logging to provision.jsonl. False when the device fails."""
    if open_port is None:
        import serial  # Optional diagnostic dependency, bundled with PlatformIO.
        open_port = lambda name: serial.Serial(name, 115200, timeout=0.2)
    with (folder / "provision.jsonl").open("a", encoding="utf-8") as log, open_port(port_name) as port:
        def emit(event, **data):
            record = dict(at=time.time(), event=event, node_id=node, **data)
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            print(json.dumps(record, ensure_ascii=False), flush=True)
        emit("provision_started", port=port_name, values=public_view(values))
        try:
            provision(port, values, timeout=timeout, emit=emit, begin_interval=begin_interval)
        except ValueError as error:
            emit("provision_failed", reason=str(error))
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True, help="시험 폴더 (lab.json, server.toml, .env.*)")
    parser.add_argument("--node", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--wifi-file", type=Path, help="기본값: 시험 폴더의 .env.wifi.json")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--begin-interval", type=float, default=1.0,
                        help="부팅 중 잃은 CONFIG_BEGIN을 다시 보내는 간격(초)")
    args = parser.parse_args()
    values = build_values(args.dir, args.node, wifi_file=args.wifi_file)
    if not run(args.dir, args.node, args.port, values, timeout=args.timeout, begin_interval=args.begin_interval):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
