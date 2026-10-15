"""반납·복구 검사 기준의 개발 기준 (FR-20, NFR-04, #90).

펌웨어 담당이 없는 동안 architecture.md §4 "반납 검사와 종료"의 표를 따른다.
기준의 검사 항목마다 {"value": bool, "observed_at": ISO 8601}이 있어야 유효한 결과다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from nearpilot.shared.enums import SafetyCheckOutcome
from nearpilot.shared.models import CheckCriteria

# 기준 id → 검사 항목. 실제 기기를 검증한 뒤 펌웨어 담당이 고칠 수 있다.
CHECK_ITEMS: Mapping[str, tuple[str, ...]] = {
    "locker-return": ("door_closed", "locked"),
    "light-return": ("actuator_idle",),
    "speaker-return": ("actuator_idle",),
}


def criteria_of(release_policy: Mapping[str, Any]) -> CheckCriteria | None:
    """종료 정책의 check_criteria를 읽는다. 없거나 형식이 다르면 None이다."""
    raw = release_policy.get("check_criteria")
    if not isinstance(raw, Mapping):
        return None
    criteria_id, version = raw.get("id"), raw.get("version")
    if not isinstance(criteria_id, str) or not criteria_id:
        return None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    return CheckCriteria(criteria_id, version)


def evidence_valid(
    criteria: CheckCriteria, outcome: SafetyCheckOutcome, evidence: Mapping[str, Any],
) -> bool:
    """passed·not_ready 결과의 근거가 기준의 항목을 모두 담는지 검사한다.

    passed는 모든 항목이 true여야 하고 not_ready는 하나 이상 false여야 한다.
    다른 결과(fault·unknown)는 항목을 검사하지 않는다. 개발 기준에 없는 기준은 유효하지 않다.
    """
    if outcome not in (SafetyCheckOutcome.PASSED, SafetyCheckOutcome.NOT_READY):
        return True
    items = CHECK_ITEMS.get(criteria.criteria_id)
    if items is None:
        return False
    values = []
    for name in items:
        item = evidence.get(name)
        if not isinstance(item, Mapping) or not isinstance(item.get("value"), bool):
            return False
        observed_at = item.get("observed_at")
        if not isinstance(observed_at, str):
            return False
        try:
            datetime.fromisoformat(observed_at)
        except ValueError:
            return False
        values.append(item["value"])
    return all(values) if outcome == SafetyCheckOutcome.PASSED else not all(values)
