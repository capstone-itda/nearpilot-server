"""`nearpilot split` (FR-16, NFR-13, specs/core-ops-cli/spec.md 5절·6절 "split").

실제 SQLite 파일에 `main(argv)`를 같은 프로세스에서 불러 확인한다.
DB의 기기, 비콘, 회차, 프레임은 `make_collection_db`가 실제 쓰기 경로로 만든다.
분할 로직의 세부(몫, 제외 사유, 파일 형식)는 `tests/tools/test_evaluation_split.py`가 확인한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli import split as split_cli
from nearpilot.core.proximity.fit import AnchorLayout, PlaceLayout
from tools.evaluation.split import load_split

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 20, 5, 0, 0, tzinfo=timezone.utc)

FIT_FILE = """\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
min_distance_m = 0.3
taus = [1.0]
stays = [0.9]
window_secs = [2.0]

[acceptance]
max_misselection = 0.05
max_ece = 0.05
"""

TABLE = (
    "라벨  학습  검증  시험\n"
    "locker-1  3  1  1\n"
    "locker-2  3  1  1\n"
    "none  3  1  1\n"
)
EXCLUDED_TABLE = (
    "제외 회차  사유\n"
    "col-021  관측 없음\n"
    "col-022  라벨 기기가 장소에 없다\n"
    "col-023  진행 중\n"
)


def layout(place_id: str, *node_ids: str) -> PlaceLayout:
    """장소와 기기 ID만 쓰는 배치. 좌표는 DB에 들어가지 않는다."""
    return PlaceLayout(place_id, {node_id: AnchorLayout(0.0, 0.0, 0.0, 0.5) for node_id in node_ids})


def frames(node_id: str, count: int) -> dict[str, list[float]]:
    return {node_id: [-60.0] * count}


def write_fit(server: Path, name: str = "place-a.toml", text: str = FIT_FILE) -> Path:
    fit_dir = server / "config" / "fit"
    fit_dir.mkdir(parents=True, exist_ok=True)
    path = fit_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def split_path(server: Path, name: str = "place-a-seed42.json") -> Path:
    return (server / "var" / "splits" / name).resolve()


@pytest.fixture
def split_db(server_dir, make_collection_db) -> list[str]:
    """place-a(locker-1, locker-2)의 닫힌 회차를 라벨마다 5개 만들고, 분할에서 빠질 회차 셋과 place-b의 회차 5개를 더한다.

    col-001~015가 분할 대상이다(locker-1, locker-2, none 순서로 5개씩). col-016~020은 place-b의 회차다.
    col-021은 관측이 없고, col-022는 라벨 기기가 장소에 없고, col-023은 진행 중이다.
    """
    specs = [
        *[("locker-1", 30, frames("locker-1", 3)) for _ in range(5)],
        *[("locker-2", 30, frames("locker-2", 3)) for _ in range(5)],
        *[("none", 30, frames("locker-1", 2)) for _ in range(5)],
        *[("locker-9", 30, frames("locker-9", 3)) for _ in range(5)],
        ("locker-1", 30, {}),
        ("locker-8", 30, frames("locker-1", 1)),
        ("locker-2", None, frames("locker-2", 2)),
    ]
    runs = [(label, T0 + timedelta(seconds=60 * i), seconds, observed)
            for i, (label, seconds, observed) in enumerate(specs)]
    ids = make_collection_db(
        server_dir, [layout("place-a", "locker-1", "locker-2"), layout("place-b", "locker-9")], runs,
        unplaced=("locker-8",))
    assert ids[0] == "col-001" and ids[-1] == "col-023"
    return ids


@pytest.fixture
def fixed_now(monkeypatch):
    """분할 결과 파일의 작성 시각을 고정한다. `fixed_now.value`를 바꾸면 다음 실행부터 달라진다."""
    state = SimpleNamespace(value=NOW)
    monkeypatch.setattr(split_cli, "now_utc", lambda: state.value)
    return state


# ── 정상 실행 ─────────────────────────────────────────────────────────


def test_FR16_split_cli_writes_file(server_dir, run_cli, split_db, fixed_now):
    write_fit(server_dir)

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    path = split_path(server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert out == TABLE + EXCLUDED_TABLE + f"작성: {path}\n"
    result = load_split(path)
    assert (result.place_id, result.seed, result.ratios) == ("place-a", 42, (0.6, 0.2, 0.2))
    assert result.labels == {"locker-1": (3, 1, 1), "locker-2": (3, 1, 1), "none": (3, 1, 1)}
    assert (len(result.train), len(result.validation), len(result.test)) == (9, 3, 3)
    assert sorted(result.train + result.validation + result.test) == [f"col-{i:03d}" for i in range(1, 16)]
    assert result.excluded == (("col-021", "관측 없음"), ("col-022", "라벨 기기가 장소에 없다"),
                               ("col-023", "진행 중"))
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["db_path"] == str((server_dir / "var" / "nearpilot.db").resolve())
    assert data["created_at"] == "2026-10-20T05:00:00+00:00"
    assert b"\r\n" not in path.read_bytes() and text.endswith("}\n")


def test_FR16_split_cli_without_excluded_runs_prints_no_excluded_table(server_dir, run_cli, make_collection_db,
                                                                       fixed_now):
    runs = [("locker-1", T0 + timedelta(seconds=60 * i), 30, frames("locker-1", 2)) for i in range(5)]
    make_collection_db(server_dir, layout("place-a", "locker-1"), runs)
    write_fit(server_dir)

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert out == f"라벨  학습  검증  시험\nlocker-1  3  1  1\n작성: {split_path(server_dir)}\n"


def test_FR16_split_cli_seed_and_out_options(server_dir, run_cli, split_db, fixed_now, tmp_path):
    write_fit(server_dir)

    code, out, err = run_cli(["split", "place-a", "--seed", "7"], cwd=server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert out.splitlines()[-1] == f"작성: {split_path(server_dir, 'place-a-seed7.json')}"
    assert load_split(split_path(server_dir, "place-a-seed7.json")).seed == 7

    # 이름만 주면 var/splits/에, 경로를 주면 실행 폴더 기준으로 쓰고 없는 폴더는 만든다
    code, out, _ = run_cli(["split", "place-a", "--out", "mine.json"], cwd=server_dir)
    assert code == EXIT_OK and out.splitlines()[-1] == f"작성: {split_path(server_dir, 'mine.json')}"
    work = tmp_path / "work"
    work.mkdir()
    config = str(server_dir / "config" / "server.toml")
    code, out, _ = run_cli(["split", "place-a", "--out", "./sub/dir/x.json", "--config", config], cwd=work)
    assert code == EXIT_OK and out.splitlines()[-1] == f"작성: {(work / 'sub' / 'dir' / 'x.json').resolve()}"
    assert load_split(work / "sub" / "dir" / "x.json").place_id == "place-a"
    assert sorted(path.name for path in (server_dir / "var" / "splits").iterdir()) == [
        "mine.json", "place-a-seed7.json"]


def test_NFR13_split_deterministic_across_runs(server_dir, run_cli, split_db, fixed_now):
    write_fit(server_dir)

    texts = []
    for name in ("a.json", "b.json"):
        assert run_cli(["split", "place-a", "--out", name], cwd=server_dir)[0] == EXIT_OK
        texts.append(split_path(server_dir, name).read_bytes().decode("utf-8"))
        fixed_now.value += timedelta(hours=1, microseconds=3)  # 작성 시각만 달라진다

    assert texts[0] != texts[1]
    assert texts[0].replace("2026-10-20T05:00:00+00:00", "2026-10-20T06:00:00.000003+00:00") == texts[1]


def test_FR16_split_now_utc_is_utc():
    now = split_cli.now_utc()

    assert now.utcoffset() == timedelta(0)


# ── 덮어쓰지 않는다 ───────────────────────────────────────────────────


def test_NFR13_split_does_not_overwrite(server_dir, run_cli, split_db, fixed_now, monkeypatch):
    write_fit(server_dir)
    assert run_cli(["split", "place-a"], cwd=server_dir)[0] == EXIT_OK
    path = split_path(server_dir)
    before = path.read_bytes()
    fixed_now.value += timedelta(days=1)

    def must_not_read_db(config):
        raise AssertionError("결과 파일이 이미 있으면 DB를 읽기 전에 거부한다")

    with monkeypatch.context() as patch:
        patch.setattr(split_cli, "open_read_db", must_not_read_db)
        code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"분할 결과가 이미 있다: {path}. 다른 --seed나 --out을 쓴다\n"
    assert path.read_bytes() == before  # 내용이 그대로다

    # --out으로 이미 있는 파일을 가리켜도 같다
    code, out, err = run_cli(["split", "place-a", "--seed", "1", "--out", str(path)], cwd=server_dir)
    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"분할 결과가 이미 있다: {path}. 다른 --seed나 --out을 쓴다\n"
    assert path.read_bytes() == before

    # 시드를 바꾸면 새 파일이다
    assert run_cli(["split", "place-a", "--seed", "43"], cwd=server_dir)[0] == EXIT_OK
    assert split_path(server_dir, "place-a-seed43.json").is_file() and path.read_bytes() == before


# ── 입력 오류 (종료 코드 2) ───────────────────────────────────────────


def test_FR16_split_cli_requires_fit_file(server_dir, run_cli, split_db):
    fit_dir = (server_dir / "config" / "fit").resolve()

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"적합 설정 파일이 없다: {fit_dir / 'place-a.toml'}. {fit_dir}에 있는 파일: (없음)\n"
    assert not (server_dir / "var" / "splits").exists()

    # 다른 장소의 설정 파일이 있어도 이 장소의 파일이 아니다. 기본 폴더의 파일 목록을 알려 준다
    write_fit(server_dir, "place-b.toml")
    code, _, err = run_cli(["split", "place-a"], cwd=server_dir)
    assert code == EXIT_INVALID and err.endswith(f"{fit_dir}에 있는 파일: place-b.toml\n")

    # 경로로 주면 실행 폴더 기준이다
    code, _, err = run_cli(["split", "place-a", "--fit", "./nope.toml"], cwd=server_dir)
    assert code == EXIT_INVALID and err == f"적합 설정 파일이 없다: {(server_dir / 'nope.toml').resolve()}\n"


def test_FR16_split_cli_fit_option_name_and_path(server_dir, run_cli, split_db, fixed_now, tmp_path):
    strict = FIT_FILE.replace("min_runs_per_label = 5", "min_runs_per_label = 6")  # 닫힌 회차는 5개라 모자란다
    write_fit(server_dir, "place-a.toml")
    write_fit(server_dir, "strict.toml", strict)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "p.toml").write_text(strict, encoding="utf-8")
    config = str(server_dir / "config" / "server.toml")

    # 이름은 fit/ 폴더에서 찾는다 (기본은 <PLACE>.toml)
    assert run_cli(["split", "place-a", "--fit", "strict.toml"], cwd=server_dir)[0] == EXIT_FAILED
    assert not (server_dir / "var" / "splits").exists()
    assert run_cli(["split", "place-a"], cwd=server_dir)[0] == EXIT_OK
    # 경로는 실행 폴더 기준이다. 이름이면 elsewhere/p.toml 이 아니라 fit/ 폴더를 본다
    code, _, err = run_cli(["split", "place-a", "--fit", "p.toml", "--config", config, "--seed", "1"], cwd=elsewhere)
    assert code == EXIT_INVALID and err.startswith("적합 설정 파일이 없다: ")
    code, _, err = run_cli(["split", "place-a", "--fit", "./p.toml", "--config", config, "--seed", "1"], cwd=elsewhere)
    assert code == EXIT_FAILED and "회차가 부족한 라벨이 있다 (라벨마다 최소 6개)" in err


def test_FR16_split_cli_rejects_invalid_fit_file(server_dir, run_cli, split_db):
    write_fit(server_dir, "place-a.toml", FIT_FILE.replace("min_runs_per_label = 5", "min_runs_per_label = 2"))

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == "적합 설정 파일 오류: split.min_runs_per_label: 3 이상의 정수여야 한다 (2)\n"
    assert not (server_dir / "var" / "splits").exists()


def test_FR16_split_cli_without_database_is_input_error(server_dir, run_cli):
    write_fit(server_dir)

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == (f"DB 파일이 없다: {(server_dir / 'var' / 'nearpilot.db').resolve()}. "
                   "먼저 nearpilot serve --collect-only로 서버를 한 번 시작한다\n")
    assert not (server_dir / "var").exists()  # 빈 DB도 결과 폴더도 만들지 않는다


def test_FR16_split_cli_rejects_other_schema_version(server_dir, run_cli, split_db):
    write_fit(server_dir)
    conn = sqlite3.connect(server_dir / "var" / "nearpilot.db")
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith("DB 스키마 버전 99이 이 도구가 기대하는 ") and err.count("\n") == 1
    assert not (server_dir / "var" / "splits").exists()


def test_FR16_split_cli_unwritable_out_is_input_error(server_dir, run_cli, split_db):
    write_fit(server_dir)
    (server_dir / "var" / "splits").write_text("폴더 자리에 파일이 있다", encoding="utf-8")

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"분할 결과를 쓸 수 없다: {split_path(server_dir)} (")


# ── 분할할 수 없을 때 (종료 코드 1) ───────────────────────────────────


def test_NFR13_split_cli_rejects_too_few_runs(server_dir, run_cli, split_db):
    write_fit(server_dir, "place-a.toml", FIT_FILE.replace("min_runs_per_label = 5", "min_runs_per_label = 6"))

    code, out, err = run_cli(["split", "place-a"], cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "회차가 부족한 라벨이 있다 (라벨마다 최소 6개): locker-1 5개, locker-2 5개, none 5개\n"
    assert not (server_dir / "var" / "splits").exists()  # 파일도 폴더도 만들지 않는다


def test_NFR13_split_cli_rejects_place_without_runs(server_dir, run_cli, split_db):
    write_fit(server_dir, "place-z.toml")

    code, out, err = run_cli(["split", "place-z"], cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "place-z에 분할할 회차가 없다 (끝났고 관측이 있는 회차가 필요하다)\n"
    assert not (server_dir / "var" / "splits").exists()


# ── 읽기 전용 ─────────────────────────────────────────────────────────


def test_NFR13_split_cli_reads_through_a_read_only_connection(server_dir, run_cli, split_db, fixed_now, monkeypatch):
    write_fit(server_dir)
    db_file = server_dir / "var" / "nearpilot.db"
    real_list_runs = split_cli.list_runs
    seen = []

    def checking_list_runs(conn):
        with pytest.raises(sqlite3.OperationalError):  # 쓰기를 시도하면 sqlite가 거부한다
            conn.execute("DELETE FROM collection_run")
        seen.append(conn)
        return real_list_runs(conn)

    monkeypatch.setattr(split_cli, "list_runs", checking_list_runs)

    code, _, _ = run_cli(["split", "place-a"], cwd=server_dir)

    assert code == EXIT_OK and len(seen) == 1
    with pytest.raises(sqlite3.ProgrammingError):  # 읽고 나면 연결을 닫는다
        seen[0].execute("SELECT 1")
    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute("SELECT count(*) FROM collection_run").fetchone()[0] == 23  # DB는 그대로다
    finally:
        conn.close()


def test_FR16_split_cli_closes_connection_on_failure(server_dir, run_cli, split_db, monkeypatch):
    write_fit(server_dir)
    opened = []
    real = split_cli.open_read_db

    def spy(config):
        conn = real(config)
        opened.append(conn)
        return conn

    def failing(conn):
        raise RuntimeError("읽다가 실패")

    monkeypatch.setattr(split_cli, "open_read_db", spy)
    monkeypatch.setattr(split_cli, "list_runs", failing)

    with pytest.raises(RuntimeError, match="읽다가 실패"):
        run_cli(["split", "place-a"], cwd=server_dir)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


# ── 명령줄 ───────────────────────────────────────────────────────────


def test_FR16_split_config_option_goes_anywhere(server_dir, run_cli, split_db, fixed_now, tmp_path):
    write_fit(server_dir)
    config = str(server_dir / "config" / "server.toml")
    work = tmp_path / "work"
    work.mkdir()

    for i, argv in enumerate((["--config", config, "split", "place-a"],
                              ["split", "--config", config, "place-a"],
                              ["split", "place-a", "--config", config])):
        code, out, err = run_cli([*argv, "--out", f"{i}.json"], cwd=work)  # 실행 폴더와 상관없이 같은 설정을 읽는다
        assert (code, err) == (EXIT_OK, ""), argv
        assert out.startswith(TABLE)
    assert run_cli(["split", "place-a"], cwd=work)[0] == EXIT_INVALID  # 쓰지 않으면 실행 폴더의 config/server.toml


def test_FR16_split_help_and_usage_errors(run_cli, tmp_path):
    code, out, _ = run_cli(["--help"], cwd=tmp_path)
    assert code == EXIT_OK and "split" in out

    code, out, _ = run_cli(["split", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(text in out for text in ("PLACE", "--seed", "--fit", "--out", "--config", "42"))

    for argv in (["split"], ["split", "place-a", "--seed", "x"], ["split", "place-a", "extra"]):
        code, out, err = run_cli(argv, cwd=tmp_path)  # 인자 오류는 argparse가 사용법과 사유를 stderr에 쓰고 2다
        assert (code, out) == (EXIT_INVALID, ""), argv
        assert "nearpilot" in err and "split" in err, argv
