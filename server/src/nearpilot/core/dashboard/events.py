"""FR-12A·FR-12B 행 → 모니터링 이벤트와 감사 행 (specs/core-dashboard-queries/spec.md 6절).

DB 에서 읽은 행을 아키텍처 §5 형식의 값으로 바꾼다. 조회 서비스와 따라 읽기가 함께 쓴다.
`MonitorEvent.fields` 의 키는 camelCase 이고 값은 열거형 그대로 둔다 (api/ws/schema.py 가 문자열로 바꾼다).
사람은 `subject` 로만 넘긴다. 시각은 행의 시각을 그대로 쓰고 현재 시각을 읽지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from nearpilot.core.dashboard.steps import judgment, proximity, request_steps, request_tool, responded_at
from nearpilot.core.dashboard.text import audit_code, audit_text, exec_result
from nearpilot.shared.enums import ExecutionStatus, Occupancy, ReasonCode, Step, Verdict
from nearpilot.shared.models import (
    DashboardApproval,
    DashboardAudit,
    DashboardCommand,
    DashboardNode,
    DashboardRequest,
    MonitorEvent,
    Subject,
    TargetSpec,
)

_RUNNING = (ExecutionStatus.PENDING, ExecutionStatus.IN_PROGRESS)  # 아직 끝나지 않은 명령


def target_of(target_spec: str) -> dict[str, Any]:
    """저장된 대상 표기 → {"kind", "capability", "nodeId"?}. 형식이 틀리면 ValueError (TargetSpec.parse)."""
    target = TargetSpec.parse(target_spec)
    out: dict[str, Any] = {"kind": target.kind, "capability": target.capability}
    if target.node_id is not None:
        out["nodeId"] = target.node_id
    return out


def audit_node(
    a: DashboardAudit, requests: Mapping[str, DashboardRequest], commands: Mapping[str, DashboardCommand],
) -> str | None:
    """감사 행이 가리키는 기기. payload 의 node_id, 요청의 node_id, 명령의 node_id 순이고 없으면 None."""
    node_id = a.payload.get("node_id")
    if isinstance(node_id, str):
        return node_id
    req = None if a.internal_request_id is None else requests.get(a.internal_request_id)
    if req is not None and req.node_id is not None:
        return req.node_id
    cmd = None if a.command_id is None else commands.get(a.command_id)
    return None if cmd is None else cmd.node_id


def audit_row(a: DashboardAudit, node_id: str | None, names: Mapping[str, str]) -> dict[str, Any]:
    """REST 감사 행이자 스트림 audit 이벤트의 row."""
    row: dict[str, Any] = {"id": a.log_id, "at": a.created_at, "step": a.step}
    if node_id is not None:
        row["nodeId"] = node_id
    row["text"] = audit_text(a, names)
    row["code"] = audit_code(a)
    return row


def request_events(
    req: DashboardRequest, audits: Sequence[DashboardAudit], commands: Sequence[DashboardCommand],
    subject: Subject | None, occupancy: Occupancy | None, names: Mapping[str, str],
) -> list[MonitorEvent]:
    """요청 하나가 지금까지 만든 이벤트 전체. 상태가 나아가면 앞부분은 그대로이고 뒤에만 붙는다.

    mcp_call, step(닿은 단계), posterior, verdict, 명령마다 command·exec_result, mcp_result 순이다.
    방문자의 request_use 만 도구 호출로 발행한다. 서버 내부 요청, request_access, 아직 감사 행이 없는 요청은
    명령만 발행한다. 사람(subject)이 없는 방문자 요청도 mcp_call 에 사람이 없어 도구 호출로 발행하지 않는다.
    판정(verdict)은 있는데 사유 코드가 없는 행은 ValueError 다 (저장소가 둘을 함께 쓴다).
    """
    rows = sorted(audits, key=lambda a: a.seq)
    rid = req.internal_request_id
    events: list[MonitorEvent] = []
    called = req.kind == "visitor" and subject is not None and request_tool(rows) == "request_use"
    if called:
        events.append(MonitorEvent("mcp_call", req.received_at, {
            "requestId": rid, "tool": "request_use", "target": target_of(req.target_spec),
            "action": req.action}, subject))
        if req.verdict is not None:
            events.extend(_judgment_events(req, rows, occupancy, names))
            if req.verdict is not Verdict.OK:
                events.append(MonitorEvent("mcp_result", events[-1].at, {"requestId": rid}))

    ordered = sorted(commands, key=lambda c: c.seq)
    for cmd in ordered:
        # 보내지 않고 취소한 명령은 전송 시작 뒤여도 발행하지 않는다
        if cmd.send_started_at is not None and cmd.status is not ExecutionStatus.NOT_SENT:
            events.append(MonitorEvent("command", cmd.send_started_at, {
                "requestId": rid, "nodeId": cmd.node_id, "action": cmd.action}))
        result = exec_result(cmd.outcome)
        if result is not None:
            at = cmd.occurred_at or cmd.send_started_at or req.received_at
            events.append(MonitorEvent("exec_result", at, {"requestId": rid, "nodeId": cmd.node_id, "result": result}))
    if called and req.verdict is Verdict.OK and ordered and all(c.status not in _RUNNING for c in ordered):
        events.append(MonitorEvent("mcp_result", events[-1].at, {"requestId": rid}))
    return events


def _judgment_events(
    req: DashboardRequest, rows: Sequence[DashboardAudit], occupancy: Occupancy | None,
    names: Mapping[str, str],
) -> list[MonitorEvent]:
    """판정이 저장된 요청의 step, posterior, verdict. 시각은 판정 행까지만 본다 (뒤에 붙는 실행 행이 바꾸지 못하게)."""
    if req.reason_code is None:
        raise ValueError("판정은 있는데 사유 코드가 없다")
    rid = req.internal_request_id
    decided_at = responded_at(judgment(rows)[0]) or req.received_at
    states = request_steps(req, rows, occupancy, names)
    events: list[MonitorEvent] = []
    for state in states:
        if state.result not in ("pass", "stop"):
            continue
        fields: dict[str, Any] = {"requestId": rid, "step": state.step, "result": state.result}
        if state.detail is not None:
            fields["detail"] = state.detail
        # 새 요청의 ⓪은 감사 행이 없다. 받은 시각으로 두어 이벤트 순서와 시각 순서를 맞춘다
        fallback = req.received_at if state.step is Step.DEDUP else decided_at
        events.append(MonitorEvent("step", state.at or fallback, fields))
    near = proximity(rows)
    if near is not None:
        at = next(s.at for s in states if s.step is Step.PROXIMITY) or decided_at
        events.append(MonitorEvent("posterior", at, {
            "requestId": rid, "posteriors": near[0], "threshold": near[1]}))
    fields = {"requestId": rid, "verdict": req.verdict, "reason": req.reason_code}
    if req.node_id is not None:
        fields["nodeId"] = req.node_id
    events.append(MonitorEvent("verdict", decided_at, fields))
    return events


def transition_event(
    node: DashboardNode, at: datetime, *, overdue: bool, fault: ReasonCode | None,
) -> MonitorEvent:
    """기기 상태 전이. overdue 가 거짓이면 키를 뺀다. fault 는 격리 원인이고 없으면 키를 뺀다."""
    fields: dict[str, Any] = {"nodeId": node.node_id, "to": node.device_state}
    if overdue:
        fields["overdue"] = True
    if fault is not None:
        fields["fault"] = {"cause": fault}
    return MonitorEvent("transition", at, fields)


def beacon_event(subject: Subject, seen: bool, at: datetime) -> MonitorEvent:
    return MonitorEvent("beacon", at, {"seen": seen}, subject)


def approval_event(a: DashboardApproval, kind: str, subject: Subject, at: datetime) -> MonitorEvent:
    """kind 는 requested, approved, denied, expired 중 하나다."""
    return MonitorEvent("approval", at, {
        "requestId": a.internal_request_id, "nodeId": a.node_id, "kind": kind}, subject)
