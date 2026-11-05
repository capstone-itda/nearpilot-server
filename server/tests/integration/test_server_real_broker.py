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
NODES = {NODE: ("locker", "rental")}


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
               for kind in ("announce", "rssi", "status", "result", "time_request")]
    device += [f"topic read nearpilot/node/{NODE}/{kind}" for kind in ("cmd", "time", "restart")]
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
                      delivery={"clock_max_error_sec": 0.0})  # 같은 PC 의 시뮬레이터
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


def test_FR07_runtime_answers_time_request_over_real_broker(broker, tmp_path):
    """#236: 기기 계정이 보낸 time_request에 서버가 같은 request_id와 서버 시각으로 답한다."""
    import json
    import queue
    import uuid

    import paho.mqtt.client as mqtt

    port, server_secret, node_secret, passwords = broker
    cfg = make_config(tmp_path, mqtt={"port": port, "password_file": str(server_secret)},
                      delivery={"clock_max_error_sec": 0.5, "clock_trust_sec": 120, "clock_resync_sec": 60})
    seed(cfg.database.path, nodes=NODES)
    replies: queue.Queue = queue.Queue()
    subscribed = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"test-{secrets.token_hex(4)}",
                         clean_session=True, protocol=mqtt.MQTTv311)
    client.username_pw_set(NODE, passwords[NODE])
    client.on_subscribe = lambda *args: subscribed.put(True)
    client.on_message = lambda c, u, message: replies.put((message.topic, message.qos, message.retain,
                                                           json.loads(message.payload)))
    client.connect("127.0.0.1", port, keepalive=5)
    client.loop_start()
    try:
        client.subscribe(f"nearpilot/node/{NODE}/time", qos=0)
        subscribed.get(timeout=3)
        runtime = Runtime(cfg, proximity_factory=lambda models, s: FakeProximity({NODE: 0.0}))
        with running(runtime):
            before = datetime.now(timezone.utc)
            client.publish(f"nearpilot/node/{NODE}/time_request", json.dumps(dict(
                schema_version=2, node_id=NODE, session_id=str(uuid.uuid4()), request_id="rq-1")), qos=0)
            topic, qos, retain, body = replies.get(timeout=5)
            after = datetime.now(timezone.utc)
        assert (topic, qos, retain) == (f"nearpilot/node/{NODE}/time", 0, False)
        assert (body["request_id"], body["max_error_sec"], body["trust_sec"], body["resync_sec"]) == \
            ("rq-1", 0.5, 120, 60)
        server_time = datetime.fromisoformat(body["server_time"].replace("Z", "+00:00"))
        assert before <= server_time <= after
    finally:
        client.loop_stop()
        client.disconnect()


def test_NFR04_runtime_restart_reaches_device_over_real_broker(broker, tmp_path):
    """#235: 소유자의 재시작 요청이 기기 ACL로 현재 연결 ID를 담은 `restart`로 도착한다."""
    import json
    import queue
    import uuid

    import paho.mqtt.client as mqtt

    from _runtime import OWNER
    from nearpilot.shared.enums import RestartOutcome

    port, server_secret, node_secret, passwords = broker
    cfg = make_config(tmp_path, mqtt={"port": port, "password_file": str(server_secret)})
    seed(cfg.database.path, nodes=NODES)
    session = str(uuid.uuid4())
    received: queue.Queue = queue.Queue()
    subscribed = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"test-{secrets.token_hex(4)}",
                         clean_session=True, protocol=mqtt.MQTTv311)
    client.username_pw_set(NODE, passwords[NODE])
    client.on_subscribe = lambda *args: subscribed.put(True)
    client.on_message = lambda c, u, message: received.put((message.topic, message.qos, message.retain,
                                                            json.loads(message.payload)))
    client.connect("127.0.0.1", port, keepalive=5)
    client.loop_start()
    try:
        client.subscribe(f"nearpilot/node/{NODE}/restart", qos=1)
        subscribed.get(timeout=3)
        runtime = Runtime(cfg, proximity_factory=lambda models, s: FakeProximity({NODE: 0.0}))
        with running(runtime):
            assert runtime.nodes.restart(OWNER, NODE).outcome is RestartOutcome.NODE_OFFLINE
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            client.publish(f"nearpilot/node/{NODE}/status", json.dumps(dict(
                schema_version=2, node_id=NODE, session_id=session, seq=1, sent_at=now, observed_at=now,
                command_state="idle")), qos=1)
            wait_until(lambda: runtime.services.delivery.current_session(NODE) == session, timeout=5)
            assert runtime.nodes.restart(VISITOR_A, NODE).outcome is RestartOutcome.NOT_OWNER
            result = runtime.nodes.restart(OWNER, NODE)
            topic, qos, retain, body = received.get(timeout=5)
        assert result.outcome is RestartOutcome.SENT
        assert (topic, qos, retain) == (f"nearpilot/node/{NODE}/restart", 1, False)
        assert (body["restart_id"], body["target_session_id"]) == (result.restart_id, session)
        assert received.empty()  # 소유자가 아닌 요청과 오프라인 요청은 발행하지 않았다
    finally:
        client.loop_stop()
        client.disconnect()
