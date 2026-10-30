"""지표 3(점유 원자성·멱등성)과 지표 7(서버 판정 지연)의 부하 생성기와 집계 (specs/db-evaluation-server-metrics/spec.md 6절 "결정").

`run_metric3`은 동시 클라이언트 요청과 같은 `request_id`의 재전송을 보내고, 이중 점유와 같은 요청의 중복 실행을
응답이 아니라 저장된 결과로 센다 (NFR-03). `run_metric7`은 한 방문자가 `request_use` → 반납을 순서대로 반복하며
`request_use` 왕복 시간을 모은다 (NFR-01). 서버는 고치지 않는다. MCP와 읽기 전용 DB 연결, 시뮬레이터의 명령 기록만 쓴다.

이중 점유는 같은 기기의 `use_session` 구간이 겹치는 것이다. 구간의 시작은 `use_session.reserved_at`이 아니라 예약을
확정한 시각이다. `reserved_at`은 요청을 받은 시각이라서, 이전 세션이 닫히기 전에 도착해 닫힌 뒤에 예약된 정상 요청도
이전 세션과 겹쳐 보인다. 예약을 확정한 시각은 판정 단계 5의 감사 기록(`audit_log`, outcome `reserved`)이 예약 저장
직후에 남기는 `created_at`이고, 이전 세션의 `closed_at`은 닫는 저장보다 먼저 읽은 시각이라서 정상 순서에서는 겹치지
않는다. 명령을 보내기 전에 취소된 세션도 예약을 했으므로 구간이 있다. 감사 기록이 없는 세션은 `reserved_at`을 쓴다.

세션과 명령은 `request_id`가 지표 3의 접두어(`m3-`)로 시작하는 요청의 것만 센다. 같은 서버에서 다른 지표를 먼저 돌려도
그 지표의 세션과 명령이 섞이지 않고, 겹침도 지표 3의 세션끼리만 본다.
"""

from __future__ import annotations

import random
import statistics
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from typing import Any

from nearpilot.shared.enums import Capability, ReasonCode, ReserveOutcome, Role, Step, Verdict
from tools.evaluation.harness import Harness, HarnessError
from tools.evaluation.rows import MetricOutcome, metric_row
from tools.evaluation.server_config import EvalConfig, EvalConfigError
from tools.node_sim.store import CommandRecord

_REQUEST = {"target_spec": "nearest:locker", "action": "open"}  # 지표 3과 7이 보내는 요청. request_id만 요청마다 다르다
_UNMEASURED = "미측정: LLM 호스트 실행 필요"
_PREFIX = "m3-"  # 지표 3이 보내는 request_id의 접두어. 재전송은 "m3-rt-"로 시작한다
# 요청 ID가 지표 3의 접두어로 시작하는 요청의 세션마다 예약을 확정한 시각(단계 5의 감사 기록)과 닫힌 시각.
# 인자는 단계, 결과 값, 접두어의 길이와 접두어다.
_SESSIONS = (
    "SELECT s.use_id, s.node_id, COALESCE(a.created_at, s.reserved_at) AS started_at, s.closed_at "
    "FROM use_session s JOIN request r ON r.internal_request_id = s.internal_request_id "
    "LEFT JOIN audit_log a ON a.internal_request_id = s.internal_request_id "
    "AND a.step = ? AND json_extract(a.payload_json, '$.outcome') = ? "
    "WHERE substr(r.request_id, 1, ?) = ?"
)
# 요청 ID가 `?`로 시작하는 요청의 명령. 서버가 스스로 낸 명령은 요청 ID가 없어서 빠진다.
_COMMANDS = (
    "SELECT c.command_id, c.internal_request_id, c.node_id FROM node_command c "
    "JOIN request r ON r.internal_request_id = c.internal_request_id WHERE substr(r.request_id, 1, ?) = ?"
)


def run_metric3(h: Harness, config: EvalConfig, rng: random.Random) -> MetricOutcome:
    """동시 요청과 재전송을 보내고 이중 점유와 중복 실행을 센다 (지표 3).

    모든 방문자가 trusted 사물함마다 near RSSI로 들려서 같은 사물함을 두고 겨룬다. 클라이언트(`m3_clients`개의
    스레드)가 합쳐서 `m3_requests`번 새 `request_id`로 보내고, 이어서 `m3_retransmits`번 같은 사용자·`request_id`·인자의
    요청을 둘씩 동시에 보낸다. 사용자는 `rng`로 고른다. 클라이언트가 사용자보다 많아서 같은 사용자 토큰을 여러
    클라이언트가 쓴다. `OK`를 받은 클라이언트는 바로 반납한다. 재전송 짝은 차례로 보내고 짝마다 기기가 비기를
    기다린다. 짝끼리 겨루면 거의 모든 짝이 기기에 닿기 전에 거절되어 같은 요청의 중복 실행을 볼 수 없다.
    """
    visitors, lockers = _visitors(config), _trusted_lockers(config)
    clients = config.counts["m3_clients"]
    near = config.rssi["near"]
    _hear(h, config, {node_id: {user_id: near for user_id in visitors} for node_id in lockers})
    _wait_ready(h, config, visitors)

    requests = [(rng.choice(visitors), f"{_PREFIX}{index}") for index in range(config.counts["m3_requests"])]
    retransmits = [(rng.choice(visitors), f"{_PREFIX}rt-{index}") for index in range(config.counts["m3_retransmits"])]
    responses = _flatten(_run_parallel(clients, [partial(_exchange, h, config, user_id, request_id)
                                                 for user_id, request_id in requests]))
    for user_id, request_id in retransmits:
        _wait_ready(h, config, [user_id])
        barrier = threading.Barrier(2)  # 짝이 모이면 같은 순간에 보낸다
        responses += _flatten(_run_parallel(2, [partial(_exchange, h, config, user_id, request_id, barrier)] * 2))

    sessions = h.query(_SESSIONS, Step.DEVICE_OCCUPANCY.value, ReserveOutcome.RESERVED.value, len(_PREFIX), _PREFIX)
    overlaps = overlapping_sessions(sessions)
    executed = _executed_commands(h)
    tally = _tally(responses)
    return MetricOutcome(
        rows=_metric3_rows(config, overlaps, executed, tally, len(sessions)),
        raw={"clients": clients, "lockers": lockers, "responses": responses, "tally": tally,
             "sessions": len(sessions), "overlaps": [list(overlap) for overlap in overlaps],
             "executed": executed, "duplicates": duplicate_executions(executed)},
    )


def run_metric7(h: Harness, config: EvalConfig) -> MetricOutcome:
    """한 방문자가 기기 앞에서 `request_use` → 반납을 순서대로 반복하며 `request_use` 왕복 시간을 모은다 (지표 7).

    방문자는 첫 trusted 사물함 앞에 선다. 그 사물함만 near RSSI로 듣고 다른 기기는 아무도 듣지 못한다 (값이 없는
    앵커는 근접 확률의 증거로 쓰이지 않는다). 판정마다 목록에서 기기를 발견하고 저장 관측의 근접 확률이 임계값 이상인지
    별도로 확인한다. 반납 직후에 보내면 근접 확률이 임계값 아래라서 `LOW_CONFIDENCE`로 거절되는 일이 잦고(기다리지 않은 30회
    중 18회), 거절된 판정은 기기를 구동하지 않아서 지연이 짧게 나온다. 지연은 `Harness.mcp`가
    `time.perf_counter()`로 잰 `request_use` 왕복 시간이고 반납과 기다린 시간은 넣지 않는다.
    호스트 쪽 두 지표(발화→안내 지연, 3종 호스트 일치)는 LLM 호스트를 돌려야 해서 행만 두고 미측정으로 쓴다.
    """
    visitor, target = _visitors(config)[0], _trusted_lockers(config)[0]
    _hear(h, config, {target: {visitor: config.rssi["near"]}})

    responses: list[dict[str, Any]] = []
    for index in range(config.counts["m7_server_requests"]):
        _wait_ready(h, config, [visitor])
        responses += _exchange(h, config, visitor, f"m7-{index}")
    decisions = [response for response in responses if response["tool"] == "request_use"]
    latencies = [response["elapsed_sec"] for response in decisions]
    granted = sum(response["verdict"] == Verdict.OK for response in decisions)
    return MetricOutcome(
        rows=_metric7_rows(config, latencies, granted),
        raw={"visitor": visitor, "node_id": target, "latencies": latencies, "responses": responses},
    )


def overlapping_sessions(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """같은 기기에서 구간이 겹치는 세션 쌍 (node_id, 먼저 시작한 use_id, 나중에 시작한 use_id).

    `rows`의 각 행은 `use_id`, `node_id`, `started_at`, `closed_at`이다. 시각은 ISO 문자열이나 `datetime`이고,
    `closed_at`이 None이면 아직 열린 세션이다. 구간은 시작 이상 끝 미만이라서 한 세션의 끝과 다음 세션의 시작이
    맞닿는 것은 겹침이 아니다. 열린 세션은 그 뒤에 시작한 세션과 모두 겹친다.
    """
    by_node: dict[str, list[tuple[datetime, datetime | None, str]]] = {}
    for row in rows:
        end = row["closed_at"]
        by_node.setdefault(row["node_id"], []).append(
            (_instant(row["started_at"]), None if end is None else _instant(end), row["use_id"]))
    found: list[tuple[str, str, str]] = []
    for node_id, sessions in sorted(by_node.items()):
        sessions.sort(key=lambda session: (session[0], session[2]))
        for index, (_, end, use_id) in enumerate(sessions):
            for later_start, _, later_id in sessions[index + 1:]:
                if end is not None and later_start >= end:
                    break  # 뒤 세션은 시작이 더 늦어서 겹치지 않는다
                if later_id != use_id:
                    found.append((node_id, use_id, later_id))
    return found


def duplicate_executions(executed: Mapping[str, int]) -> list[str]:
    """시뮬레이터가 둘 이상 구동한 원래 요청의 `internal_request_id`. `executed`는 요청 ID → 구동 수다."""
    return sorted(request_id for request_id, count in executed.items() if count >= 2)


def latency_summary(seconds: Sequence[float]) -> tuple[float, float]:
    """(median, p95). p95는 포함(inclusive) 방식의 백분위수다. 표본이 하나면 둘 다 그 값이다."""
    if len(seconds) == 1:
        return seconds[0], seconds[0]
    return statistics.median(seconds), statistics.quantiles(seconds, n=100, method="inclusive")[94]


# ── 클라이언트 ───────────────────────────────────────────────────────


def _exchange(h: Harness, config: EvalConfig, user_id: str, request_id: str,
              barrier: threading.Barrier | None = None) -> list[dict[str, Any]]:
    """클라이언트 하나가 요청을 한 번 보내고 `OK`면 반납한다. 보낸 호출의 응답을 모두 돌려준다.

    `barrier`가 있으면 재전송의 짝이 모일 때까지 기다렸다가 같은 순간에 보낸다.
    """
    if barrier is not None:
        barrier.wait(config.startup_timeout_sec)
    calls = [_call(h, user_id, "request_use", {**_REQUEST, "request_id": request_id})]
    if calls[0]["verdict"] == Verdict.OK and calls[0]["use_id"]:
        calls += _release(h, config, user_id, calls[0]["use_id"])
    return calls


def _release(h: Harness, config: EvalConfig, user_id: str, use_id: str) -> list[dict[str, Any]]:
    """`release_use`가 끝날 때까지 다시 부른다 (제한 시간은 `startup_timeout_sec`). 부른 호출의 응답을 모두 돌려준다.

    같은 사용자의 다른 클라이언트가 같은 점유로 재조작하는 중이면 반납이 `BUSY`로 밀리고, 저장 오류는
    `INTERNAL_ERROR`나 HTTP 오류로 온다. 이 셋만 다시 부르고, `OK`나 다른 거절은 다시 불러도 같아서 끝낸다.
    반납하지 못하면 이후의 요청이 모두 막혀서 부하가 의미를 잃는다.
    """
    calls: list[dict[str, Any]] = []

    def finished() -> bool:
        calls.append(_call(h, user_id, "release_use", {"use_id": use_id}))
        last = calls[-1]
        return not (last["verdict"] in (None, Verdict.BUSY) or last["reason"] == ReasonCode.INTERNAL_ERROR)

    h.wait_until(finished, config.startup_timeout_sec)
    return calls


def _call(h: Harness, user_id: str, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """MCP 도구를 한 번 부르고 응답을 원자료 한 건으로 줄인다. HTTP 오류나 읽을 수 없는 도구 결과는 verdict가 None이다."""
    status, result, elapsed = h.mcp(user_id, tool, arguments)
    result = result or {}
    return {
        "tool": tool, "user_id": user_id, "request_id": arguments.get("request_id"),
        "use_id": result.get("use_id") or arguments.get("use_id"), "status": status,
        "verdict": result.get("verdict"), "reason": result.get("reason"), "node_id": result.get("node_id"),
        "elapsed_sec": elapsed,
    }


def _run_parallel(workers: int, jobs: Sequence[Callable[[], Any]]) -> list[Any]:
    """`jobs`를 스레드 `workers`개로 돌려 결과를 `jobs` 순서대로 돌려준다. 하나가 예외를 내면 시작하지 않은 일은 취소한다."""
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval-client")
    try:
        futures = [pool.submit(job) for job in jobs]
        return [future.result() for future in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _flatten(groups: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [item for group in groups for item in group]


# ── 장면 ─────────────────────────────────────────────────────────────


def _visitors(config: EvalConfig) -> list[str]:
    visitors = [user.user_id for user in config.users if user.role == Role.VISITOR]
    if not visitors:
        raise EvalConfigError("users: 방문자(role = visitor)가 한 명 이상 있어야 한다 (지표 3과 7이 요청을 보낸다)")
    return visitors


def _trusted_lockers(config: EvalConfig) -> list[str]:
    lockers = [node.node_id for node in config.nodes if node.capability == Capability.LOCKER and node.trusted]
    if not lockers:
        raise EvalConfigError("nodes: trusted 사물함이 한 대 이상 있어야 한다 (지표 3과 7이 사물함을 요청한다)")
    return lockers


def _hear(h: Harness, config: EvalConfig, heard: Mapping[str, Mapping[str, float]]) -> None:
    """기기마다 듣는 사용자와 RSSI를 정한다. `heard`에 없는 기기는 아무도 듣지 못한다."""
    for node in config.nodes:
        h.set_rssi(node.node_id, heard.get(node.node_id, {}))


def _wait_ready(h: Harness, config: EvalConfig, user_ids: Sequence[str]) -> None:
    """모든 방문자의 목록에 사물함이 있고, 그 사물함의 재현 확률이 임계값 이상일 때까지 기다린다.

    새 RSSI가 근접 확률에 반영되기 전에, 또는 앞 사용이 끝나 기기가 비기 전에 요청을 보내면 판정이 거절된다.
    제한 시간(`startup_timeout_sec`) 안에 나오지 않으면 장면이 만들어지지 않은 것이라 `HarnessError`다.
    """
    lockers = set(_trusted_lockers(config))

    def ready() -> bool:
        for user_id in user_ids:
            _, found, _ = h.mcp(user_id, "find_available_devices", {})
            if not any(h.proximity_ready(user_id, node["node_id"])
                       for node in (found or {}).get("devices", [])
                       if node["node_id"] in lockers):
                return False
        return True

    if not h.wait_until(ready, config.startup_timeout_sec):
        raise HarnessError(f"근접 확률이 임계값을 넘는 기기가 {config.startup_timeout_sec:g}초 안에 나타나지 않았다 "
                           f"(방문자 {list(user_ids)})")


# ── 집계 ─────────────────────────────────────────────────────────────


def _instant(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _actuated(record: CommandRecord) -> bool:
    """시뮬레이터가 제어 신호 출력을 시작한 명령. 접수만 한 것, 거부, 시작 전에 실패한 것(`error_code`가 있는 failed)은 아니다."""
    return record.state in ("running", "succeeded", "unknown") or (record.state == "failed" and record.error_code is None)


def _executed_commands(h: Harness) -> dict[str, int]:
    """지표 3의 원래 요청(`internal_request_id`)마다 시뮬레이터가 구동한 명령 수. 구동한 명령이 없는 요청은 넣지 않는다."""
    executed: Counter[str] = Counter()
    for row in h.query(_COMMANDS, len(_PREFIX), _PREFIX):
        record = h.nodes[row["node_id"]].store.command(row["command_id"])
        if record is not None and _actuated(record):
            executed[row["internal_request_id"]] += 1
    return dict(executed)


def _tally(responses: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """응답을 도구마다 "verdict/reason"별로 센다. `INTERNAL_ERROR`와 HTTP 오류는 따로 센다.

    HTTP 오류는 상태가 200이 아니거나 도구 결과를 읽을 수 없는 응답이다. 둘 다 실패로 세지 않는다.
    """
    decisions: dict[str, Counter[str]] = {}
    internal_errors = http_errors = 0
    for response in responses:
        if response["status"] != 200 or response["verdict"] is None:
            http_errors += 1
            kind = f"HTTP {response['status']}"
        else:
            internal_errors += response["reason"] == ReasonCode.INTERNAL_ERROR
            kind = f"{response['verdict']}/{response['reason']}"
        decisions.setdefault(response["tool"], Counter())[kind] += 1
    return {"decisions": {tool: dict(sorted(counts.items())) for tool, counts in sorted(decisions.items())},
            "internal_errors": internal_errors, "http_errors": http_errors}


def _metric3_rows(config: EvalConfig, overlaps: Sequence[tuple[str, str, str]], executed: Mapping[str, int],
                  tally: Mapping[str, Any], sessions: int) -> list[dict[str, Any]]:
    """지표 3의 행 셋. 통과 기준은 설정에서 읽는다.

    시험 횟수는 보낸 요청 수가 아니라 실제로 겨룬 횟수다: 이중 점유는 예약까지 닿은 요청(만든 세션과 `DEVICE_BUSY`),
    중복 실행은 기기를 구동한 요청이다. 요청 대부분이 예약 앞에서 거절되므로 보낸 수로 신뢰구간을 내면 실제보다 좋게 보인다.
    """
    counts, limits = config.counts, config.acceptance
    scene = f"클라이언트 {counts['m3_clients']}개 (D8: 클라이언트 수는 측정 뒤 정한다)"
    most = max(executed.values(), default=0)
    # 요청 대부분이 예약 앞에서 거절되므로, 실제로 기기를 겨룬 요청이 얼마나 되는지 함께 적는다
    busy = tally["decisions"].get("request_use", {}).get(f"{Verdict.BUSY}/{ReasonCode.DEVICE_BUSY}", 0)
    contended = f"기기를 구동한 요청 {len(executed)}건, 예약에서 막힌 요청(DEVICE_BUSY) {busy}건"
    return [
        metric_row("m3_double_occupancy", limit=limits["m3_max_double_occupancy"],
                   passed=len(overlaps) <= limits["m3_max_double_occupancy"], trials=sessions + busy,
                   failures=len(overlaps), note=f"{scene}. {contended}"),
        metric_row("m3_duplicate_execution", value=most, limit=limits["m3_max_duplicate_execution"],
                   passed=most <= limits["m3_max_duplicate_execution"],
                   trials=len(executed),
                   failures=len(duplicate_executions(executed)), note=scene),
        metric_row("m3_lock_errors", value=tally["internal_errors"] + tally["http_errors"],
                   note=f"INTERNAL_ERROR {tally['internal_errors']}건과 HTTP 오류 {tally['http_errors']}건을 실패로 세지 않고 "
                        f"따로 센다. 이중 점유와 중복 실행은 저장된 결과로 판정한다. {scene}"),
    ]


def _metric7_rows(config: EvalConfig, latencies: Sequence[float], granted: int) -> list[dict[str, Any]]:
    """지표 7의 행 넷. 서버 판정 지연의 통과 기준은 설정에서 읽고, 호스트 쪽 둘은 미측정이다."""
    median, p95 = latency_summary(latencies)
    limits = config.acceptance
    note = f"localhost MCP 왕복, 기기 구동 시간 포함 (OK {granted}/{len(latencies)})"
    return [
        metric_row("m7_server_latency_median", value=median, limit=limits["m7_max_median_sec"],
                   passed=median <= limits["m7_max_median_sec"], trials=len(latencies), note=note),
        metric_row("m7_server_latency_p95", value=p95, limit=limits["m7_max_p95_sec"],
                   passed=p95 <= limits["m7_max_p95_sec"], trials=len(latencies), note=note),
        metric_row("m7_host_latency", note=_UNMEASURED),
        metric_row("m7_host_agreement", note=_UNMEASURED),
    ]
