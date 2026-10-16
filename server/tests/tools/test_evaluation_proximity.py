"""시험 회차의 정면 오선택률과 ECE (NFR-06, specs/core-ops-cli/spec.md 5절·6절 "eval").

`compute_metrics`와 `metric_rows`는 `Sample`을 손으로 만들어 손으로 계산한 값과 맞춘다.
`collect_samples`는 기기 둘(a, b)의 작은 모델과 손으로 정한 RSSI 회차로 확인한다. 모델은 기기마다 자기 앞 −50 dBm,
다른 기기 앞 −70 dBm, 어느 기기 앞도 아님 −90 dBm을 기대하고 흔들림이 2 dB라서, 정답이 아닌 상태의 확률은 0으로 떨어진다.
평가 시각은 `validation_log_loss`가 쓰는 시각과 같은지 맞춰 본다.
프레임은 정수 초 사이(t + 0.5초)에 받고 수신은 관측과 같은 시각이다. 평가 시각이 정수 초이고 회차의 끝 시각 이후 프레임은 없어서,
정수 초에 프레임을 두면 마지막 평가 시각의 마지막 칸이 비고 사후확률이 `stay`로 흐려진다.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.core.proximity import fit as fit_module
from nearpilot.core.proximity.files import AcceptanceSettings
from nearpilot.core.proximity.fit import AnchorLayout, LabeledRun, PlaceLayout, validation_log_loss
from nearpilot.core.proximity.model import NodeModel, ProximityModel
from nearpilot.shared.enums import TrustState
from nearpilot.shared.models import BeaconKey, ProximityResult, RssiFrame
from tools.evaluation import proximity
from tools.evaluation.proximity import (
    EvaluationError,
    Metrics,
    Sample,
    collect_samples,
    compute_metrics,
    metric_rows,
)
from tools.evaluation.report import write_report

T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("11111111-2222-3333-4444-555555555555", 1, 1)
OTHER_BEACON = BeaconKey(BEACON.uuid, 1, 2)
ACCEPTANCE = AcceptanceSettings(max_misselection=0.05, max_ece=0.05)

# 기기 a는 앵커가 (0, 0), 앞이 (1, 0)이고 b는 앵커가 (11, 0), 앞이 (10, 0)이다. 자기 앞은 1 m, 다른 기기 앞은 10 m다.
# A = −50, n = 2라서 자기 앞은 −50 dBm, 다른 기기 앞은 −50 − 20 × log10(10) = −70 dBm이다.
MODEL = ProximityModel(
    place_id="place-a", model_version="v1", calib_version="c1", window_sec=3.0, stay=0.9, min_distance_m=0.3,
    calibration_method="none", validation={},
    nodes={
        node_id: NodeModel(node_id, x, 0.0, front, 0.0, A=-50.0, n=2.0, sigma=2.0, none_rssi=-90.0, none_sigma=2.0)
        for node_id, x, front in (("a", 0.0, 1.0), ("b", 11.0, 10.0))
    },
)
LAYOUT = PlaceLayout(
    "place-a", {node_id: AnchorLayout(node.x, node.y, node.front_x, node.front_y) for node_id, node in MODEL.nodes.items()}
)
LEVELS = {  # 라벨 → 앵커마다 받는 RSSI
    "a": {"a": -50.0, "b": -70.0},
    "b": {"a": -70.0, "b": -50.0},
    "none": {"a": -90.0, "b": -90.0},
}


def make_run(run_id: str, label: str, levels: Mapping[str, float] | None = None, *, seconds: int = 20,
             beacon: BeaconKey = BEACON, start: datetime = T0) -> LabeledRun:
    """앵커마다 1초에 하나씩(t + 0.5초에) `levels`의 RSSI를 받은 회차. `levels`가 없으면 `LEVELS[label]`이다."""
    frames = []
    for node_id, rssi in (LEVELS[label] if levels is None else levels).items():
        for t in range(seconds):
            at = start + timedelta(seconds=t + 0.5)
            frames.append(RssiFrame(node_id, beacon, rssi, at, f"{run_id}-{node_id}", t + 1, at, at))
    return LabeledRun(run_id, label, beacon, start, start + timedelta(seconds=seconds), frames)


def sample(label: str, **probabilities: float) -> Sample:
    return Sample("run", label, probabilities)


def read_report(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """보고서 CSV의 머리말 주석 줄과 지표 행."""
    lines = path.read_text(encoding="utf-8").splitlines()
    comments = [line for line in lines if line.startswith("#")]
    rows = list(csv.DictReader(line for line in lines if not line.startswith("#")))
    return comments, rows


# ── compute_metrics ───────────────────────────────────────────────────


def test_NFR06_eval_misselection_and_ece():
    # 기기 라벨 셋과 none 라벨 하나. 주석은 (예측 상태, 신뢰도, 정답 여부, 구간)이다.
    samples = [
        sample("a", a=0.85, b=0.10, none=0.05),  # a, 0.85, 정답, 구간 8
        sample("b", a=0.62, b=0.30, none=0.08),  # a, 0.62, 오답, 구간 6 — 가장 큰 기기가 라벨과 다르다
        sample("b", a=0.0, b=1.0, none=0.0),  # b, 1.0, 정답, 구간 9 (신뢰도 1.0은 마지막 구간)
        sample("none", a=0.20, b=0.15, none=0.65),  # none, 0.65, 정답, 구간 6
    ]

    metrics = compute_metrics(samples)

    assert metrics.misselection_trials == 3 and metrics.misselection_failures == 1
    assert metrics.misselection_rate == pytest.approx(1 / 3)
    # 구간 6: 표본 둘, 정확도 0.5, 평균 신뢰도 0.635 → 2/4 × 0.135. 구간 8: 표본 하나, 정확도 1, 신뢰도 0.85 → 1/4 × 0.15.
    # 구간 9: 정확도 1, 신뢰도 1 → 0.
    assert metrics.ece == pytest.approx(0.5 * 0.135 + 0.25 * 0.15)
    assert metrics.ece == pytest.approx(0.105)
    assert metrics.ece_samples == 4


def test_NFR06_eval_ece_confidence_one_joins_last_bin():
    # 신뢰도 1.0의 오답과 0.95의 정답은 같은 구간(마지막)이다: 정확도 0.5, 평균 신뢰도 0.975 → |0.5 − 0.975|.
    # 1.0이 따로 구간을 만들면 (0.05 + 1.0) / 2 = 0.525가 된다.
    samples = [
        sample("b", a=1.0, b=0.0, none=0.0),
        sample("a", a=0.95, b=0.03, none=0.02),
    ]

    assert compute_metrics(samples).ece == pytest.approx(0.475)


def test_NFR06_eval_ece_bin_edges_belong_to_the_upper_bin():
    # 신뢰도 0.5는 구간 5 = [0.5, 0.6)이다. 구간 5: 0.5(정답), 0.55(오답) → 정확도 0.5, 평균 신뢰도 0.525 → 2/3 × 0.025.
    # 구간 4: 0.45(정답) → 1/3 × 0.55. 0.5가 아래 구간으로 가면 0.5333이 된다.
    samples = [
        sample("a", a=0.50, b=0.30, none=0.20),
        sample("b", a=0.55, b=0.25, none=0.20),
        sample("a", a=0.45, b=0.35, none=0.20),
    ]

    assert compute_metrics(samples).ece == pytest.approx(2 / 3 * 0.025 + 1 / 3 * 0.55)


def test_NFR06_eval_ece_bins_option():
    samples = [
        sample("a", a=0.9, b=0.05, none=0.05),  # a, 0.9, 정답
        sample("a", a=0.7, b=0.2, none=0.1),  # a, 0.7, 정답
        sample("b", a=0.5, b=0.3, none=0.2),  # a, 0.5, 오답
    ]

    # 구간이 둘(또는 하나)이면 셋 다 같은 구간이다: 정확도 2/3, 평균 신뢰도 0.7 → |2/3 − 0.7|
    assert compute_metrics(samples, bins=2).ece == pytest.approx(abs(2 / 3 - 0.7))
    assert compute_metrics(samples, bins=1).ece == pytest.approx(abs(2 / 3 - 0.7))
    # 구간이 열이면 갈라진다: (|1 − 0.9| + |1 − 0.7| + |0 − 0.5|) / 3
    assert compute_metrics(samples).ece == pytest.approx((0.1 + 0.3 + 0.5) / 3)


@pytest.mark.parametrize("probabilities, label, ece", [
    # 확률이 같으면 사전순으로 앞서는 상태가 예측이다. 신뢰도는 0.4이니 예측이 맞으면 |1 − 0.4|, 틀리면 |0 − 0.4|다.
    ({"a": 0.4, "b": 0.4, "none": 0.2}, "a", 0.6),
    ({"a": 0.4, "b": 0.4, "none": 0.2}, "b", 0.4),
    ({"node-1": 0.4, "none": 0.4, "zone-1": 0.2}, "node-1", 0.6),  # "node-1" < "none"
    ({"node-1": 0.4, "none": 0.4, "zone-1": 0.2}, "none", 0.4),
    ({"zone-1": 0.4, "none": 0.4, "node-1": 0.2}, "none", 0.6),  # "none" < "zone-1"
    ({"zone-1": 0.4, "none": 0.4, "node-1": 0.2}, "zone-1", 0.4),
])
def test_NFR06_eval_ece_ties_pick_the_first_state(probabilities, label, ece):
    # 딕셔너리에 넣은 순서가 결과를 바꾸지 못한다
    for items in (list(probabilities.items()), list(reversed(probabilities.items()))):
        assert compute_metrics([Sample("run", label, dict(items))]).ece == pytest.approx(ece)


def test_NFR06_eval_misselection_ties_pick_the_first_node_id():
    # 기기 확률이 같으면 node_id 사전순으로 앞선 기기가 선택이다. a와 b가 같으면 a를 골라 라벨 b는 오선택이다.
    samples = [sample("b", a=0.4, b=0.4, none=0.2), sample("a", a=0.4, b=0.4, none=0.2)]

    metrics = compute_metrics(samples)

    assert (metrics.misselection_trials, metrics.misselection_failures) == (2, 1)
    # none이 기기와 같은 확률이어도 기기끼리만 겨룬다: none이 가장 커도 가장 큰 기기가 라벨이면 오선택이 아니다
    assert compute_metrics([sample("a", a=0.3, b=0.1, none=0.6)]).misselection_failures == 0
    assert compute_metrics([sample("b", a=0.3, b=0.1, none=0.6)]).misselection_failures == 1


def test_NFR06_eval_metrics_without_device_labels():
    samples = [sample("none", a=0.1, b=0.1, none=0.8), sample("none", a=0.3, b=0.2, none=0.5)]

    metrics = compute_metrics(samples)

    assert (metrics.misselection_rate, metrics.misselection_trials, metrics.misselection_failures) == (None, 0, 0)
    assert metrics.ece == pytest.approx(0.5 * 0.2 + 0.5 * 0.5)  # 구간 8: 신뢰도 0.8의 정답, 구간 5: 신뢰도 0.5의 정답
    assert metrics.ece_samples == 2


def test_NFR06_eval_perfect_samples_have_zero_ece_and_no_misselection():
    samples = [sample("a", a=1.0, b=0.0, none=0.0), sample("b", a=0.0, b=1.0, none=0.0),
               sample("none", a=0.0, b=0.0, none=1.0)]

    assert compute_metrics(samples) == Metrics(0.0, 2, 0, 0.0, 3)


def test_NFR06_eval_metrics_reject_bad_input():
    with pytest.raises(EvaluationError, match="표본이 없"):
        compute_metrics([])
    with pytest.raises(EvaluationError, match="bins"):
        compute_metrics([sample("a", a=1.0, none=0.0)], bins=0)
    # 라벨이 확률을 가진 상태가 아니면 오답으로 세지 않고 거부한다 (collect_samples도 같은 라벨을 거부한다)
    with pytest.raises(EvaluationError, match="'c'"):
        compute_metrics([sample("c", a=0.5, b=0.3, none=0.2)])
    with pytest.raises(EvaluationError, match="'none'"):
        compute_metrics([Sample("run", "none", {"a": 1.0})])


# ── metric_rows ───────────────────────────────────────────────────────


def test_NFR06_eval_metric_rows_pass_and_fail():
    rows = metric_rows(Metrics(0.04, 100, 4, 0.049, 250), ACCEPTANCE)

    assert [list(row) for row in rows] == [["metric", "value", "limit", "passed", "trials", "failures", "note"]] * 4
    assert rows[0] == {"metric": "front_misselection_rate", "value": 0.04, "limit": 0.05, "passed": True,
                       "trials": 100, "failures": 4, "note": ""}
    assert rows[1] == {"metric": "ece", "value": 0.049, "limit": 0.05, "passed": True,
                       "trials": 250, "failures": "", "note": ""}

    rows = metric_rows(Metrics(0.06, 100, 6, 0.051, 250), ACCEPTANCE)
    assert (rows[0]["passed"], rows[1]["passed"]) == (False, False)
    assert metric_rows(Metrics(0.06, 100, 6, 0.01, 250), ACCEPTANCE)[1]["passed"] is True  # 지표마다 따로 판정한다


def test_NFR06_eval_metric_rows_limit_is_inclusive():
    rows = metric_rows(Metrics(0.05, 20, 1, 0.05, 20), ACCEPTANCE)

    assert (rows[0]["passed"], rows[1]["passed"]) == (True, True)  # 기준과 같으면 통과다 (value <= limit)
    rows = metric_rows(Metrics(0.0, 20, 0, 0.0, 20), AcceptanceSettings(0.0, 0.0))
    assert (rows[0]["passed"], rows[1]["passed"]) == (True, True)


def test_NFR06_eval_metric_rows_unmeasured_items():
    rows = metric_rows(Metrics(0.0, 10, 0, 0.01, 10), ACCEPTANCE)

    assert [row["metric"] for row in rows] == ["front_misselection_rate", "ece", "switch_time", "flicker"]
    for row in rows[2:]:
        assert {key: value for key, value in row.items() if key not in ("metric", "note")} == {
            "value": "", "limit": "", "passed": "", "trials": "", "failures": ""}
        assert row["note"] == "미측정"


def test_NFR06_eval_metric_rows_without_device_labels():
    rows = metric_rows(Metrics(None, 0, 0, 0.02, 30), ACCEPTANCE)

    # 기준은 그대로 두고 값과 통과 여부를 비운다. 실패 수도 비운다 (write_report가 failures == 0이면 trials로 상한을 낸다)
    assert rows[0] == {"metric": "front_misselection_rate", "value": "", "limit": 0.05, "passed": "",
                       "trials": 0, "failures": "", "note": "기기 라벨 시험 회차 없음"}
    assert rows[1]["value"] == 0.02 and rows[1]["trials"] == 30


def test_NFR06_eval_metric_rows_are_accepted_by_write_report(tmp_path):
    csv_path, md_path = tmp_path / "r.csv", tmp_path / "r.md"
    generated_at = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)

    def write(metrics: Metrics) -> list[dict[str, str]]:
        write_report(metric_rows(metrics, ACCEPTANCE), db_path="eval.db", schema_version=5,
                     sample_count=metrics.ece_samples, generated_at=generated_at, csv_path=str(csv_path),
                     md_path=str(md_path), git_commit="abc", force=True)
        return read_report(csv_path)[1]

    rows = write(Metrics(0.0, 40, 0, 0.01, 90))  # 실패 0건이면 write_report가 신뢰구간 상한을 덧붙인다
    assert [row["metric"] for row in rows] == ["front_misselection_rate", "ece", "switch_time", "flicker"]
    assert float(rows[0]["confidence_bound_95"]) == pytest.approx(1 - 0.025 ** (1 / 40))
    assert rows[1]["confidence_bound_95"] == ""  # ECE 행은 실패 수가 없어서 상한이 없다

    rows = write(Metrics(None, 0, 0, 0.01, 30))  # 기기 라벨 표본이 없어도 쓸 수 있다 (trials = 0으로 상한을 내려 하지 않는다)
    assert rows[0]["value"] == "" and rows[0]["trials"] == "0" and rows[0]["failures"] == ""
    assert rows[0]["note"] == "기기 라벨 시험 회차 없음" and "confidence_bound_95" not in rows[0]
    assert "기기 라벨 시험 회차 없음" in md_path.read_text(encoding="utf-8")


# ── collect_samples ───────────────────────────────────────────────────


def recorded_times(monkeypatch: pytest.MonkeyPatch, module, call) -> list[datetime]:
    """`call()`이 `module.compute_posteriors`에 넘긴 평가 시각을 차례로 모은다."""
    real = module.compute_posteriors
    times: list[datetime] = []

    def spy(model, frames, beacon, candidates, place_nodes, at, *, bin_sec):
        times.append(at)
        return real(model, frames, beacon, candidates, place_nodes, at, bin_sec=bin_sec)

    monkeypatch.setattr(module, "compute_posteriors", spy)
    call()
    return times


def test_NFR06_eval_collect_samples_counts_times_like_validation_log_loss():
    # 회차 20초, window 3초, 칸 1초: 3초부터 20초까지 1초마다 18개
    samples = collect_samples(MODEL, [make_run("r1", "a"), make_run("r2", "b")], bin_sec=1.0)

    assert [(s.run_id, s.label) for s in samples] == [("r1", "a")] * 18 + [("r2", "b")] * 18


@pytest.mark.parametrize(
    "window_sec, bin_sec",
    # 1/3초는 정수 마이크로초가 아니다. k번째 시각을 k × (칸 길이를 마이크로초로 자른 값)으로 세는 것까지 같아야 한다
    [(3.0, 1.0), (2.5, 0.5), (3.0, 0.3), (3.0, 1 / 3), (2.0, 0.7), (20.0, 1.0), (4.0, 2.0)],
)
def test_NFR06_eval_collect_samples_uses_validation_log_loss_times(monkeypatch, window_sec, bin_sec):
    model = replace(MODEL, window_sec=window_sec)
    run = make_run("r1", "a")
    expected = recorded_times(
        monkeypatch, fit_module, lambda: validation_log_loss(model, LAYOUT, [run], bin_sec=bin_sec))

    actual = recorded_times(monkeypatch, proximity, lambda: collect_samples(model, [run], bin_sec=bin_sec))

    assert actual == expected and len(expected) > 0
    first = run.started_at + timedelta(seconds=window_sec)
    assert actual[0] == first and actual[-1] <= run.ended_at < actual[-1] + timedelta(seconds=bin_sec)


def test_NFR06_eval_collect_samples_probabilities_follow_the_model():
    runs = [make_run("r-a", "a"), make_run("r-b", "b"), make_run("r-none", "none")]

    samples = collect_samples(MODEL, runs, bin_sec=1.0)

    assert len(samples) == 54
    for sample_ in samples:
        assert set(sample_.probabilities) == {"a", "b", "none"}  # 모델의 모든 기기와 none
        assert math.fsum(sample_.probabilities.values()) == pytest.approx(1.0)
        top = max(sample_.probabilities, key=sample_.probabilities.get)
        assert top == sample_.label and sample_.probabilities[top] == pytest.approx(1.0)
        assert all(0.0 <= p <= 1.0 for p in sample_.probabilities.values())
    metrics = compute_metrics(samples)
    assert (metrics.misselection_failures, metrics.misselection_trials) == (0, 36)
    assert metrics.ece == pytest.approx(0.0, abs=1e-9) and metrics.ece_samples == 54


def test_NFR06_eval_collect_samples_counts_a_wrongly_labeled_run():
    # 라벨은 a인데 b 앞에서 받은 RSSI다. 모든 시각에서 모델은 b를 고르고 확신하므로, 오선택이고 ECE는 1에 가깝다.
    samples = collect_samples(MODEL, [make_run("r1", "a", LEVELS["b"])], bin_sec=1.0)

    assert len(samples) == 18 and all(s.probabilities["b"] == pytest.approx(1.0) for s in samples)
    metrics = compute_metrics(samples)
    assert (metrics.misselection_rate, metrics.misselection_failures) == (1.0, 18)
    assert metrics.ece == pytest.approx(1.0)


def test_NFR06_eval_collect_samples_none_probability_is_one_minus_sum_and_not_negative(monkeypatch):
    # 합이 0.75와 1 + 1e-12다. 후보 확률의 합은 반올림 때문에 1을 조금 넘을 수 있다 (`ProximityResult`가 허용하는 오차다)
    results = iter([{"a": 0.5, "b": 0.25}, {"a": 0.5, "b": 0.500000000001}])

    def fake(model, frames, beacon, candidates, place_nodes, at, *, bin_sec):
        return ProximityResult(next(results), at - timedelta(seconds=3), at, "v1", "c1")

    monkeypatch.setattr(proximity, "compute_posteriors", fake)
    run = make_run("r1", "a", seconds=4)  # 평가 시각이 3초, 4초 둘이다

    first, second = collect_samples(MODEL, [run], bin_sec=1.0)

    assert dict(first.probabilities) == {"a": 0.5, "b": 0.25, "none": 0.25}
    assert dict(second.probabilities) == {"a": 0.5, "b": 0.500000000001, "none": 0.0}  # −1e-12는 0으로 자른다


def test_NFR06_eval_collect_samples_passes_every_model_node_as_trusted_candidates(monkeypatch):
    seen = []

    def spy(model, frames, beacon, candidates, place_nodes, at, *, bin_sec):
        seen.append((model, list(frames), beacon, candidates, place_nodes, bin_sec))
        return ProximityResult({"a": 0.5, "b": 0.5}, at - timedelta(seconds=3), at, "v1", "c1")

    monkeypatch.setattr(proximity, "compute_posteriors", spy)
    run = make_run("r1", "a", seconds=4)

    collect_samples(MODEL, [run], bin_sec=0.5)

    assert len(seen) == 3  # 3초, 3.5초, 4초
    model, frames, beacon, candidates, place_nodes, bin_sec = seen[0]
    assert model is MODEL and beacon == BEACON and bin_sec == 0.5
    assert candidates == place_nodes and [node.node_id for node in candidates] == ["a", "b"]
    assert {(node.place_id, node.trust_state) for node in candidates} == {("place-a", TrustState.TRUSTED)}
    assert len(frames) == 8 and set(frames) == set(run.frames)  # 쓸 수 있는 프레임이 모두 간다


def test_NFR06_eval_collect_samples_ignores_frames_the_run_does_not_use():
    clean = make_run("r1", "a")
    at = T0 + timedelta(seconds=10)
    junk = [
        RssiFrame("a", OTHER_BEACON, -90.0, at, "junk", 1, at, at),  # 다른 비콘
        RssiFrame("z", BEACON, -90.0, at, "junk", 2, at, at),  # 모델에 없는 기기
        RssiFrame("a", BEACON, -90.0, T0 + timedelta(seconds=25), "junk", 3, at, at),  # 회차의 끝 뒤
        RssiFrame("a", BEACON, -90.0, T0 - timedelta(seconds=1), "junk", 4, at, at),  # 회차의 시작 앞
        RssiFrame("b", BEACON, math.nan, at, "junk", 5, at, at),  # 유한하지 않은 RSSI
        # 같은 (기기, 연결, 순번, 비콘)의 늦은 중복. 먼저 온 프레임(t = 10, 10.5초)만 쓴다
        RssiFrame("a", BEACON, -90.0, T0 + timedelta(seconds=12.5), "r1-a", 11, at, at),
    ]
    noisy = LabeledRun(clean.run_id, clean.label, clean.beacon, clean.started_at, clean.ended_at, [*clean.frames, *junk])

    assert collect_samples(MODEL, [noisy], bin_sec=1.0) == collect_samples(MODEL, [clean], bin_sec=1.0)


def test_NFR06_eval_collect_samples_leaves_late_frames_out_like_the_decision():
    # 수신이 평가 시각 뒤인 프레임은 그 시각에 서버가 아직 받지 못한 프레임이다. 모두 30초 늦게 받았으면 증거가 없다.
    on_time = make_run("r1", "a")
    late = LabeledRun(
        "r1", "a", BEACON, T0, T0 + timedelta(seconds=20),
        [replace(frame, received_at=frame.received_at + timedelta(seconds=30)) for frame in on_time.frames])

    # 처음 분포(균등)는 전이를 거쳐도 균등이다: 상태 셋이 1/3씩이다
    for sample_ in collect_samples(MODEL, [late], bin_sec=1.0):
        assert list(sample_.probabilities.values()) == pytest.approx([1 / 3] * 3)


def test_NFR06_eval_collect_samples_rejects_runs_without_evaluation_times():
    # window_sec 3초보다 짧은 회차는 평가 시각이 없다. 어느 회차인지 알린다.
    short = make_run("short-run", "a", seconds=2)

    with pytest.raises(EvaluationError, match=r"'short-run'.*window_sec\(3초\)"):
        collect_samples(MODEL, [make_run("r1", "a"), short], bin_sec=1.0)
    # 회차 길이와 window_sec이 같으면 시각이 하나(끝 시각)다
    assert len(collect_samples(MODEL, [make_run("edge", "a", seconds=3)], bin_sec=1.0)) == 1


def test_NFR06_eval_collect_samples_rejects_unknown_label():
    with pytest.raises(EvaluationError, match=r"'r1'.*'c'.*'none'"):
        collect_samples(MODEL, [make_run("r1", "c", LEVELS["a"])], bin_sec=1.0)


@pytest.mark.parametrize(
    "bin_sec, message",
    [
        (0, "0보다 큰 유한한 수"), (0.0, "0보다 큰 유한한 수"), (-1.0, "0보다 큰 유한한 수"),
        (math.nan, "0보다 큰 유한한 수"), (math.inf, "0보다 큰 유한한 수"),
        (1e-7, "1 마이크로초 단위"),  # timedelta가 마이크로초로 반올림해서 0이 된다
    ],
)
def test_NFR06_eval_collect_samples_rejects_bad_bin_sec(bin_sec, message):
    with pytest.raises(EvaluationError, match=f"bin_sec: {message}"):
        collect_samples(MODEL, [make_run("r1", "a")], bin_sec=bin_sec)


def test_NFR06_eval_collect_samples_without_runs_is_empty():
    assert collect_samples(MODEL, [], bin_sec=1.0) == []
