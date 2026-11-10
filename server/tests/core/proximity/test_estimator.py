"""근접 추정기 (FR-16, FR-22). 합성 공통 모델 파일과 손으로 만든 프레임으로 확인한다.

자동 영점을 찾고 저장하는 동작은 `test_auto_zero_estimator.py`가 본다. 여기서는 자동 영점이 없는 추정기의 버퍼, 근접 참여 기기, 입력 검사, 잠금을 본다.
"""

from __future__ import annotations

import inspect
import math
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nearpilot.core.proximity import HmmProximityEstimator
from nearpilot.core.proximity.common_model import CommonModel, ModelMismatchError, load_common_model
from nearpilot.core.proximity.posterior import ProximityInputError, compute_posteriors
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import TrustState
from nearpilot.shared.interfaces import ProximityEstimator
from nearpilot.shared.models import BeaconKey, NodeInfo, ProximityResult, RssiFrame, RssiReport

from .conftest import BEACON, OTHER_BEACON, PLACE_A_NODES, make_frame, make_node, place_a_nodes
from .test_common_posterior import AT, standing_frames

MODEL = load_common_model(Path(__file__).resolve().parent / "fixtures" / "common" / "syn-common-1.json")
SETTINGS = Settings()  # rssi_frame_sec 1.0, anchor_recent_sec 3.0
EXTREME_RSSI = -30.0  # 어느 상태에서도 거의 나오지 않는 값. 쓰이면 결과가 크게 달라진다
OLD_AT = AT - timedelta(seconds=100)  # 버퍼 보존 길이(window_sec 3 + 시간 칸 1)보다 한참 이른 판정 시각
REPORT_AT = AT - timedelta(seconds=0.5)  # 기본 보고 시각. 최근 구간(3초) 안이다
NODES_B = [make_node("mood-1", "place-b"), make_node("mood-2", "place-b"), make_node("mood-3", "place-b")]


# ── 도우미 ───────────────────────────────────────────────────────────


def report(node_id: str, received_at: datetime = REPORT_AT, *, seq: int = 1, session_id: str | None = None) -> RssiReport:
    return RssiReport(
        node_id=node_id, session_id=session_id or f"s-{node_id}", seq=seq, frame_ts=received_at, sent_at=received_at,
        received_at=received_at,
    )


def report_all(estimator: HmmProximityEstimator, nodes: list[NodeInfo], received_at: datetime = REPORT_AT) -> None:
    """nodes가 모두 빈 보고를 보냈다. 보고한 trusted 기기가 근접 참여 기기다."""
    for node in nodes:
        estimator.observe_report(report(node.node_id, received_at), node=node)


def observed(
    frames: list[RssiFrame],
    *,
    nodes: list[NodeInfo] | None = None,
    model: CommonModel | None = MODEL,
    settings: Settings = SETTINGS,
    reporting: list[str] | None = None,
) -> HmmProximityEstimator:
    """frames를 순서대로 observe한 추정기. 프레임마다 node_id가 같은 기기를 nodes(기본값: place-a 전체)에서 찾아 넘긴다.

    reporting(기본값: nodes 전체)의 기기가 REPORT_AT에 빈 보고를 보냈다.
    """
    nodes = place_a_nodes() if nodes is None else nodes
    by_id = {node.node_id: node for node in nodes}
    estimator = HmmProximityEstimator(model, settings)
    for frame in frames:
        estimator.observe(frame, node=by_id[frame.node_id])
    report_all(estimator, nodes if reporting is None else [by_id[node_id] for node_id in reporting])
    return estimator


def ask(
    estimator: HmmProximityEstimator,
    *,
    beacons: list[BeaconKey] | tuple[BeaconKey, ...] = (BEACON,),
    candidates: list[NodeInfo] | None = None,
    place_nodes: list[NodeInfo] | None = None,
    at: datetime = AT,
) -> ProximityResult:
    """기본값은 place-a의 전체 기기를 후보로 하는 호출이다."""
    nodes = place_a_nodes() if place_nodes is None else place_nodes
    return estimator.posterior(beacons, nodes if candidates is None else candidates, at, place_nodes=nodes)


def replay(
    frames: list[RssiFrame],
    *,
    model: CommonModel = MODEL,
    beacon: BeaconKey = BEACON,
    place_nodes: list[NodeInfo] | None = None,
    candidates: list[NodeInfo] | None = None,
    participants: list[str] | None = None,
    at: datetime = AT,
    bin_sec: float = 1.0,
) -> ProximityResult:
    """추정기가 부르는 순수 함수를 직접 부른다. 기본값은 place-a의 전체 기기가 참여하고 자동 영점이 하나도 없는 호출이다."""
    nodes = place_a_nodes() if place_nodes is None else place_nodes
    return compute_posteriors(
        model, frames, beacon, nodes if candidates is None else candidates, nodes, at, bin_sec=bin_sec,
        participants=[node.node_id for node in nodes] if participants is None else participants,
        zeros={}, calib_version="0",
    )


def mixed_frames() -> list[RssiFrame]:
    """locker-2 앞에 서 있다가 마지막 한 칸에 light-1 앞을 지난 방문자. 마지막 칸은 locker-1과 light-2만 보고해서 증거가 약하다.

    칸 길이마다 결과가 달라지고, 1등은 locker-2에 머문다.
    """
    return [
        *standing_frames("locker-2", PLACE_A_NODES, count=5, last_ts=AT - timedelta(seconds=1)),
        *standing_frames("light-1", ["locker-1", "light-2"], count=1, last_ts=AT, first_seq=50),
    ]


# ── Protocol과 재현 ───────────────────────────────────────────────────


def test_FR16_estimator_satisfies_protocol():
    estimator = HmmProximityEstimator(MODEL, SETTINGS)

    assert isinstance(estimator, ProximityEstimator)
    # runtime_checkable은 메서드 이름만 본다. 인자 이름과 종류도 Protocol과 같아야 한다.
    for name in ("observe", "posterior"):
        actual = inspect.signature(getattr(HmmProximityEstimator, name)).parameters.values()
        declared = inspect.signature(getattr(ProximityEstimator, name)).parameters.values()
        assert [(p.name, p.kind) for p in actual] == [(p.name, p.kind) for p in declared]


@pytest.mark.parametrize("bin_sec", [1.0, 2.0, 0.5])
def test_FR22_replay_matches_posterior(bin_sec):
    settings = replace(SETTINGS, rssi_frame_sec=bin_sec)
    frames = mixed_frames()
    nodes = place_a_nodes()
    candidates = [node for node in nodes if node.node_id != "speaker-1"]

    result = observed(frames, settings=settings).posterior([BEACON], candidates, AT, place_nodes=nodes)

    assert result == replay(frames, candidates=candidates, bin_sec=bin_sec)
    assert result.best[0] == "locker-2"
    # 시간 칸의 길이는 Settings에서 온다. 다른 칸 길이로 계산하면 결과가 달라지는 입력이다.
    others = [replay(frames, candidates=candidates, bin_sec=other) for other in (0.5, 1.0, 2.0) if other != bin_sec]
    assert all(result != other for other in others)
    # 도착 순서는 결과를 바꾸지 않는다.
    assert observed(frames[::-1], settings=settings).posterior([BEACON], candidates, AT, place_nodes=nodes) == result


def test_FR16_restore_matches_observe():
    # locker-3은 restore의 nodes에 없고 light-2는 trusted가 아니다. 두 기기의 프레임은 쓰이지 않는다.
    known = [node for node in place_a_nodes(**{"light-2": TrustState.DISCOVERED}) if node.node_id != "locker-3"]
    by_id = {node.node_id: node for node in known}
    recent = mixed_frames()
    stale = standing_frames("locker-1", PLACE_A_NODES, count=5, last_ts=OLD_AT, first_seq=1000)  # 버퍼 보존 길이 밖이다. recent와 키가 겹치지 않는다
    frames = [*stale[:12], *recent, *stale[12:], *recent[:5]]  # 앞뒤로 오래된 프레임이 있고 끝에 중복이 있다

    by_observe = HmmProximityEstimator(MODEL, SETTINGS)
    for frame in frames:
        if frame.node_id in by_id:
            by_observe.observe(frame, node=by_id[frame.node_id])
    by_restore = HmmProximityEstimator(MODEL, SETTINGS)
    by_restore.restore(iter(frames), known)  # 한 번만 읽을 수 있는 입력도 받는다
    trusted_known = [node for node in known if node.trust_state == TrustState.TRUSTED]
    for estimator in (by_observe, by_restore):
        report_all(estimator, trusted_known)

    queries = [
        {},
        {"at": AT - timedelta(seconds=1)},
        {"candidates": [make_node("locker-1"), make_node("light-1")]},
        {"beacons": [OTHER_BEACON]},
        {"at": OLD_AT},
    ]
    for query in queries:
        assert ask(by_restore, **query) == ask(by_observe, **query), query

    trusted_ids = {node.node_id for node in trusted_known}
    used = [frame for frame in recent if frame.node_id in trusted_ids]
    assert ask(by_restore) == replay(used, participants=sorted(trusted_ids))
    assert replay(recent) != ask(by_restore)  # 버린 프레임이 쓰였다면(두 기기가 참여했다면) 결과가 달라지는 입력이다


def test_FR16_restore_adds_to_existing_frames():
    frames = standing_frames("locker-2", PLACE_A_NODES, count=3)
    first, second = frames[:9], frames[9:]
    estimator = observed(first)

    estimator.restore(second, place_a_nodes())

    assert ask(estimator) == ask(observed(frames))
    assert ask(observed(first)) != ask(observed(frames))


def test_FR16_restore_does_not_restore_reports():
    # 재시작하면 다음 보고까지 그 기기는 근접 참여 기기가 아니다 (FR-09).
    estimator = HmmProximityEstimator(MODEL, SETTINGS)
    estimator.restore(standing_frames("locker-2", PLACE_A_NODES, count=3), place_a_nodes())

    assert ask(estimator).posteriors == {}
    assert estimator.distance_order(BEACON, place_a_nodes(), AT).nodes == ()


# ── 근접 참여 기기 ────────────────────────────────────────────────────


def test_FR16_estimator_participants_follow_reports():
    frames = standing_frames("locker-1", PLACE_A_NODES, count=3)
    nodes = place_a_nodes()
    recent = timedelta(seconds=SETTINGS.anchor_recent_sec)
    reports = {
        "locker-1": AT - recent,  # 경계: 최근 구간의 시작과 같다. 참여한다.
        "locker-2": AT - recent - timedelta(microseconds=1),  # 마이크로초 하나 이르다. 참여하지 않는다.
        "locker-3": AT,
        "light-1": AT + timedelta(seconds=5),  # 판정 시각보다 늦은 보고도 최근 구간 시작 이후이면 참여한다 (distance_order와 같은 기준)
    }
    estimator = HmmProximityEstimator(MODEL, SETTINGS)
    for frame in frames:
        estimator.observe(frame, node=next(n for n in nodes if n.node_id == frame.node_id))
    for node_id, received_at in reports.items():
        estimator.observe_report(report(node_id, received_at), node=next(n for n in nodes if n.node_id == node_id))

    result = ask(estimator)

    assert set(result.posteriors) == {"locker-1", "locker-3", "light-1"}  # light-2, speaker-1은 보고하지 않았다
    assert result == replay(frames, participants=["locker-1", "locker-3", "light-1"])
    assert result.best[0] == "locker-1"


def test_FR16_empty_report_makes_participant():
    # 프레임이 하나도 없어도 빈 보고를 보낸 trusted 기기는 근접 참여 기기다 (FR-09).
    estimator = observed([], reporting=["locker-1", "locker-2"])

    result = ask(estimator)

    assert result.posteriors == pytest.approx({"locker-1": 1 / 3, "locker-2": 1 / 3})  # 상태는 두 기기와 "어느 기기 앞도 아님"이다
    assert result == replay([], participants=["locker-1", "locker-2"])


def test_FR16_untrusted_or_mismatched_report_is_ignored():
    nodes = place_a_nodes()
    estimator = HmmProximityEstimator(MODEL, SETTINGS)
    for state in (TrustState.DISCOVERED, TrustState.REVOKED):
        estimator.observe_report(report("locker-1"), node=make_node("locker-1", trust=state))
    estimator.observe_report(report("locker-2"), node=make_node("locker-3"))  # 보고와 다른 기기의 정보

    assert ask(estimator).posteriors == {}

    estimator.observe_report(report("locker-2"), node=nodes[1])
    assert set(ask(estimator).posteriors) == {"locker-2"}


def test_FR16_old_report_does_not_move_report_time_back():
    nodes = place_a_nodes()
    estimator = HmmProximityEstimator(MODEL, SETTINGS)
    estimator.observe_report(report("locker-1", REPORT_AT, seq=2), node=nodes[0])
    estimator.observe_report(report("locker-1", REPORT_AT - timedelta(seconds=60), seq=1), node=nodes[0])  # 늦게 도착한 옛 보고

    assert set(ask(estimator).posteriors) == {"locker-1"}


def test_FR16_no_participant_empty_result_is_not_an_error():
    estimator = observed(standing_frames("locker-1", PLACE_A_NODES, count=3), reporting=[])

    result = ask(estimator)

    assert result.posteriors == {} and result.unzeroed == frozenset()
    assert result == replay(standing_frames("locker-1", PLACE_A_NODES, count=3), participants=[])


# ── observe ──────────────────────────────────────────────────────────


def test_FR16_mismatched_node_frame_is_ignored():
    frames = standing_frames("locker-1", PLACE_A_NODES, count=3)
    nodes = place_a_nodes()
    next_node = {nodes[i].node_id: nodes[(i + 1) % len(nodes)] for i in range(len(nodes))}  # 프레임과 다른 기기의 정보
    estimator = HmmProximityEstimator(MODEL, SETTINGS)

    for frame in frames:
        estimator.observe(frame, node=next_node[frame.node_id])
    report_all(estimator, nodes)

    assert ask(estimator) == replay([])
    assert ask(observed(frames)) != replay([])  # node가 맞았다면 쓰이는 프레임이다


def test_FR16_untrusted_frame_does_not_change_result():
    frames = standing_frames("locker-1", PLACE_A_NODES, count=3)
    unobserved = replay([])
    assert ask(observed(frames)) != unobserved  # trusted 기기의 프레임이면 쓰인다

    # (1) observe의 node가 trusted가 아니면 프레임을 버린다. 기기가 나중에 trusted가 되어도 돌아오지 않는다.
    for state in (TrustState.DISCOVERED, TrustState.REVOKED):
        estimator = HmmProximityEstimator(MODEL, SETTINGS)
        for frame in frames:
            estimator.observe(frame, node=make_node(frame.node_id, trust=state))
        report_all(estimator, place_a_nodes())
        assert ask(estimator) == unobserved, state

    # (2) observe 때는 trusted였지만 posterior의 place_nodes에서 빠졌으면 그 기기의 프레임을 쓰지 않는다.
    own = standing_frames("locker-2", ["locker-2"], count=3)
    estimator = observed(own)
    assert ask(estimator) != unobserved
    revoked_nodes = place_a_nodes(**{"locker-2": TrustState.REVOKED})
    remaining = [node for node in revoked_nodes if node.trust_state == TrustState.TRUSTED]
    result = ask(estimator, candidates=remaining, place_nodes=revoked_nodes)
    assert result == replay([], candidates=remaining, place_nodes=revoked_nodes)
    assert result.posteriors == pytest.approx({node.node_id: 1 / 6 for node in remaining})  # 상태가 7개에서 6개로 준다


def test_FR16_duplicate_frame_is_used_once():
    base = standing_frames("locker-1", PLACE_A_NODES, count=3)
    # 마지막 칸의 프레임이다. 칸 평균이 달라지면 결과가 달라진다.
    original = make_frame(
        "locker-2", -60.0, AT - timedelta(seconds=0.5), seq=99, received_at=AT - timedelta(seconds=0.4),
    )
    estimator = observed([*base, original])
    baseline = ask(estimator)

    estimator.observe(original, node=make_node("locker-2"))
    assert ask(estimator) == baseline
    assert ask(observed([*base, original, original])) == baseline

    # 같은 키의 재전송은 값이 달라도 처음 들어온 프레임만 쓴다. 재전송의 received_at이 더 이르더라도 마찬가지다.
    # replay는 정렬 순서로 먼저인 프레임을 쓰므로 두 프레임을 모두 받으면 재전송을 쓴다.
    resent = replace(original, rssi=EXTREME_RSSI, received_at=AT - timedelta(seconds=0.45))
    estimator.observe(resent, node=make_node("locker-2"))
    assert ask(estimator) == baseline
    assert replay([*base, original, resent]) != baseline


@pytest.mark.parametrize(
    ("variant", "query_beacon"),
    [
        pytest.param({"node_id": "locker-2"}, BEACON, id="other_node"),
        pytest.param({"session_id": "s-2"}, BEACON, id="other_session"),
        pytest.param({"beacon": OTHER_BEACON}, OTHER_BEACON, id="other_beacon"),
    ],
)
def test_FR16_dedup_key_includes_node_session_and_beacon(variant, query_beacon):
    # 키 (node_id, session_id, seq, beacon)의 한 부분만 다른 프레임은 다른 프레임이다. 기기를 다시 연결하면 seq가 1부터 다시 시작한다.
    ts = AT - timedelta(seconds=0.5)
    first = make_frame("locker-1", -52.0, ts, seq=1, session_id="s-1", received_at=ts)
    other = replace(first, rssi=-63.0, **variant)

    result = ask(observed([first, other]), beacons=[query_beacon])

    assert result == replay([first, other], beacon=query_beacon)
    assert result != replay([first], beacon=query_beacon)  # other를 버리면 결과가 달라진다


@pytest.mark.parametrize("bad_rssi", [math.nan, math.inf, -math.inf])
def test_FR16_non_finite_rssi_frame_does_not_break_posterior(bad_rssi):
    # observe는 RSSI 값을 보지 않고 버퍼에 둔다. 유한하지 않은 값은 compute_posteriors가 결측으로 버리고 자동 영점 표본에도 넣지 않는다.
    base = standing_frames("locker-1", PLACE_A_NODES, count=3)
    bad = make_frame("locker-2", bad_rssi, AT - timedelta(seconds=0.5), seq=99, received_at=AT - timedelta(seconds=0.4))

    assert ask(observed([*base, bad])) == ask(observed(base))


# ── posterior의 입력 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "beacons",
    [
        pytest.param([], id="none"),
        pytest.param([BEACON, OTHER_BEACON], id="two"),
        pytest.param((BEACON, BEACON), id="same_beacon_twice"),
    ],
)
def test_FR16_beacons_must_be_single(beacons):
    estimator = observed(standing_frames("locker-1", PLACE_A_NODES, count=3))

    with pytest.raises(ProximityInputError, match="beacons"):
        ask(estimator, beacons=beacons)

    assert ask(estimator, beacons=[BEACON]).best[0] == "locker-1"  # 하나면 된다


@pytest.mark.parametrize(
    "place_nodes",
    [
        pytest.param([], id="empty"),
        pytest.param([*place_a_nodes(), make_node("mood-1", "place-b")], id="two_places"),
        pytest.param([replace(node, place_id=None) for node in place_a_nodes()], id="place_id_none"),
        pytest.param([*place_a_nodes()[:2], replace(make_node("locker-3"), place_id=None)], id="one_place_id_none"),
    ],
)
def test_FR16_place_nodes_must_name_one_place(place_nodes):
    estimator = observed([])

    with pytest.raises(ProximityInputError):
        estimator.posterior([BEACON], [], AT, place_nodes=place_nodes)


@pytest.mark.parametrize(
    "place_nodes",
    [
        pytest.param(place_a_nodes(), id="place_a"),
        pytest.param(NODES_B, id="place_b"),
    ],
)
def test_FR16_one_model_serves_every_place(place_nodes):
    # 모델이 공통이라 장소를 고르지 않는다. 어느 장소든 같은 모델로 계산한다.
    estimator = observed([], nodes=place_nodes)

    result = estimator.posterior([BEACON], place_nodes, AT, place_nodes=place_nodes)

    assert set(result.posteriors) == {node.node_id for node in place_nodes}
    assert result.model_version == MODEL.model_version


def test_FR16_missing_model_raises():
    estimator = HmmProximityEstimator(None, SETTINGS)
    estimator.observe(make_frame("locker-1", -55.0, AT), node=make_node("locker-1"))  # 모델이 없어도 observe는 된다
    nodes = place_a_nodes()

    with pytest.raises(ModelMismatchError):
        estimator.posterior([BEACON], nodes, AT, place_nodes=nodes)
    # 입력 관계가 틀린 호출은 모델이 없어도 입력 오류가 먼저다.
    with pytest.raises(ProximityInputError, match="beacons"):
        estimator.posterior([], nodes, AT, place_nodes=nodes)


@pytest.mark.parametrize(
    ("place_nodes", "candidates", "error"),
    [
        pytest.param(place_a_nodes(), [make_node("locker-9")], ProximityInputError, id="candidate_not_in_place_nodes"),
        pytest.param(
            place_a_nodes(), [make_node("locker-1", "place-b")], ProximityInputError, id="candidate_in_other_place",
        ),
        pytest.param(
            place_a_nodes(), [make_node("locker-1", trust=TrustState.REVOKED)], ProximityInputError,
            id="candidate_not_trusted",
        ),
    ],
)
def test_FR16_input_checks_come_from_compute_posteriors(place_nodes, candidates, error):
    # 장소를 확인한 뒤의 검사는 compute_posteriors의 몫이다. 추정기는 그 예외를 바꾸지 않고 올린다.
    estimator = observed(standing_frames("locker-1", PLACE_A_NODES, count=3))

    with pytest.raises(error):
        estimator.posterior([BEACON], candidates, AT, place_nodes=place_nodes)


def test_FR16_explicit_and_general_requests_get_same_output():
    estimator = observed(mixed_frames())
    nodes = place_a_nodes()

    # 지정 여부와 지정 대상을 받지 않는다. 판정은 어느 요청이든 같은 입력을 넘긴다.
    assert list(inspect.signature(estimator.posterior).parameters) == ["beacons", "candidates", "at", "place_nodes"]
    first = ask(estimator)
    ask(estimator, candidates=nodes[:2])  # 사이에 다른 호출이 끼어도
    second = ask(estimator)

    assert second == first
    assert first.best[0] == "locker-2"


# ── 버퍼 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("old_first", [True, False], ids=["old_frames_arrive_first", "old_frames_arrive_late"])
def test_FR16_buffer_drops_old_frames(old_first):
    old = standing_frames("locker-1", PLACE_A_NODES, count=3, last_ts=OLD_AT)
    new = [make_frame("locker-1", -50.0, AT, seq=100, received_at=AT)]  # old의 seq는 1~3이라 중복이 아니다
    unobserved = replay([], at=OLD_AT)

    # 오래된 프레임만 있으면 그 구간을 물을 때 쓰인다.
    assert ask(observed(old), at=OLD_AT) == replay(old, at=OLD_AT)
    assert replay(old, at=OLD_AT) != unobserved

    # 새 프레임이 들어오면 오래된 프레임은 버퍼에서 빠진다. 오래된 프레임이 늦게 도착해도 들어오자마자 빠진다.
    estimator = observed([*old, *new] if old_first else [*new, *old])
    assert ask(estimator, at=OLD_AT) == unobserved
    assert ask(estimator) == replay(new)  # 새 프레임은 남아 있다


@pytest.mark.parametrize("frame_sec", [1.0, 2.0])
@pytest.mark.parametrize(
    ("extra", "kept"),
    [
        pytest.param(timedelta(0), True, id="exactly_retain_is_kept"),
        pytest.param(timedelta(microseconds=1), False, id="one_microsecond_over_is_dropped"),
    ],
)
def test_FR16_buffer_retention_boundary(frame_sec, extra, kept):
    settings = replace(SETTINGS, rssi_frame_sec=frame_sec)
    retain = timedelta(seconds=max(MODEL.window_sec, SETTINGS.anchor_recent_sec) + frame_sec)
    # seen은 AT의 관측 구간 안에서 쓰이는 프레임이다. later는 seen보다 retain(+extra) 늦게 도착한다.
    # later의 received_at이 AT보다 뒤라서 AT의 결과에는 later 자체가 쓰이지 않는다.
    seen = make_frame("mood-1", -55.0, AT - timedelta(seconds=0.5), seq=1, received_at=AT - timedelta(seconds=0.4))
    later_at = seen.received_at + retain + extra
    later = make_frame("mood-1", -55.0, later_at, seq=2, received_at=later_at)
    used = replay([seen], place_nodes=NODES_B, bin_sec=frame_sec)
    unused = replay([], place_nodes=NODES_B, bin_sec=frame_sec)
    assert used != unused

    estimator = observed([seen, later], nodes=NODES_B, settings=settings)

    assert ask(estimator, place_nodes=NODES_B) == (used if kept else unused)


def test_FR16_buffer_retention_follows_longest_window():
    # 보존 길이는 모델의 window_sec와 최근 구간(anchor_recent_sec) 가운데 긴 쪽에 칸 하나를 더한 값이다.
    long_model = replace(MODEL, window_sec=30.0)
    seen = make_frame("mood-1", -55.0, AT - timedelta(seconds=25), seq=1, received_at=AT - timedelta(seconds=25))
    latest = make_frame("mood-1", -55.0, AT, seq=2, received_at=AT)
    used = replay([seen, latest], model=long_model, place_nodes=NODES_B)
    assert used != replay([latest], model=long_model, place_nodes=NODES_B)  # seen이 30초 구간 안에서 쓰인다

    estimator = observed([seen, latest], nodes=NODES_B, model=long_model)

    assert ask(estimator, place_nodes=NODES_B) == used


def test_FR16_buffer_retention_follows_recent_sec():
    # 관측 구간이 짧아도 거리 순서가 쓰는 최근 구간의 프레임은 버퍼에 남는다. 모델이 없어도 마찬가지다.
    settings = replace(SETTINGS, anchor_recent_sec=20.0)
    old = make_frame("locker-1", -50.0, AT - timedelta(seconds=15), seq=1, received_at=AT - timedelta(seconds=15))
    latest = make_frame("locker-2", -60.0, AT, seq=2, received_at=AT)
    nodes = place_a_nodes()

    for model in (MODEL, None):
        estimator = observed([old, latest], settings=settings, model=model)
        order = estimator.distance_order(BEACON, nodes, AT)
        assert order.values["locker-1"] == -50.0, model  # 15초 전의 프레임이 남아 있다


# ── 잠금 ─────────────────────────────────────────────────────────────


def test_FR16_posterior_computes_outside_lock(monkeypatch):
    frames = standing_frames("locker-1", PLACE_A_NODES, count=3)
    estimator = observed(frames)
    late = make_frame("locker-1", EXTREME_RSSI, AT, seq=99, received_at=AT)
    passed = []

    def spy(model, buffered, *args, **kwargs):
        # 계산하는 동안 observe가 잠금을 얻을 수 있어야 한다. 잠금을 쥐고 있으면 여기서 막힌다.
        estimator.observe(late, node=make_node("locker-1"))
        passed.append(list(buffered))  # observe 뒤에 읽는다. 넘긴 것이 버퍼 자체였다면 late가 보인다
        return compute_posteriors(model, passed[-1], *args, **kwargs)

    monkeypatch.setattr("nearpilot.core.proximity.estimator.compute_posteriors", spy)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(result=ask(estimator)), daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "posterior가 계산하는 동안 잠금을 쥐고 있다"
    # 넘긴 프레임은 복사본이다. 계산 중에 들어온 프레임은 이번 결과에 쓰이지 않는다.
    assert late not in passed[0]
    assert outcome["result"] == replay(frames)
    monkeypatch.undo()
    assert ask(estimator) == replay([*frames, late])


def test_FR16_concurrent_observe_and_posterior():
    # 40초에 걸친 프레임이라 넣는 동안 버퍼에서 프레임이 빠지기도 한다.
    # 자동 영점은 찾지 않게 min_bins를 크게 둔다. 자동 영점의 동시성은 test_auto_zero_estimator.py가 본다.
    model = replace(MODEL, auto_zero_min_bins=10_000)
    frames = standing_frames("locker-2", PLACE_A_NODES, count=40)
    random.Random(0).shuffle(frames)
    nodes = {node.node_id: node for node in place_a_nodes()}
    estimator = HmmProximityEstimator(model, SETTINGS)
    start = threading.Barrier(7, timeout=30)
    done = threading.Event()

    def observe_chunk(chunk: list[RssiFrame]) -> None:
        start.wait()
        for _ in range(3):  # 같은 프레임을 다시 넣어도 된다
            for frame in chunk:
                estimator.observe(frame, node=nodes[frame.node_id])

    def report_until_done() -> None:
        start.wait()
        seq = 0
        while not done.is_set():
            seq += 1
            for node in nodes.values():
                estimator.observe_report(report(node.node_id, REPORT_AT, seq=seq), node=node)

    def ask_until_done() -> int:
        start.wait()
        count = 0
        while count == 0 or not done.is_set():
            ask(estimator)  # 예외 없이 끝난다
            count += 1
        return count

    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 스레드가 자주 바뀌게 한다
    try:
        with ThreadPoolExecutor(max_workers=7) as pool:
            reporter = pool.submit(report_until_done)
            observers = [pool.submit(observe_chunk, frames[i::4]) for i in range(4)]
            askers = [pool.submit(ask_until_done) for _ in range(2)]
            try:
                for future in observers:
                    future.result(timeout=60)
            finally:
                done.set()
            reporter.result(timeout=60)
            assert all(future.result(timeout=60) > 0 for future in askers)
    finally:
        sys.setswitchinterval(switch_interval)

    result = ask(estimator)
    assert result == ask(observed(frames, model=model))  # 한 스레드로 같은 프레임을 넣은 결과
    assert result.best[0] == "locker-2"
