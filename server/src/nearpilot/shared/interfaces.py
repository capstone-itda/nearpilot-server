"""계층 간 인터페이스 (architecture.md §4).

각 계층은 상대 계층의 구현이 아니라 이 Protocol 에만 의존한다 (의존 규칙 4).
그래서 상대가 아직 없어도 가짜 구현(fake)으로 개발·테스트할 수 있다.

모든 함수는 동기 함수다. SQLite 와 paho-mqtt(스레드)가 동기 방식이고, FastAPI 는 `def`
엔드포인트를 스레드풀에서 실행하므로 이벤트 루프를 막지 않는다. 동시 요청의 이중 점유는
`Repository.reserve_if_free()` 의 `BEGIN IMMEDIATE` 트랜잭션이 막는다.
예약 이후의 상태 변경은 `ExecutionStore` 의 사건별 함수 하나가 트랜잭션 하나다 (#17).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from nearpilot.shared.enums import (
    Action,
    Capability,
    CommandOutcome,
    EndReason,
    ExecutionStatus,
    ReasonCode,
)
from nearpilot.shared.models import (
    Account,
    Approval,
    AuditEvent,
    BeaconKey,
    CommandRecord,
    CommandResult,
    Decision,
    ExecutionContext,
    ExecutionResult,
    NodeAnnounce,
    NodeCommand,
    NodeInfo,
    NodeStatus,
    PolicyRevision,
    ProximityResult,
    RequestLink,
    ReserveRequest,
    ReserveResult,
    RssiFrame,
    StoreResult,
    UseRequest,
    UseSession,
)

# ── DB 제공 ─────────────────────────────────────────────────────────


@runtime_checkable
class AccountLookup(Protocol):
    """API 가 DB 를 직접 부르는 유일한 경로 (의존 규칙 1)."""

    def account_by_token_hash(self, token_hash: str) -> Account | None: ...


@runtime_checkable
class Repository(Protocol):
    """DB → CORE 저장소. 쓰기는 이 인터페이스 한 경로로만 한다 (의존 규칙 5)."""

    # ⓪ 중복
    def link_request(self, req: UseRequest, input_digest: str) -> RequestLink: ...

    def save_decision(
        self, decision: Decision, context: Mapping[str, Any], settings_version: str
    ) -> None: ...

    # ① 비콘
    def beacons_of(self, user_id: str) -> Sequence[BeaconKey]: ...

    def save_rssi(self, frame: RssiFrame) -> None: ...

    def rssi_since(self, beacons: Sequence[BeaconKey], since: datetime) -> Sequence[RssiFrame]: ...

    # ② 후보
    def nodes(self, capability: Capability | None = None) -> Sequence[NodeInfo]: ...

    def upsert_announced_node(self, announce: NodeAnnounce) -> None: ...

    def touch_node(self, node_id: str, seen_at: datetime) -> None: ...

    # ④ 정책
    def current_policy(self, node_id: str) -> PolicyRevision: ...

    def valid_approval(
        self, user_id: str, node_id: str, action: Action, policy_version: int, now: datetime
    ) -> Approval | None: ...

    # ⑤⑥ — BEGIN IMMEDIATE 한 트랜잭션 (FR-19)
    def reserve_if_free(self, req: ReserveRequest) -> ReserveResult: ...

    # 조회 — 예약 이후의 쓰기는 ExecutionStore 로만 한다
    def session(self, use_id: str) -> UseSession | None: ...

    def session_owner_matches(self, use_id: str, user_id: str) -> bool: ...  # FR-21


@runtime_checkable
class ExecutionStore(Protocol):
    """DB → CORE 예약 이후 사건별 저장 (#17, architecture.md §4).

    - 함수 하나가 트랜잭션 하나다. 사건에 딸린 기기·세션·명령·승인 사용분·감사 기록을 함께 반영한다.
    - 사건은 대상 식별자와 함수로 식별한다. 같은 사건·같은 내용은 ALREADY_APPLIED, 다른 내용은 CONFLICT 다.
    - CONFLICT 는 기존 기록을 덮어쓰지 않는다. 상충한 결과는 감사 기록에 남긴다.
    - 저장 장애는 `errors.StoreNotApplied` 또는 `errors.StoreOutcomeUnknown` 으로 알린다.
    - 공용형에는 점유 세션이 없다. 세션 관련 반영은 대여형에만 적용한다.
    """

    def link_command(self, internal_request_id: str, node_id: str, action: Action) -> StoreResult:
        """요청의 명령을 연결한다. 이미 있으면 기존 명령과 진행 상태를 반환한다. 키: internal_request_id"""
        ...

    def begin_send(self, command_id: str) -> StoreResult:
        """PENDING → IN_PROGRESS. APPLIED 일 때만 전송을 시작한다. 취소된 명령은 CONFLICT. 키: command_id"""
        ...

    def cancel_not_sent(
        self,
        internal_request_id: str,
        expected: ExecutionStatus,
        reason: ReasonCode,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        """확실한 미전송 취소. 현재 상태가 expected 가 아니거나 결과가 있으면 CONFLICT.

        대여형: 기기 AVAILABLE, 세션 CLOSED·cancelled. 공통: 사용분 released, 명령 NOT_SENT, 감사.
        재검사 실패와 예약 대기 제한은 expected=PENDING, IOT 의 NOT_SENT 증명은 expected=IN_PROGRESS 다.
        """
        ...

    def record_result(
        self,
        command_id: str,
        outcome: CommandOutcome,
        occurred_at: datetime,
        rental_due_at: datetime | None,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        """실행 결과를 명령 하나에 한 번만 확정한다. outcome 은 SUCCEEDED·FAILED·UNKNOWN 이다.

        SUCCEEDED: 명령 성공, 대여형 기기·세션 ACTIVE 와 rental_due_at, 사용분 consumed, 감사.
        FAILED·UNKNOWN: 명령 결과, 기기 FAULT, 사용분 held, 세션 보존, 감사.
        이미 다른 결과가 있으면 CONFLICT 다. 늦은 성공으로 FAULT 를 해제하지 않는다.
        """
        ...

    def isolate_device(self, node_id: str, evidence: Mapping[str, Any]) -> StoreResult:
        """명령 결과가 아닌 이상으로 기기를 FAULT 로 격리한다 (NFR-04). 키: node_id

        센서 이상, 종료 뒤 안전 미확인 등이 해당한다. 점유 세션과 승인 사용분은 바꾸지 않는다.
        이미 FAULT 이면 ALREADY_APPLIED 를 반환하고 새 근거를 감사 기록에 남긴다.
        """
        ...

    def begin_close(self, use_id: str, reason: EndReason) -> StoreResult:
        """대여형 기기·세션 ACTIVE → CLOSING. 키: use_id"""
        ...

    def finish_close(
        self, use_id: str, closed_at: datetime, evidence: Mapping[str, Any]
    ) -> StoreResult:
        """CORE 가 안전을 확인한 뒤 세션 CLOSED, 기기 AVAILABLE, closed_at, 종료 기록. 키: use_id"""
        ...

    def execution(self, internal_request_id: str) -> CommandRecord | None:
        """반영 여부가 불명일 때 다시 조회한다."""
        ...


@runtime_checkable
class AuditLog(Protocol):
    """DB → CORE 감사 로그 (FR-22). 테이블 + JSONL."""

    def append(self, event: AuditEvent) -> None: ...


# ── CORE 내부 (decision ↔ lifecycle, #16) ──────────────────────────


@runtime_checkable
class DecisionRechecker(Protocol):
    """판정 제공, 상태 관리 사용. 전송 직전에 허가·근접·확정 대상을 다시 검사한다.

    통과는 Verdict.OK 뿐이다. 정상 거부는 판정과 사유를 그대로 반환한다.
    검사 오류는 Verdict.DENY 와 ReasonCode.RECHECK_FAILED 로 반환한다.
    자기 요청의 예약을 타인 점유로 보지 않는다.
    """

    def recheck(self, ctx: ExecutionContext) -> Decision: ...


@runtime_checkable
class ExecutionService(Protocol):
    """상태 관리 제공, 판정 사용. 허가된 요청의 실행을 이어 간다.

    같은 internal_request_id 의 재인계는 기존 진행 상태나 결과를 반환한다. 실행 시작은 한 번이다.
    업무 결과는 ExecutionResult 로 반환하고 예외로 알리지 않는다.
    """

    def execute(self, ctx: ExecutionContext) -> ExecutionResult: ...


# ── CORE 내부 (decision ↔ proximity) ────────────────────────────────


@runtime_checkable
class ProximityEstimator(Protocol):
    """관측 모델 + HMM (FR-16). 교체 가능한 인터페이스로 분리한다 (requirements.md §1.2)."""

    def observe(self, frame: RssiFrame) -> None:
        """RSSI 프레임 하나로 내부 상태(HMM 필터)를 갱신한다."""
        ...

    def posterior(
        self, beacons: Sequence[BeaconKey], candidates: Sequence[NodeInfo], at: datetime
    ) -> ProximityResult:
        """후보 장치별 사후확률. 후보가 없으면 빈 posteriors."""
        ...


# ── IOT 제공 ────────────────────────────────────────────────────────


@runtime_checkable
class NodeCommandGateway(Protocol):
    """IOT → CORE 노드 명령 (FR-06).

    결과는 NOT_SENT / SUCCEEDED / FAILED / UNKNOWN 으로 구분한다.
    시간 초과·연결 끊김은 UNKNOWN 이며 미실행으로 간주하지 않는다.
    같은 command_id 로만 재전송하고 새 ID 로 자동 재시도하지 않는다.
    """

    def send(self, command: NodeCommand, timeout_sec: float) -> CommandResult: ...


# ── CORE 제공 ───────────────────────────────────────────────────────


@runtime_checkable
class NodeEventSink(Protocol):
    """CORE 가 구현하고 IOT 가 호출한다. 검증을 마친 MQTT 메시지만 넘긴다."""

    def on_announce(self, announce: NodeAnnounce) -> None: ...

    def on_rssi(self, frame: RssiFrame) -> None: ...

    def on_status(self, status: NodeStatus) -> None: ...


@runtime_checkable
class CoreService(Protocol):
    """CORE → API 기능 (FR-09). MCP 도구 6개에 대응한다.

    user_id 는 API 가 토큰으로 식별해 넘긴다. 반환값에는 user_id·점유 주체가 없다.
    관리 기능(계정 등록·노드 신뢰·정책·승인 처리·FAULT 복구)은 여기에 넣지 않고,
    API 담당 배정 후 별도 `AdminService` Protocol 로 추가한다.
    """

    def find_available_devices(self, user_id: str) -> Sequence[Mapping[str, Any]]: ...

    def get_device_state(self, user_id: str, node_id: str) -> Mapping[str, Any]: ...

    def request_access(self, user_id: str, node_id: str, action: Action, request_id: str) -> Decision: ...

    def request_use(self, req: UseRequest) -> Decision: ...

    def release_use(self, user_id: str, use_id: str) -> Decision: ...

    def get_use_status(self, user_id: str, use_id: str) -> Mapping[str, Any]: ...
