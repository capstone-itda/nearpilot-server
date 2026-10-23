"""FR-12B 사람 이름표와 대시보드 설정 접점. 사용자 ID 는 이름표를 거쳐 사람 필드로만 나간다."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from _dashboard import settings
from nearpilot.api.ws.names import PersonNames
from nearpilot.shared.enums import Role
from nearpilot.shared.models import Subject


def visitor(n: int) -> Subject:
    return Subject(f"user-visitor-{n}", Role.VISITOR)


def test_FR12B_방문자는_나타난_순서로_글자를_받고_소유자는_소유자():
    names = PersonNames()
    subjects = [Subject("user-a", Role.VISITOR), Subject("user-owner", Role.OWNER),
                Subject("user-b", Role.VISITOR), Subject("user-c", Role.VISITOR)]
    people = [names.person(s) for s in subjects]
    assert [p.pseudonym for p in people] == ["방문자 A", "소유자", "방문자 B", "방문자 C"]
    assert [p.letter for p in people] == ["A", None, "B", "C"]
    assert [p.person_id for p in people] == ["p1", "p2", "p3", "p4"]
    assert [p.role for p in people] == [Role.VISITOR, Role.OWNER, Role.VISITOR, Role.VISITOR]
    # 같은 사용자를 다시 부르면 처음 만든 Person 이다
    assert all(names.person(s) is p for s, p in zip(reversed(subjects), reversed(people)))
    # 사용자 ID 는 이름표에 들어가지 않는다
    assert not any(s.user_id in repr(p) for s, p in zip(subjects, people))

    names = PersonNames()
    visitors = [names.person(visitor(n)) for n in range(53)]
    assert [p.pseudonym for p in visitors[:3]] == ["방문자 A", "방문자 B", "방문자 C"]
    assert visitors[25].pseudonym == "방문자 Z"
    assert (visitors[26].pseudonym, visitors[27].pseudonym) == ("방문자 AA", "방문자 AB")  # 27번째, 28번째
    assert (visitors[51].pseudonym, visitors[52].pseudonym) == ("방문자 AZ", "방문자 BA")
    assert all(p.pseudonym == f"방문자 {p.letter}" for p in visitors)
    assert len({p.person_id for p in visitors}) == 53
    assert not any(f"user-visitor-{n}" in repr(p) for n, p in enumerate(visitors))


def test_FR12B_소유자가_여럿이어도_이름은_소유자이고_person_id로_구분한다():
    names = PersonNames()
    first = names.person(Subject("user-owner-1", Role.OWNER))
    second = names.person(Subject("user-owner-2", Role.OWNER))
    assert first.pseudonym == second.pseudonym == "소유자"
    assert first.letter is None and second.letter is None
    assert first.person_id != second.person_id
    assert names.person(visitor(1)).pseudonym == "방문자 A"  # 소유자는 방문자 글자 순번을 쓰지 않는다


def test_FR12B_이미_본_사용자는_나중에_다른_역할로_와도_처음_이름표를_유지한다():
    names = PersonNames()
    as_visitor = names.person(Subject("user-x", Role.VISITOR))
    as_owner = names.person(Subject("user-y", Role.OWNER))
    assert names.person(Subject("user-x", Role.OWNER)) is as_visitor
    assert names.person(Subject("user-y", Role.VISITOR)) is as_owner
    assert (as_visitor.pseudonym, as_visitor.role) == ("방문자 A", Role.VISITOR)
    assert (as_owner.pseudonym, as_owner.role) == ("소유자", Role.OWNER)
    after = names.person(visitor(1))  # 역할이 달라진 재방문은 번호와 글자를 쓰지 않는다
    assert (after.person_id, after.pseudonym) == ("p3", "방문자 B")


def test_FR12B_여러_스레드가_불러도_person_id와_글자가_겹치지_않는다():
    names, workers, per_worker = PersonNames(), 8, 100

    def work(worker: int):
        return [names.person(Subject(f"user-{worker}-{n}", Role.VISITOR)) for n in range(per_worker)]

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 스레드가 자주 바뀌게 해서 잠금이 없으면 번호가 겹치게 한다
    try:
        with ThreadPoolExecutor(workers) as pool:
            people = [p for batch in pool.map(work, range(workers)) for p in batch]
    finally:
        sys.setswitchinterval(interval)
    total = workers * per_worker
    assert {p.person_id for p in people} == {f"p{n}" for n in range(1, total + 1)}
    assert len({p.pseudonym for p in people}) == total


def test_FR12A_FR12B_대시보드_설정은_경계값을_받는다():
    s = settings(queue_max_events=1, batch_max_events=1, audit_limit_max=1, auth_timeout_sec=0.001)
    assert (s.queue_max_events, s.batch_max_events, s.audit_limit_max, s.auth_timeout_sec) == (1, 1, 1, 0.001)
    assert settings(auth_timeout_sec=2).auth_timeout_sec == 2  # 정수 초도 받는다


INVALID_SETTINGS = (
    [(name, value) for name in ("queue_max_events", "batch_max_events", "audit_limit_max")
     for value in (0, -1, True, 1.0, "10", None)]
    + [("auth_timeout_sec", value) for value in (0, -1.0, True, float("nan"), float("inf"), "1", None)]
)


@pytest.mark.parametrize(("name", "value"), INVALID_SETTINGS)
def test_FR12A_FR12B_대시보드_설정_값이_틀리면_ValueError(name, value):
    with pytest.raises(ValueError, match=name):
        settings(**{name: value})
