"""NFR-06 지표 2: 시험 회차 표본의 비용 비율별 재질문률·오작동률 곡선 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

`reask_curve`는 `Sample`을 손으로 만들어 손으로 센 값과 맞춘다. 임계값 `r / (1 + r)`의 경계에 놓는 확률(0.5, 0.75)은 이진수로 정확해서
"확률이 임계값과 같으면 구동한다"를 부동소수점 오차 없이 확인한다. `collect_samples`가 만든 표본으로 돌리는 시험은 하나다:
기기 둘(a, b)의 작은 모델과 손으로 정한 RSSI 회차를 쓰고, 모델은 기기마다 자기 앞 −50 dBm, 다른 기기 앞 −70 dBm,
어느 기기 앞도 아님 −90 dBm을 기대하며 흔들림이 2 dB라서 정답이 아닌 상태의 확률은 0으로 떨어진다 (`test_evaluation_proximity.py`와 같은 모델).
"""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.core.proximity.fit import LabeledRun
from nearpilot.core.proximity.model import NodeModel, ProximityModel
from nearpilot.shared.models import BeaconKey, RssiFrame
from tools.evaluation.proximity import EvaluationError, Sample, collect_samples
from tools.evaluation.report import write_report
from tools.evaluation.reask import CurvePoint, reask_curve, reask_rows

COLUMNS = ["metric", "value", "limit", "passed", "trials", "failures", "note"]
GENERATED_AT = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("11111111-2222-3333-4444-555555555555", 1, 1)
MODEL = ProximityModel(
    place_id="place-a", model_version="v1", calib_version="c1", window_sec=3.0, stay=0.9, min_distance_m=0.3,
    calibration_method="none", validation={},
    nodes={
        node_id: NodeModel(node_id, x, 0.0, front, 0.0, A=-50.0, n=2.0, sigma=2.0, none_rssi=-90.0, none_sigma=2.0)
        for node_id, x, front in (("a", 0.0, 1.0), ("b", 11.0, 10.0))
    },
)
LEVELS = {  # 라벨 → 앵커마다 받는 RSSI
    "a": {"a": -50.0, "b": -70.0},
    "b": {"a": -70.0, "b": -50.0},
    "none": {"a": -90.0, "b": -90.0},
}


def sample(label: str, **probabilities: float) -> Sample:
    return Sample("run", label, probabilities)


# 주석은 서버가 고를 기기, 그 확률, 라벨과 맞는지다.
KNOWN = [
    sample("a", a=0.92, b=0.05, none=0.03),  # a, 0.92, 맞다
    sample("a", a=0.80, b=0.15, none=0.05),  # a, 0.80, 맞다
    sample("b", a=0.80, b=0.15, none=0.05),  # a, 0.80, 라벨 b와 다르다 — 확신하는 오선택
    sample("b", a=0.20, b=0.75, none=0.05),  # b, 0.75, 맞다. r = 3의 임계값 0.75와 같다
    sample("b", a=0.75, b=0.20, none=0.05),  # a, 0.75, 라벨 b와 다르다. r = 3의 임계값 0.75와 같다
    sample("none", a=0.55, b=0.05, none=0.40),  # a, 0.55, 라벨이 none이라 구동하면 오작동
    sample("none", a=0.10, b=0.20, none=0.70),  # b, 0.20. none의 확률 0.70은 기기를 고르는 확률이 아니다
    sample("a", a=0.50, b=0.50, none=0.00),  # 같아서 사전순으로 앞선 a, 0.50, 맞다. r = 1의 임계값 0.5와 같다
]


def read_report(path: Path) -> list[dict[str, str]]:
    """보고서 CSV의 지표 행."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return list(csv.DictReader(line for line in lines if not line.startswith("#")))


def make_run(run_id: str, label: str, levels: Mapping[str, float], *, seconds: int = 20) -> LabeledRun:
    """앵커마다 1초에 하나씩(t + 0.5초에) `levels`의 RSSI를 받은 회차."""
    frames = []
    for node_id, rssi in levels.items():
        for t in range(seconds):
            at = T0 + timedelta(seconds=t + 0.5)
            frames.append(RssiFrame(node_id, BEACON, rssi, at, f"{run_id}-{node_id}", t + 1, at, at))
    return LabeledRun(run_id, label, BEACON, T0, T0 + timedelta(seconds=seconds), frames)


# ── reask_curve ───────────────────────────────────────────────────────


def test_NFR06_reask_curve_known_samples():
    points = reask_curve(KNOWN, [1, 3, 9, 19])

    # 구동하는 표본은 기기 확률의 최댓값이 임계값 이상이다 (r = 1: 0.5, r = 3: 0.75, r = 9: 0.9, r = 19: 0.95).
    # r = 1: 0.20만 재질문. 구동한 일곱 가운데 오선택 둘(0.80, 0.75)과 none 라벨(0.55)이 오작동이다.
    # r = 3: 0.55, 0.20, 0.50이 재질문. 구동한 0.92, 0.80, 0.80, 0.75, 0.75 가운데 오선택 둘(0.80, 0.75)이 오작동이다.
    #        임계값과 같은 0.75 둘도 구동하고, 하나는 맞고 하나는 오작동이다.
    # r = 9: 0.92만 구동하고 맞다. r = 19: 0.92도 0.95 미만이라 모두 재질문한다.
    expected = [  # (비용 비율, 임계값, 재질문률, 오작동률)
        (1, 0.5, 1 / 8, 3 / 8),
        (3, 0.75, 3 / 8, 2 / 8),
        (9, 0.9, 7 / 8, 0.0),
        (19, 0.95, 1.0, 0.0),
    ]
    assert len(points) == len(expected)
    for point, values in zip(points, expected):
        assert (point.cost_ratio, point.threshold, point.reask_rate, point.misoperation_rate) == pytest.approx(values)
    # 임계값이 오를수록 구동하는 표본이 줄어서 재질문률은 줄지 않고 오작동률은 늘지 않는다
    assert [p.reask_rate for p in points] == sorted(p.reask_rate for p in points)
    assert [p.misoperation_rate for p in points] == sorted((p.misoperation_rate for p in points), reverse=True)


def test_NFR06_reask_curve_none_label_counts_any_execution_as_misoperation():
    samples = [
        sample("a", a=0.9, b=0.05, none=0.05),  # a, 0.9, 맞다
        sample("none", a=0.9, b=0.05, none=0.05),  # a, 0.9, 어느 기기 앞도 아니라서 어느 기기를 구동해도 오작동
    ]

    executed, asked = reask_curve(samples, [1, 19])  # 임계값 0.5, 0.95

    assert (executed.reask_rate, executed.misoperation_rate) == (0.0, 0.5)  # 분모는 none 라벨 표본까지 둘이다
    assert (asked.reask_rate, asked.misoperation_rate) == (1.0, 0.0)


@pytest.mark.parametrize("label, misoperation", [("a", 0.0), ("b", 1.0)])
def test_NFR06_reask_curve_ties_pick_the_first_node_id(label, misoperation):
    probabilities = {"a": 0.5, "b": 0.5, "none": 0.0}

    # 기기 확률이 같으면 node_id 사전순으로 앞선 a를 고른다. 딕셔너리에 넣은 순서가 결과를 바꾸지 못한다
    for items in (list(probabilities.items()), list(reversed(probabilities.items()))):
        (point,) = reask_curve([Sample("run", label, dict(items))], [1])
        assert (point.reask_rate, point.misoperation_rate) == (0.0, misoperation)


def test_NFR06_reask_curve_follows_cost_ratio_order():
    points = reask_curve(KNOWN, [19, 1, 3.5])

    assert [p.cost_ratio for p in points] == [19.0, 1.0, 3.5]
    assert all(isinstance(p.cost_ratio, float) for p in points)  # 정수로 준 비율도 float다
    assert points[2].threshold == pytest.approx(3.5 / 4.5)


def test_NFR06_reask_curve_without_labels():
    only_none = [sample("none", a=0.9, b=0.05, none=0.05), sample("none", a=0.1, b=0.1, none=0.8)]

    # 기기 라벨 표본이 없으면 곡선이 없다. 표본이 아예 없어도 같다
    assert reask_curve(only_none, [1, 3]) == []
    assert reask_curve([], [1, 3]) == []
    # 곡선 행은 칸을 비우고 note에 이유를 쓴다. 표본이 몇 개든 같다
    for trials in (0, 2):
        assert reask_rows([], trials) == [
            {"metric": "m2_reask_curve", "value": "", "limit": "", "passed": "", "trials": "", "failures": "",
             "note": "기기 라벨 시험 회차 없음"}
        ]


@pytest.mark.parametrize("ratios, message", [
    ([], "비용 비율이 하나 이상"),
    ([1.0, 0.0], "0보다 큰 유한한 수여야 한다 \\(0.0\\)"),
    ([-1.0], "0보다 큰 유한한 수여야 한다 \\(-1.0\\)"),  # −1은 1 + r이 0이라 임계값을 낼 수 없다
    ([math.nan], "0보다 큰 유한한 수"),
    ([math.inf], "0보다 큰 유한한 수"),
])
def test_NFR06_reask_curve_rejects_bad_cost_ratios(ratios, message):
    with pytest.raises(EvaluationError, match=message):
        reask_curve(KNOWN, ratios)
    # 설정의 오류라서 표본이 없어도 먼저 알린다
    with pytest.raises(EvaluationError, match=message):
        reask_curve([], ratios)


def test_NFR06_reask_curve_rejects_malformed_samples():
    # 라벨이 확률을 가진 상태가 아니면 오작동으로 세지 않고 거부한다 (`compute_metrics`와 같다)
    with pytest.raises(EvaluationError, match="회차 'run'.*'c'"):
        reask_curve([sample("c", a=0.5, b=0.3, none=0.2)], [1])
    # 기기의 확률이 없으면 서버가 고를 기기가 없다. 어느 회차인지 알린다
    with pytest.raises(EvaluationError, match="회차 'bad'.*기기의 확률"):
        reask_curve([sample("a", a=0.9, none=0.1), Sample("bad", "none", {"none": 1.0})], [1])


def test_NFR06_reask_curve_from_collect_samples():
    runs = [
        make_run("r-a", "a", LEVELS["a"]),
        make_run("r-b", "b", LEVELS["b"]),
        make_run("r-wrong", "a", LEVELS["b"]),  # 라벨은 a인데 b 앞에서 받은 RSSI. 모델은 b를 확신한다
        make_run("r-none", "none", LEVELS["none"]),  # 모든 기기의 확률이 0으로 떨어진다
    ]
    samples = collect_samples(MODEL, runs, bin_sec=1.0)  # 회차마다 18개

    points = reask_curve(samples, [1, 19])

    assert len(samples) == 72 and len(points) == 2
    # 맞은 회차 둘은 어느 임계값에서나 구동하고 맞다. 틀린 회차는 구동하고 오작동이다. none 회차는 기기 확률이 0에 가까워 재질문이다
    for point in points:
        assert (point.reask_rate, point.misoperation_rate) == pytest.approx((0.25, 0.25))


# ── reask_rows ────────────────────────────────────────────────────────


def test_NFR06_reask_rows_one_row_per_cost_ratio():
    points = reask_curve(KNOWN, [1, 3, 9, 19])

    rows = reask_rows(points, len(KNOWN))

    assert [list(row) for row in rows] == [COLUMNS] * 5
    assert [row["metric"] for row in rows] == [
        "m2_reask_curve", "m2_reask_r1", "m2_reask_r3", "m2_reask_r9", "m2_reask_r19"]
    curve_row, *ratio_rows = rows
    assert {key: curve_row[key] for key in ("value", "limit", "passed", "trials", "failures")} == {
        "value": "", "limit": "", "passed": "", "trials": 8, "failures": ""}
    assert "m2_reask_r*" in curve_row["note"] and "비교 기준 미결" in curve_row["note"]
    # 값은 오작동률이고 failures는 오작동 표본 수다. 통과 기준은 비교 기준이 미결이라 limit과 passed를 비우고 note에 쓴다
    assert [row["value"] for row in ratio_rows] == pytest.approx([3 / 8, 2 / 8, 0.0, 0.0])
    assert [row["trials"] for row in ratio_rows] == [8] * 4
    assert [row["failures"] for row in ratio_rows] == [3, 2, 0, 0]  # 정수다
    assert [(row["limit"], row["passed"]) for row in ratio_rows] == [("", "")] * 4
    # note에 재질문률과 확률 임계값이 있다
    prefixes = ["재질문률 0.1250, 확률 임계값 0.5000", "재질문률 0.3750, 확률 임계값 0.7500",
                "재질문률 0.8750, 확률 임계값 0.9000", "재질문률 1.0000, 확률 임계값 0.9500"]
    for row, prefix in zip(ratio_rows, prefixes):
        assert row["note"].startswith(prefix)
        assert "통과 여부 미판정 (같은 재질문률 비교 기준 미결)" in row["note"]


def test_NFR06_reask_rows_name_the_ratio_without_trailing_zero():
    points = [CurvePoint(0.5, 1 / 3, 0.0, 0.0), CurvePoint(2.0, 2 / 3, 0.0, 0.0), CurvePoint(10.0, 10 / 11, 0.0, 0.0)]

    rows = reask_rows(points, 10)

    assert [row["metric"] for row in rows[1:]] == ["m2_reask_r0.5", "m2_reask_r2", "m2_reask_r10"]


def test_NFR06_reask_rows_are_accepted_by_write_report(tmp_path):
    csv_path, md_path = tmp_path / "r.csv", tmp_path / "r.md"

    def write(rows: list[dict]) -> list[dict[str, str]]:
        write_report(rows, db_path="eval.db", schema_version=5, sample_count=len(KNOWN), generated_at=GENERATED_AT,
                     csv_path=str(csv_path), md_path=str(md_path), git_commit="abc", force=True)
        return read_report(csv_path)

    rows = {row["metric"]: row for row in write(reask_rows(reask_curve(KNOWN, [1, 3, 9, 19]), len(KNOWN)))}
    # 오작동이 0건인 행에는 write_report가 신뢰구간 상한을 덧붙인다. 오작동이 있는 행과 곡선 행에는 없다
    assert float(rows["m2_reask_r9"]["confidence_bound_95"]) == pytest.approx(1 - 0.025 ** (1 / 8))
    assert float(rows["m2_reask_r19"]["confidence_bound_95"]) == pytest.approx(1 - 0.025 ** (1 / 8))
    assert rows["m2_reask_r1"]["confidence_bound_95"] == rows["m2_reask_curve"]["confidence_bound_95"] == ""
    assert rows["m2_reask_r1"]["passed"] == "" and "재질문률 0.1250" in rows["m2_reask_r1"]["note"]

    empty = write(reask_rows([], 0))  # 기기 라벨이 없어도 쓸 수 있다 (failures가 비어서 상한을 내려 하지 않는다)
    assert [row["metric"] for row in empty] == ["m2_reask_curve"]
    assert empty[0]["value"] == "" and empty[0]["trials"] == "" and "confidence_bound_95" not in empty[0]
    assert "기기 라벨 시험 회차 없음" in md_path.read_text(encoding="utf-8")
