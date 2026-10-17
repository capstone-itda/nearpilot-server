"""회차 단위 분할과 분할 결과 파일 (FR-16, NFR-13, specs/core-ops-cli/spec.md 5절·6절 "split").

`RunInfo`를 직접 만들어 `split_runs`에 넣는다. DB는 열지 않는다.
몫의 기대값은 `floor(n × 비율)`과 남는 회차 규칙을 손으로 계산해서 적은 값이다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.core.proximity.files import SplitSettings
from tools.evaluation.runs import RunInfo
from tools.evaluation.split import SplitError, SplitResult, load_split, split_runs, split_to_json

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
CREATED_AT = datetime(2026, 10, 20, 5, 0, 0, tzinfo=timezone.utc)
SETTINGS = SplitSettings((0.6, 0.2, 0.2), 5)
PARTS = ("train", "validation", "test")


def make_run(number: int, label: str, *, place_id: str | None = "place-a", frames: int = 5, closed: bool = True,
             place_problem: str | None = None) -> RunInfo:
    """회차 ID가 `col-<번호 3자리>`인 회차. 닫힌 회차는 30초 길이다."""
    started = T0 + timedelta(seconds=60 * number)
    return RunInfo(
        collection_id=f"col-{number:03d}", beacon_id="beacon-1", label=label, started_at=started,
        ended_at=started + timedelta(seconds=30) if closed else None, frame_count=frames,
        place_id=place_id, place_problem=place_problem)


def make_runs(label: str, count: int, first: int = 1, **kwargs) -> list[RunInfo]:
    return [make_run(first + i, label, **kwargs) for i in range(count)]


def part_ids(result: SplitResult) -> list[str]:
    """세 분할의 회차 ID를 모두 모아 정렬한다."""
    return sorted(collection_id for part in PARTS for collection_id in getattr(result, part))


# ── 제외 ─────────────────────────────────────────────────────────────


def test_NFR13_split_excludes_open_and_empty_runs():
    runs = [
        *make_runs("locker-1", 5),  # col-001~005: 분할에 들어간다
        make_run(6, "locker-1", closed=False),
        make_run(7, "locker-1", frames=0),
        make_run(8, "locker-1", closed=False, frames=0),  # 둘 다면 진행 중이 먼저다
        make_run(9, "locker-8", place_id=None, place_problem="라벨 기기가 장소에 없다"),
        make_run(10, "none", place_id=None, place_problem="관측이 없어 장소를 정할 수 없다", frames=0),
        make_run(11, "none", place_id=None, place_problem="여러 장소의 관측이 섞였다"),
        make_run(12, "none", place_id=None, place_problem="여러 장소의 관측이 섞였다", closed=False),  # 장소를 모르면 진행 중이어도 이 사유다
        make_run(13, "locker-1", place_id=None),  # 사유가 비어 있어도 장소 모름으로 뺀다
        make_run(14, "locker-9", place_id="place-b"),  # 다른 장소의 회차는 이 분할과 상관없어서 제외 목록에도 없다
    ]

    result = split_runs(runs, "place-a", SETTINGS, 42)

    assert result.excluded == (
        ("col-006", "진행 중"),
        ("col-007", "관측 없음"),
        ("col-008", "진행 중"),
        ("col-009", "라벨 기기가 장소에 없다"),
        ("col-010", "관측이 없어 장소를 정할 수 없다"),
        ("col-011", "여러 장소의 관측이 섞였다"),
        ("col-012", "여러 장소의 관측이 섞였다"),
        ("col-013", "장소 모름"),
    )
    assert part_ids(result) == [f"col-00{i}" for i in range(1, 6)]  # 제외한 회차도 다른 장소의 회차도 분할에 없다
    assert result.labels == {"locker-1": (3, 1, 1)}


def test_NFR13_split_excluded_runs_are_sorted_by_collection_id():
    runs = [
        *make_runs("locker-1", 5),
        make_run(30, "locker-1", closed=False),
        make_run(8, "locker-1", frames=0),
        make_run(20, "none", place_id=None, place_problem="여러 장소의 관측이 섞였다"),
    ]

    result = split_runs(list(reversed(runs)), "place-a", SETTINGS, 42)

    assert [collection_id for collection_id, _ in result.excluded] == ["col-008", "col-020", "col-030"]


# ── 같은 입력과 시드 ──────────────────────────────────────────────────


def test_NFR13_split_deterministic_by_seed():
    runs = [*make_runs("locker-1", 40), *make_runs("none", 40, first=41)]

    first = split_runs(runs, "place-a", SETTINGS, 42)

    assert split_runs(runs, "place-a", SETTINGS, 42) == first
    assert split_runs(list(reversed(runs)), "place-a", SETTINGS, 42) == first  # 입력 순서와 상관없다
    assert split_to_json(first, created_at=CREATED_AT, db_path="/db") == split_to_json(
        split_runs(runs, "place-a", SETTINGS, 42), created_at=CREATED_AT, db_path="/db")
    other = split_runs(runs, "place-a", SETTINGS, 43)
    assert other.labels == first.labels  # 몫은 시드와 상관없고 배정만 달라진다
    assert other.train != first.train and other.validation != first.validation and other.test != first.test


def test_NFR13_split_label_assignment_does_not_depend_on_other_labels():
    locker_1 = make_runs("locker-1", 12)
    alone = split_runs(locker_1, "place-a", SETTINGS, 42)

    crowded = split_runs([*locker_1, *make_runs("locker-2", 7, first=13), *make_runs("none", 9, first=20)],
                         "place-a", SETTINGS, 42)

    own = {run.collection_id for run in locker_1}
    for part in PARTS:  # 다른 라벨이 늘어도 이 라벨의 회차는 같은 분할에 들어간다
        assert [i for i in getattr(crowded, part) if i in own] == list(getattr(alone, part))
    assert crowded.labels["locker-1"] == alone.labels["locker-1"]


def test_NFR13_split_shuffles_each_label_independently():
    runs = [*make_runs("locker-1", 40), *make_runs("locker-2", 40, first=41)]

    result = split_runs(runs, "place-a", SETTINGS, 42)

    def train_ranks(first: int) -> set[int]:  # 라벨 안에서 몇 번째 회차가 학습에 들어갔는지
        numbers = [int(collection_id.removeprefix("col-")) for collection_id in result.train]
        return {number - first for number in numbers if first <= number < first + 40}

    assert len(train_ranks(1)) == len(train_ranks(41)) == 24
    assert train_ranks(1) != train_ranks(41)  # 라벨마다 같은 순열로 섞지 않는다


# ── 회차가 모자랄 때 ──────────────────────────────────────────────────


def test_NFR13_split_rejects_too_few_runs():
    runs = [
        *make_runs("locker-1", 5),
        *make_runs("locker-2", 4, first=6),
        *make_runs("none", 2, first=10),
        make_run(12, "locker-1", closed=False),  # 진행 중인 회차는 세지 않는다
    ]

    with pytest.raises(SplitError) as error:
        split_runs(runs, "place-a", SETTINGS, 42)

    assert str(error.value) == "회차가 부족한 라벨이 있다 (라벨마다 최소 5개): locker-2 4개, none 2개"

    # 제외된 회차를 빼고 세므로 닫힌 회차 5개 가운데 하나가 비어 있으면 부족하다
    with pytest.raises(SplitError, match=r"locker-1 4개"):
        split_runs([*make_runs("locker-1", 4), make_run(5, "locker-1", frames=0)], "place-a", SETTINGS, 42)
    # 최소 회차 수와 같으면 나눈다
    assert split_runs(make_runs("locker-1", 5), "place-a", SETTINGS, 42).labels == {"locker-1": (3, 1, 1)}


def test_NFR13_split_rejects_place_without_usable_runs():
    runs = [make_run(1, "locker-1", closed=False), make_run(2, "locker-1", frames=0),
            *make_runs("locker-9", 5, first=3, place_id="place-b")]

    for given in (runs, []):
        with pytest.raises(SplitError, match="place-a에 분할할 회차가 없다"):
            split_runs(given, "place-a", SETTINGS, 42)


# ── 몫 ───────────────────────────────────────────────────────────────

# 손으로 계산한 라벨별 (학습, 검증, 시험) 회차 수. floor(n × 비율)에 남는 회차를 학습 → 검증 → 시험 순으로 더하고,
# 검증이나 시험이 0이면 학습에서 하나씩 옮긴다.
QUOTAS = {
    (0.6, 0.2, 0.2): {3: (1, 1, 1), 5: (3, 1, 1), 7: (5, 1, 1), 10: (6, 2, 2), 13: (8, 3, 2)},
    (0.5, 0.25, 0.25): {3: (1, 1, 1), 5: (3, 1, 1), 7: (4, 2, 1), 10: (6, 2, 2), 13: (7, 3, 3)},
}


@pytest.mark.parametrize("ratios", list(QUOTAS))
@pytest.mark.parametrize("n", [3, 5, 7, 10, 13])
def test_FR16_split_run_in_exactly_one_part(n, ratios):
    runs = make_runs("locker-1", n)

    result = split_runs(runs, "place-a", SplitSettings(ratios, 3), 42)

    expected = QUOTAS[ratios][n]
    assert part_ids(result) == [run.collection_id for run in runs]  # 모든 회차가 정확히 한 분할에 한 번 있다
    assert [len(getattr(result, part)) for part in PARTS] == list(expected)
    assert result.labels == {"locker-1": expected}
    assert len(result.validation) >= 1 and len(result.test) >= 1
    for part in PARTS:
        assert list(getattr(result, part)) == sorted(getattr(result, part))
    assert result.excluded == ()


@pytest.mark.parametrize(("ratios", "n", "expected"), [
    ((0.5, 0.4, 0.1), 5, (2, 2, 1)),  # 시험만 0이라 학습에서 하나 옮긴다
    ((0.5, 0.1, 0.4), 5, (2, 1, 2)),  # 검증만 0이라 학습에서 하나 옮긴다
    ((0.7, 0.15, 0.15), 90, (64, 13, 13)),  # 0.7 × 90 = 63인데 부동소수점 곱은 62.99999999999999다
    ((0.7, 0.15, 0.15), 170, (120, 25, 25)),
])
def test_FR16_split_quota_rules(ratios, n, expected):
    result = split_runs(make_runs("locker-1", n), "place-a", SplitSettings(ratios, 3), 42)

    assert result.labels == {"locker-1": expected}
    assert [len(getattr(result, part)) for part in PARTS] == list(expected)


def test_FR16_split_counts_each_label_on_its_own():
    runs = [*make_runs("locker-1", 5), *make_runs("none", 7, first=6), *make_runs("locker-2", 10, first=13)]

    result = split_runs(runs, "place-a", SETTINGS, 42)

    assert list(result.labels) == ["locker-1", "locker-2", "none"]  # 라벨 이름 순
    assert result.labels == {"locker-1": (3, 1, 1), "locker-2": (6, 2, 2), "none": (5, 1, 1)}
    assert [len(getattr(result, part)) for part in PARTS] == [14, 4, 4]
    label_of = {run.collection_id: run.label for run in runs}
    for index, part in enumerate(PARTS):  # 라벨마다 몫만큼 그 분할에 들어갔다
        for label, counts in result.labels.items():
            assert sum(label_of[i] == label for i in getattr(result, part)) == counts[index]


# ── 분할 파일 쓰기 ────────────────────────────────────────────────────


def sample_result() -> SplitResult:
    runs = [*make_runs("locker-1", 5), *make_runs("none", 5, first=6),
            make_run(11, "locker-1", closed=False), make_run(12, "none", place_id=None, place_problem="여러 장소의 관측이 섞였다")]
    return split_runs(runs, "place-a", SETTINGS, 42)


def test_NFR13_split_json_format():
    result = sample_result()

    text = split_to_json(result, created_at=CREATED_AT, db_path="/data/nearpilot.db")

    data = json.loads(text)
    assert list(data) == ["place_id", "seed", "ratios", "created_at", "db_path", "train", "validation", "test",
                          "excluded", "labels"]
    assert data["place_id"] == "place-a" and data["seed"] == 42 and data["ratios"] == [0.6, 0.2, 0.2]
    assert data["created_at"] == "2026-10-20T05:00:00+00:00" and data["db_path"] == "/data/nearpilot.db"
    assert (data["train"], data["validation"], data["test"]) == (
        list(result.train), list(result.validation), list(result.test))
    assert data["excluded"] == [{"collection_id": "col-011", "reason": "진행 중"},
                                {"collection_id": "col-012", "reason": "여러 장소의 관측이 섞였다"}]
    assert data["labels"] == {"locker-1": [3, 1, 1], "none": [3, 1, 1]} and list(data["labels"]) == ["locker-1", "none"]
    # 들여쓰기 2, 끝 줄바꿈 하나, 한글은 그대로 쓴다
    assert text.startswith('{\n  "place_id": "place-a",\n  "seed": 42,\n  "ratios": [\n    0.6,\n')
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert '"reason": "진행 중"' in text


def test_NFR13_split_json_differs_only_in_created_at():
    result = sample_result()
    later = CREATED_AT + timedelta(days=1, microseconds=5)

    first = split_to_json(result, created_at=CREATED_AT, db_path="/db")
    second = split_to_json(result, created_at=later, db_path="/db")

    assert first != second
    assert first.replace(CREATED_AT.isoformat(), later.isoformat()) == second  # 바이트까지 같고 작성 시각만 다르다
    # 시각은 UTC로 쓴다
    local = datetime(2026, 10, 20, 14, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    assert json.loads(split_to_json(result, created_at=local, db_path="/db"))["created_at"] == (
        "2026-10-20T05:00:00+00:00")


def test_NFR13_load_split_reads_what_split_to_json_wrote(tmp_path):
    result = sample_result()
    path = tmp_path / "split.json"
    path.write_text(split_to_json(result, created_at=CREATED_AT, db_path="/db"), encoding="utf-8")

    assert load_split(path) == result  # 한글 사유와 라벨 몫까지 그대로 읽힌다


# ── 분할 파일 읽기 ────────────────────────────────────────────────────


def good_data() -> dict:
    return json.loads(split_to_json(sample_result(), created_at=CREATED_AT, db_path="/db"))


def write_data(tmp_path: Path, data) -> Path:
    path = tmp_path / "split.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_FR16_load_split_rejects_run_in_two_parts(tmp_path):
    for source, target in (("train", "test"), ("train", "validation"), ("validation", "test"), ("test", "train")):
        data = good_data()
        shared = data[source][0]
        data[target].append(shared)
        with pytest.raises(SplitError, match="한 회차가 둘 이상에 들어 있다") as error:
            load_split(write_data(tmp_path, data))
        assert f"{shared} (" in str(error.value) and source in str(error.value) and target in str(error.value)


def test_FR16_load_split_rejects_repeated_and_excluded_runs(tmp_path):
    data = good_data()
    data["train"].append(data["train"][0])  # 같은 분할에 두 번
    with pytest.raises(SplitError, match="한 회차가 둘 이상에 들어 있다"):
        load_split(write_data(tmp_path, data))

    data = good_data()
    data["excluded"].append({"collection_id": data["test"][0], "reason": "진행 중"})  # 제외한 회차가 분할에도 있다
    with pytest.raises(SplitError, match=rf"{data['test'][0]} \(test, excluded\)"):
        load_split(write_data(tmp_path, data))


def _without(key):
    return lambda data: data.pop(key)


def _set(key, value):
    return lambda data: data.__setitem__(key, value)


@pytest.mark.parametrize(("mutate", "reason"), [
    (_without("seed"), "seed: 필드가 없다"),
    (_without("labels"), "labels: 필드가 없다"),
    (_set("extra", 1), "extra: 모르는 필드다"),
    (_set("place_id", ""), "place_id: 비어 있지 않은 문자열이어야 한다"),
    (_set("place_id", 3), "place_id: 비어 있지 않은 문자열이어야 한다"),
    (_set("place_id", "  "), "place_id: 비어 있지 않은 문자열이어야 한다"),
    (_set("seed", "42"), "seed: 정수여야 한다"),
    (_set("seed", True), "seed: 정수여야 한다"),
    (_set("seed", 4.5), "seed: 정수여야 한다"),
    (_set("ratios", [0.6, 0.4]), "ratios: 수 3개의 목록이어야 한다"),
    (_set("ratios", [0.6, "0.2", 0.2]), "ratios: 수 3개의 목록이어야 한다"),
    (_set("ratios", [True, 0.2, 0.2]), "ratios: 수 3개의 목록이어야 한다"),
    (_set("ratios", 1), "ratios: 수 3개의 목록이어야 한다"),
    (_set("created_at", "어제"), "created_at: ISO 8601 시각이어야 한다"),
    (_set("created_at", 20261020), "created_at: 비어 있지 않은 문자열이어야 한다"),
    (_set("db_path", None), "db_path: 비어 있지 않은 문자열이어야 한다"),
    (_set("train", "col-001"), "train: JSON 배열이어야 한다"),
    (_set("validation", [1]), "validation[0]: 비어 있지 않은 문자열이어야 한다"),
    (_set("test", [""]), "test[0]: 비어 있지 않은 문자열이어야 한다"),
    (_set("excluded", {}), "excluded: JSON 배열이어야 한다"),
    (_set("excluded", ["col-011"]), "excluded[0]: collection_id와 reason만 가진 JSON 객체여야 한다"),
    (_set("excluded", [{"collection_id": "col-011"}]), "excluded[0]: collection_id와 reason만 가진 JSON 객체여야 한다"),
    (_set("excluded", [{"collection_id": "col-011", "reason": "x", "more": 1}]),
     "excluded[0]: collection_id와 reason만 가진 JSON 객체여야 한다"),
    (_set("excluded", [{"collection_id": "col-011", "reason": ""}]), "excluded[0].reason: 비어 있지 않은"),
    (_set("labels", []), "labels: JSON 객체여야 한다"),
    (_set("labels", {"locker-1": [3, 1]}), "labels.locker-1: 회차 수 3개(학습, 검증, 시험)의 목록이어야 한다"),
    (_set("labels", {"locker-1": 5}), "labels.locker-1: 회차 수 3개"),
    (_set("labels", {"locker-1": [3, -1, 1]}), "labels.locker-1[1]: 0 이상의 정수여야 한다"),
    (_set("labels", {"locker-1": [3, True, 1]}), "labels.locker-1[1]: 0 이상의 정수여야 한다"),
    (_set("labels", {"locker-1": [3, 1.0, 1]}), "labels.locker-1[1]: 0 이상의 정수여야 한다"),
    (_set("labels", {"": [3, 1, 1]}), "labels: 라벨: 비어 있지 않은 문자열이어야 한다"),
])
def test_FR16_load_split_rejects_bad_fields(tmp_path, mutate, reason):
    data = good_data()
    mutate(data)

    with pytest.raises(SplitError) as error:
        load_split(write_data(tmp_path, data))

    assert reason in str(error.value)


def test_FR16_load_split_reports_every_missing_or_unknown_key(tmp_path):
    data = good_data()
    del data["seed"], data["test"]
    data["extra"] = 1

    with pytest.raises(SplitError) as error:
        load_split(write_data(tmp_path, data))

    assert str(error.value) == "seed: 필드가 없다; test: 필드가 없다; extra: 모르는 필드다"


def test_FR16_load_split_rejects_unreadable_files(tmp_path):
    with pytest.raises(SplitError, match="읽을 수 없다") as error:
        load_split(tmp_path / "missing.json")
    assert str(error.value).startswith(f"{tmp_path / 'missing.json'}: 읽을 수 없다 (")

    with pytest.raises(SplitError, match="UTF-8 JSON으로 읽을 수 없다"):
        load_split(write_data(tmp_path, "{이것은 JSON이 아니다"))

    cp949 = tmp_path / "cp949.json"
    cp949.write_bytes(json.dumps(good_data(), ensure_ascii=False).encode("cp949"))  # 한글 사유가 UTF-8이 아니다
    with pytest.raises(SplitError, match="UTF-8 JSON으로 읽을 수 없다"):
        load_split(cp949)

    for text in ("[]", "null", '"split"'):
        with pytest.raises(SplitError, match="최상위가 JSON 객체여야 한다"):
            load_split(write_data(tmp_path, text))
