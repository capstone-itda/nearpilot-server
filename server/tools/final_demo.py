"""Prepare an isolated configuration for the existing server; never run a second server implementation."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import secrets
import shutil
import subprocess

from nearpilot.runtime import load_config
from tools.node_lab.setup import executable, write_json
from tools.node_sim.config import acl_sections, load_config as load_fleet

SERVER = Path(__file__).resolve().parents[1]
TEMPLATES = SERVER / "config" / "final-demo"


def prepare(folder: Path, *, host: str, mode: str = "hardware", mqtt_port: int = 18883,
            http_port: int = 8000) -> Path:
    folder = folder.resolve()
    if folder.exists():
        raise ValueError("시연 폴더가 이미 있다. 기존 기록을 보존하려면 다른 경로를 쓴다.")
    address = ipaddress.IPv4Address(host)
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        raise ValueError("서버 주소는 기기가 접속할 IPv4 주소여야 한다")
    if mode not in ("hardware", "sim"):
        raise ValueError("mode는 hardware 또는 sim이다")
    if mode == "sim" and str(address) != "127.0.0.1":
        raise ValueError("시뮬레이터의 0초 시각 오차는 127.0.0.1에서만 쓴다")
    if any(type(port) is not int or not 1 <= port <= 65535 for port in (mqtt_port, http_port)):
        raise ValueError("포트는 1~65535의 정수여야 한다")
    if mqtt_port == http_port:
        raise ValueError("MQTT와 HTTP 포트는 달라야 한다")
    passwd = executable("mosquitto_passwd")
    fleet = json.loads((TEMPLATES / "fleet.template.json").read_text(encoding="utf-8"))
    options = json.loads((TEMPLATES / "lab.template.json").read_text(encoding="utf-8"))
    config = (TEMPLATES / "server.template.toml").read_text(encoding="utf-8")
    script = (TEMPLATES / "run.template.ps1").read_text(encoding="utf-8")
    replacements = {"@HOST@": host, "@HTTP_PORT@": str(http_port), "@MQTT_PORT@": str(mqtt_port)}
    for old, new in replacements.items():
        config = config.replace(old, new)
    if mode == "sim":
        config += "\n" + "\n".join(f'"{node}" = 0.0' for node in options["nodes"]) + "\n"
    options["mode"] = mode
    fleet["broker"].update(host=host, port=mqtt_port)
    for device in fleet["devices"]:
        device["password_file"] = str(folder / f'.env.{device["node_id"]}')
        device["store"] = str(folder / f'sim-{device["node_id"]}.sqlite3')
    # Read templates and resolve the required executable before creating the output directory.
    script = script.replace("@SERVER_ROOT@", str(SERVER).replace("'", "''"))
    folder.mkdir(parents=True)
    for name in ("seed.toml", "README.md"):
        shutil.copyfile(TEMPLATES / name, folder / name)
    shutil.copytree(TEMPLATES / "models", folder / "models")
    (folder / "server.toml").write_text(config, encoding="utf-8")
    (folder / "run.ps1").write_text(script, encoding="utf-8-sig")
    write_json(folder / "fleet.json", fleet)
    write_json(folder / "lab.json", options)
    credentials = {name: secrets.token_hex(24) for name in ("nearpilot-server", *options["nodes"])}
    for name, password in credentials.items():
        (folder / f".env.{name}").write_text(password + "\n", encoding="utf-8")
    password_file = folder / ".env.broker-passwords"
    password_file.write_text("".join(f"{name}:{value}\n" for name, value in credentials.items()), encoding="utf-8")
    try:
        result = subprocess.run([passwd, "-U", str(password_file)], capture_output=True)
    except OSError:
        password_file.unlink(missing_ok=True)
        raise
    if result.returncode:
        password_file.unlink(missing_ok=True)
        raise ValueError("브로커 비밀번호 암호화에 실패했다. 다른 시연 폴더에서 다시 준비한다")
    server_acl = "user nearpilot-server\n" + "\n".join(
        [f"topic read nearpilot/node/+/{kind}" for kind in ("announce", "rssi", "status", "result")]
        + [f"topic write nearpilot/node/+/{kind}" for kind in ("cmd", "occupancy")]) + "\n\n"
    (folder / "broker.acl").write_text(server_acl + acl_sections(load_fleet(folder / "fleet.json")), encoding="utf-8")
    bind = "127.0.0.1" if mode == "sim" else "0.0.0.0"
    (folder / "broker.conf").write_text(
        f"listener {mqtt_port} {bind}\nallow_anonymous false\n"
        "password_file .env.broker-passwords\nacl_file broker.acl\n"
        "persistence false\nlog_type error\nlog_type warning\n", encoding="utf-8")
    load_config(folder / "server.toml")
    return folder


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="기존 NearPilot 서버의 최종 시연 구성을 만든다. server/에서 실행한다.")
    parser.add_argument("--dir", type=Path, default=Path("var/final-demo"))
    parser.add_argument("--host", help="기기가 접속할 서버 PC의 IPv4 주소")
    parser.add_argument("--mode", choices=("hardware", "sim"), default="hardware")
    parser.add_argument("--mqtt-port", type=int, default=18883)
    parser.add_argument("--http-port", type=int, default=8000)
    args = parser.parse_args(argv)
    host = args.host or ("127.0.0.1" if args.mode == "sim" else "10.106.106.199")
    try:
        folder = prepare(args.dir, host=host, mode=args.mode, mqtt_port=args.mqtt_port, http_port=args.http_port)
    except (OSError, ValueError) as error:
        print(f"시연 구성 실패: {error}")
        return 2
    print(f"시연 구성: {folder}\n앱: http://{host}:{args.http_port}\nMCP: http://{host}:{args.http_port}/mcp")
    print(f"MQTT: {host}:{args.mqtt_port}\n실행 절차: {folder / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
