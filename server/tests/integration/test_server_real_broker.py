"""NFR-08·NFR-12·NFR-13 실제 Mosquitto·SQLite·시뮬레이터로 조립한 서버를 검증한다.

localhost 임시 포트와 임시 자격정보만 쓴다. Mosquitto 가 없으면 건너뛴다
(`MOSQUITTO_EXECUTABLE`, `MOSQUITTO_PASSWD_EXECUTABLE` 또는 PATH).
한글 경로를 피하려고 브로커 설정과 ACL 은 임시 디렉터리에 복사한다.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone

import pytest

from _runtime import (
    SERVER, TOKENS, UUID, VISITOR_A, FakeProximity, make_config, query, running, seed, wait_until,
)
from nearpilot.runtime import Runtime
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Action, Verdict
from nearpilot.shared.models import TargetSpec, UseRequest
from tools.node_sim import Beacon, BrokerConfig, CommandStore, Credentials, Scenario, SimDevice, SimNode

NODE = "locker-1"  # target_spec 형식의 기기 ID. 임시 ACL 에 계정 절을 추가한다
NODES = {NODE: ("locker", "rental", "locker-return")}


@pytest.fixture
def broker(tmp_path):
    executable = os.environ.get("MOSQUITTO_EXECUTABLE") or shutil.which("mosquitto")
    passwd = os.environ.get("MOSQUITTO_PASSWD_EXECUTABLE") or shutil.which("mosquitto_passwd")
    if not executable or not passwd:
        pytest.skip("Real Mosquitto and mosquitto_passwd are required")
    passwords = {user: secrets.token_hex(20) for user in ("nearpilot-server", NODE)}
    password_file = tmp_path / ".env.passwords"
    password_file.write_text("".join(f"{u}:{p}\n" for u, p in passwords.items()), encoding="utf-8")
    assert subprocess.run([passwd, "-U", str(password_file)], capture_output=True).returncode == 0
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    acl = tmp_path / "mosquitto.acl"
    # 서버 계정 절은 예시를 그대로 쓰고, 기기 계정 절은 예시와 같은 모양으로 만든다.
    example = (SERVER / "config" / "mosquitto.acl.example").read_text(encoding="utf-8")
    server_section = example[example.index("user nearpilot-server"):example.index("user node-a")]
    device = [f"user {NODE}"]
    device += [f"topic write nearpilot/node/{NODE}/{kind}"
               for kind in ("announce", "rssi", "status", "result", "safety_check_result")]
    device += [f"topic read nearpilot/node/{NODE}/{kind}" for kind in ("cmd", "safety_check")]
    acl.write_text(server_section + "\n".join(device) + "\n", encoding="utf-8")
    text = (SERVER / "config" / "mosquitto.conf").read_text(encoding="utf-8")
    text = text.replace("listener 1883 127.0.0.1", f"listener {port} 127.0.0.1")
    text = text.replace("config/.env.mosquitto-passwords", password_file.as_posix())
    text = text.replace("config/mosquitto.acl.example", acl.as_posix())
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(text, encoding="utf-8")
    log_file = (tmp_path / "mosquitto.log").open("ab")
    process = subprocess.Popen([executable, "-c", str(conf)], stdout=log_file, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        wait_until(lambda: _listening(port) or process.poll() is not None, timeout=5)
        assert process.poll() is None, "Temporary Mosquitto failed to start"
        server_secret = tmp_path / ".env.server-password"
        server_secret.write_text(passwords["nearpilot-server"], encoding="utf-8")
        node_secret = tmp_path / f".env.node-{NODE}"
        node_secret.write_text(passwords[NODE], encoding="utf-8")
        yield port, server_secret, node_secret, passwords
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def test_NFR12_runtime_real_broker_restart(broker, tmp_path, caplog):
    port, server_secret, node_secret, passwords = broker
    cfg = make_config(tmp_path, settings={"threshold_explicit_locker": 0.5},
                      mqtt={"port": port, "password_file": str(server_secret)},
                      delivery={"clock_skew_sec": {NODE: 0.0}})  # 같은 PC 의 시뮬레이터
    seed(cfg.database.path, nodes=NODES)
    proximity = FakeProximity({NODE: 0.97})
    node = SimNode(NODE, "locker", "rental", CommandStore(tmp_path / "node-a.sqlite3"),
                   Scenario(actuation_sec=0.1, beacons=(Beacon(UUID, 1, 2, -55.0),)))
    device = SimDevice(node, BrokerConfig("127.0.0.1", port, 5, 1, 2), Credentials(NODE, node_secret),
                       Settings(status_report_sec=0.2, rssi_frame_sec=0.2))
    device.start()
    try:
        with caplog.at_level(logging.DEBUG):
            first = Runtime(cfg, proximity_factory=lambda models, s: proximity)
            with running(first):
                wait_until(lambda: first.services.restore.can_execute(NODE), timeout=10)
                wait_until(lambda: query(cfg.database.path, "SELECT count(*) c FROM rssi_observation")[0]["c"])
                decision = first.core.request_use(UseRequest(
                    VISITOR_A, "real-1", TargetSpec.parse(NODE), Action.OPEN, None,
                    datetime.now(timezone.utc)))
                assert decision.verdict is Verdict.OK, decision
                assert node.actuations == 1

            # 재시작: 같은 DB 의 점유를 복원하고 현재 연결을 다시 확인한다 (NFR-13)
            second = Runtime(cfg, proximity_factory=lambda models, s: proximity)
            with running(second):
                wait_until(lambda: second.services.restore.can_execute(NODE), timeout=10)
                status = second.core.get_use_status(VISITOR_A, decision.use_id)
                assert status["state"] == "ACTIVE"
                time.sleep(0.5)
                assert node.actuations == 1  # 재시작으로 다시 구동하지 않는다
        for secret in (*passwords.values(), *TOKENS.values()):
            assert secret not in caplog.text  # 비밀을 로그에 남기지 않는다 (NFR-08)
    finally:
        device.stop()
        node.store.close()
