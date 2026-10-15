"""`tests/tools/` 공통 fixture (specs/core-proximity-data-collection/spec.md).

테스트 모듈은 이 파일을 import하지 않고 fixture로만 받는다. 이 디렉터리와 그 아래에 `__init__.py`를
두지 않는다. 두면 테스트 패키지 이름이 `tools`가 되어 도구 패키지(`server/tools/`)와 겹친다.
`fixtures/plans/`의 계획 파일은 테스트용 합성 값이다. 실제 장소의 계획 파일이 아니다.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nearpilot.shared.enums import AdminWriteOutcome, AdminWriteReason
from nearpilot.shared.models import AdminWriteResult, BeaconKey

# `pyproject.toml`의 pythonpath는 `src`뿐이다. `server/`를 직접 넣어야 `import tools.data_collection`이 된다.
_SERVER_DIR = str(Path(__file__).resolve().parents[2])
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
BEACON = BeaconKey("11111111-2222-3333-4444-555555555555", 1, 1)
SAMPLE_PLAN = Path(__file__).resolve().parent / "fixtures" / "plans" / "sample.json"


def _next_result(results: list[AdminWriteResult | BaseException]) -> AdminWriteResult:
    """맨 앞 결과를 꺼낸다. 예외면 던진다. 비었으면 APPLIED/OK다."""
    if not results:
        return AdminWriteResult(AdminWriteOutcome.APPLIED, AdminWriteReason.OK)
    result = results.pop(0)
    if isinstance(result, BaseException):
        raise result
    return result


class FakeAdminRepository:
    """`start_collection`과 `end_collection`만 가진 fake. 다른 메서드는 두지 않는다 —

    도구가 그 외의 메서드를 부르면 `AttributeError`로 테스트가 실패한다.
    호출은 결과와 상관없이 `calls`에 먼저 기록한다. 결과는 `start_results`, `end_results`의 맨 앞부터
    하나씩 꺼내 돌려주고 (예외면 던진다), 비었으면 APPLIED/OK를 돌려준다.
    `end_results`가 비었을 때 `ended_at`이 그 회차의 `started_at`보다 뒤가 아니면 실제 DB처럼
    `INVALID_INTERVAL`로 거부한다 (`AdminRepository.end_collection` docstring).
    """

    def __init__(self) -> None:
        # ("start", collection_id, beacon, label, started_at) 또는 ("end", collection_id, ended_at)
        self.calls: list[tuple] = []
        self.start_results: list[AdminWriteResult | BaseException] = []
        self.end_results: list[AdminWriteResult | BaseException] = []
        self._started: dict[str, datetime] = {}  # collection_id → started_at

    def start_collection(
        self, collection_id: str, beacon: BeaconKey, label: str, started_at: datetime
    ) -> AdminWriteResult:
        self.calls.append(("start", collection_id, beacon, label, started_at))
        self._started.setdefault(collection_id, started_at)
        return _next_result(self.start_results)

    def end_collection(self, collection_id: str, ended_at: datetime) -> AdminWriteResult:
        self.calls.append(("end", collection_id, ended_at))
        started_at = self._started.get(collection_id)
        if not self.end_results and started_at is not None and ended_at <= started_at:
            return AdminWriteResult(AdminWriteOutcome.REJECTED, AdminWriteReason.INVALID_INTERVAL)
        return _next_result(self.end_results)


class FakeIO:
    """시계, 대기, 입력, 출력을 흉내 낸다. 실제 시간을 기다리지 않는다.

    시각은 `sleep`으로만 간다. `interrupt_on_sleep`번째(0부터) `sleep`은 넘겨받은 초의 절반만 시각을 늘리고
    `KeyboardInterrupt`를 던진다 (대기 도중에 Ctrl+C를 누른 것과 같다). `inputs`의 예외는 `read_line`이 그 차례에 던지고, 입력이 다 떨어지면
    `EOFError`를 던진다.
    """

    def __init__(
        self,
        inputs: list[str | BaseException],
        *,
        start: datetime = T0,
        interrupt_on_sleep: int | None = None,
    ) -> None:
        self.inputs = list(inputs)  # 아직 읽지 않은 입력. 넘겨받은 목록은 건드리지 않는다
        self.interrupt_on_sleep = interrupt_on_sleep
        self.now = start
        self.sleeps: list[float] = []
        self.outputs: list[str] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, sec: float) -> None:
        index = len(self.sleeps)
        self.sleeps.append(sec)
        if index == self.interrupt_on_sleep:
            self.now += timedelta(seconds=sec / 2)
            raise KeyboardInterrupt
        self.now += timedelta(seconds=sec)

    def read_line(self) -> str:
        if not self.inputs:
            raise EOFError
        line = self.inputs.pop(0)
        if isinstance(line, BaseException):
            raise line
        return line

    def write(self, text: str) -> None:
        self.outputs.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.outputs)


@pytest.fixture
def t0() -> datetime:
    return T0


@pytest.fixture
def beacon() -> BeaconKey:
    return BEACON


@pytest.fixture
def fake_repo() -> FakeAdminRepository:
    return FakeAdminRepository()


@pytest.fixture
def make_io() -> type[FakeIO]:
    """`FakeIO`를 만드는 함수. 예: `make_io(["", ""], interrupt_on_sleep=1)`"""
    return FakeIO


@pytest.fixture
def sample_plan_path() -> Path:
    return SAMPLE_PLAN


@pytest.fixture
def plan_data() -> dict:
    """`sample.json`을 읽은 새 dict. 테스트마다 새 복사본이라 마음대로 고쳐도 된다."""
    return json.loads(SAMPLE_PLAN.read_text(encoding="utf-8"))


@pytest.fixture
def write_plan(tmp_path: Path):
    """`write_plan(data, name="plan.json")`: `tmp_path`에 계획 파일을 쓰고 경로를 돌려준다.

    `data`가 str이면 그대로 쓰고, dict면 JSON으로 쓴다.
    """

    def write(data: dict | str, name: str = "plan.json") -> Path:
        path = tmp_path / name
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return path

    return write
