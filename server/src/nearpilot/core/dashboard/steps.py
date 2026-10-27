"""FR-12A·FR-12B 요청 한 건의 판정 단계 복원 (specs/core-dashboard-queries/spec.md 6절).

요청 행과 감사 행에서 ⓪~⑥ 일곱 칸의 결과를 만든다. 요청 한 건 조회와 따라 읽기의 `step` 이벤트가 같은 함수를
써서 같은 단계 결과를 본다. DB 를 읽지 않고 현재 시각을 읽지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from nearpilot.core.dashboard.text import step_detail
from nearpilot.shared.enums import Occupancy, Step, Verdict
from nearpilot.shared.models import DashboardAudit, DashboardRequest

_PENDING, _PASS, _STOP, _SKIP = "pending", "pass", "stop", "skip"

_STEPS = (
    Step.DEDUP, Step.BEACON, Step.CANDIDATE, Step.PROXIMITY, Step.POLICY, Step.DEVICE_OCCUPANCY,
    Step.USER_OCCUPANCY,
)
_INDEX = {step: i for i, step in enumerate(_STEPS)}


@dataclass(frozen=True, slots=True)
class StepState:
    step: Step  # Step.DEDUP ~ Step.USER_OCCUPANCY
    result: str  # "pending" | "pass" | "stop" | "skip"
    detail: str | None  # text.step_detail 로 만든 문구. 감사 행이 없으면 None
    at: datetime | None  # 그 단계의 감사 행 시각. 없으면 None


def request_tool(audits: Sequence[DashboardAudit]) -> str | None:
    """요청 하나의 감사 행(rowid 순)으로 도구를 가린다. 첫 행이 ④면 "request_access", 아니면 "request_use",
    행이 없으면 None (아직 모른다). `request_access` 는 ④ 행만 남긴다 (core/decision/access.py)."""
    first = min(audits, key=lambda a: a.seq, default=None)
    if first is None:
        return None
    return "request_access" if first.step is Step.POLICY else "request_use"


def request_steps(
    req: DashboardRequest, audits: Sequence[DashboardAudit], occupancy: Occupancy | None,
    names: Mapping[str, str],
) -> list[StepState]:
    """⓪~⑥ 일곱 칸. 판정이 멈춘 단계는 stop, 그 앞은 pass, 뒤는 skip 이다.

    판정이 OK 면 모든 단계가 pass 이고 공용형은 ⑤⑥이 skip 이다. 명시 지정의 ③ 생략 행은 skip 이다.
    판정 중이면 감사 행이 닿은 단계까지 pass,
    나머지는 pending 이다. occupancy 는 req.node_id 기기의 점유형이고 모르면 None 이다 (공용형 규칙을 쓰지 않는다).
    """
    rows, decision = judgment(audits)
    reached = max((_INDEX[a.step] for a in rows), default=-1)
    count = len(_STEPS)
    if req.verdict is None:
        results = [_PASS if i <= reached else _PENDING for i in range(count)]
    elif req.verdict is Verdict.OK:
        results = [_PASS] * count
        if occupancy is Occupancy.SHARED:
            results[_INDEX[Step.DEVICE_OCCUPANCY]:] = [_SKIP, _SKIP]
    else:
        # 멈춘 단계는 마지막 판정 행이 남은 단계다. 판정 행이 없으면(서버 오류, 정리된 감사) 닿은 단계에서 멈춘 것으로 본다
        stop = _INDEX[decision.step] if decision is not None else max(reached, 0)
        results = [_PASS if i < stop else _STOP if i == stop else _SKIP for i in range(count)]
    if results[_INDEX[Step.PROXIMITY]] == _PASS and any(
        a.step is Step.PROXIMITY and a.payload.get("skipped") is True for a in rows
    ):
        results[_INDEX[Step.PROXIMITY]] = _SKIP
    return [_state(step, result, req, rows, decision, names) for step, result in zip(_STEPS, results)]


def proximity(audits: Sequence[DashboardAudit]) -> tuple[Mapping[str, float], float] | None:
    """③ 감사 행의 (posteriors, threshold). ③이 없거나 threshold 가 없으면 None."""
    for a in reversed(judgment(audits)[0]):
        posteriors = a.payload.get("posteriors")
        if a.step is not Step.PROXIMITY or not isinstance(posteriors, Mapping):
            continue
        threshold = a.payload.get("threshold")
        if threshold is None:
            return None
        return {str(node_id): float(p) for node_id, p in posteriors.items()}, float(threshold)
    return None


def responded_at(audits: Sequence[DashboardAudit]) -> datetime | None:
    """요청의 마지막 감사 행 시각. 행이 없으면 None (spec 6절 지연 시간)."""
    last = max(audits, key=lambda a: a.seq, default=None)
    return None if last is None else last.created_at


def judgment(audits: Sequence[DashboardAudit]) -> tuple[list[DashboardAudit], DashboardAudit | None]:
    """판정 단계(⓪~⑥)의 행(rowid 순)과 마지막 판정 행. 마지막 시도의 행만 남긴다.

    오류로 끝난 요청을 다시 판정하면 앞 시도의 행이 그대로 남는다. 마지막 재판정 행(`link_request_retry`)부터가
    마지막 시도다. 판정 행 뒤에 붙은 행은 뺀다. 같은 키를 다른 입력으로 다시 보내면 기존 요청에 ⓪ 행이 뒤늦게
    붙는데, 기존 판정의 단계가 바뀌지 않아야 하고 `step` 이벤트의 앞부분도 그대로여야 한다.
    `verdict` 이벤트의 시각을 정할 때도 events.py 가 이 행들만 본다 (뒤에 붙는 실행 행이 시각을 바꾸지 못하게).
    """
    rows = sorted((a for a in audits if a.step in _INDEX), key=lambda a: a.seq)
    rows = rows[max((i for i, a in enumerate(rows) if _is_retry(a)), default=0):]
    decision = next((a for a in reversed(rows) if _is_decision(a)), None)
    if decision is not None:
        rows = [a for a in rows if a.seq <= decision.seq]
    return rows, decision


def _is_retry(a: DashboardAudit) -> bool:
    """오류로 끝난 요청을 다시 판정하기 시작한 ⓪ 행 (`SqliteRepository` 의 `link_request_retry`)."""
    return a.step is Step.DEDUP and a.payload.get("event") == "link_request_retry"


def _is_decision(a: DashboardAudit) -> bool:
    """판정 단계가 판정을 확정하며 남기는 마지막 행. 단계 상세 행은 `verdict` 가 있어도 `settings_version` 이 없다."""
    return a.step in _INDEX and "verdict" in a.payload and "settings_version" in a.payload


def _state(
    step: Step, result: str, req: DashboardRequest, rows: Sequence[DashboardAudit],
    decision: DashboardAudit | None, names: Mapping[str, str],
) -> StepState:
    if result in (_PENDING, _SKIP):
        return StepState(step, result, None, None)
    row = _row_of(step, req.node_id, rows, decision)
    if row is None:
        return StepState(step, result, None, None)
    return StepState(step, result, step_detail(step, row.payload, names), row.created_at)


def _row_of(
    step: Step, node_id: str | None, rows: Sequence[DashboardAudit], decision: DashboardAudit | None,
) -> DashboardAudit | None:
    """그 단계의 상세 행 하나. 상세 행이 없으면 그 단계에서 멈춘 판정 행이다.

    ⑥은 ⑤와 감사 행 하나를 같이 쓴다. 한 단계에 상세 행이 여럿이면(`any:` 의 ④⑤) 요청 기기의 행을, 없으면 마지막 행을 쓴다.
    """
    source = Step.DEVICE_OCCUPANCY if step is Step.USER_OCCUPANCY else step
    own = [a for a in rows if a.step is source and not _is_decision(a)]
    own = [a for a in own if node_id is not None and a.payload.get("node_id") == node_id] or own
    if own:
        return own[-1]
    return decision if decision is not None and decision.step is step else None
