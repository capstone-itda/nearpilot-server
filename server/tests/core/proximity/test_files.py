"""배치 파일(JSON)과 적합 설정 파일(TOML) 읽기 (FR-16, specs/core-ops-cli/spec.md 6절 "split", "fit", "eval").

오류는 잘못된 필드 경로를 모두 모아 "; "로 이은 메시지 하나로 낸다. 단일 문제 사례는 메시지 전체를 비교해서
검사가 다른 문제를 덩달아 내지 않는지도 본다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nearpilot.core.proximity.fit import AnchorLayout, PlaceLayout
from nearpilot.core.proximity.files import (
    AcceptanceSettings,
    FitFile,
    FitFileError,
    FitSettings,
    LayoutFileError,
    SplitSettings,
    load_fit_file,
    load_layout,
)

LAYOUT = {
    "place_id": "place-a",
    "nodes": {
        "locker-1": {"x": 0.6, "y": 4.0, "front_x": 0.6, "front_y": 3.4},
        "locker-2": {"x": 3, "y": 4, "front_x": 3, "front_y": 3},  # 정수도 받는다
    },
    "none_points": [[2.4, 1.0], [5, 2]],
}
NODE = LAYOUT["nodes"]["locker-1"]

FIT_FILE = """\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
calib_version = "c1"
min_distance_m = 0.3
taus = [1.0, 1.5, 2.0]
stays = [0.8, 0.9, 0.95]
window_secs = [2.0, 3.0]

[acceptance]
max_misselection = 0.05
max_ece = 0.05
"""


def write_json(tmp_path: Path, data: object, name: str = "layout.json") -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def write_toml(tmp_path: Path, text: str, name: str = "fit.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def without(data: dict, key: str) -> dict:
    return {k: v for k, v in data.items() if k != key}


# ── 배치 파일 ─────────────────────────────────────────────────────────


def test_FR16_layout_file_reads_layout(tmp_path):
    layout = load_layout(write_json(tmp_path, LAYOUT))

    assert layout == PlaceLayout(
        "place-a",
        {"locker-1": AnchorLayout(0.6, 4.0, 0.6, 3.4), "locker-2": AnchorLayout(3.0, 4.0, 3.0, 3.0)},
        ((2.4, 1.0), (5.0, 2.0)),
    )
    assert list(layout.nodes) == ["locker-1", "locker-2"]  # 파일의 순서를 지킨다
    assert type(layout.nodes["locker-2"].x) is float  # 정수는 float로 읽는다
    assert all(type(v) is float for point in layout.none_points for v in point)


def test_FR16_layout_file_none_points_are_optional(tmp_path):
    assert load_layout(write_json(tmp_path, without(LAYOUT, "none_points"))).none_points == ()
    assert load_layout(write_json(tmp_path, {**LAYOUT, "none_points": []})).none_points == ()


def test_FR16_layout_file_reads_korean_text(tmp_path):
    # 파일은 UTF-8이다. 기기 ID와 장소 ID에 한글이 있어도 읽는다 (한국어 Windows 기본 인코딩과 무관하다).
    data = {"place_id": "연구실", "nodes": {"보관함-1": dict(NODE)}}
    layout = load_layout(write_json(tmp_path, data))
    assert layout.place_id == "연구실" and list(layout.nodes) == ["보관함-1"]


BAD_LAYOUTS = {
    "unknown_top_key": ({**LAYOUT, "extra": 1}, ["extra: 모르는 필드다"]),
    "missing_place_id": (without(LAYOUT, "place_id"), ["place_id: 필드가 없다"]),
    "missing_nodes": (without(LAYOUT, "nodes"), ["nodes: 필드가 없다"]),
    "place_id_empty": ({**LAYOUT, "place_id": ""}, ["place_id: 비어 있지 않은 문자열이어야 한다 ('')"]),
    "place_id_blank": ({**LAYOUT, "place_id": "  "}, ["place_id: 비어 있지 않은 문자열이어야 한다 ('  ')"]),
    "place_id_number": ({**LAYOUT, "place_id": 7}, ["place_id: 비어 있지 않은 문자열이어야 한다 (7)"]),
    "nodes_empty": ({**LAYOUT, "nodes": {}}, ["nodes: 비어 있지 않은 JSON 객체여야 한다 ({})"]),
    "nodes_list": ({**LAYOUT, "nodes": [NODE]}, [f"nodes: 비어 있지 않은 JSON 객체여야 한다 ({[NODE]!r:.40})"]),
    "node_id_empty": ({**LAYOUT, "nodes": {"": NODE}}, ["nodes: 기기 ID는 비어 있지 않은 문자열이어야 한다 ('')"]),
    "node_not_object": (
        {**LAYOUT, "nodes": {"locker-1": [1, 2, 3, 4]}}, ["nodes.locker-1: JSON 객체여야 한다 ([1, 2, 3, 4])"]),
    "node_unknown_key": (
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "z": 1}}}, ["nodes.locker-1.z: 모르는 필드다"]),
    "node_missing_key": (
        {**LAYOUT, "nodes": {"locker-1": without(NODE, "front_y")}}, ["nodes.locker-1.front_y: 필드가 없다"]),
    "bool_coordinate": (
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "x": True}}}, ["nodes.locker-1.x: 숫자여야 한다 (True)"]),
    "false_coordinate": (
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "front_y": False}}},
        ["nodes.locker-1.front_y: 숫자여야 한다 (False)"]),
    "string_coordinate": (
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "y": "4.0"}}}, ["nodes.locker-1.y: 숫자여야 한다 ('4.0')"]),
    "null_coordinate": (
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "y": None}}}, ["nodes.locker-1.y: 숫자여야 한다 (None)"]),
    "nan_coordinate": (
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "x": float("nan")}}},
        ["nodes.locker-1.x: 유한한 숫자여야 한다 (nan)"]),
    "infinite_coordinate": (
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "front_x": float("-inf")}}},
        ["nodes.locker-1.front_x: 유한한 숫자여야 한다 (-inf)"]),
    "huge_int_coordinate": (  # float로 바꿀 수 없는 정수. 메시지는 값을 잘라서 쓴다
        {**LAYOUT, "nodes": {"locker-1": {**NODE, "x": 10**400}}},
        [f"nodes.locker-1.x: 유한한 숫자여야 한다 ({repr(10**400)[:40]})"]),
    "none_points_not_list": ({**LAYOUT, "none_points": "a"}, ["none_points: JSON 배열이어야 한다 ('a')"]),
    "none_point_too_short": (
        {**LAYOUT, "none_points": [[1]]}, ["none_points[0]: [x, y] 숫자 두 개여야 한다 ([1])"]),
    "none_point_too_long": (
        {**LAYOUT, "none_points": [[1, 2, 3]]}, ["none_points[0]: [x, y] 숫자 두 개여야 한다 ([1, 2, 3])"]),
    "none_point_not_list": (
        {**LAYOUT, "none_points": [[1, 2], {"x": 1}]}, ["none_points[1]: [x, y] 숫자 두 개여야 한다 ({'x': 1})"]),
    "none_point_bool": (
        {**LAYOUT, "none_points": [[True, 1]]}, ["none_points[0][0]: 숫자여야 한다 (True)"]),
    "none_point_nan": (
        {**LAYOUT, "none_points": [[1, float("nan")]]}, ["none_points[0][1]: 유한한 숫자여야 한다 (nan)"]),
}


@pytest.mark.parametrize("data, problems", list(BAD_LAYOUTS.values()), ids=list(BAD_LAYOUTS))
def test_FR16_layout_file_rejects_bad_fields(tmp_path, data, problems):
    with pytest.raises(LayoutFileError) as error:
        load_layout(write_json(tmp_path, data))
    assert str(error.value) == "; ".join(problems)


def test_FR16_layout_file_reports_every_problem(tmp_path):
    data = {
        "place_id": "",
        "nodes": {"locker-1": {"x": True, "y": 1, "front_x": 1, "extra": 2}, "locker-2": 5},
        "none_points": [[1], "a"],
        "typo": 0,
    }
    with pytest.raises(LayoutFileError) as error:
        load_layout(write_json(tmp_path, data))

    assert str(error.value).split("; ") == [
        "typo: 모르는 필드다",
        "place_id: 비어 있지 않은 문자열이어야 한다 ('')",
        "nodes.locker-1.front_y: 필드가 없다",
        "nodes.locker-1.extra: 모르는 필드다",
        "nodes.locker-1.x: 숫자여야 한다 (True)",
        "nodes.locker-2: JSON 객체여야 한다 (5)",
        "none_points[0]: [x, y] 숫자 두 개여야 한다 ([1])",
        "none_points[1]: [x, y] 숫자 두 개여야 한다 ('a')",
    ]


def test_FR16_layout_file_unreadable_or_not_json(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(LayoutFileError) as error:
        load_layout(missing)
    assert str(error.value).startswith(f"{missing}: 읽을 수 없다 (")

    broken = write_json(tmp_path, "{not json", "broken.json")
    with pytest.raises(LayoutFileError) as error:
        load_layout(broken)
    assert str(error.value).startswith(f"{broken}: UTF-8 JSON으로 읽을 수 없다 (")

    not_utf8 = tmp_path / "cp949.json"
    not_utf8.write_bytes('{"place_id": "연구실"}'.encode("cp949"))
    with pytest.raises(LayoutFileError) as error:
        load_layout(not_utf8)
    assert str(error.value).startswith(f"{not_utf8}: UTF-8 JSON으로 읽을 수 없다 (")

    for text in ("[1, 2]", '"place-a"', "null"):
        top = write_json(tmp_path, text, "top.json")
        with pytest.raises(LayoutFileError) as error:
            load_layout(top)
        assert str(error.value) == f"{top}: 최상위가 JSON 객체여야 한다"


# ── 적합 설정 파일 ────────────────────────────────────────────────────


def test_FR16_fit_file_reads_settings(tmp_path):
    fit_file = load_fit_file(write_toml(tmp_path, FIT_FILE))

    assert fit_file == FitFile(
        split=SplitSettings(ratios=(0.6, 0.2, 0.2), min_runs_per_label=5),
        fit=FitSettings(calib_version="c1", min_distance_m=0.3, taus=(1.0, 1.5, 2.0),
                        stays=(0.8, 0.9, 0.95), window_secs=(2.0, 3.0)),
        acceptance=AcceptanceSettings(max_misselection=0.05, max_ece=0.05),
    )
    # 목록은 tuple이다. 파일을 읽은 뒤 바꿀 수 없다.
    assert all(type(v) is tuple for v in (
        fit_file.split.ratios, fit_file.fit.taus, fit_file.fit.stays, fit_file.fit.window_secs))
    with pytest.raises(AttributeError):
        fit_file.split.min_runs_per_label = 9  # type: ignore[misc]


def test_FR16_fit_file_calib_version_is_optional(tmp_path):
    text = FIT_FILE.replace('calib_version = "c1"\n', "")
    assert "calib_version" not in text
    assert load_fit_file(write_toml(tmp_path, text)).fit.calib_version is None


def test_FR16_fit_file_accepts_integers_and_boundaries(tmp_path):
    text = (FIT_FILE.replace("min_distance_m = 0.3", "min_distance_m = 1")
            .replace("taus = [1.0, 1.5, 2.0]", "taus = [1, 2]")
            .replace("window_secs = [2.0, 3.0]", "window_secs = [2]")
            .replace("max_misselection = 0.05", "max_misselection = 0")
            .replace("max_ece = 0.05", "max_ece = 1"))
    fit_file = load_fit_file(write_toml(tmp_path, text))

    assert fit_file.fit.min_distance_m == 1.0 and type(fit_file.fit.min_distance_m) is float
    assert fit_file.fit.taus == (1.0, 2.0) and all(type(v) is float for v in fit_file.fit.taus)
    assert fit_file.fit.window_secs == (2.0,)
    assert (fit_file.acceptance.max_misselection, fit_file.acceptance.max_ece) == (0.0, 1.0)


def test_FR16_fit_file_min_runs_boundary(tmp_path):
    # 학습·검증·시험에 한 회차씩은 들어가야 하므로 3이 가장 작은 값이다
    three = FIT_FILE.replace("min_runs_per_label = 5", "min_runs_per_label = 3")
    assert load_fit_file(write_toml(tmp_path, three)).split.min_runs_per_label == 3


def test_FR16_fit_file_ratio_sum_tolerance(tmp_path):
    # 합이 1에서 1e-9 이내로 벗어나는 것까지 받는다
    near = FIT_FILE.replace("ratios = [0.6, 0.2, 0.2]", "ratios = [0.6, 0.2, 0.2000000005]")
    assert load_fit_file(write_toml(tmp_path, near)).split.ratios == (0.6, 0.2, 0.2000000005)

    # 1e-9를 조금만 넘어도 거부한다 (2e-9, 1e-7, 2e-3)
    for third in ("0.200000002", "0.2000001", "0.202"):
        far = FIT_FILE.replace("ratios = [0.6, 0.2, 0.2]", f"ratios = [0.6, 0.2, {third}]")
        with pytest.raises(FitFileError, match=r"split\.ratios: 합이 1이어야 한다 \(합 1\.00\d+\)"):
            load_fit_file(write_toml(tmp_path, far))


# (고칠 부분, 바꾼 값, 기대하는 문제 목록). 문제 하나만 나와야 한다.
BAD_FIT_FILES = {
    "ratios_sum_not_one": ("ratios = [0.6, 0.2, 0.2]", "ratios = [0.5, 0.25, 0.5]",
                           ["split.ratios: 합이 1이어야 한다 (합 1.25)"]),
    "ratios_two_values": ("ratios = [0.6, 0.2, 0.2]", "ratios = [0.6, 0.4]",
                          ["split.ratios: 양수 3개의 목록이어야 한다 ([0.6, 0.4])"]),
    "ratios_four_values": ("ratios = [0.6, 0.2, 0.2]", "ratios = [0.4, 0.2, 0.2, 0.2]",
                           ["split.ratios: 양수 3개의 목록이어야 한다 ([0.4, 0.2, 0.2, 0.2])"]),
    "ratios_not_list": ("ratios = [0.6, 0.2, 0.2]", "ratios = 1", ["split.ratios: 양수 3개의 목록이어야 한다 (1)"]),
    "ratios_zero": ("ratios = [0.6, 0.2, 0.2]", "ratios = [1.0, 0.0, 0.0]",
                    ["split.ratios[1]: 0보다 커야 한다 (0.0)", "split.ratios[2]: 0보다 커야 한다 (0.0)"]),
    "ratios_negative": ("ratios = [0.6, 0.2, 0.2]", "ratios = [1.2, -0.1, -0.1]",
                        ["split.ratios[1]: 0보다 커야 한다 (-0.1)", "split.ratios[2]: 0보다 커야 한다 (-0.1)"]),
    "ratios_bool": ("ratios = [0.6, 0.2, 0.2]", "ratios = [0.6, 0.2, true]",
                    ["split.ratios[2]: 숫자여야 한다 (True)"]),
    "ratios_nan": ("ratios = [0.6, 0.2, 0.2]", "ratios = [0.6, 0.2, nan]",
                   ["split.ratios[2]: 유한한 숫자여야 한다 (nan)"]),
    "min_runs_two": ("min_runs_per_label = 5", "min_runs_per_label = 2",
                     ["split.min_runs_per_label: 3 이상의 정수여야 한다 (2)"]),
    "min_runs_bool": ("min_runs_per_label = 5", "min_runs_per_label = true",
                      ["split.min_runs_per_label: 3 이상의 정수여야 한다 (True)"]),
    "min_runs_float": ("min_runs_per_label = 5", "min_runs_per_label = 5.0",
                       ["split.min_runs_per_label: 3 이상의 정수여야 한다 (5.0)"]),
    "min_runs_string": ("min_runs_per_label = 5", 'min_runs_per_label = "5"',
                        ["split.min_runs_per_label: 3 이상의 정수여야 한다 ('5')"]),
    "stays_one": ("stays = [0.8, 0.9, 0.95]", "stays = [0.8, 1.0]",
                  ["fit.stays[1]: 0보다 크고 1보다 작아야 한다 (1.0)"]),
    "stays_zero": ("stays = [0.8, 0.9, 0.95]", "stays = [0]", ["fit.stays[0]: 0보다 크고 1보다 작아야 한다 (0)"]),
    "stays_above_one": ("stays = [0.8, 0.9, 0.95]", "stays = [1.5]",
                        ["fit.stays[0]: 0보다 크고 1보다 작아야 한다 (1.5)"]),
    "stays_empty": ("stays = [0.8, 0.9, 0.95]", "stays = []", ["fit.stays: 비어 있지 않은 목록이어야 한다 ([])"]),
    "taus_empty": ("taus = [1.0, 1.5, 2.0]", "taus = []", ["fit.taus: 비어 있지 않은 목록이어야 한다 ([])"]),
    "taus_not_list": ("taus = [1.0, 1.5, 2.0]", "taus = 1.0", ["fit.taus: 비어 있지 않은 목록이어야 한다 (1.0)"]),
    "taus_non_positive": ("taus = [1.0, 1.5, 2.0]", "taus = [1.0, -1.5, 0]",
                          ["fit.taus[1]: 0보다 커야 한다 (-1.5)", "fit.taus[2]: 0보다 커야 한다 (0)"]),
    "taus_bool": ("taus = [1.0, 1.5, 2.0]", "taus = [1.0, true]", ["fit.taus[1]: 숫자여야 한다 (True)"]),
    "window_secs_empty": ("window_secs = [2.0, 3.0]", "window_secs = []",
                          ["fit.window_secs: 비어 있지 않은 목록이어야 한다 ([])"]),
    "window_secs_zero": ("window_secs = [2.0, 3.0]", "window_secs = [0.0]", ["fit.window_secs[0]: 0보다 커야 한다 (0.0)"]),
    "min_distance_zero": ("min_distance_m = 0.3", "min_distance_m = 0", ["fit.min_distance_m: 0보다 커야 한다 (0)"]),
    "min_distance_negative": ("min_distance_m = 0.3", "min_distance_m = -0.3",
                              ["fit.min_distance_m: 0보다 커야 한다 (-0.3)"]),
    "min_distance_inf": ("min_distance_m = 0.3", "min_distance_m = inf",
                         ["fit.min_distance_m: 유한한 숫자여야 한다 (inf)"]),
    "min_distance_bool": ("min_distance_m = 0.3", "min_distance_m = true",
                          ["fit.min_distance_m: 숫자여야 한다 (True)"]),
    "min_distance_string": ("min_distance_m = 0.3", 'min_distance_m = "0.3"',
                            ["fit.min_distance_m: 숫자여야 한다 ('0.3')"]),
    "calib_version_empty": ('calib_version = "c1"', 'calib_version = ""',
                            ["fit.calib_version: 비어 있지 않은 문자열이어야 한다 ('')"]),
    "calib_version_number": ('calib_version = "c1"', "calib_version = 1",
                             ["fit.calib_version: 비어 있지 않은 문자열이어야 한다 (1)"]),
    "misselection_negative": ("max_misselection = 0.05", "max_misselection = -0.1",
                              ["acceptance.max_misselection: 0 이상 1 이하여야 한다 (-0.1)"]),
    "misselection_above_one": ("max_misselection = 0.05", "max_misselection = 1.01",
                               ["acceptance.max_misselection: 0 이상 1 이하여야 한다 (1.01)"]),
    "ece_above_one": ("max_ece = 0.05", "max_ece = 2", ["acceptance.max_ece: 0 이상 1 이하여야 한다 (2)"]),
    "ece_nan": ("max_ece = 0.05", "max_ece = nan", ["acceptance.max_ece: 유한한 숫자여야 한다 (nan)"]),
    "ece_bool": ("max_ece = 0.05", "max_ece = false", ["acceptance.max_ece: 숫자여야 한다 (False)"]),
    # 모르는 키와 빠진 키
    "unknown_section": ("[acceptance]", "[extra]\nvalue = 1\n\n[acceptance]",
                        ["extra: 모르는 필드다"]),
    "unknown_split_key": ("min_runs_per_label = 5", "min_runs_per_label = 5\nseed = 3",
                          ["split.seed: 모르는 필드다"]),
    "unknown_fit_key": ("window_secs = [2.0, 3.0]", "window_secs = [2.0, 3.0]\nfast = true",
                        ["fit.fast: 모르는 필드다"]),
    "unknown_acceptance_key": ("max_ece = 0.05", "max_ece = 0.05\nmax_ece_2 = 0.1",
                               ["acceptance.max_ece_2: 모르는 필드다"]),
    "misspelled_key": ("max_ece = 0.05", "max_ec = 0.05",
                       ["acceptance.max_ece: 필드가 없다", "acceptance.max_ec: 모르는 필드다"]),
    "missing_ratios": ("ratios = [0.6, 0.2, 0.2]\n", "", ["split.ratios: 필드가 없다"]),
    "missing_min_runs": ("min_runs_per_label = 5\n", "", ["split.min_runs_per_label: 필드가 없다"]),
    "missing_min_distance": ("min_distance_m = 0.3\n", "", ["fit.min_distance_m: 필드가 없다"]),
    "missing_taus": ("taus = [1.0, 1.5, 2.0]\n", "", ["fit.taus: 필드가 없다"]),
    "missing_stays": ("stays = [0.8, 0.9, 0.95]\n", "", ["fit.stays: 필드가 없다"]),
    "missing_window_secs": ("window_secs = [2.0, 3.0]\n", "", ["fit.window_secs: 필드가 없다"]),
    "missing_max_misselection": ("max_misselection = 0.05\n", "", ["acceptance.max_misselection: 필드가 없다"]),
    "missing_max_ece": ("max_ece = 0.05\n", "", ["acceptance.max_ece: 필드가 없다"]),
}


@pytest.mark.parametrize("old, new, problems", list(BAD_FIT_FILES.values()), ids=list(BAD_FIT_FILES))
def test_FR16_fit_file_rejects_bad_fields(tmp_path, old, new, problems):
    assert old in FIT_FILE  # 고칠 부분이 없으면 이 사례는 아무것도 검사하지 못한다
    with pytest.raises(FitFileError) as error:
        load_fit_file(write_toml(tmp_path, FIT_FILE.replace(old, new)))
    assert str(error.value) == "; ".join(problems)


def test_FR16_fit_file_requires_all_three_sections(tmp_path):
    sections = FIT_FILE.split("\n\n")
    assert len(sections) == 3
    for index, name in enumerate(("split", "fit", "acceptance")):
        text = "\n\n".join(section for i, section in enumerate(sections) if i != index) + "\n"
        with pytest.raises(FitFileError) as error:
            load_fit_file(write_toml(tmp_path, text.replace("\n\n\n", "\n\n")))
        assert str(error.value) == f"{name}: 필드가 없다"
    with pytest.raises(FitFileError) as error:
        load_fit_file(write_toml(tmp_path, ""))
    assert str(error.value) == "split: 필드가 없다; fit: 필드가 없다; acceptance: 필드가 없다"


def test_FR16_fit_file_section_must_be_a_table(tmp_path):
    text = "split = 1\n\n" + FIT_FILE.split("\n\n", 1)[1]
    with pytest.raises(FitFileError) as error:
        load_fit_file(write_toml(tmp_path, text))
    assert str(error.value) == "split: 표여야 한다 (1)"


def test_FR16_fit_file_reports_every_problem(tmp_path):
    text = """\
[split]
ratios = [0.5, 0.5]
min_runs_per_label = 2
unknown = 1

[fit]
min_distance_m = 0
taus = []
stays = [1.0]
window_secs = [true]

[acceptance]
max_misselection = 2
"""
    with pytest.raises(FitFileError) as error:
        load_fit_file(write_toml(tmp_path, text))

    assert str(error.value).split("; ") == [
        "split.unknown: 모르는 필드다",
        "split.ratios: 양수 3개의 목록이어야 한다 ([0.5, 0.5])",
        "split.min_runs_per_label: 3 이상의 정수여야 한다 (2)",
        "fit.min_distance_m: 0보다 커야 한다 (0)",
        "fit.taus: 비어 있지 않은 목록이어야 한다 ([])",
        "fit.stays[0]: 0보다 크고 1보다 작아야 한다 (1.0)",
        "fit.window_secs[0]: 숫자여야 한다 (True)",
        "acceptance.max_ece: 필드가 없다",
        "acceptance.max_misselection: 0 이상 1 이하여야 한다 (2)",
    ]


def test_FR16_fit_file_unreadable_or_not_toml(tmp_path):
    missing = tmp_path / "missing.toml"
    with pytest.raises(FitFileError) as error:
        load_fit_file(missing)
    assert str(error.value).startswith(f"{missing}: 읽을 수 없다 (")

    broken = write_toml(tmp_path, "[split\nratios = ", "broken.toml")
    with pytest.raises(FitFileError) as error:
        load_fit_file(broken)
    assert str(error.value).startswith(f"{broken}: UTF-8 TOML로 읽을 수 없다 (")

    not_utf8 = tmp_path / "cp949.toml"
    not_utf8.write_bytes('# 적합 설정\n[split]\n'.encode("cp949"))
    with pytest.raises(FitFileError) as error:
        load_fit_file(not_utf8)
    assert str(error.value).startswith(f"{not_utf8}: UTF-8 TOML로 읽을 수 없다 (")


def test_FR16_fit_file_reads_korean_comments(tmp_path):
    # 사람이 주석을 달아 고치는 파일이다. 한글 주석이 있어도 UTF-8로 읽는다.
    text = "# 적합 설정 — 분할 비율\n" + FIT_FILE.replace("[fit]", "# 후보 값\n[fit]")
    assert load_fit_file(write_toml(tmp_path, text)).split.min_runs_per_label == 5
