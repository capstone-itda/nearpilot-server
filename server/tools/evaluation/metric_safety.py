"""지표 5(정책·발견·차단)와 지표 6(종료·복구)의 시나리오와 집계 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

`run_metric5`는 미등록, 비콘 없음, 무권한, 1회 승인, 기간 승인, 타인 핸들, 미신뢰를 `counts["m5_each"]`번씩 돌려서 허가 없이 기기가 구동되거나
남의 점유가 바뀐 횟수를 센다 (NFR-07). `run_metric6`은 명시 해제, 이탈, 대여시간 초과, 무응답, 명령 처리 실패, 결과 불명, 미완료 명령 중 반납,
타인 반납을 `counts["m6_each"]`번씩 돌려서 자동 반납, 타인 반납, 미완료 명령·장애 중 일반 반납, 소유자 승인 없는 복구, 복구 성공을 센다
(NFR-04, FR-07, FR-20).
서버는 고치지 않는다. MCP와 REST, 읽기 전용 DB 연결, 시뮬레이터의 명령 기록만 쓴다.

기기를 구동했는지는 응답이 아니라 시뮬레이터의 명령 기록으로 센다. 시도마다 요청 ID에 지표, 시나리오, 순번을 넣고, 그 접두어로 시작하는
요청이 만든 명령 가운데 시뮬레이터가 구동을 시작한 것을 센다. 서버가 스스로 낸 명령(공용 조명의 자동 끄기)은 요청 ID가 없어서 들어가지 않는다.

시나리오를 시작할 장면(기기가 AVAILABLE, 열린 세션 없음, 방문자의 기기 목록에 그 기기가 있음, 승인 시나리오는 방문자에게 승인 없음)을
만들지 못하면 그 시도는 `setup_failed`다. 지표의 실패가 아니라서 값과 시험 횟수에 넣지 않고 사유를 원자료에 남긴다. 장면을 만든 뒤
서버가 기대와 다르게 답한 것(승인했는데 구동하지 못함, 장애를 복구하지 못함)은 `setup_failed`가 아니라 실패다. 근접 확률 때문에 거절된
`ASK`와 앞 명령이 끝나지 않아 거절된 `BUSY`는 새 `request_id`로 다시 보낸다 (같은 ID로 다시 보내면 저장된 판정을 돌려준다).

기다리는 시간은 서버 설정(`server.example.toml`의 `[settings]` 위에 평가 설정의 `[server_settings]`를 덮은 값)에서 구한다. 방문자의 기기 앞
위치를 바꾸면 앞 장면의 RSSI 프레임이 `beacon_recent_sec` 동안 서버에 남아서 그만큼 기다린다. 기기가 새 상태를 보고하고 서버가 반영하는 데
걸리는 시간의 상한은 상태·RSSI 보고 주기 가운데 큰 값(`margin`)으로 본다. 이탈은 `leave_absent_sec + leave_grace_sec`, 대여시간 초과는
`rental_duration_sec`, 장애는 `exec_timeout_sec`에 `margin`을 더해 기다린다. 이 지표에 무작위 입력은 없다. 방문자는 순번으로 돌려 쓰고
`seed`는 쓰지 않는다.

승인 종류는 기기의 `approval_mode`로 고른다. 1회 승인은 `one_time`, 기간 승인은 `time_window` 기기에서 돌리고, 설정에 그 기기가 없으면 그
시나리오는 건너뛰고 사유를 행의 note와 원자료에 쓴다. 기간 승인은 시도마다 승인의 유효시간이 끝나기를 기다려서 다음 시도에 승인이 남지
않게 하므로 `approval_ttl_sec`이 있어야 한다.
"""

from __future__ import annotations

import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from functools import partial
from typing import Any

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    AccessMode, ApprovalMode, Capability, DeviceState, EndReason, ExecutionStatus, Occupancy, ReasonCode, RecoveryStatus,
    Role, SessionState, Verdict)
from tools.evaluation.harness import Harness
from tools.evaluation.metric_load import _actuated, _hear, _release, _run_parallel
from tools.evaluation.rows import MetricOutcome, metric_row
from tools.evaluation.server_config import EvalConfig, EvalConfigError, NodeSpec

# 시도의 `scenario` 값. 행마다 어느 시나리오의 시도를 세는지가 이 이름으로 정해진다.
M5_SCENARIOS = ("unregistered", "no_beacon", "unauthorized", "one_time", "time_window", "foreign_handle", "untrusted")
M6_SCENARIOS = ("explicit_release", "leave", "rental_overrun", "no_response", "command_failed", "result_unknown",
                "pending_command_release", "foreign_release")
_APPROVAL_SCENARIOS = ("one_time", "time_window")  # 소유자가 승인한 요청이 구동되는지 보는 시나리오
_LEAVE_SCENARIOS = ("leave", "rental_overrun")  # 서버가 점유를 닫을 수 있는 기회
_FAULT_SCENARIOS = ("no_response", "command_failed", "result_unknown")  # 기기가 FAULT가 되는 시나리오
_UNFINISHED_SCENARIOS = (*_FAULT_SCENARIOS, "pending_command_release")  # 방문자의 반납이 일반 반납이 되면 안 되는 시나리오

_ACTION = {Capability.LOCKER: "open", Capability.LIGHT: "on", Capability.SPEAKER: "on"}  # 기기 종류마다 요청하는 동작
_APPROVAL_REQUIRED = "approval_required"  # `find_available_devices`의 `access` 값 (MCP 계약)
_STRANGER = "unregistered"  # 틀린 토큰으로 보낼 때 쓰는 가짜 사용자 ID
_NORMAL = {"final": "succeeded", "drop_final_result": False, "result_delay_sec": None}  # 장애를 걷은 시뮬레이터 시나리오
# 결과 불명의 결과는 `exec_timeout_sec`에 `margin`을 이만큼 더한 뒤에 온다: 서버가 FAULT로 본 뒤 복구 확인(보고 몇 주기)이 끝나고 온다
_LATE_MARGINS = 6
# 요청 ID가 `?`로 시작하는 방문자 요청의 명령 가운데 서버가 보내려고 한 것. 서버가 스스로 낸 명령은 요청 ID가 없어서 빠진다.
_COMMANDS = (
    "SELECT c.command_id, c.node_id FROM node_command c JOIN request r ON r.internal_request_id = c.internal_request_id "
    "WHERE c.send_attempted = 1 AND substr(r.request_id, 1, ?) = ?"
)


class _SetupFailed(Exception):
    """시나리오를 시작할 장면을 만들지 못했다. 지표의 실패가 아니라서 시도를 `setup_failed`로 센다."""


def run_metric5(h: Harness, config: EvalConfig) -> MetricOutcome:
    """지표 5의 시나리오를 `counts["m5_each"]`번씩 돌리고 집계한다.

    시나리오는 `M5_SCENARIOS` 순서다. 설정에 필요한 기기가 없으면 그 시나리오는 건너뛴다.
      unregistered   토큰 없음과 틀린 토큰으로 `request_use`. 401이고 구동 0이어야 한다.
      no_beacon      유효한 토큰이지만 어느 기기도 방문자의 비콘을 듣지 못하는 장면에서 `request_use`. 거절되고 구동 0이어야 한다.
      unauthorized   제한 기기에 승인 없이 `request_use`. 승인 필요로 거절되고 구동 0이어야 한다.
      one_time       1회 승인(`one_time` 제한 기기): 승인 → 요청 OK(구동 1) → 반납 → 다시 요청은 승인이 필요하고 구동 0.
      time_window    기간 승인(`time_window` 제한 기기): 승인 → 유효시간 안에 두 번 OK → 유효시간이 끝난 뒤 요청은 구동 0.
      foreign_handle 방문자 B가 A의 `use_id`로 `release_use`와 `get_use_status`. 거부되고 A의 세션은 그대로여야 한다.
      untrusted      미신뢰 기기 앞의 방문자. 기기 목록에 그 기기가 없고 `request_use`는 구동 0이어야 한다.
    """
    bench = _Bench(h, config, "m5")
    each = _count(config.counts, "m5_each")
    summarize_metric5([], config.acceptance)  # 통과 기준이 빠졌으면 긴 시나리오를 돌리기 전에 알린다
    scenarios = (
        ("unregistered", _unregistered, bench.open_node, "신뢰하는 개방 기기가 설정에 없다"),
        ("no_beacon", _no_beacon, bench.open_node, "신뢰하는 개방 기기가 설정에 없다"),
        ("unauthorized", _unauthorized, bench.restricted, "신뢰하는 제한 기기(mode = restricted)가 설정에 없다"),
        ("one_time", _one_time, bench.one_time, "approval_mode = one_time인 제한 기기가 설정에 없다"),
        ("time_window", _time_window, bench.time_window,
         "approval_mode = time_window이고 approval_ttl_sec가 있는 제한 기기가 설정에 없다"),
        ("foreign_handle", _foreign_handle, bench.rental, "신뢰하는 개방 대여형 기기가 설정에 없다"),
        ("untrusted", _untrusted, bench.untrusted, "신뢰하지 않는 기기(trusted = false)가 설정에 없다"),
    )
    attempts, skipped = _run(bench, scenarios, each)
    rows = summarize_metric5(attempts, config.acceptance)
    _note_skipped(rows, skipped, {"one_time": "m5_valid_approval_success", "time_window": "m5_valid_approval_success",
                                  "untrusted": "m5_untrusted_candidates"}, default="m5_out_of_scope")
    return MetricOutcome(rows=rows, raw=_raw(bench, attempts, skipped))


def run_metric6(h: Harness, config: EvalConfig) -> MetricOutcome:
    """지표 6의 시나리오를 `counts["m6_each"]`번씩 돌리고 집계한다.

    첫 신뢰 개방 대여형 기기에서 `M6_SCENARIOS` 순서로 돌린다.
      explicit_release 대여 → `release_use` → 세션 CLOSED. 반납이 되는지 보는 기준이고 행은 없다 (원자료의 `released`).
      leave            대여 중 방문자의 비콘을 끊고 이탈 판단 시간 + 여유를 기다린다. 세션이 열려 있어야 한다.
      rental_overrun   대여 시간을 넘기고 여유를 기다린다. 세션이 열려 있어야 한다.
      no_response      `drop_final_result`: 최종 결과가 오지 않는다.
      command_failed   `final = failed`: 명령 처리가 실패한다.
      result_unknown   `result_delay_sec` > `exec_timeout_sec`: 결과가 제한 시간 뒤에 온다.
                       셋은 기기가 FAULT가 되길 기다린 뒤 같은 순서로 확인한다: 방문자의 `release_use`는 일반 반납이 되지 않아야 하고,
                       방문자의 복구 승인은 거부되고, 준비 전 소유자 승인도 거부되어야 한다. 시나리오를 정상으로 되돌리고 `ready`가 되면
                       소유자 승인으로 `completed`와 기기 AVAILABLE이 되어야 한다.
      pending_command_release  대여 명령에 기기가 제한 시간 안에 늦게 답한다. 명령이 처리 중이고 기기가 아직 FAULT가 아닐 때 같은 방문자가
                       `release_use`를 보낸다. 서버가 반납을 보류해야 하고 세션이 일반 반납으로 닫히면 실패다. 장애 중 일반 반납과
                       같은 행(`m6_release_during_fault`)에 센다.
      foreign_release  방문자 B가 A의 대여를 반납한다. 거부되고 A의 세션은 그대로여야 한다.
    """
    bench = _Bench(h, config, "m6")
    if bench.rental is None:
        raise EvalConfigError("nodes: 신뢰하는 개방 대여형 기기가 한 대 이상 있어야 한다 (지표 6이 점유를 만든다)")
    each = _count(config.counts, "m6_each")
    summarize_metric6([], config.acceptance)  # 통과 기준이 빠졌으면 긴 시나리오를 돌리기 전에 알린다
    scenarios = (
        ("explicit_release", _explicit_release, bench.rental, ""),
        ("leave", _leave, bench.rental, ""),
        ("rental_overrun", _rental_overrun, bench.rental, ""),
        *((name, _fault(name), bench.rental, "") for name in _FAULT_SCENARIOS),
        ("pending_command_release", _pending_command_release, bench.rental, ""),
        ("foreign_release", _foreign_release, bench.rental, ""),
    )
    attempts, skipped = _run(bench, scenarios, each)
    return MetricOutcome(rows=summarize_metric6(attempts, config.acceptance), raw=_raw(bench, attempts, skipped))


def summarize_metric5(attempts: Sequence[Mapping[str, Any]], acceptance: Mapping[str, float]) -> list[dict[str, Any]]:
    """지표 5의 행 셋. 통과 기준은 `acceptance`에서 읽는다.

    `setup_failed` 시도는 값과 시험 횟수에 넣지 않는다. 시험 횟수는 보낸 시도 수가 아니라 그 행이 판정하는 시나리오의 시도 수다:
    범위 밖 조작은 모든 시나리오가, 미신뢰 후보는 `untrusted`만 판정한다. 유효 승인 성공은 `approval_ok`가 있는 시도(1회, 기간 승인)다.
    """
    return [
        _count_row("m5_out_of_scope", _limit(acceptance, "m5_max_out_of_scope"), attempts, M5_SCENARIOS, "out_of_scope",
                   "허가 없이 구동되거나 남의 점유가 바뀐 조작 수. 미등록, 비콘 근거 없는 요청, 무권한, 승인 소진·만료 뒤 요청, "
                   "타인 핸들, 미신뢰 기기 앞의 요청을 시뮬레이터 구동 기록과 응답으로 센다"),
        _count_row("m5_untrusted_candidates", _limit(acceptance, "m5_max_untrusted_candidates"), attempts, ("untrusted",),
                   "untrusted_candidate", "미신뢰 기기가 기기 목록이나 요청 응답에 나온 횟수"),
        _rate_row("m5_valid_approval_success", _limit(acceptance, "m5_min_valid_approval_success_rate"), attempts,
                  _APPROVAL_SCENARIOS, "approval_ok",
                  "소유자가 승인한 요청이 OK로 구동된 비율 (1회 승인, 기간 승인. 기간 승인은 유효시간 안의 두 요청이 모두 구동돼야 성공)"),
    ]


def summarize_metric6(attempts: Sequence[Mapping[str, Any]], acceptance: Mapping[str, float]) -> list[dict[str, Any]]:
    """지표 6의 행 셋. 통과 기준은 `acceptance`에서 읽는다.

    `setup_failed` 시도는 값과 시험 횟수에 넣지 않는다. 시험 횟수는 그 행이 판정하는 시나리오의 시도 수다: 자동 반납은 이탈·대여시간 초과,
    타인 반납은 `foreign_release`, 미완료 명령·장애 중 반납은 장애 셋과 `pending_command_release`, 승인 없는 복구는 장애 셋이다.
    복구 성공은 `recovered`가 있는 시도(장애 셋)다.
    """
    return [
        _count_row("m6_auto_release", _limit(acceptance, "m6_max_auto_release"), attempts, _LEAVE_SCENARIOS, "auto_release",
                   "이탈이나 대여시간 초과 뒤 서버가 점유를 닫은 횟수"),
        _count_row("m6_foreign_release", _limit(acceptance, "m6_max_foreign_release"), attempts, ("foreign_release",),
                   "foreign_release", "다른 방문자의 반납이 받아들여진 횟수"),
        _count_row("m6_release_during_fault", _limit(acceptance, "m6_max_release_during_fault"), attempts, _UNFINISHED_SCENARIOS,
                   "release_during_fault",
                   "명령이 끝나지 않았거나 기기가 FAULT인 동안 방문자의 반납이 일반 반납으로 끝난 횟수 "
                   "(무응답, 명령 처리 실패, 결과 불명, 처리 중인 명령)"),
        _count_row("m6_unapproved_recovery", _limit(acceptance, "m6_max_unapproved_recovery"), attempts, _FAULT_SCENARIOS,
                   "unapproved_recovery",
                   "방문자의 승인이나 준비 전의 승인이 받아들여졌거나, 소유자의 준비 상태 승인 없이 FAULT가 풀린 횟수"),
        _rate_row("m6_recovery_success", _limit(acceptance, "m6_min_recovery_success_rate"), attempts, _FAULT_SCENARIOS,
                  "recovered", "FAULT가 된 기기를 소유자 승인으로 복구해 AVAILABLE이 된 비율 (무응답, 명령 처리 실패, 결과 불명)"),
    ]


# ── 집계 ─────────────────────────────────────────────────────────────


def _count_row(metric: str, limit: float, attempts: Sequence[Mapping[str, Any]], scenarios: Sequence[str], field: str,
               note: str) -> dict[str, Any]:
    """`scenarios`의 시도에서 `field`를 합친 값이 한도 이하면 통과인 행. 실패는 `field`가 0보다 큰 시도 수다."""
    counted, note = _select(attempts, scenarios, note)
    if not counted:
        return metric_row(metric, limit=limit, note=note)
    total = sum(attempt[field] for attempt in counted)
    return metric_row(metric, value=total, limit=limit, passed=total <= limit, trials=len(counted),
                      failures=sum(attempt[field] > 0 for attempt in counted), note=note)


def _rate_row(metric: str, limit: float, attempts: Sequence[Mapping[str, Any]], scenarios: Sequence[str], field: str,
              note: str) -> dict[str, Any]:
    """`scenarios`의 시도에서 `field`가 True인 비율이 한도 이상이면 통과인 행. `field`가 None인 시도는 이 행의 시험이 아니다."""
    counted, note = _select(attempts, scenarios, note)
    trials = [attempt for attempt in counted if attempt[field] is not None]
    if not trials:
        return metric_row(metric, limit=limit, note=note)
    failures = sum(not attempt[field] for attempt in trials)
    rate = (len(trials) - failures) / len(trials)
    return metric_row(metric, value=rate, limit=limit, passed=rate >= limit, trials=len(trials), failures=failures,
                      note=note)


def _select(attempts: Sequence[Mapping[str, Any]], scenarios: Sequence[str],
            note: str) -> tuple[list[Mapping[str, Any]], str]:
    """`scenarios`의 시도 가운데 장면을 만든 것과, 만들지 못한 시도 수를 덧붙인 note. 시도가 없으면 재지 못했다고 쓴다."""
    chosen = [attempt for attempt in attempts if attempt["scenario"] in scenarios]
    counted = [attempt for attempt in chosen if not attempt["setup_failed"]]
    if len(counted) < len(chosen):
        note += f". 장면을 만들지 못한 시도 {len(chosen) - len(counted)}건은 세지 않았다"
    if not counted:
        note += ". 장면을 만든 시도가 없어서 재지 못했다"
    return counted, note


def _limit(acceptance: Mapping[str, float], key: str) -> float:
    if key not in acceptance:
        raise EvalConfigError(f"acceptance.{key}: 지표 5·6이 읽는 통과 기준이 없다")
    return acceptance[key]


def _count(counts: Mapping[str, int], key: str) -> int:
    if key not in counts:
        raise EvalConfigError(f"counts.{key}: 지표 5·6이 읽는 횟수가 없다")
    return counts[key]


def _note_skipped(rows: list[dict[str, Any]], skipped: Mapping[str, str], row_of: Mapping[str, str], *,
                  default: str) -> None:
    """건너뛴 시나리오를 그 시나리오를 판정하는 행의 note 끝에 쓴다."""
    by_metric = {row["metric"]: row for row in rows}
    for scenario, reason in skipped.items():
        row = by_metric[row_of.get(scenario, default)]
        row["note"] += f". 건너뜀: {scenario} ({reason})"


def _attempt(scenario: str, index: int, **fields: Any) -> dict[str, Any]:
    """집계가 읽는 키를 모두 가진 시도 한 건. 재지 않은 칸은 0이나 None이다."""
    return {
        "scenario": scenario, "out_of_scope": 0, "untrusted_candidate": 0, "approval_ok": None, "auto_release": 0,
        "foreign_release": 0, "release_during_fault": 0, "unapproved_recovery": 0, "recovered": None,
        "setup_failed": False, "index": index, **fields,
    }


def _raw(bench: _Bench, attempts: Sequence[Mapping[str, Any]], skipped: Mapping[str, str]) -> dict[str, Any]:
    return {"attempts": list(attempts), "skipped": dict(skipped), "waits": bench.waits()}


def _run(bench: _Bench, scenarios: Sequence[tuple[str, Callable[..., dict[str, Any]], Any, str]],
         each: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """시나리오를 차례로 `each`번씩 돌린다. 필요한 기기가 없는 시나리오는 건너뛰고 사유를 돌려준다."""
    attempts: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    for name, scenario, needs, why in scenarios:
        if needs is None:
            skipped[name] = why
            continue
        for index in range(each):
            try:
                fields = scenario(bench, index)
            except _SetupFailed as failed:
                fields = {"setup_failed": True, "reason": str(failed)}
            attempts.append(_attempt(name, index, **fields))
    return attempts, skipped


# ── 시나리오 도구 ────────────────────────────────────────────────────


class _Bench:
    """시나리오가 같이 쓰는 도구: 장면, 판정 호출, 구동 수, 대기, 승인과 복구 REST. 기다리는 시간은 서버 설정에서 구한다."""

    def __init__(self, h: Harness, config: EvalConfig, prefix: str) -> None:
        self.h, self.config, self.prefix = h, config, prefix
        example = tomllib.loads((h.server_root / "config" / "server.example.toml").read_text(encoding="utf-8"))
        self.settings = Settings(**{**example["settings"], **config.server_settings})  # 서버가 쓰는 값과 같다 (`Harness._start`)
        self.margin = max(self.settings.status_report_sec, self.settings.rssi_frame_sec)
        self.limit = config.startup_timeout_sec  # 조건이 나타나기를 기다리는 시간의 한계
        self.near = config.rssi["near"]
        self.owner = next(user.user_id for user in config.users if user.role == Role.OWNER)
        self.visitors = [user.user_id for user in config.users if user.role == Role.VISITOR]
        if len(self.visitors) < 2:
            raise EvalConfigError("users: 방문자(role = visitor)가 둘 이상 있어야 한다 (타인 핸들과 타인 반납이 다른 방문자를 쓴다)")
        trusted = [node for node in config.nodes if node.trusted]
        opened = [node for node in trusted if node.mode == AccessMode.OPEN]
        restricted = [node for node in trusted if node.mode == AccessMode.RESTRICTED]
        self.open_node = next(iter(opened), None)
        self.rental = next((node for node in opened if node.occupancy == Occupancy.RENTAL), None)
        self.restricted = next(iter(restricted), None)
        self.one_time = next((node for node in restricted if node.approval_mode == ApprovalMode.ONE_TIME), None)
        self.time_window = next((node for node in restricted if node.approval_mode == ApprovalMode.TIME_WINDOW
                                 and node.approval_ttl_sec is not None), None)
        self.untrusted = next((node for node in config.nodes if not node.trusted), None)
        self._heard: dict[str, dict[str, float]] | None = None  # 지금 장면. None이면 앞 지표가 남긴 장면을 모른다

    def waits(self) -> dict[str, float]:
        """원자료에 남기는, 설정에서 구한 기다리는 시간(초)."""
        settings = self.settings
        return {"margin": self.margin, "scene": settings.beacon_recent_sec + self.margin,
                "leave": settings.leave_absent_sec + settings.leave_grace_sec + self.margin,
                "rental_overrun": settings.rental_duration_sec + self.margin,
                "fault": settings.exec_timeout_sec + self.margin,
                "limit": self.limit}

    # 장면

    def visitor(self, index: int) -> str:
        return self.visitors[index % len(self.visitors)]

    def stranger(self, index: int) -> str:
        return self.visitors[(index + 1) % len(self.visitors)]

    def everyone_near(self, node: NodeSpec) -> dict[str, dict[str, float]]:
        """모든 방문자가 `node` 앞에 서 있고 다른 기기는 아무도 듣지 못하는 장면 (값이 없는 앵커는 근접 확률의 증거가 아니다)."""
        return {node.node_id: {visitor: self.near for visitor in self.visitors}}

    def hear(self, heard: Mapping[str, Mapping[str, float]]) -> None:
        _hear(self.h, self.config, heard)
        self._heard = {node_id: dict(users) for node_id, users in heard.items()}

    def scene(self, heard: Mapping[str, Mapping[str, float]]) -> None:
        """장면을 바꾼다. 사라지는 (기기, 방문자) 쌍이 있으면 그 프레임이 서버에 `beacon_recent_sec` 동안 남아서 그만큼 기다린다."""
        before = self._heard
        self.hear(heard)
        if before is None or _pairs(before) - _pairs(heard):
            self.pause(self.settings.beacon_recent_sec + self.margin)

    def pause(self, seconds: float) -> None:
        self.h.wait_until(lambda: False, seconds)  # 브로커나 서버가 끝났으면 `HarnessError`다

    def rid(self, scenario: str, index: int, step: str) -> str:
        """시도의 요청 ID. 접두어(`step`이 빈 문자열)로 그 시도의 요청을 모두 찾는다."""
        return f"{self.prefix}-{scenario}-{index:03d}-{step}"

    # 호출

    def call(self, user_id: str | None, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """MCP 도구를 한 번 부르고 응답을 원자료 한 건으로 줄인다. HTTP 오류나 읽을 수 없는 결과는 verdict가 None이다."""
        status, result, _ = self.h.mcp(user_id, tool, arguments)
        result = result or {}
        return {"tool": tool, "user_id": user_id, "status": status, "verdict": result.get("verdict"),
                "reason": result.get("reason"), "node_id": result.get("node_id"), "use_id": result.get("use_id"),
                "state": result.get("state"), "overtime_sec": result.get("overtime_sec")}

    def request(self, user_id: str, request_id: str, node: NodeSpec) -> list[dict[str, Any]]:
        """`node`와 같은 종류의 기기를 `nearest:`로 요청한다. ASK와 BUSY는 새 `request_id`로 다시 보내고 보낸 호출을 모두 돌려준다.

        근접 확률이 임계값에 닿기 전이나 앞 명령이 끝나기 전에 보내면 거절된다. 한도는 `limit`을 `margin`으로 나눈 횟수다.
        """
        arguments = {"target_spec": f"nearest:{node.capability}", "action": _ACTION[node.capability]}
        calls: list[dict[str, Any]] = []
        for _ in range(max(1, int(self.limit / self.margin))):
            suffix = f"-r{len(calls)}" if calls else ""
            calls.append(self.call(user_id, "request_use", {**arguments, "request_id": request_id + suffix}))
            if calls[-1]["verdict"] not in (Verdict.ASK, Verdict.BUSY):
                break
            self.pause(self.margin)
        return calls

    def anonymous(self, request_id: str, node: NodeSpec) -> list[dict[str, Any]]:
        """토큰 없이, 그리고 틀린 토큰으로 `request_use`를 보낸다."""
        arguments = {"target_spec": f"nearest:{node.capability}", "action": _ACTION[node.capability]}
        tokens = self.h.tokens
        self.h.tokens = {**tokens, _STRANGER: "not-a-token"}
        try:
            return [self.call(None, "request_use", {**arguments, "request_id": request_id + "-none"}),
                    self.call(_STRANGER, "request_use", {**arguments, "request_id": request_id + "-wrong"})]
        finally:
            self.h.tokens = tokens

    def access(self, user_id: str, node_id: str) -> str | None:
        """방문자의 기기 목록에서 `node_id`의 `access`. 목록에 없으면 None이다."""
        _, found, _ = self.h.mcp(user_id, "find_available_devices", {})
        return next((device["access"] for device in (found or {}).get("devices", []) if device["node_id"] == node_id), None)

    def devices(self, user_id: str) -> list[str]:
        _, found, _ = self.h.mcp(user_id, "find_available_devices", {})
        return [device["node_id"] for device in (found or {}).get("devices", [])]

    def wait_access(self, user_id: str, node_id: str) -> str:
        """방문자의 기기 목록에 `node_id`가 나올 때까지 기다리고 그 `access`를 돌려준다. 기다린 시간은 값에 넣지 않는다."""
        seen: list[str | None] = [None]

        def listed() -> bool:
            seen[0] = self.access(user_id, node_id)
            return seen[0] is not None

        if not self.h.wait_until(listed, self.limit):
            raise _SetupFailed(f"{user_id}의 기기 목록에 {node_id}가 {self.limit:g}초 안에 나오지 않았다")
        return seen[0]  # type: ignore[return-value]

    # 저장된 상태

    def device_state(self, node_id: str) -> str:
        return self.h.query("SELECT device_state FROM node WHERE node_id = ?", node_id)[0]["device_state"]

    def pending(self, node_id: str) -> bool:
        """기기에 결과를 기다리는 명령이 있다. 서버는 이 동안 반납을 시작하지 않는다 (FR-20)."""
        return bool(self.h.query("SELECT 1 FROM node_command WHERE node_id = ? AND status = ?",
                                 node_id, ExecutionStatus.IN_PROGRESS.value))

    def session(self, use_id: str) -> dict[str, Any] | None:
        rows = self.h.query(
            "SELECT use_id, user_id, state, end_reason, closed_at FROM use_session WHERE use_id = ?", use_id)
        return dict(rows[0]) if rows else None

    def open_use(self, node_id: str, user_id: str) -> str | None:
        """방문자가 `node_id`에 열어 둔 점유의 ID. 거부 응답에는 `use_id`가 없어서 DB에서 읽는다."""
        rows = self.h.query("SELECT use_id FROM use_session WHERE node_id = ? AND user_id = ? AND closed_at IS NULL",
                            node_id, user_id)
        return rows[0]["use_id"] if rows else None

    def settle(self, node_id: str) -> None:
        """기기가 AVAILABLE이고 열린 세션이 없을 때까지 기다린다. 앞 시도의 점유가 남아 있으면 이 시도의 값이 달라진다.

        `TARGET_UNAVAILABLE` 뒤에도 `RESERVED` 세션이 `reservation_wait_sec` 동안 남아서 그 시간까지 기다린다.
        """
        def idle() -> bool:
            return (self.device_state(node_id) == DeviceState.AVAILABLE
                    and not self.h.query("SELECT 1 FROM use_session WHERE node_id = ? AND closed_at IS NULL", node_id))

        if not self.h.wait_until(idle, max(self.limit, self.settings.reservation_wait_sec + self.margin)):
            raise _SetupFailed(f"{node_id}가 AVAILABLE이 되지 않았다 (device_state {self.device_state(node_id)})")

    def at_rest(self, node_id: str) -> bool:
        """기기가 제한 시간 안에 AVAILABLE이 되고 열린 세션이 없다."""
        try:
            self.settle(node_id)
        except _SetupFailed:
            return False
        return True

    def restore(self, node_id: str) -> None:
        """정리: FAULT로 남은 기기를 정상 절차(준비 확인, 소유자 승인)로 복구한다. 못 하면 다음 시도의 장면 확인이 잡는다."""
        if self.device_state(node_id) == DeviceState.FAULT:
            ready = self.wait_ready(node_id)
            if ready:
                self.approve_recovery(self.owner, node_id, ready["recovery_id"])

    def idle(self, node_id: str) -> None:
        """시뮬레이터가 제어 신호 출력을 마칠 때까지 기다린다. 출력 중에 보낸 명령은 기기가 거부한다."""
        self.h.wait_until(lambda: not self.h.nodes[node_id].busy, self.limit)

    def actuations(self, request_id: str) -> int:
        """요청 ID가 `request_id`로 시작하는 요청의 명령 가운데 시뮬레이터가 구동을 시작한 수.

        서버가 보내려고 한 명령은 시뮬레이터가 받아서 처리를 시작할 때까지 기다린다. 보내려고 하지 않은 명령은 구동할 수 없다.
        """
        count = 0
        for row in self.h.query(_COMMANDS, len(request_id), request_id):
            store = self.h.nodes[row["node_id"]].store
            record = None

            def received() -> bool:
                nonlocal record
                record = store.command(row["command_id"])
                return record is not None and record.state != "accepted"

            self.h.wait_until(received, self.limit)
            count += record is not None and _actuated(record)
        return count

    def breaches(self, calls: Sequence[Mapping[str, Any]], request_id: str) -> int:
        """허가 없이 처리된 요청 수. OK 응답 수와 구동 수 가운데 큰 쪽이다 (OK는 보통 명령 하나를 구동한다)."""
        granted = sum(call["verdict"] == Verdict.OK for call in calls)
        return max(granted, self.actuations(request_id))

    # 점유

    def rent(self, user_id: str, request_id: str, node: NodeSpec) -> str:
        """방문자가 기기를 빌려서 점유가 ACTIVE가 될 때까지 기다린다. 못 빌리면 `_SetupFailed`다. 점유 ID를 돌려준다."""
        last = self.request(user_id, request_id, node)[-1]
        if last["verdict"] != Verdict.OK:
            raise _SetupFailed(f"request_use가 허가되지 않았다: {last['verdict']}/{last['reason']}")
        use_id = last["use_id"] or self.open_use(node.node_id, user_id)
        if use_id is None:
            raise _SetupFailed("request_use가 OK인데 열린 점유가 없다")
        if not self.h.wait_until(lambda: (self.session(use_id) or {}).get("state") == SessionState.ACTIVE, self.limit):
            raise _SetupFailed("점유가 ACTIVE가 되지 않았다")
        return use_id

    def give_back(self, user_id: str, use_id: str | None, node_id: str) -> None:
        """정리: 점유를 반납하고 기기가 AVAILABLE이 되기를 기다린다. 못 하면 다음 시도의 장면 확인이 잡는다."""
        if use_id is not None:
            _release(self.h, self.config, user_id, use_id)
        try:
            self.settle(node_id)
        except _SetupFailed:
            pass

    # 승인과 복구

    def approve(self, user_id: str, node: NodeSpec, request_id: str) -> Mapping[str, Any]:
        """방문자가 `request_access`로 승인을 신청하고 소유자가 `GET /approvals`에서 찾아 승인한다. 승인 응답을 돌려준다.

        신청과 승인이 이뤄지지 않으면 승인된 상태를 만들지 못한 것이라 `_SetupFailed`다. 신청에는 요청자 정보가 없어서
        신청 전 목록과 비교해 새로 생긴 것을 고른다. 이미 대기 중인 신청이 있으면 새로 만들지 않으므로 그 신청을 고른다.
        """
        action = _ACTION[node.capability]
        before = {item["approval_id"] for item in self._pending()}
        asked = self.call(user_id, "request_access", {"node_id": node.node_id, "action": action, "request_id": request_id})
        if (asked["verdict"], asked["reason"]) != (Verdict.NEED_APPROVAL, ReasonCode.APPROVAL_PENDING):
            raise _SetupFailed(f"request_access가 승인 신청을 만들지 못했다: {asked['verdict']}/{asked['reason']}")
        mine = [item for item in self._pending() if item["node_id"] == node.node_id and item["action"] == action]
        chosen = [item for item in mine if item["approval_id"] not in before] or mine
        if not chosen:
            raise _SetupFailed("소유자의 승인 대기 목록에 신청이 없다")
        newest = max(chosen, key=lambda item: item["created_at"])
        status, body = self.h.rest(self.owner, "POST", f"/approvals/{newest['approval_id']}/decision",
                                   {"decision": "approved"})
        if status != 200 or not isinstance(body, dict):
            raise _SetupFailed(f"소유자 승인이 {status}로 끝났다")
        return body

    def _pending(self) -> list[Mapping[str, Any]]:
        status, items = self.h.rest(self.owner, "GET", "/approvals")
        if status != 200 or not isinstance(items, list):
            raise _SetupFailed(f"GET /approvals가 {status}로 끝났다")
        return items

    def now(self) -> datetime:
        """서버와 같은 PC의 현재 시각. 승인의 유효시간과 견준다."""
        return datetime.now(timezone.utc)

    def wait_expiry(self, grant: Mapping[str, Any]) -> None:
        """승인의 유효시간(`valid_until`)이 끝나기를 기다린다. 서버와 같은 PC의 시계로 잰다."""
        if not grant.get("valid_until"):
            raise _SetupFailed("승인에 valid_until이 없다")
        remaining = (datetime.fromisoformat(grant["valid_until"]) - self.now()).total_seconds()
        self.pause(max(0.0, remaining) + self.margin)

    def readiness(self, node_id: str) -> Mapping[str, Any]:
        """소유자의 복구 준비 조회. 읽지 못하면 빈 매핑이다."""
        status, body = self.h.rest(self.owner, "GET", f"/admin/nodes/{node_id}/recovery")
        return body if status == 200 and isinstance(body, dict) else {}

    def wait_ready(self, node_id: str) -> Mapping[str, Any]:
        """복구 준비 상태가 `ready`가 될 때까지 기다리고 그 조회 결과를 돌려준다. 제한 시간 안에 안 되면 빈 매핑이다."""
        latest: dict[str, Any] = {}

        def ready() -> bool:
            latest.clear()
            latest.update(self.readiness(node_id))
            return latest.get("status") == RecoveryStatus.READY

        return latest if self.h.wait_until(ready, self.limit) else {}

    def approve_recovery(self, user_id: str, node_id: str, recovery_id: str) -> tuple[int, Any]:
        return self.h.rest(user_id, "POST", f"/admin/nodes/{node_id}/recovery/{recovery_id}/approve")


def _pairs(heard: Mapping[str, Mapping[str, float]]) -> set[tuple[str, str]]:
    return {(node_id, user_id) for node_id, users in heard.items() for user_id in users}


def _brief(call: Mapping[str, Any]) -> str:
    """원자료에 남기는 호출 한 줄: 도구, 사용자, HTTP 상태, 판정."""
    return f"{call['tool']} {call['user_id'] or '-'} {call['status']} {call['verdict']}/{call['reason']}"


def _open(session: Mapping[str, Any] | None) -> bool:
    """점유가 이어지는 중이다 (예약됐거나 활성이다)."""
    return session is not None and session["state"] in (SessionState.RESERVED, SessionState.ACTIVE)


def _taken_effect(release: Mapping[str, Any], session: Mapping[str, Any] | None) -> bool:
    """다른 방문자의 반납이 받아들여졌다: OK 응답이거나 점유가 더는 이어지지 않는다."""
    return release["verdict"] == Verdict.OK or not _open(session)


def _normal_release(release: Mapping[str, Any], session: Mapping[str, Any] | None) -> bool:
    """FAULT 중의 반납이 일반 반납으로 끝났다: OK 응답이거나 세션이 `release`로 끝났다. 복구가 정산한 종료는 아니다."""
    return release["verdict"] == Verdict.OK or (session is not None and session["end_reason"] == EndReason.RELEASE)


# ── 지표 5 시나리오 ──────────────────────────────────────────────────


def _unregistered(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.open_node, b.visitor(index)
    b.scene(b.everyone_near(node))
    b.settle(node.node_id)
    b.wait_access(visitor, node.node_id)  # 등록된 방문자는 그 기기를 쓸 수 있는 자리에 있다
    calls = b.anonymous(b.rid("unregistered", index, "a"), node)
    return {"out_of_scope": b.breaches(calls, b.rid("unregistered", index, "")), "trace": [_brief(call) for call in calls]}


def _no_beacon(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.open_node, b.visitor(index)
    b.scene({})  # 어느 기기도 방문자의 비콘을 듣지 못한다. 토큰은 유효하다
    b.settle(node.node_id)
    listed = b.devices(visitor)
    if listed:  # 앞 장면의 프레임이 서버에 남았으면 비콘 근거가 없는 장면이 아니다
        raise _SetupFailed(f"비콘을 듣는 기기가 없는데 {visitor}의 기기 목록에 {listed}가 있다")
    request_id = b.rid("no_beacon", index, "a")
    call = b.call(visitor, "request_use", {"target_spec": f"nearest:{node.capability}", "action": _ACTION[node.capability],
                                           "request_id": request_id})
    return {"out_of_scope": b.breaches([call], request_id), "trace": [_brief(call)]}


def _unauthorized(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.restricted, b.visitor(index)
    b.scene(b.everyone_near(node))
    b.settle(node.node_id)
    # 승인을 받은 적이 없다 (이 시나리오가 승인 시나리오보다 먼저 돈다). 서버가 `operable`로 보이든 구동했는지로 센다
    access = b.wait_access(visitor, node.node_id)
    request_id = b.rid("unauthorized", index, "a")
    calls = b.request(visitor, request_id, node)
    _expect_decided(calls)
    return {"out_of_scope": b.breaches(calls, request_id),
            "trace": [f"기기 목록의 access {access}"] + [_brief(call) for call in calls]}


def _one_time(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.one_time, b.visitor(index)
    b.scene(b.everyone_near(node))
    b.settle(node.node_id)
    _expect_no_approval(visitor, b.wait_access(visitor, node.node_id))
    b.approve(visitor, node, b.rid("one_time", index, "a"))
    use, again = b.rid("one_time", index, "b"), b.rid("one_time", index, "c")
    first = b.request(visitor, use, node)
    granted = first[-1]["verdict"] == Verdict.OK
    ok = granted and b.actuations(use) == 1
    if granted:
        b.idle(node.node_id)
        b.give_back(visitor, first[-1]["use_id"], node.node_id)
    second = b.request(visitor, again, node)  # 승인을 쓴 뒤에는 다시 승인이 필요하다
    _expect_decided(second)
    return {"approval_ok": ok, "out_of_scope": b.breaches(second, again),
            "trace": [_brief(call) for call in first + second]}


def _time_window(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.time_window, b.visitor(index)
    b.scene(b.everyone_near(node))
    b.settle(node.node_id)
    _expect_no_approval(visitor, b.wait_access(visitor, node.node_id))
    grant = b.approve(visitor, node, b.rid("time_window", index, "a"))
    first_id, second_id, late_id = (b.rid("time_window", index, step) for step in ("b1", "b2", "c"))
    first = b.request(visitor, first_id, node)
    b.idle(node.node_id)
    second = b.request(visitor, second_id, node)
    ok = (first[-1]["verdict"] == Verdict.OK and second[-1]["verdict"] == Verdict.OK
          and b.actuations(first_id) == 1 and b.actuations(second_id) == 1)
    b.idle(node.node_id)
    b.give_back(visitor, second[-1]["use_id"] or first[-1]["use_id"], node.node_id)
    b.wait_expiry(grant)  # 다음 시도에 승인이 남지 않게 한다. 유효시간이 지난 요청은 구동하면 안 된다
    late = b.request(visitor, late_id, node)
    _expect_decided(late)
    return {"approval_ok": ok, "out_of_scope": b.breaches(late, late_id),
            "trace": [_brief(call) for call in first + second + late]}


def _foreign_handle(b: _Bench, index: int) -> dict[str, Any]:
    release, status, session = _intrude(b, index, "foreign_handle")
    effects = int(_taken_effect(release, session)) + int(status["state"] is not None)  # 반납이 먹혔거나 상태가 보였다
    return {"out_of_scope": effects, "trace": [_brief(release), _brief(status)]}


def _untrusted(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.untrusted, b.visitor(index)
    b.scene(b.everyone_near(node))  # 미신뢰 기기만 방문자를 듣는다
    listed = b.devices(visitor).count(node.node_id)  # 요청이 기기를 점유하기 전에 본다
    request_id = b.rid("untrusted", index, "a")
    call = b.call(visitor, "request_use", {"target_spec": f"nearest:{node.capability}", "action": _ACTION[node.capability],
                                           "request_id": request_id})
    return {"untrusted_candidate": listed + (call["node_id"] == node.node_id),
            "out_of_scope": b.breaches([call], request_id),
            "trace": [f"기기 목록에 {'있다' if listed else '없다'}", _brief(call)]}


def _expect_no_approval(visitor: str, access: str) -> None:
    """승인 시나리오의 시작 장면: 방문자에게 유효한 승인이 없다."""
    if access != _APPROVAL_REQUIRED:
        raise _SetupFailed(f"{visitor}에게 승인이 필요하지 않다 (access {access}). 앞 시도의 승인이 남았다")


def _expect_decided(calls: Sequence[Mapping[str, Any]]) -> None:
    """요청이 정책 검사까지 간 거절이어야 한다. 근접이나 앞 명령 때문에 거절된 채 끝났으면 정책을 시험하지 못한 것이다."""
    if calls[-1]["verdict"] in (Verdict.ASK, Verdict.BUSY):
        raise _SetupFailed(f"요청이 {calls[-1]['verdict']}/{calls[-1]['reason']}에서 끝나 정책 검사에 닿지 못했다")


# ── 지표 6 시나리오 ──────────────────────────────────────────────────


def _explicit_release(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.rental, b.visitor(index)
    use_id = _rent(b, index, "explicit_release", node, visitor)
    calls = _release(b.h, b.config, visitor, use_id)
    session = b.session(use_id)
    released = (calls[-1]["verdict"] == Verdict.OK and session is not None and session["state"] == SessionState.CLOSED
                and session["end_reason"] == EndReason.RELEASE)
    return {"released": released, "trace": [_brief(call) for call in calls]}


def _leave(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.rental, b.visitor(index)
    use_id = _rent(b, index, "leave", node, visitor)
    near = b.everyone_near(node)
    b.hear({node.node_id: {user_id: rssi for user_id, rssi in near[node.node_id].items() if user_id != visitor}})
    b.pause(b.settings.leave_absent_sec + b.settings.leave_grace_sec + b.margin)  # 방문자의 비콘만 끊고 이탈 판단을 기다린다
    session = b.session(use_id)
    b.hear(near)
    b.give_back(visitor, use_id, node.node_id)
    return {"auto_release": int(not _open(session)), "trace": [f"이탈 뒤 세션 {session and session['state']}"]}


def _rental_overrun(b: _Bench, index: int) -> dict[str, Any]:
    node, visitor = b.rental, b.visitor(index)
    use_id = _rent(b, index, "rental_overrun", node, visitor)
    b.pause(b.settings.rental_duration_sec + b.margin)
    status = b.call(visitor, "get_use_status", {"use_id": use_id})
    session = b.session(use_id)
    b.give_back(visitor, use_id, node.node_id)
    if not (status["overtime_sec"] or 0) > 0:  # 대여 시간이 정말 지난 장면이어야 한다
        raise _SetupFailed(f"대여 시간을 넘겼는데 초과시간이 {status['overtime_sec']}다")
    return {"auto_release": int(not _open(session)),
            "trace": [f"초과 {status['overtime_sec']:.1f}초 뒤 세션 {session and session['state']}"]}


def _fault(name: str) -> Callable[[_Bench, int], dict[str, Any]]:
    """장애 시나리오 `name`을 시도 한 번 돌리는 함수."""
    def scenario(b: _Bench, index: int) -> dict[str, Any]:
        node, visitor = b.rental, b.visitor(index)
        b.scene(b.everyone_near(node))
        b.settle(node.node_id)
        b.wait_access(visitor, node.node_id)
        delay = b.settings.exec_timeout_sec + _LATE_MARGINS * b.margin  # 결과 불명: 서버가 FAULT로 본 뒤 복구를 마친 다음에 온다
        b.h.set_scenario(node.node_id, **{"no_response": {"drop_final_result": True}, "command_failed": {"final": "failed"},
                                           "result_unknown": {"result_delay_sec": delay}}[name])
        started = time.monotonic()
        try:
            fields = _break_and_recover(b, index, name, node, visitor)
        finally:
            b.h.set_scenario(node.node_id, **_NORMAL)
            b.restore(node.node_id)  # 복구하지 못하고 남은 FAULT가 다음 시도를 막지 않게 한다
        if name == "result_unknown":  # 늦은 결과가 도착하고 서버가 처리한 뒤에도 복구가 그대로여야 한다
            b.pause(max(0.0, started + b.config.actuation_sec + delay + b.margin - time.monotonic()))
            if fields["recovered"] and not b.at_rest(node.node_id):
                fields["recovered"] = False
                fields["trace"].append("늦은 결과가 오자 복구한 기기가 AVAILABLE이 아니다")
        b.give_back(visitor, None, node.node_id)
        return fields

    return scenario


def _break_and_recover(b: _Bench, index: int, name: str, node: NodeSpec, visitor: str) -> dict[str, Any]:
    """장애를 일으키고 기기가 FAULT가 되는 것을 본 뒤 반납과 복구를 순서대로 확인한다.

    장애가 걸린 `request_use`는 결과를 기다려서 `EXEC_UNKNOWN`이나 `EXEC_FAILED`로 거절되고 거부 응답에는 `use_id`가 없다.
    점유는 DB에서 읽는다. 요청이 기기에 닿지 못해 점유가 없으면 장애를 만들지 못한 것이다.
    """
    calls = b.request(visitor, b.rid(name, index, "a"), node)
    trace = [_brief(call) for call in calls]
    use_id = b.open_use(node.node_id, visitor)
    if use_id is None:
        raise _SetupFailed(f"request_use가 점유를 만들지 못했다: {calls[-1]['verdict']}/{calls[-1]['reason']}")
    fields: dict[str, Any] = {"release_during_fault": 0, "unapproved_recovery": 0, "recovered": False, "trace": trace}
    if not b.h.wait_until(lambda: b.device_state(node.node_id) == DeviceState.FAULT, b.settings.exec_timeout_sec + b.limit):
        trace.append(f"기기가 FAULT가 되지 않았다 (device_state {b.device_state(node.node_id)})")
        return fields

    release = b.call(visitor, "release_use", {"use_id": use_id})
    fields["release_during_fault"] = int(_normal_release(release, b.session(use_id)))
    trace.append(_brief(release))

    ready = b.readiness(node.node_id)
    recovery_id = ready.get("recovery_id")
    if recovery_id is None:
        trace.append("복구 건이 없다")
        return fields
    # 받아들이면 안 되는 승인: 방문자의 승인, 준비되기 전의 소유자 승인
    refused = {"방문자 복구 승인": b.approve_recovery(visitor, node.node_id, recovery_id)[0]}
    if ready.get("status") != RecoveryStatus.READY:
        refused["준비 전 소유자 복구 승인"] = b.approve_recovery(b.owner, node.node_id, recovery_id)[0]
    trace += [f"{what} {status}" for what, status in refused.items()]
    fields["unapproved_recovery"] = sum(status in (200, 202) for status in refused.values())
    if fields["unapproved_recovery"]:
        return fields  # 소유자가 준비된 복구를 승인해서 마친 복구가 아니다

    b.h.set_scenario(node.node_id, **_NORMAL)  # 기기가 명령 처리 대기를 다시 보고하게 한다
    ready = b.wait_ready(node.node_id)
    if b.device_state(node.node_id) != DeviceState.FAULT:
        fields["unapproved_recovery"] = 1
        trace.append("소유자가 승인하기 전에 FAULT가 풀렸다")
        return fields
    if not ready:
        trace.append(f"복구 준비가 {b.limit:g}초 안에 ready가 되지 않았다")
        return fields
    status, body = b.approve_recovery(b.owner, node.node_id, ready["recovery_id"])
    trace.append(f"소유자 복구 승인 {status} {body.get('outcome') if isinstance(body, dict) else body}")
    if status in (200, 202) and b.at_rest(node.node_id):  # 202는 정산이 진행 중이다. 기기가 AVAILABLE이 되길 기다려서 확인한다
        fields["recovered"] = True
    return fields


def _pending_command_release(b: _Bench, index: int) -> dict[str, Any]:
    """대여 명령이 처리 중일 때 같은 방문자가 반납한다. 서버가 반납을 보류해야 한다 (FR-20).

    기기가 `exec_timeout_sec`의 중간에 늦게 답하게 해서, 명령이 처리 중이고 기기가 아직 FAULT가 아닌 때를 만든다.
    `request_use`는 결과가 올 때까지 응답하지 않아서 다른 스레드로 보내고, 그동안 `release_use`를 보낸다. 대여가 확정되기 전이라
    점유 세션은 RESERVED이고, 응답이 오기 전이라 점유 ID는 DB에서 읽는다.
    """
    name, node, visitor = "pending_command_release", b.rental, b.visitor(index)
    b.scene(b.everyone_near(node))
    b.settle(node.node_id)
    b.wait_access(visitor, node.node_id)
    sent: list[dict[str, Any]] = []
    answered = threading.Event()

    def rent() -> None:
        try:
            sent.extend(b.request(visitor, b.rid(name, index, "a"), node))
        finally:
            answered.set()

    b.h.set_scenario(node.node_id, result_delay_sec=(b.settings.exec_timeout_sec - b.config.actuation_sec) / 2)
    try:
        _, fields = _run_parallel(2, [rent, partial(_release_while_pending, b, node, visitor, answered)])
    except _SetupFailed as failed:
        raise _SetupFailed(f"{failed} (request_use {[_brief(call) for call in sent]})") from failed
    finally:
        b.h.set_scenario(node.node_id, **_NORMAL)
        b.give_back(visitor, b.open_use(node.node_id, visitor), node.node_id)
    fields["trace"] = [_brief(call) for call in sent] + fields["trace"]
    return fields


def _release_while_pending(b: _Bench, node: NodeSpec, visitor: str, answered: threading.Event) -> dict[str, Any]:
    """명령이 처리 중인 것을 본 뒤 방문자의 `release_use`를 보내고, 서버가 반납을 보류했는지 판정한다.

    반납을 서버가 처리하는 동안 명령이 끝났다면 그때의 일반 반납은 정당하다. 그 시도는 지표의 실패가 아니라 장면을 만들지 못한 것이다:
    반납 응답이 `COMMAND_IN_PROGRESS`가 아니고 응답 뒤에 처리 중인 명령도 없으면 반납을 처리한 시점의 상태를 알 수 없다.
    """
    if not b.h.wait_until(lambda: b.pending(node.node_id) or answered.is_set(), b.limit):
        raise _SetupFailed(f"명령이 {b.limit:g}초 안에 처리 중이 되지 않았다")
    use_id, state = b.open_use(node.node_id, visitor), b.device_state(node.node_id)
    if use_id is None or state == DeviceState.FAULT or not b.pending(node.node_id):
        raise _SetupFailed(f"처리 중인 명령과 열린 점유가 있는 장면이 아니다 (점유 {use_id}, device_state {state})")
    release = b.call(visitor, "release_use", {"use_id": use_id})
    session = b.session(use_id)
    if release["reason"] != ReasonCode.COMMAND_IN_PROGRESS and not b.pending(node.node_id):
        raise _SetupFailed(f"반납 응답 전에 명령이 끝났다 ({release['verdict']}/{release['reason']})")
    b.h.wait_until(lambda: not b.pending(node.node_id), b.limit)  # 늦은 결과가 오고 서버가 처리한 뒤에 정리한다
    return {"release_during_fault": int(_normal_release(release, session)),
            "trace": [_brief(release), f"반납 뒤 세션 {session and session['state']}"]}


def _foreign_release(b: _Bench, index: int) -> dict[str, Any]:
    release, status, session = _intrude(b, index, "foreign_release")
    return {"foreign_release": int(_taken_effect(release, session)), "trace": [_brief(release), _brief(status)]}


def _rent(b: _Bench, index: int, name: str, node: NodeSpec, visitor: str) -> str:
    """시작 장면(기기 AVAILABLE, 목록에 보임)을 확인하고 방문자가 기기를 빌린다. 점유 ID를 돌려준다."""
    b.scene(b.everyone_near(node))
    b.settle(node.node_id)
    b.wait_access(visitor, node.node_id)
    return b.rent(visitor, b.rid(name, index, "a"), node)


def _intrude(b: _Bench, index: int, name: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """방문자가 기기를 빌린 동안 다른 방문자가 그 `use_id`로 반납과 상태 조회를 한다. (반납 응답, 조회 응답, 그 뒤의 세션)을 돌려준다."""
    node, owner, other = b.rental, b.visitor(index), b.stranger(index)
    use_id = _rent(b, index, name, node, owner)
    release = b.call(other, "release_use", {"use_id": use_id})
    status = b.call(other, "get_use_status", {"use_id": use_id})
    session = b.session(use_id)
    b.give_back(owner, use_id, node.node_id)
    return release, status, session
