"""`nearpilot collect run/close/list` (FR-16, NFR-13, specs/core-ops-cli/spec.md 5절·6절 "collect").

실제 SQLite 파일에 `main(argv)`를 같은 프로세스에서 불러 확인한다. `run`과 `close`는 기존 수집 도구를 부르므로
`collect._session_io`를 가짜 `SessionIO`로 바꿔서 Enter 입력과 대기 시간을 기다리지 않는다.
DB의 기기, 비콘, 회차, 프레임은 `make_collection_db`가 실제 쓰기 경로로 만든다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_INVALID, EXIT_OK
from nearpilot.cli import collect as collect_cli
from nearpilot.core.proximity.fit import AnchorLayout, PlaceLayout
from nearpilot.shared.models import BeaconKey
from tools.data_collection.session import SessionIO

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
LIST_HEADER = "회차 ID  라벨  장소  시작(UTC)  길이  프레임  상태"

FIT_FILE = """\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 3

[fit]
min_distance_m = 0.3
taus = [1.0]
stays = [0.9]
window_secs = [2.0]

[acceptance]
max_misselection = 0.05
max_ece = 0.05
"""


class FakeSession:
    """`SessionIO`의 가짜 부품. Enter를 즉시 누르고, `sleep`은 시계만 앞으로 보내며 기다리지 않는다.

    `inputs`를 주면 `read_line`이 차례로 돌려주고(예외면 던지고) 다 쓰면 Enter("")다.
    수집 도구가 화면에 쓴 줄은 `lines`에 쌓인다 (stdout으로 나가지 않는다).
    """

    def __init__(self, start: datetime = T0, inputs: tuple = ()) -> None:
        self.now = start
        self.inputs = list(inputs)
        self.sleeps: list[float] = []
        self.lines: list[str] = []
        self._count = 0

    def io(self) -> SessionIO:
        return SessionIO(clock=self._clock, sleep=self._sleep, read_line=self._read_line, write=self.lines.append,
                         new_id=self._new_id)

    def _clock(self) -> datetime:
        return self.now

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def _read_line(self) -> str:
        if not self.inputs:
            return ""
        item = self.inputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def _new_id(self) -> str:
        self._count += 1
        return f"col-run-{self._count}"

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def use_session(monkeypatch):
    """`use_session(start=…, inputs=…)`: 수집 도구가 쓸 가짜 입출력을 걸고 `FakeSession`을 돌려준다."""

    def use(start: datetime = T0, inputs: tuple = ()) -> FakeSession:
        session = FakeSession(start, inputs)
        monkeypatch.setattr(collect_cli, "_session_io", session.io())
        return session

    return use


@pytest.fixture
def today(monkeypatch):
    """기본 기록 파일 이름의 날짜를 고정한다. `today.day`를 바꾸면 다음 날이 된다."""
    state = SimpleNamespace(day=date(2026, 10, 17))
    monkeypatch.setattr(collect_cli, "today_utc", lambda: state.day)
    return state


def layout(place_id: str, *node_ids: str) -> PlaceLayout:
    """장소와 기기 ID만 쓰는 배치. 좌표는 DB에 들어가지 않는다."""
    return PlaceLayout(place_id, {node_id: AnchorLayout(0.0, 0.0, 0.0, 0.5) for node_id in node_ids})


def timeline(*specs: tuple[str, float | None, dict]) -> list[tuple]:
    """`(label, seconds, 프레임)`을 60초 간격의 `make_collection_db` 회차 명세로 만든다. 구간이 겹치지 않는다."""
    return [
        (label, T0 + timedelta(seconds=60 * i), seconds, observed) for i, (label, seconds, observed) in enumerate(specs)
    ]


def frames(node_id: str, count: int) -> dict[str, list[float]]:
    return {node_id: [-60.0] * count}


def write_plan(server: Path, beacon, labels: tuple[str, ...] = ("locker-1",), name: str = "demo.json") -> Path:
    plans = server / "config" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    data = {
        "place_id": "place-a",
        "beacon": {"uuid": beacon.uuid, "major": beacon.major, "minor": beacon.minor},
        "settle_sec": 2,
        "run_sec": 30,
        "runs": [{"label": label, "spot": f"{label} 앞"} for label in labels],
    }
    path = plans / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def write_fit(server: Path, name: str = "place-a.toml", text: str = FIT_FILE) -> Path:
    fit_dir = server / "config" / "fit"
    fit_dir.mkdir(parents=True, exist_ok=True)
    path = fit_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def db_file(server: Path) -> Path:
    return server / "var" / "nearpilot.db"


def db_runs(server: Path) -> list[tuple]:
    """DB의 회차를 (회차 ID, 라벨, 끝 시각이 있는지)로 읽는다."""
    conn = sqlite3.connect(db_file(server))
    try:
        rows = conn.execute("SELECT collection_id, label, ended_at FROM collection_run ORDER BY started_at")
        return [(collection_id, label, ended_at is not None) for collection_id, label, ended_at in rows]
    finally:
        conn.close()


def record_entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ── collect run ──────────────────────────────────────────────────────


def test_FR16_collect_default_record_path(server_dir, run_cli, make_collection_db, collect_beacon, use_session,
                                          today, tmp_path):
    make_collection_db(server_dir, layout("place-a", "locker-1"), [])
    write_plan(server_dir, collect_beacon)
    session = use_session()

    code, out, err = run_cli(["collect", "run", "demo.json"], cwd=server_dir)

    # --record가 없으면 var/records/<계획 파일 이름>-<UTC 날짜>.jsonl 이다
    record = (server_dir / "var" / "records" / "demo-20261017.jsonl").resolve()
    assert (code, err) == (EXIT_OK, "")
    assert out == f"기록 파일: {record}\n"  # 시작 전에 쓴 한 줄뿐이다. 수집 도구의 안내는 가짜 입출력으로 갔다
    [entry] = record_entries(record)
    assert (entry["index"], entry["label"], entry["status"], entry["collection_id"]) == (
        1, "locker-1", "done", "col-run-1")
    assert entry["place_id"] == "place-a" and entry["ended_at"] is not None
    assert db_runs(server_dir) == [("col-run-1", "locker-1", True)]  # DB에 닫힌 회차 하나
    assert session.sleeps == [2, 30] and "모든 회차를 마쳤다." in session.lines

    # --record에 경로를 주면 실행 폴더 기준이다. 기본 기록 파일에는 쓰지 않는다.
    work = tmp_path / "work"
    work.mkdir()
    code, out, err = run_cli(["collect", "run", "demo.json", "--record", "./x.jsonl", "--config",
                              str(server_dir / "config" / "server.toml")], cwd=work)
    assert (code, err) == (EXIT_OK, "")
    assert out == f"기록 파일: {(work / 'x.jsonl').resolve()}\n"
    assert [e["collection_id"] for e in record_entries(work / "x.jsonl")] == ["col-run-2"]
    assert [e["collection_id"] for e in record_entries(record)] == ["col-run-1"]
    assert not (server_dir / "x.jsonl").exists()

    # 이름만 주면 기본 폴더(var/records/)에 쓴다
    code, out, _ = run_cli(["collect", "run", "demo.json", "--record", "mine.jsonl"], cwd=server_dir)
    assert code == EXIT_OK and out == f"기록 파일: {(server_dir / 'var' / 'records' / 'mine.jsonl').resolve()}\n"
    assert [e["collection_id"] for e in record_entries(server_dir / "var" / "records" / "mine.jsonl")] == [
        "col-run-3"]


def test_FR16_collect_default_record_appends_per_day(server_dir, run_cli, make_collection_db, collect_beacon,
                                                     use_session, today):
    make_collection_db(server_dir, layout("place-a", "locker-1"), [])
    write_plan(server_dir, collect_beacon)
    use_session()
    records = server_dir / "var" / "records"

    for _ in range(2):  # 같은 날은 같은 파일에 덧붙인다
        assert run_cli(["collect", "run", "demo.json"], cwd=server_dir)[0] == EXIT_OK
    today.day = date(2026, 10, 18)  # 날짜가 바뀌면 새 파일이다
    assert run_cli(["collect", "run", "demo.json"], cwd=server_dir)[0] == EXIT_OK

    assert sorted(path.name for path in records.iterdir()) == ["demo-20261017.jsonl", "demo-20261018.jsonl"]
    assert [e["collection_id"] for e in record_entries(records / "demo-20261017.jsonl")] == [
        "col-run-1", "col-run-2"]
    assert [e["collection_id"] for e in record_entries(records / "demo-20261018.jsonl")] == ["col-run-3"]


def test_FR16_collect_today_utc_is_the_utc_date(monkeypatch):
    # 기본 기록 파일 이름은 UTC 날짜다. 한국 시간 새벽(UTC로는 전날 16:30)에 실행해도 UTC로 센다.
    instant = datetime(2026, 10, 17, 16, 30, tzinfo=timezone.utc)

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:  # 시간대를 주지 않으면 지역 시각(한국 시간, 시간대 표시 없음)이다
                return (instant + timedelta(hours=9)).replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr(collect_cli, "datetime", FixedNow)
    assert collect_cli.today_utc() == date(2026, 10, 17)  # 지역 날짜라면 10월 18일이다


def test_FR16_collect_run_start_at_skips_earlier_runs(server_dir, run_cli, make_collection_db, collect_beacon,
                                                      use_session, today):
    make_collection_db(server_dir, layout("place-a", "locker-1"), [])
    write_plan(server_dir, collect_beacon, labels=("locker-1", "none"))
    use_session()

    code, out, _ = run_cli(["collect", "run", "demo.json", "--start-at", "2"], cwd=server_dir)

    assert code == EXIT_OK
    [entry] = record_entries(server_dir / "var" / "records" / "demo-20261017.jsonl")
    assert (entry["index"], entry["label"]) == (2, "none")  # 계획 파일 기준 번호
    assert db_runs(server_dir) == [("col-run-1", "none", True)]


def test_FR16_collect_run_returns_the_tool_exit_code(server_dir, run_cli, make_collection_db, collect_beacon,
                                                     use_session, today):
    make_collection_db(server_dir, layout("place-a", "locker-1"), [])
    write_plan(server_dir, collect_beacon, labels=("locker-1", "locker-1"))
    write_plan(server_dir, BeaconKey("99999999-0000-0000-0000-000000000000", 1, 1), name="unregistered.json")

    session = use_session(inputs=("q",))  # 첫 회차 앞에서 끝낸다
    assert run_cli(["collect", "run", "demo.json"], cwd=server_dir)[0] == EXIT_OK
    assert "수집을 끝낸다. 이 회차는 만들지 않았다." in session.lines and db_runs(server_dir) == []

    session = use_session(inputs=(KeyboardInterrupt(),))  # Enter를 기다리다 Ctrl+C
    assert run_cli(["collect", "run", "demo.json"], cwd=server_dir)[0] == EXIT_INTERRUPTED
    assert "Ctrl+C로 멈췄다. 이 회차는 만들지 않았다." in session.lines

    session = use_session()  # DB가 거부한다 (등록되지 않은 비콘)
    code, _, _ = run_cli(["collect", "run", "unregistered.json"], cwd=server_dir)
    assert code == EXIT_FAILED
    assert any("DB가 회차 1 시작을 거부했다" in line for line in session.lines)
    assert db_runs(server_dir) == []


def test_FR16_collect_run_invalid_plan_creates_no_database(server_dir, run_cli, use_session, today):
    plans = server_dir / "config" / "plans"
    plans.mkdir(parents=True)
    (plans / "bad.json").write_text('{"place_id": "place-a"}', encoding="utf-8")
    session = use_session()

    code, out, _ = run_cli(["collect", "run", "bad.json"], cwd=server_dir)

    assert code == EXIT_INVALID  # 수집 도구의 종료 코드를 그대로 돌려준다
    assert any(line.startswith("계획 파일이 올바르지 않다") for line in session.lines)
    assert not db_file(server_dir).exists()  # 도구가 검사를 마친 뒤에야 저장소를 만든다. 틀린 계획은 DB를 만들지 않는다
    assert not (server_dir / "var" / "records" / "bad-20261017.jsonl").exists()

    code, _, _ = run_cli(["collect", "run", "bad.json", "--start-at", "x"], cwd=server_dir)  # 숫자가 아닌 --start-at
    assert code == EXIT_INVALID


def test_FR16_collect_run_start_at_out_of_range_creates_no_database(server_dir, run_cli, collect_beacon, use_session,
                                                                    today):
    write_plan(server_dir, collect_beacon)
    session = use_session()

    code, _, _ = run_cli(["collect", "run", "demo.json", "--start-at", "5"], cwd=server_dir)

    assert code == EXIT_INVALID
    assert "--start-at은 1 이상 1 이하여야 한다 (5)" in session.lines
    assert not db_file(server_dir).exists()


def test_FR16_collect_run_missing_plan_lists_candidates(server_dir, run_cli, collect_beacon, use_session, today):
    write_plan(server_dir, collect_beacon, name="b.json")
    write_plan(server_dir, collect_beacon, name="a.json")
    use_session()
    plans = (server_dir / "config" / "plans").resolve()

    code, out, err = run_cli(["collect", "run", "nope.json"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"수집 계획 파일이 없다: {plans / 'nope.json'}. {plans}에 있는 파일: a.json, b.json\n"
    assert not (server_dir / "var").exists()  # 기록 폴더도 DB도 만들지 않는다

    # 경로로 주면 실행 폴더 기준이다. 기본 폴더에 같은 이름이 있어도 보지 않는다.
    code, out, err = run_cli(["collect", "run", "./a.json"], cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"수집 계획 파일이 없다: {(server_dir / 'a.json').resolve()}\n"


def test_FR16_collect_run_unwritable_record_folder_is_input_error(server_dir, run_cli, collect_beacon, use_session,
                                                                  today):
    write_plan(server_dir, collect_beacon)
    use_session()
    (server_dir / "var").mkdir()
    (server_dir / "var" / "records").write_text("폴더 자리에 파일이 있다", encoding="utf-8")

    code, out, err = run_cli(["collect", "run", "demo.json"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"기록 파일의 폴더를 만들 수 없다: {(server_dir / 'var' / 'records').resolve()} (")
    assert not db_file(server_dir).exists()


def test_FR16_collect_run_closes_database_connections(server_dir, run_cli, make_collection_db, collect_beacon,
                                                      use_session, today, monkeypatch):
    make_collection_db(server_dir, layout("place-a", "locker-1"), [])
    write_plan(server_dir, collect_beacon)
    use_session()
    opened = []
    real = collect_cli.open_write_db

    def spy(config):
        conn = real(config)
        opened.append(conn)
        return conn

    monkeypatch.setattr(collect_cli, "open_write_db", spy)

    assert run_cli(["collect", "run", "demo.json"], cwd=server_dir)[0] == EXIT_OK
    assert run_cli(["collect", "close", "--collection-id", "nothing"], cwd=server_dir)[0] == EXIT_FAILED

    assert len(opened) == 2
    for conn in opened:  # 끝난 뒤 연결이 열려 있으면 안 된다 (Windows는 열린 DB 파일을 지우지 못한다)
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


# ── collect close ────────────────────────────────────────────────────


def test_FR16_collect_close_ends_open_run(server_dir, run_cli, make_collection_db, use_session):
    ids = make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(
        ("locker-1", 30, frames("locker-1", 3)),
        ("locker-1", None, frames("locker-1", 2)),  # 서버가 죽거나 PC가 꺼져서 끝 시각을 못 남긴 회차
    ))
    assert ids == ["col-001", "col-002"] and db_runs(server_dir) == [("col-001", "locker-1", True),
                                                                      ("col-002", "locker-1", False)]
    session = use_session(start=T0 + timedelta(hours=1))

    code, out, err = run_cli(["collect", "close", "--collection-id", "col-002"], cwd=server_dir)

    assert (code, out, err) == (EXIT_OK, "", "")
    assert session.lines == ["회차를 닫았다 (col-002)."]
    assert db_runs(server_dir) == [("col-001", "locker-1", True), ("col-002", "locker-1", True)]
    conn = sqlite3.connect(db_file(server_dir))
    try:
        ended = conn.execute("SELECT ended_at FROM collection_run WHERE collection_id = 'col-002'").fetchone()[0]
    finally:
        conn.close()
    assert datetime.fromisoformat(ended) == T0 + timedelta(hours=1)  # 수집 도구의 시계(지금 시각)로 닫는다


def test_FR16_collect_close_rejects_closed_and_unknown_runs(server_dir, run_cli, make_collection_db, use_session):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(("locker-1", 30, frames("locker-1", 3))))
    session = use_session(start=T0 + timedelta(hours=1))

    for collection_id in ("col-001", "col-nothing"):  # 이미 닫힌 회차, 없는 회차
        session.lines.clear()
        code, out, _ = run_cli(["collect", "close", "--collection-id", collection_id], cwd=server_dir)
        assert (code, out) == (EXIT_FAILED, "")
        assert session.lines and session.lines[0].startswith("DB가 회차 닫기를 거부했다 (")
        assert session.lines[0].endswith(f"회차 ID: {collection_id}")
    assert db_runs(server_dir) == [("col-001", "locker-1", True)]


def test_FR16_collect_close_requires_collection_id(run_cli, tmp_path):
    code, out, err = run_cli(["collect", "close"], cwd=tmp_path)

    assert (code, out) == (EXIT_INVALID, "")
    assert "--collection-id" in err and "nearpilot collect close" in err


# ── collect list ─────────────────────────────────────────────────────


def test_FR16_collect_list_open_only(server_dir, run_cli, make_collection_db):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(
        ("locker-1", 30, frames("locker-1", 3)),
        ("none", 30, frames("locker-1", 2)),
        ("locker-1", None, frames("locker-1", 3)),
    ))

    code, out, err = run_cli(["collect", "list"], cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert out == (
        f"{LIST_HEADER}\n"
        "col-001  locker-1  place-a  2026-10-17 09:00:00  30.0초  3  닫힘\n"
        "col-002  none  place-a  2026-10-17 09:01:00  30.0초  2  닫힘\n"
        "col-003  locker-1  place-a  2026-10-17 09:02:00  -  3  "
        "진행 중 (닫기: nearpilot collect close --collection-id col-003)\n"
    )

    code, out, err = run_cli(["collect", "list", "--open"], cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert out == (
        f"{LIST_HEADER}\n"
        "col-003  locker-1  place-a  2026-10-17 09:02:00  -  3  "
        "진행 중 (닫기: nearpilot collect close --collection-id col-003)\n"
    )
    assert "col-001" not in out and "col-002" not in out


def test_FR16_collect_list_filters_by_place_and_label(server_dir, run_cli, make_collection_db):
    make_collection_db(server_dir, [layout("place-a", "locker-1", "locker-2"), layout("place-b", "locker-9")],
                       timeline(
        ("locker-1", 30, frames("locker-1", 1)),
        ("locker-2", 30, frames("locker-2", 1)),
        ("locker-9", 30, frames("locker-9", 1)),
        ("none", 30, frames("locker-9", 1)),
        ("none", 30, frames("locker-1", 1)),
    ))

    def listed(*argv: str) -> list[str]:
        code, out, err = run_cli(["collect", "list", *argv], cwd=server_dir)
        assert (code, err) == (EXIT_OK, "")
        return [line.split("  ")[0] for line in out.splitlines()[1:]]

    assert listed() == ["col-001", "col-002", "col-003", "col-004", "col-005"]
    assert listed("--place", "place-a") == ["col-001", "col-002", "col-005"]
    assert listed("--place", "place-b") == ["col-003", "col-004"]
    assert listed("--label", "none") == ["col-004", "col-005"]
    assert listed("--label", "locker-1") == ["col-001"]
    assert listed("--place", "place-b", "--label", "none") == ["col-004"]  # 거름 조건은 모두 맞아야 한다


def test_FR16_collect_list_marks_runs_without_a_place(server_dir, run_cli, make_collection_db):
    make_collection_db(server_dir, [layout("place-a", "locker-1"), layout("place-b", "locker-9")], timeline(
        ("locker-8", 30, frames("locker-1", 2)),  # 라벨의 기기가 장소에 없다
        ("none", 30, {}),  # 관측이 없다
        ("none", 30, {"locker-1": [-60.0] * 2, "locker-9": [-70.0] * 2}),  # 두 장소의 관측이 섞였다
        ("none", None, {}),  # 진행 중이면 닫는 안내가 먼저다
    ), unplaced=("locker-8",))

    code, out, err = run_cli(["collect", "list"], cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert out.splitlines()[1:] == [
        "col-001  locker-8  -  2026-10-17 09:00:00  30.0초  2  제외: 라벨 기기가 장소에 없다",
        "col-002  none  -  2026-10-17 09:01:00  30.0초  0  제외: 관측이 없어 장소를 정할 수 없다",
        "col-003  none  -  2026-10-17 09:02:00  30.0초  4  제외: 여러 장소의 관측이 섞였다",
        "col-004  none  -  2026-10-17 09:03:00  -  0  진행 중 (닫기: nearpilot collect close --collection-id col-004)",
    ]


def test_FR16_collect_list_summary(server_dir, run_cli, make_collection_db):
    make_collection_db(server_dir, [layout("place-a", "locker-1", "locker-2"), layout("place-b", "locker-9")],
                       timeline(
        ("locker-1", 30, frames("locker-1", 3)),
        ("locker-1", 30, frames("locker-1", 4)),
        ("locker-1", 30, frames("locker-1", 5)),  # locker-1: 닫힌 회차 3, 프레임 12 → 최소 3 이상
        ("locker-2", 30, frames("locker-2", 2)),
        ("locker-2", 30, frames("locker-2", 2)),  # locker-2: 닫힌 회차 2, 프레임 4 → 최소 미만
        ("locker-2", 30, {}),  # 프레임이 없는 회차는 세지 않는다
        ("none", 30, frames("locker-1", 1)),
        ("none", 30, frames("locker-2", 1)),
        ("none", 30, frames("locker-1", 1)),  # none: 닫힌 회차 3, 프레임 3
        ("none", 30, {"locker-1": [-60.0], "locker-9": [-60.0]}),  # 장소가 섞인 회차는 세지 않는다
        ("locker-9", 30, frames("locker-9", 7)),
        ("locker-2", None, frames("locker-2", 6)),  # 진행 중인 회차는 세지 않는다 (같은 비콘의 마지막 회차여야 한다)
    ))
    write_fit(server_dir, "place-a.toml")  # min_runs_per_label = 3

    code, out, err = run_cli(["collect", "list", "--summary", "--place", "place-a"], cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert out == (
        "라벨  닫힌 회차  프레임  분할 가능\n"
        "locker-1  3  12  예\n"
        "locker-2  2  4  아니오\n"
        "none  3  3  예\n"
    )

    # --place가 없으면 장소를 모르니 분할 가능 여부를 보지 않고, 모든 장소의 닫힌 회차를 센다
    code, out, err = run_cli(["collect", "list", "--summary"], cwd=server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert out == (
        "라벨  닫힌 회차  프레임\n"
        "locker-1  3  12\n"
        "locker-2  2  4\n"
        "locker-9  1  7\n"
        "none  3  3\n"
    )

    code, out, _ = run_cli(["collect", "list", "--summary", "--place", "place-a", "--label", "locker-1"],
                           cwd=server_dir)
    assert code == EXIT_OK and out == "라벨  닫힌 회차  프레임  분할 가능\nlocker-1  3  12  예\n"


def test_FR16_collect_list_summary_without_fit_file_says_so(server_dir, run_cli, make_collection_db):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(
        ("locker-1", 30, frames("locker-1", 3)),
        ("none", 30, frames("locker-1", 2)),
    ))
    expected_path = (server_dir / "config" / "fit" / "place-a.toml").resolve()

    code, out, err = run_cli(["collect", "list", "--summary", "--place", "place-a"], cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert out == (
        "라벨  닫힌 회차  프레임  분할 가능\n"
        "locker-1  1  3  -\n"
        "none  1  2  -\n"
        f"적합 설정 파일이 없어 분할 가능 여부를 보지 않았다: {expected_path}\n"
    )

    # --fit으로 다른 파일을 가리켜도 없으면 같다. 이름은 fit/ 폴더, 경로는 실행 폴더 기준이다.
    code, out, _ = run_cli(["collect", "list", "--summary", "--place", "place-a", "--fit", "other.toml"],
                           cwd=server_dir)
    assert code == EXIT_OK
    assert out.splitlines()[-1] == (
        f"적합 설정 파일이 없어 분할 가능 여부를 보지 않았다: {(server_dir / 'config' / 'fit' / 'other.toml').resolve()}")
    code, out, _ = run_cli(["collect", "list", "--summary", "--place", "place-a", "--fit", "./mine.toml"],
                           cwd=server_dir)
    assert code == EXIT_OK
    assert out.splitlines()[-1] == (
        f"적합 설정 파일이 없어 분할 가능 여부를 보지 않았다: {(server_dir / 'mine.toml').resolve()}")


def test_FR16_collect_list_summary_reads_the_chosen_fit_file(server_dir, run_cli, make_collection_db, tmp_path):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(
        *[("locker-1", 30, frames("locker-1", 2)) for _ in range(4)]))
    strict = FIT_FILE.replace("min_runs_per_label = 3", "min_runs_per_label = 5")
    write_fit(server_dir, "place-a.toml")  # 최소 3 → 예
    write_fit(server_dir, "strict.toml", strict)  # 최소 5 → 아니오
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "p.toml").write_text(strict, encoding="utf-8")

    def verdict(*extra: str, cwd: Path = server_dir) -> str:
        code, out, err = run_cli(["collect", "list", "--summary", "--place", "place-a", *extra], cwd=cwd)
        assert (code, err) == (EXIT_OK, "")
        return out.splitlines()[1].split("  ")[-1]

    assert verdict() == "예"  # 기본은 <장소>.toml
    assert verdict("--fit", "strict.toml") == "아니오"  # 이름은 fit/ 폴더에서 찾는다
    config = str(server_dir / "config" / "server.toml")
    assert verdict("--fit", "p.toml", "--config", config, cwd=elsewhere) == "-"  # 이름이면 fit/ 폴더라서 elsewhere/p.toml 이 아니다
    assert verdict("--fit", "./p.toml", "--config", config, cwd=elsewhere) == "아니오"  # 경로면 실행 폴더 기준


def test_FR16_collect_list_summary_rejects_invalid_fit_file(server_dir, run_cli, make_collection_db):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(("locker-1", 30, frames("locker-1", 2))))
    write_fit(server_dir, "place-a.toml", FIT_FILE.replace("min_runs_per_label = 3", "min_runs_per_label = 2"))

    code, out, err = run_cli(["collect", "list", "--summary", "--place", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")  # 표를 절반만 쓰고 끝내지 않는다
    assert err == "적합 설정 파일 오류: split.min_runs_per_label: 3 이상의 정수여야 한다 (2)\n"


def test_FR16_collect_list_without_runs(server_dir, run_cli, make_collection_db, announce_nodes):
    announce_nodes(server_dir, "locker-1")  # DB는 있고 회차가 없다

    for argv in (["list"], ["list", "--open"], ["list", "--summary"], ["list", "--summary", "--place", "place-a"]):
        code, out, err = run_cli(["collect", *argv], cwd=server_dir)
        assert (code, out, err) == (EXIT_OK, "수집 회차가 없다.\n", ""), argv

    # 회차가 있어도 거름 조건에 맞는 것이 없으면 같다
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(("locker-1", 30, frames("locker-1", 1))))
    for argv in (["list", "--open"], ["list", "--place", "place-z"], ["list", "--label", "none"]):
        code, out, err = run_cli(["collect", *argv], cwd=server_dir)
        assert (code, out, err) == (EXIT_OK, "수집 회차가 없다.\n", ""), argv


def test_FR16_collect_list_without_database_is_input_error(server_dir, run_cli):
    code, out, err = run_cli(["collect", "list"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"DB 파일이 없다: {(server_dir / 'var' / 'nearpilot.db').resolve()}\n"
    assert not (server_dir / "var").exists()  # sqlite가 빈 DB를 새로 만들지 않는다


def test_FR16_collect_list_rejects_other_schema_version(server_dir, run_cli, announce_nodes):
    announce_nodes(server_dir, "locker-1")
    conn = sqlite3.connect(db_file(server_dir))
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    code, out, err = run_cli(["collect", "list"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith("DB 스키마 버전 99이 이 도구가 기대하는 ") and err.count("\n") == 1


def test_NFR13_collect_list_reads_through_a_read_only_connection(server_dir, run_cli, make_collection_db,
                                                                monkeypatch):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(("locker-1", 30, frames("locker-1", 2))))
    real_list_runs = collect_cli.list_runs
    seen = []

    def checking_list_runs(conn):
        with pytest.raises(sqlite3.OperationalError):  # 쓰기를 시도하면 sqlite가 거부한다
            conn.execute("DELETE FROM collection_run")
        seen.append(conn)
        return real_list_runs(conn)

    monkeypatch.setattr(collect_cli, "list_runs", checking_list_runs)

    code, _, _ = run_cli(["collect", "list"], cwd=server_dir)

    assert code == EXIT_OK and len(seen) == 1
    with pytest.raises(sqlite3.ProgrammingError):  # 읽고 나면 연결을 닫는다
        seen[0].execute("SELECT 1")
    assert db_runs(server_dir) == [("col-001", "locker-1", True)]


def test_FR16_collect_list_closes_connection_on_failure(server_dir, run_cli, make_collection_db, monkeypatch):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(("locker-1", 30, frames("locker-1", 1))))
    opened = []
    real = collect_cli.open_readonly

    def spy(path):
        conn = real(path)
        opened.append(conn)
        return conn

    def failing(conn):
        raise RuntimeError("읽다가 실패")

    monkeypatch.setattr(collect_cli, "open_readonly", spy)
    monkeypatch.setattr(collect_cli, "list_runs", failing)

    with pytest.raises(RuntimeError, match="읽다가 실패"):
        run_cli(["collect", "list"], cwd=server_dir)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


# ── 명령줄 ───────────────────────────────────────────────────────────


def test_FR16_collect_config_option_goes_anywhere(server_dir, run_cli, make_collection_db, tmp_path):
    make_collection_db(server_dir, layout("place-a", "locker-1"), timeline(("locker-1", 30, frames("locker-1", 1))))
    config = str(server_dir / "config" / "server.toml")
    work = tmp_path / "work"
    work.mkdir()

    outputs = set()
    for argv in (["--config", config, "collect", "list"],
                 ["collect", "--config", config, "list"],
                 ["collect", "list", "--config", config]):
        code, out, err = run_cli(argv, cwd=work)  # 실행 폴더와 상관없이 같은 설정을 읽는다
        assert (code, err) == (EXIT_OK, ""), argv
        outputs.add(out)
    assert len(outputs) == 1 and "col-001" in outputs.pop()
    assert run_cli(["collect", "list"], cwd=work)[0] == EXIT_INVALID  # 쓰지 않으면 실행 폴더의 config/server.toml


def test_FR16_collect_help_and_usage_errors(run_cli, tmp_path):
    code, out, _ = run_cli(["--help"], cwd=tmp_path)
    assert code == EXIT_OK and "collect" in out

    code, out, _ = run_cli(["collect", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(name in out for name in ("run", "close", "list"))

    code, out, _ = run_cli(["collect", "run", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(text in out for text in ("FILE", "--record", "--start-at", "--config"))

    code, out, _ = run_cli(["collect", "list", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(text in out for text in ("--place", "--label", "--open", "--summary", "--fit"))

    for argv in (["collect"], ["collect", "unknown"], ["collect", "run"],
                 ["collect", "run", "plan.json", "--start-at", "x"]):
        code, out, err = run_cli(argv, cwd=tmp_path)  # 인자 오류는 argparse가 사용법과 사유를 stderr에 쓰고 2다
        assert (code, out) == (EXIT_INVALID, ""), argv
        assert "usage: nearpilot collect" in err, argv
    code, out, err = run_cli(["collect", "list", "--unknown"], cwd=tmp_path)  # 모르는 옵션은 최상위 파서가 알린다
    assert (code, out) == (EXIT_INVALID, "") and "unrecognized arguments: --unknown" in err
