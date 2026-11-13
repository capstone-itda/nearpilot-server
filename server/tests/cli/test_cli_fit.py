"""`nearpilot fit` (FR-16, NFR-13, specs/core-ops-cli/spec.md 5절·6절 "fit", specs/core-proximity-common-model/spec.md 6절 결정 13).

실제 SQLite 파일에 `main(argv)`를 같은 프로세스에서 불러 확인한다. DB의 기기, 비콘, 회차, 프레임은
`make_collection_db`가 실제 쓰기 경로로 만들고, 적합 설정·분할 결과 파일은 이 파일이 쓴다.
적합 계산의 세부는 `tests/core/proximity/test_common_fit.py`가 확인한다. 여기서는 DB의 프레임이 적합까지 이어지는지,
출력 표와 모델 파일, 종료 코드를 확인한다.

데이터: 기기 둘(locker-1, locker-2)이 있는 장소 place-a. 회차는 20초이고 기기마다 1초에 하나씩 RSSI를 받는다.
RSSI는 라벨마다 정한 평균(`LEVELS`) 둘레를 (−1, 0, +1, 0) × 흔들림으로 돌아서 역할별 값을 손으로 구할 수 있다 (`EXPECTED_ROLES`).
기기가 둘이면 자동 영점을 찾을 수 없다 (가장 센 기기를 뺀 기기가 둘 이상이어야 표본이 생긴다). 그래서 영점 표는 `-`이고
학습 회차의 값이 보정 없이 그대로 쓰인다. 장소가 여럿인 시험은 합성 생성기(`rooms=2`)가 만든 회차를 DB에 넣는다.
도우미가 만드는 프레임은 정수 초에 있고 수신(`received_at`)이 관측(`frame_ts`)보다 50ms 늦다. 평가 시각의 구간은
`round(window_sec / bin_sec)`칸으로 나뉘고 (`round`는 짝수 쪽으로 간다: `round(2.5)`는 2, `round(3.5)`는 4),
마지막 칸에 프레임이 없으면 그 칸은 예측만 해서 사후확률이 `stay`에서 멈춘다. 정수 초 구간은 마지막 프레임이 늦어서 빠지고,
3.5초는 칸이 넷인데 프레임이 셋이다. 2.5초는 칸 둘에 프레임이 하나씩이라 정답 확률이 1에 가깝다.
그래서 후보 `window_secs`는 2.5와 3.5를 쓴다 (조합마다 손실이 달라진다).
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nearpilot.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK
from nearpilot.cli import fit as fit_cli
from nearpilot.core.proximity.common_fit import CommonFitConfig, fit_common_model
from nearpilot.core.proximity.common_model import load_model_version
from nearpilot.core.proximity.synthetic import SyntheticSpec, generate_places
from tools.evaluation.split import SplitResult, split_to_json

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)

DEVICES = ("locker-1", "locker-2")
# 기기별 (자기 앞, 다른 기기 앞, 어느 기기 앞도 아님) 평균 RSSI(dBm)와 흔들림 폭(dB). none 회차의 흔들림은 두 배다.
# 기기마다 값이 달라서 역할이 뒤바뀌거나 표의 열이 바뀌면 어긋난다.
LEVELS = {"locker-1": (-44.0, -60.0, -80.0, 1.0), "locker-2": (-46.0, -64.0, -76.0, 2.0)}
JITTER = (-1.0, 0.0, 1.0, 0.0)  # 평균은 그대로 두고 표준편차만 0보다 크게 한다
TRAIN_LABELS = ("locker-1", "locker-1", "locker-2", "locker-2", "none", "none")
VALIDATION_LABELS = ("locker-1", "locker-2", "none")

# 학습 회차 데이터에서 손으로 구한 역할별 값 (mean, sigma). 두 기기의 표본이 40개씩이고, 흔들림 (−1, 0, +1, 0) × s가
# 5주기(20개)면 평균은 0이고 분산은 s²/2다. 두 기기를 합치면 평균은 두 평균의 평균이고,
# 분산은 (기기 분산 + (기기 평균 − 합친 평균)²)의 평균이다.
#   front: 기기 앞의 자기 값 (−44, s = 1)과 (−46, s = 2) → 평균 −45, 분산 ((0.5 + 1) + (2 + 1)) / 2 = 2.25
#   other: 다른 기기 앞의 값 (−60, s = 1)과 (−64, s = 2) → 평균 −62, 분산 ((0.5 + 4) + (2 + 4)) / 2 = 5.25
#   none: none 회차의 값 (−80, s = 2)과 (−76, s = 4) → 평균 −78, 분산 ((2 + 4) + (8 + 4)) / 2 = 9
EXPECTED_ROLES = {"front": (-45.0, 1.5), "other": (-62.0, math.sqrt(5.25)), "none": (-78.0, 3.0)}

ROLE_HEADER = "역할  mean  sigma"
ZERO_HEADER = "장소  기기  영점"
GRID_HEADER = "tau  stay  window_sec  손실"
ROLE_ROWS = [f"{role}  {mean:.2f}  {sigma:.2f}" for role, (mean, sigma) in EXPECTED_ROLES.items()]
ZERO_ROWS = [f"place-a  {node_id}  -" for node_id in DEVICES]  # 기기가 둘이라 영점이 없다
HEAD = [ROLE_HEADER, *ROLE_ROWS, ZERO_HEADER, *ZERO_ROWS, GRID_HEADER]  # 조합별 손실 줄 앞의 여덟 줄
GUIDE = '서버에서 쓰려면 설정의 [proximity]에 model_version = "v1"을 넣는다.'
DRY_RUN_LINE = "드라이런: 모델 파일을 쓰지 않았다."


def fit_file(taus: str = "[0.5, 1.0, 2.0]", stays: str = "[0.8, 0.9]", window_secs: str = "[2.5, 3.5]",
             auto_zero_min_bins: int = 120) -> str:
    """적합 설정 파일 내용. 기본은 조합 12개다."""
    return f"""\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
taus = {taus}
stays = {stays}
window_secs = {window_secs}
auto_zero_min_bins = {auto_zero_min_bins}

[acceptance]
max_misselection = 0.05
max_ece = 0.05
max_boundary_error = 0.05
"""


def rssis(node_id: str, label: str, seconds: int, *, validation: bool = False) -> list[float]:
    """`node_id`가 `label` 회차에서 1초마다 받는 RSSI `seconds`개.

    검증 회차는 학습 회차와 환경이 달라서 기기 앞 신호는 5dB 약하고, 다른 기기 앞은 3dB, none은 8dB 세고, 흔들림은 두 배다.
    그래서 검증 회차까지 역할별 값에 섞으면 값이 달라지고, 학습 회차만 썼는지 값으로 가려진다 (`EXPECTED_ROLES`).
    """
    near, far, none, spread = LEVELS[node_id]
    if label == "none":
        mean, spread, shift = none, 2 * spread, 8.0
    elif label == node_id:
        mean, shift = near, -5.0
    else:
        mean, shift = far, 3.0
    if validation:
        mean, spread = mean + shift, 2 * spread
    return [mean + spread * JITTER[k % len(JITTER)] for k in range(seconds)]


def printed_grid(out: str) -> list[str]:
    """출력의 조합별 손실 표 줄. 머리 줄 뒤부터 `작성:`이나 `드라이런` 줄 앞까지다."""
    lines = out.splitlines()
    start = lines.index(GRID_HEADER) + 1
    end = next((i for i, line in enumerate(lines) if line.startswith(("작성:", "드라이런"))), len(lines))
    return lines[start:end]


def parse_grid(rows: list[str]) -> list[tuple[float, float, float, float, bool]]:
    """표 줄을 (tau, stay, window_sec, 손실, 선택 표시)로 읽는다. 손실이 `inf`면 무한대다."""
    parsed = []
    for row in rows:
        cells = row.split("  ")
        parsed.append((float(cells[0]), float(cells[1]), float(cells[2]), float(cells[3]), cells[4:] == ["← 선택"]))
    return parsed


def check_grid(rows: list[str], count: int) -> list[tuple[float, float, float, float, bool]]:
    """조합별 손실 표의 모양: `count`줄이고, 손실이 작은 순서이고, 첫 줄에만 `← 선택`이 있다."""
    grid = parse_grid(rows)
    assert len(grid) == count
    assert [row[4] for row in grid] == [True] + [False] * (count - 1)
    losses = [row[3] for row in grid]
    assert losses == sorted(losses)
    return grid


def run_spec(index: int, label: str, *, seconds: int = 20, validation: bool = False, looks_like: str | None = None,
             in_progress: bool = False) -> tuple:
    """`make_collection_db`의 회차 한 건. `looks_like`는 라벨과 다른 라벨의 RSSI를 쓴다 (라벨을 잘못 단 회차)."""
    frames = {node_id: rssis(node_id, looks_like or label, seconds, validation=validation) for node_id in DEVICES}
    return label, T0 + timedelta(seconds=60 * index), None if in_progress else seconds, frames


def default_runs(*, validation_seconds: int = 20) -> list[tuple]:
    """학습 회차 여섯 건 뒤에 검증 회차 세 건."""
    specs = [(label, 20, False) for label in TRAIN_LABELS]
    specs += [(label, validation_seconds, True) for label in VALIDATION_LABELS]
    return [run_spec(i, label, seconds=seconds, validation=is_validation)
            for i, (label, seconds, is_validation) in enumerate(specs)]


def write_fit(server: Path, name: str = "common.toml", text: str | None = None) -> Path:
    path = server / "config" / "fit" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fit_file() if text is None else text, encoding="utf-8")
    return path


def write_split(server: Path, train, validation, test=(), *, place_id: str = "place-a",
                name: str = "place-a-seed42.json") -> Path:
    """분할 결과 파일. `labels`는 fit이 읽지 않아서 비워 둔다."""
    result = SplitResult(place_id, 42, (0.6, 0.2, 0.2), tuple(train), tuple(validation), tuple(test), (), {})
    path = server / "var" / "splits" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(split_to_json(result, created_at=T0, db_path="nearpilot.db"), encoding="utf-8", newline="\n")
    return path


def collect(server: Path, make_collection_db, runs: list[tuple] | None = None, *, n_train: int = len(TRAIN_LABELS),
            n_validation: int = len(VALIDATION_LABELS)) -> SimpleNamespace:
    """DB에 `runs`(기본은 `default_runs()`)를 만들고, 앞의 `n_train`건은 학습, 이어지는 `n_validation`건은 검증,
    나머지는 시험으로 나눈 분할 결과 파일과 적합 설정 파일(`common.toml`)을 쓴다. 회차 ID를 나눠 돌려준다."""
    ids = make_collection_db(server, {"place-a": list(DEVICES)}, default_runs() if runs is None else runs)
    data = SimpleNamespace(
        train=tuple(ids[:n_train]),
        validation=tuple(ids[n_train:n_train + n_validation]),
        test=tuple(ids[n_train + n_validation:]),
    )
    write_fit(server)
    write_split(server, data.train, data.validation, data.test)
    return data


def fit_argv(version: str = "v1", *extra: str) -> list[str]:
    return ["fit", "--split", "place-a-seed42.json", "--version", version, *extra]


def model_file(server: Path, version: str = "v1") -> Path:
    return (server / "var" / "models" / f"{version}.json").resolve()


@pytest.fixture
def server_dir(tmp_path: Path, write_config) -> Path:
    """기본 설정은 저장소의 시험용 모델 폴더를 가리킨다. 이 명령은 모델 파일을 쓰므로 임시 폴더 `var/models`를 가리킨다."""
    server = tmp_path / "server"
    write_config(server, proximity={"model_root": "../var/models"})
    return server


@pytest.fixture
def fit_data(server_dir, make_collection_db) -> SimpleNamespace:
    """학습 회차 6건(`col-001`~`col-006`)과 검증 회차 3건(`col-007`~`col-009`)이 있는 DB와, 이름으로 찾는 기본 위치의 입력 파일."""
    data = collect(server_dir, make_collection_db)
    assert data.train == tuple(f"col-{i:03d}" for i in range(1, 7)) and data.test == ()
    return data


# ── 정상 실행 ─────────────────────────────────────────────────────────


def test_FR16_fit_writes_model_file(server_dir, run_cli, fit_data):
    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    path = model_file(server_dir)
    assert (code, err) == (EXIT_OK, "")
    model = load_model_version(path.parent, "v1")  # 서버가 모델을 읽는 코드로 읽힌다
    assert model.model_version == "v1" and model.auto_zero_min_bins == 120  # 적합 설정 파일의 [fit]이 모델로 간다
    lines = out.splitlines()
    assert lines[:8] == HEAD and lines[-2:] == [f"작성: {path}", GUIDE]
    grid = check_grid(lines[8:-2], 10)  # 조합 12개 가운데 상위 10개
    # 첫 줄이 모델이 고른 조합이고, 그 줄의 손실이 모델 파일의 검증 손실이다
    assert grid[0][:3] == (model.calibration_tau, model.stay, model.window_sec)
    assert grid[0][3] == pytest.approx(model.validation["validation_loss"], abs=5e-5)
    assert set(model.validation) == {"validation_loss"}
    assert len({row[3] for row in grid}) > 1  # 조합마다 손실이 다르다 (창이 칸에 맞지 않는 조합은 손실이 크다)
    # 공통 모델은 장소도 기기도 모른다
    text = path.read_text(encoding="utf-8")
    assert not any(name in text for name in ("place-a", "locker-1", "locker-2"))
    assert [p.name for p in path.parent.iterdir()] == ["v1.json"] and b"\r\n" not in path.read_bytes()


def test_FR16_fit_fits_the_collected_frames(server_dir, run_cli, fit_data):
    """DB의 프레임이 적합까지 이어진다. 값은 데이터에서 손으로 구한 것과 같다."""
    assert run_cli(fit_argv(), cwd=server_dir)[0] == EXIT_OK

    model = load_model_version(model_file(server_dir).parent, "v1")
    for role, (mean, sigma) in EXPECTED_ROLES.items():
        assert (getattr(model, role).mean, getattr(model, role).sigma) == pytest.approx((mean, sigma)), role
    # 세 역할의 값이 모두 달라서, 역할이 뒤바뀌었다면 위에서 어긋난다
    assert len({EXPECTED_ROLES[role] for role in EXPECTED_ROLES}) == 3
    # 조합은 적합 설정 파일의 후보 가운데서 골랐다
    assert model.calibration_tau in (0.5, 1.0, 2.0) and model.stay in (0.8, 0.9) and model.window_sec in (2.5, 3.5)


def test_FR16_fit_dry_run_writes_nothing(server_dir, run_cli, fit_data):
    code, out, err = run_cli(fit_argv("v1", "--dry-run"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    lines = out.splitlines()
    assert lines[:8] == HEAD and len(lines) == 8 + 10 + 1 and lines[-1] == DRY_RUN_LINE
    check_grid(lines[8:-1], 10)
    assert "작성:" not in out and "[proximity]" not in out
    assert not (server_dir / "var" / "models").exists()  # 파일도 폴더도 만들지 않는다


def test_FR16_fit_dry_run_ignores_existing_version(server_dir, run_cli, fit_data):
    code, real_out, _ = run_cli(fit_argv(), cwd=server_dir)
    assert code == EXIT_OK
    path = model_file(server_dir)
    before = path.read_bytes()

    code, dry_out, err = run_cli(fit_argv("v1", "--dry-run"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")  # 쓰지 않으니 같은 버전의 파일이 있어도 된다
    assert dry_out == real_out.split("작성:")[0] + DRY_RUN_LINE + "\n"  # 표는 같다
    assert path.read_bytes() == before


def test_FR16_fit_grid_prints_inf_for_infinite_loss(server_dir, run_cli, make_collection_db):
    # 라벨을 잘못 단 검증 회차(locker-1 라벨에 locker-2 앞의 RSSI)가 있으면 τ가 아주 작은 조합은 정답 확률이 0이 된다
    runs = [*default_runs(), run_spec(9, "locker-1", looks_like="locker-2")]
    data = collect(server_dir, make_collection_db, runs, n_validation=4)
    assert len(data.validation) == 4
    write_fit(server_dir, text=fit_file(taus="[0.01, 0.5, 50.0]", stays="[0.9]"))

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    rows = printed_grid(out)
    assert len(rows) == 6 and out.splitlines()[:8] == HEAD
    infinite = [row for row in rows if row.endswith("  inf")]
    assert infinite and rows[-len(infinite):] == infinite  # 무한대는 `inf`로 쓰고 맨 뒤에 둔다
    assert all(row.startswith("0.01  ") for row in infinite)  # τ가 아주 작은 조합이다
    assert "inf" not in rows[0] and rows[0].endswith("  ← 선택")


def test_FR16_fit_does_not_read_test_runs(server_dir, run_cli, make_collection_db, monkeypatch):
    # 시험 회차 자리에 DB에 없는 ID와 진행 중인 회차를 둔다. 읽었다면 둘 다 거부된다.
    runs = [*default_runs(), run_spec(9, "locker-1", in_progress=True)]
    data = collect(server_dir, make_collection_db, runs)
    assert data.test == ("col-010",)
    write_split(server_dir, data.train, data.validation, ("col-999", *data.test))
    asked = []
    real = fit_cli.load_labeled_runs
    monkeypatch.setattr(fit_cli, "load_labeled_runs", lambda conn, ids: asked.append(tuple(ids)) or real(conn, ids))

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert asked == [data.train, data.validation]
    assert model_file(server_dir).is_file()


def test_FR16_fit_uses_frame_interval_from_settings(server_dir, run_cli, fit_data, write_config, monkeypatch):
    write_config(server_dir, settings={"version": "t1", "rssi_frame_sec": 0.5}, proximity={"model_root": "../var/models"})
    seen = []
    real = fit_cli.fit_common_model

    def spy(*args, **kwargs):
        seen.append(kwargs["bin_sec"])
        return real(*args, **kwargs)

    monkeypatch.setattr(fit_cli, "fit_common_model", spy)

    code, _, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert seen == [0.5]


# ── 장소가 여럿 ───────────────────────────────────────────────────────


def test_FR16_cli_fit_multiple_places(server_dir, run_cli, make_collection_db):
    """합성 생성기(`rooms=2`)의 두 장소 회차로 모델 파일 하나를 만든다. 파일은 장소도 기기도 모른다."""
    spec = SyntheticSpec(
        rooms=2, devices=(3, 3), train_runs=2, validation_runs=1, run_bins=8,
        taus=(1.0,), stays=(0.9,), window_secs=(3.5,), min_bins=20)
    train, validation = generate_places(spec, bin_sec=1.0)
    places = {
        group.place_id: sorted({frame.node_id for run in group.runs for frame in run.frames}) for group in train}
    assert len(places) == 2 and all(len(nodes) == 3 for nodes in places.values())

    # 회차는 시작 시각이 이른 순서로 DB에 넣는다. 생성기의 프레임은 칸의 가운데(+0.5초)이고 DB의 도우미는 칸의 시작이다.
    # 칸 번호는 같아서 칸 평균과 자동 영점은 같다.
    tagged = [
        (run, group.place_id, kind)
        for kind, groups in (("train", train), ("validation", validation)) for group in groups for run in group.runs
    ]
    tagged.sort(key=lambda item: item[0].started_at)
    db_runs = [
        (run.label, run.started_at, float(spec.run_bins),
         {node_id: [f.rssi for f in sorted(run.frames, key=lambda f: f.frame_ts) if f.node_id == node_id]
          for node_id in places[place_id]})
        for run, place_id, _ in tagged
    ]
    ids = make_collection_db(server_dir, places, db_runs)
    ids_of: dict[tuple[str, str], list[str]] = {}
    for collection_id, (_, place_id, kind) in zip(ids, tagged):
        ids_of.setdefault((place_id, kind), []).append(collection_id)
    for place_id in places:
        write_split(server_dir, ids_of[(place_id, "train")], ids_of[(place_id, "validation")], place_id=place_id,
                    name=f"{place_id}-seed42.json")
    write_fit(server_dir, text=fit_file(taus="[1.0]", stays="[0.9]", window_secs="[3.5]", auto_zero_min_bins=20))

    code, out, err = run_cli(
        ["fit", *[arg for place_id in places for arg in ("--split", f"{place_id}-seed42.json")], "--version", "v1"],
        cwd=server_dir)

    path = model_file(server_dir)
    assert (code, err) == (EXIT_OK, "")
    assert path.is_file()
    model = load_model_version(path.parent, "v1")  # 서버가 모델을 읽는 코드로 읽힌다
    assert (model.model_version, model.auto_zero_min_bins) == ("v1", 20)
    text = path.read_text(encoding="utf-8")
    assert not any(name in text for name in (*places, *(node for nodes in places.values() for node in nodes)))
    # DB를 거친 적합은 생성기의 회차를 바로 적합한 것과 같은 역할별 값과 자동 영점을 준다
    direct = fit_common_model(
        train, validation, CommonFitConfig("v1", spec.taus, spec.stays, spec.window_secs, 20), bin_sec=1.0)
    for role in ("front", "other", "none"):
        assert (getattr(model, role).mean, getattr(model, role).sigma) == pytest.approx(
            (getattr(direct.model, role).mean, getattr(direct.model, role).sigma)), role
    zero_rows = [
        f"{place_id}  {node_id}  " + (f"{direct.zeros[place_id][node_id]:.2f}" if node_id in direct.zeros[place_id] else "-")
        for place_id in sorted(places) for node_id in places[place_id]
    ]
    lines = out.splitlines()
    assert lines[lines.index(ZERO_HEADER) + 1:lines.index(GRID_HEADER)] == zero_rows
    assert any(not row.endswith("  -") for row in zero_rows)  # 기기가 셋이면 영점을 찾는다


def test_FR16_cli_fit_rejects_duplicate_place(server_dir, run_cli, fit_data):
    first = write_split(server_dir, fit_data.train, fit_data.validation, name="a.json").resolve()
    second = write_split(server_dir, fit_data.train, fit_data.validation, name="b.json").resolve()

    code, out, err = run_cli(["fit", "--split", "a.json", "--split", "b.json", "--version", "v1"], cwd=server_dir)

    # 같은 장소의 분할 결과 둘은 어느 쪽을 쓸지 정할 수 없다. DB를 읽기 전에 입력 오류로 거부한다.
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"같은 장소의 분할 결과가 둘이다: place-a ({first}, {second}). 장소마다 하나만 준다\n"
    assert not (server_dir / "var" / "models").exists()


def test_FR16_fit_old_arguments_are_gone(server_dir, run_cli, fit_data):
    # 배치 파일 인자와 --calib-version은 없다. 공통 모델은 배치도 보정 버전도 받지 않는다.
    for argv in (["fit", "place-a.json", "--split", "place-a-seed42.json", "--version", "v1"],
                 [*fit_argv("v1"), "--calib-version", "c1"]):
        code, out, err = run_cli(argv, cwd=server_dir)

        assert (code, out) == (EXIT_INVALID, ""), argv
        assert "unrecognized arguments" in err, argv
    assert not (server_dir / "var" / "models").exists()


# ── 거부 (종료 코드 1) ────────────────────────────────────────────────


def test_FR16_fit_existing_version_not_overwritten(server_dir, run_cli, fit_data, monkeypatch):
    assert run_cli(fit_argv(), cwd=server_dir)[0] == EXIT_OK
    path = model_file(server_dir)
    before = path.read_bytes()

    def must_not_read_db(config):
        raise AssertionError("모델 파일이 이미 있으면 DB를 읽기 전에 거부한다")

    with monkeypatch.context() as patch:
        patch.setattr(fit_cli, "open_read_db", must_not_read_db)
        code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"모델 파일이 이미 있다: {path}. 다른 --version을 쓴다\n"
    assert path.read_bytes() == before  # 내용이 그대로다

    # 버전을 바꾸면 새 파일이다
    assert run_cli(fit_argv("v2"), cwd=server_dir)[0] == EXIT_OK
    assert sorted(p.name for p in path.parent.iterdir()) == ["v1.json", "v2.json"] and path.read_bytes() == before


def test_FR16_fit_file_created_during_fit_is_not_overwritten(server_dir, run_cli, fit_data, monkeypatch):
    path = model_file(server_dir)
    real = fit_cli.fit_common_model

    def fit_then_other_process_writes(*args, **kwargs):
        result = real(*args, **kwargs)
        path.parent.mkdir(parents=True)
        path.write_text("다른 곳에서 만든 파일", encoding="utf-8")
        return result

    monkeypatch.setattr(fit_cli, "fit_common_model", fit_then_other_process_writes)

    code, _, err = run_cli(fit_argv(), cwd=server_dir)

    assert code == EXIT_FAILED
    assert err == f"모델 파일이 이미 있다: {path}. 다른 --version을 쓴다\n"
    assert path.read_text(encoding="utf-8") == "다른 곳에서 만든 파일"


def test_FR16_fit_error_writes_nothing(server_dir, run_cli, make_collection_db):
    # 검증 회차가 가장 짧은 window_sec(2.5초)보다 짧아서 평가 시각이 없다
    collect(server_dir, make_collection_db, default_runs(validation_seconds=1))

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err.startswith("적합 실패: 회차 'col-007'") and err.count("\n") == 1
    assert not (server_dir / "var" / "models").exists()  # 파일도 폴더도 만들지 않는다


def test_FR16_fit_missing_label_fails(server_dir, run_cli, make_collection_db):
    # locker-2 라벨의 회차가 분할에서 모두 빠졌다. 프레임에는 locker-2가 나오므로 앵커다. 적합 전에 거부한다.
    train, validation = ("locker-1", "locker-1", "none", "none"), ("locker-1", "none")
    specs = [(label, 20, False) for label in train] + [(label, 20, True) for label in validation]
    runs = [run_spec(i, label, seconds=seconds, validation=is_validation)
            for i, (label, seconds, is_validation) in enumerate(specs)]
    collect(server_dir, make_collection_db, runs, n_train=4, n_validation=2)

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err.startswith("학습 회차에 라벨이 없는 기기가 있다: locker-2.") and err.count("\n") == 1
    assert not (server_dir / "var" / "models").exists()


def test_FR16_fit_version_must_be_a_file_name(server_dir, run_cli, fit_data):
    for version in ("../evil", "../../evil", "a/b", "a\\b", "..", "."):
        code, out, err = run_cli(fit_argv(version), cwd=server_dir)

        # DB를 읽기 전에 입력 오류로 거부한다
        assert (code, out) == (EXIT_INVALID, ""), version
        assert err.startswith("--version은(는) 경로 구분자가 없는 이름이어야 한다"), version
    # 모델 폴더도, 폴더 밖으로 나간 파일도 만들지 않는다
    assert not (server_dir / "var" / "models").exists() and not (server_dir / "var" / "evil.json").exists()


def test_FR16_fit_unreadable_runs_fail(server_dir, run_cli, make_collection_db):
    runs = [*default_runs(), run_spec(9, "locker-1", in_progress=True)]
    data = collect(server_dir, make_collection_db, runs, n_validation=4)  # 검증 회차의 마지막이 진행 중이다
    assert data.validation[-1] == "col-010"

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "수집 회차를 읽을 수 없다: 진행 중인 회차 col-010\n"
    write_split(server_dir, (*data.train[:-1], "col-998"), data.validation[:-1], name="ghost.json")  # 학습 회차 하나가 DB에 없다
    code, out, err = run_cli(["fit", "--split", "ghost.json", "--version", "v1"], cwd=server_dir)
    assert (code, out) == (EXIT_FAILED, "")
    assert err == "수집 회차를 읽을 수 없다: DB에 없는 회차 col-998\n"
    assert not (server_dir / "var" / "models").exists()


# ── 입력 오류 (종료 코드 2) ───────────────────────────────────────────


def test_FR16_fit_checks_input_files_in_order(server_dir, run_cli):
    splits = (server_dir / "var" / "splits").resolve()
    fit_dir = (server_dir / "config" / "fit").resolve()
    db_file = (server_dir / "var" / "nearpilot.db").resolve()

    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"분할 결과 파일이 없다: {splits / 'place-a-seed42.json'}. {splits}에 있는 파일: (없음)\n"

    write_split(server_dir, ["col-001"], ["col-002"])
    code, _, err = run_cli(fit_argv(), cwd=server_dir)
    assert code == EXIT_INVALID
    assert err == f"적합 설정 파일이 없다: {fit_dir / 'common.toml'}. {fit_dir}에 있는 파일: (없음)\n"

    write_fit(server_dir, "place-a.toml")  # fit의 기본 설정 파일은 장소 이름이 아니라 common.toml이다
    code, _, err = run_cli(fit_argv(), cwd=server_dir)
    assert code == EXIT_INVALID and err.endswith(f"{fit_dir}에 있는 파일: place-a.toml\n")

    write_fit(server_dir)  # 파일 인자를 모두 풀고서야 DB를 연다
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"DB 파일이 없다: {db_file}. 먼저 nearpilot serve --collect-only로 서버를 한 번 시작한다\n"
    assert not db_file.exists() and not (server_dir / "var" / "models").exists()  # 빈 DB도 모델 폴더도 만들지 않는다


def test_FR16_fit_rejects_invalid_files(server_dir, run_cli):
    split_file = write_split(server_dir, ["col-001"], ["col-002"])
    split_file.write_text("JSON이 아니다", encoding="utf-8")
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"분할 결과 파일 오류: {split_file.resolve()}: UTF-8 JSON으로 읽을 수 없다 (")

    write_split(server_dir, ["col-001"], ["col-002"])
    write_fit(server_dir, text=fit_file().replace("auto_zero_min_bins = 120\n", ""))
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == "적합 설정 파일 오류: fit.auto_zero_min_bins: 필드가 없다\n"

    # 옛 배치 방식의 [fit] 키는 이제 모르는 필드다
    write_fit(server_dir, text=fit_file().replace("auto_zero_min_bins = 120\n", "min_distance_m = 0.3\n"))
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == "적합 설정 파일 오류: fit.auto_zero_min_bins: 필드가 없다; fit.min_distance_m: 모르는 필드다\n"
    assert not (server_dir / "var" / "models").exists()


def test_FR16_fit_unwritable_model_folder_is_input_error(server_dir, run_cli, fit_data):
    models = (server_dir / "var" / "models").resolve()
    models.write_text("폴더 자리에 파일이 있다", encoding="utf-8")

    code, _, err = run_cli(fit_argv(), cwd=server_dir)

    assert code == EXIT_INVALID
    assert err.startswith(f"모델 파일을 쓸 수 없다: {models / 'v1.json'} (")
    assert models.read_text(encoding="utf-8") == "폴더 자리에 파일이 있다"


# ── 읽기 전용 ─────────────────────────────────────────────────────────


def test_NFR13_fit_reads_through_a_read_only_connection(server_dir, run_cli, fit_data, monkeypatch):
    db_file = server_dir / "var" / "nearpilot.db"
    real = fit_cli.load_labeled_runs
    seen = []

    def checking(conn, ids):
        with pytest.raises(sqlite3.OperationalError):  # 쓰기를 시도하면 sqlite가 거부한다
            conn.execute("DELETE FROM collection_run")
        seen.append(conn)
        return real(conn, ids)

    monkeypatch.setattr(fit_cli, "load_labeled_runs", checking)

    code, _, _ = run_cli(fit_argv(), cwd=server_dir)

    assert code == EXIT_OK and len(seen) == 2  # 학습 회차와 검증 회차를 한 번씩 읽는다
    with pytest.raises(sqlite3.ProgrammingError):  # 읽고 나면 연결을 닫는다
        seen[0].execute("SELECT 1")
    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute("SELECT count(*) FROM collection_run").fetchone()[0] == 9  # DB는 그대로다
    finally:
        conn.close()


def test_FR16_fit_closes_connection_on_failure(server_dir, run_cli, fit_data, monkeypatch):
    opened = []
    real = fit_cli.open_read_db

    def spy(config):
        conn = real(config)
        opened.append(conn)
        return conn

    def failing(conn, ids):
        raise RuntimeError("읽다가 실패")

    monkeypatch.setattr(fit_cli, "open_read_db", spy)
    monkeypatch.setattr(fit_cli, "load_labeled_runs", failing)

    with pytest.raises(RuntimeError, match="읽다가 실패"):
        run_cli(fit_argv(), cwd=server_dir)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


# ── 명령줄 ───────────────────────────────────────────────────────────


def test_FR16_fit_file_arguments_name_and_path(server_dir, run_cli, fit_data, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    config = str(server_dir / "config" / "server.toml")
    # 실행 폴더에만 있는 사본이다. 경로로 주면 이 사본을 읽고, 이름으로 주면 기본 폴더에서 찾으니 없다.
    (work / "mine-split.json").write_bytes(write_split(server_dir, fit_data.train, fit_data.validation).read_bytes())
    (work / "mine.toml").write_text(fit_file(taus="[1.0]", stays="[0.9]", window_secs="[2.5]"), encoding="utf-8")

    def run(split: str, fit: str) -> tuple[int, str, str]:
        return run_cli(["fit", "--split", split, "--fit", fit, "--version", "v1", "--config", config, "--dry-run"],
                       cwd=work)

    code, out, err = run("./mine-split.json", "./mine.toml")
    assert (code, err) == (EXIT_OK, "")
    assert len(out.splitlines()) == 8 + 1 + 1  # 사본 적합 설정의 조합 하나만 쓴다 (기본 폴더의 파일은 열두 개다)
    for names, kind in ((("mine-split.json", "./mine.toml"), "분할 결과"),
                        (("./mine-split.json", "mine.toml"), "적합 설정")):
        code, out, err = run(*names)
        assert (code, out) == (EXIT_INVALID, ""), names
        assert err.startswith(f"{kind} 파일이 없다: "), names
    assert not (server_dir / "var" / "models").exists()


def test_FR16_fit_config_option_goes_anywhere(server_dir, run_cli, fit_data, tmp_path):
    config = str(server_dir / "config" / "server.toml")
    work = tmp_path / "work"
    work.mkdir()

    for argv in (["--config", config, *fit_argv("v1", "--dry-run")],
                 [*fit_argv("v1", "--dry-run")[:1], "--config", config, *fit_argv("v1", "--dry-run")[1:]],
                 [*fit_argv("v1", "--dry-run"), "--config", config]):
        code, out, err = run_cli(argv, cwd=work)  # 실행 폴더와 상관없이 같은 설정을 읽는다
        assert (code, err) == (EXIT_OK, ""), argv
        assert out.startswith(ROLE_HEADER)
    assert run_cli(fit_argv("v1", "--dry-run"), cwd=work)[0] == EXIT_INVALID  # 쓰지 않으면 실행 폴더의 config/server.toml


def test_FR16_fit_help_and_usage_errors(run_cli, tmp_path):
    code, out, _ = run_cli(["--help"], cwd=tmp_path)
    assert code == EXIT_OK and "fit" in out

    code, out, _ = run_cli(["fit", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(
        text in out for text in ("--split", "--version", "--fit", "--dry-run", "--config", "common.toml"))
    assert "LAYOUT" not in out and "--calib-version" not in out

    for argv in (["fit"], ["fit", "--split", "s.json"], ["fit", "--version", "v1"],
                 ["fit", "other.json", "--split", "s.json", "--version", "v1"]):
        code, out, err = run_cli(argv, cwd=tmp_path)  # 인자 오류는 argparse가 사용법과 사유를 stderr에 쓰고 2다
        assert (code, out) == (EXIT_INVALID, ""), argv
        assert "nearpilot" in err and "fit" in err, argv


def test_FR16_fit_checks_version_before_reading_anything(server_dir, run_cli):
    # 설정 파일, 분할, DB가 모두 없어도 --version 오류가 먼저다
    code, _, err = run_cli(["fit", "--split", "s.json", "--version", ".", "--config", "missing.toml"], cwd=server_dir)

    assert code == EXIT_INVALID and err.startswith("--version은(는) 경로 구분자가 없는 이름이어야 한다")
