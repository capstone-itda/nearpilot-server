"""서버 지표 평가 하네스 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

임시 폴더에 Mosquitto, `nearpilot serve` 하위 프로세스, 같은 프로세스 안의 시뮬레이터 기기(`SimDevice`)를 띄우고
`nearpilot seed`로 계정, 장소, 기기, 정책을 넣는다. MCP와 REST는 HTTP로 부르고, DB는 읽기 전용 연결로만 읽는다 (NFR-13).
운영 DB, 운영 브로커, 실제 기기에는 접속하지 않는다. 포트는 비어 있는 localhost 포트를 고른다.
`tools/`는 `tests/`를 import하지 않는다. 서버와 시드는 운영과 같은 `nearpilot` 명령이다.
`workdir`은 ASCII 경로여야 한다. Mosquitto가 한글 경로의 설정 파일을 열지 못한다. 아니면 프로세스를 띄우기 전에 `HarnessError`다.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nearpilot.core.proximity.model import load_models
from nearpilot.core.proximity.estimator import HmmProximityEstimator
from nearpilot.db.repositories.repository import SqliteRepository
from nearpilot.shared.config import Settings
from tools.evaluation.db import open_readonly
from tools.evaluation.server_config import EvalConfig
from tools.node_sim import Beacon, BrokerConfig, CommandStore, Credentials, Scenario, SimDevice, SimNode
from tools.node_sim.config import REPORTS

PLACE_ID = "place-a"  # 모든 기기가 놓이는 장소. 근접 모델 파일은 <model_root>/place-a/ 에 있다
SERVER_ACCOUNT = "nearpilot-server"  # server.example.toml의 [mqtt].username이자 ACL의 서버 계정이다

# PATH에 없을 때 Mosquitto를 찾아볼 폴더 (Homebrew, 리눅스 패키지, Windows 설치 폴더)
_DEFAULT_DIRS = (
    "/opt/homebrew/sbin", "/opt/homebrew/bin", "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin",
    r"C:\Program Files\mosquitto",
)
_POLL_SEC = 0.05  # 조건을 기다리며 다시 확인하는 간격
_STOP_GRACE_SEC = 10.0  # 하위 프로세스에 terminate를 보낸 뒤 kill하기까지 기다리는 시간
_LOG_TAIL_LINES = 20  # 시작 실패 오류에 담는 로그 줄 수
# `nearpilot` 명령은 `python -m nearpilot`으로 부를 수 없다. `__main__`은 `--config`만 받는 서버 진입점이다.
_CLI = "import sys; from nearpilot.cli import main; sys.exit(main())"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost 요청이 환경의 프록시를 타지 않게 한다
_ACCOUNT_LINE = re.compile(r"^계정 (.+): 등록$")  # `nearpilot seed`의 출력 형식 (cli/seed.py)
_TOKEN_LINE = re.compile(r"^\s+토큰: (\S+)\s+\(다시 보여 주지 않는다\)\s*$")


class HarnessError(RuntimeError):
    """하네스가 프로세스를 띄우지 못했거나 서버와 통신하지 못했다."""


def find_mosquitto() -> tuple[Path, Path]:
    """(mosquitto, mosquitto_passwd) 실행 파일을 찾는다. 없으면 `HarnessError`다.

    `MOSQUITTO_EXECUTABLE`, `MOSQUITTO_PASSWD_EXECUTABLE`로 경로를 줄 수 있다. 없으면 PATH, 기본 설치 폴더 순이다.
    """
    found = []
    for variable, program in (("MOSQUITTO_EXECUTABLE", "mosquitto"), ("MOSQUITTO_PASSWD_EXECUTABLE", "mosquitto_passwd")):
        path = (os.environ.get(variable) or shutil.which(program)
                or shutil.which(program, path=os.pathsep.join(_DEFAULT_DIRS)))
        if not path or not Path(path).is_file():
            raise HarnessError(f"{program} 실행 파일이 없다 ({path or '찾지 못했다'}). "
                               f"PATH에 두거나 {variable}에 경로를 준다")
        found.append(Path(path))
    return found[0], found[1]


class Harness:
    """`with` 블록 동안 브로커, 서버, 시뮬레이터 기기가 떠 있고 시드를 마친 상태를 만든다.

    시작 순서: 브로커 → 서버(HTTP 포트가 열릴 때까지) → 시뮬레이터 기기 → DB에 기기가 생길 때까지 →
    `nearpilot seed` → 서버 재시작 → 기기가 복원을 마칠 때까지. 시작에 실패하거나 블록이 끝나면
    시뮬레이터 → 서버 → 브로커 순으로 끈다. 제한 시간 안에 못 뜨면 `HarnessError`다.
    `startup_timeout_sec`은 시작을 기다리는 시간이자 HTTP 요청 한 번의 제한 시간이다.
    """

    def __init__(self, config: EvalConfig, workdir: Path, *, server_root: Path) -> None:
        self.config = config
        self.workdir = workdir  # 브로커·서버 설정, DB, 로그가 놓인다
        self.server_root = server_root  # 코드를 읽을 checkout의 server/. 서버 설정 예시도 여기서 읽는다
        self.tokens: Mapping[str, str] = {}  # user_id → 토큰. 시드 출력에서 읽는다
        self.nodes: dict[str, SimNode] = {}  # node_id → 시뮬레이터 기기
        self._devices: list[SimDevice] = []
        self._processes: list[subprocess.Popen] = []
        self._logs: list[Any] = []
        self._broker: subprocess.Popen | None = None
        self._server: subprocess.Popen | None = None
        self._http_port = 0
        self._db_path = workdir / "np.db"
        self._server_toml = workdir / "server.toml"

    # ── 시작과 정리 ──────────────────────────────────────────────────

    def __enter__(self) -> Harness:
        try:
            self._start()
        except BaseException as error:  # `__exit__`는 `__enter__`가 실패하면 불리지 않는다
            try:
                self._stop()
            except HarnessError as cleanup:  # 시작이 실패한 원인이 더 중요하다
                error.add_note(str(cleanup))
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop()

    def _start(self) -> None:
        config = self.config
        if not str(self.workdir).isascii():  # 폴더도 프로세스도 만들기 전에 거절한다
            raise HarnessError(f"작업 폴더 경로에 ASCII가 아닌 글자가 있다: {self.workdir}. "
                               f"Mosquitto가 한글 경로의 설정 파일을 열지 못한다. ASCII 경로를 쓴다")
        self._check_model()
        mosquitto, mosquitto_passwd = find_mosquitto()
        example = tomllib.loads((self.server_root / "config" / "server.example.toml").read_text(encoding="utf-8"))
        settings = {**example["settings"], **config.server_settings}
        try:
            sim_settings = Settings(**settings)  # 기기의 보고 주기는 서버 설정을 따른다
        except (TypeError, ValueError) as error:
            raise HarnessError(f"서버 설정이 올바르지 않다: {error}") from error
        self.workdir.mkdir(parents=True, exist_ok=True)
        mqtt_port, self._http_port = _free_ports(2)
        self._write_secrets(mosquitto_passwd)

        self._broker = self._spawn("broker", [str(mosquitto), "-c", str(self._write_broker_config(mqtt_port))])
        self._wait_listening("broker", mqtt_port, self._broker)

        self._write_server_config(example, settings, mqtt_port)
        self._server = self._spawn("serve", self._server_command(), cwd=self.server_root, env=self._env())
        self._wait_listening("serve", self._http_port, self._server)

        mqtt = example["mqtt"]
        broker = BrokerConfig("127.0.0.1", mqtt_port, mqtt["keepalive_sec"], mqtt["reconnect_min_sec"],
                              mqtt["reconnect_max_sec"])
        for spec in config.nodes:
            node = SimNode(spec.node_id, spec.capability, spec.occupancy,
                           CommandStore(self.workdir / "sim" / f"{spec.node_id}.sqlite3"),
                           Scenario(actuation_sec=config.actuation_sec))
            device = SimDevice(node, broker, Credentials(spec.node_id, self._secret_file(spec.node_id)), sim_settings)
            self.nodes[spec.node_id] = node  # 시작에 실패해도 정리가 저장소를 닫는다
            self._devices.append(device)
            device.start()
        wanted = {spec.node_id for spec in config.nodes}
        if not self.wait_until(lambda: wanted <= self._stored_nodes(), config.startup_timeout_sec):
            raise HarnessError(f"서버 DB에 기기가 생기지 않았다: {sorted(wanted - self._stored_nodes())}\n"
                               f"{self._log_tail('serve')}")

        self.tokens = self._seed(_first_owner(config))
        self._restart_server()
        self._wait_restored()

    def _stop(self) -> None:
        """시뮬레이터 → 서버 → 브로커 순으로 끈다. 하나가 실패해도 나머지를 끄고, 마지막에 첫 실패를 낸다."""
        failures: list[Exception] = []

        def attempt(action: Callable[[], Any]) -> None:
            try:
                action()
            except Exception as error:  # noqa: BLE001 - 나머지 정리를 계속한 뒤 낸다
                failures.append(error)

        devices, self._devices = self._devices, []
        for device in reversed(devices):
            attempt(device.stop)
        for node in self.nodes.values():
            attempt(node.store.close)
        processes, self._processes = self._processes, []
        for process in reversed(processes):
            attempt(lambda process=process: _terminate(process))
        logs, self._logs = self._logs, []
        for log in logs:
            attempt(log.close)
        if failures:
            raise HarnessError(f"하네스를 정리하다 실패했다: {failures[0]}") from failures[0]

    # ── 클라이언트 ───────────────────────────────────────────────────

    def set_rssi(self, node_id: str, heard: Mapping[str, float]) -> None:
        """그 기기가 듣는 사용자별 RSSI(user_id → dBm)로 바꾼다. 빠진 사용자는 듣지 못한다."""
        minors = {user.user_id: user.minor for user in self.config.users}
        beacons = tuple(Beacon(self.config.beacon_uuid, self.config.beacon_major, minors[user_id], float(rssi))
                        for user_id, rssi in heard.items())
        self.set_scenario(node_id, beacons=beacons)

    def set_scenario(self, node_id: str, **changes: Any) -> None:
        """기기의 `Scenario` 필드(final, drop_final_result, result_delay_sec 등)를 바꿔 끼운다."""
        node = self.nodes[node_id]
        node.scenario = dataclasses.replace(node.scenario, **changes)
        if "storage_error" in changes:  # SimNode는 이 값을 만들 때 한 번만 저장소에 전한다
            node.store.fail_writes = node.scenario.storage_error

    def mcp(self, user_id: str | None, tool: str, arguments: Mapping[str, Any]) -> tuple[int, dict[str, Any] | None, float]:
        """MCP 도구를 부른다. (HTTP 상태, 도구 결과 JSON 또는 None, 걸린 초)를 돌려준다.

        `user_id`가 None이면 토큰 없이 보낸다. 도구 결과를 읽을 수 없으면(401 등) 결과는 None이다.
        연결하지 못하거나 제한 시간이 지나면 `HarnessError`다.
        """
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": dict(arguments)}}
        started = time.perf_counter()
        status, raw = self._http("POST", "/mcp", user_id, body)
        elapsed = time.perf_counter() - started
        return status, _tool_result(_decode(raw)) if status == 200 else None, elapsed

    def rest(self, user_id: str | None, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """REST를 부른다. (HTTP 상태, JSON으로 읽은 응답 또는 문자열 또는 빈 응답이면 None)를 돌려준다."""
        status, raw = self._http(method, path, user_id, body)
        return status, _decode(raw)

    def query(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        """서버 DB를 읽기 전용 연결로 조회한다 (`tools.evaluation.db`). 부를 때마다 연결을 새로 연다."""
        conn = open_readonly(_uri_path(self._db_path))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def proximity_ready(self, user_id: str, node_id: str) -> bool:
        """평가 장면의 근접 확률을 저장 관측으로 재현한다. 조회 목록은 임계값 통과를 뜻하지 않는다."""
        settings = Settings(**tomllib.loads(self._server_toml.read_text(encoding="utf-8"))["settings"])
        models = load_models(self.config.model_root, {PLACE_ID: self.config.model_version})
        now = datetime.now(timezone.utc)
        conn = open_readonly(_uri_path(self._db_path))
        try:
            repo = SqliteRepository(conn, settings)  # 읽기 전용 연결이다. 마이그레이션을 실행하지 않는다.
            beacons, nodes = repo.beacons_of(user_id), repo.nodes()
            target = next((n for n in nodes if n.node_id == node_id), None)
            if len(beacons) != 1 or target is None:
                return False
            threshold = getattr(settings, f"threshold_{target.capability.value}")
            if threshold is None:
                return False
            place_nodes = [n for n in nodes if n.place_id == target.place_id]
            estimator = HmmProximityEstimator(models, settings)
            estimator.restore(repo.rssi_since(
                beacons, now - timedelta(seconds=models[PLACE_ID].window_sec)), nodes)
            result = estimator.posterior(beacons, [target], now, place_nodes=place_nodes)
            return result.posteriors.get(node_id, 0.0) >= threshold
        finally:
            conn.close()

    def wait_until(self, predicate: Callable[[], bool], timeout_sec: float) -> bool:
        """`predicate`가 참이 될 때까지 기다린다. 제한 시간이 지나면 False다. 브로커나 서버가 끝났으면 `HarnessError`다."""
        deadline = time.monotonic() + timeout_sec
        while not predicate():
            self._check_alive()
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SEC)
        return True

    def _http(self, method: str, path: str, user_id: str | None, body: Any) -> tuple[int, bytes]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if user_id is not None:
            headers["Authorization"] = f"Bearer {self.tokens[user_id]}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"http://127.0.0.1:{self._http_port}{path}", data=data, method=method,
                                         headers=headers)
        try:
            with _OPENER.open(request, timeout=self.config.startup_timeout_sec) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:  # 4xx, 5xx도 응답이다
            with error:
                return error.code, error.read()
        except OSError as error:  # URLError, 제한 시간, 연결 끊김
            self._check_alive()
            raise HarnessError(f"{method} {path} 요청에 실패했다: {error}") from error

    # ── 시작의 조각 ──────────────────────────────────────────────────

    def _check_model(self) -> None:
        """장소의 trusted 기기가 모두 근접 모델에 있어야 한다. 하나라도 없으면 서버는 모든 요청에서 모델 오류를 낸다."""
        config = self.config
        try:
            model = load_models(config.model_root, {PLACE_ID: config.model_version})[PLACE_ID]
        except (OSError, ValueError, LookupError) as error:
            raise HarnessError(f"근접 모델을 읽을 수 없다 ({config.model_root}/{PLACE_ID}/{config.model_version}.json): "
                               f"{error}") from error
        missing = sorted(node.node_id for node in config.nodes if node.trusted and node.node_id not in model.nodes)
        if missing:
            raise HarnessError(f"근접 모델 {config.model_version}에 없는 trusted 기기가 있다: {missing} "
                               f"(모델의 기기: {sorted(model.nodes)})")

    def _secret_file(self, account: str) -> Path:
        """계정의 원문 비밀번호 파일. `.env.*`는 커밋하지 않는 파일 이름이다."""
        return self.workdir / f".env.mqtt-{account}"

    def _write_secrets(self, mosquitto_passwd: Path) -> None:
        """서버 계정과 기기 계정마다 새 비밀번호를 만들어 원문 파일과 브로커의 암호화된 비밀번호 파일에 쓴다."""
        accounts = (SERVER_ACCOUNT, *(node.node_id for node in self.config.nodes))
        passwords = {account: secrets.token_hex(20) for account in accounts}
        for account, password in passwords.items():
            self._secret_file(account).write_text(password, encoding="utf-8")
        plain = self.workdir / "passwords"
        plain.write_text("".join(f"{account}:{password}\n" for account, password in passwords.items()),
                         encoding="utf-8")
        # 비밀번호 파일의 권한 경고는 stderr에 나오고 동작에는 상관없다
        hashed = subprocess.run([str(mosquitto_passwd), "-U", str(plain)], capture_output=True)
        if hashed.returncode != 0:
            raise HarnessError(f"mosquitto_passwd가 실패했다 (종료 코드 {hashed.returncode}): "
                               f"{hashed.stderr.decode('utf-8', 'replace').strip()}")

    def _write_broker_config(self, port: int) -> Path:
        """서버 계정과 기기별 계정이 각자의 토픽에만 접근하는 브로커 설정. ACL은 예시의 서버 절과 기기 절로 만든다."""
        text = (self.server_root / "config" / "mosquitto.acl.example").read_text(encoding="utf-8")
        start = text.index(f"user {SERVER_ACCOUNT}")
        end = text.find("\nuser ", start)  # 서버 절은 다음 계정 절 앞까지다
        acl = text[start:end + 1 if end != -1 else len(text)]
        for node in self.config.nodes:
            base = f"nearpilot/node/{node.node_id}"
            lines = [f"user {node.node_id}", *(f"topic write {base}/{kind}" for kind in REPORTS), f"topic read {base}/cmd"]
            acl += "\n" + "\n".join(lines) + "\n"
        acl_path = self.workdir / "mosquitto.acl"
        acl_path.write_text(acl, encoding="utf-8")
        conf = self.workdir / "mosquitto.conf"
        # 경로에 따옴표를 쓰지 않는다. Mosquitto가 따옴표까지 파일 이름으로 읽는다.
        conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous false\n"
                        f"password_file {(self.workdir / 'passwords').as_posix()}\nacl_file {acl_path.as_posix()}\n"
                        f"persistence false\n", encoding="utf-8")
        return conf

    def _write_server_config(self, example: Mapping[str, Any], settings: Mapping[str, Any], mqtt_port: int) -> None:
        """서버 설정 예시에서 포트, DB, 모델, 비밀번호 파일, 설정을 바꾼 서버 설정을 쓴다.

        기기의 시각 오차는 같은 PC의 시뮬레이터라서 0이다. 대시보드는 평가와 상관없어서 쓰지 않는다.
        """
        raw = dict(example)
        raw["settings"] = dict(settings)
        raw["http"] = {**raw["http"], "port": self._http_port}
        raw["database"] = {"path": str(self._db_path), "audit_jsonl": str(self.workdir / "audit.jsonl")}
        raw["proximity"] = {"model_root": str(self.config.model_root),
                            "models": {PLACE_ID: self.config.model_version}}
        raw["mqtt"] = {**raw["mqtt"], "port": mqtt_port, "password_file": str(self._secret_file(SERVER_ACCOUNT))}
        raw["delivery"] = {**raw["delivery"], "clock_skew_sec": {node.node_id: 0.0 for node in self.config.nodes}}
        raw.pop("dashboard", None)
        self._server_toml.write_text(_toml(raw), encoding="utf-8")

    def _seed(self, owner: str) -> dict[str, str]:
        """`nearpilot seed`로 계정, 비콘, 장소(소유자 `owner`), 기기 신뢰, 정책을 넣고 출력에서 토큰을 읽는다."""
        config = self.config
        seed = {
            "accounts": [{"user_id": user.user_id, "name": user.user_id, "role": user.role} for user in config.users],
            "beacons": [{"beacon_id": f"beacon-{user.user_id}", "user_id": user.user_id, "uuid": config.beacon_uuid,
                         "major": config.beacon_major, "minor": user.minor} for user in config.users],
            "places": [{"place_id": PLACE_ID, "owner": owner, "name": "평가 장소",
                        "nodes": [node.node_id for node in config.nodes]}],
            "nodes": [{"node_id": node.node_id, "trust": "trusted"} for node in config.nodes if node.trusted],
            "policies": [{"node_id": node.node_id, "visibility": "public", "mode": node.mode,
                          "approval_mode": node.approval_mode,
                          **({"approver": owner} if node.mode == "restricted" else {}),
                          **({"approval_ttl_sec": node.approval_ttl_sec} if node.approval_ttl_sec is not None else {}),
                          **({"max_uses": node.max_uses} if node.max_uses is not None else {})}
                         for node in config.nodes],
        }
        seed_path = self.workdir / "seed.toml"
        seed_path.write_text(_toml(seed), encoding="utf-8")
        try:
            done = subprocess.run(self._cli("seed", str(seed_path)), cwd=self.server_root, env=self._env(),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=config.startup_timeout_sec)
        except subprocess.TimeoutExpired:
            raise HarnessError(f"nearpilot seed가 {config.startup_timeout_sec:g}초 안에 끝나지 않았다") from None
        # 토큰은 한 번만 보여 주는 값이라서 오류 메시지에 싣지 않는다
        shown = "\n".join(line for line in (done.stdout + done.stderr).splitlines() if "토큰:" not in line)
        if done.returncode != 0:
            raise HarnessError(f"nearpilot seed가 실패했다 (종료 코드 {done.returncode})\n{shown}")
        tokens = _read_tokens(done.stdout)
        missing = [user.user_id for user in config.users if user.user_id not in tokens]
        if missing:
            raise HarnessError(f"nearpilot seed 출력에서 토큰을 읽지 못했다: {missing}\n{shown}")
        return tokens

    def _stored_nodes(self) -> set[str]:
        """서버 DB에 있는 기기 ID. DB가 아직 없으면 빈 집합이다."""
        try:
            return {row["node_id"] for row in self.query("SELECT node_id FROM node")}
        except sqlite3.OperationalError:
            return set()

    def _restart_server(self) -> None:
        """시드 뒤에 서버를 정상 종료하고 다시 띄운다.

        복원 서비스는 서버를 시작할 때의 기기 목록만 읽는다. 시작한 뒤에 처음 발표한 기기는 다시 시작하기 전까지
        후보가 되지 못해서 `request_use`가 `NO_CANDIDATE`가 된다. 실행 가능 검사를 우회하지 않고 운영과 같은
        종료와 시작을 거친다 (specs/core-node-lab/spec.md 시험 도구와 같은 방식).
        """
        stopped, self._server = self._server, None
        self._processes.remove(stopped)
        _terminate(stopped)
        self._server = self._spawn("serve", self._server_command(), cwd=self.server_root, env=self._env())
        self._wait_listening("serve", self._http_port, self._server)

    def _wait_restored(self) -> None:
        """다시 띄운 서버가 trusted 기기를 모두 복원해서 실행할 수 있을 때까지 기다린다.

        복원 상태는 서버 밖에서 보이지 않는다. 서버는 기기의 상태 보고를 받아서 복원을 마친다. 같은 기기의 보고는
        기기별 잠금 안에서 `last_seen_at` 갱신, 복원 인계 순서로 처리한다. 그래서 다시 띄운 뒤 `last_seen_at`이
        두 번 더 바뀌었다면 앞 보고의 복원 인계가 끝났다.
        """
        trusted = {spec.node_id for spec in self.config.nodes if spec.trusted}
        seen = {node_id: {at} for node_id, at in self._last_seen().items() if node_id in trusted}

        def restored() -> bool:
            for node_id, at in self._last_seen().items():
                if node_id in seen:
                    seen[node_id].add(at)
            return all(len(times) >= 3 for times in seen.values())  # 다시 띄운 때의 값과 새 값 둘

        if not self.wait_until(restored, self.config.startup_timeout_sec):
            waiting = sorted(node_id for node_id, times in seen.items() if len(times) < 3)
            raise HarnessError(f"서버가 기기의 상태 보고를 받지 못했다: {waiting}\n{self._log_tail('serve')}")

    def _last_seen(self) -> dict[str, str | None]:
        """기기별로 서버가 마지막으로 받은 시각 (ISO 문자열)."""
        return {row["node_id"]: row["last_seen_at"] for row in self.query("SELECT node_id, last_seen_at FROM node")}

    # ── 프로세스 ─────────────────────────────────────────────────────

    def _cli(self, *args: str) -> list[str]:
        """이 checkout의 `nearpilot` 명령. 서버 설정은 하네스가 쓴 파일이다."""
        return [sys.executable, "-c", _CLI, "--config", str(self._server_toml), *args]

    def _server_command(self) -> list[str]:
        return self._cli("serve")

    def _env(self) -> dict[str, str]:
        """이 checkout의 코드를 읽게 하는 환경. 메인 checkout이나 다른 경로의 `nearpilot`을 읽지 않는다."""
        paths = os.pathsep.join(str(path) for path in (self.server_root / "src", self.server_root))
        return {**os.environ, "PYTHONPATH": paths, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8"}

    def _spawn(self, name: str, command: list[str], **kwargs: Any) -> subprocess.Popen:
        log = (self.workdir / f"{name}.log").open("ab")  # 다시 띄운 서버의 로그가 앞 로그를 지우지 않게 이어 쓴다
        self._logs.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)
        self._processes.append(process)
        return process

    def _wait_listening(self, name: str, port: int, process: subprocess.Popen) -> None:
        """프로세스가 `port`를 열 때까지 기다린다. 먼저 끝나거나 제한 시간이 지나면 로그 끝을 담아 `HarnessError`다."""
        timeout = self.config.startup_timeout_sec
        deadline = time.monotonic() + timeout
        while not _listening(port):
            code = process.poll()
            if code is not None:
                raise HarnessError(f"{name}이(가) 포트를 열기 전에 끝났다 (종료 코드 {code})\n{self._log_tail(name)}")
            if time.monotonic() >= deadline:
                raise HarnessError(f"{name}이(가) {timeout:g}초 안에 포트 {port}를 열지 않았다\n{self._log_tail(name)}")
            time.sleep(_POLL_SEC)

    def _check_alive(self) -> None:
        """브로커나 서버가 끝났으면 기다려도 소용없으므로 로그 끝을 담아 `HarnessError`를 낸다."""
        for name, process in (("broker", self._broker), ("serve", self._server)):
            if process is not None and process.poll() is not None:
                raise HarnessError(f"{name}이(가) 끝났다 (종료 코드 {process.returncode})\n{self._log_tail(name)}")

    def _log_tail(self, name: str) -> str:
        try:
            text = (self.workdir / f"{name}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"({name} 로그를 읽을 수 없다)"
        return "\n".join(text.splitlines()[-_LOG_TAIL_LINES:])


def _first_owner(config: EvalConfig) -> str:
    """장소의 소유자이자 restricted 기기의 승인자다. 설정을 읽을 때 owner가 한 명 이상 있음을 확인했다."""
    return next(user.user_id for user in config.users if user.role == "owner")


def _free_ports(count: int) -> list[int]:
    """비어 있는 localhost 포트 `count`개. 같은 포트가 겹치지 않게 모두 연 뒤에 닫는다."""
    probes = [socket.socket() for _ in range(count)]
    try:
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        return [probe.getsockname()[1] for probe in probes]
    finally:
        for probe in probes:
            probe.close()


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def _terminate(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=_STOP_GRACE_SEC)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _uri_path(path: Path) -> str:
    """읽기 전용 URI(`file:<경로>?mode=ro`)에 넣을 경로 (cli/common.py의 `_uri_path`와 같다).

    폴더 이름의 `?`, `#`, `%`와 공백이 URI 문법으로 읽히지 않게 퍼센트 인코딩한다.
    Windows 드라이브 문자(`C:/…`)는 앞에 `/`를 붙여 `file:/C:/…`가 되게 한다.
    """
    posix = path.resolve().as_posix()
    if posix[1:3] == ":/":
        posix = "/" + posix
    return quote(posix, safe="/:")


def _decode(raw: bytes) -> Any:
    """응답 본문. JSON이면 읽은 값, 아니면 문자열, 비어 있으면 None이다."""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _tool_result(payload: Any) -> dict[str, Any] | None:
    """JSON-RPC 응답의 `result.content[0].text`를 JSON 객체로 읽는다. 읽을 수 없으면 None이다."""
    try:
        result = json.loads(payload["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _read_tokens(output: str) -> dict[str, str]:
    """`nearpilot seed` 출력에서 `계정 <ID>: 등록` 다음 줄의 토큰을 읽는다."""
    tokens: dict[str, str] = {}
    user_id = None
    for line in output.splitlines():
        account = _ACCOUNT_LINE.match(line)
        if account:
            user_id = account.group(1)
            continue
        token = _TOKEN_LINE.match(line)
        if token and user_id is not None:
            tokens[user_id] = token.group(1)
            user_id = None
    return tokens


def _toml(data: Mapping[str, Any], path: tuple[str, ...] = ()) -> str:
    """스칼라, 스칼라 배열, 표, 표 배열로만 이뤄진 매핑을 TOML 문장으로 쓴다. 표는 값 뒤에 나온다."""
    text = "".join(f"{_toml_key(key)} = {_toml_value(value)}\n" for key, value in data.items()
                   if not _is_table(value) and not _is_table_array(value))
    for key, value in data.items():
        name = ".".join(_toml_key(part) for part in (*path, key))
        if _is_table(value):
            text += f"\n[{name}]\n{_toml(value, (*path, key))}"
        elif _is_table_array(value):
            text += "".join(f"\n[[{name}]]\n{_toml(item, (*path, key))}" for item in value)
    return text


def _is_table(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(isinstance(item, Mapping) for item in value)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # TOML 기본 문자열은 JSON 이스케이프를 받는다
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"TOML로 쓸 수 없는 값이다: {value!r:.40}")
