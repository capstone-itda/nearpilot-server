"""수집 도구 CLI (FR-16). 가짜 저장소와 가짜 시계, 대기, 입력, 출력으로 확인한다. 실제 시간과 표준 입력은 쓰지 않는다."""

from __future__ import annotations

import itertools
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.data_collection.cli as cli
from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import AdminWriteResult
from tools.data_collection import (
    EXIT_FAILED,
    EXIT_INVALID,
    EXIT_OK,
    PlanError,
    SessionIO,
    default_io,
    load_plan,
    main,
    new_collection_id,
)

CLOSE_ARGV = ["close", "--collection-id", "col-x"]
CLOSED_TEXT = "닫았다"  # 닫기에 성공했을 때만 화면에 나오는 말
STATE_CONFLICT = AdminWriteResult(AdminWriteOutcome.CONFLICT, AdminWriteReason.STATE_CONFLICT)
COLLECTION_NOT_FOUND = AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.COLLECTION_NOT_FOUND)
INVALID_INTERVAL = AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.INVALID_INTERVAL)
BEACON_NOT_FOUND = AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.BEACON_NOT_FOUND)


def _after(base: datetime, seconds: float) -> datetime:
    return base + timedelta(seconds=seconds)


def _session_io(fake_io) -> SessionIO:
    """`FakeIO`의 메서드로 `SessionIO`를 만든다. 회차 ID는 `col-1`, `col-2`, …를 차례로 낸다."""
    ids = (f"col-{n}" for n in itertools.count(1))
    return SessionIO(
        clock=fake_io.clock,
        sleep=fake_io.sleep,
        read_line=fake_io.read_line,
        write=fake_io.write,
        new_id=lambda: next(ids),
    )


class _Factory:
    """`repository_factory`. 호출 횟수를 세고, `error`가 있으면 던지고 없으면 `repository`를 돌려준다."""

    def __init__(self, repository, error: BaseException | None = None) -> None:
        self.repository = repository
        self.error = error
        self.count = 0

    def __call__(self):
        self.count += 1
        if self.error is not None:
            raise self.error
        return self.repository


def _main(argv: list[str], factory: _Factory, fake_io) -> int:
    return main(argv, repository_factory=factory, io=_session_io(fake_io))


def _run_argv(plan_path: Path, record_path: Path, *options: str) -> list[str]:
    return ["run", str(plan_path), "--record", str(record_path), *options]


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def record_path(tmp_path: Path) -> Path:
    return tmp_path / "records.jsonl"


@pytest.mark.parametrize(
    "make_plan",
    [
        pytest.param(lambda data, write, tmp_path: tmp_path / "no-such-plan.json", id="missing_file"),
        pytest.param(lambda data, write, tmp_path: write("{not json"), id="not_json"),
        pytest.param(
            lambda data, write, tmp_path: write({key: value for key, value in data.items() if key != "run_sec"}),
            id="missing_run_sec",
        ),
        pytest.param(lambda data, write, tmp_path: write({**data, "runs": []}), id="empty_runs"),
        pytest.param(
            lambda data, write, tmp_path: write({**data, "runs": [{"label": " locker-1", "spot": "앞 위치"}]}),
            id="label_with_leading_space",
        ),
    ],
)
def test_FR16_invalid_plan_rejected_before_any_write(
    make_plan, plan_data, write_plan, tmp_path, fake_repo, make_io, record_path
):
    plan_path = make_plan(plan_data, write_plan, tmp_path)
    with pytest.raises(PlanError) as reason:
        load_plan(plan_path)  # 화면에 나와야 하는 사유
    factory = _Factory(fake_repo)
    io = make_io(["", "", ""])

    code = _main(_run_argv(plan_path, record_path), factory, io)

    assert code == EXIT_INVALID
    assert factory.count == 0  # 계획 파일을 모두 검사하기 전에는 저장소를 만들지 않는다
    assert fake_repo.calls == []
    assert not record_path.exists()
    assert str(reason.value) in io.text  # 사유를 화면에 쓴다
    assert io.inputs == ["", "", ""] and io.sleeps == []  # 입력을 읽지도, 기다리지도 않았다


def test_FR16_cli_run_passes_options(sample_plan_path, fake_repo, make_io, record_path, beacon, t0):
    factory = _Factory(fake_repo)
    io = make_io(["", ""])

    code = _main(_run_argv(sample_plan_path, record_path, "--start-at", "2"), factory, io)

    assert code == EXIT_OK
    assert factory.count == 1
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "none", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
        ("start", "col-2", beacon, "locker-2", _after(t0, 40)),
        ("end", "col-2", _after(t0, 70)),
    ]
    assert [record["index"] for record in _records(record_path)] == [2, 3]  # 계획 파일 기준 번호다


def test_FR16_cli_run_starts_at_first_run_by_default(sample_plan_path, fake_repo, make_io, record_path):
    io = make_io(["", "", ""])

    code = _main(_run_argv(sample_plan_path, record_path), _Factory(fake_repo), io)

    assert code == EXIT_OK
    assert [record["index"] for record in _records(record_path)] == [1, 2, 3]


@pytest.mark.parametrize(
    ("inputs", "start_results", "expected_code"),
    [
        pytest.param([""], [BEACON_NOT_FOUND], 1, id="db_rejected"),
        pytest.param([KeyboardInterrupt()], [], 130, id="ctrl_c_at_prompt"),
    ],
)
def test_FR16_cli_run_returns_session_code(
    sample_plan_path, fake_repo, make_io, record_path, inputs, start_results, expected_code
):
    fake_repo.start_results.extend(start_results)
    io = make_io(inputs)

    code = _main(_run_argv(sample_plan_path, record_path), _Factory(fake_repo), io)

    assert code == expected_code  # run_session의 종료 코드를 그대로 돌려준다


def test_FR16_cli_start_at_out_of_range(sample_plan_path, fake_repo, make_io, record_path):
    io = make_io(["", "", ""])

    code = _main(_run_argv(sample_plan_path, record_path, "--start-at", "9"), _Factory(fake_repo), io)

    assert code == EXIT_INVALID
    assert fake_repo.calls == []
    assert "--start-at" in io.text and "9" in io.text  # 사유를 화면에 쓴다
    assert not record_path.exists()


@pytest.mark.parametrize("command", ["run", "close"])
def test_FR16_cli_repository_factory_error(command, sample_plan_path, fake_repo, make_io, record_path):
    argv = _run_argv(sample_plan_path, record_path) if command == "run" else CLOSE_ARGV
    factory = _Factory(fake_repo, error=RuntimeError("db file is locked"))
    io = make_io(["", "", ""])

    code = _main(argv, factory, io)

    assert code == EXIT_FAILED
    assert factory.count == 1
    assert "db file is locked" in io.text  # 사유
    assert fake_repo.calls == []
    assert io.inputs == ["", "", ""] and io.sleeps == []  # 회차를 시작하지 않았다
    assert not record_path.exists()


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no_arguments"),
        pytest.param(["collect"], id="unknown_command"),
        pytest.param(["run", "plan.json"], id="run_without_record"),
        pytest.param(["run", "plan.json", "--record", "records.jsonl", "--start-at", "abc"], id="start_at_not_integer"),
        pytest.param(["close"], id="close_without_collection_id"),
    ],
)
def test_FR16_cli_usage_errors(argv, fake_repo, make_io, capsys):
    factory = _Factory(fake_repo)

    code = _main(argv, factory, make_io([]))  # SystemExit이 나오지 않고 종료 코드로 돌아온다

    assert code == 2
    assert factory.count == 0
    assert fake_repo.calls == []
    assert "usage:" in capsys.readouterr().err  # argparse가 사용법을 쓴다


@pytest.mark.parametrize(
    ("argv", "expected_words"),
    [
        pytest.param(["--help"], ["run", "close"], id="top"),
        pytest.param(["run", "--help"], ["PLAN", "--record", "--start-at"], id="run"),
        pytest.param(["close", "--help"], ["--collection-id"], id="close"),
    ],
)
def test_FR16_cli_help_returns_zero(argv, expected_words, fake_repo, make_io, capsys):
    factory = _Factory(fake_repo)

    code = _main(argv, factory, make_io([]))

    assert code == EXIT_OK
    assert factory.count == 0
    out = capsys.readouterr().out
    assert all(word in out for word in expected_words)


def test_FR16_close_ends_collection(fake_repo, make_io, t0):
    factory = _Factory(fake_repo)
    io = make_io([])

    code = _main(CLOSE_ARGV, factory, io)

    assert code == EXIT_OK
    assert factory.count == 1
    assert fake_repo.calls == [("end", "col-x", t0)]  # 현재 시각으로 한 번 부른다. start는 부르지 않는다
    assert io.sleeps == []  # 기다리지 않는다
    assert CLOSED_TEXT in io.text and "col-x" in io.text


@pytest.mark.parametrize(
    ("failure", "expected_text"),
    [
        pytest.param(STATE_CONFLICT, "STATE_CONFLICT", id="state_conflict"),
        pytest.param(COLLECTION_NOT_FOUND, "COLLECTION_NOT_FOUND", id="collection_not_found"),
        pytest.param(INVALID_INTERVAL, "INVALID_INTERVAL", id="invalid_interval"),
        pytest.param(RuntimeError("db down"), "db down", id="error"),
    ],
)
def test_FR16_close_reports_rejection(fake_repo, make_io, t0, failure, expected_text):
    fake_repo.end_results.append(failure)
    io = make_io([])

    code = _main(CLOSE_ARGV, _Factory(fake_repo), io)

    assert code == EXIT_FAILED
    assert fake_repo.calls == [("end", "col-x", t0)]
    assert expected_text in io.text  # 사유
    assert "col-x" in io.text
    assert CLOSED_TEXT not in io.text  # 닫았다고 알리지 않는다


@pytest.mark.parametrize("command", ["run", "close"])
def test_FR16_cli_interrupt_returns_130(command, sample_plan_path, fake_repo, make_io, record_path):
    if command == "run":
        argv = _run_argv(sample_plan_path, record_path)
        factory = _Factory(fake_repo, error=KeyboardInterrupt())
    else:
        argv = CLOSE_ARGV
        factory = _Factory(fake_repo)
        fake_repo.end_results.append(KeyboardInterrupt())
    io = make_io([])

    code = _main(argv, factory, io)

    assert code == 130
    assert "Ctrl+C" in io.text
    assert CLOSED_TEXT not in io.text


def test_FR16_cli_interrupt_leaking_from_session_returns_130(
    sample_plan_path, fake_repo, make_io, record_path, monkeypatch
):
    def leaking_session(*args, **kwargs):
        raise KeyboardInterrupt  # run_session이 코드 몇 줄 사이의 Ctrl+C를 잡지 못하고 내보낸다

    monkeypatch.setattr(cli, "run_session", leaking_session)
    io = make_io([])

    code = _main(_run_argv(sample_plan_path, record_path), _Factory(fake_repo), io)

    assert code == 130
    assert "Ctrl+C" in io.text


def test_FR16_cli_main_reads_process_argv_and_default_io(fake_repo, make_io, monkeypatch, t0):
    io = make_io([])
    monkeypatch.setattr(cli, "default_io", lambda: _session_io(io))
    monkeypatch.setattr(sys, "argv", ["data_collection", *CLOSE_ARGV])

    code = main(None, repository_factory=_Factory(fake_repo))  # argv와 io를 모두 주지 않는다

    assert code == EXIT_OK
    assert fake_repo.calls == [("end", "col-x", t0)]  # sys.argv의 명령을 default_io의 시계로 실행했다


def test_FR16_default_io_uses_utc_clock():
    io = default_io()

    now = io.clock()

    assert now.tzinfo is timezone.utc  # UTC 기준 시각이 붙어 있다
    assert abs(now - datetime.now(timezone.utc)) < timedelta(minutes=1)  # 현지 시각에 UTC 딱지만 붙인 값이 아니다
    assert io.new_id is new_collection_id
    assert (io.sleep, io.read_line, io.write) == (time.sleep, input, print)
