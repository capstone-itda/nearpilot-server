"""회차 실행과 기록 파일 (FR-16). 가짜 저장소와 가짜 시계, 대기, 입력, 출력으로 확인한다. 실제 시간은 기다리지 않는다."""

from __future__ import annotations

import errno
import itertools
import json
import re
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import AdminWriteResult
from tools.data_collection import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    EXIT_OK,
    SessionIO,
    load_plan,
    new_collection_id,
    run_session,
)

APPLIED = AdminWriteResult(AdminWriteOutcome.APPLIED, AdminWriteReason.OK)
STATE_CONFLICT = AdminWriteResult(AdminWriteOutcome.CONFLICT, AdminWriteReason.STATE_CONFLICT)
INVALID_INTERVAL = AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.INVALID_INTERVAL)
RECORD_KEYS = ["index", "collection_id", "place_id", "beacon", "label", "spot", "started_at", "ended_at", "status"]


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


def _run(plan_path: Path, repo, fake_io, record_path, **kwargs) -> int:
    return run_session(load_plan(plan_path), repo, record_path, _session_io(fake_io), **kwargs)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def record_path(tmp_path: Path) -> Path:
    return tmp_path / "records.jsonl"


def test_FR16_runs_record_collections_in_order(sample_plan_path, fake_repo, make_io, record_path, beacon, t0):
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_OK
    # 회차마다 start 한 번, end 한 번이다. 시각은 자리 잡는 5초와 회차 30초를 더한 값이다
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "locker-1", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
        ("start", "col-2", beacon, "none", _after(t0, 40)),
        ("end", "col-2", _after(t0, 70)),
        ("start", "col-3", beacon, "locker-2", _after(t0, 75)),
        ("end", "col-3", _after(t0, 105)),
    ]
    assert io.inputs == []  # Enter 세 번이 회차 세 개를 시작했다
    assert "col-1" in io.text  # 회차를 시작하면 ID를 화면에 보인다


@pytest.mark.parametrize(("settle_sec", "run_sec"), [(5, 30.0), (0, 12.5), (2.5, 1)])
def test_FR16_run_timing_uses_plan_values(
    plan_data, write_plan, fake_repo, make_io, record_path, t0, settle_sec, run_sec
):
    plan_data["settle_sec"] = settle_sec
    plan_data["run_sec"] = run_sec
    io = make_io(["", "", ""])

    code = _run(write_plan(plan_data), fake_repo, io, record_path)

    assert code == EXIT_OK
    starts = [call[4] for call in fake_repo.calls if call[0] == "start"]
    ends = [call[2] for call in fake_repo.calls if call[0] == "end"]
    assert starts[0] == _after(t0, settle_sec)  # Enter 뒤 settle_sec
    assert ends == [_after(start, run_sec) for start in starts]  # 시작 뒤 run_sec
    assert starts[1:] == [_after(end, settle_sec) for end in ends[:-1]]  # 회차 사이에 따로 기다리지 않는다
    assert io.sleeps == [float(settle_sec), float(run_sec)] * 3  # settle_sec가 0이어도 대기를 부른다


def test_FR16_record_file_lines(sample_plan_path, fake_repo, make_io, record_path):
    old_line = '{"index": 9, "status": "done"}\n'
    record_path.write_text(old_line, encoding="utf-8")
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_OK
    lines = record_path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines[0] == old_line  # 덧붙인다. 기존 줄을 지우지 않는다
    new_lines = lines[1:]
    assert len(new_lines) == 3
    assert new_lines[0] == (
        '{"index": 1, "collection_id": "col-1", "place_id": "place-a", '
        '"beacon": {"uuid": "11111111-2222-3333-4444-555555555555", "major": 1, "minor": 1}, '
        '"label": "locker-1", "spot": "앞 위치, 손에 들고 화면 위", '
        '"started_at": "2026-10-15T12:00:05+00:00", "ended_at": "2026-10-15T12:00:35+00:00", '
        '"status": "done"}\n'
    )
    records = [json.loads(line) for line in new_lines]
    assert [list(record) for record in records] == [RECORD_KEYS] * 3
    beacon_json = {"uuid": "11111111-2222-3333-4444-555555555555", "major": 1, "minor": 1}
    assert records == [
        {
            "index": 1,
            "collection_id": "col-1",
            "place_id": "place-a",
            "beacon": beacon_json,
            "label": "locker-1",
            "spot": "앞 위치, 손에 들고 화면 위",
            "started_at": "2026-10-15T12:00:05+00:00",
            "ended_at": "2026-10-15T12:00:35+00:00",
            "status": "done",
        },
        {
            "index": 2,
            "collection_id": "col-2",
            "place_id": "place-a",
            "beacon": beacon_json,
            "label": "none",
            "spot": "locker-1 뒤 2 m",
            "started_at": "2026-10-15T12:00:40+00:00",
            "ended_at": "2026-10-15T12:01:10+00:00",
            "status": "done",
        },
        {
            "index": 3,
            "collection_id": "col-3",
            "place_id": "place-a",
            "beacon": beacon_json,
            "label": "locker-2",
            "spot": "앞 위치에서 왼쪽 0.2 m, 손에 들고 화면 위",
            "started_at": "2026-10-15T12:01:15+00:00",
            "ended_at": "2026-10-15T12:01:45+00:00",
            "status": "done",
        },
    ]


def test_FR16_start_at_resumes_from_index(sample_plan_path, fake_repo, make_io, record_path, beacon, t0):
    io = make_io(["", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path, start_at=2)

    assert code == EXIT_OK
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "none", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
        ("start", "col-2", beacon, "locker-2", _after(t0, 40)),
        ("end", "col-2", _after(t0, 70)),
    ]
    assert [record["index"] for record in _records(record_path)] == [2, 3]  # 계획 파일 기준 번호다
    assert "2/3" in io.text and "3/3" in io.text and "1/3" not in io.text


def test_FR16_collection_id_is_shown_before_run_starts(sample_plan_path, fake_repo, make_io, record_path):
    io = make_io(["", "", ""])
    shown_when_starting = []
    shown_when_ending = []
    start_collection = fake_repo.start_collection
    end_collection = fake_repo.end_collection

    def start_spy(collection_id, beacon, label, started_at):
        shown_when_starting.append(collection_id in io.text)  # 시작 직후 Ctrl+C가 와도 화면의 ID로 close할 수 있다
        return start_collection(collection_id, beacon, label, started_at)

    fake_repo.start_collection = start_spy

    def spy(collection_id, ended_at):
        shown_when_ending.append(collection_id in io.text)  # 프로세스가 갑자기 죽어도 화면의 ID로 close할 수 있다
        return end_collection(collection_id, ended_at)

    fake_repo.end_collection = spy

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_OK
    assert shown_when_starting == [True, True, True]
    assert shown_when_ending == [True, True, True]


@pytest.mark.parametrize("start_at", [1, 3])
def test_FR16_start_at_accepts_first_and_last_run(sample_plan_path, fake_repo, make_io, record_path, start_at):
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path, start_at=start_at)

    assert code == EXIT_OK  # 범위의 양 끝은 받는다
    assert [record["index"] for record in _records(record_path)] == list(range(start_at, 4))


def test_FR16_collection_id_format(sample_plan_path, fake_repo, make_io, record_path):
    first, second = new_collection_id(), new_collection_id()
    assert re.fullmatch(r"col-[0-9a-f]{32}", first)
    assert re.fullmatch(r"col-[0-9a-f]{32}", second)
    assert first != second

    # SessionIO의 기본 new_id가 new_collection_id다. 그 ID가 DB 호출과 기록 파일에 간다
    io = make_io(["", ""])
    default_io = SessionIO(clock=io.clock, sleep=io.sleep, read_line=io.read_line, write=io.write)
    assert default_io.new_id is new_collection_id
    code = run_session(load_plan(sample_plan_path), fake_repo, record_path, default_io, start_at=2)
    assert code == EXIT_OK
    ids = [call[1] for call in fake_repo.calls if call[0] == "start"]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert all(re.fullmatch(r"col-[0-9a-f]{32}", cid) for cid in ids)
    assert [record["collection_id"] for record in _records(record_path)] == ids


def test_FR16_prompt_shows_run_number_label_and_spot(sample_plan_path, fake_repo, make_io, record_path):
    io = make_io(["q"])

    code = _run(sample_plan_path, fake_repo, io, record_path, start_at=2)

    assert code == EXIT_OK
    assert "2/3" in io.text  # 계획 파일 기준 회차 번호와 전체 회차 수
    assert "none" in io.text and "locker-1 뒤 2 m" in io.text  # 라벨과 서는 위치 안내
    assert "Enter" in io.text and "q" in io.text  # Enter와 q 안내
    assert fake_repo.calls == []  # q면 회차를 만들지 않는다


@pytest.mark.parametrize("start_at", [0, 4, -1])
def test_FR16_start_at_out_of_range_is_rejected(sample_plan_path, fake_repo, make_io, record_path, start_at):
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path, start_at=start_at)

    assert code == EXIT_INVALID
    assert fake_repo.calls == []
    assert io.inputs == ["", "", ""]  # 입력을 읽지 않았다
    assert io.sleeps == []
    assert "--start-at" in io.text and str(start_at) in io.text  # 사유를 화면에 쓴다
    assert not record_path.exists()  # 기록 파일도 만들지 않는다


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (AdminWriteOutcome.REJECTED, AdminWriteReason.BEACON_NOT_FOUND),
        (AdminWriteOutcome.REJECTED, AdminWriteReason.NODE_NOT_FOUND),
        (AdminWriteOutcome.REJECTED, AdminWriteReason.COLLECTION_OVERLAP),
        (AdminWriteOutcome.CONFLICT, AdminWriteReason.STATE_CONFLICT),
    ],
)
def test_FR16_start_rejected_stops_session(
    sample_plan_path, fake_repo, make_io, record_path, beacon, t0, outcome, reason
):
    fake_repo.start_results.extend([APPLIED, AdminWriteResult(outcome, reason)])  # 둘째 회차의 start가 거부된다
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_FAILED
    assert reason.value in io.text
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "locker-1", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
        ("start", "col-2", beacon, "none", _after(t0, 40)),
    ]  # 거부된 회차의 end도, 셋째 회차도 부르지 않는다
    assert io.inputs == [""]  # 셋째 회차의 Enter를 기다리지 않았다
    assert [record["index"] for record in _records(record_path)] == [1]  # 거부된 회차는 기록하지 않는다


def test_FR16_start_error_stops_session(sample_plan_path, fake_repo, make_io, record_path, beacon, t0):
    fake_repo.start_results.append(RuntimeError("db down"))
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_FAILED
    assert "db down" in io.text  # 예외
    assert "col-1" in io.text and "close --collection-id col-1" in io.text  # 회차가 생겼는지 알 수 없어서 ID와 닫는 명령을 남긴다
    assert fake_repo.calls == [("start", "col-1", beacon, "locker-1", _after(t0, 5))]  # end를 부르지 않는다
    assert record_path.read_text(encoding="utf-8") == ""


def test_FR16_start_interrupt_keeps_collection_id(sample_plan_path, fake_repo, make_io, record_path, beacon, t0):
    fake_repo.start_results.append(KeyboardInterrupt())
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_INTERRUPTED
    assert "col-1" in io.text and "close --collection-id col-1" in io.text
    assert fake_repo.calls == [("start", "col-1", beacon, "locker-1", _after(t0, 5))]
    assert record_path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("failure", "expected_text"),
    [
        pytest.param(STATE_CONFLICT, "STATE_CONFLICT", id="rejected"),
        pytest.param(RuntimeError("db down"), "db down", id="error"),
    ],
)
def test_FR16_end_failure_keeps_collection_id(
    sample_plan_path, fake_repo, make_io, record_path, beacon, t0, failure, expected_text
):
    fake_repo.end_results.append(failure)
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_FAILED
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "locker-1", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
    ]  # 다음 회차를 부르지 않는다
    [record] = _records(record_path)
    assert (record["index"], record["collection_id"], record["status"]) == (1, "col-1", "end_failed")
    assert record["started_at"] == _after(t0, 5).isoformat()
    assert record["ended_at"] is None  # DB에 끝 시각이 없다
    assert expected_text in io.text  # 사유
    assert "col-1" in io.text and "close --collection-id col-1" in io.text


@pytest.mark.parametrize(
    ("end_result", "status", "expected_text"),
    [
        pytest.param(APPLIED, "aborted", "중단", id="end_applied"),
        pytest.param(INVALID_INTERVAL, "end_failed", "INVALID_INTERVAL", id="end_rejected"),
    ],
)
def test_FR16_interrupt_during_run_ends_collection(
    sample_plan_path, fake_repo, make_io, record_path, beacon, t0, end_result, status, expected_text
):
    fake_repo.end_results.extend([APPLIED, end_result])  # 첫 회차의 end는 정상이고, 둘째 회차의 end가 end_result다
    io = make_io(["", "", ""], interrupt_on_sleep=3)  # 둘째 회차의 회차 대기에서 Ctrl+C

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_INTERRUPTED
    started_at = _after(t0, 40)
    assert io.now == started_at  # 끊긴 대기는 시각을 늘리지 않는다
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "locker-1", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
        ("start", "col-2", beacon, "none", started_at),
        ("end", "col-2", io.now),  # Ctrl+C를 누른 시각으로 닫는다. 셋째 회차는 부르지 않는다
    ]
    assert io.sleeps == [5.0, 30.0, 5.0, 30.0]
    first, second = _records(record_path)
    assert first["status"] == "done"
    assert (second["index"], second["collection_id"], second["status"]) == (2, "col-2", status)
    assert second["ended_at"] == (started_at.isoformat() if status == "aborted" else None)
    assert expected_text in io.text
    assert ("close --collection-id col-2" in io.text) is (status == "end_failed")


def test_FR16_interrupt_while_ending_is_end_failed(sample_plan_path, fake_repo, make_io, record_path):
    fake_repo.end_results.append(KeyboardInterrupt())
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_INTERRUPTED
    assert len(fake_repo.calls) == 2  # 다음 회차를 부르지 않는다
    [record] = _records(record_path)
    assert (record["collection_id"], record["status"], record["ended_at"]) == ("col-1", "end_failed", None)
    assert "close --collection-id col-1" in io.text


@pytest.mark.parametrize(
    ("inputs", "interrupt_on_sleep", "expected_code"),
    [
        pytest.param(["q"], None, EXIT_OK, id="q"),
        pytest.param([" q "], None, EXIT_OK, id="q_with_spaces"),
        pytest.param([], None, EXIT_OK, id="eof"),
        pytest.param([KeyboardInterrupt()], None, EXIT_INTERRUPTED, id="ctrl_c_at_prompt"),
        pytest.param([""], 0, EXIT_INTERRUPTED, id="ctrl_c_while_settling"),
    ],
)
def test_FR16_quit_before_start_writes_nothing(
    sample_plan_path, fake_repo, make_io, record_path, inputs, interrupt_on_sleep, expected_code
):
    io = make_io(inputs, interrupt_on_sleep=interrupt_on_sleep)

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == expected_code
    assert fake_repo.calls == []
    assert record_path.read_text(encoding="utf-8") == ""  # 기록 파일은 열었지만 쓰지 않았다


def test_FR16_quit_between_runs_keeps_finished_runs(sample_plan_path, fake_repo, make_io, record_path, beacon, t0):
    io = make_io(["", "q"])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_OK
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "locker-1", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
    ]
    assert io.sleeps == [5.0, 30.0]  # q 뒤에는 기다리지 않는다
    assert [(record["index"], record["status"]) for record in _records(record_path)] == [(1, "done")]


def test_FR16_any_input_but_q_counts_as_enter(sample_plan_path, fake_repo, make_io, record_path):
    io = make_io(["go", "  ", "x"])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_OK
    assert [call[1] for call in fake_repo.calls if call[0] == "start"] == ["col-1", "col-2", "col-3"]


def test_FR16_record_line_is_flushed_before_next_run(sample_plan_path, fake_repo, make_io, record_path):
    lines_on_disk = []
    start_collection = fake_repo.start_collection

    def spy(*args):
        lines_on_disk.append(record_path.read_text(encoding="utf-8").count("\n"))  # 다른 핸들로 읽어서 flush했는지 본다
        return start_collection(*args)

    fake_repo.start_collection = spy
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_OK
    assert lines_on_disk == [0, 1, 2]  # 앞 회차의 줄이 다음 회차를 시작하기 전에 파일에 있다


@pytest.mark.parametrize(
    "make_path",
    [
        pytest.param(lambda tmp_path: tmp_path, id="directory"),
        pytest.param(lambda tmp_path: tmp_path / "no-such-dir" / "records.jsonl", id="missing_parent_directory"),
    ],
)
def test_FR16_unwritable_record_is_rejected_before_any_write(
    sample_plan_path, fake_repo, make_io, tmp_path, make_path
):
    record_path = make_path(tmp_path)
    io = make_io(["", "", ""])

    code = _run(sample_plan_path, fake_repo, io, record_path)

    assert code == EXIT_INVALID
    assert fake_repo.calls == []
    assert io.inputs == ["", "", ""]  # 입력을 읽지 않았다
    assert io.sleeps == []
    assert str(record_path) in io.text  # 사유를 화면에 쓴다


class _FullDisk(StringIO):
    """쓰면 디스크가 가득 찼다는 `OSError`를 던지는 파일."""

    def write(self, text: str) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


class _FullDiskPath:
    """`Path.open`만 흉내 낸다. 연 파일을 `files`에 남겨서 세션이 닫았는지 확인한다."""

    def __init__(self) -> None:
        self.files: list[_FullDisk] = []

    def open(self, *args, **kwargs) -> _FullDisk:
        self.files.append(_FullDisk())
        return self.files[-1]

    def __str__(self) -> str:
        return "full-disk.jsonl"


@pytest.mark.parametrize(
    ("end_failure", "expects_close_hint"),
    [
        pytest.param(None, False, id="end_applied"),
        pytest.param(RuntimeError("db down"), True, id="end_failed"),
    ],
)
def test_FR16_record_write_failure_stops_session(
    sample_plan_path, fake_repo, make_io, beacon, t0, end_failure, expects_close_hint
):
    if end_failure is not None:
        fake_repo.end_results.append(end_failure)
    disk = _FullDiskPath()
    io = make_io(["", "", ""])

    code = run_session(load_plan(sample_plan_path), fake_repo, disk, _session_io(io))

    assert code == EXIT_FAILED
    assert fake_repo.calls == [
        ("start", "col-1", beacon, "locker-1", _after(t0, 5)),
        ("end", "col-1", _after(t0, 35)),
    ]  # 다음 회차를 부르지 않는다
    assert "col-1" in io.text and "No space left on device" in io.text
    assert ("close --collection-id col-1" in io.text) is expects_close_hint  # 끝 시각을 기록하지 못했을 때만 닫는 명령을 안내한다
    assert [file.closed for file in disk.files] == [True]  # 실패해도 파일을 닫는다
