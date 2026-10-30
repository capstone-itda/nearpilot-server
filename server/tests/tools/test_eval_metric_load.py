"""NFR-03 지표 3 부하 생성과 NFR-01 지표 7 서버 판정 지연 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

집계 함수(`overlapping_sessions`, `duplicate_executions`, `latency_summary`, 응답 집계, 행 만들기)는 합성 원자료로 시험한다.
`run_metric3`·`run_metric7`의 보내는 방식은 `Harness`를 흉내 내는 가짜로 시험한다 (프로세스를 띄우지 않는다).
실제 Mosquitto와 `nearpilot serve`와 시뮬레이터를 띄우는 시험은 지표마다 하나(`runs_on_harness`)다.
Mosquitto가 없으면 그 둘을 건너뛴다 (`MOSQUITTO_EXECUTABLE`, `MOSQUITTO_PASSWD_EXECUTABLE`, PATH, 기본 설치 폴더).
"""

from __future__ import annotations

import dataclasses
import json
import random
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.shared.enums import ReasonCode, ReserveOutcome, Step, Verdict
from tools.evaluation import metric_load
from tools.evaluation.harness import Harness, HarnessError, find_mosquitto
from tools.evaluation.metric_load import (
    duplicate_executions, latency_summary, overlapping_sessions, run_metric3, run_metric7)
from tools.evaluation.server_config import EvalConfigError, load_eval_config
from tools.node_sim.store import CommandRecord

SERVER = Path(__file__).resolve().parents[2]
EXAMPLE = SERVER / "config" / "eval" / "server-metrics.example.toml"
UNMEASURED = "미측정: LLM 호스트 실행 필요"


@pytest.fixture
def mosquitto():
    try:
        return find_mosquitto()
    except HarnessError as error:
        pytest.skip(f"Real Mosquitto and mosquitto_passwd are required ({error})")


def with_counts(**counts):
    """예시 설정에서 횟수만 바꾼다."""
    config = load_eval_config(EXAMPLE)
    return dataclasses.replace(config, counts={**config.counts, **counts})


def session(use_id, node_id, started_at, closed_at):
    return {"use_id": use_id, "node_id": node_id, "started_at": started_at, "closed_at": closed_at}


def response(tool, verdict, reason, status=200):
    """`metric_load`이 모으는 응답 한 건."""
    return {"tool": tool, "user_id": "visitor-1", "request_id": "r", "use_id": None, "status": status,
            "verdict": verdict, "reason": reason, "node_id": None, "elapsed_sec": 0.01}


class FakeHarness:
    """`Harness`의 MCP 호출과 DB 질의를 흉내 낸다. 요청은 모두 `OK`이고 저장된 세션과 명령은 없다.

    `request_use`의 왕복 시간은 `request_seconds`에서 차례로 꺼내고, 다른 도구는 크게 돌려줘서 섞이면 값이 달라진다.
    처음 `first_wave`번의 `request_use`는 서로 만나야 지나가고, 재전송(`m3-rt-`)은 같은 `request_id`끼리 만나야 지나간다.
    호출이 동시에 오지 않으면 장벽이 깨져서 호출이 실패한다.
    """

    OTHER_SEC = 9.0

    def __init__(self, config, *, request_seconds=(), first_wave=0):
        self.config = config
        self.calls = []  # (user_id, tool, arguments)
        self.heard = {}  # node_id → {user_id: rssi}
        self._request_seconds = iter(request_seconds)
        self._lock = threading.Lock()
        self._wave = threading.Barrier(first_wave, timeout=10) if first_wave else None
        self._wave_left = first_wave
        self._pairs = {}

    def set_rssi(self, node_id, heard):
        self.heard[node_id] = dict(heard)

    def mcp(self, user_id, tool, arguments):
        with self._lock:
            self.calls.append((user_id, tool, dict(arguments)))
        if tool == "find_available_devices":
            return 200, {"devices": [{"node_id": node.node_id} for node in self.config.nodes]}, self.OTHER_SEC
        if tool == "release_use":
            return 200, {"verdict": "OK", "reason": "OK", "use_id": arguments["use_id"]}, self.OTHER_SEC
        self._meet(arguments["request_id"])
        with self._lock:
            seconds = next(self._request_seconds, 0.001)
        result = {"verdict": "OK", "reason": "OK", "node_id": "locker-1", "use_id": f"use-{arguments['request_id']}"}
        return 200, result, seconds

    def query(self, sql, *params):
        return []

    def wait_until(self, predicate, timeout_sec):
        return predicate()

    def proximity_ready(self, user_id, node_id):
        return True

    def _meet(self, request_id):
        with self._lock:
            if request_id.startswith("m3-rt-"):
                barrier = self._pairs.setdefault(request_id, threading.Barrier(2, timeout=10))
            elif self._wave_left:
                self._wave_left -= 1
                barrier = self._wave
            else:
                return
        barrier.wait()


class DbHarness(FakeHarness):
    """`FakeHarness`에 서버 DB와 시뮬레이터의 명령 기록을 붙인다. 질의는 실제 SQL로 돈다."""

    def __init__(self, config, db, stored):
        super().__init__(config)
        self.db = db
        self.nodes = {node.node_id: SimpleNamespace(store=SimpleNamespace(command=stored.get)) for node in config.nodes}

    def query(self, sql, *params):
        return self.db.execute(sql, params).fetchall()


# ── 이중 점유와 중복 실행 판정 ────────────────────────────────────────


def test_NFR03_load_overlapping_sessions():
    rows = [
        # a와 b는 겹친다. b가 a의 끝보다 먼저 시작한다
        session("a", "locker-1", "2026-10-26T10:00:00+00:00", "2026-10-26T10:00:05+00:00"),
        session("b", "locker-1", "2026-10-26T10:00:03.500000+00:00", "2026-10-26T10:00:08+00:00"),
        # c는 b의 끝과 맞닿는다. 겹침이 아니다
        session("c", "locker-1", "2026-10-26T10:00:08+00:00", "2026-10-26T10:00:09+00:00"),
        # 다른 기기가 같은 시각을 쓰는 것은 겹침이 아니다
        session("d", "locker-2", "2026-10-26T10:00:00+00:00", "2026-10-26T10:00:09+00:00"),
    ]

    assert overlapping_sessions(rows) == [("locker-1", "a", "b")]
    assert overlapping_sessions(rows[2:]) == []  # 맞닿기만 하는 세션과 다른 기기뿐이다
    assert overlapping_sessions([]) == []

    open_rows = [
        session("open", "locker-1", "2026-10-26T10:00:10+00:00", None),  # 아직 닫히지 않았다
        session("after-1", "locker-1", "2026-10-26T10:00:11+00:00", "2026-10-26T10:00:12+00:00"),
        session("after-2", "locker-1", "2026-10-26T10:00:20+00:00", None),
        session("before", "locker-1", "2026-10-26T10:00:00+00:00", "2026-10-26T10:00:10+00:00"),  # 열린 세션과 맞닿는다
    ]
    assert overlapping_sessions(open_rows) == [("locker-1", "open", "after-1"), ("locker-1", "open", "after-2")]


def test_NFR03_load_overlapping_sessions_compare_instants():
    utc = datetime(2026, 10, 26, 16, 0, 6, tzinfo=timezone.utc)
    rows = [
        session("a", "locker-1", "2026-10-26T16:00:00+00:00", "2026-10-26T16:00:05+00:00"),
        # 오프셋이 다른 표기다. 16:00:04Z라서 a와 겹치지만 문자열로는 a의 끝보다 뒤다
        session("b", "locker-1", "2026-10-27T01:00:04+09:00", utc),
        # datetime으로 준 시각이 b의 끝과 맞닿는다
        session("c", "locker-1", utc, "2026-10-26T16:00:07+00:00"),
    ]

    assert overlapping_sessions(rows) == [("locker-1", "a", "b")]
    assert overlapping_sessions([rows[0], rows[0]]) == []  # 같은 세션이 두 번 와도 자기와 겹치지 않는다


def server_db():
    """지표 3의 질의가 읽는 서버 테이블만 가진 빈 DB."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE request (internal_request_id TEXT, request_id TEXT);
        CREATE TABLE use_session (use_id TEXT, internal_request_id TEXT, node_id TEXT, reserved_at TEXT, closed_at TEXT);
        CREATE TABLE audit_log (internal_request_id TEXT, step TEXT, payload_json TEXT, created_at TEXT);
        CREATE TABLE node_command (command_id TEXT, internal_request_id TEXT, node_id TEXT);
    """)
    return db


def test_NFR03_load_sessions_start_at_reservation_audit_time():
    db = server_db()
    sessions = [  # (use_id, 요청, 요청을 받은 시각 reserved_at, 닫힌 시각)
        ("a", "ra", "2026-10-26T10:00:00+00:00", "2026-10-26T10:00:05+00:00"),
        ("b", "rb", "2026-10-26T10:00:01+00:00", "2026-10-26T10:00:09+00:00"),  # a가 닫히기 전에 받았고 닫힌 뒤에 예약했다
        ("c", "rc", "2026-10-26T10:00:20+00:00", None),  # 예약 감사 기록이 없다
    ]
    audits = [  # (요청, 단계, 결과, 남긴 시각)
        ("ra", Step.DEVICE_OCCUPANCY, ReserveOutcome.RESERVED, "2026-10-26T10:00:00.200000+00:00"),
        ("rb", Step.DEVICE_OCCUPANCY, ReserveOutcome.RESERVED, "2026-10-26T10:00:05.100000+00:00"),
        ("rc", Step.DEVICE_OCCUPANCY, ReserveOutcome.DEVICE_BUSY, "2026-10-26T10:00:20.100000+00:00"),  # 예약이 아니다
        ("rc", Step.BEACON, ReserveOutcome.RESERVED, "2026-10-26T10:00:20.200000+00:00"),  # 단계가 다르다
    ]
    db.executemany("INSERT INTO request VALUES (?, ?)", [("ra", "m3-0"), ("rb", "m3-1"), ("rc", "m3-rt-0")])
    db.executemany("INSERT INTO use_session VALUES (?, ?, 'locker-1', ?, ?)", sessions)
    db.executemany("INSERT INTO audit_log VALUES (?, ?, ?, ?)", [
        (request, step.value, json.dumps({"outcome": outcome}), created_at) for request, step, outcome, created_at in audits])

    rows = db.execute(metric_load._SESSIONS, (Step.DEVICE_OCCUPANCY.value, ReserveOutcome.RESERVED.value,
                                              len(metric_load._PREFIX), metric_load._PREFIX)).fetchall()

    assert {row["use_id"]: row["started_at"] for row in rows} == {  # 감사 기록이 없는 세션은 reserved_at이다
        "a": "2026-10-26T10:00:00.200000+00:00", "b": "2026-10-26T10:00:05.100000+00:00",
        "c": "2026-10-26T10:00:20+00:00"}
    assert overlapping_sessions(rows) == []
    by_arrival = [session(use_id, "locker-1", received, closed) for use_id, _, received, closed in sessions]
    assert overlapping_sessions(by_arrival) == [("locker-1", "a", "b")]  # 요청을 받은 시각으로 보면 겹쳐 보인다


def test_NFR03_load_duplicate_executions():
    assert duplicate_executions({"req-1": 1, "req-3": 3, "req-2": 2, "req-0": 0}) == ["req-2", "req-3"]
    assert duplicate_executions({"req-1": 1}) == []
    assert duplicate_executions({}) == []


def test_NFR03_load_executed_commands_count_actuated_by_original_request():
    def record(state, error_code=None):
        return CommandRecord("c", "{}", state, "e", "2026-10-26T10:00:00Z", error_code)

    stored = {  # 시뮬레이터가 남긴 명령 기록
        "c1": record("running"), "c2": record("succeeded"),  # r1: 둘 다 구동했다
        "c3": record("rejected", "DEVICE_BUSY"),  # r2: 거부했다
        "c4": record("failed", "STORAGE_ERROR"), "c5": record("accepted"),  # r3: 시작 전에 실패했거나 접수만 했다
        "c6": record("failed"),  # r4: 구동한 뒤 실패했다
        "c7": record("unknown"),  # r5: 구동했는지 알 수 없다
    }
    commands = [("c1", "r1"), ("c2", "r1"), ("c3", "r2"), ("c4", "r3"), ("c5", "r3"), ("c6", "r4"), ("c7", "r5"),
                ("c8", "r6")]  # c8은 시뮬레이터에 기록이 없다 (보내지 못했다)
    harness = SimpleNamespace(
        query=lambda sql, *params: [
            {"command_id": command_id, "internal_request_id": request_id, "node_id": "locker-1"}
            for command_id, request_id in commands],
        nodes={"locker-1": SimpleNamespace(store=SimpleNamespace(command=stored.get))})

    executed = metric_load._executed_commands(harness)

    assert executed == {"r1": 2, "r4": 1, "r5": 1}
    assert duplicate_executions(executed) == ["r1"]


def test_NFR03_load_counts_only_its_own_requests():
    def at(second):
        return f"2026-10-26T10:00:{second:02d}+00:00"

    def fill(requests):
        """요청마다 세션 하나와 구동한 명령을 DB와 시뮬레이터 기록에 넣는다. 요청은 (요청 ID, 시작, 끝, 명령 수)다."""
        db, stored = server_db(), {}
        for request_id, started, closed, commands in requests:
            internal = f"internal-{request_id}"
            db.execute("INSERT INTO request VALUES (?, ?)", (internal, request_id))
            db.execute("INSERT INTO use_session VALUES (?, ?, 'locker-1', ?, ?)", (f"use-{request_id}", internal, started, closed))
            db.execute("INSERT INTO audit_log VALUES (?, ?, ?, ?)", (
                internal, Step.DEVICE_OCCUPANCY.value, json.dumps({"outcome": ReserveOutcome.RESERVED.value}), started))
            for number in range(commands):
                command_id = f"{request_id}-{number}"
                db.execute("INSERT INTO node_command VALUES (?, ?, 'locker-1')", (command_id, internal))
                stored[command_id] = CommandRecord(command_id, "{}", "succeeded", "e", at(0), None)
        return db, stored

    own = [("m3-0", at(0), at(5), 1), ("m3-rt-0", at(5), at(9), 1)]  # 맞닿을 뿐 겹치지 않고, 한 번씩 구동했다
    others = [  # 같은 기기를 쓴 다른 지표의 요청. 거르지 않으면 지표 3의 세션과 겹치고 중복 구동이 생긴다
        ("m1-0", at(1), at(4), 1), ("m4-1", at(2), None, 2), ("m5-explicit-000-rent", at(3), at(8), 1),
        ("m6-leave-001-rent", at(4), at(6), 2), ("m7-0", at(6), None, 1),
        ("m30-0", at(7), at(8), 2),  # 접두어와 비슷하지만 `m3-`로 시작하지 않는다
    ]
    config = with_counts(m3_requests=2, m3_retransmits=1, m3_clients=2)

    alone = run_metric3(DbHarness(config, *fill(own)), config, random.Random(1))
    mixed = run_metric3(DbHarness(config, *fill(own + others)), config, random.Random(1))

    assert (alone.raw["sessions"], alone.raw["overlaps"], alone.raw["duplicates"]) == (2, [], [])
    assert alone.raw["executed"] == {"internal-m3-0": 1, "internal-m3-rt-0": 1}
    assert mixed.rows == alone.rows  # trials·failures·passed·note가 다른 지표의 요청에 흔들리지 않는다
    for key in ("sessions", "overlaps", "executed", "duplicates"):
        assert mixed.raw[key] == alone.raw[key]
    everything = fill(own + others)[0].execute(
        "SELECT use_id, node_id, reserved_at AS started_at, closed_at FROM use_session").fetchall()
    assert overlapping_sessions(everything)  # 거르지 않으면 이중 점유로 보였을 자료다


def test_NFR03_load_counts_lock_errors():
    responses = [
        response("request_use", "OK", "OK"),
        response("request_use", "BUSY", "DEVICE_BUSY"),
        response("request_use", "DENY", "INTERNAL_ERROR"),
        response("request_use", "DENY", "INTERNAL_ERROR"),
        response("release_use", "DENY", "INTERNAL_ERROR"),
        response("request_use", None, None, status=500),  # 잠금 오류로 서버가 500을 낸 응답
        response("release_use", "OK", "OK"),
    ]
    config = load_eval_config(EXAMPLE)

    tally = metric_load._tally(responses)
    double, duplicate, errors = metric_load._metric3_rows(config, [], {"req-1": 1}, tally, 1)

    assert (tally["internal_errors"], tally["http_errors"]) == (3, 1)
    assert tally["decisions"] == {
        "release_use": {"DENY/INTERNAL_ERROR": 1, "OK/OK": 1},
        "request_use": {"BUSY/DEVICE_BUSY": 1, "DENY/INTERNAL_ERROR": 2, "HTTP 500": 1, "OK/OK": 1},
    }
    assert errors["metric"] == "m3_lock_errors"
    assert errors["value"] == 4
    assert (errors["passed"], errors["failures"]) == ("", "")  # 오류는 실패로 세지 않는다
    assert "D8: 클라이언트 수는 측정 뒤 정한다" in errors["note"]
    assert (double["passed"], double["failures"]) == (True, 0)  # 이중 점유와 중복 실행은 저장된 결과로 판정한다
    assert (duplicate["passed"], duplicate["failures"]) == (True, 0)


def test_NFR03_load_rows_judge_overlap_and_duplicates():
    config = load_eval_config(EXAMPLE)
    tally = metric_load._tally([])

    clean = metric_load._metric3_rows(config, [], {"req-1": 1, "req-2": 1}, tally, 2)
    broken = metric_load._metric3_rows(
        config, [("locker-1", "a", "b")], {"req-1": 2, "req-2": 3, "req-3": 1}, tally, 3)

    assert [row["metric"] for row in clean] == ["m3_double_occupancy", "m3_duplicate_execution", "m3_lock_errors"]
    assert "기기를 구동한 요청 2건, 예약에서 막힌 요청(DEVICE_BUSY) 0건" in clean[0]["note"]
    busy = metric_load._tally([response("request_use", "BUSY", "DEVICE_BUSY")] * 5 + [response("release_use", "OK", "OK")])
    blocked = metric_load._metric3_rows(config, [], {"req-1": 1}, busy, 1)
    assert "예약에서 막힌 요청(DEVICE_BUSY) 5건" in blocked[0]["note"]
    assert blocked[0]["trials"] == 6  # 예약까지 닿은 요청: 만든 세션 1 + DEVICE_BUSY 5
    double, duplicate = clean[:2]
    assert (double["limit"], double["passed"], double["trials"], double["failures"]) == (
        config.acceptance["m3_max_double_occupancy"], True, 2, 0)  # 보낸 요청 수가 아니라 예약까지 닿은 수
    assert (duplicate["value"], duplicate["limit"], duplicate["passed"], duplicate["trials"], duplicate["failures"]) == (
        1, config.acceptance["m3_max_duplicate_execution"], True, 2, 0)  # 기기를 구동한 요청 수
    double, duplicate = broken[:2]
    assert (double["passed"], double["failures"]) == (False, 1)
    assert (duplicate["value"], duplicate["passed"], duplicate["failures"]) == (3, False, 2)  # 2회 이상 구동한 요청 둘


def reply(verdict, reason, status=200):
    return status, None if verdict is None else {"verdict": verdict, "reason": reason}


@pytest.mark.parametrize(("replies", "calls"), [
    pytest.param([reply(Verdict.BUSY, ReasonCode.COMMAND_IN_PROGRESS), reply(Verdict.DENY, ReasonCode.INTERNAL_ERROR),
                  reply(None, None, status=500), reply(Verdict.OK, ReasonCode.OK)], 4, id="retries-transient"),
    pytest.param([reply(Verdict.OK, ReasonCode.OK)], 1, id="stops-on-ok"),
    pytest.param([reply(Verdict.DENY, ReasonCode.USE_NOT_FOUND)], 1, id="stops-on-not-found"),
    pytest.param([reply(Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE)], 1, id="stops-on-unavailable"),
])
def test_NFR03_load_release_retries_only_transient_replies(replies, calls):
    class Scripted:
        def __init__(self):
            self.left = list(replies)

        def mcp(self, user_id, tool, arguments):
            status, result = self.left.pop(0)
            return status, result, 0.001

        def wait_until(self, predicate, timeout_sec):
            return any(predicate() for _ in range(10))

    harness = Scripted()

    responses = metric_load._release(harness, load_eval_config(EXAMPLE), "visitor-1", "use-1")

    assert len(responses) == calls
    assert not harness.left  # 끝낼 응답이 온 뒤에는 더 부르지 않는다


# ── 부하를 보내는 방식 ────────────────────────────────────────────────


def test_NFR03_load_sends_concurrent_requests_and_retransmit_pairs():
    config = with_counts(m3_requests=8, m3_retransmits=3, m3_clients=4)
    harness = FakeHarness(config, first_wave=4)  # 처음 네 요청은 클라이언트 네 개가 동시에 보내야 지나간다

    outcome = run_metric3(harness, config, random.Random(1))

    sent = [(user_id, arguments) for user_id, tool, arguments in harness.calls if tool == "request_use"]
    assert Counter(arguments["request_id"] for _, arguments in sent) == Counter(
        {**{f"m3-{index}": 1 for index in range(8)}, **{f"m3-rt-{index}": 2 for index in range(3)}})
    for index in range(3):  # 재전송은 같은 사용자·같은 인자로 보낸다
        first, second = [call for call in sent if call[1]["request_id"] == f"m3-rt-{index}"]
        assert first == second
        assert first[1] == {"target_spec": "nearest:locker", "action": "open", "request_id": f"m3-rt-{index}"}
    released = [arguments["use_id"] for _, tool, arguments in harness.calls if tool == "release_use"]
    assert sorted(released) == sorted(f"use-{arguments['request_id']}" for _, arguments in sent)  # OK마다 반납한다

    visitors = [user.user_id for user in config.users if user.role == "visitor"]
    lockers = [node.node_id for node in config.nodes if node.capability == "locker" and node.trusted]
    near = {user_id: config.rssi["near"] for user_id in visitors}  # trusted 사물함마다 모든 방문자가 near로 들린다
    assert harness.heard == {node.node_id: near if node.node_id in lockers else {} for node in config.nodes}
    assert outcome.raw["lockers"] == lockers
    assert [row["metric"] for row in outcome.rows] == ["m3_double_occupancy", "m3_duplicate_execution", "m3_lock_errors"]
    assert (outcome.rows[0]["trials"], outcome.rows[1]["trials"]) == (0, 0)  # 가짜 하네스는 세션과 구동이 없다
    assert outcome.raw["clients"] == 4
    assert (outcome.raw["sessions"], outcome.raw["overlaps"], outcome.raw["duplicates"]) == (0, [], [])
    assert outcome.raw["tally"]["decisions"]["request_use"] == {"OK/OK": 14}
    json.dumps(outcome.raw)  # JSON으로 쓸 수 있다


def test_NFR03_load_rejects_config_without_visitor_or_trusted_locker():
    config = load_eval_config(EXAMPLE)
    no_visitor = dataclasses.replace(config, users=tuple(user for user in config.users if user.role != "visitor"))
    no_locker = dataclasses.replace(
        config, nodes=tuple(node for node in config.nodes if not (node.capability == "locker" and node.trusted)))

    for broken, missing in ((no_visitor, "방문자"), (no_locker, "사물함")):
        with pytest.raises(EvalConfigError, match=missing):
            run_metric3(FakeHarness(broken), broken, random.Random(1))
        with pytest.raises(EvalConfigError, match=missing):
            run_metric7(FakeHarness(broken), broken)


# ── 지표 7 ─────────────────────────────────────────────────────────────


def test_NFR01_metric7_latency_summary():
    seconds = [float(second) for second in range(1, 101)]

    median, p95 = latency_summary(random.Random(7).sample(seconds, len(seconds)))  # 순서와 상관없다

    assert median == pytest.approx(50.5)
    assert p95 == pytest.approx(95.05)  # 포함(inclusive) 방식의 95번째 백분위수
    assert latency_summary([0.1, 0.3]) == pytest.approx((0.2, 0.29))
    assert latency_summary([0.2]) == (0.2, 0.2)  # 표본이 하나여도 쓸 수 있다


def test_NFR01_metric7_host_rows_unmeasured():
    config = load_eval_config(EXAMPLE)

    rows = metric_load._metric7_rows(config, [0.1, 0.2, 0.3], granted=3)

    assert [row["metric"] for row in rows] == [
        "m7_server_latency_median", "m7_server_latency_p95", "m7_host_latency", "m7_host_agreement"]
    for row in rows[2:]:
        assert [row[column] for column in ("value", "limit", "passed", "trials", "failures")] == [""] * 5
        assert row["note"] == UNMEASURED
    median, p95 = rows[:2]
    assert (median["limit"], p95["limit"]) == (
        config.acceptance["m7_max_median_sec"], config.acceptance["m7_max_p95_sec"])
    assert (median["passed"], p95["passed"], median["trials"], p95["trials"]) == (True, True, 3, 3)
    assert "localhost MCP 왕복, 기기 구동 시간 포함 (OK 3/3)" in median["note"]

    slow = metric_load._metric7_rows(config, [1.5, 2.5, 3.5], granted=2)
    assert (slow[0]["passed"], slow[1]["passed"]) == (False, False)
    assert "(OK 2/3)" in slow[0]["note"]
    assert slow[2:] == rows[2:]


def test_NFR01_metric7_collects_only_request_use_latency():
    config = with_counts(m7_server_requests=3)
    harness = FakeHarness(config, request_seconds=[0.1, 0.4, 0.2])

    outcome = run_metric7(harness, config)

    assert outcome.raw["latencies"] == [0.1, 0.4, 0.2]  # 기기를 찾는 호출과 반납의 9초가 섞이지 않는다
    assert [tool for _, tool, _ in harness.calls] == ["find_available_devices", "request_use", "release_use"] * 3
    assert [arguments["request_id"] for _, tool, arguments in harness.calls if tool == "request_use"] == [
        "m7-0", "m7-1", "m7-2"]
    visitor = next(user.user_id for user in config.users if user.role == "visitor")
    target = next(node.node_id for node in config.nodes if node.capability == "locker" and node.trusted)
    assert {user_id for user_id, _, _ in harness.calls} == {visitor}  # 한 방문자가 보낸다
    assert harness.heard[target] == {visitor: config.rssi["near"]}
    assert [heard for node_id, heard in harness.heard.items() if node_id != target] == [{}] * (len(config.nodes) - 1)
    median, p95 = latency_summary([0.1, 0.4, 0.2])
    assert (outcome.rows[0]["value"], outcome.rows[1]["value"]) == (median, p95)
    json.dumps(outcome.raw)


def test_NFR01_metric7_discovery_does_not_replace_proximity_preparation(monkeypatch):
    config = with_counts(m7_server_requests=1)
    harness = FakeHarness(config)
    monkeypatch.setattr(harness, "proximity_ready", lambda user_id, node_id: False)
    with pytest.raises(HarnessError):
        run_metric7(harness, config)
    assert [tool for _, tool, _ in harness.calls] == ["find_available_devices"]


# ── 실제 프로세스 ──────────────────────────────────────────────────────


def test_NFR03_load_runs_on_harness(ascii_tmp_path, mosquitto):
    config = with_counts(m3_requests=20, m3_retransmits=5, m3_clients=4, m7_server_requests=3)

    with Harness(config, ascii_tmp_path / "work", server_root=SERVER) as harness:
        run_metric7(harness, config)  # 평가 명령은 지표 3 앞에서 다른 지표를 돌린다. 그 세션과 명령이 같은 DB에 있다
        others = harness.query("SELECT count(*) AS n FROM use_session")[0]["n"]
        outcome = run_metric3(harness, config, random.Random(config.seed))
        own = harness.query(
            "SELECT s.internal_request_id FROM use_session s JOIN request r ON r.internal_request_id = s.internal_request_id "
            "WHERE r.request_id LIKE 'm3-%'")
        own_requests = {row["internal_request_id"] for row in harness.query(
            "SELECT internal_request_id FROM request WHERE request_id LIKE 'm3-%'")}

    assert others >= 1  # 지표 7이 세션을 만들었으므로, 거르지 않으면 지표 3의 집계가 달라진다
    assert outcome.raw["sessions"] == len(own)  # 지표 3의 요청이 만든 세션만 센다
    assert set(outcome.raw["executed"]) <= own_requests
    double, duplicate, errors = outcome.rows
    assert [row["metric"] for row in outcome.rows] == ["m3_double_occupancy", "m3_duplicate_execution", "m3_lock_errors"]
    assert (double["failures"], double["passed"]) == (0, True)
    assert (duplicate["failures"], duplicate["passed"]) == (0, True)
    assert outcome.raw["overlaps"] == []
    assert outcome.raw["duplicates"] == []
    assert Counter(r["tool"] for r in outcome.raw["responses"])["request_use"] == 20 + 2 * 5
    assert outcome.raw["sessions"] >= 1  # 판정을 통과해 기기를 쓴 요청이 있어야 이중 점유 판정이 비지 않는다
    assert max(outcome.raw["executed"].values(), default=0) == 1  # 구동한 요청은 한 번씩만 구동했다
    assert errors["value"] == outcome.raw["tally"]["internal_errors"] + outcome.raw["tally"]["http_errors"]
    json.dumps(outcome.raw)


def test_NFR01_metric7_runs_on_harness(ascii_tmp_path, mosquitto):
    config = with_counts(m7_server_requests=5)

    with Harness(config, ascii_tmp_path / "work", server_root=SERVER) as harness:
        outcome = run_metric7(harness, config)

    latencies = outcome.raw["latencies"]
    assert len(latencies) == 5
    assert all(latency > 0 for latency in latencies)
    assert len(outcome.rows) == 4
    assert [row["metric"] for row in outcome.rows] == [
        "m7_server_latency_median", "m7_server_latency_p95", "m7_host_latency", "m7_host_agreement"]
    median, p95 = latency_summary(latencies)
    assert (outcome.rows[0]["value"], outcome.rows[1]["value"]) == (median, p95)
    granted = [r for r in outcome.raw["responses"] if r["tool"] == "request_use" and r["verdict"] == "OK"]
    assert granted  # 기기를 구동한 판정이 있어야 지연이 구동 시간을 포함한다
    json.dumps(outcome.raw)
