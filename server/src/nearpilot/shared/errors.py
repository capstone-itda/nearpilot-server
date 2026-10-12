"""계층 간 예외 (architecture.md §4).

판정 결과와 사건 충돌은 값으로 반환한다. 여기에는 저장 계층의 장애만 둔다.
"""


class StoreError(Exception):
    """`ExecutionStore` 저장 오류의 기반 클래스 (#17)."""


class StoreNotApplied(StoreError):
    """변경이 없음이 확실하다. 같은 호출을 다시 해도 된다."""


class StoreOutcomeUnknown(StoreError):
    """반영 여부를 확정하지 못했다. 식별자로 다시 조회한 뒤에 진행한다."""
