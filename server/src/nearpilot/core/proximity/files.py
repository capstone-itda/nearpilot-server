"""배치 파일(JSON)과 적합 설정 파일(TOML) 읽기 (specs/core-ops-cli/spec.md 6절 "split", "fit", "eval").

배치 파일은 `PlaceLayout`이 되고, 적합 설정 파일은 `split`, `fit`, `eval`이 나눠 쓰는 `FitFile`이 된다.
두 파일 모두 모르는 키와 빠진 키를 거부하고 (오타를 막는다), 잘못된 필드를 모두 모아 한 번에 알린다.
값의 범위는 적합이 다시 검사한다 (`fit_model`). 이 모듈은 파일을 읽어 검사할 뿐 DB와 모델 파일을 건드리지 않는다.
"""

from __future__ import annotations

import json
import math
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nearpilot.core.proximity.fit import AnchorLayout, PlaceLayout

MIN_RUNS_FLOOR = 3  # 라벨 하나가 학습·검증·시험에 한 회차씩은 들어가려면 필요한 회차 수
RATIO_SUM_TOLERANCE = 1e-9  # 분할 비율의 합이 1에서 벗어날 수 있는 한계


class LayoutFileError(ValueError):
    """배치 파일을 쓸 수 없다. 메시지에 잘못된 필드 경로를 모두 "; "로 잇는다."""


class FitFileError(ValueError):
    """적합 설정 파일을 쓸 수 없다. 메시지에 잘못된 필드 경로를 모두 "; "로 잇는다."""


@dataclass(frozen=True)
class SplitSettings:
    """`[split]`. 회차를 학습·검증·시험으로 나누는 설정."""

    ratios: tuple[float, float, float]  # 학습, 검증, 시험
    min_runs_per_label: int


@dataclass(frozen=True)
class FitSettings:
    """`[fit]`. 모델 적합의 후보 값."""

    calib_version: str | None
    min_distance_m: float
    taus: tuple[float, ...]
    stays: tuple[float, ...]
    window_secs: tuple[float, ...]


@dataclass(frozen=True)
class AcceptanceSettings:
    """`[acceptance]`. 평가의 통과 기준."""

    max_misselection: float
    max_ece: float


@dataclass(frozen=True)
class FitFile:
    """적합 설정 파일의 내용. 세 절이 모두 있어야 한다."""

    split: SplitSettings
    fit: FitSettings
    acceptance: AcceptanceSettings


_LAYOUT_KEYS = ("place_id", "nodes", "none_points")
_LAYOUT_OPTIONAL = ("none_points",)
_ANCHOR_KEYS = ("x", "y", "front_x", "front_y")
_SPLIT_KEYS = ("ratios", "min_runs_per_label")
_FIT_KEYS = ("calib_version", "min_distance_m", "taus", "stays", "window_secs")
_FIT_OPTIONAL = ("calib_version",)
_ACCEPTANCE_KEYS = ("max_misselection", "max_ece")
_FIT_FILE_SECTIONS = ("split", "fit", "acceptance")


# ── 공용 검사 ─────────────────────────────────────────────────────────


def _field(where: str, key: str) -> str:
    """`where` 안의 `key` 필드 경로. 최상위(`where`가 빈 문자열)면 `key` 그대로다."""
    return f"{where}.{key}" if where else key


def _check_keys(
    table: dict[str, Any], where: str, keys: tuple[str, ...], optional: tuple[str, ...], problems: list[str]
) -> None:
    """빠진 필수 키와 모르는 키를 모두 문제로 모은다. `keys`는 허용하는 키 전부이고 `optional`은 그중 없어도 되는 키다."""
    problems.extend(f"{_field(where, key)}: 필드가 없다" for key in keys if key not in table and key not in optional)
    problems.extend(f"{_field(where, key)}: 모르는 필드다" for key in table if key not in keys)


def _number(value: Any, where: str, problems: list[str]) -> float | None:
    """bool이 아닌 유한한 int 또는 float를 float로 바꾼다. 아니면 문제를 모으고 None을 돌려준다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"{where}: 숫자여야 한다 ({value!r:.40})")
        return None
    try:
        number = float(value)
    except OverflowError:  # float로 바꿀 수 없을 만큼 큰 int
        number = math.inf
    if not math.isfinite(number):
        problems.append(f"{where}: 유한한 숫자여야 한다 ({value!r:.40})")  # 큰 int가 메시지를 채우지 않게 자른다
        return None
    return number


def _text(value: Any, where: str, problems: list[str]) -> str | None:
    """비어 있지 않은 문자열이다. 공백만 있어도 거부한다."""
    if isinstance(value, str) and value.strip():
        return value
    problems.append(f"{where}: 비어 있지 않은 문자열이어야 한다 ({value!r:.40})")
    return None


# ── 배치 파일 ─────────────────────────────────────────────────────────


def load_layout(path: Path) -> PlaceLayout:
    """배치 JSON을 읽어 검사한다. 읽을 수 없거나 잘못된 필드가 있으면 모두 모아 `LayoutFileError`로 낸다.

    형식: `place_id`, `nodes`(기기 ID → `x`, `y`, `front_x`, `front_y`), `none_points`(선택, `[[x, y], ...]`).
    `nodes`는 비어 있지 않다. 좌표는 bool이 아닌 유한한 수다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise LayoutFileError(f"{path}: 읽을 수 없다 ({error.strerror or error})") from error
    except ValueError as error:  # JSONDecodeError, UnicodeDecodeError
        raise LayoutFileError(f"{path}: UTF-8 JSON으로 읽을 수 없다 ({error})") from error
    if not isinstance(raw, dict):
        raise LayoutFileError(f"{path}: 최상위가 JSON 객체여야 한다")

    problems: list[str] = []
    _check_keys(raw, "", _LAYOUT_KEYS, _LAYOUT_OPTIONAL, problems)
    place_id = _text(raw["place_id"], "place_id", problems) if "place_id" in raw else None
    nodes = _layout_nodes(raw["nodes"], problems) if "nodes" in raw else {}
    none_points = _none_points(raw["none_points"], problems) if "none_points" in raw else ()
    if problems:
        raise LayoutFileError("; ".join(problems))
    return PlaceLayout(place_id, nodes, none_points)


def _layout_nodes(value: Any, problems: list[str]) -> dict[str, AnchorLayout]:
    """`nodes` 객체를 읽는다. 문제가 있는 기기는 결과에서 뺀다."""
    if not isinstance(value, dict) or not value:
        problems.append(f"nodes: 비어 있지 않은 JSON 객체여야 한다 ({value!r:.40})")
        return {}
    nodes: dict[str, AnchorLayout] = {}
    for node_id, entry in value.items():
        where = f"nodes.{node_id}"
        if not node_id.strip():
            problems.append(f"nodes: 기기 ID는 비어 있지 않은 문자열이어야 한다 ({node_id!r})")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{where}: JSON 객체여야 한다 ({entry!r:.40})")
            continue
        _check_keys(entry, where, _ANCHOR_KEYS, (), problems)
        coords = {key: _number(entry[key], f"{where}.{key}", problems) for key in _ANCHOR_KEYS if key in entry}
        if len(coords) == len(_ANCHOR_KEYS) and None not in coords.values():
            nodes[node_id] = AnchorLayout(**coords)
    return nodes


def _none_points(value: Any, problems: list[str]) -> tuple[tuple[float, float], ...]:
    """`none_points`를 읽는다. 각 점은 `[x, y]`다. 문제가 있는 점은 결과에서 뺀다."""
    if not isinstance(value, list):
        problems.append(f"none_points: JSON 배열이어야 한다 ({value!r:.40})")
        return ()
    points = []
    for i, point in enumerate(value):
        where = f"none_points[{i}]"
        if not isinstance(point, list) or len(point) != 2:
            problems.append(f"{where}: [x, y] 숫자 두 개여야 한다 ({point!r:.40})")
            continue
        x = _number(point[0], f"{where}[0]", problems)
        y = _number(point[1], f"{where}[1]", problems)
        if x is not None and y is not None:
            points.append((x, y))
    return tuple(points)


# ── 적합 설정 파일 ────────────────────────────────────────────────────


def load_fit_file(path: Path) -> FitFile:
    """적합 설정 TOML을 읽어 검사한다. 읽을 수 없거나 잘못된 필드가 있으면 모두 모아 `FitFileError`로 낸다.

    `[split]`, `[fit]`, `[acceptance]` 세 절이 모두 있어야 한다. `[fit]`의 `calib_version`만 선택이다.
    목록은 tuple로 돌려준다.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise FitFileError(f"{path}: 읽을 수 없다 ({error.strerror or error})") from error
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise FitFileError(f"{path}: UTF-8 TOML로 읽을 수 없다 ({error})") from error

    problems: list[str] = []
    _check_keys(raw, "", _FIT_FILE_SECTIONS, (), problems)
    split = _split_settings(raw, problems)
    fit = _fit_settings(raw, problems)
    acceptance = _acceptance_settings(raw, problems)
    if problems:
        raise FitFileError("; ".join(problems))
    return FitFile(split, fit, acceptance)


def _section(
    raw: dict[str, Any], name: str, keys: tuple[str, ...], optional: tuple[str, ...], problems: list[str]
) -> dict[str, Any] | None:
    """절 하나의 표를 꺼내 키를 검사한다. 절이 없으면 None이다 (없는 절은 최상위 검사가 이미 모았다)."""
    if name not in raw:
        return None
    table = raw[name]
    if not isinstance(table, dict):
        problems.append(f"{name}: 표여야 한다 ({table!r:.40})")
        return None
    _check_keys(table, name, keys, optional, problems)
    return table


def _split_settings(raw: dict[str, Any], problems: list[str]) -> SplitSettings | None:
    table = _section(raw, "split", _SPLIT_KEYS, (), problems)
    if table is None:
        return None
    ratios = _ratios(table["ratios"], problems) if "ratios" in table else None
    min_runs = _min_runs(table["min_runs_per_label"], problems) if "min_runs_per_label" in table else None
    if ratios is None or min_runs is None:
        return None
    return SplitSettings(ratios, min_runs)


def _ratios(value: Any, problems: list[str]) -> tuple[float, float, float] | None:
    """양수 3개이고 합이 1이다 (오차 `RATIO_SUM_TOLERANCE`)."""
    if not isinstance(value, list) or len(value) != 3:
        problems.append(f"split.ratios: 양수 3개의 목록이어야 한다 ({value!r:.40})")
        return None
    items = [_positive(item, f"split.ratios[{i}]", problems) for i, item in enumerate(value)]
    if None in items:
        return None
    total = math.fsum(items)
    if abs(total - 1.0) > RATIO_SUM_TOLERANCE:
        problems.append(f"split.ratios: 합이 1이어야 한다 (합 {total!r})")
        return None
    return tuple(items)


def _min_runs(value: Any, problems: list[str]) -> int | None:
    """`MIN_RUNS_FLOOR` 이상의 정수다. bool은 정수로 보지 않는다."""
    if type(value) is not int or value < MIN_RUNS_FLOOR:
        problems.append(f"split.min_runs_per_label: {MIN_RUNS_FLOOR} 이상의 정수여야 한다 ({value!r:.40})")
        return None
    return value


def _fit_settings(raw: dict[str, Any], problems: list[str]) -> FitSettings | None:
    table = _section(raw, "fit", _FIT_KEYS, _FIT_OPTIONAL, problems)
    if table is None:
        return None
    calib_version = _text(table["calib_version"], "fit.calib_version", problems) if "calib_version" in table else None
    min_distance_m = (
        _positive(table["min_distance_m"], "fit.min_distance_m", problems) if "min_distance_m" in table else None
    )
    taus = _candidates(table["taus"], "fit.taus", _positive, problems) if "taus" in table else None
    stays = _candidates(table["stays"], "fit.stays", _open_fraction, problems) if "stays" in table else None
    window_secs = (
        _candidates(table["window_secs"], "fit.window_secs", _positive, problems) if "window_secs" in table else None
    )
    if any(item is None for item in (min_distance_m, taus, stays, window_secs)):  # calib_version은 선택이라 None이 정상이다
        return None
    return FitSettings(calib_version, min_distance_m, taus, stays, window_secs)


def _acceptance_settings(raw: dict[str, Any], problems: list[str]) -> AcceptanceSettings | None:
    table = _section(raw, "acceptance", _ACCEPTANCE_KEYS, (), problems)
    if table is None:
        return None
    values = [
        _closed_fraction(table[key], f"acceptance.{key}", problems) if key in table else None
        for key in _ACCEPTANCE_KEYS
    ]
    if None in values:
        return None
    return AcceptanceSettings(*values)


def _positive(value: Any, where: str, problems: list[str]) -> float | None:
    """0보다 큰 유한한 수다."""
    number = _number(value, where, problems)
    if number is not None and number <= 0:
        problems.append(f"{where}: 0보다 커야 한다 ({value!r:.40})")
        return None
    return number


def _open_fraction(value: Any, where: str, problems: list[str]) -> float | None:
    """0 초과 1 미만의 수다."""
    number = _number(value, where, problems)
    if number is not None and not 0 < number < 1:
        problems.append(f"{where}: 0보다 크고 1보다 작아야 한다 ({value!r:.40})")
        return None
    return number


def _closed_fraction(value: Any, where: str, problems: list[str]) -> float | None:
    """0 이상 1 이하의 수다."""
    number = _number(value, where, problems)
    if number is not None and not 0 <= number <= 1:
        problems.append(f"{where}: 0 이상 1 이하여야 한다 ({value!r:.40})")
        return None
    return number


def _candidates(
    value: Any, where: str, check: Callable[[Any, str, list[str]], float | None], problems: list[str]
) -> tuple[float, ...] | None:
    """비어 있지 않은 목록이고 값이 모두 `check`를 통과해야 한다. 적합이 후보를 하나 이상 요구한다 (`fit_model`)."""
    if not isinstance(value, list) or not value:
        problems.append(f"{where}: 비어 있지 않은 목록이어야 한다 ({value!r:.40})")
        return None
    items = [check(item, f"{where}[{i}]", problems) for i, item in enumerate(value)]
    if any(item is None for item in items):
        return None
    return tuple(items)
