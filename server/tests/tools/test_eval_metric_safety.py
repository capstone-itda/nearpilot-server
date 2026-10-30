"""NFR-07 지표 5(정책·발견·차단)와 NFR-04 지표 6(종료·복구)의 시나리오와 집계 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

집계 함수(`summarize_metric5`, `summarize_metric6`)는 합성 시도로 시험한다. 시나리오는 `Harness`를 흉내 내는 메모리 서버(`FakeHarness`)로
시험한다: 규칙을 지키는 서버에서는 모든 행이 통과하고, 서버에 규칙을 어기는 결함(`bugs`)을 하나씩 넣으면 그 결함을 세는 시도와 행이
실패한다 (프로세스를 띄우지 않는다). 실제 Mosquitto와 `nearpilot serve`와 시뮬레이터를 띄우는 시험은 지표마다 하나(`runs_on_harness`)다.
Mosquitto가 없으면 그 둘을 건너뛴다 (`MOSQUITTO_EXECUTABLE`, `MOSQUITTO_PASSWD_EXECUTABLE`, PATH, 기본 설치 폴더).
"""

from __future__ import annotations

import csv
import dataclasses
import itertools
import json
import re
import sqlite3
import subprocess
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    AccessMode, ApprovalMode, DeviceState, EndReason, Occupancy, ReasonCode, RecoveryStatus, SessionState, Verdict)
from tools.evaluation import metric_safety
from tools.evaluation.harness import Harness, HarnessError, find_mosquitto
from tools.evaluation.metric_safety import (
    M5_SCENARIOS, M6_SCENARIOS, run_metric5, run_metric6, summarize_metric5, summarize_metric6)
from tools.evaluation.report import write_report
from tools.evaluation.server_config import EvalConfigError, load_eval_config

SERVER = Path(__file__).resolve().parents[2]
EXAMPLE = SERVER / "config" / "eval" / "server-metrics.example.toml"
FAULTS = ("no_response", "command_failed", "result_unknown")
ACTION = {"locker": "open", "light": "on", "speaker": "on"}  # 기기 종류마다 요청하는 동작
EPOCH = datetime(2026, 10, 27, tzinfo=timezone.utc)  # 가짜 시계가 0초일 때의 현재 시각


@pytest.fixture
def mosquitto():
    try:
        return find_mosquitto()
    except HarnessError as error:
        pytest.skip(f"Real Mosquitto and mosquitto_passwd are required ({error})")


@pytest.fixture
def acceptance():
    return load_eval_config(EXAMPLE).acceptance


@pytest.fixture(autouse=True)
def virtual_clock(monkeypatch):
    """`FakeHarness`로 도는 시나리오는 현재 시각을 가짜 시계(`FakeHarness.clock`)로 읽는다. 실제 하네스로 도는 시험은 실제 시계를 쓴다."""
    real = metric_safety._Bench.now
    monkeypatch.setattr(metric_safety._Bench, "now", lambda self: EPOCH + timedelta(seconds=self.h.clock)
                        if isinstance(self.h, FakeHarness) else real(self))


def with_counts(**counts):
    """예시 설정에서 횟수만 바꾼다."""
    config = load_eval_config(EXAMPLE)
    return dataclasses.replace(config, counts={**config.counts, **counts})


def with_two_restricted(**counts):
    """`with_counts`에 제한 기기를 하나 더한다. 예시의 `light-2`는 기간 승인이라서 `light-1`을 1회 승인 기기로 둔다."""
    config = with_counts(**counts)
    nodes = tuple(dataclasses.replace(node, mode="restricted", approval_mode="one_time", approval_ttl_sec=600, max_uses=1)
                  if node.node_id == "light-1" else node for node in config.nodes)
    return dataclasses.replace(config, nodes=nodes)


def attempt(scenario, **fields):
    """집계가 읽는 키를 모두 가진 시도. 재지 않은 칸은 0, None, False다."""
    return {"scenario": scenario, "out_of_scope": 0, "untrusted_candidate": 0, "approval_ok": None, "auto_release": 0,
            "foreign_release": 0, "release_during_fault": 0, "unapproved_recovery": 0, "recovered": None,
            "setup_failed": False, **fields}


# ── 집계 ───────────────────────────────────────────────────────────────


def test_NFR07_metric5_summary_counts(acceptance):
    attempts = [
        attempt("unregistered"), attempt("unregistered"),
        attempt("no_beacon"), attempt("no_beacon", out_of_scope=1),
        attempt("unauthorized"), attempt("unauthorized", out_of_scope=1),
        attempt("one_time", approval_ok=True), attempt("one_time", approval_ok=False),
        attempt("time_window", approval_ok=True), attempt("time_window", approval_ok=True, out_of_scope=2),
        attempt("foreign_handle"), attempt("foreign_handle", out_of_scope=1),
        attempt("untrusted"), attempt("untrusted", untrusted_candidate=2, out_of_scope=1),
    ]

    scope, untrusted, approval = summarize_metric5(attempts, acceptance)

    assert [row["metric"] for row in (scope, untrusted, approval)] == [
        "m5_out_of_scope", "m5_untrusted_candidates", "m5_valid_approval_success"]
    # 범위 밖 조작은 모든 시나리오의 합이고, 실패는 그 값이 0보다 큰 시도 수다 (값 6, 시도 5건)
    assert (scope["value"], scope["limit"], scope["passed"], scope["trials"], scope["failures"]) == (
        6, acceptance["m5_max_out_of_scope"], False, 14, 5)
    # 미신뢰 후보는 `untrusted` 시나리오만 판정한다. 다른 시나리오의 값은 섞이지 않는다
    assert (untrusted["value"], untrusted["limit"], untrusted["passed"], untrusted["trials"], untrusted["failures"]) == (
        2, acceptance["m5_max_untrusted_candidates"], False, 2, 1)
    # 유효 승인 성공은 `approval_ok`가 있는 시도(1회, 기간 승인)의 비율이다
    assert (approval["value"], approval["limit"], approval["passed"], approval["trials"], approval["failures"]) == (
        0.75, acceptance["m5_min_valid_approval_success_rate"], False, 4, 1)

    clean = [attempt(name, approval_ok=True) if name in ("one_time", "time_window") else attempt(name)
             for name in M5_SCENARIOS] * 2
    scope, untrusted, approval = summarize_metric5(clean, acceptance)
    assert (scope["value"], scope["passed"], scope["trials"], scope["failures"]) == (0, True, 14, 0)
    assert (untrusted["value"], untrusted["passed"], untrusted["trials"], untrusted["failures"]) == (0, True, 2, 0)
    assert (approval["value"], approval["passed"], approval["trials"], approval["failures"]) == (1.0, True, 4, 0)

    # 통과 기준은 설정에서 읽고, 값이 한도와 같으면 통과다
    limits = {**acceptance, "m5_max_out_of_scope": 6, "m5_max_untrusted_candidates": 2,
              "m5_min_valid_approval_success_rate": 0.75}
    assert [row["passed"] for row in summarize_metric5(attempts, limits)] == [True, True, True]
    with pytest.raises(EvalConfigError, match="m5_max_out_of_scope"):
        summarize_metric5(attempts, {})


def test_NFR07_metric5_setup_failed_not_counted(acceptance):
    attempts = [
        attempt("unauthorized"),
        attempt("unauthorized", setup_failed=True, out_of_scope=5),  # 장면을 만들지 못한 시도는 값이 있어도 세지 않는다
        attempt("one_time", approval_ok=True),
        attempt("one_time", setup_failed=True, approval_ok=False),
        attempt("untrusted", setup_failed=True, untrusted_candidate=3),
    ]

    scope, untrusted, approval = summarize_metric5(attempts, acceptance)

    assert (scope["value"], scope["passed"], scope["trials"], scope["failures"]) == (0, True, 2, 0)
    assert "장면을 만들지 못한 시도 3건은 세지 않았다" in scope["note"]
    assert (approval["value"], approval["passed"], approval["trials"], approval["failures"]) == (1.0, True, 1, 0)
    assert "장면을 만들지 못한 시도 1건은 세지 않았다" in approval["note"]  # 승인 시나리오의 시도만 센다
    # 장면을 만든 시도가 하나도 없으면 통과도 실패도 아니다. 신뢰구간을 붙일 시험 횟수가 없어서 칸을 비운다
    assert (untrusted["value"], untrusted["passed"], untrusted["trials"], untrusted["failures"]) == ("", "", "", "")
    assert untrusted["limit"] == acceptance["m5_max_untrusted_candidates"]
    assert "장면을 만든 시도가 없어서 재지 못했다" in untrusted["note"]

    nothing = summarize_metric5([], acceptance)
    assert [(row["value"], row["passed"], row["trials"], row["failures"]) for row in nothing] == [("", "", "", "")] * 3


def test_NFR04_metric6_summary_counts(acceptance):
    attempts = [
        attempt("explicit_release", released=True),  # 행이 없는 기준 시나리오. 원자료의 다른 키는 집계가 읽지 않는다
        attempt("leave"), attempt("leave", auto_release=1), attempt("rental_overrun"),
        attempt("no_response", recovered=True), attempt("no_response", release_during_fault=1, recovered=True),
        attempt("command_failed", unapproved_recovery=2, recovered=False),
        attempt("result_unknown", unapproved_recovery=1, recovered=True),
        attempt("pending_command_release"), attempt("pending_command_release", release_during_fault=1),
        attempt("foreign_release"), attempt("foreign_release", foreign_release=1),
    ]

    auto, foreign, fault_release, unapproved, recovery = summarize_metric6(attempts, acceptance)

    assert [row["metric"] for row in (auto, foreign, fault_release, unapproved, recovery)] == [
        "m6_auto_release", "m6_foreign_release", "m6_release_during_fault", "m6_unapproved_recovery", "m6_recovery_success"]
    # 자동 반납은 이탈과 대여시간 초과, 타인 반납은 `foreign_release`, 미완료 명령·장애 중 반납은 장애 셋과 `pending_command_release`,
    # 승인 없는 복구는 장애 셋의 시도다
    assert (auto["value"], auto["limit"], auto["passed"], auto["trials"], auto["failures"]) == (
        1, acceptance["m6_max_auto_release"], False, 3, 1)
    assert (foreign["value"], foreign["passed"], foreign["trials"], foreign["failures"]) == (1, False, 2, 1)
    assert (fault_release["value"], fault_release["passed"], fault_release["trials"], fault_release["failures"]) == (
        2, False, 6, 2)
    assert (unapproved["value"], unapproved["passed"], unapproved["trials"], unapproved["failures"]) == (3, False, 4, 2)
    assert (recovery["value"], recovery["limit"], recovery["passed"], recovery["trials"], recovery["failures"]) == (
        0.75, acceptance["m6_min_recovery_success_rate"], False, 4, 1)

    clean = [attempt(name, recovered=True) if name in FAULTS else attempt(name) for name in M6_SCENARIOS] * 2
    rows = summarize_metric6(clean, acceptance)
    assert [(row["value"], row["passed"], row["trials"], row["failures"]) for row in rows] == [
        (0, True, 4, 0), (0, True, 2, 0), (0, True, 8, 0), (0, True, 6, 0), (1.0, True, 6, 0)]
    with pytest.raises(EvalConfigError, match="m6_max_auto_release"):
        summarize_metric6(attempts, {})


def test_NFR04_metric6_recovery_rate_threshold(acceptance):
    def recovery_row(recovered, limit, *, unmeasured=0):
        attempts = [attempt("no_response", recovered=index < recovered) for index in range(20)]
        attempts += [attempt("no_response", recovered=None)] * unmeasured  # 복구를 재지 않은 시도는 시험이 아니다
        return summarize_metric6(attempts, {**acceptance, "m6_min_recovery_success_rate": limit})[4]

    # 한도와 같은 비율은 통과이고, 한도는 설정에서 읽는다
    for recovered, limit, passed in ((19, 0.95, True), (18, 0.95, False), (18, 0.9, True), (20, 1.0, True), (19, 1.0, False)):
        row = recovery_row(recovered, limit)
        assert (row["value"], row["limit"], row["passed"], row["trials"], row["failures"]) == (
            recovered / 20, limit, passed, 20, 20 - recovered)
    assert (recovery_row(19, 0.95, unmeasured=5)["trials"], recovery_row(19, 0.95, unmeasured=5)["passed"]) == (20, True)


def test_NFR04_metric6_setup_failed_not_counted(acceptance):
    attempts = [
        attempt("no_response", recovered=True),
        attempt("command_failed", setup_failed=True, unapproved_recovery=2, recovered=False),
        attempt("foreign_release", setup_failed=True, foreign_release=1),
        attempt("leave", setup_failed=True, auto_release=1),
    ]

    auto, foreign, fault_release, unapproved, recovery = summarize_metric6(attempts, acceptance)

    assert (unapproved["value"], unapproved["passed"], unapproved["trials"], unapproved["failures"]) == (0, True, 1, 0)
    assert (recovery["value"], recovery["passed"], recovery["trials"], recovery["failures"]) == (1.0, True, 1, 0)
    assert "장면을 만들지 못한 시도 1건은 세지 않았다" in recovery["note"]
    for blank in (auto, foreign):  # 장면을 만든 시도가 없다
        assert (blank["value"], blank["passed"], blank["trials"], blank["failures"]) == ("", "", "", "")
        assert "장면을 만든 시도가 없어서 재지 못했다" in blank["note"]
    assert fault_release["trials"] == 1


def test_NFR07_metric_rows_fit_report(tmp_path, acceptance):
    """장면을 만든 시도가 없는 행(빈 칸)과 실패 0건인 행이 보고서를 막지 않는다 (실패 0건이면 신뢰구간 상한을 붙인다)."""
    clean5 = [attempt(name, approval_ok=True) if name in ("one_time", "time_window") else attempt(name) for name in M5_SCENARIOS]
    clean6 = [attempt(name, recovered=True) if name in FAULTS else attempt(name) for name in M6_SCENARIOS]
    rows = (summarize_metric5([], acceptance) + summarize_metric5(clean5, acceptance)
            + summarize_metric6([], acceptance) + summarize_metric6(clean6, acceptance))

    csv_path, md_path = tmp_path / "report.csv", tmp_path / "report.md"
    write_report(rows, db_path="-", schema_version=1, sample_count=0, generated_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
                 csv_path=str(csv_path), md_path=str(md_path), git_commit="test")

    with csv_path.open(encoding="utf-8", newline="") as file:
        written = list(csv.DictReader(line for line in file if not line.startswith("#")))
    assert len(written) == len(rows) == 16
    blank, measured = written[0], written[3]
    assert (blank["metric"], blank["trials"], blank["confidence_bound_95"]) == ("m5_out_of_scope", "", "")
    assert (measured["metric"], measured["trials"]) == ("m5_out_of_scope", "7")
    assert 0 < float(measured["confidence_bound_95"]) < 1
    assert md_path.read_text(encoding="utf-8").count("m6_recovery_success") == 2


# ── 판정 도우미 ────────────────────────────────────────────────────────


def test_NFR04_metric6_judges_foreign_and_fault_releases():
    active, closed = {"state": "ACTIVE", "end_reason": None}, lambda reason: {"state": "CLOSED", "end_reason": reason}
    deny, ok = {"verdict": "DENY"}, {"verdict": "OK"}

    # 타인의 반납은 OK 응답이거나 점유가 더는 이어지지 않으면 받아들여진 것이다
    assert not metric_safety._taken_effect(deny, active)
    assert metric_safety._taken_effect(ok, active)
    assert metric_safety._taken_effect(deny, closed("release"))
    # FAULT 중의 반납은 OK 응답이거나 점유가 `release`로 끝나면 일반 반납이다. 복구가 정산한 종료(`fault`)는 아니다
    assert not metric_safety._normal_release(deny, active)
    assert not metric_safety._normal_release(deny, closed("fault"))
    assert metric_safety._normal_release(deny, closed("release"))
    assert metric_safety._normal_release(ok, active)


def test_NFR07_metric5_counts_actuations_of_the_attempts_requests_only():
    config = with_counts()
    fake = FakeHarness(config)
    bench = metric_safety._Bench(fake, config, "m5")
    commands = [  # (요청 ID, 서버가 보내려고 했는가, 시뮬레이터의 기록 상태)
        ("m5-a-000-x", 1, "succeeded"),  # 이 시도의 요청이 낸 명령을 구동했다
        ("m5-a-000-y", 1, "rejected"),  # 보냈지만 기기가 거부했다
        ("m5-a-000-z", 1, "accepted"),  # 접수만 하고 시작하지 않았다
        ("m5-a-000-w", 0, None),  # 보내려고 하지 않았다
        ("m5-a-001-x", 1, "succeeded"),  # 다른 시도의 요청이다
    ]
    for serial, (request_id, sent, state) in enumerate(commands):
        fake.db.execute("INSERT INTO request VALUES (?, ?)", (f"i{serial}", request_id))
        fake.db.execute("INSERT INTO node_command (command_id, node_id, internal_request_id, send_attempted) "
                        "VALUES (?, 'light-1', ?, ?)", (f"c{serial}", f"i{serial}", sent))
        if state:
            fake.commands[f"c{serial}"] = SimpleNamespace(state=state, error_code=None)
    fake.db.execute("INSERT INTO node_command (command_id, node_id, internal_request_id, send_attempted) "
                    "VALUES ('c-internal', 'light-1', NULL, 1)")  # 서버가 스스로 낸 명령 (요청 없음)
    fake.commands["c-internal"] = SimpleNamespace(state="succeeded", error_code=None)

    assert bench.actuations("m5-a-000-") == 1
    assert bench.actuations("m5-a-001-") == 1
    assert bench.actuations("m5-a-002-") == 0
    # 허가 없이 처리된 요청 수는 OK 응답 수와 구동 수 가운데 큰 쪽이다. 한쪽만 있어도 센다
    assert bench.breaches([{"verdict": "OK"}], "m5-a-002-") == 1
    assert bench.breaches([{"verdict": "DENY"}], "m5-a-001-") == 1
    assert bench.breaches([{"verdict": "DENY"}], "m5-a-002-") == 0


# ── 설정과 시드의 승인 정책 ────────────────────────────────────────────


def test_NFR13_eval_config_reads_node_policy_keys(tmp_path):
    config = load_eval_config(EXAMPLE)
    policies = {node.node_id: (node.approval_mode, node.approval_ttl_sec, node.max_uses) for node in config.nodes}
    assert policies["light-2"] == ("time_window", 5, None)  # 예시의 제한 기기
    assert policies["locker-1"] == ("one_time", None, None)  # 키가 없으면 정책 키를 쓰지 않는다

    text = EXAMPLE.read_text(encoding="utf-8").replace(
        'approval_mode = "time_window"\napproval_ttl_sec = 5\n',
        'approval_mode = "one_time"\napproval_ttl_sec = 600\nmax_uses = 1\n')
    path = tmp_path / "eval.toml"
    path.write_text(text, encoding="utf-8")
    node = next(node for node in load_eval_config(path).nodes if node.node_id == "light-2")
    assert (node.approval_mode, node.approval_ttl_sec, node.max_uses) == ("one_time", 600, 1)


@pytest.mark.parametrize(("old", "new", "key"), [
    pytest.param('approval_mode = "time_window"', 'approval_mode = "forever"', "approval_mode", id="unknown-approval-mode"),
    pytest.param("approval_ttl_sec = 5", "approval_ttl_sec = 0", "approval_ttl_sec", id="zero-ttl"),
    pytest.param("approval_ttl_sec = 5", 'approval_ttl_sec = "5"', "approval_ttl_sec", id="text-ttl"),
    pytest.param("approval_ttl_sec = 5", "approval_ttl_sec = 5\nmax_uses = 0", "max_uses", id="zero-max-uses"),
    pytest.param("approval_ttl_sec = 5", "approval_ttl_sec = 5\nmax_uses = 1.5", "max_uses", id="fractional-max-uses"),
])
def test_NFR13_eval_config_rejects_bad_node_policy(tmp_path, old, new, key):
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "eval.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")

    with pytest.raises(EvalConfigError, match=re.escape(f"nodes[4].{key}")):  # light-2가 다섯째 기기다
        load_eval_config(path)


def test_NFR13_harness_seeds_node_policy_keys(monkeypatch, tmp_path):
    config = with_two_restricted()
    output = "".join(f"계정 {user.user_id}: 등록\n  토큰: token-{user.user_id} (다시 보여 주지 않는다)\n" for user in config.users)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""))

    Harness(config, tmp_path, server_root=SERVER)._seed("owner")

    seed = tomllib.loads((tmp_path / "seed.toml").read_text(encoding="utf-8"))
    policies = {policy["node_id"]: policy for policy in seed["policies"]}
    assert policies["light-2"] == {"node_id": "light-2", "visibility": "public", "mode": "restricted",
                                   "approval_mode": "time_window", "approver": "owner", "approval_ttl_sec": 5}
    assert policies["light-1"] == {"node_id": "light-1", "visibility": "public", "mode": "restricted",
                                   "approval_mode": "one_time", "approver": "owner", "approval_ttl_sec": 600, "max_uses": 1}
    assert policies["locker-1"] == {"node_id": "locker-1", "visibility": "public", "mode": "open",
                                    "approval_mode": "one_time"}  # 지금까지의 동작과 같다


# ── 가짜 서버 ──────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE node (node_id TEXT PRIMARY KEY, device_state TEXT);
CREATE TABLE use_session (use_id TEXT PRIMARY KEY, node_id TEXT, user_id TEXT, state TEXT, end_reason TEXT, closed_at TEXT);
CREATE TABLE request (internal_request_id TEXT PRIMARY KEY, request_id TEXT);
CREATE TABLE node_command (command_id TEXT PRIMARY KEY, node_id TEXT, internal_request_id TEXT, send_attempted INTEGER,
                           status TEXT DEFAULT 'succeeded');
"""
APPROVAL_REASONS = {"none": ReasonCode.APPROVAL_REQUIRED, "expired": ReasonCode.APPROVAL_EXPIRED,
                    "exhausted": ReasonCode.APPROVAL_EXHAUSTED}


def reply(verdict, reason, node_id=None, use_id=None):
    return {"verdict": verdict, "reason": reason, "node_id": node_id, "use_id": use_id}


class FakeHarness:
    """`Harness`의 MCP, REST, DB 질의, 시뮬레이터 기록을 흉내 내는 메모리 서버. 프로세스를 띄우지 않는다.

    DB는 메모리 SQLite라서 `metric_safety`의 질의가 그대로 돈다. 시간은 흐르지 않는다: `wait_until`은 조건을 한 번 보고, 거짓이면
    제한 시간이 모두 지난 것으로 보고 가상 시계(`clock`)만 올린다. 현재 시각도 이 시계다 (`virtual_clock`).
    제한 시간 안에 늦게 답하는 대여 명령은 점유를 `RESERVED`로 두고 명령을 처리 중으로 둔 채, 요청이 가상 시간이 지날 때까지 멈춘다.
    그 요청은 다른 스레드에서 돈다. 멈춰 선 뒤에야 조건을 보도록 `wait_until`이 기다리고(`parked`), 가상 시간이 지나면 요청을 깨운다(`answer`).
    기본은 규칙을 지키는 서버이고, `bugs`에 든 이름의 규칙을 어긴다:
      open_policy       제한 기기를 승인 없이, 승인이 소진되거나 만료된 뒤에도 구동한다 (기기 목록은 승인 필요로 보인다)
      no_token          토큰이 없는 요청을 `visitor-1`의 요청으로 받는다
      bad_token         틀린 토큰의 요청을 `visitor-1`의 요청으로 받는다
      list_untrusted    미신뢰 기기를 기기 목록과 요청 대상에 올린다
      beaconless        어느 기기도 듣지 못한 방문자의 요청도 그 종류의 첫 기기를 구동한다
      foreign_use       다른 방문자의 `release_use`와 `get_use_status`를 받아들인다
      ok_without_actuation  `request_use`가 OK로 답하지만 기기를 구동하지 않는다
      leave_release     방문자가 이탈하면 점유를 닫는다
      overrun_release   대여 시간을 넘기면 점유를 닫는다
      release_in_fault  FAULT 중의 방문자 반납을 일반 반납으로 닫는다
      release_in_pending  명령이 처리 중인 동안의 방문자 반납을 일반 반납으로 닫는다
      visitor_recovery  방문자의 복구 승인으로 복구한다
      visitor_pending   방문자의 복구 승인을 접수한다 (202). 복구하지는 않는다
      early_recovery    준비되기 전의 소유자 복구 승인으로 복구한다
      auto_recovery     시뮬레이터가 정상으로 돌아오면 승인 없이 복구한다
      stuck_recovery    소유자가 승인해도 복구하지 못한다 (409)
      fake_completion   소유자의 승인에 completed로 답하지만 복구하지 않는다
      late_result       복구한 뒤에 늦게 온 결과가 기기를 다시 FAULT로 만든다
      release_noop      `release_use`가 OK로 답하지만 점유를 닫지 않는다
    처음 `asks`번의 `request_use`는 근접 확률이 모자라다며 `ASK`로 거절한다.
    승인은 규칙대로 센다: 1회 승인은 `max_uses`(없으면 1)번, 기간 승인은 `approval_ttl_sec` 동안 쓴다.
    """

    def __init__(self, config, bugs=(), asks=0):
        self.config, self.bugs, self.asks = config, set(bugs), asks
        self.request_ids = []  # `request_use`가 받은 요청 ID
        self.heard_at_request = {}  # 요청 ID → 그 요청을 받을 때 기기마다 듣던 사용자
        self.late = []  # (결과가 오는 시계, node_id)
        self.server_root = SERVER
        self.tokens = {user.user_id: f"token-{user.user_id}" for user in config.users}  # 하네스가 시드에서 읽는 값
        self.owner = next(user.user_id for user in config.users if user.role == "owner")
        self.clock = 0.0
        self.heard = {}  # node_id → {user_id: rssi}
        self.sim = {node.node_id: {"final": "succeeded", "drop_final_result": False, "result_delay_sec": None}
                    for node in config.nodes}  # 시뮬레이터의 `Scenario`
        self.approvals = []
        self.due = {}  # use_id → 대여 시간이 끝나는 시계
        self.commands = {}  # command_id → 시뮬레이터의 명령 기록
        self.nodes = {node.node_id: SimpleNamespace(busy=False, store=SimpleNamespace(command=self.commands.get))
                      for node in config.nodes}
        self.db = sqlite3.connect(":memory:", check_same_thread=False)  # 멈춰 선 요청이 다른 스레드에서 돈다
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.armed = False  # 늦게 답하는 대여 명령의 요청이 올 것이다
        self.parked = threading.Event()  # 그 요청이 멈춰 섰다
        self.answer = threading.Event()  # 가상 시간이 지나 늦은 결과가 왔다
        self.db.executemany("INSERT INTO node VALUES (?, ?)", [(node.node_id, DeviceState.AVAILABLE) for node in config.nodes])
        self._serial = itertools.count(1)

    # `Harness`가 하는 일

    def set_rssi(self, node_id, heard):
        self.heard[node_id] = dict(heard)
        if "leave_release" in self.bugs:
            for use_id, user_id in self._open_uses(node_id):
                if user_id not in heard:
                    self._close(use_id, EndReason.LEAVE)

    def set_scenario(self, node_id, **changes):
        with self.lock:
            self.sim[node_id].update(changes)
            self.armed = any(self._slow(node.node_id) for node in self.config.nodes)
            if "auto_recovery" in self.bugs and self._state(node_id) == DeviceState.FAULT and not self._faulty(node_id):
                self._recover(node_id)

    def wait_until(self, predicate, timeout_sec):
        if self.armed:  # 늦게 답하는 명령의 요청이 멈춰 설 때까지 (실제 시간으로 최대 10초) 기다린 뒤 조건을 본다
            self.parked.wait(10)
        if predicate():
            return True
        with self.lock:
            self.clock += timeout_sec
            for item in [item for item in self.late if item[0] <= self.clock]:
                self.late.remove(item)
                self._set_state(item[1], DeviceState.FAULT)
            if "overrun_release" in self.bugs:
                for use_id, due in list(self.due.items()):
                    if due <= self.clock and self._session(use_id)["closed_at"] is None:
                        self._close(use_id, EndReason.TIMEOUT)
        if self.armed:
            self.answer.set()  # 가상 시간이 지났으니 늦은 결과가 온다
        return False

    def query(self, sql, *params):
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def mcp(self, user_id, tool, arguments):
        user = self._user(user_id)
        if user is None:
            return 401, None, 0.0
        with self.lock:
            result = getattr(self, f"_{tool}")(user, arguments)
        if callable(result):  # 늦은 결과를 기다리는 요청은 잠금 밖에서 멈춘다
            result = result()
        return 200, result, 0.001

    def rest(self, user_id, method, path, body=None):
        user = self._user(user_id)
        if user is None:
            return 401, {"error": "INVALID_TOKEN"}
        if (method, path) == ("GET", "/approvals"):
            return 200, [{key: item[key] for key in ("approval_id", "node_id", "action", "created_at")}
                         for item in self.approvals if item["status"] == "pending"]  # 요청자는 보이지 않는다
        decision = re.fullmatch(r"/approvals/([^/]+)/decision", path)
        if method == "POST" and decision:
            return self._decide(decision.group(1), body)
        recovery = re.fullmatch(r"/admin/nodes/([^/]+)/recovery(?:/([^/]+)/approve)?", path)
        if recovery:
            node_id, recovery_id = recovery.groups()
            return self._approve_recovery(user, node_id) if recovery_id else self._readiness(user, node_id)
        raise AssertionError(f"흉내 내지 않는 요청: {method} {path}")

    # MCP 도구

    def _find_available_devices(self, user, arguments):
        devices = []
        for node in self.config.nodes:
            if (user in self.heard.get(node.node_id, {}) and self._listed(node)
                    and self._state(node.node_id) == DeviceState.AVAILABLE):
                needs = (node.mode == AccessMode.RESTRICTED
                         and self._approval(user, node.node_id, ACTION[node.capability]) != "valid")
                devices.append({"node_id": node.node_id, "access": "approval_required" if needs else "operable"})
        return {"devices": devices}

    def proximity_ready(self, user_id, node_id):
        return True

    def _request_access(self, user, arguments):
        self.approvals.append({"approval_id": f"ap-{next(self._serial)}", "user_id": user, "node_id": arguments["node_id"],
                               "action": arguments["action"], "status": "pending", "created_at": next(self._serial)})
        return reply(Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING, arguments["node_id"])

    def _request_use(self, user, arguments):
        self.request_ids.append(arguments["request_id"])
        self.heard_at_request[arguments["request_id"]] = {node_id: dict(users) for node_id, users in self.heard.items()}
        capability = arguments["target_spec"].split(":", 1)[1]
        heard = [node for node in self.config.nodes if node.capability == capability
                 and user in self.heard.get(node.node_id, {}) and self._listed(node)]
        if not heard and "beaconless" in self.bugs:
            heard = [node for node in self.config.nodes if node.capability == capability and self._listed(node)][:1]
        if not heard:
            return reply(Verdict.DENY, ReasonCode.BEACON_NOT_SEEN)
        node, action = heard[0], arguments["action"]
        if self.asks:
            self.asks -= 1
            return reply(Verdict.ASK, ReasonCode.LOW_CONFIDENCE, node.node_id)
        state = self._state(node.node_id)
        if node.occupancy == Occupancy.RENTAL and state != DeviceState.AVAILABLE:
            if state == DeviceState.FAULT:
                return reply(Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, node.node_id)
            return reply(Verdict.BUSY, ReasonCode.DEVICE_BUSY, node.node_id)
        if node.mode == AccessMode.RESTRICTED and "open_policy" not in self.bugs:
            approval = self._approval(user, node.node_id, action)
            if approval != "valid":
                return reply(Verdict.NEED_APPROVAL, APPROVAL_REASONS[approval], node.node_id)
            latest = self._approved(user, node.node_id, action)[-1]
            if latest["uses_left"] is not None:
                latest["uses_left"] -= 1
        return self._execute(user, node, arguments["request_id"])

    def _execute(self, user, node, request_id):
        """기기를 구동한다. 시뮬레이터에 장애가 걸려 있으면 결과가 오지 않거나 실패해서 거절하고 기기는 FAULT가 된다.

        대여 명령에 제한 시간 안의 늦은 결과가 걸려 있으면 점유는 `RESERVED`, 명령은 처리 중으로 두고, 늦은 결과를 기다리는 함수를 돌려준다.
        """
        broken, failed = self._faulty(node.node_id), self.sim[node.node_id]["final"] == "failed"
        delay = self.sim[node.node_id]["result_delay_sec"]
        if "late_result" in self.bugs and delay is not None and not self._slow(node.node_id):
            self.late.append((self.clock + delay, node.node_id))
        use_id = None
        if node.occupancy == Occupancy.RENTAL and self._slow(node.node_id):
            return self._start_pending(user, node, request_id)
        if node.occupancy == Occupancy.RENTAL:
            use_id = f"use-{next(self._serial)}"
            self.db.execute("INSERT INTO use_session VALUES (?, ?, ?, ?, NULL, NULL)",
                            (use_id, node.node_id, user, SessionState.RESERVED if broken else SessionState.ACTIVE))
            self.due[use_id] = self.clock + self.config.server_settings["rental_duration_sec"]
            self._set_state(node.node_id, DeviceState.FAULT if broken else DeviceState.ACTIVE)
        serial = next(self._serial)
        self.db.execute("INSERT INTO request VALUES (?, ?)", (f"internal-{serial}", request_id))
        self.db.execute("INSERT INTO node_command (command_id, node_id, internal_request_id, send_attempted) VALUES (?, ?, ?, 1)",
                        (f"command-{serial}", node.node_id, f"internal-{serial}"))
        self.commands[f"command-{serial}"] = SimpleNamespace(
            state="failed" if failed else "unknown" if broken
            else "rejected" if "ok_without_actuation" in self.bugs else "succeeded",
            error_code=None)
        if broken:
            return reply(Verdict.DENY, ReasonCode.EXEC_FAILED if failed else ReasonCode.EXEC_UNKNOWN, node.node_id)
        return reply(Verdict.OK, ReasonCode.OK, node.node_id, use_id)

    def _start_pending(self, user, node, request_id):
        """대여 명령이 처리 중이다: 점유와 기기는 `RESERVED`다. 늦은 결과가 오면 점유가 `RESERVED`인 채일 때만 `ACTIVE`가 된다."""
        use_id, serial = f"use-{next(self._serial)}", next(self._serial)
        self.parked.clear()
        self.answer.clear()  # 앞서 지난 가상 시간은 이 요청이 기다릴 시간이 아니다
        self.db.execute("INSERT INTO use_session VALUES (?, ?, ?, ?, NULL, NULL)", (use_id, node.node_id, user, SessionState.RESERVED))
        self.due[use_id] = self.clock + self.config.server_settings["rental_duration_sec"]
        self._set_state(node.node_id, DeviceState.RESERVED)
        self.db.execute("INSERT INTO request VALUES (?, ?)", (f"internal-{serial}", request_id))
        self.db.execute("INSERT INTO node_command (command_id, node_id, internal_request_id, send_attempted, status) "
                        "VALUES (?, ?, ?, 1, 'in_progress')", (f"command-{serial}", node.node_id, f"internal-{serial}"))
        self.commands[f"command-{serial}"] = SimpleNamespace(state="succeeded", error_code=None)

        def answered():
            self.parked.set()
            self.answer.wait(10)  # 가상 시간이 지나기를 기다린다. 실제 시간은 어긋났을 때를 위한 상한이다
            with self.lock:
                self.db.execute("UPDATE node_command SET status = 'succeeded' WHERE command_id = ?", (f"command-{serial}",))
                if self._session(use_id)["state"] == SessionState.RESERVED:  # 닫힌 점유를 늦은 성공이 되살리지 않는다
                    self.db.execute("UPDATE use_session SET state = ? WHERE use_id = ?", (SessionState.ACTIVE, use_id))
                    self._set_state(node.node_id, DeviceState.ACTIVE)
                self.parked.clear()
                self.answer.clear()
            return reply(Verdict.OK, ReasonCode.OK, node.node_id, use_id)

        return answered

    def _release_use(self, user, arguments):
        session = self._session(arguments["use_id"])
        if session is None or (session["user_id"] != user and "foreign_use" not in self.bugs):
            return reply(Verdict.DENY, ReasonCode.USE_NOT_FOUND)
        if self._state(session["node_id"]) == DeviceState.FAULT and "release_in_fault" not in self.bugs:
            return reply(Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, session["node_id"])
        if self._state(session["node_id"]) == DeviceState.RESERVED and "release_in_pending" not in self.bugs:  # 명령이 처리 중이다
            return reply(Verdict.DENY, ReasonCode.TARGET_UNAVAILABLE, session["node_id"])
        if session["closed_at"] is None and "release_noop" not in self.bugs:
            self._close(session["use_id"], EndReason.RELEASE)
        return reply(Verdict.OK, ReasonCode.OK, session["node_id"], session["use_id"])

    def _get_use_status(self, user, arguments):
        session = self._session(arguments["use_id"])
        if session is None or (session["user_id"] != user and "foreign_use" not in self.bugs):  # 조회는 판정 없이 상태만 돌려준다
            return {"use_id": arguments["use_id"], "state": None, "overtime_sec": None, "reason": ReasonCode.USE_NOT_FOUND}
        return {"use_id": session["use_id"], "state": session["state"], "closed_at": session["closed_at"], "reason": None,
                "overtime_sec": max(0.0, self.clock - self.due[session["use_id"]])}

    # REST

    def _decide(self, approval_id, body):
        approval = next(item for item in self.approvals if item["approval_id"] == approval_id)
        node = next(node for node in self.config.nodes if node.node_id == approval["node_id"])
        ttl = node.approval_ttl_sec
        approval.update(status=body["decision"], expires=self.clock + ttl if ttl is not None else float("inf"),
                        uses_left=node.max_uses or (1 if node.approval_mode == ApprovalMode.ONE_TIME else None))
        valid_until = EPOCH + timedelta(seconds=self.clock + ttl) if ttl is not None else None
        return 200, {"approval_id": approval_id, "status": body["decision"],
                     "valid_until": valid_until and valid_until.isoformat()}

    def _readiness(self, user, node_id):
        if user != self.owner:
            return 403, {"error": "NOT_OWNER"}
        if self._state(node_id) != DeviceState.FAULT:
            return 404, {"error": "NO_RECOVERY"}
        status = RecoveryStatus.NOT_READY if self._faulty(node_id) else RecoveryStatus.READY  # 정상으로 돌아와야 준비된다
        return 200, {"node_id": node_id, "recovery_id": f"recovery-{node_id}", "status": status, "blockers": [],
                     "evidence": {}, "checked_at": "2026-10-27T00:00:00+00:00"}

    def _approve_recovery(self, user, node_id):
        if user != self.owner:
            if "visitor_pending" in self.bugs:
                return 202, {"outcome": "pending", "node_id": node_id}
            if "visitor_recovery" not in self.bugs:
                return 403, {"error": "NOT_OWNER"}
        if self._state(node_id) != DeviceState.FAULT:
            return 409, {"error": "STATE_CONFLICT"}
        if user == self.owner and self._faulty(node_id) and "early_recovery" not in self.bugs:
            return 409, {"error": "NOT_READY"}
        if "stuck_recovery" in self.bugs:
            return 409, {"error": "STATE_CONFLICT"}
        if "fake_completion" not in self.bugs:
            self._recover(node_id)
        return 200, {"outcome": "completed", "node_id": node_id}

    # 상태

    def _user(self, user_id):
        """토큰이 맞는 사용자. 틀리거나 없으면 None이다."""
        if user_id is not None and self.tokens.get(user_id) == f"token-{user_id}":
            return user_id
        return "visitor-1" if ("no_token" if user_id is None else "bad_token") in self.bugs else None

    def _listed(self, node):
        return node.trusted or "list_untrusted" in self.bugs

    def _faulty(self, node_id):
        sim = self.sim[node_id]
        return (sim["final"] == "failed" or sim["drop_final_result"]
                or (sim["result_delay_sec"] is not None and not self._slow(node_id)))

    def _slow(self, node_id):
        """결과가 늦지만 제한 시간(`exec_timeout_sec`) 안에 온다. 기기는 FAULT가 되지 않는다."""
        delay = self.sim[node_id]["result_delay_sec"]
        return delay is not None and self.config.actuation_sec + delay < self.config.server_settings["exec_timeout_sec"]

    def _approved(self, user, node_id, action):
        return [item for item in self.approvals if (item["user_id"], item["node_id"], item["action"], item["status"]) == (
            user, node_id, action, "approved")]

    def _approval(self, user, node_id, action):
        """가장 최근 승인의 상태: none | expired | exhausted | valid."""
        approved = self._approved(user, node_id, action)
        if not approved:
            return "none"
        if approved[-1]["expires"] <= self.clock:
            return "expired"
        return "exhausted" if approved[-1]["uses_left"] == 0 else "valid"

    def _state(self, node_id):
        return self.db.execute("SELECT device_state FROM node WHERE node_id = ?", (node_id,)).fetchone()[0]

    def _set_state(self, node_id, state):
        self.db.execute("UPDATE node SET device_state = ? WHERE node_id = ?", (state, node_id))

    def _session(self, use_id):
        return self.db.execute("SELECT * FROM use_session WHERE use_id = ?", (use_id,)).fetchone()

    def _open_uses(self, node_id):
        return self.db.execute("SELECT use_id, user_id FROM use_session WHERE node_id = ? AND closed_at IS NULL",
                               (node_id,)).fetchall()

    def _close(self, use_id, reason):
        node_id = self._session(use_id)["node_id"]
        self.db.execute("UPDATE use_session SET state = ?, end_reason = ?, closed_at = 'closed' WHERE use_id = ?",
                        (SessionState.CLOSED, reason, use_id))
        if self._state(node_id) != DeviceState.FAULT:  # FAULT는 복구해야 풀린다
            self._set_state(node_id, DeviceState.AVAILABLE)

    def _recover(self, node_id):
        for use_id, _ in self._open_uses(node_id):
            self._close(use_id, EndReason.FAULT)
        self._set_state(node_id, DeviceState.AVAILABLE)


# ── 지표 5 시나리오 ────────────────────────────────────────────────────


def test_NFR07_metric5_passes_on_a_correct_server():
    config = with_two_restricted(m5_each=2)

    outcome = run_metric5(FakeHarness(config), config)

    attempts = outcome.raw["attempts"]
    assert [a["scenario"] for a in attempts] == [name for name in M5_SCENARIOS for _ in range(2)]  # 순서대로 각 두 번
    assert not any(a["setup_failed"] for a in attempts), [a.get("reason") for a in attempts]
    assert outcome.raw["skipped"] == {}
    scope, untrusted, approval = outcome.rows
    assert (scope["value"], scope["passed"], scope["trials"], scope["failures"]) == (0, True, 14, 0)
    assert (untrusted["value"], untrusted["passed"], untrusted["trials"], untrusted["failures"]) == (0, True, 2, 0)
    assert (approval["value"], approval["passed"], approval["trials"], approval["failures"]) == (1.0, True, 4, 0)
    assert {a["scenario"]: a["approval_ok"] for a in attempts if a["approval_ok"] is not None} == {
        "one_time": True, "time_window": True}
    json.dumps(outcome.raw)

    # 기다리는 시간은 서버 설정(서버 설정 예시 위에 평가 설정을 덮은 값)에서 구한다
    waits, settings = outcome.raw["waits"], config.server_settings
    example = tomllib.loads((SERVER / "config" / "server.example.toml").read_text(encoding="utf-8"))["settings"]
    assert waits["margin"] == max(settings["status_report_sec"], settings["rssi_frame_sec"])
    assert waits["scene"] == Settings(**{**example, **settings}).beacon_recent_sec + waits["margin"]
    assert waits["leave"] == settings["leave_absent_sec"] + settings["leave_grace_sec"] + waits["margin"]
    assert waits["rental_overrun"] == settings["rental_duration_sec"] + waits["margin"]
    assert waits["fault"] == settings["exec_timeout_sec"] + waits["margin"]

    # 타인 핸들은 방문자를 돌려 가며 그 다음 방문자가 쓴다
    foreign = [a for a in attempts if a["scenario"] == "foreign_handle"]
    assert [a["trace"][0].split()[1] for a in foreign] == ["visitor-2", "visitor-3"]


def test_NFR07_metric5_skips_scenarios_without_a_device():
    config = with_counts(m5_each=1)  # 예시에는 1회 승인 기기가 없다

    outcome = run_metric5(FakeHarness(config), config)

    assert list(outcome.raw["skipped"]) == ["one_time"]
    assert [a["scenario"] for a in outcome.raw["attempts"]] == [name for name in M5_SCENARIOS if name != "one_time"]
    approval = outcome.rows[2]
    assert (approval["value"], approval["passed"], approval["trials"]) == (1.0, True, 1)  # 기간 승인만 잰다
    assert "건너뜀: one_time (approval_mode = one_time인 제한 기기가 설정에 없다)" in approval["note"]

    nothing = dataclasses.replace(config, nodes=())
    outcome = run_metric5(FakeHarness(nothing), nothing)
    assert outcome.raw["attempts"] == [] and list(outcome.raw["skipped"]) == list(M5_SCENARIOS)
    scope, untrusted, approval = outcome.rows
    assert all(row["value"] == "" and row["passed"] == "" for row in outcome.rows)  # 하나도 재지 못했다
    assert all(f"건너뜀: {name}" in scope["note"] for name in ("unregistered", "no_beacon", "unauthorized", "foreign_handle"))
    assert "건너뜀: untrusted" in untrusted["note"]
    assert "건너뜀: one_time" in approval["note"] and "건너뜀: time_window" in approval["note"]

    no_ttl = dataclasses.replace(config, nodes=tuple(  # 기간 승인은 유효시간이 끝나기를 기다리므로 approval_ttl_sec가 있어야 한다
        dataclasses.replace(node, approval_ttl_sec=None) if node.node_id == "light-2" else node for node in config.nodes))
    assert "time_window" in run_metric5(FakeHarness(no_ttl), no_ttl).raw["skipped"]


@pytest.mark.parametrize(("bug", "scenario", "field", "expected"), [
    pytest.param("no_token", "unregistered", "out_of_scope", 1, id="tokenless-request-granted"),
    pytest.param("bad_token", "unregistered", "out_of_scope", 1, id="wrong-token-request-granted"),
    pytest.param("beaconless", "no_beacon", "out_of_scope", 1, id="request-without-beacon-granted"),
    pytest.param("open_policy", "unauthorized", "out_of_scope", 1, id="unapproved-request-granted"),
    pytest.param("open_policy", "one_time", "out_of_scope", 1, id="request-after-one-time-use-granted"),
    pytest.param("open_policy", "time_window", "out_of_scope", 1, id="request-after-expiry-granted"),
    pytest.param("foreign_use", "foreign_handle", "out_of_scope", 2, id="foreign-handle-accepted"),
    pytest.param("list_untrusted", "untrusted", "untrusted_candidate", 2, id="untrusted-device-offered"),
])
def test_NFR07_metric5_counts_each_breach(bug, scenario, field, expected):
    config = with_two_restricted(m5_each=1)

    outcome = run_metric5(FakeHarness(config, [bug]), config)

    broken = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert not broken[scenario]["setup_failed"], broken[scenario]
    assert broken[scenario][field] == expected
    metric = "m5_untrusted_candidates" if field == "untrusted_candidate" else "m5_out_of_scope"
    row = {row["metric"]: row for row in outcome.rows}[metric]
    assert (row["passed"], row["failures"] >= 1) == (False, True)


def test_NFR07_metric5_valid_approval_needs_actuation():
    config = with_two_restricted(m5_each=1)

    outcome = run_metric5(FakeHarness(config, ["ok_without_actuation"]), config)  # OK로 답했지만 기기는 구동하지 않았다

    attempts = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert [attempts[name]["approval_ok"] for name in ("one_time", "time_window")] == [False, False]
    approval = outcome.rows[2]
    assert (approval["value"], approval["passed"], approval["trials"], approval["failures"]) == (0.0, False, 2, 2)


def test_NFR07_metric5_unauthorized_counts_actuation_not_device_list():
    """기기 목록이 제한 기기를 승인 없이 쓸 수 있다고 보여도 무권한 시나리오는 요청을 보내서 구동 여부로 센다."""
    config = with_counts(m5_each=1)

    class OperableEverywhere(FakeHarness):
        def _find_available_devices(self, user, arguments):
            found = super()._find_available_devices(user, arguments)
            return {"devices": [{**device, "access": "operable"} for device in found["devices"]]}

    outcome = run_metric5(OperableEverywhere(config, ["open_policy"]), config)

    unauthorized = next(a for a in outcome.raw["attempts"] if a["scenario"] == "unauthorized")
    assert (unauthorized["setup_failed"], unauthorized["out_of_scope"]) == (False, 1)
    approvals = [a for a in outcome.raw["attempts"] if a["scenario"] == "time_window"]
    assert all(a["setup_failed"] and "승인이 필요하지 않다" in a["reason"] for a in approvals)  # 승인 시나리오는 시작 장면이 아니다


def test_NFR07_metric5_no_beacon_sends_a_valid_token_request_nobody_hears():
    config = with_counts(m5_each=2)
    fake = FakeHarness(config)

    outcome = run_metric5(fake, config)

    attempts = [a for a in outcome.raw["attempts"] if a["scenario"] == "no_beacon"]
    assert not any(a["setup_failed"] for a in attempts), [a.get("reason") for a in attempts]
    # 응답이 401이 아니다: 토큰은 유효하고, 서버가 비콘 근거가 없어서 거절했다
    assert [a["trace"] for a in attempts] == [
        ["request_use visitor-1 200 DENY/BEACON_NOT_SEEN"], ["request_use visitor-2 200 DENY/BEACON_NOT_SEEN"]]
    assert [a["out_of_scope"] for a in attempts] == [0, 0]
    sent = [request_id for request_id in fake.request_ids if request_id.startswith("m5-no_beacon-")]
    assert len(sent) == 2
    for request_id in sent:  # 요청을 받을 때 어느 기기도 방문자를 듣지 못했다
        assert not any(fake.heard_at_request[request_id].values())
    scope = outcome.rows[0]
    assert (scope["passed"], scope["failures"]) == (True, 0)


def test_NFR07_metric5_no_beacon_needs_a_scene_without_beacon_evidence():
    """서버에 앞 장면의 프레임이 남아 기기 목록에 기기가 나오면 비콘 근거가 없는 장면이 아니다. 지표의 실패가 아니라 장면을 만들지 못한 시도다."""
    config = with_counts(m5_each=1)

    class StaleFrames(FakeHarness):
        def _find_available_devices(self, user, arguments):
            return {"devices": [{"node_id": "locker-1", "access": "operable"}]}

    outcome = run_metric5(StaleFrames(config), config)

    stale = next(a for a in outcome.raw["attempts"] if a["scenario"] == "no_beacon")
    assert stale["setup_failed"] and "기기 목록에 ['locker-1']" in stale["reason"]
    assert stale["out_of_scope"] == 0  # 요청을 보내지 않았다
    assert "장면을 만들지 못한 시도 " in outcome.rows[0]["note"]


# ── 지표 6 시나리오 ────────────────────────────────────────────────────


def test_NFR04_metric6_passes_on_a_correct_server():
    config = with_counts(m6_each=2)

    outcome = run_metric6(FakeHarness(config), config)

    attempts = outcome.raw["attempts"]
    assert [a["scenario"] for a in attempts] == [name for name in M6_SCENARIOS for _ in range(2)]
    assert not any(a["setup_failed"] for a in attempts), [a.get("reason") for a in attempts]
    assert [(row["metric"], row["value"], row["passed"], row["trials"], row["failures"]) for row in outcome.rows] == [
        ("m6_auto_release", 0, True, 4, 0), ("m6_foreign_release", 0, True, 2, 0), ("m6_release_during_fault", 0, True, 8, 0),
        ("m6_unapproved_recovery", 0, True, 6, 0), ("m6_recovery_success", 1.0, True, 6, 0)]
    assert all(a["released"] for a in attempts if a["scenario"] == "explicit_release")
    assert all(a["recovered"] for a in attempts if a["scenario"] in FAULTS)
    # 장애 시나리오의 확인 순서: 방문자 승인 403, 준비 전 소유자 승인 409, 정상화한 뒤 소유자 승인 200
    trace = next(a for a in attempts if a["scenario"] == "no_response")["trace"]
    assert trace[-3:] == ["방문자 복구 승인 403", "준비 전 소유자 복구 승인 409", "소유자 복구 승인 200 completed"]
    assert outcome.raw["waits"]["fault"] == 2.5  # exec_timeout_sec 2 + margin 0.5
    json.dumps(outcome.raw)


@pytest.mark.parametrize(("bug", "scenarios", "field", "expected", "metric"), [
    pytest.param("leave_release", ("leave",), "auto_release", 1, "m6_auto_release", id="closed-after-leave"),
    pytest.param("overrun_release", ("rental_overrun",), "auto_release", 1, "m6_auto_release", id="closed-after-overrun"),
    pytest.param("foreign_use", ("foreign_release",), "foreign_release", 1, "m6_foreign_release", id="foreign-release-accepted"),
    pytest.param("release_in_fault", FAULTS, "release_during_fault", 1, "m6_release_during_fault",
                 id="release-in-fault-is-normal"),
    pytest.param("visitor_recovery", FAULTS, "unapproved_recovery", 1, "m6_unapproved_recovery", id="visitor-approves-recovery"),
    pytest.param("visitor_pending", FAULTS, "unapproved_recovery", 1, "m6_unapproved_recovery",
                 id="visitor-approval-accepted-as-pending"),
    pytest.param("early_recovery", FAULTS, "unapproved_recovery", 1, "m6_unapproved_recovery", id="owner-approves-before-ready"),
    pytest.param("auto_recovery", FAULTS, "unapproved_recovery", 1, "m6_unapproved_recovery", id="fault-clears-without-approval"),
])
def test_NFR04_metric6_counts_each_breach(bug, scenarios, field, expected, metric):
    config = with_counts(m6_each=1)

    outcome = run_metric6(FakeHarness(config, [bug]), config)

    broken = {a["scenario"]: a for a in outcome.raw["attempts"]}
    for scenario in scenarios:
        assert not broken[scenario]["setup_failed"], broken[scenario]
        assert broken[scenario][field] == expected, broken[scenario]
    row = {row["metric"]: row for row in outcome.rows}[metric]
    assert (row["value"], row["passed"], row["failures"]) == (len(scenarios) * expected, False, len(scenarios))
    if field == "unapproved_recovery":  # 승인 없이 복구된 시도는 소유자 승인으로 복구한 것이 아니다
        assert not any(broken[scenario]["recovered"] for scenario in scenarios)


@pytest.mark.parametrize("bug", [
    pytest.param("stuck_recovery", id="owner-approval-rejected"),
    pytest.param("fake_completion", id="completed-without-recovering"),
])
def test_NFR04_metric6_failed_recovery_fails_the_rate_and_blocks_later_attempts(bug):
    config = with_counts(m6_each=1)

    outcome = run_metric6(FakeHarness(config, [bug]), config)

    broken = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert (broken["no_response"]["setup_failed"], broken["no_response"]["recovered"]) == (False, False)
    assert broken["no_response"]["unapproved_recovery"] == 0
    # 복구하지 못해 FAULT로 남은 기기에서는 뒤 시도의 장면을 만들 수 없다. 그 시도는 지표의 실패가 아니라 `setup_failed`다
    for scenario in ("command_failed", "result_unknown", "pending_command_release", "foreign_release"):
        assert broken[scenario]["setup_failed"] and "AVAILABLE이 되지 않았다" in broken[scenario]["reason"]
    recovery = outcome.rows[4]
    assert (recovery["value"], recovery["passed"], recovery["trials"], recovery["failures"]) == (0.0, False, 1, 1)
    assert "장면을 만들지 못한 시도 2건은 세지 않았다" in recovery["note"]


def test_FR07_metric6_late_result_after_recovery_fails_recovery():
    config = with_counts(m6_each=1)

    outcome = run_metric6(FakeHarness(config, ["late_result"]), config)

    broken = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert [broken[name]["recovered"] for name in FAULTS] == [True, True, False]  # 늦은 결과는 결과 불명 시나리오에만 온다
    assert "늦은 결과가 오자" in broken["result_unknown"]["trace"][-1]
    recovery = outcome.rows[4]
    assert (recovery["value"], recovery["passed"], recovery["trials"], recovery["failures"]) == (2 / 3, False, 3, 1)


def test_NFR04_metric6_explicit_release_reports_open_session():
    config = with_counts(m6_each=1)

    outcome = run_metric6(FakeHarness(config, ["release_noop"]), config)

    first = outcome.raw["attempts"][0]  # 반납이 OK로 답했지만 점유가 닫히지 않았다
    assert (first["scenario"], first["released"], first["setup_failed"]) == ("explicit_release", False, False)


def test_FR20_metric6_pending_release_counts_into_the_release_during_fault_row(acceptance):
    attempts = [
        attempt("no_response", recovered=True),
        attempt("pending_command_release"),
        attempt("pending_command_release", release_during_fault=1),
        attempt("pending_command_release", setup_failed=True, release_during_fault=1),  # 장면을 만들지 못한 시도는 세지 않는다
    ]

    rows = {row["metric"]: row for row in summarize_metric6(attempts, acceptance)}

    release = rows["m6_release_during_fault"]
    assert (release["value"], release["passed"], release["trials"], release["failures"]) == (1, False, 3, 1)
    assert "처리 중인 명령" in release["note"] and "장면을 만들지 못한 시도 1건은 세지 않았다" in release["note"]
    for metric in ("m6_unapproved_recovery", "m6_recovery_success"):  # 승인 없는 복구와 복구 성공은 장애 셋만 센다
        assert rows[metric]["trials"] == 1


def test_FR20_metric6_server_holds_release_while_the_command_is_pending():
    config = with_counts(m6_each=2)
    fake = FakeHarness(config)

    outcome = run_metric6(fake, config)

    pending = [a for a in outcome.raw["attempts"] if a["scenario"] == "pending_command_release"]
    assert len(pending) == 2 and not any(a["setup_failed"] for a in pending), [a.get("reason") for a in pending]
    # 늦게 답한 대여는 성공으로 끝나지만, 명령이 처리 중이던 때의 반납은 받아들여지지 않았다
    assert pending[0]["trace"] == [
        "request_use visitor-1 200 OK/OK", "release_use visitor-1 200 DENY/TARGET_UNAVAILABLE", "반납 뒤 세션 RESERVED"]
    assert pending[1]["trace"][1] == "release_use visitor-2 200 DENY/TARGET_UNAVAILABLE"
    assert [a["release_during_fault"] for a in pending] == [0, 0]
    release = {row["metric"]: row for row in outcome.rows}["m6_release_during_fault"]
    assert (release["passed"], release["trials"], release["failures"]) == (True, 8, 0)  # 장애 셋과 처리 중인 명령
    assert fake.query("SELECT count(*) FROM node_command WHERE status = 'in_progress'")[0][0] == 0  # 늦은 명령도 끝났다
    json.dumps(outcome.raw)


def test_FR20_metric6_counts_release_that_closes_while_the_command_is_pending():
    config = with_counts(m6_each=2)

    outcome = run_metric6(FakeHarness(config, ["release_in_pending"]), config)

    attempts = outcome.raw["attempts"]
    assert not any(a["setup_failed"] for a in attempts), [a.get("reason") for a in attempts]  # 늦은 성공이 닫힌 점유를 되살리지 않는다
    pending = [a for a in attempts if a["scenario"] == "pending_command_release"]
    assert [a["release_during_fault"] for a in pending] == [1, 1]
    assert pending[0]["trace"][1:] == ["release_use visitor-1 200 OK/OK", "반납 뒤 세션 CLOSED"]
    release = {row["metric"]: row for row in outcome.rows}["m6_release_during_fault"]
    assert (release["value"], release["passed"], release["trials"], release["failures"]) == (2, False, 8, 2)


def test_FR20_metric6_pending_release_without_a_pending_command_is_not_counted():
    config = with_counts(m6_each=1)

    class IgnoresDelay(FakeHarness):
        def set_scenario(self, node_id, **changes):
            delay = changes.get("result_delay_sec")
            if delay is not None and delay < self.config.server_settings["exec_timeout_sec"]:  # 늦게 답하라는 설정을 따르지 않는다
                changes = {**changes, "result_delay_sec": None}
            super().set_scenario(node_id, **changes)

    outcome = run_metric6(IgnoresDelay(config), config)

    attempts = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert attempts["pending_command_release"]["setup_failed"] and "처리 중" in attempts["pending_command_release"]["reason"]
    assert not attempts["foreign_release"]["setup_failed"]  # 정리한 뒤라서 다음 시도의 장면이 만들어진다
    release = {row["metric"]: row for row in outcome.rows}["m6_release_during_fault"]
    assert (release["passed"], release["trials"], release["failures"]) == (True, 3, 0)
    assert "장면을 만들지 못한 시도 1건은 세지 않았다" in release["note"]


def test_FR20_metric6_release_after_the_command_finished_is_not_a_failure():
    """서버가 반납을 처리하기 전에 명령이 끝났다면 그때의 일반 반납은 정당하다. 반납 시점의 상태를 몰라서 그 시도는 세지 않는다."""
    config = with_counts(m6_each=1)

    class FinishesFirst(FakeHarness):
        def _release_use(self, user, arguments):
            session = self._session(arguments["use_id"])
            if session is not None and self._state(session["node_id"]) == DeviceState.RESERVED:  # 처리하기 직전에 명령이 끝난다
                self.db.execute("UPDATE node_command SET status = 'succeeded' WHERE status = 'in_progress'")
                self.db.execute("UPDATE use_session SET state = ? WHERE use_id = ?", (SessionState.ACTIVE, session["use_id"]))
                self._set_state(session["node_id"], DeviceState.ACTIVE)
                self.answer.set()
            return super()._release_use(user, arguments)

    outcome = run_metric6(FinishesFirst(config), config)

    attempts = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert attempts["pending_command_release"]["setup_failed"]
    assert "반납 응답 전에 명령이 끝났다 (OK/OK)" in attempts["pending_command_release"]["reason"]
    assert not attempts["foreign_release"]["setup_failed"]
    release = {row["metric"]: row for row in outcome.rows}["m6_release_during_fault"]
    assert (release["passed"], release["trials"], release["failures"]) == (True, 3, 0)


def test_NFR07_metric5_retries_asked_requests_with_new_request_ids():
    config = with_two_restricted(m5_each=1)
    fake = FakeHarness(config, asks=2)  # 처음 두 `request_use`는 근접 확률이 모자라 ASK로 거절된다

    outcome = run_metric5(fake, config)

    attempts = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert not any(a["setup_failed"] for a in attempts.values())
    assert [line.split()[-1] for line in attempts["unauthorized"]["trace"][1:]] == [
        "ASK/LOW_CONFIDENCE", "ASK/LOW_CONFIDENCE", "NEED_APPROVAL/APPROVAL_REQUIRED"]
    assert len(set(fake.request_ids)) == len(fake.request_ids)  # 같은 ID로 다시 보내면 저장된 판정을 돌려받는다
    assert (outcome.rows[0]["value"], outcome.rows[0]["passed"]) == (0, True)

    # 끝까지 ASK이면 정책 검사에 닿지 못한 것이다. 지표의 실패가 아니라 장면을 만들지 못한 시도다
    stuck = FakeHarness(config, asks=10_000)
    unauthorized = next(a for a in run_metric5(stuck, config).raw["attempts"] if a["scenario"] == "unauthorized")
    assert unauthorized["setup_failed"] and "정책 검사에 닿지 못했다" in unauthorized["reason"]


def test_NFR04_metric6_rejects_config_it_cannot_run():
    config = with_counts(m6_each=1)
    no_rental = dataclasses.replace(config, nodes=tuple(node for node in config.nodes if node.occupancy != "rental"))
    one_visitor = dataclasses.replace(
        config, users=tuple(user for user in config.users if user.user_id not in ("visitor-2", "visitor-3")))
    no_count = dataclasses.replace(config, counts={})
    no_limit = dataclasses.replace(config, acceptance={})

    with pytest.raises(EvalConfigError, match="대여형"):
        run_metric6(FakeHarness(no_rental), no_rental)
    for run in (run_metric5, run_metric6):  # 타인 핸들과 타인 반납은 다른 방문자가 필요하다
        with pytest.raises(EvalConfigError, match="방문자"):
            run(FakeHarness(one_visitor), one_visitor)
    for run, count, limit in ((run_metric5, "m5_each", "m5_max_out_of_scope"), (run_metric6, "m6_each", "m6_max_auto_release")):
        with pytest.raises(EvalConfigError, match=f"counts.{count}"):
            run(FakeHarness(no_count), no_count)
        fake = FakeHarness(no_limit)
        with pytest.raises(EvalConfigError, match=limit):
            run(fake, no_limit)
        assert fake.clock == 0.0  # 통과 기준이 없으면 시나리오를 돌리기 전에 알린다


# ── 실제 프로세스 ──────────────────────────────────────────────────────


def test_NFR07_metric5_runs_on_harness(ascii_tmp_path, mosquitto):
    config = with_two_restricted(m5_each=1)  # 1회 승인(light-1)과 기간 승인(light-2)을 모두 돌린다

    with Harness(config, ascii_tmp_path / "work", server_root=SERVER) as harness:
        outcome = run_metric5(harness, config)

    attempts = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert list(attempts) == list(M5_SCENARIOS)
    assert not any(a["setup_failed"] for a in attempts.values()), [a.get("reason") for a in attempts.values()]
    assert outcome.raw["skipped"] == {}
    scope, untrusted, approval = outcome.rows
    assert (scope["value"], scope["passed"], scope["trials"], scope["failures"]) == (0, True, 7, 0)
    assert (untrusted["value"], untrusted["passed"], untrusted["trials"], untrusted["failures"]) == (0, True, 1, 0)
    assert approval["trials"] == 2
    # 서버가 사유로 거절했는지는 응답에서 읽는다: 비콘 근거 없음, 무권한, 유효시간이 끝난 요청, 타인 핸들
    assert attempts["no_beacon"]["trace"] == ["request_use visitor-1 200 DENY/BEACON_NOT_SEEN"]
    assert "request_use visitor-1 200 NEED_APPROVAL/APPROVAL_REQUIRED" in attempts["unauthorized"]["trace"]
    assert attempts["time_window"]["approval_ok"] is True
    assert attempts["time_window"]["trace"][-1] == "request_use visitor-1 200 NEED_APPROVAL/APPROVAL_EXPIRED"
    assert "release_use visitor-2 200 DENY/USE_NOT_FOUND" in attempts["foreign_handle"]["trace"]
    # 1회 승인의 성공 여부는 고정하지 않는다: 서버가 `max_uses = 1`인 승인을 첫 사용에서 소진으로 거절한다
    # (실행 직전 재검사가 자기 예약을 사용분으로 센다). 서버를 고치면 `is True`로 고정한다
    assert isinstance(attempts["one_time"]["approval_ok"], bool)
    assert approval["failures"] == (0 if attempts["one_time"]["approval_ok"] else 1)
    json.dumps(outcome.raw)


def test_NFR04_metric6_runs_on_harness(ascii_tmp_path, mosquitto):
    config = with_counts(m6_each=1)

    with Harness(config, ascii_tmp_path / "work", server_root=SERVER) as harness:
        outcome = run_metric6(harness, config)

    attempts = {a["scenario"]: a for a in outcome.raw["attempts"]}
    assert list(attempts) == list(M6_SCENARIOS)
    assert not any(a["setup_failed"] for a in attempts.values()), [a.get("reason") for a in attempts.values()]
    assert [(row["metric"], row["value"], row["passed"], row["trials"], row["failures"]) for row in outcome.rows] == [
        ("m6_auto_release", 0, True, 2, 0), ("m6_foreign_release", 0, True, 1, 0), ("m6_release_during_fault", 0, True, 4, 0),
        ("m6_unapproved_recovery", 0, True, 3, 0), ("m6_recovery_success", 1.0, True, 3, 0)]
    assert attempts["explicit_release"]["released"] is True  # 반납이 되는 경우를 기준으로 본다
    for scenario in FAULTS:
        assert attempts[scenario]["recovered"] is True
        trace = attempts[scenario]["trace"]
        assert "방문자 복구 승인 403" in trace
        assert trace[-1] == "소유자 복구 승인 200 completed"
        # 실제 브로커에서는 준비 조회 전에 idle 보고가 도착할 수도 있다.
        early = [entry for entry in trace if entry.startswith("준비 전 소유자 복구 승인")]
        assert early in ([], ["준비 전 소유자 복구 승인 409"])
        if early:
            assert trace.index("방문자 복구 승인 403") < trace.index(early[0]) < len(trace) - 1
    assert "release_use visitor-2 200 DENY/USE_NOT_FOUND" in attempts["foreign_release"]["trace"]
    # 늦게 답한 대여는 성공으로 끝났고, 명령이 처리 중이던 때의 반납은 받아들여지지 않았다 (점유는 그때 RESERVED였다)
    pending = attempts["pending_command_release"]
    rentals = [entry for entry in pending["trace"] if entry.startswith("request_use ")]
    assert rentals[-1] == "request_use visitor-1 200 OK/OK"
    assert all(entry in ("request_use visitor-1 200 ASK/LOW_CONFIDENCE",
                         "request_use visitor-1 200 ASK/MOVE_CLOSER",
                         "request_use visitor-1 200 BUSY/DEVICE_BUSY",
                         "request_use visitor-1 200 BUSY/COMMAND_IN_PROGRESS") for entry in rentals[:-1])
    releases = [entry for entry in pending["trace"] if entry.startswith("release_use ")]
    assert len(releases) == 1 and releases[0].startswith("release_use visitor-1 200 ")
    assert not releases[0].endswith(" OK/OK")
    assert pending["trace"].index(rentals[-1]) < pending["trace"].index(releases[0])
    assert pending["release_during_fault"] == 0
    json.dumps(outcome.raw)
