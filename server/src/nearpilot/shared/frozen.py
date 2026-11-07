"""읽기 전용 매핑. dict JSON 직렬화와 dataclass `asdict`를 그대로 쓴다."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


class FrozenDict(dict):
    """Read-only snapshot that keeps dict JSON and dataclass serialization."""

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("Snapshot mappings cannot be modified")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation
    __ior__ = _reject_mutation

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        return FrozenDict(
            (deepcopy(key, memo), deepcopy(value, memo))
            for key, value in self.items()
        )


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict((key, freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """JSON으로 보낼 일반 dict·list 로 되돌린다."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value
