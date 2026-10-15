"""수집 계획 파일 읽기와 검증, 테스트 fixture (FR-16). 합성 계획 파일과 임시 파일로 확인한다."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import tools.data_collection as data_collection
from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import AdminWriteResult
from tools.data_collection import CollectionPlan, PlanError, PlannedRun, load_plan

_DROP = object()


def _edit(path: tuple[str | int, ...], value: Any) -> Callable[[dict, Callable[..., Path]], Path]:
    """sample 계획의 `path` 위치를 value로 바꾼 계획 파일을 만드는 함수. value가 `_DROP`이면 그 키를 지운다.

    없는 키를 가리키면 키를 더한다.
    """

    def make(plan_data: dict, write_plan: Callable[..., Path]) -> Path:
        target = plan_data
        for key in path[:-1]:
            target = target[key]
        if value is _DROP:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return write_plan(plan_data)

    return make


def _text(text: str) -> Callable[[dict, Callable[..., Path]], Path]:
    """`text`를 그대로 쓴 계획 파일을 만드는 함수."""
    return lambda plan_data, write_plan: write_plan(text)


def _bytes(content: bytes) -> Callable[[dict, Callable[..., Path]], Path]:
    """`content`를 그대로 쓴 계획 파일을 만드는 함수. UTF-8이 아닌 내용을 만들 때 쓴다."""

    def make(plan_data: dict, write_plan: Callable[..., Path]) -> Path:
        path = write_plan("")
        path.write_bytes(content)
        return path

    return make


def _missing(plan_data: dict, write_plan: Callable[..., Path]) -> Path:
    """계획 파일을 쓰고, 같은 디렉터리의 없는 파일을 가리킨다."""
    return write_plan(plan_data).with_name("missing.json")


def test_FR16_plan_file_is_loaded(sample_plan_path, plan_data, beacon):
    assert type(plan_data["settle_sec"]) is int and type(plan_data["run_sec"]) is float  # 정수 시간 값도 받는다

    plan = load_plan(sample_plan_path)

    assert isinstance(plan, CollectionPlan)
    assert plan.place_id == "place-a"
    assert plan.beacon == beacon
    assert (plan.settle_sec, type(plan.settle_sec)) == (5.0, float)
    assert (plan.run_sec, type(plan.run_sec)) == (30.0, float)
    assert isinstance(plan.runs, tuple)
    assert plan.runs == (
        PlannedRun("locker-1", "앞 위치, 손에 들고 화면 위"),
        PlannedRun("none", "locker-1 뒤 2 m"),
        PlannedRun("locker-2", "앞 위치에서 왼쪽 0.2 m, 손에 들고 화면 위"),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.run_sec = 1.0


def test_FR16_plan_accepts_boundary_values_and_repeated_labels(plan_data, write_plan):
    plan_data["beacon"].update(major=0, minor=65535)  # iBeacon 필드 범위의 양 끝
    plan_data["settle_sec"] = 0  # 기다리지 않는다
    plan_data["run_sec"] = 0.001  # 0보다 크면 된다
    plan_data["runs"] = [
        {"label": "locker-1", "spot": "앞 위치"},
        {"label": "locker-1", "spot": "앞 위치에서 왼쪽 0.2 m"},  # 같은 라벨이 연이어도 읽는다
    ]

    plan = load_plan(write_plan(plan_data))

    assert (plan.beacon.major, plan.beacon.minor) == (0, 65535)
    assert (plan.settle_sec, plan.run_sec) == (0.0, 0.001)
    assert [run.label for run in plan.runs] == ["locker-1", "locker-1"]


# (계획 파일을 만드는 함수, 오류 메시지에 들어 있어야 하는 말). 말이 같이 맞아야 엉뚱한 이유로 통과하지 않는다.
INVALID_PLANS = [
    # 파일
    pytest.param(_missing, "missing.json", id="missing_file"),
    pytest.param(_text("{not json"), "JSON", id="not_json"),
    pytest.param(_bytes(b"\xff\xfe{}"), "JSON", id="not_utf8"),
    pytest.param(_text("[]"), "JSON 객체", id="top_level_array"),
    # 키
    pytest.param(_edit(("run_sec",), _DROP), "run_sec: 필드가 없다", id="missing_top_key"),
    pytest.param(_edit(("run_secs",), 30.0), "run_secs: 모르는 필드다", id="unknown_top_key"),
    pytest.param(_edit(("beacon", "uuid"), _DROP), "beacon.uuid: 필드가 없다", id="missing_beacon_key"),
    pytest.param(_edit(("beacon", "mayor"), 1), "beacon.mayor: 모르는 필드다", id="unknown_beacon_key"),
    pytest.param(_edit(("runs", 1, "spot"), _DROP), "runs[1].spot: 필드가 없다", id="missing_run_key"),
    pytest.param(_edit(("runs", 1, "spots"), "x"), "runs[1].spots: 모르는 필드다", id="unknown_run_key"),
    # 형식
    pytest.param(_edit(("runs",), []), "runs", id="empty_runs"),
    pytest.param(_edit(("runs",), {"label": "locker-1", "spot": "앞 위치"}), "runs", id="runs_not_array"),
    pytest.param(_edit(("runs", 2), "locker-2"), "runs[2]", id="run_not_object"),
    pytest.param(_edit(("beacon",), "x"), "beacon", id="beacon_not_object"),
    # 문자열
    pytest.param(_edit(("runs", 0, "label"), ""), "runs[0].label", id="empty_label"),
    pytest.param(_edit(("runs", 0, "label"), " locker-1"), "runs[0].label", id="label_leading_space"),
    pytest.param(_edit(("runs", 0, "label"), "locker-1 "), "runs[0].label", id="label_trailing_space"),
    pytest.param(_edit(("runs", 0, "label"), 1), "runs[0].label", id="label_not_string"),
    pytest.param(_edit(("runs", 2, "spot"), ""), "runs[2].spot", id="empty_spot"),
    pytest.param(_edit(("runs", 2, "spot"), None), "runs[2].spot", id="spot_not_string"),
    pytest.param(_edit(("place_id",), ""), "place_id", id="empty_place_id"),
    pytest.param(_edit(("place_id",), 1), "place_id", id="place_id_not_string"),
    pytest.param(_edit(("beacon", "uuid"), ""), "beacon.uuid", id="empty_uuid"),
    # 비콘 번호
    pytest.param(_edit(("beacon", "major"), -1), "beacon.major", id="major_negative"),
    pytest.param(_edit(("beacon", "minor"), 65536), "beacon.minor", id="minor_too_large"),
    pytest.param(_edit(("beacon", "major"), True), "beacon.major", id="major_bool"),
    pytest.param(_edit(("beacon", "major"), 1.0), "beacon.major", id="major_float"),
    pytest.param(_edit(("beacon", "minor"), "1"), "beacon.minor", id="minor_string"),
    # 시간
    pytest.param(_edit(("settle_sec",), -1), "settle_sec", id="settle_sec_negative"),
    pytest.param(_edit(("settle_sec",), True), "settle_sec", id="settle_sec_bool"),
    pytest.param(_edit(("settle_sec",), "5"), "settle_sec", id="settle_sec_string"),
    pytest.param(_edit(("run_sec",), 0), "run_sec", id="run_sec_zero"),
    pytest.param(_edit(("run_sec",), -1), "run_sec", id="run_sec_negative"),
    pytest.param(_edit(("run_sec",), float("nan")), "run_sec", id="run_sec_nan"),  # JSON 리터럴 NaN
    pytest.param(_edit(("run_sec",), float("inf")), "run_sec", id="run_sec_infinity"),
    pytest.param(_edit(("run_sec",), 10**400), "run_sec", id="run_sec_int_too_large_for_float"),
    pytest.param(_edit(("run_sec",), True), "run_sec", id="run_sec_bool"),
    pytest.param(_edit(("run_sec",), "30"), "run_sec", id="run_sec_string"),
]


@pytest.mark.parametrize(("make_file", "expected"), INVALID_PLANS)
def test_FR16_invalid_plan_raises(plan_data, write_plan, make_file, expected):
    path = make_file(plan_data, write_plan)

    with pytest.raises(PlanError, match=re.escape(expected)):
        load_plan(path)


def test_FR16_plan_error_is_value_error():
    assert issubclass(PlanError, ValueError)


def test_FR16_package_exports_plan_api():
    # 뒤 단계가 이름을 더하므로 포함하는지만 본다
    assert {"CollectionPlan", "PlanError", "PlannedRun", "load_plan"} <= set(data_collection.__all__)


def test_FR16_fake_repository_and_io(fake_repo, make_io, beacon, t0):
    ok = AdminWriteResult(AdminWriteOutcome.APPLIED, AdminWriteReason.OK)
    overlap = AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.COLLECTION_OVERLAP)
    conflict = AdminWriteResult(AdminWriteOutcome.CONFLICT, AdminWriteReason.STATE_CONFLICT)
    t1 = t0 + timedelta(seconds=30)

    # fake 저장소: 결과 큐가 비면 APPLIED/OK이고, 호출은 기록한다
    assert fake_repo.start_collection("col-1", beacon, "locker-1", t0) == ok
    assert fake_repo.end_collection("col-1", t1) == ok
    assert fake_repo.calls == [("start", "col-1", beacon, "locker-1", t0), ("end", "col-1", t1)]

    # 결과는 호출마다 맨 앞을 꺼낸다. 시작과 끝의 큐는 따로다. 예외는 던지되 호출은 기록한다
    fake_repo.start_results.extend([overlap, RuntimeError("db down")])
    fake_repo.end_results.append(conflict)
    assert fake_repo.start_collection("col-2", beacon, "none", t0) == overlap
    with pytest.raises(RuntimeError, match="db down"):
        fake_repo.start_collection("col-3", beacon, "none", t0)
    assert fake_repo.start_collection("col-4", beacon, "none", t0) == ok
    assert fake_repo.end_collection("col-2", t1) == conflict
    assert fake_repo.end_collection("col-2", t1) == ok
    assert fake_repo.calls[2:] == [
        ("start", "col-2", beacon, "none", t0),
        ("start", "col-3", beacon, "none", t0),
        ("start", "col-4", beacon, "none", t0),
        ("end", "col-2", t1),
        ("end", "col-2", t1),
    ]
    assert fake_repo.start_results == [] and fake_repo.end_results == []

    # 다른 메서드는 두지 않는다
    methods = {name for name in dir(fake_repo) if not name.startswith("_") and callable(getattr(fake_repo, name))}
    assert methods == {"start_collection", "end_collection"}

    # FakeIO의 시계: 시각은 sleep으로만 간다. 2번째(0부터) sleep은 시각을 늘리지 않고 KeyboardInterrupt를 던진다
    io = make_io(["", "q", KeyboardInterrupt()], interrupt_on_sleep=2)
    assert io.clock() == t0
    io.sleep(5)
    io.sleep(1.5)
    assert io.now == t0 + timedelta(seconds=6.5)
    with pytest.raises(KeyboardInterrupt):
        io.sleep(3)
    assert io.now == t0 + timedelta(seconds=6.5)
    io.sleep(1)  # 중단은 그 한 번뿐이다
    assert io.now == t0 + timedelta(seconds=7.5)
    assert io.sleeps == [5, 1.5, 3, 1]  # 중단된 sleep도 기록한다

    # FakeIO의 입력: 맨 앞부터 꺼낸다. 예외는 던진다. 다 떨어지면 EOFError다
    assert io.read_line() == ""
    assert io.read_line() == "q"
    with pytest.raises(KeyboardInterrupt):
        io.read_line()
    with pytest.raises(EOFError):
        io.read_line()

    # FakeIO의 출력
    io.write("회차 1/3")
    io.write("Enter를 누른다")
    assert io.outputs == ["회차 1/3", "Enter를 누른다"]
    assert io.text == "회차 1/3\nEnter를 누른다"

    # start 와 interrupt_on_sleep 의 기본값. 넘겨받은 입력 목록은 건드리지 않는다
    inputs = ["", ""]
    quiet = make_io(inputs, start=t1)
    assert quiet.clock() == t1
    quiet.sleep(1)
    quiet.sleep(2)
    assert (quiet.now, quiet.sleeps) == (t1 + timedelta(seconds=3), [1, 2])
    quiet.read_line()
    assert inputs == ["", ""]
