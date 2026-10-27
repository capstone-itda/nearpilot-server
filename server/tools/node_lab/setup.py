"""Create an isolated lab. Never overwrite existing data or invent clock trust."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from uuid import UUID

from nearpilot.runtime import load_config
from tools.node_sim.config import acl_sections, load_config as load_fleet


def executable(name: str) -> str:
    found = shutil.which(name)
    fallback = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "mosquitto" / f"{name}.exe"
    if found:
        return found
    if fallback.is_file():
        return str(fallback)
    raise ValueError(f"{name} 실행 파일이 필요하다. PATH에 추가한다.")


# Device ID prefix -> (capability, occupancy). Lockers are rental, lights shared (requirements.md).
KINDS = {"light": ("light", "shared"), "locker": ("locker", "rental")}


def node_kind(node: str) -> tuple[str, str]:
    match = re.fullmatch(r"(light|locker)-[a-zA-Z0-9_-]+", node)
    if not match:
        raise ValueError("기기 ID는 light- 또는 locker- 접두사의 영문·숫자·밑줄·하이픈이다")
    return KINDS[match.group(1)]


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def initialize(folder: Path, *, mode: str, host: str, port: int, nodes: list[str],
               beacon_uuid: str, major: int, minor: int) -> Path:
    folder = folder.resolve()
    if folder.exists():
        raise ValueError("시험 폴더가 이미 있다. 기존 기록을 보존하려면 다른 경로를 쓴다.")
    if mode not in ("sim", "hardware") or not 1 <= port <= 65535:
        raise ValueError("mode 또는 port가 올바르지 않다")
    if not host or any(c in host for c in '\r\n"'):
        raise ValueError("host가 올바르지 않다")
    if mode == "sim" and host not in ("127.0.0.1", "localhost"):
        raise ValueError("시뮬레이터의 0초 시각 오차는 같은 PC의 localhost에서만 쓴다")
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("기기 ID는 서로 달라야 한다")
    kinds = {node: node_kind(node) for node in nodes}
    beacon_uuid = str(UUID(beacon_uuid))
    if not 0 <= major <= 65535 or not 0 <= minor <= 65535:
        raise ValueError("major와 minor는 0~65535다")
    passwd = executable("mosquitto_passwd")  # Validate dependency before creating files.
    folder.mkdir(parents=True)
    credentials = {name: secrets.token_hex(24) for name in ("nearpilot-server", *nodes)}
    for name, password in credentials.items():
        (folder / f".env.{name}").write_text(password + "\n", encoding="utf-8")
    password_path = folder / ".env.broker-passwords"
    password_path.write_text("".join(f"{k}:{v}\n" for k, v in credentials.items()), encoding="utf-8")
    result = subprocess.run([passwd, "-U", str(password_path)], capture_output=True)
    if result.returncode:
        password_path.unlink(missing_ok=True)
        raise ValueError("시험 비밀번호 파일을 암호화하지 못했다. 다른 시험 폴더에서 다시 준비한다.")
    q = lambda value: json.dumps(value, ensure_ascii=False)
    skew = "\n".join(f"{q(node)} = 0.0" for node in nodes) if mode == "sim" else ""
    (folder / "server.toml").write_text(f'''# Generated lab configuration, not a deployment model.
[settings]
version = "node-lab-1"
[runtime]
[http]
host = "127.0.0.1"
port = 8000
[database]
path = "lab.db"
audit_jsonl = "audit.jsonl"
[proximity]
model_root = "models"
[proximity.models]
node-lab = "synthetic-1"
[mqtt]
host = {q(host)}
port = {port}
client_id = "nearpilot-node-lab"
username = "nearpilot-server"
password_file = ".env.nearpilot-server"
keepalive_sec = 30
connect_timeout_sec = 5.0
start_timeout_sec = 10.0
stop_timeout_sec = 5.0
reconnect_min_sec = 1
reconnect_max_sec = 8
[delivery]
pending_limit = 1000
retry_interval_sec = 0.5
stop_timeout_sec = 5.0
# Hardware: fill only measured maximum clock errors, in seconds.
[delivery.clock_skew_sec]
{skew}
''', encoding="utf-8")
    model_nodes = {node: dict(x=i * 2.0, y=0.0, front_x=i * 2.0, front_y=0.5,
                             A=-59.0, n=2.0, sigma=4.0, none_rssi=-90.0, none_sigma=6.0)
                   for i, node in enumerate(nodes)}
    model = dict(place_id="node-lab", model_version="synthetic-1", calib_version="none-1",
                 window_sec=3.5, transition={"stay": 0.9}, min_distance_m=0.3,
                 calibration={"method": "none"}, nodes=model_nodes,
                 none_points=[{"x": i * 2.0, "y": 2.5} for i in range(len(nodes))],
                 validation={"source": "SYNTHETIC_NOT_MEASURED", "quality_verified": False})
    root = folder / "models" / "node-lab"
    root.mkdir(parents=True)
    write_json(root / "synthetic-1.json", model)
    beacon = dict(uuid=beacon_uuid, major=major, minor=minor)
    write_json(folder / "lab.json", dict(mode=mode, nodes=nodes, beacon=beacon,
        sample_sec=1.0, observe_timeout_sec=15.0, user_id="lab-visitor", owner_id="lab-owner"))
    devices = []
    for i, node in enumerate(nodes):
        distance = math.hypot(i * 2.0, 0.5)
        capability, occupancy = kinds[node]
        devices.append(dict(node_id=node, place="node-lab", capability=capability, occupancy=occupancy,
            username=node, password_file=str(folder / f".env.{node}"),
            store=str(folder / f"sim-{node}.sqlite3"), scenario=dict(actuation_sec=0.1,
                beacons=[{**beacon, "rssi": -59.0 - 20.0 * math.log10(distance)}])))
    write_json(folder / "fleet.json", dict(broker=dict(host=host, port=port, keepalive_sec=30,
        reconnect_min_sec=1, reconnect_max_sec=8), settings={}, devices=devices))
    acl = "user nearpilot-server\n" + "\n".join(
        [f"topic read nearpilot/node/+/{kind}" for kind in
         ("announce", "rssi", "status", "result")]
        + ["topic write nearpilot/node/+/cmd"]) + "\n\n"
    (folder / "broker.acl").write_text(acl + acl_sections(load_fleet(folder / "fleet.json")), encoding="utf-8")
    (folder / "broker.conf").write_text(
        f"listener {port} {host}\nallow_anonymous false\n"
        'password_file .env.broker-passwords\nacl_file broker.acl\n'
        "persistence false\nlog_type error\nlog_type warning\n", encoding="utf-8")
    load_config(folder / "server.toml")
    return folder


def broker_process(folder: Path):
    """Keep Mosquitto's referenced paths ASCII on Windows (AGENTS Gotchas)."""
    from contextlib import contextmanager

    @contextmanager
    def managed():
        with tempfile.TemporaryDirectory(prefix="np-lab-") as tmp:
            temp = Path(tmp)
            for name in (".env.broker-passwords", "broker.acl"):
                shutil.copyfile(folder / name, temp / name)
            config = (folder / "broker.conf").read_text(encoding="utf-8")
            for name in (".env.broker-passwords", "broker.acl"):
                config = config.replace(name, (temp / name).as_posix())
            (temp / "broker.conf").write_text(config, encoding="utf-8")
            with (folder / "broker.log").open("ab") as output:
                process = subprocess.Popen([executable("mosquitto"), "-c", str(temp / "broker.conf")],
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    yield process
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
    return managed()
