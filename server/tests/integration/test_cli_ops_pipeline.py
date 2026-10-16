"""운영 CLI 전체 흐름 (FR-16, specs/core-ops-cli/spec.md 5절·8절 `test_FR16_ops_pipeline_end_to_end`).

`seed → serve --collect-only → collect run → collect list → split → fit → eval → serve(모델로 시작)`를 한 테스트로 확인한다.
명령은 `nearpilot.cli.main`을 같은 프로세스에서 부르고, 서버는 실제 SQLite·runtime 조립·IOT 보고 처리 위에서
시뮬레이터 기기를 fake Paho 로 연결한다 (`_runtime.py`). 실제 uvicorn 은 띄우지 않는다.

수집 중 RSSI 는 DB 에 직접 넣지 않고 실제 서버 경로(MQTT 수신 → 검증 → 저장)로 들어온다. 수집 도구의 가짜 `SessionIO` 는
대기 시간을 기다리지 않는다. 1초 칸마다 기기의 상태와 RSSI 를 발행하고, 서버가 저장하기를 기다린 뒤 시계를 1초 앞으로 보낸다.
수집 도구, 서버(`Runtime(clock=…)`), 시뮬레이터 기기(`SimNode(clock=…)`)가 이 가짜 시계 하나를 본다. 그래서 관측 시각이 수신 시각과
같고, 시계가 실제 시간보다 앞서 가도 IOT 의 "미래 관측"·"오래된 관측" 검사를 통과한다.

데이터: 기기 둘(locker-1, locker-2)이 있는 장소 place-a. 라벨(기기 둘과 none)마다 20초 회차를 5개 수집하면 분할은 3/1/1이다.
RSSI 는 라벨마다 정한 평균에 (−1, 0, +1, 0) × 흔들림 폭을 차례로 더한 값이라서, 학습 회차에서 나온 앵커 값을 손으로 구할 수 있다
(`expected_anchor`). `window_secs` 는 예시 설정처럼 .5초 단위로 둔다.
"""

from __future__ import annotations

import csv
import itertools
import json
import math
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _runtime import UUID, SimBroker, query, running, wait_until
from nearpilot.cli import collect as collect_cli
from nearpilot.cli import main, serve as serve_cli
from nearpilot.core.proximity.estimator import HmmProximityEstimator
from nearpilot.core.proximity.model import load_model
from nearpilot.runtime import Runtime
from nearpilot.shared.models import BeaconKey
from nearpilot.shared.tokens import hash_token
from tools.data_collection.session import SessionIO
from tools.node_sim.node import SimNode

CONFIG = Path(__file__).resolve().parents[2] / "config"
PLACE = "place-a"
DEVICES = ("locker-1", "locker-2")
BEACON = BeaconKey(UUID, 1, 1)  # 수집하는 사람(owner)의 비콘. 예시 시드의 beacon-owner와 같다
ROUNDS, RUN_SEC = 5, 20
LABELS = (*DEVICES, "none") * ROUNDS  # 계획 파일의 회차 순서
# 기기별 (자기 앞, 다른 기기 앞, 어느 기기 앞도 아님) 평균 RSSI(dBm)와 흔들림 폭(dB). none 회차의 흔들림은 두 배다.
LEVELS = {"locker-1": (-44.0, -60.0, -80.0, 1.0), "locker-2": (-46.0, -64.0, -76.0, 2.0)}
JITTER = (-1.0, 0.0, 1.0, 0.0)  # 평균은 그대로 두고 표준편차만 0보다 크게 한다
FRONT = {"locker-1": (0.0, 0.5), "locker-2": (4.0, 0.5)}  # 기기 앞 위치. 기기는 (0, 0), (4, 0)에 있다
NONE_POINT = (2.0, 3.0)

SERVER_TOML = """\
[settings]
version = "t1"

[runtime]
execution_tick_sec = 0.05
release_tick_sec = 0.05
restore_tick_sec = 0.05
recovery_tick_sec = 0.05
shared_light_tick_sec = 0.1
shutdown_timeout_sec = 5.0

[http]
host = "127.0.0.1"
port = 8000

[database]
path = "../var/nearpilot.db"
audit_jsonl = "../var/audit.jsonl"

[proximity]
model_root = "../var/models"
{models}
[mqtt]
host = "127.0.0.1"
port = 1883
client_id = "test-server"
username = "nearpilot-server"
password_file = ".env.mqtt-password"
keepalive_sec = 30
connect_timeout_sec = 1.0
start_timeout_sec = 2.0
stop_timeout_sec = 2.0
reconnect_min_sec = 1
reconnect_max_sec = 2

[delivery]
pending_limit = 1000
retry_interval_sec = 0.05
stop_timeout_sec = 2.0

[delivery.clock_skew_sec]
locker-1 = 0.0
locker-2 = 0.0
"""
FIT_TOML = """\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
min_distance_m = 0.3
taus = [1.0]
stays = [0.9]
window_secs = [2.5]

[acceptance]
max_misselection = 0.05
max_ece = 0.05
"""


# ── 입력 파일 ─────────────────────────────────────────────────────────


def write_server(server: Path, models: dict[str, str] | None = None) -> None:
    """`config/server.toml`을 쓴다. `models`가 있으면 `[proximity.models]`를 넣는다 (설정에 모델 버전을 적는 일)."""
    config_dir = server / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env.mqtt-password").write_text("test-only-password\n", encoding="utf-8")
    table = "".join(f'\n[proximity.models]\n{place} = "{version}"\n' for place, version in (models or {}).items())
    (config_dir / "server.toml").write_text(SERVER_TOML.format(models=table), encoding="utf-8")


def write_inputs(server: Path) -> None:
    """사람이 쓰는 입력 파일: 시드, 수집 계획, 배치, 적합 설정. 이름은 `server/config/` 예시와 같다.

    시드는 저장소의 예시 파일을 그대로 복사한다 (README의 `nearpilot seed example.toml`이 실제 서버에 반영되는지 본다).
    나머지는 이 시뮬레이션의 회차 길이와 배치에 맞춘 값이다. 배치는 예시 배치와 같은 위치를 쓴다.
    """
    (server / "config" / "seed").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CONFIG / "seed" / "example.toml", server / "config" / "seed" / "example.toml")
    plan = {
        "place_id": PLACE, "beacon": {"uuid": BEACON.uuid, "major": BEACON.major, "minor": BEACON.minor},
        "settle_sec": 0, "run_sec": RUN_SEC, "runs": [{"label": label, "spot": f"{label} 앞"} for label in LABELS],
    }
    layout = {
        "place_id": PLACE,
        "nodes": {node_id: {"x": x, "y": 0.0, "front_x": FRONT[node_id][0], "front_y": FRONT[node_id][1]}
                  for node_id, x in zip(DEVICES, (0.0, 4.0))},
        "none_points": [list(NONE_POINT)],
    }
    files = {
        "plans/example.json": json.dumps(plan, ensure_ascii=False),
        "layouts/example.json": json.dumps(layout), "fit/example.toml": FIT_TOML,
    }
    for name, text in files.items():
        path = server / "config" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# ── 시계와 수집하는 사람 ───────────────────────────────────────────────


class FakeClock:
    """테스트 시계. 수집 도구, 서버, 시뮬레이터 기기가 모두 이 시계를 본다."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def observation_count(db: Path) -> int:
    return query(db, "SELECT count(*) c FROM rssi_observation")[0]["c"]


class Collector:
    """계획대로 기기 앞에 서는 사람. 가짜 `SessionIO` 로 수집 도구에 연결된다.

    회차마다 Enter(`read_line`)를 누르면 계획의 다음 라벨 자리에 선다. 대기(`sleep`)는 기다리지 않고 1초 칸마다
    기기의 상태와 RSSI 를 서버로 발행한 뒤 시계를 앞으로 보낸다. 칸마다 회차 안의 위치가 같아서 흔들림의 평균이 0이다.
    """

    def __init__(self, broker: SimBroker, clock: FakeClock, db: Path) -> None:
        self.broker, self.clock, self.db = broker, clock, db
        self.lines: list[str] = []  # 수집 도구가 사람에게 보여 준 줄
        self._labels = iter(LABELS)
        self._ids = itertools.count(1)
        self._label = ""
        self._step = 0

    def io(self) -> SessionIO:
        return SessionIO(clock=self.clock.now, sleep=self._sleep, read_line=self._enter, write=self.lines.append,
                         new_id=lambda: f"col-{next(self._ids):03d}")

    def _enter(self) -> str:
        self._label = next(self._labels)
        return ""

    def _sleep(self, seconds: float) -> None:
        whole, rest = divmod(seconds, 1.0)
        for _ in range(int(whole)):
            self._report()
            self.clock.advance(1.0)
        self.clock.advance(rest)

    def _report(self) -> None:
        """지금 시각의 보고: 기기마다 상태와 RSSI 하나. 서버가 모두 저장한 뒤에 돌아온다."""
        before = observation_count(self.db)
        for node_id, node in self.broker.nodes.items():
            self.broker.report(node_id, [node.status()])
            kind, body = node.rssi()
            body["observations"] = [{"uuid": BEACON.uuid, "major": BEACON.major, "minor": BEACON.minor,
                                     "rssi": self._rssi(node_id)}]
            self.broker.report(node_id, [(kind, body)])
        wait_until(lambda: observation_count(self.db) >= before + len(self.broker.nodes), interval=0.005)
        self._step += 1

    def _rssi(self, node_id: str) -> float:
        near, far, none, spread = LEVELS[node_id]
        if self._label == "none":
            mean, spread = none, 2 * spread
        else:
            mean = near if self._label == node_id else far
        return mean + spread * JITTER[self._step % len(JITTER)]


def use_clock(broker: SimBroker, clock: FakeClock) -> None:
    """시뮬레이터 기기가 `sent_at`, `observed_at`, `frame_ts` 를 테스트 시계로 쓰게 한다."""
    for node_id, node in list(broker.nodes.items()):
        broker.nodes[node_id] = SimNode(node.node_id, node.capability, node.occupancy, node.store, node.scenario,
                                        clock=clock.now)


# ── 기대값 ────────────────────────────────────────────────────────────


def expected_anchor(node_id: str, train_runs: int) -> tuple[float, float, float, float, float]:
    """학습 회차 데이터에서 손으로 구한 앵커 값 (A, n, sigma, none_rssi, none_sigma). 라벨마다 학습 회차가 `train_runs`개다.

    기기 앞 표본은 두 거리(자기 앞, 다른 기기 앞)에 있고, 거리마다 평균이 `near`, `far`라서 최소제곱 직선은 두 평균을 지난다.
    흔들림 (−1, 0, +1, 0) × s가 5주기(20개)면 평균은 0이고 제곱합은 10 s²이다.
    기기 앞 표본의 잔차 제곱합은 회차 2·`train_runs`개 × 10 s²이고 자유도는 40·`train_runs` − 2다.
    none 표본의 제곱합은 회차 `train_runs`개 × 10 (2s)²이고 자유도는 20·`train_runs` − 1이다.
    """
    near, far, none, spread = LEVELS[node_id]
    other = next(other_id for other_id in DEVICES if other_id != node_id)
    u_near = -10 * math.log10(math.dist((0.0 if node_id == DEVICES[0] else 4.0, 0.0), FRONT[node_id]))
    u_far = -10 * math.log10(math.dist((0.0 if node_id == DEVICES[0] else 4.0, 0.0), FRONT[other]))
    n = (near - far) / (u_near - u_far)
    a = (near + far) / 2 - n * (u_near + u_far) / 2
    sigma = spread * math.sqrt(2 * train_runs * 10 / (40 * train_runs - 2))
    none_sigma = spread * math.sqrt(train_runs * 40 / (20 * train_runs - 1))
    return a, n, sigma, none, none_sigma


def read_report(path: Path) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """보고서 CSV 의 머리말(`# 키: 값`)과 지표 행(`metric` → 행)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    header = dict(line[2:].split(": ", 1) for line in lines if line.startswith("# "))
    rows = {row["metric"]: row for row in csv.DictReader(line for line in lines if not line.startswith("#"))}
    return header, rows


# ── 시나리오 ──────────────────────────────────────────────────────────


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """`run_cli(argv, cwd=폴더)`: 실행 폴더를 바꾸고 `main(argv)`를 같은 프로세스에서 부른다. `(종료 코드, stdout, stderr)`."""

    def run(argv: list[str], *, cwd: Path) -> tuple[int, str, str]:
        monkeypatch.chdir(cwd)
        code = main(argv)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list:
    """`nearpilot serve`가 서버를 띄우려고 부른 `(설정, collect_only)`. 실제 uvicorn 은 띄우지 않는다."""
    calls: list = []
    monkeypatch.setattr(serve_cli, "run_server", lambda config, *, collect_only: calls.append((config, collect_only)) or 0)
    return calls


def test_FR16_ops_pipeline_end_to_end(tmp_path, monkeypatch, run_cli, served):
    server = tmp_path / "server"
    write_server(server)
    write_inputs(server)
    (server / "var").mkdir()  # `serve`는 DB 파일의 부모 폴더를 만들지 않으므로 사람이 먼저 만든 것으로 둔다
    db = server / "var" / "nearpilot.db"
    clock = FakeClock(datetime.now(timezone.utc).replace(microsecond=0))
    broker = SimBroker(tmp_path, DEVICES, beacons={node_id: () for node_id in DEVICES})
    use_clock(broker, clock)
    collector = Collector(broker, clock, db)

    def cli(*argv: str) -> tuple[int, str, str]:
        return run_cli(list(argv), cwd=server)

    try:
        # 1. 수집 전용 서버를 시작한다. 모델 표가 없는 설정이고, 기기가 발표하면 DB에 기기 행이 생긴다 (FR-05)
        assert cli("serve", "--collect-only") == (0, "", "")
        config, collect_only = served.pop()
        assert collect_only and not config.proximity.versions
        runtime = Runtime(config, client_factory=broker.factory, clock=clock.now, collect_only=True)
        with running(runtime):
            wait_until(lambda: {row["node_id"] for row in query(
                db, "SELECT node_id FROM node WHERE session_id IS NOT NULL AND last_seen_at IS NOT NULL")} == set(DEVICES))

            # 2. 시드: 기기가 발표된 뒤라서 모든 항목이 반영된다. 토큰은 계정마다 한 번만 나오고 DB 에는 해시만 있다 (FR-01)
            code, out, err = cli("seed", "example.toml")
            assert (code, err) == (0, "")
            tokens = dict(re.findall(r"계정 (\S+): 등록\n  토큰: (\S+)  \(다시 보여 주지 않는다\)", out))
            assert set(tokens) == {"owner", "visitor-1"} and out.count("토큰:") == 2
            stored = {row["user_id"]: row["token_hash"] for row in query(db, "SELECT user_id, token_hash FROM user")}
            assert stored == {user: hash_token(token) for user, token in tokens.items()}
            assert {row["node_id"]: (row["place_id"], row["trust_state"]) for row in query(
                db, "SELECT node_id, place_id, trust_state FROM node")} == {node_id: (PLACE, "trusted") for node_id in DEVICES}

            # 3. 수집: 계획의 회차를 차례로 실행한다. RSSI 는 시뮬레이터 기기가 MQTT 로 보내고 서버가 저장한다 (FR-16)
            monkeypatch.setattr(collect_cli, "_session_io", collector.io())
            code, out, err = cli("collect", "run", "example.json")
            assert (code, err) == (0, "") and collector.lines[-1] == "모든 회차를 마쳤다."
            (record,) = (server / "var" / "records").glob("example-*.jsonl")  # 기본 기록 파일
            entries = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
            assert [entry["label"] for entry in entries] == list(LABELS) and {entry["status"] for entry in entries} == {"done"}

            # 4. 조회: 라벨마다 닫힌 회차 5개, 프레임은 회차마다 기기 둘 × 20초다. 분할할 수 있다
            code, out, err = cli("collect", "list", "--summary", "--place", PLACE, "--fit", "example.toml")
            frames = ROUNDS * RUN_SEC * len(DEVICES)
            assert (code, err) == (0, "")
            assert out.splitlines() == ["라벨  닫힌 회차  프레임  분할 가능",
                                        *(f"{label}  {ROUNDS}  {frames}  예" for label in (*DEVICES, "none"))]
        # 수집이 끝났으니 수집 전용 서버를 끈다

        # 5. 분할: 회차 단위로 라벨마다 3/1/1이다
        code, out, err = cli("split", PLACE, "--fit", "example.toml")
        assert (code, err) == (0, "")
        lines = out.splitlines()
        assert lines[:4] == ["라벨  학습  검증  시험", *(f"{label}  3  1  1" for label in (*DEVICES, "none"))]
        assert len(lines) == 5 and lines[4].startswith("작성: ")  # 제외 회차 표가 없다
        split = json.loads((server / "var" / "splits" / "place-a-seed42.json").read_text(encoding="utf-8"))
        collected = [f"col-{k:03d}" for k in range(1, len(LABELS) + 1)]
        assert (len(split["train"]), len(split["validation"]), len(split["test"]), split["excluded"]) == (9, 3, 3, [])
        assert sorted(split["train"] + split["validation"] + split["test"]) == collected  # 모든 회차가 정확히 한 분할에 있다

        # 6. 적합: 학습 회차의 프레임이 DB 에서 모델 파일까지 이어진다. 값은 시뮬레이션한 RSSI 에서 손으로 구한 것과 같다
        code, out, err = cli("fit", "example.json", "--split", "place-a-seed42.json", "--version", "v1",
                             "--fit", "example.toml")
        model_path = server / "var" / "models" / PLACE / "v1.json"
        assert (code, err) == (0, "") and f'{PLACE} = "v1"' in out and model_path.is_file()
        model = load_model(model_path)
        assert set(model.nodes) == set(DEVICES) and model.none_points == (NONE_POINT,)
        for node_id in DEVICES:
            node = model.nodes[node_id]
            assert (node.A, node.n, node.sigma, node.none_rssi, node.none_sigma) == pytest.approx(
                expected_anchor(node_id, train_runs=3))
        assert sorted(model.validation["train_runs"]) == sorted(split["train"])
        assert sorted(model.validation["validation_runs"]) == sorted(split["validation"])

        # 7. 평가: 시험 회차 셋(기기 둘, none)만 읽는다. 평가 시각은 회차마다 18개(2.5초부터 19.5초까지)다
        code, out, err = cli("eval", f"{PLACE}/v1", "--split", "place-a-seed42.json", "--fit", "example.toml",
                             "--out", "pipeline")
        reports = server / "var" / "reports"
        assert (code, err) == (0, "") and (reports / "pipeline.md").is_file()
        header, rows = read_report(reports / "pipeline.csv")
        assert header["sample_count"] == str(3 * 18)
        assert list(rows) == ["front_misselection_rate", "ece", "switch_time", "flicker"]
        misselection = rows["front_misselection_rate"]
        assert (misselection["trials"], misselection["limit"]) == (str(2 * 18), "0.05")
        assert misselection["value"] != "" and float(misselection["value"]) <= float(misselection["limit"])
        assert misselection["passed"] == "True" and rows["ece"]["value"] != ""
        assert rows["switch_time"]["note"] == rows["flicker"]["note"] == "미측정"

        # 8. 설정에 모델 버전을 넣으면 일반 serve 가 그 모델로 시작한다. 넣기 전에는 시작하지 않는다
        assert cli("serve")[0] == 2 and not served
        write_server(server, models={PLACE: "v1"})
        assert cli("serve") == (0, "", "")
        config, collect_only = served.pop()
        assert not collect_only and dict(config.proximity.versions) == {PLACE: "v1"}
        built: list[dict] = []

        def proximity_factory(models, settings):
            built.append(dict(models))
            return HmmProximityEstimator(models, settings)

        runtime = Runtime(config, client_factory=broker.factory, clock=clock.now, proximity_factory=proximity_factory)
        with running(runtime):
            (models,) = built  # 수집한 데이터로 적합한 모델이 근접 추정기에 들어갔다
            assert set(models) == {PLACE} and models[PLACE] == model
            # 시드가 출력한 토큰으로 서버가 계정을 식별한다 (FR-01)
            assert runtime.authenticator.authenticate(f"Bearer {tokens['owner']}").user_id == "owner"
    finally:
        broker.close()
