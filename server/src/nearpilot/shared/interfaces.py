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
    CloseResult,
    CommandRecord,
    CommandResult,
    Decision,
    ExecutionContext,
    ExecutionResult,
    NodeAnnounce,
    NodeCommand,
    NodeInfo,
    NodeResult,
    NodeStatus,
    PolicyCheckResult,
    PolicyRevision,
    ProximityResult,
    RequestLink,
    ReserveRequest,
    ReserveResult,
    RssiFrame,
    SafetyCheckRequest,
    SafetyCheckResult,
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
class PolicyChecker(Protocol):
    """DB(`core/policy`) → CORE ④ 정책 검사 (#23).

    정책 모드, 승인 범위, 정책 버전 비교 규칙은 이 함수 안에 둔다. 판정은 순서와 중단만 맡는다.
    ③ 에서 확정한 대상만 검사한다. 검사 결과로 다른 기기를 고르지 않는다 (FR-18).
    저장소를 바꾸지 않는다. 승인 사용분 예약은 ⑤⑥ 의 `reserve_if_free()` 가 한다.
    """

    def check(
        self, user_id: str, node_id: str, action: Action, now: datetime
    ) -> PolicyCheckResult: ...


@runtime_checkable
class Repository(Protocol):
    """DB → CORE 저장소. 쓰기는 이 인터페이스 한 경로로만 한다 (의존 규칙 5)."""

    # ⓪ 중복
    def link_request(self, req: UseRequest, input_digest: str) -> RequestLink:
        """외부 키 (user_id, request_id) 를 내부 요청 ID 에 연결한다.

        input_digest 는 판정이 정규화 입력(대상 지정, 동작, 원 요청 연결 ID)으로 계산해서 넘긴다 (#24).
        DB 는 값을 저장하고 비교만 한다.
        """
        ...

    def save_decision(
        self, decision: Decision, context: Mapping[str, Any], settings_version: str
    ) -> None: ...

    # ① 비콘
    def beacons_of(self, user_id: str) -> Sequence[BeaconKey]: ...

    def save_rssi(self, frame: RssiFrame) -> None:
        """등록된 활성 비콘의 프레임만 저장한다. 그 밖의 비콘은 저장하지 않는다 (FR-05).

        `(node_id, session_id, seq, beacon)` 이 같은 프레임은 한 번만 저장한다.
        수집 라벨은 `tools/data_collection` 만 쓴다. 이 경로의 라벨은 NULL 이다.
        """
        ...

    def rssi_since(self, beacons: Sequence[BeaconKey], since: datetime) -> Sequence[RssiFrame]: ...

    # ② 후보
    def nodes(self, capability: Capability | None = None) -> Sequence[NodeInfo]: ...

    def upsert_announced_node(self, announce: NodeAnnounce) -> None:
        """기능 발표를 반영한다 (FR-04). 발표는 신뢰 부여가 아니다.

        처음 보는 node_id 는 DISCOVERED 로 만든다. 내용이 같은 재발표는 신뢰 상태를 바꾸지 않는다.
        기능이나 점유 방식이 다르면 새 값으로 바꾸고 신뢰 상태를 DISCOVERED 로 되돌린다.
        되돌린 변경은 같은 트랜잭션에서 감사 로그에 남긴다.
        """
        ...

    def touch_node(self, node_id: str, seen_at: datetime) -> None:
        """저장한 값보다 새로운 시각일 때만 last_seen_at 을 바꾼다 (FR-13)."""
        ...

    def set_node_session(self, node_id: str, session_id: str, first_seen_at: datetime) -> None:
        """현재 기기 연결 ID 와 그 연결의 첫 수신 시각을 저장한다 (FR-04).

        같은 session_id 를 다시 받으면 첫 수신 시각을 바꾸지 않는다.
        조회는 `nodes()` 의 `NodeInfo.session_id`, `NodeInfo.session_first_seen_at` 으로 한다.
        """
        ...

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
        self, use_id: str, closed_at: datetime, check_id: str, evidence: Mapping[str, Any]
    ) -> StoreResult:
        """CORE 가 종료를 결정한 뒤 세션 CLOSED, 기기 AVAILABLE, closed_at, 종료 기록. 키: use_id

        check_id 는 종료 근거로 채택한 반납 검사다. evidence 는 그 검사의 근거다. 함께 저장한다.
        기기 상태, 점유 세션, 채택한 검사가 바뀌지 않았는지 같은 트랜잭션에서 검사한다.
        같은 use_id·같은 check_id 의 재호출은 ALREADY_APPLIED, 다른 check_id 나 내용은 CONFLICT 다.
        DB 는 문·구동부·센서 조건을 다시 판단하지 않는다.
        """
        ...

    def close_result(self, use_id: str) -> CloseResult | None:
        """반납의 저장된 완료 기록과 채택한 check_id·근거를 조회한다 (FR-20). 키: use_id

        finish_close 의 반영 여부가 불명일 때 쓴다. 기록이 없으면 None 이다.
        None 은 미반영의 증거가 아니다. 저장이 진행 중일 수 있다.
        """
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
        """후보 장치별 사후확률. 후보가 없으면 빈 posteriors.

        판정은 명시 지정 요청에도 ② 의 전체 후보를 넘긴다 (#26). 지정 대상은 인자로 받지 않는다.
        같은 입력에는 지정 여부와 상관없이 같은 출력을 낸다 (NFR-09).
        판정이 `posteriors[지정 대상]` 을 읽어 임계값과 비교한다.
        """
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


@runtime_checkable
class SafetyCheckGateway(Protocol):
    """IOT → CORE 반납 검사 요청 (FR-20). 물리 구동 명령과 구분한다.

    결과를 기다리지 않는다. 결과는 `NodeEventSink.on_safety_check_result` 로 온다.
    응답 기한과 재검사는 CORE 가 `Settings` 값으로 잰다. 전송 실패도 CORE 에서는 응답 기한 초과다.
    같은 check_id 의 재전송은 내용을 바꾸지 않는다. 다시 측정하려면 새 check_id 를 쓴다.
    """

    def request_check(self, request: SafetyCheckRequest) -> None: ...


# ── CORE 제공 ───────────────────────────────────────────────────────


@runtime_checkable
class NodeEventSink(Protocol):
    """CORE 가 구현하고 IOT 가 호출한다. 검증을 마친 MQTT 메시지만 넘긴다.

    IOT 는 안전을 판정하지 않는다. 점유 해제와 장애 상태 변경은 CORE 가 결정한다.
    인계가 예외로 실패하면 IOT 는 실패를 기록하고 다시 인계한다. 중복 반영은 CORE 와 DB 가 막는다.
    """

    def on_announce(self, announce: NodeAnnounce) -> None: ...

    def on_rssi(self, frame: RssiFrame) -> None: ...

    def on_status(self, status: NodeStatus) -> None: ...

    def on_result(self, result: NodeResult) -> None:
        """기기가 보고한 실행 결과. 결과 대기 중과 결과 대기가 끝난 뒤의 보고를 모두 받는다 (FR-07).

        과거 명령의 결과는 정산과 감사에만 쓴다. 현재 온라인·안전 상태를 갱신하지 않는다.
        """
        ...

    def on_safety_check_result(self, result: SafetyCheckResult) -> None:
        """기기가 보고한 반납 검사 결과 (FR-20)."""
        ...


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
