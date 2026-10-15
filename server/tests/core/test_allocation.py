"""FR-23 전역 할당 배정 함수. 순수 함수라서 fake 가 필요 없다."""

from __future__ import annotations

import itertools
import math
import random

import pytest

from nearpilot.core.allocation import AllocationRequest, allocate

EPS = 1e-9


def req(request_id, **candidates) -> AllocationRequest:
    return AllocationRequest(request_id, candidates)


def cost_of(requests, assignment) -> float:
    by_id = {r.request_id: r for r in requests}
    return sum(-math.log(by_id[rid].candidates[node]) for rid, node in assignment.items())


def test_FR23_요청이_없으면_빈_배정이다():
    assert allocate([]) == {}


def test_FR23_허용쌍이_없는_요청은_배정하지_않는다():
    assert allocate([req("r1"), req("r2", n1=0.9)]) == {"r2": "n1"}


def test_FR23_요청_하나는_확률이_큰_기기를_받는다():
    assert allocate([req("r1", n1=0.3, n2=0.6)]) == {"r1": "n2"}


def test_FR23_한_기기는_한_요청에만_배정한다():
    result = allocate([req("r1", n1=0.9), req("r2", n1=0.8)])
    assert len(set(result.values())) == len(result) == 1
    assert result == {"r1": "n1"}  # 비용이 더 작은 쪽이 받는다


def test_FR23_배정_수가_비용보다_우선이다():
    requests = [req("r1", n1=0.99, n2=0.5), req("r2", n1=0.9)]
    # r1 만 n1 을 받으면 비용이 더 작지만 배정 수가 1 이다
    assert allocate(requests) == {"r1": "n2", "r2": "n1"}


def test_FR23_배정_수가_같으면_비용_합이_작은_배정이다():
    requests = [req("r1", n1=0.9, n2=0.8), req("r2", n1=0.9, n2=0.1)]
    # r1 이 자기에게 가장 가까운 n1 을 받으면 합이 커진다
    assert allocate(requests) == {"r1": "n2", "r2": "n1"}


def test_FR23_동률이면_요청_ID_순으로_정한다():
    requests = [req("r1", n1=0.5, n2=0.5), req("r2", n1=0.5, n2=0.5)]
    assert allocate(requests) == {"r1": "n1", "r2": "n2"}


def test_FR23_동률이면_앞선_요청이_배정받는다():
    assert allocate([req("r2", n1=0.5), req("r1", n1=0.5)]) == {"r1": "n1"}


def test_FR23_입력_순서와_관계없이_같은_배정이다():
    base = [req("r1", n1=0.5, n2=0.5, n3=0.2), req("r2", n1=0.5, n2=0.5), req("r3", n2=0.7, n3=0.4)]
    expected = allocate(base)
    for perm in itertools.permutations(base):
        assert allocate(list(perm)) == expected
    reordered = [AllocationRequest(r.request_id, dict(reversed(list(r.candidates.items())))) for r in base]
    assert allocate(reordered) == expected


def test_FR23_비용_오차_안이면_동률이다():
    # 확률 차이로 생기는 비용 차이가 오차 안이다. 동률이므로 더 작은 기기 ID 를 준다
    assert allocate([req("r1", a=0.5, b=0.5000000000001)]) == {"r1": "a"}
    # 오차 밖이면 비용이 작은 쪽이다
    assert allocate([req("r1", a=0.5, b=0.51)]) == {"r1": "b"}


def reference_optimum(requests) -> tuple[int, float]:
    """기준 최적해: 가능한 모든 배정에서 최대 배정 수와 그 안의 최소 비용을 구한다."""
    best: tuple[int, float] | None = None
    for choice in itertools.product(*[[None, *r.candidates] for r in requests]):
        used = [c for c in choice if c is not None]
        if len(used) != len(set(used)):
            continue
        cost = sum(-math.log(r.candidates[c]) for r, c in zip(requests, choice) if c is not None)
        if best is None or len(used) > best[0] or (len(used) == best[0] and cost < best[1]):
            best = (len(used), cost)
    assert best is not None
    return best


def test_FR23_50세트가_기준_최적해와_일치한다():
    rng = random.Random(2026)
    for _ in range(50):  # 지표 9: 3명 × 3대
        requests = []
        for i in range(3):
            cands = {f"locker-{j}": rng.uniform(0.05, 1.0) for j in range(1, 4) if rng.random() < 0.7}
            requests.append(AllocationRequest(f"r{i}", cands))
        result = allocate(requests)
        allowed = {r.request_id: r.candidates for r in requests}
        assert all(node in allowed[rid] for rid, node in result.items())  # 허용쌍만
        assert len(set(result.values())) == len(result)  # 중복 배정 0건
        count, cost = reference_optimum(requests)
        assert len(result) == count
        assert abs(cost_of(requests, result) - cost) <= EPS


@pytest.mark.parametrize("p", [0, -0.1, 1.1, float("nan"), float("inf"), "0.5", None])
def test_FR23_잘못된_확률은_ValueError(p):
    with pytest.raises(ValueError):
        allocate([req("r1", n1=p)])


def test_FR23_요청_ID가_겹치면_ValueError():
    with pytest.raises(ValueError):
        allocate([req("r1", n1=0.5), req("r1", n2=0.5)])
    with pytest.raises(ValueError):
        allocate([req("", n1=0.5)])


def test_FR23_후보_기기가_상한을_넘으면_ValueError():
    requests = [req("r1", n1=0.5, n2=0.5, n3=0.5)]
    assert allocate(requests, max_nodes=3) == {"r1": "n1"}
    with pytest.raises(ValueError):
        allocate(requests, max_nodes=2)


def test_FR23_입력을_바꾸지_않는다():
    cands = {"n1": 0.5, "n2": 0.4}
    requests = [AllocationRequest("r1", cands), AllocationRequest("r0", {"n1": 0.9})]
    allocate(requests)
    assert [r.request_id for r in requests] == ["r1", "r0"]
    assert cands == {"n1": 0.5, "n2": 0.4} and dict(requests[0].candidates) == cands
