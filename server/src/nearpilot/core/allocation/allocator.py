"""FR-23 전역 할당: 요청과 기기를 1:1로 배정하는 순수 함수.

기준은 ① 배정 수 최대 ② 비용 `-log(근접확률)` 합 최소 ③ 요청 ID 순 사전식 최소다.
근접 확률을 다시 정규화하거나 임계값을 완화하지 않는다. 허용쌍은 호출자가 거른 것으로 본다.
시간, 설정, 난수를 쓰지 않는다. 같은 입력은 입력 순서와 관계없이 같은 배정을 낸다 (NFR-09).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

COST_TOLERANCE = 1e-9  # 비용 합의 부동소수점 오차. 이 안이면 같은 비용이다.


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    """배정 요청 하나. candidates 는 허용쌍을 이루는 기기 ID → 근접 확률이다."""

    request_id: str
    candidates: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", MappingProxyType(dict(self.candidates)))


# (배정 수, 비용 합, 요청 순서대로의 기기 번호. 배정받지 못하면 None)
_Plan = tuple[int, float, tuple[int | None, ...]]


def allocate(requests: Sequence[AllocationRequest], max_nodes: int = 20) -> dict[str, str]:
    """요청 ID → 기기 ID. 배정받지 못한 요청은 담지 않는다. 입력이 틀리면 ValueError 다."""
    reqs = sorted(requests, key=lambda r: r.request_id)
    _validate(reqs)
    node_ids = sorted({node for r in reqs for node in r.candidates})
    if len(node_ids) > max_nodes:
        raise ValueError(f"후보 기기가 상한을 넘는다: {len(node_ids)} > {max_nodes}")
    number = {node: i for i, node in enumerate(node_ids)}
    # 요청마다 (기기 번호, 비용). 번호 오름차순 = 기기 ID 오름차순이다.
    options = [
        sorted((number[node], -math.log(p)) for node, p in r.candidates.items()) for r in reqs
    ]

    memo: dict[tuple[int, int], _Plan] = {}

    def solve(i: int, used: int) -> _Plan:
        if i == len(reqs):
            return 0, 0.0, ()
        key = (i, used)
        if key in memo:
            return memo[key]
        best: _Plan | None = None
        for j, cost in options[i]:
            if used >> j & 1:
                continue
            count, total, plan = solve(i + 1, used | 1 << j)
            best = _better(best, (count + 1, total + cost, (j, *plan)))
        count, total, plan = solve(i + 1, used)
        best = _better(best, (count, total, (None, *plan)))
        memo[key] = best
        return best

    _, _, plan = solve(0, 0)
    return {r.request_id: node_ids[j] for r, j in zip(reqs, plan) if j is not None}


def _validate(reqs: Sequence[AllocationRequest]) -> None:
    seen: set[str] = set()
    for r in reqs:
        if not r.request_id or r.request_id in seen:
            raise ValueError(f"요청 ID가 비었거나 겹친다: {r.request_id!r}")
        seen.add(r.request_id)
        for node, p in r.candidates.items():
            if not node:
                raise ValueError("기기 ID가 비었다")
            if not (isinstance(p, (int, float)) and math.isfinite(p) and 0 < p <= 1):
                raise ValueError(f"근접 확률은 0 초과 1 이하여야 한다: {node}={p!r}")


def _better(best: _Plan | None, cand: _Plan) -> _Plan:
    """배정 수 → 비용 합 → 요청 순 사전식(배정받는 쪽이 앞선다) 순으로 비교한다."""
    if best is None:
        return cand
    if cand[0] != best[0]:
        return cand if cand[0] > best[0] else best
    if abs(cand[1] - best[1]) > COST_TOLERANCE:
        return cand if cand[1] < best[1] else best
    return cand if _order(cand[2]) < _order(best[2]) else best


def _order(plan: tuple[int | None, ...]) -> tuple[float, ...]:
    return tuple(math.inf if j is None else j for j in plan)
