"""좌표표와 수집 회차 기록 파일 읽기 (specs/db-evaluation-distance-order/spec.md 6절 결정 8, 9).

지표 10(거리 순서)은 좌표표로 정답 순서를 정한다 (FR-09, FR-16, #279). 좌표표는 평가 도구만 읽는다.
서버, 공통 모델 파일, 수집 도구는 좌표를 받지 않는다 (#252).

- 좌표표(TOML): 장소, 기기 좌표, 서는 위치(spot) 좌표다. 단위는 m이고, spot 이름은 수집 계획의 spot과 글자까지 같다.
- 회차 기록 파일(JSONL): `collect run`이 회차마다 한 줄을 덧붙인다. DB의 수집 회차에는 spot이 없어서
  여기서 회차 ID → spot을 읽는다.

이 모듈은 파일을 읽어 검사할 뿐 DB를 열지 않는다. 현재 시각과 무작위를 쓰지 않는다.
"""

from __future__ import annotations

import json
import math
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TOP_KEYS = ("place_id", "nodes", "spots")
_RECORD_KEYS = ("collection_id", "spot")  # 기록 한 줄에서 읽는 키. 나머지(`label`, `status` 등)는 읽지 않는다


class LayoutError(ValueError):
    """좌표표나 기록 파일이 없거나, 형식이 틀렸거나, 서로 맞지 않는다. 메시지에 파일과 필드 경로를 쓴다."""


@dataclass(frozen=True)
class Layout:
    """좌표표의 내용. 좌표는 (x, y)이고 단위는 m다."""

    place_id: str
    nodes: Mapping[str, tuple[float, float]]  # 기기 ID → (x, y)
    spots: Mapping[str, tuple[float, float]]  # 서는 위치(수집 계획의 spot) → (x, y)

    def distance(self, spot: str, node_id: str) -> float:
        """서는 위치와 기기의 유클리드 거리(m). 없는 이름이면 KeyError."""
        return math.dist(self.spots[spot], self.nodes[node_id])


# ── 좌표표 ────────────────────────────────────────────────────────────


def load_layout(path: Path) -> Layout:
    """좌표표 TOML을 읽어 검사한다. 읽을 수 없거나 잘못된 필드가 있으면 모두 모아 `LayoutError`로 낸다.

    최상위 키는 `place_id`, `nodes`, `spots` 셋이 정확히 있어야 한다 (빠진 키와 모르는 키를 거부한다).
    좌표는 모두 유한한 수 두 개의 배열이고, 이름(기기 ID, spot)은 파일에 적은 글자 그대로 쓴다.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise LayoutError(f"{path}: 읽을 수 없다 ({error.strerror or error})") from error
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise LayoutError(f"{path}: UTF-8 TOML로 읽을 수 없다 ({error})") from error

    problems = [f"{key}: 필드가 없다" for key in _TOP_KEYS if key not in raw]
    problems += [f"{key}: 모르는 필드다" for key in raw if key not in _TOP_KEYS]
    place_id = _place_id(raw["place_id"], problems) if "place_id" in raw else None
    nodes = _points(raw["nodes"], "nodes", problems) if "nodes" in raw else None
    spots = _points(raw["spots"], "spots", problems) if "spots" in raw else None
    if problems:
        raise LayoutError("; ".join(problems))
    return Layout(place_id, nodes, spots)


def _place_id(value: Any, problems: list[str]) -> str | None:
    """빈 문자열이 아닌 문자열이다."""
    if not isinstance(value, str) or not value:
        problems.append(f"place_id: 비어 있지 않은 문자열이어야 한다 ({value!r:.40})")
        return None
    return value


def _points(value: Any, where: str, problems: list[str]) -> dict[str, tuple[float, float]] | None:
    """이름 → [x, y]의 표다. 비어 있지 않아야 하고, 좌표가 틀린 이름은 모두 문제로 모은다."""
    if not isinstance(value, dict) or not value:
        problems.append(f"{where}: 비어 있지 않은 표여야 한다 ({value!r:.40})")
        return None
    points = {name: _point(item, f"{where}.{name}", problems) for name, item in value.items()}
    if None in points.values():
        return None
    return points


def _point(value: Any, where: str, problems: list[str]) -> tuple[float, float] | None:
    """bool이 아닌 유한한 수 두 개의 배열이다 ([x, y])."""
    if not isinstance(value, list) or len(value) != 2:
        problems.append(f"{where}: 수 두 개의 배열이어야 한다 ({value!r:.40})")
        return None
    x = _number(value[0], f"{where}[0]", problems)
    y = _number(value[1], f"{where}[1]", problems)
    if x is None or y is None:
        return None
    return x, y


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


# ── 회차 기록 파일 ────────────────────────────────────────────────────


def load_run_spots(paths: Sequence[Path]) -> dict[str, str]:
    """기록 파일들의 회차 ID → spot. 같은 회차가 같은 spot으로 여러 번 나오면 하나로 본다.

    `collect run`이 회차마다 JSON 객체 한 줄을 덧붙인 파일이다. 읽는 키는 `collection_id`와 `spot`뿐이고
    (`label`, `status` 등은 읽지 않는다), 빈 줄은 건너뛴다. 읽을 수 없는 파일, JSON 객체가 아닌 줄,
    `collection_id`나 `spot`이 없거나 비어 있지 않은 문자열이 아닌 줄은 `파일:줄 번호`와 함께 `LayoutError`다.
    같은 회차 ID에 다른 spot이 나와도 `LayoutError`다 (두 값과 두 위치를 쓴다).
    """
    found: dict[str, tuple[str, str]] = {}  # 회차 ID → (spot, 처음 나온 곳 "파일:줄 번호")
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise LayoutError(f"{path}: 읽을 수 없다 ({error.strerror or error})") from error
        except UnicodeDecodeError as error:
            raise LayoutError(f"{path}: UTF-8로 읽을 수 없다 ({error})") from error
        # 줄은 \n으로만 나눈다. splitlines()는 JSON 문자열 안의 U+2028 같은 글자에서도 끊는다
        for number, line in enumerate(text.split("\n"), start=1):
            if not line.strip():
                continue
            where = f"{path}:{number}"
            collection_id, spot = _record(line, where)
            if collection_id not in found:
                found[collection_id] = (spot, where)
            elif found[collection_id][0] != spot:
                first_spot, first_where = found[collection_id]
                raise LayoutError(
                    f"{where}: 회차 {collection_id}의 spot이 {spot!r}이다 ({first_where}에서는 {first_spot!r})"
                )
    return {collection_id: spot for collection_id, (spot, _) in found.items()}


def _record(line: str, where: str) -> tuple[str, str]:
    """기록 한 줄에서 (회차 ID, spot)을 꺼낸다. `where`는 오류 메시지에 쓸 "파일:줄 번호"다."""
    try:
        entry = json.loads(line)
    except ValueError as error:
        raise LayoutError(f"{where}: JSON으로 읽을 수 없다 ({error})") from error
    if not isinstance(entry, dict):
        raise LayoutError(f"{where}: JSON 객체여야 한다 ({entry!r:.40})")
    problems = []
    for key in _RECORD_KEYS:
        if key not in entry:
            problems.append(f"{key}: 필드가 없다")
        elif not isinstance(entry[key], str) or not entry[key]:
            problems.append(f"{key}: 비어 있지 않은 문자열이어야 한다 ({entry[key]!r:.40})")
    if problems:
        raise LayoutError(f"{where}: " + "; ".join(problems))
    return entry["collection_id"], entry["spot"]


def require_run_spots(spots: Mapping[str, str], run_ids: Iterable[str]) -> dict[str, str]:
    """`run_ids`의 spot만 골라 돌려준다. 기록에 없는 회차 ID를 모두 모아 `LayoutError` 하나로 낸다.

    돌려주는 사전은 `run_ids`에 처음 나온 순서이고, 같은 회차 ID는 한 번만 담는다.
    """
    wanted = list(dict.fromkeys(run_ids))
    missing = sorted(run_id for run_id in wanted if run_id not in spots)
    if missing:
        raise LayoutError(f"기록 파일에 회차 {', '.join(missing)}가 없다")
    return {run_id: spots[run_id] for run_id in wanted}


# ── 좌표표와 쓰는 이름의 대조 ─────────────────────────────────────────


def check_layout_covers(layout: Layout, spots: Iterable[str], nodes: Iterable[str]) -> None:
    """좌표표에 없는 spot과 기기를 모두 모아 `LayoutError` 하나로 낸다. 모두 있으면 아무것도 하지 않는다.

    이름은 정렬하고 따옴표로 감싸서 쓴다. spot은 "locker-1 앞, 손에 들고"처럼 쉼표가 들어갈 수 있다.
    """
    problems = []
    missing_spots = sorted({spot for spot in spots if spot not in layout.spots})
    if missing_spots:
        problems.append(f"좌표표에 없는 spot: {', '.join(map(repr, missing_spots))}")
    missing_nodes = sorted({node_id for node_id in nodes if node_id not in layout.nodes})
    if missing_nodes:
        problems.append(f"좌표표에 없는 기기: {', '.join(map(repr, missing_nodes))}")
    if problems:
        raise LayoutError("; ".join(problems))
