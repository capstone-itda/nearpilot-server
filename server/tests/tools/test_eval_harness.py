"""NFR-13 서버 지표 평가 하네스 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

실제 Mosquitto와 `nearpilot serve`와 시뮬레이터를 띄우는 시험은 `cleans_up_on_error`와 `runs_tool_call` 둘이다.
`startup_timeout_cleans_up`은 서버 자리에 값싼 명령을 두고 실제 Mosquitto만 띄운다.
Mosquitto가 없으면 실제 프로세스 시험을 건너뛴다 (`MOSQUITTO_EXECUTABLE`, `MOSQUITTO_PASSWD_EXECUTABLE`,
PATH, 기본 설치 폴더). 임시 폴더와 localhost 임시 포트만 쓴다.
"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.db.schema import connect
from nearpilot.shared.models import BeaconKey, RssiFrame
import tools.evaluation.harness as harness_module
from tools.evaluation.harness import Harness, HarnessError, find_mosquitto
from tools.evaluation.server_config import NodeSpec, load_eval_config
from tools.node_sim import Beacon, CommandStore, Scenario, SimNode

SERVER = Path(__file__).resolve().parents[2]
EXAMPLE = SERVER / "config" / "eval" / "server-metrics.example.toml"


def small_config():
    """예시 설정에서 사용자 owner, visitor-1과 사물함 locker-1만 남긴다."""
    config = load_eval_config(EXAMPLE)
    return dataclasses.replace(
        config,
        users=tuple(user for user in config.users if user.user_id in ("owner", "visitor-1")),
        nodes=tuple(node for node in config.nodes if node.node_id == "locker-1"))


@pytest.fixture
def mosquitto():
    try:
        return find_mosquitto()
    except HarnessError as error:
        pytest.skip(f"Real Mosquitto and mosquitto_passwd are required ({error})")


@pytest.fixture
def spawned(monkeypatch):
    """하네스가 띄운 하위 프로세스를 모두 담는다 (`subprocess.run`이 만든 것도 포함)."""
    processes = []

    class Recorder(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            processes.append(self)

    monkeypatch.setattr(subprocess, "Popen", Recorder)
    return processes


def assert_stopped(spawned):
    """하위 프로세스가 모두 끝났고 시뮬레이터 기기의 스레드가 남지 않았다."""
    assert all(process.poll() is not None for process in spawned)
    assert not [thread.name for thread in threading.enumerate() if thread.name.startswith("node-sim-")]


def test_NFR13_harness_requires_mosquitto(monkeypatch, ascii_tmp_path, spawned):
    monkeypatch.delenv("MOSQUITTO_EXECUTABLE", raising=False)
    monkeypatch.delenv("MOSQUITTO_PASSWD_EXECUTABLE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    workdir = ascii_tmp_path / "work"

    with pytest.raises(HarnessError, match="mosquitto"):
        find_mosquitto()
    monkeypatch.setenv("MOSQUITTO_EXECUTABLE", str(ascii_tmp_path / "missing"))  # 가리키는 파일이 없는 경로도 없는 것이다
    with pytest.raises(HarnessError, match="missing"):
        find_mosquitto()
    with pytest.raises(HarnessError, match="mosquitto"):
        with Harness(small_config(), workdir, server_root=SERVER):
            pytest.fail("Mosquitto가 없으면 시작하지 않아야 한다")

    assert spawned == []  # 아무 프로세스도 띄우지 않았다
    assert not workdir.exists()


def test_NFR13_harness_rejects_unreadable_model(ascii_tmp_path, spawned):
    config = small_config()
    # 공통 모델은 기기를 모른다. 목록에 없는 기기를 trusted로 두어도 모델 때문에 막히지 않는다.
    config = dataclasses.replace(config, nodes=(*config.nodes, NodeSpec("ghost-1", "locker", "rental")))
    broken = dataclasses.replace(config, model_version="missing-version")

    with pytest.raises(HarnessError, match="missing-version"):  # 모델 파일을 읽지 못하면 서버가 시작하지 못한다
        with Harness(broken, ascii_tmp_path / "work", server_root=SERVER):
            pytest.fail("모델을 읽을 수 없으면 시작하지 않아야 한다")

    assert spawned == []


def test_NFR13_harness_rejects_non_ascii_workdir_before_starting(tmp_path, spawned):
    workdir = tmp_path / "작업 폴더"  # Mosquitto가 한글 경로의 설정 파일을 열지 못한다

    with pytest.raises(HarnessError, match="ASCII"):
        with Harness(small_config(), workdir, server_root=SERVER):
            pytest.fail("ASCII가 아닌 작업 폴더는 시작하지 않아야 한다")

    assert spawned == []  # 아무 프로세스도 띄우지 않았다
    assert not workdir.exists()


def test_NFR13_harness_set_rssi_and_scenario_replace_simulated_scenario(tmp_path):
    config = small_config()
    harness = Harness(config, tmp_path / "work", server_root=SERVER)
    store = CommandStore(tmp_path / "locker-1.sqlite3")
    node = SimNode("locker-1", "locker", store, Scenario(actuation_sec=config.actuation_sec))
    harness.nodes["locker-1"] = node
    minor = next(user.minor for user in config.users if user.user_id == "visitor-1")
    try:
        harness.set_rssi("locker-1", {"visitor-1": -53.0})
        assert node.scenario.beacons == (Beacon(config.beacon_uuid, config.beacon_major, minor, -53.0),)

        harness.set_scenario("locker-1", final="failed", storage_error=True)
        assert (node.scenario.final, node.scenario.storage_error) == ("failed", True)
        assert node.store.fail_writes is True  # SimNode는 만들 때 한 번만 저장소에 전한다
        assert node.scenario.beacons  # 바꾸지 않은 필드는 그대로다

        harness.set_rssi("locker-1", {})  # 빠진 사용자는 듣지 못한다
        assert node.scenario.beacons == ()
    finally:
        store.close()


def ready_scene(root, rssi):
    """하네스가 읽는 DB 파일과 서버 설정 파일만 놓는다 (프로세스를 띄우지 않는다).

    방문자가 사물함 locker-1 앞에서 `rssi`로 들리는 프레임 셋이 방금 들어왔다 (칸 길이 1초).
    (연결, 저장소, 지금, 준비 확인 함수)를 돌려준다. 연결은 부른 쪽이 닫는다.
    """
    config = small_config()
    harness = Harness(config, root / "work", server_root=SERVER)
    harness._db_path.parent.mkdir(parents=True)
    harness._server_toml.write_text('[settings]\nversion = "t1"\n', encoding="utf-8")
    minor = next(user.minor for user in config.users if user.user_id == "visitor-1")
    beacon = BeaconKey(config.beacon_uuid, config.beacon_major, minor)
    conn = connect(str(harness._db_path))
    for user_id, role in (("owner", "owner"), ("visitor-1", "visitor")):
        conn.execute("INSERT INTO user (user_id, name, token_hash, role) VALUES (?, ?, ?, ?)",
                     (user_id, user_id, f"hash-{user_id}", role))
    conn.execute("INSERT INTO beacon (beacon_id, user_id, uuid, major, minor, is_active) VALUES ('b-1', 'visitor-1', ?, ?, ?, 1)",
                 (beacon.uuid, beacon.major, beacon.minor))
    conn.execute("INSERT INTO place (place_id, owner_user_id, name) VALUES ('place-a', 'owner', 'lab')")
    conn.execute("INSERT INTO node (node_id, place_id, kind, occupancy, trust_state, device_state, exclusive_group, release_policy) "
                 "VALUES ('locker-1', 'place-a', 'locker', 'rental', 'trusted', 'AVAILABLE', NULL, '{}')")
    repo = SqliteRepository(conn)
    now = datetime.now(timezone.utc)
    for seq in range(1, 4):  # 최근 구간 안의 프레임 셋
        at = now - timedelta(seconds=seq - 0.8)
        repo.save_rssi(RssiFrame("locker-1", beacon, rssi, at, "s-1", seq, at, at))
    return conn, repo, now, lambda: harness.proximity_ready("visitor-1", "locker-1")


def test_NFR13_harness_ready_uses_conservative_probability(tmp_path, monkeypatch):
    """근접 준비 확인은 판정과 같이 `threshold_probability`로 임계값(사물함 0.95)을 본다 (결정 17).

    대상이 자동 영점을 찾지 못했으면(`unzeroed`) 보수적 사후확률이다. 시험 모델(`syn-common-1`)에서 강한 신호(`near`)는
    자동 영점이 없어도 보수적 사후확률이 임계값을 넘어 준비됐다. 6 dB 약한 신호는 사후확률만 임계값을 넘는다.
    그래서 자동 영점 행이 없거나 마지막 행이 버림이면 준비되지 않았고, 0 dB 행이 있으면 같은 관측으로 준비됐다.
    """
    near = small_config().rssi["near"]
    strong_conn, strong_repo, now, strong_ready = ready_scene(tmp_path / "strong", near)
    weak_conn, weak_repo, scene_now, weak_ready = ready_scene(tmp_path / "weak", near - 6.0)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return scene_now

    # 준비 확인의 현재 시각을 장면 시각으로 고정한다. 실행이 느려도 프레임이 관측 구간 밖으로 밀려나지 않는다
    monkeypatch.setattr(harness_module, "datetime", FrozenDatetime)
    try:
        assert strong_ready()  # 자동 영점 행이 없어도 보수적 사후확률이 임계값 이상이다
        strong_repo.save_auto_zero("locker-1", 0.0, None, None, now)
        assert strong_ready()
        strong_repo.save_auto_zero("locker-1", None, None, None, now)
        assert strong_ready()  # 마지막 행이 버림이어도 같다

        assert not weak_ready()  # 자동 영점 행이 없고 보수적 사후확률이 임계값 미만이다
        weak_repo.save_auto_zero("locker-1", 0.0, None, None, now)
        assert weak_ready()  # 영점이 있으면 같은 관측으로 준비됐다
        weak_repo.save_auto_zero("locker-1", None, None, None, now)
        assert not weak_ready()  # 마지막 행이 버림이라 영점이 없다
    finally:
        strong_conn.close()
        weak_conn.close()


@pytest.mark.parametrize("server_script", [
    pytest.param("import sys; print('stand-in server', flush=True); sys.exit(3)", id="exits"),
    pytest.param("import time; print('stand-in server', flush=True); time.sleep(60)", id="never-listens"),
])
def test_NFR13_harness_startup_timeout_cleans_up(monkeypatch, ascii_tmp_path, mosquitto, spawned, server_script):
    config = dataclasses.replace(small_config(), startup_timeout_sec=2.0)
    monkeypatch.setattr(Harness, "_server_command", lambda self: [sys.executable, "-c", server_script])

    with pytest.raises(HarnessError, match="stand-in server"):  # 서버 로그 끝이 오류에 담긴다
        with Harness(config, ascii_tmp_path / "work", server_root=SERVER):
            pytest.fail("서버가 포트를 열지 않으면 시작하지 않아야 한다")

    assert_stopped(spawned)


def test_NFR13_harness_cleans_up_on_error(ascii_tmp_path, mosquitto, spawned):
    with pytest.raises(RuntimeError, match="scenario failed"):  # 정리하고도 예외를 삼키지 않는다
        with Harness(small_config(), ascii_tmp_path / "work", server_root=SERVER):
            raise RuntimeError("scenario failed")

    assert_stopped(spawned)


def test_NFR13_harness_runs_tool_call(ascii_tmp_path, mosquitto, spawned):
    config = small_config()
    with Harness(config, ascii_tmp_path / "work", server_root=SERVER) as harness:
        harness.set_rssi("locker-1", {"visitor-1": config.rssi["near"]})

        def listed() -> bool:  # 장소의 기기 발견과 근접 지정의 준비를 따로 확인한다.
            _, found, _ = harness.mcp("visitor-1", "find_available_devices", {})
            return (found is not None and [device["node_id"] for device in found["devices"]] == ["locker-1"]
                    and harness.proximity_ready("visitor-1", "locker-1"))

        assert harness.wait_until(listed, config.startup_timeout_sec)
        status, result, elapsed = harness.mcp("visitor-1", "request_use", {
            "target_spec": "nearest:locker", "action": "open", "request_id": "req-1"})
        assert (status, result["verdict"], result["node_id"]) == (200, "OK", "locker-1")
        assert elapsed > 0
        use_id = result["use_id"]

        def active() -> bool:
            _, state, _ = harness.mcp("visitor-1", "get_use_status", {"use_id": use_id})
            return state is not None and state["state"] == "ACTIVE"

        assert harness.wait_until(active, config.startup_timeout_sec)
        assert harness.nodes["locker-1"].actuations == 1  # 시뮬레이터가 제어 신호 출력을 한 번 마쳤다
        _, released, _ = harness.mcp("visitor-1", "release_use", {"use_id": use_id})
        assert released["verdict"] == "OK"

        assert harness.mcp(None, "find_available_devices", {})[:2] == (401, None)  # 토큰이 없으면 도구 결과가 없다
        assert harness.rest("owner", "GET", "/approvals") == (200, [])
        assert harness.rest(None, "GET", "/approvals")[0] == 401
        assert [tuple(row) for row in harness.query(
            "SELECT node_id FROM use_session WHERE use_id = ?", use_id)] == [("locker-1",)]
        with pytest.raises(sqlite3.OperationalError):  # 읽기 전용 연결은 쓰지 못한다
            harness.query("DELETE FROM use_session")

    assert_stopped(spawned)
