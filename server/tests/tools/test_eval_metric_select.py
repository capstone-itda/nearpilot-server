"""NFR-05 지표 1(기기 선택)·지표 4(다중 사용자)와 FR-23 지표 9(전역 할당)의 절차와 집계 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

지표 9(`run_metric9`, `reference_allocation`, `allocation_matches`)는 서버 없이 순수 함수로 시험한다. 지표 1·4의 집계 함수는 합성 시도로,
보내는 방식은 `Harness`를 흉내 내는 가짜(`FakeHarness`)로 시험한다 (프로세스를 띄우지 않는다). 실제 Mosquitto와 `nearpilot serve`와
시뮬레이터를 띄우는 시험은 지표마다 하나(`runs_on_harness`)다. Mosquitto가 없으면 그 둘을 건너뛴다
(`MOSQUITTO_EXECUTABLE`, `MOSQUITTO_PASSWD_EXECUTABLE`, PATH, 기본 설치 폴더).
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.core.allocation.allocator import COST_TOLERANCE, AllocationRequest, allocate
from nearpilot.shared.config import Settings
from tools.evaluation import metric_select
from tools.evaluation.harness import Harness, HarnessError, find_mosquitto
from tools.evaluation.metric_select import (
    allocation_matches, reference_allocation, run_metric1, run_metric4, run_metric9, summarize_metric1,
    summarize_metric4)
from tools.evaluation.report import write_report
from tools.evaluation.server_config import EvalConfigError, load_eval_config
from tools.node_sim.store import CommandRecord

SERVER = Path(__file__).resolve().parents[2]
EXAMPLE = SERVER / "config" / "eval" / "server-metrics.example.toml"
SIMULATED = "시뮬레이터 측정 (실측 아님)"
VISITORS = ("visitor-1", "visitor-2", "visitor-3")
FRONTS = ("locker-1", "locker-2", "locker-3")


@pytest.fixture
def mosquitto():
    try:
        return find_mosquitto()
    except HarnessError as error:
        pytest.skip(f"Real Mosquitto and mosquitto_passwd are required ({error})")


@pytest.fixture
def acceptance():
    return load_eval_config(EXAMPLE).acceptance


def with_counts(**counts):
    """예시 설정에서 횟수만 바꾼다."""
    config = load_eval_config(EXAMPLE)
    return dataclasses.replace(config, counts={**config.counts, **counts})


def request(request_id, **candidates):
    return AllocationRequest(request_id, candidates)


def shot(front="locker-1", actuated=("locker-1",), verdict="OK", reason="OK"):
    """집계가 읽는 키를 가진 발화(요청) 한 건."""
    return {"front": front, "actuated": list(actuated), "verdict": verdict, "reason": reason}


def asked(front="locker-1", reason="LOW_CONFIDENCE"):
    """재질문: 기기를 구동하지 않았다."""
    return shot(front, actuated=(), verdict="ASK", reason=reason)


class FakeHarness:
    """`Harness`의 MCP 호출, DB 질의, 시뮬레이터 명령 기록을 흉내 낸다.

    `decide(user_id, request_id)`가 `request_use`의 (verdict, reason, 구동한 기기 또는 None)을 정한다. 구동한 기기가 있으면 시뮬레이터가
    그 명령을 `succeeded`로 기록한 것처럼 보인다. `states`는 요청 ID마다 시뮬레이터가 명령을 읽을 때 돌려줄 (state, error_code)의
    차례다 (마지막 값을 반복한다). `find_available_devices`는 `listed(user_id)`의 기기를 돌려준다.
    `meet`가 있으면 `request_use`는 그 수만큼 모여야 지나간다. 요청이 동시에 오지 않으면 장벽이 깨져서 호출이 실패한다.
    """

    def __init__(self, config, decide, listed, *, meet=0, states=None):
        self.config = config
        self.calls = []  # (user_id, tool, arguments)
        self.heard = {}  # node_id → {user_id: rssi}
        self.commands = []  # (request_id, command_id, node_id)
        self.nodes = {node.node_id: SimpleNamespace(store=self) for node in config.nodes}  # 명령 기록은 이 객체가 돌려준다
        self._decide, self._listed = decide, listed
        self.reads = {}  # command_id → 시뮬레이터가 그 명령을 읽은 횟수
        self._states = states or {}
        self._lock = threading.Lock()
        self._meeting = threading.Barrier(meet, timeout=10) if meet else None

    def set_rssi(self, node_id, heard):
        self.heard[node_id] = dict(heard)

    def mcp(self, user_id, tool, arguments):
        with self._lock:
            self.calls.append((user_id, tool, dict(arguments)))
        if tool == "find_available_devices":
            return 200, {"devices": [{"node_id": node_id} for node_id in self._listed(user_id)]}, 0.001
        if tool == "release_use":
            return 200, {"verdict": "OK", "reason": "OK", "use_id": arguments["use_id"]}, 0.001
        if self._meeting is not None:
            self._meeting.wait()
        verdict, reason, node_id = self._decide(user_id, arguments["request_id"])
        result = {"verdict": verdict, "reason": reason, "node_id": node_id}
        if verdict == "OK":
            result["use_id"] = f"use-{arguments['request_id']}"
        if node_id is not None:
            with self._lock:
                self.commands.append((arguments["request_id"], f"command-{len(self.commands)}", node_id))
        return 200, result, 0.01

    def query(self, sql, *params):
        length, prefix = params
        assert "node_command" in sql and length == len(prefix)
        return [{"request_id": request_id, "command_id": command_id, "node_id": node_id}
                for request_id, command_id, node_id in self.commands if request_id.startswith(prefix)]

    def command(self, command_id):  # `CommandStore.command`
        for request_id, known, _ in self.commands:
            if known == command_id:
                states = self._states.get(request_id, [("succeeded", None)])
                self.reads[command_id] = read = self.reads.get(command_id, 0) + 1
                state, error_code = states[min(read, len(states)) - 1]
                return CommandRecord(command_id, "{}", state, "event", "time", error_code)
        return None

    def wait_until(self, predicate, timeout_sec):
        return any(predicate() for _ in range(3))


def request_use_ids(harness):
    return [arguments["request_id"] for _, tool, arguments in harness.calls if tool == "request_use"]


# ── 지표 9: 기준해와 비교 ──────────────────────────────────────────────


def test_FR23_metric9_reference_allocation_known_case():
    # 배정 수가 우선이다: r1은 A뿐이고 r2는 A나 B다. 둘을 배정하면 r2가 낮은 확률의 B를 받지만 한 건만 배정하는 것보다 낫다
    count_first = [request("r1", A=0.99), request("r2", A=0.98, B=0.1)]
    assert reference_allocation(count_first) == (2, pytest.approx(-math.log(0.99) - math.log(0.1)))

    # 배정 수가 같으면 비용 합이 작은 쪽이다: (r1→A, r2→B)가 0.5208, (r1→B, r2→A)가 0.7985다
    cheaper = [request("r1", A=0.99, B=0.5), request("r2", A=0.9, B=0.6)]
    assert reference_allocation(cheaper) == (2, pytest.approx(-math.log(0.99) - math.log(0.6)))

    # 셋이 한 기기를 원하면 한 건만 배정하고, 확률이 가장 높은 요청이 받는다
    crowded = [request("r1", A=0.96), request("r2", A=0.99), request("r3", A=0.97)]
    assert reference_allocation(crowded) == (1, pytest.approx(-math.log(0.99)))

    # 사슬: r2는 A뿐이라 r1이 B로 밀리고 r3은 C를 받는다. 배정 수 3은 이 배치뿐이다
    chain = [request("r1", A=0.99, B=0.96), request("r2", A=0.97), request("r3", B=0.98, C=0.95)]
    assert reference_allocation(chain) == (3, pytest.approx(-math.log(0.96) - math.log(0.97) - math.log(0.95)))

    assert reference_allocation([]) == (0, 0.0)
    assert reference_allocation([request("r1")]) == (0, 0.0)  # 허용쌍이 없다


def test_FR23_metric9_reference_agrees_with_allocate_on_random_requests():
    rng = random.Random(7)
    for _ in range(300):
        nodes = [f"n{number}" for number in range(rng.randint(1, 4))]
        requests = [
            AllocationRequest(f"r{number}", {node: rng.uniform(0.05, 1.0) for node in nodes if rng.random() < 0.6})
            for number in range(rng.randint(0, 4))
        ]

        count, cost = reference_allocation(requests)
        assigned = allocate(requests)

        assert len(assigned) == count
        assert sum(-math.log(next(r for r in requests if r.request_id == rid).candidates[node])
                   for rid, node in assigned.items()) == pytest.approx(cost, abs=COST_TOLERANCE)
        assert allocation_matches(requests, assigned)


def test_FR23_metric9_detects_mismatch(monkeypatch):
    requests = [request("r1", A=0.99, B=0.5), request("r2", A=0.9, B=0.6)]

    assert allocation_matches(requests, {"r1": "A", "r2": "B"})
    assert not allocation_matches(requests, {"r1": "B", "r2": "A"})  # 배정 수는 같지만 비용 합이 크다
    assert not allocation_matches(requests, {"r1": "A"})  # 배정 수가 모자란다
    assert not allocation_matches(requests, {})
    assert not allocation_matches(requests, {"r1": "A", "r2": "A"})  # 중복 배정
    assert not allocation_matches(requests, {"r1": "A", "r2": "C"})  # 허용쌍이 아닌 기기
    assert not allocation_matches(requests, {"r1": "A", "r3": "B"})  # 요청에 없는 ID
    assert not allocation_matches([], {"r1": "A"})  # 요청이 없는데 배정했다
    assert allocation_matches([], {})
    # 중복 배정만 틀린 경우: r2가 B 대신 A를 받아도 배정 수와 비용 합은 기준해와 같다
    shared = [request("r1", A=0.9), request("r2", A=0.9, B=0.9)]
    assert allocation_matches(shared, {"r1": "A", "r2": "B"})
    assert not allocation_matches(shared, {"r1": "A", "r2": "A"})
    # 배정 수만 모자란 경우: 확률 1인 쌍은 비용이 0이라서 빼도 비용 합이 같다
    free = [request("r1", A=0.9), request("r2", B=1.0)]
    assert allocation_matches(free, {"r1": "A", "r2": "B"})
    assert not allocation_matches(free, {"r1": "A"})
    # 비용이 같은 배정은 어느 쪽이든 일치다
    ties = [request("r1", A=0.9, B=0.9), request("r2", A=0.9, B=0.9)]
    assert allocation_matches(ties, {"r1": "A", "r2": "B"}) and allocation_matches(ties, {"r1": "B", "r2": "A"})
    # 비용 차이가 허용 오차 안이면 같고, 밖이면 다르다
    near = [request("r1", A=0.9, B=0.9 * math.exp(-COST_TOLERANCE / 2))]
    assert allocation_matches(near, {"r1": "B"})
    far = [request("r1", A=0.9, B=0.9 * math.exp(-COST_TOLERANCE * 10))]
    assert not allocation_matches(far, {"r1": "B"})

    # 틀린 배정기를 끼우면 불일치 세트를 세고 행이 실패한다 (아무도 배정하지 않는 배정기는 허용쌍이 있는 세트마다 틀린다)
    config = with_counts(m9_sets=50)
    right = run_metric9(config, random.Random(config.seed))
    monkeypatch.setattr(metric_select, "allocate", lambda requests: {})
    wrong = run_metric9(config, random.Random(config.seed))

    (row,) = wrong.rows
    with_pairs = sum(bool(record["requests"]) for record in wrong.raw["sets"])
    assert with_pairs > 0
    assert (row["failures"], row["trials"], row["passed"]) == (with_pairs, 50, False)
    assert row["value"] == pytest.approx((50 - with_pairs) / 50)
    assert [record["match"] for record in wrong.raw["sets"]] == [not record["requests"] for record in wrong.raw["sets"]]
    assert right.rows[0]["failures"] == 0


def test_FR23_metric9_all_sets_match():
    config = load_eval_config(EXAMPLE)

    outcome = run_metric9(config, random.Random(config.seed))

    (row,) = outcome.rows
    assert row["metric"] == "m9_allocation_match"
    assert (row["value"], row["limit"], row["passed"], row["trials"], row["failures"]) == (
        1.0, config.acceptance["m9_min_match_rate"], True, 50, 0)
    sets = outcome.raw["sets"]
    assert len(sets) == 50 and all(record["match"] for record in sets)
    # 일치가 빈 세트의 일치뿐이면 시험이 아니다: 배정이 이뤄지고 같은 기기를 두고 겨루는 세트가 있어야 한다
    assert sum(bool(record["assigned"]) for record in sets) >= 20
    assert sum(metric_select._contested(record["requests"]) for record in sets) >= 5
    assert any(len(record["occupied"]) == 3 for record in sets) and any(not record["occupied"] for record in sets)
    assert "겨루는 세트" in row["note"] and "순열 완전 탐색" in row["note"]
    for record in sets:  # 허용쌍은 점유 중이 아니고 확률이 임계값 이상인 쌍이다
        for person, candidates in record["requests"].items():
            for node, p in candidates.items():
                assert node not in record["occupied"] and p >= 0.95
                assert record["probabilities"][person][node] == p
        assert record["reference"]["count"] == len(record["assigned"])
    json.dumps(outcome.raw)


def test_FR23_metric9_is_deterministic_for_a_seed():
    config = load_eval_config(EXAMPLE)

    first = run_metric9(config, random.Random(5))
    again = run_metric9(config, random.Random(5))
    other = run_metric9(config, random.Random(6))

    assert first.raw == again.raw and first.rows == again.rows
    assert first.raw["sets"] != other.raw["sets"]


def test_FR23_metric9_reads_threshold_and_limits_from_config():
    config = load_eval_config(EXAMPLE)
    assert config.server_settings["threshold_locker"] == 0.95  # 예시는 서버 초기값을 명시한다

    low = dataclasses.replace(config, server_settings={**config.server_settings, "threshold_locker": 0.4})
    outcome = run_metric9(low, random.Random(1))
    assert outcome.raw["threshold"] == 0.4 and "임계값 0.4" in outcome.rows[0]["note"]
    allowed = [p for record in outcome.raw["sets"] for candidates in record["requests"].values() for p in candidates.values()]
    assert allowed and min(allowed) >= 0.4 and min(allowed) < 0.95

    unset = dataclasses.replace(config, server_settings={key: value for key, value in config.server_settings.items()
                                                         if key != "threshold_locker"})
    assert run_metric9(unset, random.Random(1)).raw["threshold"] == Settings().threshold_locker

    # 통과 기준은 설정에서 읽는다. 일치율이 한도 이상이면 통과다
    strict = dataclasses.replace(config, acceptance={**config.acceptance, "m9_min_match_rate": 0.5})
    assert run_metric9(strict, random.Random(1)).rows[0]["limit"] == 0.5
    with pytest.raises(EvalConfigError, match="m9_min_match_rate"):
        run_metric9(dataclasses.replace(config, acceptance={}), random.Random(1))
    with pytest.raises(EvalConfigError, match="m9_sets"):
        run_metric9(dataclasses.replace(config, counts={}), random.Random(1))


# ── 지표 1·4: 집계 ─────────────────────────────────────────────────────


def test_NFR05_metric1_summary(acceptance):
    limit = acceptance["m1_max_wrong_rate"]
    attempts = [shot()] * 7 + [shot(actuated=("locker-2",))] + [asked()] * 2

    (row,) = summarize_metric1(attempts, acceptance)

    # 시험 횟수는 기기를 구동한 발화 수(8)이고, 재질문 둘은 이유를 note에 센다
    assert row["metric"] == "m1_wrong_device_rate"
    assert (row["value"], row["limit"], row["passed"], row["trials"], row["failures"]) == (1 / 8, limit, False, 8, 1)
    assert row["note"] == (f"{SIMULATED}. 발화 10건: 정면 기기 구동 7건, 다른 기기 구동 1건, 구동 없음 2건 "
                           f"(ASK/LOW_CONFIDENCE 2건)")

    # 실패가 없으면 value 0이고 통과다. 한도와 같으면 통과다 (1/40 = 0.025)
    (clean,) = summarize_metric1([shot()] * 40, acceptance)
    assert (clean["value"], clean["passed"], clean["trials"], clean["failures"]) == (0.0, True, 40, 0)
    (edge,) = summarize_metric1([shot()] * 39 + [shot(actuated=("locker-3",))], acceptance)
    assert (edge["value"], edge["passed"]) == (limit, True)
    # 정면 기기와 다른 기기를 함께 구동한 발화도 실패다
    (both,) = summarize_metric1([shot(actuated=("locker-1", "locker-2"))], acceptance)
    assert (both["failures"], both["passed"]) == (1, False)

    # 통과 기준은 설정에서 읽는다
    (loose,) = summarize_metric1(attempts, {**acceptance, "m1_max_wrong_rate": 0.2})
    assert (loose["limit"], loose["passed"]) == (0.2, True)
    with pytest.raises(EvalConfigError, match="m1_max_wrong_rate"):
        summarize_metric1(attempts, {})

    # 기기를 구동한 발화가 없으면 재지 못했다. 값, 통과, 시험 횟수, 실패를 비운다 (write_report가 시험 횟수 0을 받지 못한다)
    (none,) = summarize_metric1([asked(), asked(reason="MOVE_CLOSER"), shot(verdict=None, reason=None, actuated=())], acceptance)
    assert [none[column] for column in ("value", "passed", "trials", "failures")] == [""] * 4
    assert none["limit"] == limit
    assert "재지 못했다" in none["note"] and "응답 없음 1건" in none["note"]
    assert "ASK/LOW_CONFIDENCE 1건" in none["note"] and "ASK/MOVE_CLOSER 1건" in none["note"]
    assert summarize_metric1([], acceptance)[0]["trials"] == ""
    assert SIMULATED in row["note"] and SIMULATED in none["note"]


def test_NFR05_metric4_summary(acceptance):
    max_cross, min_success = acceptance["m4_max_cross_user_malfunction"], acceptance["m4_min_success_rate"]

    def round_of(front, actuated=None, **kwargs):
        return shot(front, actuated=(front,) if actuated is None else actuated, **kwargs)

    clean = [round_of(front) for front in FRONTS] * 50
    cross, success = summarize_metric4(clean, acceptance)
    assert (cross["metric"], success["metric"]) == ("m4_cross_user_malfunction", "m4_success_rate")
    assert (cross["value"], cross["limit"], cross["passed"], cross["trials"], cross["failures"]) == (
        0, max_cross, True, 150, 0)
    assert (success["value"], success["limit"], success["passed"], success["trials"], success["failures"]) == (
        1.0, min_success, True, 150, 0)

    # 방문자 1의 요청이 방문자 2의 정면 기기를 구동했다: 교차 오동작 1건이고 그 요청은 성공이 아니다
    leaked = [round_of("locker-1", actuated=("locker-2",))] + [round_of(front) for front in FRONTS] * 9
    cross, success = summarize_metric4(leaked, acceptance)
    assert (cross["value"], cross["passed"], cross["trials"], cross["failures"]) == (1, False, 28, 1)
    assert (success["value"], success["passed"], success["trials"], success["failures"]) == (27 / 28, True, 28, 1)
    assert "다른 방문자의 기기 구동 1건" in cross["note"] and SIMULATED in cross["note"]
    # 정면 기기와 다른 방문자의 기기를 함께 구동해도 교차 오동작이다
    both = [round_of("locker-1", actuated=("locker-1", "locker-3"))] + [round_of(front) for front in FRONTS]
    assert summarize_metric4(both, acceptance)[0]["failures"] == 1
    # 누구의 정면도 아닌 기기(locker-4)를 구동한 요청은 교차 오동작이 아니지만 성공도 아니다
    stray = [round_of("locker-1", actuated=("locker-4",))] + [round_of(front) for front in FRONTS]
    cross, success = summarize_metric4(stray, acceptance)
    assert (cross["value"], cross["failures"], success["failures"]) == (0, 0, 1)
    assert "그 밖의 기기 구동 1건" in cross["note"]

    # 재질문은 성공이 아니다. 142/150 = 0.9467이 한도 0.95에 못 미친다. 교차 오동작 행의 시험 횟수는 구동한 요청 수(142)다
    reasked = clean[:142] + [asked(front, reason="MOVE_CLOSER") for front in FRONTS * 3][:8]
    cross, success = summarize_metric4(reasked, acceptance)
    assert (success["value"], success["passed"], success["trials"], success["failures"]) == (142 / 150, False, 150, 8)
    assert (cross["passed"], cross["trials"], cross["failures"]) == (True, 142, 0)
    assert "구동 없음 8건 (ASK/MOVE_CLOSER 8건)" in success["note"]
    # 성공률이 한도와 같으면 통과다 (19/20 = 0.95)
    _, at_limit = summarize_metric4(clean[:19] + [asked("locker-1")], acceptance)
    assert (at_limit["value"], at_limit["passed"]) == (min_success, True)
    # 정면 기기를 구동했어도 서버가 OK로 답하지 않았으면 성공이 아니다 (명령 처리 실패)
    failed = clean[:3] + [round_of("locker-1", verdict="DENY", reason="EXEC_FAILED")]
    assert summarize_metric4(failed, acceptance)[1]["failures"] == 1

    # 통과 기준은 설정에서 읽는다
    cross, success = summarize_metric4(reasked, {**acceptance, "m4_min_success_rate": 0.9})
    assert (success["limit"], success["passed"]) == (0.9, True)
    cross, _ = summarize_metric4(leaked, {**acceptance, "m4_max_cross_user_malfunction": 1})
    assert (cross["limit"], cross["passed"]) == (1, True)
    with pytest.raises(EvalConfigError, match="m4_min_success_rate"):
        summarize_metric4(clean, {"m4_max_cross_user_malfunction": 0})

    # 기기를 구동한 요청이 없으면 교차 오동작은 재지 못했고, 성공률은 0이다. 요청이 없으면 둘 다 재지 못했다
    silent = [asked(front) for front in FRONTS]
    cross, success = summarize_metric4(silent, acceptance)
    assert [cross[column] for column in ("value", "passed", "trials", "failures")] == [""] * 4
    assert (success["value"], success["passed"], success["trials"], success["failures"]) == (0.0, False, 3, 3)
    empty = summarize_metric4([], acceptance)
    assert [row[column] for row in empty for column in ("value", "passed", "trials", "failures")] == [""] * 8
    assert [row["limit"] for row in empty] == [max_cross, min_success]


def test_NFR05_metric_rows_fit_report(tmp_path, acceptance):
    # 재지 못한 행(시험 횟수와 실패가 빈 칸)도 보고서에 쓸 수 있어야 한다
    rows = [*summarize_metric1([asked()], acceptance), *summarize_metric4([], acceptance),
            *summarize_metric1([shot()] * 3, acceptance)]
    csv_path = tmp_path / "report.csv"

    write_report(rows, db_path="eval.db", schema_version=5, sample_count=0,
                 generated_at=datetime(2026, 10, 27, tzinfo=timezone.utc), csv_path=str(csv_path),
                 md_path=str(tmp_path / "report.md"), git_commit="abc123")

    text = csv_path.read_text(encoding="utf-8")
    assert "m1_wrong_device_rate" in text and "m4_success_rate" in text and "confidence_bound_95" in text


# ── 지표 1·4: 보내는 방식 ──────────────────────────────────────────────


def test_NFR05_metric1_sends_one_request_per_utterance():
    config = with_counts(m1_utterances=4)
    script = {"m1-001": ("ASK", "LOW_CONFIDENCE", None), "m1-003": ("OK", "OK", "locker-2")}
    harness = FakeHarness(config, lambda user_id, rid: script.get(rid, ("OK", "OK", "locker-1")),
                          lambda user_id: ["locker-1"])

    outcome = run_metric1(harness, config)

    # 장면: 첫 방문자가 첫 사물함 앞에 서서 near로 들린다. 나머지 trusted 사물함은 far로 들리고 다른 기기는 아무도 듣지 못한다
    near, far = config.rssi["near"], config.rssi["far"]
    assert harness.heard == {"locker-1": {"visitor-1": near}, "locker-2": {"visitor-1": far},
                             "locker-3": {"visitor-1": far}, "light-1": {}, "light-2": {}, "locker-4": {}}
    # 발화마다 요청 하나, 같은 발화. 재질문(m1-001)도 다시 보내지 않는다. 발화 앞에서 정면 기기가 목록에 나오기를 확인한다
    assert request_use_ids(harness) == ["m1-000", "m1-001", "m1-002", "m1-003"]
    assert {user_id for user_id, _, _ in harness.calls} == {"visitor-1"}
    for _, tool, arguments in harness.calls:
        if tool == "request_use":
            assert (arguments["target_spec"], arguments["action"]) == ("nearest:locker", "open")
    assert [(tool, arguments.get("request_id") or arguments.get("use_id")) for _, tool, arguments in harness.calls] == [
        ("find_available_devices", None), ("request_use", "m1-000"), ("release_use", "use-m1-000"),
        ("find_available_devices", None), ("request_use", "m1-001"),  # ASK는 반납하지 않는다
        ("find_available_devices", None), ("request_use", "m1-002"), ("release_use", "use-m1-002"),
        ("find_available_devices", None), ("request_use", "m1-003"), ("release_use", "use-m1-003"),
    ]
    # 행: 구동한 발화 3건 가운데 locker-2를 구동한 것이 1건
    (row,) = outcome.rows
    assert (row["trials"], row["failures"], row["passed"]) == (3, 1, False)
    assert "ASK/LOW_CONFIDENCE 1건" in row["note"] and SIMULATED in row["note"]
    assert [attempt["actuated"] for attempt in outcome.raw["attempts"]] == [["locker-1"], [], ["locker-1"], ["locker-2"]]
    assert outcome.raw["front"] == "locker-1" and outcome.raw["neighbors"] == ["locker-2", "locker-3"]
    assert [attempt["released"] for attempt in outcome.raw["attempts"]] == ["OK", None, "OK", "OK"]
    json.dumps(outcome.raw)


def test_NFR05_metric4_sends_concurrent_requests_per_round():
    config = with_counts(m4_each=3)
    fronts = dict(zip(VISITORS, FRONTS))
    script = {"m4-001-visitor-2": ("OK", "OK", "locker-3"), "m4-002-visitor-3": ("ASK", "MOVE_CLOSER", None)}
    harness = FakeHarness(config, lambda user_id, rid: script.get(rid, ("OK", "OK", fronts[user_id])),
                          lambda user_id: [fronts[user_id]], meet=3)

    outcome = run_metric4(harness, config)

    # 장면: 사물함마다 자기 앞 방문자는 near, 나머지 두 방문자는 far로 듣는다. 다른 기기는 아무도 듣지 못한다
    near, far = config.rssi["near"], config.rssi["far"]
    for front, owner in zip(FRONTS, VISITORS):
        assert harness.heard[front] == {visitor: near if visitor == owner else far for visitor in VISITORS}
    assert [harness.heard[node] for node in ("light-1", "light-2", "locker-4")] == [{}, {}, {}]
    # 회차마다 세 방문자의 요청이 한 번씩 오고(장벽이 지나갔다), 요청 ID가 회차와 방문자를 가린다
    assert sorted(request_use_ids(harness)) == sorted(f"m4-{index:03d}-{visitor}" for index in range(3) for visitor in VISITORS)
    assert len({user_id for user_id, tool, _ in harness.calls if tool == "request_use"}) == 3
    # 회차 앞에서 세 방문자의 정면 기기가 모두 목록에 나오기를 확인한다
    tools = [(user_id, tool) for user_id, tool, _ in harness.calls]
    assert tools[:3] == [(visitor, "find_available_devices") for visitor in VISITORS]
    assert {tool for _, tool in tools[3:6]} == {"request_use"}
    # 행: 방문자 2의 요청이 방문자 3의 정면 기기를 구동했다(교차 오동작 1건), 방문자 3의 한 요청은 재질문이다
    cross, success = outcome.rows
    assert (cross["value"], cross["passed"], cross["trials"], cross["failures"]) == (1, False, 8, 1)
    assert (success["trials"], success["failures"], success["passed"]) == (9, 2, False)
    assert success["value"] == pytest.approx(7 / 9)
    assert "ASK/MOVE_CLOSER 1건" in success["note"] and SIMULATED in success["note"]
    attempts = {attempt["request_id"]: attempt for attempt in outcome.raw["attempts"]}
    assert attempts["m4-001-visitor-2"]["actuated"] == ["locker-3"] and attempts["m4-001-visitor-2"]["front"] == "locker-2"
    assert attempts["m4-002-visitor-3"]["actuated"] == [] and attempts["m4-002-visitor-3"]["released"] is None
    assert outcome.raw["fronts"] == fronts
    json.dumps(outcome.raw)


def test_NFR05_metric4_sends_the_round_at_the_same_moment(monkeypatch):
    config = with_counts(m4_each=2)
    fronts = dict(zip(VISITORS, FRONTS))
    journal = []  # 장벽에 닿은 일("wait")과 서버가 요청을 받은 일("send")을 일어난 순서대로 적는다

    class RecordingBarrier(threading.Barrier):
        def wait(self, timeout=None):
            journal.append("wait")  # 장벽이 열리려면 모두 닿아야 해서, 이 기록은 어느 요청보다 앞선다
            return super().wait(timeout)

    def decide(user_id, request_id):
        journal.append("send")
        return "OK", "OK", fronts[user_id]

    monkeypatch.setattr(metric_select, "threading", SimpleNamespace(Barrier=RecordingBarrier))
    harness = FakeHarness(config, decide, lambda user_id: [fronts[user_id]])

    run_metric4(harness, config)

    # 회차마다 세 방문자가 모두 같은 장벽에 닿은 뒤에야 요청이 나간다. 먼저 닿은 방문자는 나머지를 기다린다
    assert journal == (["wait"] * 3 + ["send"] * 3) * 2


def test_NFR05_metric_select_counts_only_commands_the_simulator_started():
    config = with_counts(m1_utterances=6)
    sent = {"m1-001": "locker-2", "m1-005": "locker-3"}  # 나머지는 정면 기기 locker-1로 명령을 보낸다
    states = {
        "m1-000": [("rejected", None)],  # 시뮬레이터가 거부했다
        "m1-001": [("succeeded", None)],  # 다른 기기를 구동했다
        "m1-002": [("failed", "BUSY")],  # 구동을 시작하기 전에 실패했다 (error_code가 있다)
        "m1-003": [("accepted", None)],  # 접수만 하고 끝내 시작하지 않았다
        "m1-004": [("accepted", None), ("running", None)],  # 늦게 시작했다: 기다려서 센다
        "m1-005": [("failed", None)],  # 구동하다 실패했다 (error_code가 없다)
    }
    harness = FakeHarness(config, lambda user_id, rid: ("OK", "OK", sent.get(rid, "locker-1")), lambda user_id: ["locker-1"],
                          states=states)
    harness.commands.append(("m4-000-visitor-1", "command-m4", "locker-2"))  # 같은 하네스에서 앞서 돈 다른 지표의 명령

    outcome = run_metric1(harness, config)

    assert [attempt["actuated"] for attempt in outcome.raw["attempts"]] == [[], ["locker-2"], [], [], ["locker-1"], ["locker-3"]]
    (row,) = outcome.rows
    assert (row["trials"], row["failures"], row["value"]) == (3, 2, 2 / 3)
    assert "구동 없음 3건" in row["note"]
    assert "command-m4" not in harness.reads  # 이 지표의 요청이 아닌 명령은 읽지도 기다리지도 않는다


def test_NFR05_metric_select_rejects_config_it_cannot_run():
    config = with_counts(m1_utterances=1, m4_each=1)
    ok = FakeHarness(config, lambda user_id, rid: ("OK", "OK", "locker-1"), lambda user_id: ["locker-1"])
    fewer_visitors = dataclasses.replace(config, users=tuple(user for user in config.users if user.user_id != "visitor-3"))
    fewer_lockers = dataclasses.replace(config, nodes=tuple(node for node in config.nodes if node.node_id != "locker-3"))
    no_visitors = dataclasses.replace(config, users=tuple(user for user in config.users if user.role == "owner"))
    no_lockers = dataclasses.replace(config, nodes=tuple(node for node in config.nodes if node.capability != "locker"))

    for broken, run, why in [
        (fewer_visitors, run_metric4, "방문자.*3명"), (fewer_lockers, run_metric4, "사물함이 3대"),
        (no_visitors, run_metric1, "방문자.*1명"), (no_lockers, run_metric1, "사물함이 1대"),
    ]:
        with pytest.raises(EvalConfigError, match=why):
            run(ok, broken)
    assert not ok.calls  # 장면을 만들기 전에 알린다
    for key, run in [("m1_utterances", run_metric1), ("m4_each", run_metric4)]:
        with pytest.raises(EvalConfigError, match=key):
            run(ok, dataclasses.replace(config, counts={}))
    for key, run in [("m1_max_wrong_rate", run_metric1), ("m4_max_cross_user_malfunction", run_metric4)]:
        with pytest.raises(EvalConfigError, match=key):
            run(ok, dataclasses.replace(config, acceptance={}))
    assert not ok.calls and not ok.heard  # 통과 기준이 없으면 긴 시험을 돌리기 전에 알린다


def test_NFR05_metric_select_fails_when_front_device_never_listed():
    config = dataclasses.replace(with_counts(m1_utterances=2, m4_each=1), startup_timeout_sec=1.0)

    for run in (run_metric1, run_metric4):
        harness = FakeHarness(config, lambda user_id, rid: ("OK", "OK", "locker-1"), lambda user_id: ["locker-2"])
        with pytest.raises(HarnessError, match=r"locker-[12]가 1초 안에 나오지 않았다.*rssi\.near"):
            run(harness, config)
        assert not request_use_ids(harness)  # 정면 기기가 없는 장면에서는 요청을 보내지 않는다


# ── 실제 프로세스 ──────────────────────────────────────────────────────


def test_NFR05_metric1_runs_on_harness(ascii_tmp_path, mosquitto):
    # 서버가 RSSI 프레임을 늦게 넘기는 시점에는 재질문이 잦다. 구동한 발화가 하나는 있도록 넉넉히 보낸다
    config = with_counts(m1_utterances=10)

    with Harness(config, ascii_tmp_path / "work", server_root=SERVER) as harness:
        outcome = run_metric1(harness, config)

    (row,) = outcome.rows
    attempts = outcome.raw["attempts"]
    assert row["metric"] == "m1_wrong_device_rate" and SIMULATED in row["note"]
    assert len(attempts) == 10 and len({attempt["request_id"] for attempt in attempts}) == 10
    # 모델이 시뮬레이터 RSSI로 정면 기기를 고른다: 구동한 기기는 모두 정면 기기이고, 구동한 발화가 있다
    assert all(attempt["actuated"] in ([], [attempt["front"]]) for attempt in attempts)
    served = [attempt for attempt in attempts if attempt["actuated"]]
    assert served and all(attempt["verdict"] == "OK" and attempt["released"] == "OK" for attempt in served)
    assert (row["trials"], row["failures"], row["value"], row["passed"]) == (len(served), 0, 0.0, True)
    json.dumps(outcome.raw)


def test_NFR05_metric4_runs_on_harness(ascii_tmp_path, mosquitto):
    # 동시 요청 중에는 재질문 비율이 시점에 따라 0~60%로 바뀐다 (위 지표 1 시험의 주석). 구동한 요청이 하나는 있도록 넉넉히 보낸다
    config = with_counts(m4_each=4)

    with Harness(config, ascii_tmp_path / "work", server_root=SERVER) as harness:
        outcome = run_metric4(harness, config)

    cross, success = outcome.rows
    attempts = outcome.raw["attempts"]
    assert [row["metric"] for row in outcome.rows] == ["m4_cross_user_malfunction", "m4_success_rate"]
    assert len(attempts) == 12 and {attempt["user_id"] for attempt in attempts} == set(VISITORS)
    assert outcome.raw["fronts"] == dict(zip(VISITORS, FRONTS)) and SIMULATED in success["note"]
    # 구동한 기기는 모두 요청자의 정면 기기다. 다른 방문자의 기기는 구동되지 않는다
    assert all(attempt["actuated"] in ([], [attempt["front"]]) for attempt in attempts)
    assert (cross["value"], cross["failures"], cross["passed"]) == (0, 0, True)
    served = [attempt for attempt in attempts if attempt["actuated"]]
    assert served and cross["trials"] == len(served)
    assert success["trials"] == 12 and success["failures"] == 12 - sum(attempt["verdict"] == "OK" for attempt in served)
    json.dumps(outcome.raw)
