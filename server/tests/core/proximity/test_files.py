"""적합 설정 파일(TOML) 읽기 (FR-16, specs/core-ops-cli/spec.md 6절 "split", "fit", "eval",
specs/core-proximity-common-model/spec.md 6절 결정 13).

오류는 잘못된 필드 경로를 모두 모아 "; "로 이은 메시지 하나로 낸다. 단일 문제 사례는 메시지 전체를 비교해서
검사가 다른 문제를 덩달아 내지 않는지도 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nearpilot.core.proximity.files import (
    AcceptanceSettings,
    FitFile,
    FitFileError,
    FitSettings,
    SplitSettings,
    load_fit_file,
)

FIT_FILE = """\
[split]
ratios = [0.6, 0.2, 0.2]
min_runs_per_label = 5

[fit]
taus = [1.0, 1.5, 2.0]
stays = [0.8, 0.9, 0.95]
window_secs = [2.0, 3.0]
auto_zero_min_bins = 120

[acceptance]
max_misselection = 0.05
max_ece = 0.05
"""


def write_toml(tmp_path: Path, text: str, name: str = "fit.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ── 적합 설정 파일 ────────────────────────────────────────────────────


def test_FR16_fit_file_reads_settings(tmp_path):
    fit_file = load_fit_file(write_toml(tmp_path, FIT_FILE))

    assert fit_file == FitFile(
        split=SplitSettings(ratios=(0.6, 0.2, 0.2), min_runs_per_label=5),
        fit=FitSettings(taus=(1.0, 1.5, 2.0), stays=(0.8, 0.9, 0.95), window_secs=(2.0, 3.0),
                        auto_zero_min_bins=120),
        acceptance=AcceptanceSettings(max_misselection=0.05, max_ece=0.05),
    )
    # 목록은 tuple이다. 파일을 읽은 뒤 바꿀 수 없다.
    assert all(type(v) is tuple for v in (
        fit_file.split.ratios, fit_file.fit.taus, fit_file.fit.stays, fit_file.fit.window_secs))
    assert type(fit_file.fit.auto_zero_min_bins) is int
    with pytest.raises(AttributeError):
        fit_file.split.min_runs_per_label = 9  # type: ignore[misc]


def test_FR16_fit_file_accepts_integers_and_boundaries(tmp_path):
    text = (FIT_FILE.replace("auto_zero_min_bins = 120", "auto_zero_min_bins = 1")
            .replace("taus = [1.0, 1.5, 2.0]", "taus = [1, 2]")
            .replace("window_secs = [2.0, 3.0]", "window_secs = [2]")
            .replace("max_misselection = 0.05", "max_misselection = 0")
            .replace("max_ece = 0.05", "max_ece = 1"))
    fit_file = load_fit_file(write_toml(tmp_path, text))

    assert fit_file.fit.auto_zero_min_bins == 1  # 가장 작은 값이다
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
    "auto_zero_min_bins_zero": ("auto_zero_min_bins = 120", "auto_zero_min_bins = 0",
                                ["fit.auto_zero_min_bins: 1 이상의 정수여야 한다 (0)"]),
    "auto_zero_min_bins_negative": ("auto_zero_min_bins = 120", "auto_zero_min_bins = -5",
                                    ["fit.auto_zero_min_bins: 1 이상의 정수여야 한다 (-5)"]),
    "auto_zero_min_bins_bool": ("auto_zero_min_bins = 120", "auto_zero_min_bins = true",
                                ["fit.auto_zero_min_bins: 1 이상의 정수여야 한다 (True)"]),
    "auto_zero_min_bins_float": ("auto_zero_min_bins = 120", "auto_zero_min_bins = 120.0",
                                 ["fit.auto_zero_min_bins: 1 이상의 정수여야 한다 (120.0)"]),
    "auto_zero_min_bins_string": ("auto_zero_min_bins = 120", 'auto_zero_min_bins = "120"',
                                  ["fit.auto_zero_min_bins: 1 이상의 정수여야 한다 ('120')"]),
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
    # 옛 배치 방식의 키는 이제 모르는 필드다 (옛 파일을 그대로 쓰면 이유가 나온다)
    "removed_fit_key_calib_version": ("window_secs = [2.0, 3.0]", 'window_secs = [2.0, 3.0]\ncalib_version = "c1"',
                                      ["fit.calib_version: 모르는 필드다"]),
    "removed_fit_key_min_distance_m": ("window_secs = [2.0, 3.0]", "window_secs = [2.0, 3.0]\nmin_distance_m = 0.3",
                                       ["fit.min_distance_m: 모르는 필드다"]),
    "unknown_acceptance_key": ("max_ece = 0.05", "max_ece = 0.05\nmax_ece_2 = 0.1",
                               ["acceptance.max_ece_2: 모르는 필드다"]),
    "misspelled_key": ("max_ece = 0.05", "max_ec = 0.05",
                       ["acceptance.max_ece: 필드가 없다", "acceptance.max_ec: 모르는 필드다"]),
    "missing_ratios": ("ratios = [0.6, 0.2, 0.2]\n", "", ["split.ratios: 필드가 없다"]),
    "missing_min_runs": ("min_runs_per_label = 5\n", "", ["split.min_runs_per_label: 필드가 없다"]),
    "missing_taus": ("taus = [1.0, 1.5, 2.0]\n", "", ["fit.taus: 필드가 없다"]),
    "missing_stays": ("stays = [0.8, 0.9, 0.95]\n", "", ["fit.stays: 필드가 없다"]),
    "missing_window_secs": ("window_secs = [2.0, 3.0]\n", "", ["fit.window_secs: 필드가 없다"]),
    "missing_auto_zero_min_bins": ("auto_zero_min_bins = 120\n", "", ["fit.auto_zero_min_bins: 필드가 없다"]),
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
min_distance_m = 0.3
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
        "fit.auto_zero_min_bins: 필드가 없다",
        "fit.min_distance_m: 모르는 필드다",
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
