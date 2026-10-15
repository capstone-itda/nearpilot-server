"""회차 실행과 기록 파일 (specs/core-proximity-data-collection/spec.md 6절 "도구").

계획 파일의 회차를 차례로 실행한다. 회차마다 `start_collection`과 `end_collection`을 한 번씩 부르고
기록 파일(JSONL)에 한 줄을 덧붙인다. 시계, 대기, 입력, 출력, ID 생성은 `SessionIO`로 주입받는다.
수집 회차는 `AdminRepository`로만 기록하고 SQL을 쓰지 않는다 (#131). 이 모듈은 CLI를 만들지 않는다.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from nearpilot.shared.enums import AdminWriteOutcome
from nearpilot.shared.interfaces import AdminRepository
from tools.data_collection.plan import CollectionPlan

EXIT_OK = 0  # 모든 회차를 마쳤거나 q로 끝냈다
EXIT_FAILED = 1  # DB가 거부했거나 예외를 던졌다
EXIT_INVALID = 2  # 시작 전 입력이 잘못됐다 (계획 파일, --start-at, 기록 파일)
EXIT_INTERRUPTED = 130  # Ctrl+C


class RunStatus(StrEnum):
    """기록 파일 한 줄의 `status`."""

    DONE = "done"  # 끝 시각을 기록했다
    ABORTED = "aborted"  # Ctrl+C로 중단했고 끝 시각은 기록했다
    END_FAILED = "end_failed"  # 끝 시각을 기록하지 못했다. DB에 진행 중인 회차가 남았을 수 있다


def new_collection_id() -> str:
    """`col-` 뒤에 `uuid4().hex`(32자)를 붙인 회차 ID. 수집 PC가 여럿이어도 겹치지 않는다."""
    return f"col-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class SessionIO:
    """세션이 바깥과 닿는 함수 다섯 개. 테스트는 가짜 함수로 바꿔서 실제 시간을 기다리지 않는다."""

    clock: Callable[[], datetime]  # UTC 기준 시각이 붙은 현재 시각
    sleep: Callable[[float], None]
    read_line: Callable[[], str]  # 한 줄 입력. EOF면 EOFError
    write: Callable[[str], None]  # 화면에 한 줄 쓴다
    new_id: Callable[[], str] = new_collection_id


def run_session(
    plan: CollectionPlan,
    repository: AdminRepository,
    record_path: Path,
    io: SessionIO,
    *,
    start_at: int = 1,
) -> int:
    """계획의 회차를 `start_at`번(1부터)부터 차례로 실행하고 종료 코드를 돌려준다.

    시작 전에 `start_at`과 기록 파일을 검사한다. 어기면 DB를 부르지 않고 `EXIT_INVALID`다.
    DB가 거부하거나 예외를 던지면 남은 회차를 실행하지 않는다.
    """
    total = len(plan.runs)
    if not 1 <= start_at <= total:
        io.write(f"--start-at은 1 이상 {total} 이하여야 한다 ({start_at})")
        return EXIT_INVALID
    try:
        # 회차를 만들기 전에 연다. 열지 못하면 DB에 회차가 생기기 전에 멈춘다. 줄바꿈은 운영체제와 상관없이 "\n"이다
        record = record_path.open("a", encoding="utf-8", newline="\n")
    except OSError as error:
        io.write(f"기록 파일을 열 수 없다: {record_path} ({error.strerror or error})")
        return EXIT_INVALID

    with record:
        for index in range(start_at, total + 1):
            code = _await_start(index, plan, io)
            if code is None:
                code = _collect(index, plan, repository, record, io)
            if code is not None:
                return code
    io.write("모든 회차를 마쳤다.")
    return EXIT_OK


def _await_start(index: int, plan: CollectionPlan, io: SessionIO) -> int | None:
    """회차 안내를 쓰고 Enter와 자리 잡는 시간을 기다린다. 회차를 만들기 전에 끝내야 하면 종료 코드를 돌려준다."""
    run = plan.runs[index - 1]
    io.write(f"[회차 {index}/{len(plan.runs)}] 라벨: {run.label}")
    io.write(f"서는 위치: {run.spot}")
    io.write("자리를 잡았으면 Enter를 누른다. 끝내려면 q를 입력하고 Enter를 누른다.")
    try:
        line = io.read_line()
    except KeyboardInterrupt:
        io.write("Ctrl+C로 멈췄다. 이 회차는 만들지 않았다.")
        return EXIT_INTERRUPTED
    except EOFError:
        line = "q"  # 입력이 끝나면 q와 같다
    if line.strip() == "q":
        io.write("수집을 끝낸다. 이 회차는 만들지 않았다.")
        return EXIT_OK

    io.write(f"{plan.settle_sec:g}초 뒤에 시작한다. 자리를 잡는다.")
    try:
        io.sleep(plan.settle_sec)
    except KeyboardInterrupt:
        io.write("Ctrl+C로 멈췄다. 이 회차는 만들지 않았다.")
        return EXIT_INTERRUPTED
    return None


def _reason(error: BaseException) -> str:
    """화면에 쓸 예외 사유. Ctrl+C는 따로 알린다."""
    if isinstance(error, KeyboardInterrupt):
        return "Ctrl+C를 눌렀다"
    return f"예외가 났다 ({error!r})"


def _collect(
    index: int, plan: CollectionPlan, repository: AdminRepository, record: TextIO, io: SessionIO
) -> int | None:
    """회차를 만들고 `run_sec`만큼 지난 뒤 끝 시각을 기록하고, 기록 파일에 한 줄을 덧붙인다.

    세션을 끝내야 하면 종료 코드를, 다음 회차로 가도 되면 `None`을 돌려준다.
    """
    run = plan.runs[index - 1]
    cid = io.new_id()
    started_at = io.clock()
    try:
        result = repository.start_collection(cid, plan.beacon, run.label, started_at)
    except (Exception, KeyboardInterrupt) as error:
        # 회차가 만들어졌는지 알 수 없다. 만들어졌다면 사람이 close로 닫도록 ID를 남기고 기록 파일에는 쓰지 않는다
        io.write(f"회차 {index} 시작 중 {_reason(error)}.")
        io.write(f"DB에 회차가 생겼는지 알 수 없다. 회차 ID: {cid}")
        io.write(f"생겼다면 닫는다: close --collection-id {cid}")
        return EXIT_INTERRUPTED if isinstance(error, KeyboardInterrupt) else EXIT_FAILED
    if result.outcome != AdminWriteOutcome.APPLIED:
        io.write(f"DB가 회차 {index} 시작을 거부했다 ({result.reason}). 남은 회차를 실행하지 않는다.")
        return EXIT_FAILED
    io.write(f"회차 {index} 시작 ({cid}). {plan.run_sec:g}초 동안 그 자리에 선다.")

    interrupted = False
    try:
        io.sleep(plan.run_sec)
    except KeyboardInterrupt:
        interrupted = True  # 그 시각으로 회차를 닫는다

    ended_at = io.clock()
    end_failure = None  # 끝 시각을 기록하지 못한 사유. 기록했으면 None
    try:
        result = repository.end_collection(cid, ended_at)
    except (Exception, KeyboardInterrupt) as error:
        interrupted = interrupted or isinstance(error, KeyboardInterrupt)
        end_failure = _reason(error)
    else:
        if result.outcome != AdminWriteOutcome.APPLIED:
            end_failure = f"DB가 거부했다 ({result.reason})"

    if end_failure is not None:
        status = RunStatus.END_FAILED
    elif interrupted:
        status = RunStatus.ABORTED
    else:
        status = RunStatus.DONE

    if status is RunStatus.END_FAILED:
        # 기록 파일에 쓰다 실패해도 사람이 회차를 닫을 수 있게 쓰기 전에 알린다
        io.write(f"회차 {index}의 끝 시각을 기록하지 못했다: {end_failure}. 회차 ID: {cid}")
        io.write(f"DB에 진행 중인 회차가 남았다. 닫는다: close --collection-id {cid}")
    entry = {
        "index": index,  # 계획 파일 기준 번호. --start-at으로 이어도 같다
        "collection_id": cid,
        "place_id": plan.place_id,
        "beacon": asdict(plan.beacon),
        "label": run.label,
        "spot": run.spot,
        "started_at": started_at.isoformat(),
        "ended_at": None if status is RunStatus.END_FAILED else ended_at.isoformat(),
        "status": status.value,
    }
    try:
        record.write(json.dumps(entry, ensure_ascii=False) + "\n")
        record.flush()
    except OSError as error:
        io.write(f"기록 파일에 쓰지 못했다. 회차 ID: {cid} ({error.strerror or error})")
        return EXIT_FAILED

    if status is RunStatus.END_FAILED:
        return EXIT_INTERRUPTED if interrupted else EXIT_FAILED
    if status is RunStatus.ABORTED:
        io.write(f"회차 {index}: Ctrl+C로 중단했다. 끝 시각을 기록했다. 남은 회차를 실행하지 않는다.")
        return EXIT_INTERRUPTED
    io.write(f"회차 {index} 끝 ({cid}).")
    return None
