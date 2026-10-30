"""지표 1(기기 선택)·지표 4(다중 사용자)·지표 9(전역 할당)의 절차와 집계 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

`run_metric1`은 방문자 하나가 사물함 A 앞에 서서 같은 발화(`request_use nearest:locker open`)를 `counts["m1_utterances"]`번 보내고,
`run_metric4`는 방문자 3명이 각자 다른 사물함 앞에서 동시에 `counts["m4_each"]`번씩 보낸다 (NFR-05). 두 지표는 시뮬레이터 RSSI로
절차와 서버 경로만 확인한다. 시뮬레이터 RSSI는 실제 전파를 흉내 내지 않으므로 실측이 아니고, 행의 note에 "시뮬레이터 측정 (실측 아님)"을
쓴다. `run_metric9`는 서버를 띄우지 않고 무작위 장면 `counts["m9_sets"]`개에서 `allocate`의 배정을 순열 완전 탐색 기준해와 비교한다 (FR-23).
서버는 고치지 않는다. MCP와 읽기 전용 DB 연결, 시뮬레이터의 명령 기록만 쓴다.

장면은 기기가 듣는 방문자의 RSSI로 만든다. 방문자가 선 사물함은 방문자를 `rssi.near`로 듣고, 같은 장면의 다른 사물함은 `rssi.far`로 듣는다.
다른 기기는 아무도 듣지 못한다 (값이 없는 앵커는 근접 확률의 증거가 아니다). `rssi.far`는 근접 모델이 이웃 사물함에서 기대하는 값에
가까워야 한다. 모델 syn-a-1에서 기기 앞 방문자를 이웃 사물함이 듣는 평균은 -60.0(옆)과 -65.3 dBm(둘 건넌)이다. 이웃이 이보다 약하게(-68 이하)
들으면 모델은 방문자가 사물함 앞이 아니라 다른 자리(조명 앞 등)에 있다고 보고 정면 기기의 사후확률이 임계값 0.95 아래로 내려가서 준비 확인을 통과하지
못한다 (예시 설정의 -62에서는 사물함 세 대 앞 모두 0.997 이상이다).
발화마다 목록에서 정면 기기를 발견하고 저장 관측의 근접 확률이 임계값 이상인지 별도로 확인한다. 반납 직후나 장면을 바꾼 직후에 보내면 근접 확률이 아직 낮다.
정면 기기가 제한 시간(`startup_timeout_sec`) 안에 나오지 않으면 장면을 만들지 못한 것이라 `HarnessError`다.

`request_use`는 발화마다 한 번만 보낸다. ASK(재질문)와 BUSY는 새 `request_id`로 다시 보내지 않고 첫 응답을 센다. 재질문 자체가 측정 대상이다.
기기를 구동했는지는 응답이 아니라 시뮬레이터의 명령 기록으로 센다. 발화마다 요청 ID에 지표와 순번(과 방문자)을 넣고, 그 요청이 만든 명령 가운데
시뮬레이터가 구동을 시작한 것을 센다. 시험 횟수(`trials`)는 그 행이 실제로 판정한 발화 수다. 다른 기기가 동작하는 비율은 기기를 구동한 발화 가운데
정면이 아닌 기기를 구동한 발화의 비율이고, 구동하지 않은 발화(재질문, 거절)는 이유를 note에 센다. 구동한 발화가 없으면 value·passed·trials·failures를
비우고 note에 사유를 쓴다. 정면 기기 구동 성공은 정면 기기만 구동했고 서버가 OK로 답한 요청이다.
서버가 받은 RSSI 프레임을 근접 추정기에 넘기는 데 동시 요청 중에는 0.25초 가까이 걸릴 때가 있다 (3명 동시 실측: 수신 뒤 중앙 0.03초, p95 0.15초).
프레임 주기와 추정기의 시간칸(`rssi_frame_sec`)이 같아서, 그때 판정이나 실행 직전 재검사를 만난 요청은 마지막 칸이 비어 근접 확률이 0.9 근처로
내려가고 임계값 밑이라 재질문(`ASK`)이 된다. 그래서 재질문 비율이 시점과 부하에 따라 크게 바뀌고 `m4_success_rate`가 한도를 넘지 못할 수 있다.
다른 기기를 구동하는 오동작은 아니다. 실기기에서는 다시 재야 한다.

지표 9의 사람은 모두 `any:locker` 요청이다. 사물함이 점유 중이면 후보에서 빼고, 근접 확률이 임계값 미만인 쌍은 허용쌍에서 뺀다.
서버의 판정 단계가 `allocate`에 허용쌍이 있는 요청만 넘기는 것과 같다. 임계값은 `[server_settings]`의 `threshold_locker`이고,
없으면 `Settings`의 초기값이다. 확률은 쌍마다 `_CLOSE_SHARE`의 확률로 임계값 이상, 아니면 임계값 미만에서 균등하게 뽑는다.
균등분포로 뽑으면 임계값 0.95에서 허용쌍이 5%뿐이라 같은 기기를 두고 겨루는 세트가 거의 없다.
"""

from __future__ import annotations

import itertools
import math
import random
import threading
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from functools import partial
from typing import Any

from nearpilot.core.allocation.allocator import COST_TOLERANCE, AllocationRequest, allocate
from nearpilot.shared.config import Settings
from nearpilot.shared.enums import Capability, Role, Verdict
from tools.evaluation.harness import Harness, HarnessError
from tools.evaluation.metric_load import _actuated, _call, _hear, _release, _run_parallel
from tools.evaluation.rows import MetricOutcome, metric_row
from tools.evaluation.server_config import EvalConfig, EvalConfigError
from tools.node_sim.store import CommandRecord

_SIMULATED = "시뮬레이터 측정 (실측 아님)"
_REQUEST = {"target_spec": "nearest:locker", "action": "open"}  # 같은 발화. request_id만 발화마다 다르다
_SIZE = 3  # 지표 4의 방문자 수이자 지표 9의 사람 수와 사물함 수 (docs/requirements.md §4 표)
_OCCUPIED_SHARE = 0.5  # 지표 9: 사물함이 점유 중일 확률
_CLOSE_SHARE = 0.5  # 지표 9: 쌍의 근접 확률이 임계값 이상일 확률
# 요청 ID가 `?`로 시작하는 요청의 명령 가운데 서버가 보내려고 한 것. 서버가 스스로 낸 명령은 요청 ID가 없어서 빠진다.
_COMMANDS = (
    "SELECT r.request_id, c.command_id, c.node_id FROM node_command c "
    "JOIN request r ON r.internal_request_id = c.internal_request_id "
    "WHERE c.send_attempted = 1 AND substr(r.request_id, 1, ?) = ?"
)


def run_metric1(h: Harness, config: EvalConfig) -> MetricOutcome:
    """같은 발화를 `counts["m1_utterances"]`번 보내고 다른 기기가 동작한 비율을 센다 (지표 1).

    첫 방문자가 첫 trusted 사물함 A 앞에 선다. A는 방문자를 `rssi.near`로, 나머지 trusted 사물함은 `rssi.far`로 듣는다.
    발화마다 A가 방문자의 기기 목록에 나올 때까지 기다린 뒤 `request_use nearest:locker open`을 한 번 보내고, OK면 반납한다.
    기다린 시간은 재지 않는다. 구동한 기기가 A가 아니면 실패다.
    """
    utterances = _count(config.counts, "m1_utterances")
    summarize_metric1([], config.acceptance)  # 통과 기준이 빠졌으면 긴 시험을 돌리기 전에 알린다
    visitor = _visitors(config, 1, "1")[0]
    front, *neighbors = _lockers(config, 1, "1")
    near, far = config.rssi["near"], config.rssi["far"]
    _hear(h, config, {front: {visitor: near}, **{locker: {visitor: far} for locker in neighbors}})

    attempts = []
    for index in range(utterances):
        _wait_listed(h, config, visitor, front)
        attempts.append(_utter(h, config, visitor, f"m1-{index:03d}"))
    actuated = _actuations(h, config, "m1-")
    attempts = [{**attempt, "front": front, "actuated": actuated.get(attempt["request_id"], [])} for attempt in attempts]
    return MetricOutcome(
        rows=summarize_metric1(attempts, config.acceptance),
        raw={"visitor": visitor, "front": front, "neighbors": neighbors, "rssi": {"near": near, "far": far},
             "attempts": attempts},
    )


def run_metric4(h: Harness, config: EvalConfig) -> MetricOutcome:
    """방문자 3명이 각자 다른 사물함 앞에서 동시에 `counts["m4_each"]`번씩 요청하고 교차 오동작과 성공률을 센다 (지표 4).

    첫 방문자 3명이 첫 trusted 사물함 3대 앞에 한 명씩 선다. 사물함마다 자기 앞 방문자를 `rssi.near`로, 다른 두 방문자를 `rssi.far`로
    듣는다. 한 회차는 세 방문자의 정면 기기가 모두 목록에 나온 뒤 세 요청을 같은 순간에 보내고, OK면 각자 반납한다. 요청자가 아닌
    방문자의 정면 기기가 그 요청으로 구동되면 다른 방문자의 기기 오동작이다.
    """
    each = _count(config.counts, "m4_each")
    summarize_metric4([], config.acceptance)  # 통과 기준이 빠졌으면 긴 시험을 돌리기 전에 알린다
    visitors = _visitors(config, _SIZE, "4")
    fronts = _lockers(config, _SIZE, "4")[:_SIZE]
    near, far = config.rssi["near"], config.rssi["far"]
    _hear(h, config, {front: {visitor: near if visitor == owner else far for visitor in visitors}
                      for front, owner in zip(fronts, visitors)})

    attempts: list[dict[str, Any]] = []
    for index in range(each):
        for visitor, front in zip(visitors, fronts):
            _wait_listed(h, config, visitor, front)
        barrier = threading.Barrier(_SIZE)  # 요청을 같은 순간에 보낸다
        attempts += _run_parallel(_SIZE, [partial(_utter, h, config, visitor, f"m4-{index:03d}-{visitor}", barrier)
                                          for visitor in visitors])
    front_of = dict(zip(visitors, fronts))
    actuated = _actuations(h, config, "m4-")
    attempts = [{**attempt, "front": front_of[attempt["user_id"]], "actuated": actuated.get(attempt["request_id"], [])}
                for attempt in attempts]
    return MetricOutcome(
        rows=summarize_metric4(attempts, config.acceptance),
        raw={"visitors": visitors, "fronts": front_of, "rssi": {"near": near, "far": far}, "attempts": attempts},
    )


def run_metric9(config: EvalConfig, rng: random.Random) -> MetricOutcome:
    """무작위 `counts["m9_sets"]`세트에서 `allocate`의 배정을 완전 탐색 기준해와 비교한다 (지표 9). 서버를 띄우지 않는다.

    세트마다 사람 3명과 사물함 3대가 있다. 사물함은 점유 중일 수 있고, 사람과 사물함 쌍마다 근접 확률이 있다. 점유 중인 사물함과
    확률이 임계값 미만인 쌍을 뺀 허용쌍이 있는 사람만 요청을 보낸다. `allocate` 결과가 허용쌍만 쓰고, 한 기기를 둘 이상에게 주지 않고,
    배정 수와 비용 합이 기준해와 같으면 일치다 (`allocation_matches`).
    """
    sets = _count(config.counts, "m9_sets")
    min_rate = _limit(config.acceptance, "m9_min_match_rate")
    threshold = _locker_threshold(config)

    records = []
    for index in range(sets):
        requests, occupied, probabilities = _random_set(rng, threshold)
        assigned = allocate(requests)
        count, cost = reference_allocation(requests)
        records.append({
            "index": index, "occupied": occupied, "probabilities": probabilities,
            "requests": {request.request_id: dict(request.candidates) for request in requests},
            "assigned": assigned, "reference": {"count": count, "cost": cost},
            "match": allocation_matches(requests, assigned),
        })
    mismatches = sum(not record["match"] for record in records)
    rate = (sets - mismatches) / sets
    with_pairs = sum(bool(record["requests"]) for record in records)
    contested = sum(_contested(record["requests"]) for record in records)
    note = (f"무작위 {sets}세트 (사람 {_SIZE}명, 사물함 {_SIZE}대), 근접 임계값 {threshold:g}. 허용쌍이 있는 세트 {with_pairs}, "
            f"같은 기기를 두고 겨루는 세트 {contested}. 기준해는 순열 완전 탐색 (배정 수 최대, 그 안에서 비용 합 최소)")
    return MetricOutcome(
        rows=[metric_row("m9_allocation_match", value=rate, limit=min_rate, passed=rate >= min_rate, trials=sets,
                         failures=mismatches, note=note)],
        raw={"threshold": threshold, "sets": records},
    )


def reference_allocation(requests: Sequence[AllocationRequest]) -> tuple[int, float]:
    """순열 완전 탐색 기준해 (배정 수, 비용 합). 배정 수가 가장 많은 배정 가운데 비용 `-log(p)` 합이 가장 작은 것이다.

    기기 ID와 "배정 없음" 자리를 요청 수만큼 놓는 순열을 모두 보고, 허용쌍이 아닌 자리가 걸린 배치는 버린다.
    `allocate`의 동적 계획법과 다른 방법이다. 요청과 기기가 서넛을 넘으면 느리다.
    """
    nodes = sorted({node for request in requests for node in request.candidates})
    slots = [*nodes, *([None] * len(requests))]
    best = (0, 0.0)
    for placed in itertools.permutations(slots, len(requests)):
        count, cost = 0, 0.0
        for request, node in zip(requests, placed):
            if node is None:
                continue
            if node not in request.candidates:
                break  # 허용쌍이 아니다
            count += 1
            cost -= math.log(request.candidates[node])
        else:
            if count > best[0] or (count == best[0] and cost < best[1]):
                best = (count, cost)
    return best


def allocation_matches(requests: Sequence[AllocationRequest], assigned: Mapping[str, str]) -> bool:
    """`assigned`(요청 ID → 기기 ID)가 허용쌍만 쓰고, 한 기기를 둘 이상에게 주지 않고, 기준해와 배정 수·비용 합이 같다.

    비용 합은 `COST_TOLERANCE` 안이면 같다.
    """
    candidates = {request.request_id: request.candidates for request in requests}
    nodes = list(assigned.values())
    if len(set(nodes)) != len(nodes):
        return False  # 중복 배정
    cost = 0.0
    for request_id, node in assigned.items():
        if node not in candidates.get(request_id, {}):
            return False  # 허용쌍이 아니다
        cost -= math.log(candidates[request_id][node])
    count, best = reference_allocation(requests)
    return len(assigned) == count and abs(cost - best) <= COST_TOLERANCE


def summarize_metric1(attempts: Sequence[Mapping[str, Any]], acceptance: Mapping[str, float]) -> list[dict[str, Any]]:
    """지표 1의 행. 통과 기준은 `acceptance`에서 읽는다.

    발화(`attempts`)마다 `front`(방문자가 선 기기), `actuated`(시뮬레이터가 구동한 기기 ID 목록), `verdict`, `reason`이 있다.
    시험 횟수는 기기를 구동한 발화 수이고, 실패는 그 가운데 정면이 아닌 기기를 구동한 발화 수다.
    """
    limit = _limit(acceptance, "m1_max_wrong_rate")
    kinds = [_kind(attempt, {attempt["front"]}) for attempt in attempts]
    counts = Counter(kinds)
    wrong = counts["cross"] + counts["other"]
    note = (f"{_SIMULATED}. 발화 {len(attempts)}건: 정면 기기 구동 {counts['front']}건, 다른 기기 구동 {wrong}건, "
            f"구동 없음 {counts['none']}건{_refusals(attempts, kinds)}")
    acted = len(attempts) - counts["none"]
    if not acted:
        return [metric_row("m1_wrong_device_rate", limit=limit, note=f"{note}. 기기를 구동한 발화가 없어서 재지 못했다")]
    rate = wrong / acted
    return [metric_row("m1_wrong_device_rate", value=rate, limit=limit, passed=rate <= limit, trials=acted, failures=wrong,
                       note=note)]


def summarize_metric4(attempts: Sequence[Mapping[str, Any]], acceptance: Mapping[str, float]) -> list[dict[str, Any]]:
    """지표 4의 행 둘. 통과 기준은 `acceptance`에서 읽는다.

    요청(`attempts`)마다 `front`(요청자가 선 기기), `actuated`, `verdict`, `reason`이 있다. 방문자들의 정면 기기는 요청들의 `front`다.
    다른 방문자의 기기 오동작은 요청자가 아닌 방문자의 정면 기기가 그 요청으로 구동된 횟수이고, 시험 횟수는 기기를 구동한 요청 수다.
    정면 기기 구동 성공률의 시험 횟수는 보낸 요청 수이고, 구동하지 못한 요청이 모두 실패다.
    """
    max_cross = _limit(acceptance, "m4_max_cross_user_malfunction")
    min_success = _limit(acceptance, "m4_min_success_rate")
    fronts = {attempt["front"] for attempt in attempts}
    kinds = [_kind(attempt, fronts) for attempt in attempts]
    counts = Counter(kinds)
    note = (f"{_SIMULATED}. 방문자 {len(fronts)}명이 동시에 보낸 요청 {len(attempts)}건: 정면 기기 구동 {counts['front']}건, "
            f"다른 방문자의 기기 구동 {counts['cross']}건, 그 밖의 기기 구동 {counts['other']}건, 구동 없음 {counts['none']}건"
            f"{_refusals(attempts, kinds)}")
    if not attempts:
        unmeasured = f"{note}. 보낸 요청이 없어서 재지 못했다"
        return [metric_row("m4_cross_user_malfunction", limit=max_cross, note=unmeasured),
                metric_row("m4_success_rate", limit=min_success, note=unmeasured)]
    cross = sum(node in fronts and node != attempt["front"] for attempt in attempts for node in attempt["actuated"])
    served = sum(kind == "front" and attempt["verdict"] == Verdict.OK for kind, attempt in zip(kinds, attempts))
    rate = served / len(attempts)
    success = metric_row("m4_success_rate", value=rate, limit=min_success, passed=rate >= min_success, trials=len(attempts),
                         failures=len(attempts) - served, note=note)
    acted = len(attempts) - counts["none"]
    if not acted:
        return [metric_row("m4_cross_user_malfunction", limit=max_cross, note=f"{note}. 기기를 구동한 요청이 없어서 재지 못했다"),
                success]
    return [metric_row("m4_cross_user_malfunction", value=cross, limit=max_cross, passed=cross <= max_cross, trials=acted,
                       failures=counts["cross"], note=note),
            success]


# ── 장면과 요청 ──────────────────────────────────────────────────────


def _visitors(config: EvalConfig, count: int, metric: str) -> list[str]:
    """설정의 앞에서 `count`명의 방문자. 모자라면 지표 `metric`을 돌릴 수 없다."""
    visitors = [user.user_id for user in config.users if user.role == Role.VISITOR]
    if len(visitors) < count:
        raise EvalConfigError(f"users: 방문자(role = visitor)가 {count}명 이상 있어야 한다 (지표 {metric}의 장면에 선다)")
    return visitors[:count]


def _lockers(config: EvalConfig, count: int, metric: str) -> list[str]:
    """설정의 trusted 사물함 전부. `count`대보다 적으면 지표 `metric`을 돌릴 수 없다."""
    lockers = [node.node_id for node in config.nodes if node.capability == Capability.LOCKER and node.trusted]
    if len(lockers) < count:
        raise EvalConfigError(f"nodes: trusted 사물함이 {count}대 이상 있어야 한다 (지표 {metric}의 장면에서 방문자가 앞에 선다)")
    return lockers


def _wait_listed(h: Harness, config: EvalConfig, user_id: str, node_id: str) -> None:
    """목록에 기기가 있고 재현 확률이 임계값 이상일 때까지 기다린다. 제한 시간이 지나면 `HarnessError`다."""
    def listed() -> bool:
        _, found, _ = h.mcp(user_id, "find_available_devices", {})
        return (any(device["node_id"] == node_id for device in (found or {}).get("devices", []))
                and h.proximity_ready(user_id, node_id))

    if not h.wait_until(listed, config.startup_timeout_sec):
        raise HarnessError(f"{user_id}의 기기 목록에 {node_id}가 {config.startup_timeout_sec:g}초 안에 나오지 않았다. 근접 확률이 "
                           f"임계값을 넘지 못한 것이다 (rssi.near {config.rssi['near']:g}, rssi.far {config.rssi['far']:g})")


def _utter(h: Harness, config: EvalConfig, user_id: str, request_id: str,
           barrier: threading.Barrier | None = None) -> dict[str, Any]:
    """발화 한 번: `request_use`를 한 번 보내고 첫 응답을 돌려준다. OK면 반납한다. ASK와 BUSY도 다시 보내지 않는다.

    `barrier`가 있으면 모든 방문자가 모일 때까지 기다렸다가 같은 순간에 보낸다. `released`는 반납의 판정이고 반납하지 않았으면 None이다.
    """
    if barrier is not None:
        barrier.wait(config.startup_timeout_sec)
    call = _call(h, user_id, "request_use", {**_REQUEST, "request_id": request_id})
    released = None
    if call["verdict"] == Verdict.OK and call["use_id"]:
        released = _release(h, config, user_id, call["use_id"])[-1]["verdict"]
    return {**call, "released": released}


def _actuations(h: Harness, config: EvalConfig, prefix: str) -> dict[str, list[str]]:
    """요청 ID가 `prefix`로 시작하는 요청마다 시뮬레이터가 구동을 시작한 기기 ID. 구동한 명령이 없는 요청은 넣지 않는다.

    서버가 보내려고 한 명령은 시뮬레이터가 받아서 처리를 시작할 때까지 기다린다. 보내려고 하지 않은 명령은 구동할 수 없다.
    """
    found: dict[str, list[str]] = {}
    for row in h.query(_COMMANDS, len(prefix), prefix):
        record = _started(h, config, row["node_id"], row["command_id"])
        if record is not None and _actuated(record):
            found.setdefault(row["request_id"], []).append(row["node_id"])
    return found


def _started(h: Harness, config: EvalConfig, node_id: str, command_id: str) -> CommandRecord | None:
    """시뮬레이터가 명령을 받아 처리를 시작한 기록. 제한 시간 안에 시작하지 않으면 그때까지의 기록(없으면 None)이다."""
    store = h.nodes[node_id].store
    latest: list[CommandRecord | None] = [None]

    def started() -> bool:
        latest[0] = store.command(command_id)
        return latest[0] is not None and latest[0].state != "accepted"

    h.wait_until(started, config.startup_timeout_sec)
    return latest[0]


# ── 집계 ─────────────────────────────────────────────────────────────


def _kind(attempt: Mapping[str, Any], fronts: Collection[str]) -> str:
    """발화 한 건의 결과: front(정면 기기만 구동), cross(다른 방문자의 정면 기기를 구동), other(그 밖의 기기를 구동), none(구동 없음)."""
    nodes = attempt["actuated"]
    if not nodes:
        return "none"
    if any(node in fronts and node != attempt["front"] for node in nodes):
        return "cross"
    return "other" if any(node != attempt["front"] for node in nodes) else "front"


def _refusals(attempts: Sequence[Mapping[str, Any]], kinds: Sequence[str]) -> str:
    """기기를 구동하지 않은 발화의 응답을 "verdict/reason" 별로 센 문장. 구동하지 않은 발화가 없으면 빈 문자열이다."""
    refused = Counter(f"{attempt['verdict']}/{attempt['reason']}" if attempt["verdict"] else "응답 없음"
                      for attempt, kind in zip(attempts, kinds) if kind == "none")
    return f" ({', '.join(f'{what} {count}건' for what, count in sorted(refused.items()))})" if refused else ""


def _limit(acceptance: Mapping[str, float], key: str) -> float:
    if key not in acceptance:
        raise EvalConfigError(f"acceptance.{key}: 지표 1·4·9가 읽는 통과 기준이 없다")
    return acceptance[key]


def _count(counts: Mapping[str, int], key: str) -> int:
    if key not in counts:
        raise EvalConfigError(f"counts.{key}: 지표 1·4·9가 읽는 횟수가 없다")
    return counts[key]


# ── 지표 9의 장면 ────────────────────────────────────────────────────


def _locker_threshold(config: EvalConfig) -> float:
    """`any:locker` 요청의 근접 임계값. 평가 설정의 `[server_settings]`에 있는 값이고, 없으면 `Settings`의 초기값이다."""
    return Settings(**{"version": "eval", **config.server_settings}).threshold_locker


def _random_set(rng: random.Random, threshold: float) -> tuple[list[AllocationRequest], list[str], dict[str, dict[str, float]]]:
    """무작위 장면 하나: (허용쌍이 있는 사람의 요청, 점유 중인 사물함, 사람과 사물함 쌍의 근접 확률).

    뽑는 순서는 점유 여부 → 쌍의 확률이다. 허용쌍 여부와 상관없이 항상 같은 수만큼 뽑아서 같은 시드면 같은 장면이 나온다.
    """
    people = [f"visitor-{number}" for number in range(1, _SIZE + 1)]
    lockers = [f"locker-{number}" for number in range(1, _SIZE + 1)]
    occupied = [locker for locker in lockers if rng.random() < _OCCUPIED_SHARE]
    probabilities = {person: {locker: _probability(rng, threshold) for locker in lockers} for person in people}
    requests = []
    for person in people:
        allowed = {locker: p for locker, p in probabilities[person].items() if locker not in occupied and p >= threshold}
        if allowed:
            requests.append(AllocationRequest(person, allowed))
    return requests, occupied, probabilities


def _probability(rng: random.Random, threshold: float) -> float:
    """쌍의 근접 확률. `_CLOSE_SHARE`의 확률로 [임계값, 1]에서, 아니면 [0, 임계값]에서 균등하게 뽑는다."""
    return rng.uniform(threshold, 1.0) if rng.random() < _CLOSE_SHARE else rng.uniform(0.0, threshold)


def _contested(requests: Mapping[str, Mapping[str, float]]) -> bool:
    """둘 이상의 요청이 같은 기기를 허용쌍으로 갖는다."""
    wanted = Counter(node for candidates in requests.values() for node in candidates)
    return any(count >= 2 for count in wanted.values())
