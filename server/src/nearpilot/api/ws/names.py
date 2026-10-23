"""FR-11·FR-12B 사용자 ID 를 사람 이름표로 바꾼다 (architecture.md §5, spec 6절). 이름은 서버 프로세스 동안 유지한다."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from nearpilot.shared.enums import Role
from nearpilot.shared.models import Subject


@dataclass(frozen=True, slots=True)
class Person:
    """사용자 ID 를 지운 사람. 스트림과 조회가 이 값으로만 사람을 내보낸다."""

    person_id: str  # "p1", "p2", … 불투명한 값
    pseudonym: str  # "방문자 A", "소유자"
    letter: str | None  # 방문자만 "A", "B", …
    role: Role


def _letter(index: int) -> str:
    """0부터 센 순번을 A, B, …, Z, AA, AB, … 로 쓴다 (26진 전단사 표기)."""
    letter = ""
    n = index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        letter = chr(ord("A") + rest) + letter
    return letter


class PersonNames:
    """사용자 ID 마다 사람 하나를 붙인다. 스트림과 조회가 한 개체를 함께 써서 같은 사람이 같은 이름을 받는다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # 여러 스레드가 부른다
        self._people: dict[str, Person] = {}
        self._visitors = 0  # 글자를 받은 방문자 수. 다음 방문자의 순번이다

    def person(self, subject: Subject) -> Person:
        """처음 보는 사용자에게는 새 이름표를 붙이고, 본 사용자에게는 처음 붙인 것을 돌려준다."""
        with self._lock:
            known = self._people.get(subject.user_id)
            if known is not None:
                return known
            if subject.role == Role.OWNER:  # 소유자는 방문자 글자 순번을 쓰지 않는다
                pseudonym, letter = "소유자", None
            else:
                letter = _letter(self._visitors)
                self._visitors += 1
                pseudonym = f"방문자 {letter}"
            person = Person(f"p{len(self._people) + 1}", pseudonym, letter, subject.role)
            self._people[subject.user_id] = person
            return person
