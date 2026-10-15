"""수집 계획 파일 읽기 (specs/core-proximity-data-collection/spec.md 6절 "도구").

계획 파일(JSON)을 읽을 때 형식과 값을 모두 검사하고 잘못된 파일은 `PlanError`로 거부한다.
읽은 계획은 바꿀 수 없다. 이 모듈은 파일을 읽어 검사할 뿐 `AdminRepository`를 부르지 않는다.
라벨이 등록된 기기 ID인지는 검사하지 않는다 (DB가 `NODE_NOT_FOUND`로 판정한다).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import inf, isfinite
from pathlib import Path
from typing import Any

from nearpilot.shared.models import BeaconKey


class PlanError(ValueError):
    """계획 파일이 없거나, JSON이 아니거나, 내용이 잘못됐다. 메시지에 잘못된 필드 경로를 쓴다."""


@dataclass(frozen=True, slots=True)
class PlannedRun:
    """회차 하나의 계획."""

    label: str  # node_id 또는 "none". 계획 파일 값 그대로
    spot: str  # 서는 위치 안내


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    """수집 계획 파일의 내용."""

    place_id: str  # 기록용 장소 ID. DB 호출에 쓰지 않는다
    beacon: BeaconKey
    settle_sec: float  # Enter 뒤 회차를 시작하기 전까지 기다리는 시간. 0 이상
    run_sec: float  # 회차 길이. 0보다 크다
    runs: tuple[PlannedRun, ...]


_TOP_KEYS = ("place_id", "beacon", "settle_sec", "run_sec", "runs")
_BEACON_KEYS = ("uuid", "major", "minor")
_RUN_KEYS = ("label", "spot")


def _field(where: str, key: str) -> str:
    """`where` 객체 안의 `key` 필드 경로. 최상위(`where`가 빈 문자열)면 `key` 그대로다."""
    return f"{where}.{key}" if where else key


def _fixed_object(value: Any, where: str, keys: tuple[str, ...]) -> dict[str, Any]:
    """키가 정해진 JSON 객체다. 빠진 키와 모르는 키를 모두 거부한다 (오타를 막는다)."""
    if not isinstance(value, dict):
        raise PlanError(f"{where}: JSON 객체여야 한다")
    problems = [f"{_field(where, key)}: 필드가 없다" for key in keys if key not in value]
    problems += [f"{_field(where, key)}: 모르는 필드다" for key in value if key not in keys]
    if problems:
        raise PlanError("; ".join(problems))
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlanError(f"{where}: 비어 있지 않은 문자열이어야 한다 ({value!r})")
    return value


def _number(value: Any, where: str) -> float:
    """bool이 아닌 유한한 int 또는 float를 float로 바꾼다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{where}: 숫자여야 한다 ({value!r})")
    try:
        number = float(value)
    except OverflowError:  # float로 바꿀 수 없을 만큼 큰 int
        number = inf
    if not isfinite(number):
        raise PlanError(f"{where}: 유한한 숫자여야 한다 ({value!r:.40})")  # 큰 int가 메시지를 채우지 않게 자른다
    return number


def _beacon_field(value: Any, where: str) -> int:
    """iBeacon의 major, minor. bool이 아닌 int로, 0 이상 65535 이하다."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError(f"{where}: 정수여야 한다 ({value!r})")
    if not 0 <= value <= 65535:
        raise PlanError(f"{where}: 0 이상 65535 이하여야 한다 ({value!r})")
    return value


def _beacon(value: Any) -> BeaconKey:
    entry = _fixed_object(value, "beacon", _BEACON_KEYS)
    return BeaconKey(
        uuid=_string(entry["uuid"], "beacon.uuid"),
        major=_beacon_field(entry["major"], "beacon.major"),
        minor=_beacon_field(entry["minor"], "beacon.minor"),
    )


def _runs(value: Any) -> tuple[PlannedRun, ...]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"runs: 비어 있지 않은 JSON 배열이어야 한다 ({value!r})")
    runs = []
    for i, raw in enumerate(value):
        where = f"runs[{i}]"
        entry = _fixed_object(raw, where, _RUN_KEYS)
        label = _string(entry["label"], f"{where}.label")
        if label != label.strip():
            raise PlanError(f"{where}.label: 앞뒤에 공백이 없어야 한다 ({label!r})")
        runs.append(PlannedRun(label=label, spot=_string(entry["spot"], f"{where}.spot")))
    return tuple(runs)


def load_plan(path: Path) -> CollectionPlan:
    """계획 파일 하나를 읽어 검사한다. 파일을 읽을 수 없거나 형식, 값이 맞지 않으면 `PlanError`.

    같은 라벨이 연이어 나오는지는 검사하지 않는다. 회차 순서는 계획을 짜는 사람이 정한다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PlanError(f"{path}: 읽을 수 없다 ({error.strerror or error})") from error
    except ValueError as error:  # JSONDecodeError, UnicodeDecodeError
        raise PlanError(f"{path}: UTF-8 JSON으로 읽을 수 없다 ({error})") from error
    if not isinstance(raw, dict):
        raise PlanError(f"{path}: 최상위가 JSON 객체여야 한다")

    top = _fixed_object(raw, "", _TOP_KEYS)
    place_id = _string(top["place_id"], "place_id")
    beacon = _beacon(top["beacon"])
    settle_sec = _number(top["settle_sec"], "settle_sec")
    if settle_sec < 0:
        raise PlanError(f"settle_sec: 0 이상이어야 한다 ({top['settle_sec']!r})")
    run_sec = _number(top["run_sec"], "run_sec")
    if run_sec <= 0:
        raise PlanError(f"run_sec: 0보다 커야 한다 ({top['run_sec']!r})")
    return CollectionPlan(
        place_id=place_id,
        beacon=beacon,
        settle_sec=settle_sec,
        run_sec=run_sec,
        runs=_runs(top["runs"]),
    )
