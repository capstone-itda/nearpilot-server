"""좌표표와 수집 회차 기록 파일 읽기 (FR-09, FR-16, specs/db-evaluation-distance-order/spec.md 6절 결정 8, 9).

좌표표는 지표 10(거리 순서)의 정답 순서를 정한다. 오류 메시지는 잘못된 필드 경로(좌표표)나 `파일:줄 번호`(기록 파일)를 쓴다.
기록 파일의 줄은 `collect run`이 쓰는 모양 그대로 만들고, 이 모듈이 읽지 않는 키(`label`, `status` 등)도 함께 넣는다.
단일 문제 사례는 메시지 전체를 비교해서 검사가 다른 문제를 덩달아 내지 않는지도 본다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nearpilot.cli.seed import load_seed_file
from tools.data_collection.plan import load_plan
from tools.evaluation.layout import (
    Layout,
    LayoutError,
    check_layout_covers,
    load_layout,
    load_run_spots,
    require_run_spots,
)

CONFIG = Path(__file__).resolve().parents[2] / "config"

# P1은 두 기기에서 모두 3-4-5 삼각형의 빗변(5.0)이고, P2는 locker-1까지 6, locker-2까지 8이다 (정수 좌표도 받는다)
LAYOUT_TEXT = """\
place_id = "place-a"

[nodes]
"locker-1" = [0.0, 0.0]
"locker-2" = [6, 8]

[spots]
P1 = [3.0, 4.0]
P2 = [6, 0]
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def record(run_id: str, spot: str, **extra) -> str:
    """`collect run`이 회차마다 쓰는 한 줄(JSON 객체). 이 모듈이 읽지 않는 키도 그대로 들어 있다."""
    entry = {
        "index": 1,
        "collection_id": run_id,
        "place_id": "place-a",
        "beacon": {"uuid": "11111111-2222-3333-4444-555555555555", "major": 1, "minor": 1},
        "label": "locker-1",
        "spot": spot,
        "started_at": "2026-10-28T01:00:00+00:00",
        "ended_at": "2026-10-28T01:00:30+00:00",
        "status": "done",
    }
    entry.update(extra)
    return json.dumps(entry, ensure_ascii=False)


# ── 좌표표 ────────────────────────────────────────────────────────────


def test_FR09_layout_loads_nodes_and_spots(tmp_path):
    layout = load_layout(write(tmp_path, "layout.toml", LAYOUT_TEXT))

    assert layout == Layout(
        place_id="place-a",
        nodes={"locker-1": (0.0, 0.0), "locker-2": (6.0, 8.0)},
        spots={"P1": (3.0, 4.0), "P2": (6.0, 0.0)},
    )
    # 정수로 적어도 float다
    assert all(type(value) is float for point in (*layout.nodes.values(), *layout.spots.values()) for value in point)
    # 거리는 서는 위치와 기기의 유클리드 거리(m)다
    assert layout.distance("P1", "locker-1") == 5.0  # 3-4-5 삼각형
    assert layout.distance("P1", "locker-2") == 5.0  # (6-3, 8-4)도 3-4-5다
    assert layout.distance("P2", "locker-1") == 6.0
    assert layout.distance("P2", "locker-2") == 8.0
    # 없는 이름이면 KeyError다. 서는 위치와 기기의 이름이 서로 바뀌어도 거리를 내지 않는다
    for spot, node_id in (("P9", "locker-1"), ("P1", "locker-9"), ("locker-1", "P1")):
        with pytest.raises(KeyError):
            layout.distance(spot, node_id)
    with pytest.raises(AttributeError):  # 읽은 좌표표는 바꿀 수 없다
        layout.place_id = "place-b"  # type: ignore[misc]


def test_FR09_layout_reads_korean_names_and_comments(tmp_path):
    # 사람이 주석을 달아 고치는 파일이다. 수집 계획의 spot은 한글에 쉼표가 들어갈 수 있다
    text = "# 좌표표 — 단위는 m\n" + LAYOUT_TEXT.replace("P2 = [6, 0]", '"locker-1 앞 위치, 손에 들고" = [0.0, 0.6]  # 앞')
    layout = load_layout(write(tmp_path, "layout.toml", text))

    assert layout.spots["locker-1 앞 위치, 손에 들고"] == (0.0, 0.6)
    assert layout.distance("locker-1 앞 위치, 손에 들고", "locker-1") == 0.6


# (고칠 부분, 바꾼 값, 기대하는 문제 목록). 문제 하나만 나와야 한다.
BAD_LAYOUTS = {
    "missing_place_id": ('place_id = "place-a"\n', "", ["place_id: 필드가 없다"]),
    "missing_nodes": ('[nodes]\n"locker-1" = [0.0, 0.0]\n"locker-2" = [6, 8]\n\n', "", ["nodes: 필드가 없다"]),
    "missing_spots": ("\n[spots]\nP1 = [3.0, 4.0]\nP2 = [6, 0]\n", "", ["spots: 필드가 없다"]),
    "unknown_key": ('place_id = "place-a"\n', 'place_id = "place-a"\nunit = "m"\n', ["unit: 모르는 필드다"]),
    "misspelled_key": ('place_id = "place-a"', 'place = "place-a"',
                       ["place_id: 필드가 없다", "place: 모르는 필드다"]),
    "empty_place_id": ('place_id = "place-a"', 'place_id = ""', ["place_id: 비어 있지 않은 문자열이어야 한다 ('')"]),
    "place_id_number": ('place_id = "place-a"', "place_id = 1", ["place_id: 비어 있지 않은 문자열이어야 한다 (1)"]),
    "empty_nodes": ('"locker-1" = [0.0, 0.0]\n"locker-2" = [6, 8]\n', "", ["nodes: 비어 있지 않은 표여야 한다 ({})"]),
    "empty_spots": ("P1 = [3.0, 4.0]\nP2 = [6, 0]\n", "", ["spots: 비어 있지 않은 표여야 한다 ({})"]),
    "nodes_not_table": ('[nodes]\n"locker-1" = [0.0, 0.0]\n"locker-2" = [6, 8]\n\n[spots]',
                        '[[nodes]]\nid = 1\n\n[spots]', ["nodes: 비어 있지 않은 표여야 한다 ([{'id': 1}])"]),
    "coordinate_one_number": ('"locker-1" = [0.0, 0.0]', '"locker-1" = [1.0]',
                              ["nodes.locker-1: 수 두 개의 배열이어야 한다 ([1.0])"]),
    "coordinate_three_numbers": ("P1 = [3.0, 4.0]", "P1 = [3.0, 4.0, 5.0]",
                                 ["spots.P1: 수 두 개의 배열이어야 한다 ([3.0, 4.0, 5.0])"]),
    "coordinate_string": ('"locker-1" = [0.0, 0.0]', '"locker-1" = "0.0, 0.0"',
                          ["nodes.locker-1: 수 두 개의 배열이어야 한다 ('0.0, 0.0')"]),
    "coordinate_table": ('"locker-1" = [0.0, 0.0]', '"locker-1" = {x = 0.0, y = 0.0}',
                         ["nodes.locker-1: 수 두 개의 배열이어야 한다 ({'x': 0.0, 'y': 0.0})"]),
    "coordinate_item_string": ('"locker-1" = [0.0, 0.0]', '"locker-1" = ["0.0", 0.0]',
                               ["nodes.locker-1[0]: 숫자여야 한다 ('0.0')"]),
    "coordinate_inf": ('"locker-1" = [0.0, 0.0]', '"locker-1" = [inf, 0.0]',
                       ["nodes.locker-1[0]: 유한한 숫자여야 한다 (inf)"]),
    "coordinate_nan": ("P1 = [3.0, 4.0]", "P1 = [3.0, nan]", ["spots.P1[1]: 유한한 숫자여야 한다 (nan)"]),
    "coordinate_huge_int": ("P1 = [3.0, 4.0]", f"P1 = [{10 ** 400}, 4.0]",
                            [f"spots.P1[0]: 유한한 숫자여야 한다 ({10 ** 400!r:.40})"]),
    "coordinate_bool": ('"locker-1" = [0.0, 0.0]', '"locker-1" = [true, 0.0]',
                        ["nodes.locker-1[0]: 숫자여야 한다 (True)"]),
}


@pytest.mark.parametrize("old, new, problems", list(BAD_LAYOUTS.values()), ids=list(BAD_LAYOUTS))
def test_FR09_layout_rejects_bad_file(tmp_path, old, new, problems):
    assert old in LAYOUT_TEXT  # 고칠 부분이 없으면 이 사례는 아무것도 검사하지 못한다
    with pytest.raises(LayoutError) as error:
        load_layout(write(tmp_path, "layout.toml", LAYOUT_TEXT.replace(old, new)))
    assert str(error.value) == "; ".join(problems)


def test_FR09_layout_rejects_unreadable_file(tmp_path):
    missing = tmp_path / "missing.toml"
    with pytest.raises(LayoutError) as error:
        load_layout(missing)
    assert str(error.value).startswith(f"{missing}: 읽을 수 없다 (")

    broken = write(tmp_path, "broken.toml", '[nodes\n"locker-1" = ')
    with pytest.raises(LayoutError) as error:
        load_layout(broken)
    assert str(error.value).startswith(f"{broken}: UTF-8 TOML로 읽을 수 없다 (")

    not_utf8 = tmp_path / "cp949.toml"
    not_utf8.write_bytes("# 좌표표\n".encode("cp949"))
    with pytest.raises(LayoutError) as error:
        load_layout(not_utf8)
    assert str(error.value).startswith(f"{not_utf8}: UTF-8 TOML로 읽을 수 없다 (")


def test_FR09_layout_reports_every_problem(tmp_path):
    text = """\
unit = "m"

[nodes]
"locker-1" = [0.0]
"locker-2" = [6, 8]
"locker-3" = [inf, 0.0]

[spots]
P1 = "3, 4"
P2 = [true, 0]
"""
    with pytest.raises(LayoutError) as error:
        load_layout(write(tmp_path, "layout.toml", text))

    assert str(error.value).split("; ") == [
        "place_id: 필드가 없다",
        "unit: 모르는 필드다",
        "nodes.locker-1: 수 두 개의 배열이어야 한다 ([0.0])",
        "nodes.locker-3[0]: 유한한 숫자여야 한다 (inf)",
        "spots.P1: 수 두 개의 배열이어야 한다 ('3, 4')",
        "spots.P2[0]: 숫자여야 한다 (True)",
    ]


def test_FR09_layout_example_is_valid():
    layout = load_layout(CONFIG / "layouts" / "example.toml")

    assert layout.place_id and layout.nodes and layout.spots  # 비어 있는 파일이 읽히는 것은 아니다
    # 예시 시드의 장소·기기와 예시 수집 계획의 spot을 글자까지 같게 담는다 (README의 명령 흐름이 예시 파일만으로 이어진다)
    seed = load_seed_file(CONFIG / "seed" / "example.toml")
    plan = load_plan(CONFIG / "plans" / "example.json")
    place = next(place for place in seed.places if place.place_id == plan.place_id)
    assert layout.place_id == plan.place_id
    check_layout_covers(layout, [run.spot for run in plan.runs], place.nodes)


# ── 회차 기록 파일 ────────────────────────────────────────────────────


def test_FR09_records_map_run_to_spot(tmp_path):
    first = write(tmp_path, "a.jsonl", "\n".join([
        record("run-1", "P1"),
        "",  # 빈 줄은 건너뛴다
        record("run-2", "locker-2 앞 위치, 손에 들고", label=None, status=["읽지 않는 키"], beacon="읽지 않는 키"),
        "   ",
        record("run-4", "줄 바꿈 글자가 든 spot"),  # 줄은 \n으로만 나눈다. U+2028은 JSON 문자열 안의 글자다
    ]) + "\n")
    # 같은 회차가 다른 파일에 같은 spot으로 한 번 더 나온다. Windows의 `collect run`은 줄을 \r\n으로 쓴다
    second_lines = [record("run-2", "locker-2 앞 위치, 손에 들고"), record("run-3", "P1")]
    second = tmp_path / "b.jsonl"
    second.write_bytes(("\r\n".join(second_lines) + "\r\n").encode("utf-8"))

    assert load_run_spots([first, second]) == {
        "run-1": "P1",
        "run-2": "locker-2 앞 위치, 손에 들고",
        "run-4": "줄 바꿈 글자가 든 spot",
        "run-3": "P1",
    }
    # 같은 파일을 두 번 줘도, 파일을 하나도 주지 않아도 된다
    assert load_run_spots([first, first]) == {
        "run-1": "P1",
        "run-2": "locker-2 앞 위치, 손에 들고",
        "run-4": "줄 바꿈 글자가 든 spot",
    }
    assert load_run_spots([]) == {}


# 줄 번호가 3이 되도록 앞에 좋은 줄과 빈 줄을 둔다. (나쁜 줄, 기대하는 메시지 뒷부분)
BAD_RECORD_LINES = {
    "not_json": ("이건 JSON이 아니다", "JSON으로 읽을 수 없다 ("),
    "truncated_json": ('{"collection_id": "run-9", "spot"', "JSON으로 읽을 수 없다 ("),
    "json_array": ('["run-9", "P1"]', "JSON 객체여야 한다 (['run-9', 'P1'])"),
    "json_string": ('"run-9"', "JSON 객체여야 한다 ('run-9')"),
    "missing_collection_id": ('{"spot": "P1"}', "collection_id: 필드가 없다"),
    "missing_spot": ('{"collection_id": "run-9"}', "spot: 필드가 없다"),
    "collection_id_number": ('{"collection_id": 9, "spot": "P1"}', "collection_id: 비어 있지 않은 문자열이어야 한다 (9)"),
    "collection_id_null": ('{"collection_id": null, "spot": "P1"}',
                           "collection_id: 비어 있지 않은 문자열이어야 한다 (None)"),
    "spot_number": ('{"collection_id": "run-9", "spot": 1}', "spot: 비어 있지 않은 문자열이어야 한다 (1)"),
    "spot_empty": ('{"collection_id": "run-9", "spot": ""}', "spot: 비어 있지 않은 문자열이어야 한다 ('')"),
    "both_wrong": ('{"spot": 1}', "collection_id: 필드가 없다; spot: 비어 있지 않은 문자열이어야 한다 (1)"),
}


@pytest.mark.parametrize("line, message", list(BAD_RECORD_LINES.values()), ids=list(BAD_RECORD_LINES))
def test_FR09_records_reject_bad_lines(tmp_path, line, message):
    other = write(tmp_path, "other.jsonl", record("run-0", "P1") + "\n")
    path = write(tmp_path, "records.jsonl", "\n".join([record("run-1", "P1"), "", line, record("run-2", "P2")]) + "\n")

    with pytest.raises(LayoutError) as error:
        load_run_spots([other, path])  # 앞 파일은 읽히고, 나쁜 줄이 있는 파일과 줄 번호를 알린다

    assert str(error.value).startswith(f"{path}:3: ")
    assert str(error.value).removeprefix(f"{path}:3: ").startswith(message)


def test_FR09_records_reject_unreadable_file(tmp_path):
    good = write(tmp_path, "good.jsonl", record("run-1", "P1") + "\n")
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(LayoutError) as error:
        load_run_spots([good, missing])
    assert str(error.value).startswith(f"{missing}: 읽을 수 없다 (")

    not_utf8 = tmp_path / "cp949.jsonl"
    not_utf8.write_bytes(record("run-2", "서는 위치").encode("cp949"))
    with pytest.raises(LayoutError) as error:
        load_run_spots([good, not_utf8])
    assert str(error.value).startswith(f"{not_utf8}: UTF-8로 읽을 수 없다 (")


def test_FR09_records_conflicting_spot_rejected(tmp_path):
    first = write(tmp_path, "a.jsonl", "\n".join([record("run-1", "P1"), record("run-2", "P2")]) + "\n")
    second = write(tmp_path, "b.jsonl", "\n".join([record("run-3", "P1"), record("run-2", "P3")]) + "\n")

    # 같은 회차가 다른 파일에 다른 spot으로 있다. 두 값과 두 위치를 쓴다
    with pytest.raises(LayoutError) as error:
        load_run_spots([first, second])
    assert str(error.value) == f"{second}:2: 회차 run-2의 spot이 'P3'이다 ({first}:2에서는 'P2')"

    # 같은 파일 안에서도 마찬가지다
    same_file = write(tmp_path, "c.jsonl", "\n".join([record("run-1", "P1"), "", record("run-1", "P2")]) + "\n")
    with pytest.raises(LayoutError) as error:
        load_run_spots([same_file])
    assert str(error.value) == f"{same_file}:3: 회차 run-1의 spot이 'P2'이다 ({same_file}:1에서는 'P1')"


def test_FR09_records_missing_run_rejected():
    spots = {"run-1": "P1", "run-2": "P2", "run-3": "P1"}

    # 모두 있으면 그 회차들만 담는다. 순서는 run_ids를 따르고, 기록에 더 있는 회차는 담지 않는다
    assert require_run_spots(spots, ["run-3", "run-1"]) == {"run-3": "P1", "run-1": "P1"}
    assert list(require_run_spots(spots, iter(["run-3", "run-1", "run-3"]))) == ["run-3", "run-1"]
    assert require_run_spots(spots, []) == {}

    # 없는 회차는 모두 모아 하나로 낸다. 정렬하고 같은 ID는 한 번만 쓴다
    with pytest.raises(LayoutError) as error:
        require_run_spots(spots, ["run-1", "run-9", "run-7", "run-9"])
    assert str(error.value) == "기록 파일에 회차 run-7, run-9가 없다"


# ── 좌표표와 쓰는 이름의 대조 ─────────────────────────────────────────


def test_FR09_layout_missing_names_rejected(tmp_path):
    layout = load_layout(write(tmp_path, "layout.toml", LAYOUT_TEXT))

    # 모두 있으면 아무 일도 없다. 좌표표가 더 많이 가져도 괜찮다
    assert check_layout_covers(layout, ["P1", "P2", "P1"], ["locker-1", "locker-2"]) is None
    assert check_layout_covers(layout, ["P1"], ["locker-2"]) is None
    assert check_layout_covers(layout, [], []) is None

    # 없는 spot 둘과 기기 하나를 모두 모아 하나로 낸다. 정렬하고, 같은 이름은 한 번만 쓰고, 쉼표가 든 이름도 가를 수 있다
    with pytest.raises(LayoutError) as error:
        check_layout_covers(layout, iter(["P1", "P9", "locker-3 앞, 손에 들고", "P9"]), iter(["locker-1", "locker-8"]))
    assert str(error.value) == "좌표표에 없는 spot: 'P9', 'locker-3 앞, 손에 들고'; 좌표표에 없는 기기: 'locker-8'"

    # 한쪽만 없으면 그쪽만 쓴다
    with pytest.raises(LayoutError) as error:
        check_layout_covers(layout, ["P1"], ["locker-9", "locker-1", "locker-5"])
    assert str(error.value) == "좌표표에 없는 기기: 'locker-5', 'locker-9'"
    with pytest.raises(LayoutError) as error:
        check_layout_covers(layout, ["P3"], ["locker-1"])
    assert str(error.value) == "좌표표에 없는 spot: 'P3'"
