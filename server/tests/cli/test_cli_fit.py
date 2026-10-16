"""`nearpilot fit` (FR-16, NFR-13, specs/core-ops-cli/spec.md 5절·6절 "fit").

실제 SQLite 파일에 `main(argv)`를 같은 프로세스에서 불러 확인한다. DB의 기기, 비콘, 회차, 프레임은
`make_collection_db`가 실제 쓰기 경로로 만들고, 배치·적합 설정·분할 결과 파일은 이 파일이 쓴다.
적합 계산의 세부는 `tests/core/proximity/test_fit.py`가 확인한다. 여기서는 DB의 프레임이 적합까지 이어지는지,
출력 표와 모델 파일, 종료 코드를 확인한다.

데이터: 기기 둘(locker-1, locker-2)이 있는 장소 place-a. 회차는 20초이고 기기마다 1초에 하나씩 RSSI를 받는다.
RSSI는 라벨마다 정한 평균(`LEVELS`) 둘레를 (−1, 0, +1, 0) × 흔들림으로 돌아서 앵커 값을 손으로 구할 수 있다 (`expected_anchor`).
`window_sec`는 .5초로 둔다. 도우미가 만드는 프레임은 수신(`received_at`)이 관측(`frame_ts`)보다 50ms 늦은데,
평가 시각은 관측 시각과 같은 순간이라서 정수 초 구간이면 마지막 칸의 프레임이 모두 빠지고 사후확률이 `stay`에서 멈춘다.
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
from nearpilot.core.proximity.fit import AnchorLayout, PlaceLayout
from nearpilot.core.proximity.model import load_model
from tools.evaluation.split import SplitResult, split_to_json

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)

LAYOUT = PlaceLayout(
    "place-a",
    {"locker-1": AnchorLayout(0.0, 0.0, 0.0, 0.5), "locker-2": AnchorLayout(4.0, 0.0, 4.0, 0.5)},
    none_points=((2.0, 3.0),),
)
# 기기별 (자기 앞, 다른 기기 앞, 어느 기기 앞도 아님) 평균 RSSI(dBm)와 흔들림 폭(dB). none 회차의 흔들림은 두 배다.
# 기기마다 값이 달라서 앵커가 뒤바뀌거나 표의 열이 바뀌면 어긋난다.
LEVELS = {"locker-1": (-44.0, -60.0, -80.0, 1.0), "locker-2": (-46.0, -64.0, -76.0, 2.0)}
JITTER = (-1.0, 0.0, 1.0, 0.0)  # 평균은 그대로 두고 표준편차만 0보다 크게 한다
TRAIN_LABELS = ("locker-1", "locker-1", "locker-2", "locker-2", "none", "none")
VALIDATION_LABELS = ("locker-1", "locker-2", "none")

ANCHOR_HEADER = "기기  A  n  sigma  none_rssi  none_sigma"
GRID_HEADER = "tau  stay  window_sec  손실"
GUIDE = '서버에서 쓰려면 설정의 [proximity.models]에 place-a = "v1"을 넣는다.'


def fit_file(taus: str = "[0.5, 1.0, 2.0]", stays: str = "[0.8, 0.9]", window_secs: str = "[2.5, 3.5]",
             calib_version: str = "") -> str:
    """적합 설정 파일 내용. `calib_version`은 `[fit]`의 한 줄(줄바꿈 포함)이다. 기본은 조합 12개다."""
    return f"""\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
{calib_version}min_distance_m = 0.3
taus = {taus}
stays = {stays}
window_secs = {window_secs}

[acceptance]
max_misselection = 0.05
max_ece = 0.05
"""


def rssis(node_id: str, label: str, seconds: int, *, validation: bool = False) -> list[float]:
    """`node_id`가 `label` 회차에서 1초마다 받는 RSSI `seconds`개.

    검증 회차는 학습 회차와 환경이 달라서 기기 앞 신호는 5dB 약하고, 다른 기기 앞은 3dB, none은 8dB 세고, 흔들림은 두 배다.
    그래서 모델이 정답을 완전히 확신하지 못하고, 조합마다 검증 손실이 다르다.
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


def expected_anchor(node_id: str) -> tuple[float, float, float, float, float]:
    """학습 회차 데이터에서 손으로 구한 앵커 값 (A, n, sigma, none_rssi, none_sigma). s는 그 기기의 흔들림 폭이다.

    기기 앞 표본은 두 거리(자기 앞, 다른 기기 앞)에 40개씩이라 최소제곱 직선은 두 평균을 지난다.
    흔들림 (−1, 0, +1, 0) × a가 5주기(20개)면 평균은 0이고 제곱합은 10a²다.
    그래서 기기 앞 표본의 잔차 제곱합은 회차 4개 × 10 s² = 40 s²이고 자유도는 80 − 2 = 78이다.
    none 표본의 제곱합은 회차 2개 × 10 (2s)² = 80 s²이고 자유도는 40 − 1 = 39다.
    """
    near, far, none, spread = LEVELS[node_id]
    anchor = LAYOUT.nodes[node_id]
    other = next(candidate for other_id, candidate in LAYOUT.nodes.items() if other_id != node_id)
    u_near = -10 * math.log10(math.dist((anchor.x, anchor.y), (anchor.front_x, anchor.front_y)))
    u_far = -10 * math.log10(math.dist((anchor.x, anchor.y), (other.front_x, other.front_y)))
    n = (near - far) / (u_near - u_far)
    a = (near + far) / 2 - n * (u_near + u_far) / 2
    return a, n, spread * math.sqrt(40 / 78), none, spread * math.sqrt(80 / 39)


def anchor_rows() -> list[str]:
    return ["  ".join([node_id, *(f"{value:.2f}" for value in expected_anchor(node_id))]) for node_id in LAYOUT.nodes]


def grid_rows(model_path: Path) -> list[str]:
    """모델 파일에 적힌 조합별 손실에서 기대하는 표 줄. 손실이 작은 순서 상위 10개이고 첫 줄이 고른 조합이다."""
    data = json.loads(model_path.read_text(encoding="utf-8"))
    grid = [{**row, "loss": math.inf if row["loss"] is None else row["loss"]} for row in data["validation"]["grid"]]
    ranked = sorted(grid, key=lambda row: row["loss"])[:10]
    chosen = {"tau": data["calibration"]["tau"], "stay": data["transition"]["stay"], "window_sec": data["window_sec"]}
    assert {key: ranked[0][key] for key in chosen} == chosen  # 첫 줄이 모델이 고른 조합이다
    rows = ["  ".join([f"{row['tau']:g}", f"{row['stay']:g}", f"{row['window_sec']:g}", f"{row['loss']:.4f}"])
            for row in ranked]
    rows[0] += "  ← 선택"
    return rows


def run_spec(index: int, label: str, *, seconds: int = 20, validation: bool = False, looks_like: str | None = None,
             in_progress: bool = False) -> tuple:
    """`make_collection_db`의 회차 한 건. `looks_like`는 라벨과 다른 라벨의 RSSI를 쓴다 (라벨을 잘못 단 회차)."""
    frames = {node_id: rssis(node_id, looks_like or label, seconds, validation=validation) for node_id in LAYOUT.nodes}
    return label, T0 + timedelta(seconds=60 * index), None if in_progress else seconds, frames


def default_runs(*, validation_seconds: int = 20) -> list[tuple]:
    """학습 회차 여섯 건 뒤에 검증 회차 세 건."""
    specs = [(label, 20, False) for label in TRAIN_LABELS]
    specs += [(label, validation_seconds, True) for label in VALIDATION_LABELS]
    return [run_spec(i, label, seconds=seconds, validation=is_validation)
            for i, (label, seconds, is_validation) in enumerate(specs)]


def write_layout(server: Path, name: str = "place-a.json", *, extra: dict | None = None) -> Path:
    data = {
        "place_id": LAYOUT.place_id,
        "nodes": {node_id: {"x": a.x, "y": a.y, "front_x": a.front_x, "front_y": a.front_y}
                  for node_id, a in LAYOUT.nodes.items()},
        "none_points": [list(point) for point in LAYOUT.none_points],
        **(extra or {}),
    }
    path = server / "config" / "layouts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_fit(server: Path, name: str = "place-a.toml", text: str | None = None) -> Path:
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
    나머지는 시험으로 나눈 분할 결과 파일과 배치·적합 설정 파일을 쓴다. 회차 ID를 나눠 돌려준다."""
    ids = make_collection_db(server, LAYOUT, default_runs() if runs is None else runs)
    data = SimpleNamespace(
        train=tuple(ids[:n_train]),
        validation=tuple(ids[n_train:n_train + n_validation]),
        test=tuple(ids[n_train + n_validation:]),
    )
    write_layout(server)
    write_fit(server)
    write_split(server, data.train, data.validation, data.test)
    return data


def fit_argv(version: str = "v1", *extra: str) -> list[str]:
    return ["fit", "place-a.json", "--split", "place-a-seed42.json", "--version", version, *extra]


def model_file(server: Path, version: str = "v1") -> Path:
    return (server / "var" / "models" / "place-a" / f"{version}.json").resolve()


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
    model = load_model(path)  # 서버가 모델을 읽는 코드로 읽힌다
    assert (model.place_id, model.model_version, model.calib_version) == ("place-a", "v1", "v1")
    assert model.none_points == ((2.0, 3.0),)  # 배치 파일의 none_points가 모델로 간다
    assert (list(model.validation["train_runs"]), list(model.validation["validation_runs"])) == (
        list(fit_data.train), list(fit_data.validation))  # 분할 파일의 학습·검증 회차를 읽었다
    assert out == "\n".join([ANCHOR_HEADER, *anchor_rows(), GRID_HEADER, *grid_rows(path), f"작성: {path}", GUIDE]) + "\n"
    assert out.count("← 선택") == 1 and len(grid_rows(path)) == 10  # 조합 12개 가운데 상위 10개
    assert [p.name for p in path.parent.iterdir()] == ["v1.json"] and b"\r\n" not in path.read_bytes()


def test_FR16_fit_fits_the_collected_frames(server_dir, run_cli, fit_data):
    """DB의 프레임이 적합까지 이어진다. 값은 데이터에서 손으로 구한 것과 같다."""
    assert run_cli(fit_argv(), cwd=server_dir)[0] == EXIT_OK

    model = load_model(model_file(server_dir))
    for node_id in LAYOUT.nodes:
        node = model.nodes[node_id]
        assert (node.A, node.n, node.sigma, node.none_rssi, node.none_sigma) == pytest.approx(expected_anchor(node_id))
        assert (node.x, node.y, node.front_x, node.front_y) == (
            LAYOUT.nodes[node_id].x, LAYOUT.nodes[node_id].y, LAYOUT.nodes[node_id].front_x,
            LAYOUT.nodes[node_id].front_y)
    # 두 기기의 다섯 값이 모두 다른 데이터라서, 앵커나 열이 뒤바뀌었다면 위에서 어긋난다
    assert all(a != b for a, b in zip(expected_anchor("locker-1"), expected_anchor("locker-2")))
    assert model.min_distance_m == 0.3 and model.calibration_method == "temperature"


def test_FR16_fit_dry_run_writes_nothing(server_dir, run_cli, fit_data):
    code, out, err = run_cli(fit_argv("v1", "--dry-run"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    lines = out.splitlines()
    assert lines[:3] == [ANCHOR_HEADER, *anchor_rows()] and lines[3] == GRID_HEADER
    assert len(lines) == 3 + 1 + 10 + 1 and lines[-1] == "드라이런: 모델 파일을 쓰지 않았다."
    assert out.count("← 선택") == 1 and "작성:" not in out and "[proximity.models]" not in out
    assert not (server_dir / "var" / "models").exists()  # 파일도 폴더도 만들지 않는다


def test_FR16_fit_dry_run_ignores_existing_version(server_dir, run_cli, fit_data):
    code, real_out, _ = run_cli(fit_argv(), cwd=server_dir)
    assert code == EXIT_OK
    path = model_file(server_dir)
    before = path.read_bytes()

    code, dry_out, err = run_cli(fit_argv("v1", "--dry-run"), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")  # 쓰지 않으니 같은 버전의 파일이 있어도 된다
    assert dry_out == real_out.split("작성:")[0] + "드라이런: 모델 파일을 쓰지 않았다.\n"  # 표는 같다
    assert path.read_bytes() == before


def test_FR16_fit_grid_prints_inf_for_infinite_loss(server_dir, run_cli, make_collection_db):
    # 라벨을 잘못 단 검증 회차(locker-1 라벨에 locker-2 앞의 RSSI)가 있으면 τ가 아주 작은 조합은 정답 확률이 0이 된다
    runs = [*default_runs(), run_spec(9, "locker-1", looks_like="locker-2")]
    data = collect(server_dir, make_collection_db, runs, n_validation=4)
    assert len(data.validation) == 4
    write_fit(server_dir, text=fit_file(taus="[0.01, 0.5, 50.0]", stays="[0.9]"))

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    rows = grid_rows(model_file(server_dir))
    assert len(rows) == 6 and out.splitlines()[4:10] == rows  # 머리 줄 둘과 앵커 줄 둘 뒤에 조합 여섯 줄
    infinite = [row for row in rows if row.endswith("  inf")]
    assert infinite and rows[-len(infinite):] == infinite  # 무한대는 `inf`로 쓰고 맨 뒤에 둔다
    assert all(row.startswith("0.01  ") for row in infinite)  # τ가 아주 작은 조합이다
    assert "inf" not in rows[0] and rows[0].endswith("  ← 선택")


def test_FR16_fit_calib_version_precedence(server_dir, run_cli, fit_data):
    def calib_version(version: str, *extra: str) -> str:
        assert run_cli(fit_argv(version, *extra), cwd=server_dir)[0] == EXIT_OK
        return load_model(model_file(server_dir, version)).calib_version

    write_fit(server_dir, text=fit_file(calib_version='calib_version = "calib-file"\n'))
    assert calib_version("v1", "--calib-version", "calib-cli") == "calib-cli"  # --calib-version이 먼저
    assert calib_version("v2") == "calib-file"  # 없으면 [fit].calib_version
    write_fit(server_dir)
    assert calib_version("v3") == "v3"  # 그것도 없으면 --version
    assert calib_version("v4", "--calib-version", "calib-cli") == "calib-cli"


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
    write_config(server_dir, settings={"version": "t1", "rssi_frame_sec": 0.5})
    seen = []
    real = fit_cli.fit_model

    def spy(*args, **kwargs):
        seen.append(kwargs["bin_sec"])
        return real(*args, **kwargs)

    monkeypatch.setattr(fit_cli, "fit_model", spy)

    code, _, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, err) == (EXIT_OK, "")
    assert seen == [0.5]


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
        other_calib = run_cli(fit_argv("v1", "--calib-version", "other"), cwd=server_dir)  # 파일 이름은 --version이다

    assert (code, out) == (EXIT_FAILED, "")
    assert err == f"모델 파일이 이미 있다: {path}. 다른 --version을 쓴다\n"
    assert other_calib == (EXIT_FAILED, "", err)
    assert path.read_bytes() == before  # 내용이 그대로다

    # 버전을 바꾸면 새 파일이다
    assert run_cli(fit_argv("v2"), cwd=server_dir)[0] == EXIT_OK
    assert sorted(p.name for p in path.parent.iterdir()) == ["v1.json", "v2.json"] and path.read_bytes() == before


def test_FR16_fit_file_created_during_fit_is_not_overwritten(server_dir, run_cli, fit_data, monkeypatch):
    path = model_file(server_dir)
    real = fit_cli.fit_model

    def fit_then_other_process_writes(*args, **kwargs):
        result = real(*args, **kwargs)
        path.parent.mkdir(parents=True)
        path.write_text("다른 곳에서 만든 파일", encoding="utf-8")
        return result

    monkeypatch.setattr(fit_cli, "fit_model", fit_then_other_process_writes)

    code, _, err = run_cli(fit_argv(), cwd=server_dir)

    assert code == EXIT_FAILED
    assert err == f"모델 파일이 이미 있다: {path}. 다른 --version을 쓴다\n"
    assert path.read_text(encoding="utf-8") == "다른 곳에서 만든 파일"


def test_FR16_fit_error_writes_nothing(server_dir, run_cli, make_collection_db):
    # 검증 회차가 가장 긴 window_sec(3.5초)보다 짧아서 평가 시각이 없다
    collect(server_dir, make_collection_db, default_runs(validation_seconds=1))

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err.startswith("적합 실패: 검증 회차 'col-007'") and err.count("\n") == 1
    assert not (server_dir / "var" / "models").exists()  # 파일도 폴더도 만들지 않는다


def test_FR16_fit_missing_label_fails(server_dir, run_cli, make_collection_db):
    # locker-2 라벨의 회차가 분할에서 모두 빠졌다. 적합 전에 거부한다.
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
    for version in ("../evil", "../../evil", "a/b", "a\\b", ".."):
        code, out, err = run_cli(fit_argv(version), cwd=server_dir)

        assert (code, out) == (EXIT_FAILED, ""), version
        assert err.startswith("적합 실패: model_version: "), version
    # 모델 폴더도, 폴더 밖으로 나간 파일도 만들지 않는다
    assert not (server_dir / "var" / "models").exists() and not (server_dir / "var" / "evil.json").exists()


def test_FR16_fit_empty_calib_version_is_rejected(server_dir, run_cli, fit_data):
    write_fit(server_dir, text=fit_file(calib_version='calib_version = "calib-file"\n'))

    code, out, err = run_cli(fit_argv("v1", "--calib-version", ""), cwd=server_dir)

    # 준 값은 비어 있어도 "있는" 값이다. [fit].calib_version으로 넘어가지 않고 적합이 거부한다.
    assert (code, out) == (EXIT_FAILED, "")
    assert err.startswith("적합 실패: calib_version: ")
    assert not (server_dir / "var" / "models").exists()


def test_FR16_fit_unreadable_runs_fail(server_dir, run_cli, make_collection_db):
    runs = [*default_runs(), run_spec(9, "locker-1", in_progress=True)]
    data = collect(server_dir, make_collection_db, runs, n_validation=4)  # 검증 회차의 마지막이 진행 중이다
    assert data.validation[-1] == "col-010"

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_FAILED, "")
    assert err == "수집 회차를 읽을 수 없다: 진행 중인 회차 col-010\n"
    write_split(server_dir, (*data.train[:-1], "col-998"), data.validation[:-1], name="ghost.json")  # 학습 회차 하나가 DB에 없다
    code, out, err = run_cli(["fit", "place-a.json", "--split", "ghost.json", "--version", "v1"], cwd=server_dir)
    assert (code, out) == (EXIT_FAILED, "")
    assert err == "수집 회차를 읽을 수 없다: DB에 없는 회차 col-998\n"
    assert not (server_dir / "var" / "models").exists()


# ── 입력 오류 (종료 코드 2) ───────────────────────────────────────────


def test_FR16_fit_place_mismatch_rejected(server_dir, run_cli, fit_data):
    write_split(server_dir, fit_data.train, fit_data.validation, place_id="place-b")

    code, out, err = run_cli(fit_argv(), cwd=server_dir)

    assert (code, out) == (EXIT_INVALID, "")
    assert err == "분할 결과의 장소(place-b)와 배치의 장소(place-a)가 다르다\n"
    assert not (server_dir / "var" / "models").exists()


def test_FR16_fit_checks_input_files_in_order(server_dir, run_cli):
    layouts = (server_dir / "config" / "layouts").resolve()
    splits = (server_dir / "var" / "splits").resolve()
    fit_dir = (server_dir / "config" / "fit").resolve()
    db_file = (server_dir / "var" / "nearpilot.db").resolve()

    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"배치 파일이 없다: {layouts / 'place-a.json'}. {layouts}에 있는 파일: (없음)\n"

    write_layout(server_dir)
    code, _, err = run_cli(fit_argv(), cwd=server_dir)
    assert code == EXIT_INVALID
    assert err == f"분할 결과 파일이 없다: {splits / 'place-a-seed42.json'}. {splits}에 있는 파일: (없음)\n"

    write_split(server_dir, ["col-001"], ["col-002"])
    code, _, err = run_cli(fit_argv(), cwd=server_dir)
    assert code == EXIT_INVALID
    assert err == f"적합 설정 파일이 없다: {fit_dir / 'place-a.toml'}. {fit_dir}에 있는 파일: (없음)\n"

    write_fit(server_dir, "place-b.toml")  # 다른 장소의 설정 파일은 이 장소의 파일이 아니다
    code, _, err = run_cli(fit_argv(), cwd=server_dir)
    assert code == EXIT_INVALID and err.endswith(f"{fit_dir}에 있는 파일: place-b.toml\n")

    write_fit(server_dir)  # 파일 인자를 모두 풀고서야 DB를 연다
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == f"DB 파일이 없다: {db_file}. 먼저 nearpilot serve --collect-only로 서버를 한 번 시작한다\n"
    assert not db_file.exists() and not (server_dir / "var" / "models").exists()  # 빈 DB도 모델 폴더도 만들지 않는다


def test_FR16_fit_rejects_invalid_files(server_dir, run_cli):
    write_layout(server_dir, extra={"colour": "red"})
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == "배치 파일 오류: colour: 모르는 필드다\n"

    write_layout(server_dir)
    split_file = write_split(server_dir, ["col-001"], ["col-002"])
    split_file.write_text("JSON이 아니다", encoding="utf-8")
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err.startswith(f"분할 결과 파일 오류: {split_file.resolve()}: UTF-8 JSON으로 읽을 수 없다 (")

    write_split(server_dir, ["col-001"], ["col-002"])
    write_fit(server_dir, text=fit_file().replace("min_distance_m = 0.3\n", ""))
    code, out, err = run_cli(fit_argv(), cwd=server_dir)
    assert (code, out) == (EXIT_INVALID, "")
    assert err == "적합 설정 파일 오류: fit.min_distance_m: 필드가 없다\n"
    assert not (server_dir / "var" / "models").exists()


def test_FR16_fit_unwritable_model_folder_is_input_error(server_dir, run_cli, fit_data):
    models = (server_dir / "var" / "models").resolve()
    models.write_text("폴더 자리에 파일이 있다", encoding="utf-8")

    code, _, err = run_cli(fit_argv(), cwd=server_dir)

    assert code == EXIT_INVALID
    assert err.startswith(f"모델 파일을 쓸 수 없다: {models / 'place-a' / 'v1.json'} (")
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
    (work / "mine.json").write_bytes(write_layout(server_dir).read_bytes())
    (work / "mine-split.json").write_bytes(write_split(server_dir, fit_data.train, fit_data.validation).read_bytes())
    (work / "mine.toml").write_text(fit_file(taus="[1.0]", stays="[0.9]", window_secs="[2.5]"), encoding="utf-8")

    def run(layout: str, split: str, fit: str) -> tuple[int, str, str]:
        return run_cli(["fit", layout, "--split", split, "--fit", fit, "--version", "v1", "--config", config,
                        "--dry-run"], cwd=work)

    code, out, err = run("./mine.json", "./mine-split.json", "./mine.toml")
    assert (code, err) == (EXIT_OK, "")
    assert len(out.splitlines()) == 1 + 2 + 1 + 1 + 1  # 사본 적합 설정의 조합 하나만 쓴다 (기본 폴더의 파일은 열두 개다)
    for names, kind in ((("mine.json", "./mine-split.json", "./mine.toml"), "배치"),
                        (("./mine.json", "mine-split.json", "./mine.toml"), "분할 결과"),
                        (("./mine.json", "./mine-split.json", "mine.toml"), "적합 설정")):
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
        assert out.startswith(ANCHOR_HEADER)
    assert run_cli(fit_argv("v1", "--dry-run"), cwd=work)[0] == EXIT_INVALID  # 쓰지 않으면 실행 폴더의 config/server.toml


def test_FR16_fit_help_and_usage_errors(run_cli, tmp_path):
    code, out, _ = run_cli(["--help"], cwd=tmp_path)
    assert code == EXIT_OK and "fit" in out

    code, out, _ = run_cli(["fit", "--help"], cwd=tmp_path)
    assert code == EXIT_OK and all(
        text in out for text in ("LAYOUT", "--split", "--version", "--fit", "--calib-version", "--dry-run", "--config"))

    for argv in (["fit"], ["fit", "place-a.json"], ["fit", "place-a.json", "--split", "s.json"],
                 ["fit", "place-a.json", "--version", "v1"],
                 ["fit", "place-a.json", "other.json", "--split", "s.json", "--version", "v1"]):
        code, out, err = run_cli(argv, cwd=tmp_path)  # 인자 오류는 argparse가 사용법과 사유를 stderr에 쓰고 2다
        assert (code, out) == (EXIT_INVALID, ""), argv
        assert "nearpilot" in err and "fit" in err, argv
