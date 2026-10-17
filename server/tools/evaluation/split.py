"""수집 회차를 학습·검증·시험으로 나누고, 분할 결과 파일을 읽고 쓴다 (specs/core-ops-cli/spec.md 6절 "split").

분할 단위는 회차다. 같은 회차의 프레임은 서로 다른 분할에 섞지 않는다 (FR-16).
이 모듈은 회차 ID만 다루고 DB를 열지 않는다. 회차 목록(`runs.list_runs`)은 호출자가 읽기 전용 연결로 읽어 넘긴다 (NFR-13).
같은 회차 목록, 설정, 시드는 같은 결과를 낸다. 섞는 순서는 라벨마다 시드와 라벨 이름으로 따로 정해서,
다른 라벨의 회차 수가 달라져도 이 라벨의 배정은 그대로다.
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from nearpilot.core.proximity.files import SplitSettings
from tools.evaluation.runs import RunInfo

# 분할 결과 JSON의 키와 순서. `split_to_json`이 이 순서로 쓰고 `load_split`이 이 열 개만 받는다.
_KEYS = ("place_id", "seed", "ratios", "created_at", "db_path", "train", "validation", "test", "excluded", "labels")
_EXCLUDED_KEYS = {"collection_id", "reason"}


class SplitError(ValueError):
    """분할할 수 없거나 분할 결과 파일이 올바르지 않다."""


@dataclass(frozen=True)
class SplitResult:
    """분할 결과. 회차 ID 목록은 정렬되어 있고, 모든 회차는 `train`, `validation`, `test`, `excluded` 중 한 곳에만 있다."""

    place_id: str
    seed: int
    ratios: tuple[float, float, float]  # 학습, 검증, 시험
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]  # (collection_id, 사유)
    labels: Mapping[str, tuple[int, int, int]]  # 라벨 → (학습, 검증, 시험) 회차 수


def split_runs(runs: Sequence[RunInfo], place_id: str, settings: SplitSettings, seed: int) -> SplitResult:
    """`place_id` 장소의 회차를 라벨마다 학습·검증·시험으로 나눈다.

    대상은 끝났고 관측이 있는 회차다. 진행 중이거나 관측이 없는 회차, 장소를 정할 수 없는 회차는
    `excluded`에 사유와 함께 담는다. 다른 장소의 회차는 이 분할과 상관없어서 담지 않는다.
    라벨의 회차가 `settings.min_runs_per_label`보다 적거나 나눌 회차가 하나도 없으면 `SplitError`다.
    """
    excluded: list[tuple[str, str]] = []
    by_label: defaultdict[str, list[str]] = defaultdict(list)
    for run in runs:
        if run.place_id is None:
            # 어느 장소의 회차인지 몰라서 모든 분할에서 빼고, 사용자가 빠진 회차를 보게 한다
            excluded.append((run.collection_id, run.place_problem or "장소 모름"))
        elif run.place_id != place_id:
            continue
        elif run.ended_at is None:
            excluded.append((run.collection_id, "진행 중"))
        elif run.frame_count == 0:
            excluded.append((run.collection_id, "관측 없음"))
        else:
            by_label[run.label].append(run.collection_id)

    if not by_label:
        raise SplitError(f"{place_id}에 분할할 회차가 없다 (끝났고 관측이 있는 회차가 필요하다)")
    short = {label: len(ids) for label, ids in by_label.items() if len(ids) < settings.min_runs_per_label}
    if short:
        listed = ", ".join(f"{label} {count}개" for label, count in sorted(short.items()))
        raise SplitError(f"회차가 부족한 라벨이 있다 (라벨마다 최소 {settings.min_runs_per_label}개): {listed}")

    train: list[str] = []
    validation: list[str] = []
    test: list[str] = []
    labels: dict[str, tuple[int, int, int]] = {}
    for label in sorted(by_label):
        ids = sorted(by_label[label])
        random.Random(f"{seed}:{label}").shuffle(ids)
        n_train, n_validation, n_test = _quotas(len(ids), settings.ratios)
        labels[label] = (n_train, n_validation, n_test)
        train += ids[:n_train]
        validation += ids[n_train:n_train + n_validation]
        test += ids[n_train + n_validation:]
    return SplitResult(
        place_id=place_id,
        seed=seed,
        ratios=settings.ratios,
        train=tuple(sorted(train)),
        validation=tuple(sorted(validation)),
        test=tuple(sorted(test)),
        excluded=tuple(sorted(excluded)),
        labels=labels,
    )


def _quotas(n: int, ratios: tuple[float, float, float]) -> tuple[int, int, int]:
    """라벨 하나의 (학습, 검증, 시험) 회차 수.

    몫은 `floor(n × 비율)`이고, 남는 회차는 학습 → 검증 → 시험 순으로 하나씩 더한다.
    검증이나 시험이 0이면 학습에서 하나씩 옮긴다. 비율은 파일에 적은 십진수로 계산한다.
    부동소수점 곱은 `0.7 × 90`이 62.99999999999999라서 floor가 한 칸 모자라다.
    """
    counts = [math.floor(n * Fraction(str(ratio))) for ratio in ratios]
    for i in range(n - sum(counts)):
        counts[i % 3] += 1
    for i in (1, 2):
        if counts[i] == 0:
            counts[0] -= 1
            counts[i] += 1
    return counts[0], counts[1], counts[2]


def split_to_json(result: SplitResult, *, created_at: datetime, db_path: str) -> str:
    """분할 결과를 JSON 문자열로 쓴다. 키 순서는 `_KEYS`이고 들여쓰기 2에 끝 줄바꿈 하나다.

    같은 결과와 `db_path`는 `created_at`만 다르고 나머지가 바이트까지 같다.
    """
    data = {
        "place_id": result.place_id,
        "seed": result.seed,
        "ratios": list(result.ratios),
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "db_path": db_path,
        "train": list(result.train),
        "validation": list(result.validation),
        "test": list(result.test),
        "excluded": [{"collection_id": collection_id, "reason": reason} for collection_id, reason in result.excluded],
        "labels": {label: list(counts) for label, counts in result.labels.items()},
    }
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def load_split(path: Path) -> SplitResult:
    """분할 결과 JSON을 읽어 검사한다. 읽을 수 없거나 형식이 틀리면 `SplitError`다.

    키는 `split_to_json`이 쓰는 열 개가 정확히 있어야 한다. 한 회차는 `train`, `validation`, `test`, `excluded`를
    통틀어 한 번만 나온다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SplitError(f"{path}: 읽을 수 없다 ({error.strerror or error})") from error
    except ValueError as error:  # JSONDecodeError, UnicodeDecodeError
        raise SplitError(f"{path}: UTF-8 JSON으로 읽을 수 없다 ({error})") from error
    if not isinstance(raw, dict):
        raise SplitError(f"{path}: 최상위가 JSON 객체여야 한다")
    problems = [f"{key}: 필드가 없다" for key in _KEYS if key not in raw]
    problems += [f"{key}: 모르는 필드다" for key in raw if key not in _KEYS]
    if problems:
        raise SplitError("; ".join(problems))

    result = SplitResult(
        place_id=_text(raw["place_id"], "place_id"),
        seed=_integer(raw["seed"], "seed"),
        ratios=_ratios(raw["ratios"]),
        train=_ids(raw["train"], "train"),
        validation=_ids(raw["validation"], "validation"),
        test=_ids(raw["test"], "test"),
        excluded=_excluded(raw["excluded"]),
        labels=_labels(raw["labels"]),
    )
    _check_metadata(raw)
    _check_each_run_once(result)
    return result


def _check_metadata(raw: dict[str, Any]) -> None:
    """`created_at`은 ISO 8601 시각이고 `db_path`는 문자열이다. 결과 객체에는 담지 않는다."""
    created_at = _text(raw["created_at"], "created_at")
    try:
        datetime.fromisoformat(created_at)
    except ValueError:
        raise SplitError(f"created_at: ISO 8601 시각이어야 한다 ({created_at!r:.40})") from None
    _text(raw["db_path"], "db_path")


def _check_each_run_once(result: SplitResult) -> None:
    """한 회차가 둘 이상의 목록에 있으면 `SplitError`다. 제외한 회차도 분할에 있으면 안 된다."""
    homes: defaultdict[str, list[str]] = defaultdict(list)
    parts = (
        ("train", result.train),
        ("validation", result.validation),
        ("test", result.test),
        ("excluded", [collection_id for collection_id, _ in result.excluded]),
    )
    for name, ids in parts:
        for collection_id in ids:
            homes[collection_id].append(name)
    repeated = {collection_id: names for collection_id, names in homes.items() if len(names) > 1}
    if repeated:
        listed = ", ".join(f"{collection_id} ({', '.join(names)})" for collection_id, names in sorted(repeated.items()))
        raise SplitError(f"한 회차가 둘 이상에 들어 있다: {listed}")


def _text(value: Any, where: str) -> str:
    """비어 있지 않은 문자열이다."""
    if isinstance(value, str) and value.strip():
        return value
    raise SplitError(f"{where}: 비어 있지 않은 문자열이어야 한다 ({value!r:.40})")


def _integer(value: Any, where: str, minimum: int | None = None) -> int:
    """bool이 아닌 정수다. `minimum`이 있으면 그 이상이어야 한다."""
    if type(value) is not int or (minimum is not None and value < minimum):
        need = "정수" if minimum is None else f"{minimum} 이상의 정수"
        raise SplitError(f"{where}: {need}여야 한다 ({value!r:.40})")
    return value


def _ratios(value: Any) -> tuple[float, float, float]:
    """bool이 아닌 수 세 개의 목록이다."""
    if not isinstance(value, list) or len(value) != 3 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in value
    ):
        raise SplitError(f"ratios: 수 3개의 목록이어야 한다 ({value!r:.40})")
    return float(value[0]), float(value[1]), float(value[2])


def _ids(value: Any, where: str) -> tuple[str, ...]:
    """회차 ID 문자열의 목록이다."""
    if not isinstance(value, list):
        raise SplitError(f"{where}: JSON 배열이어야 한다 ({value!r:.40})")
    return tuple(_text(item, f"{where}[{i}]") for i, item in enumerate(value))


def _excluded(value: Any) -> tuple[tuple[str, str], ...]:
    """`collection_id`와 `reason`만 가진 객체의 목록이다."""
    if not isinstance(value, list):
        raise SplitError(f"excluded: JSON 배열이어야 한다 ({value!r:.40})")
    entries = []
    for i, item in enumerate(value):
        where = f"excluded[{i}]"
        if not isinstance(item, dict) or set(item) != _EXCLUDED_KEYS:
            raise SplitError(f"{where}: collection_id와 reason만 가진 JSON 객체여야 한다 ({item!r:.40})")
        collection_id = _text(item["collection_id"], f"{where}.collection_id")
        entries.append((collection_id, _text(item["reason"], f"{where}.reason")))
    return tuple(entries)


def _labels(value: Any) -> dict[str, tuple[int, int, int]]:
    """라벨 → 회차 수 세 개(학습, 검증, 시험)의 객체다."""
    if not isinstance(value, dict):
        raise SplitError(f"labels: JSON 객체여야 한다 ({value!r:.40})")
    labels = {}
    for label, counts in value.items():
        where = f"labels.{label}"
        _text(label, "labels: 라벨")
        if not isinstance(counts, list) or len(counts) != 3:
            raise SplitError(f"{where}: 회차 수 3개(학습, 검증, 시험)의 목록이어야 한다 ({counts!r:.40})")
        labels[label] = tuple(_integer(count, f"{where}[{i}]", 0) for i, count in enumerate(counts))
    return labels
