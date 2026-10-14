"""계층 사이에서 주고받는 값 객체 (architecture.md §4 도메인 모델).

모두 불변(frozen)이다. Mapping 필드는 복사하고 내부 JSON 컨테이너도 불변으로 만든다.
DB 행을 그대로 옮긴 것이 아니라 계층 간에 필요한 필드만 담는다.
DB 스키마의 전체 필드는 architecture.md §6 과 db 계층이 정의한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nearpilot.shared.config import Settings
from nearpilot.shared.enums import (
    ApprovalUsageState,
    ExecutionKind,
    StoreConflictReason,
    RecoveryStatus,
    RecoveryBlocker,
    RecoveryOutcome,
    RecoveryReason,
    AccessMode,
    Action,
    ActuatorState,
    ApprovalMode,
    ApprovalStatus,
    Capability,
    CommandOutcome,
    DeviceState,
    EndReason,
    ExecutionStatus,
    NodeResultState,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    Role,
    SafetyCheckOutcome,
    SessionState,
    Step,
    StoreOutcome,
    TargetKind,
    TrustState,
    Verdict,
    Visibility,
)


class _FrozenDict(dict):
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

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenDict:
        return _FrozenDict(
            (deepcopy(key, memo), deepcopy(value, memo))
            for key, value in self.items()
        )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


# ── 계정·비콘 ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Account:
    """토큰으로 식별한 계정. API 가 DB 에서 직접 받는 유일한 값 (의존 규칙 1)."""

    user_id: str
    role: Role


@dataclass(frozen=True, slots=True)
class BeaconKey:
    """iBeacon 식별자. `(uuid, major, minor)` 조합이 고유하다."""

    uuid: str
    major: int
    minor: int


# ── 노드·정책 ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NodeInfo:
    """판정에 필요한 노드 정보. ONLINE 여부는 저장하지 않고 last_seen_at + TTL 로 계산한다."""

    node_id: str
    place_id: str | None
    capability: Capability
    occupancy: Occupancy
    trust_state: TrustState
    device_state: DeviceState
    exclusive_group: str | None
    last_seen_at: datetime | None
    release_policy: Mapping[str, Any] = field(default_factory=dict)
    anchor_params: Mapping[str, float] = field(default_factory=dict)  # A, n, σ
    session_id: str | None = None  # 현재 기기 연결 ID. 확인한 연결이 없으면 None
    session_first_seen_at: datetime | None = None  # 현재 연결의 첫 수신 시각

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_policy", _freeze(self.release_policy))
        object.__setattr__(self, "anchor_params", _freeze(self.anchor_params))


@dataclass(frozen=True, slots=True)
class PolicyRevision:
    policy_id: str
    version: int
    node_id: str
    visibility: Visibility
    mode: AccessMode
    approver_user_id: str | None
    approval_mode: ApprovalMode
    approval_ttl_sec: int | None
    max_uses: int | None  # 기간형 무제한이면 None


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    node_id: str
    requester_user_id: str
    action: Action
    policy_id: str
    policy_version: int
    status: ApprovalStatus
    valid_until: datetime | None
    remaining_uses: int | None  # 예약·보류·소진을 뺀 잔여. 무제한이면 None


@dataclass(frozen=True, slots=True)
class PolicyCheckResult:
    """④ 정책 검사 결과 (#23). 사용자 ID·점유 주체 필드를 두지 않는다.

    verdict 는 OK(허용)·NEED_APPROVAL(승인 필요)·DENY(거부) 중 하나다.
    NEED_APPROVAL 의 reason 은 APPROVAL_REQUIRED·APPROVAL_PENDING·APPROVAL_REJECTED·
    APPROVAL_EXPIRED·APPROVAL_REQUEST_EXPIRED(대기 신청 만료)·APPROVAL_EXHAUSTED(사용 횟수 소진)·
    ACTION_NOT_ALLOWED(승인과 다른 동작)로 구분한다. 다시 승인받으면 되는 경우이므로 DENY 로 주지 않는다. approval_id 는 ⑤⑥ 의
    `ReserveRequest.approval_id` 로 넘어간다. 정책이 없는 기기는 DENY 로 거부하며 이때
    policy_version 은 None 이다.
    """

    verdict: Verdict
    reason: ReasonCode
    policy_version: int | None  # 적용한 정책이 없으면 None
    approval_id: str | None = None  # 승인이 필요 없거나 아직 없으면 None


# ── 요청·판정 ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TargetSpec:
    kind: TargetKind
    capability: Capability
    node_id: str | None = None  # EXPLICIT 일 때만

    @classmethod
    def parse(cls, raw: str) -> TargetSpec:
        """`nearest:locker` · `any:locker` · `locker-1` 를 해석한다. 형식이 틀리면 ValueError."""
        if ":" in raw:
            prefix, cap = raw.split(":", 1)
            return cls(TargetKind(prefix), Capability(cap))
        cap, sep, num = raw.rpartition("-")
        if not sep or not num.isdigit():
            raise ValueError(f"target_spec 형식 오류: {raw!r}")
        return cls(TargetKind.EXPLICIT, Capability(cap), raw)


@dataclass(frozen=True, slots=True)
class UseRequest:
    """`request_use` 입력. API 가 토큰으로 user_id 를 채워 CORE 에 넘긴다."""

    user_id: str
    request_id: str  # 외부 멱등 키, (user_id, request_id) 로 고유
    target: TargetSpec
    action: Action
    host_kind: str | None
    received_at: datetime
    parent_internal_request_id: str | None = None  # 재질문·승인 후 재요청 연결


@dataclass(frozen=True, slots=True)
class RequestLink:
    """⓪ 결과: 외부 키를 내부 요청 ID 에 연결한 결과."""

    internal_request_id: str
    is_new: bool
    conflict: bool  # 같은 키, 다른 입력
    previous: Decision | None = None  # 이미 판정이 끝난 요청이면 그 결과


@dataclass(frozen=True, slots=True)
class Decision:
    """CORE → API 판정 결과.

    사용자 ID·점유 주체 필드를 **처음부터 두지 않는다** (FR-11, NFR-08).
    API 는 이 객체를 그대로 호스트 응답으로 바꿔도 누출되지 않는다.
    """

    internal_request_id: str
    request_id: str
    verdict: Verdict
    reason: ReasonCode
    node_id: str | None = None
    use_id: str | None = None
    p_max: float | None = None
    message: str | None = None  # 호스트에 보여줄 안내 문구 (선택)


# ── 근접 (FR-16·17) ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RssiFrame:
    """노드 하나가 약 1초 동안 본 비콘 하나의 RSSI.

    DB 는 등록된 활성 비콘의 프레임만 저장한다. `(node_id, session_id, seq, beacon)` 이
    같은 프레임은 한 번만 저장한다 (FR-05).
    """

    node_id: str
    beacon: BeaconKey
    rssi: float  # dBm
    frame_ts: datetime  # 관측 시각. 수신 시각으로 바꾸지 않는다.
    session_id: str  # 기기 연결 ID
    seq: int  # 같은 연결·토픽의 발행 순번
    sent_at: datetime  # 기기 발행 시각
    received_at: datetime  # 서버 수신 시각


@dataclass(frozen=True, slots=True)
class ProximityResult:
    posteriors: Mapping[str, float]  # node_id → 사후확률, 합 ≈ 1
    window_start: datetime
    window_end: datetime
    model_version: str
    calib_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "posteriors", _freeze(self.posteriors))

    @property
    def best(self) -> tuple[str, float] | None:
        """사후확률 최대 장치. 동률이면 node_id 순 (결정론)."""
        if not self.posteriors:
            return None
        return min(self.posteriors.items(), key=lambda kv: (-kv[1], kv[0]))


# ── 점유 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReserveRequest:
    """`reserve_if_free` 입력. ⑤⑥ 검사·RESERVED 생성·승인 사용분 예약을 한 트랜잭션에서.

    대여시간 기준 `rental_due_at` 은 예약 때 정하지 않는다. 실행 성공 때 CORE 가 계산한다.
    """

    internal_request_id: str
    user_id: str
    node_id: str
    action: Action
    approval_id: str | None  # 승인이 필요 없으면 None
    release_policy: Mapping[str, Any] = field(default_factory=dict)  # 생성 당시 값 보존

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_policy", _freeze(self.release_policy))


@dataclass(frozen=True, slots=True)
class ReserveResult:
    outcome: ReserveOutcome
    use_id: str | None = None  # 공용형은 세션이 없으므로 None
    usage_id: str | None = None  # 승인 사용분 ID


@dataclass(frozen=True, slots=True)
class UseSession:
    """대여형 점유 세션 (FR-19, FR-20). 초과시간은 저장하지 않고 계산한다."""

    use_id: str
    internal_request_id: str
    node_id: str
    state: SessionState
    rental_due_at: datetime | None = None  # 대여시간 기준 시각. 실행 성공 전에는 None
    closed_at: datetime | None = None
    end_reason: EndReason | None = None
    release_policy: Mapping[str, Any] = field(default_factory=dict)  # 생성 당시 종료 정책

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_policy", _freeze(self.release_policy))

    @property
    def check_criteria(self) -> CheckCriteria | None:
        """생성 당시 종료 정책이 정한 반납 검사 기준. 없으면 None.

        `release_policy["check_criteria"]` 의 `id` 와 `version` 을 읽는다.
        기준이 없는 점유 세션을 검사 통과로 추정하지 않는다 (FR-20).
        """
        raw = self.release_policy.get("check_criteria")
        if not isinstance(raw, Mapping):
            return None
        criteria_id, version = raw.get("id"), raw.get("version")
        if not isinstance(criteria_id, str) or not criteria_id:
            return None
        if isinstance(version, bool) or not isinstance(version, int):
            return None
        return CheckCriteria(criteria_id, version)


@dataclass(frozen=True, slots=True)
class CheckCriteria:
    """반납 검사 기준의 ID 와 버전. 점유 세션 생성 당시 종료 정책과 함께 보존한다 (FR-20)."""

    criteria_id: str
    criteria_version: int


# ── 실행 인계와 사건별 저장 (#16, #17) ──────────────────────────────


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """판정 → 상태 관리 실행 인계 (#16). CORE 내부 값이며 호스트 응답이 아니다."""

    internal_request_id: str
    request: UseRequest  # 토큰 계정, 외부 요청 ID, 원래 대상 지정과 동작
    node_id: str  # 확정한 물리 대상. 재검사와 실행이 바꾸지 않는다.
    use_id: str | None  # 공용형이면 None
    usage_id: str | None  # 승인이 필요 없으면 None
    settings: Settings  # 판정에 적용한 설정. 실행과 대여시간 계산도 이 값을 쓴다.
    execution_kind: ExecutionKind = ExecutionKind.RENTAL_INITIAL

    def __post_init__(self) -> None:
        if (self.execution_kind == ExecutionKind.SHARED) != (self.use_id is None):
            raise ValueError("공용형은 use_id 가 없고 대여형은 use_id 가 있어야 한다")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """상태 관리 → 판정 실행 결과 (#16). 사용자 ID·점유 주체 필드를 두지 않는다."""

    internal_request_id: str
    status: ExecutionStatus
    command_id: str | None = None  # 명령을 연결하기 전에 막혔으면 None
    use_id: str | None = None  # 호스트에는 SUCCEEDED 일 때만 반환한다
    recheck: Decision | None = None  # 재검사가 전송을 막았을 때 그 판정


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """요청 하나에 연결된 물리 명령과 저장된 진행 상태 (#17)."""

    command_id: str
    internal_request_id: str
    node_id: str
    action: Action
    status: ExecutionStatus
    execution_kind: ExecutionKind = ExecutionKind.RENTAL_INITIAL
    use_id: str | None = None
    usage_id: str | None = None
    reserved_at: datetime | None = None
    send_started_at: datetime | None = None
    send_attempted: bool = False
    outcome: CommandOutcome | None = None
    occurred_at: datetime | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze(self.evidence))


@dataclass(frozen=True, slots=True)
class StoreResult:
    """`ExecutionStore` 사건 반영 결과와 반영 뒤의 현재 기록 (#17)."""

    outcome: StoreOutcome
    command: CommandRecord | None = None
    session: UseSession | None = None
    conflict_reason: StoreConflictReason | None = None
    recovery: RecoveryRecord | None = None


@dataclass(frozen=True, slots=True)
class CloseResult:
    """`ExecutionStore.close_result` 가 반환하는 반납 완료 기록 (FR-20).

    기기의 현재 상태가 아니라 그 반납의 저장된 완료 기록이다.
    """

    use_id: str
    closed_at: datetime
    end_reason: EndReason
    check_id: str  # 종료 근거로 채택한 반납 검사
    evidence: Mapping[str, Any] = field(default_factory=dict)  # 채택한 검사 근거

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze(self.evidence))


# ── 노드 이벤트·명령 (IOT ↔ CORE) ──────────────────────────────────
#
# 기기 보고의 공통 정보는 모든 보고 모델에서 같은 이름을 쓴다 (architecture.md §4 기기 보고의 인계).
#   session_id  기기 연결 ID. 기기가 MQTT 에 접속할 때마다 새로 만든다.
#   seq         같은 연결·토픽에서 1부터 증가하는 발행 순번
#   sent_at     기기 발행 시각. 관측 시각과 구분한다.
#   received_at 서버 수신 시각. 관측 시각을 이 값으로 바꾸지 않는다.
# `detail` 에는 선택적인 진단 정보만 둔다. 판정에 쓰는 값은 명시적인 필드로 둔다.


@dataclass(frozen=True, slots=True)
class NodeAnnounce:
    """기능 발표 (FR-04). 발표는 신뢰 부여가 아니다."""

    node_id: str
    capability: Capability
    occupancy: Occupancy
    received_at: datetime
    session_id: str
    seq: int
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class PhysicalState:
    """`status` 의 원시 물리 관측 (architecture.md §5 MQTT 3절). None 은 확인하지 못했다는 뜻이다."""

    door_closed: bool | None
    light_on: bool | None
    actuator_state: ActuatorState
    sensor_fault: bool | None


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """기기 상태 보고. 원시 관측만 담는다.

    IOT 는 안전을 판정하지 않는다. 이 보고는 반납 검사를 대신하지 못한다 (FR-07).
    """

    node_id: str
    received_at: datetime
    session_id: str
    seq: int
    sent_at: datetime
    observed_at: datetime  # 관측 시각
    physical: PhysicalState
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _freeze(self.detail))


@dataclass(frozen=True, slots=True)
class NodeResult:
    """기기가 보고한 실행 결과 사건 하나 (FR-07).

    기기 보고만 이 모델로 만든다. 서버의 타임아웃과 통신 오류는 이 모델로 만들지 않는다.
    같은 보고를 다시 받아도 event_id, state, occurred_at 은 같다.
    state 와 error_code 는 기기 값을 그대로 담는다. IOT 는 DEVICE_BUSY 거부를 NOT_SENT 로 바꾸지 않는다.
    """

    node_id: str
    session_id: str
    seq: int
    sent_at: datetime
    received_at: datetime
    command_id: str
    event_id: str  # 결과 사건 ID. 기기가 붙인다.
    state: NodeResultState
    occurred_at: datetime  # 결과 발생 시각
    error_code: str | None = None  # 기기 통신의 근거. rejected 의 값은 RejectCode 다.
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _freeze(self.detail))


@dataclass(frozen=True, slots=True)
class NodeCommand:
    """CORE → IOT 실행 명령 (FR-06). 사용자 ID·점유 주체 필드를 두지 않는다.

    같은 command_id 의 재전송은 아래 내용을 바꾸지 않는다.
    """

    command_id: str
    node_id: str
    action: Action
    target_session_id: str  # 대상 기기 연결 ID. `NodeInfo.session_id` 에서 가져온다.
    issued_at: datetime
    expires_at: datetime  # issued_at + Settings.exec_timeout_sec


@dataclass(frozen=True, slots=True)
class CommandResult:
    """`NodeCommandGateway.send` 한 번의 결과.

    report 는 이 결과의 근거가 된 기기 보고다. 서버가 만든 결과(NOT_SENT, 타임아웃, 통신 오류)는
    report 가 None 이다. 그래서 서버가 만든 결과에는 event_id 가 없다.
    같은 기기 보고는 `NodeEventSink.on_result` 에도 같은 event_id 로 온다.
    """

    command_id: str
    outcome: CommandOutcome
    report: NodeResult | None = None
    reported_at: datetime | None = None  # 서버가 결과를 정한 시각


@dataclass(frozen=True, slots=True)
class SafetyCheckRequest:
    """CORE → IOT 반납 검사 요청 (FR-20). 물리 구동 명령이 아니다.

    검사 시도마다 새 check_id 를 쓴다. 반납(use_id)과의 연결은 서버 안에만 둔다.
    사용자 ID·점유 주체 필드를 두지 않는다.
    """

    check_id: str
    node_id: str
    target_session_id: str  # 대상 기기 연결 ID
    criteria: CheckCriteria  # 점유 세션 생성 당시 종료 정책의 검사 기준
    issued_at: datetime
    expires_at: datetime  # issued_at + Settings.safety_check_timeout_sec


@dataclass(frozen=True, slots=True)
class SafetyCheckResult:
    """기기가 보고한 반납 검사 결과 (FR-07, FR-20).

    기기는 요청을 받은 뒤 새로 검사한다. 같은 check_id 의 재전송에는 같은 결과를 보고한다.
    종료는 CORE 가 결정한다. PASSED 는 점유 해제 결정이 아니다.
    """

    node_id: str
    session_id: str
    seq: int
    sent_at: datetime
    received_at: datetime
    check_id: str
    event_id: str  # 결과 사건 ID. 기기가 붙인다.
    criteria: CheckCriteria  # 기기가 실제로 적용한 검사 기준
    checked_at: datetime  # 검사 시각
    outcome: SafetyCheckOutcome
    evidence: Mapping[str, Any] = field(default_factory=dict)  # 검사 근거

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze(self.evidence))


# ── 감사 (FR-22) ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    step: Step
    payload: Mapping[str, Any]  # 입력·결과·버전·임계값·후보 확률
    internal_request_id: str | None = None  # 인증 실패 등은 None
    command_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))


# ── 복원·복구 (FR-19, NFR-04) ───────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PendingRequestRecord:
    internal_request_id: str
    request: UseRequest
    input_digest: str
    execution_kind: ExecutionKind
    use_id: str | None
    usage_id: str | None
    reserved_at: datetime
    decision: Decision | None = None


@dataclass(frozen=True, slots=True)
class RestoredSession:
    session: UseSession
    user_id: str
    created_at: datetime
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze(self.evidence))


@dataclass(frozen=True, slots=True)
class ApprovalUsageRecord:
    usage_id: str
    approval_id: str
    internal_request_id: str
    command_id: str | None
    state: ApprovalUsageState


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    """근거 참조와 시각. None 은 확인하지 못했다는 뜻이다. 현재성은 CORE 가 검사한다."""

    evidence_id: str
    session_id: str
    observed_at: datetime
    execution_outcome: CommandOutcome | None = None
    safety_confirmed: bool | None = None
    physical_state_synced: bool | None = None


@dataclass(frozen=True, slots=True)
class ApprovalUsageSettlement:
    usage_id: str
    expected_state: ApprovalUsageState
    state: ApprovalUsageState

    def __post_init__(self) -> None:
        if self.state not in (ApprovalUsageState.CONSUMED, ApprovalUsageState.RELEASED):
            raise ValueError("복구 정산의 사용분 결과는 consumed 또는 released 다")


@dataclass(frozen=True, slots=True)
class RecoverySettlement:
    """CORE → DB 논리 정산. digest 는 CORE 가 계산하고 DB 는 저장·대조한다.

    digest 는 대상 연결, 채택한 근거, 사용분 정산, 종료 내용으로 계산한다.
    승인 호출 시각은 제외한다. 실제 직렬화 방식은 구현 담당자가 spec 에 정한다.
    """

    recovery_id: str
    node_id: str
    approved_by_user_id: str
    approved_at: datetime
    settlement_digest: str
    expected_node_state: DeviceState
    expected_recovery_status: RecoveryStatus
    expected_commands: Mapping[str, ExecutionStatus]
    expected_sessions: Mapping[str, SessionState]
    evidence: tuple[RecoveryEvidence, ...]
    usage_settlements: tuple[ApprovalUsageSettlement, ...]
    use_id: str | None = None
    closed_at: datetime | None = None
    end_reason: EndReason | None = None

    def __post_init__(self) -> None:
        for name in ("expected_commands", "expected_sessions", "evidence", "usage_settlements"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))
        if not self.settlement_digest:
            raise ValueError("정산 요약값은 비어 있을 수 없다")
        if self.use_id is None:
            if self.closed_at is not None or self.end_reason is not None:
                raise ValueError("공용형 복구에는 점유 종료가 없다")
        elif self.closed_at is None or self.end_reason is None:
            raise ValueError("대여형 복구에는 종료 시각과 사유가 필요하다")


@dataclass(frozen=True, slots=True)
class RecoveryCompletion:
    settlement: RecoverySettlement
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    recovery_id: str
    node_id: str
    internal_request_ids: tuple[str, ...]
    command_ids: tuple[str, ...]
    use_id: str | None
    created_at: datetime
    status: RecoveryStatus
    evidence: tuple[RecoveryEvidence, ...] = ()
    completion: RecoveryCompletion | None = None

    def __post_init__(self) -> None:
        for name in ("internal_request_ids", "command_ids", "evidence"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class RestoreBundle:
    """기기 하나의 일관된 DB 읽기 시점. 내부 조회이며 호스트 응답이 아니다."""

    node: NodeInfo
    read_at: datetime
    requests: tuple[PendingRequestRecord, ...] = ()
    commands: tuple[CommandRecord, ...] = ()
    sessions: tuple[RestoredSession, ...] = ()
    approval_usages: tuple[ApprovalUsageRecord, ...] = ()
    recoveries: tuple[RecoveryRecord, ...] = ()

    def __post_init__(self) -> None:
        for name in ("requests", "commands", "sessions", "approval_usages", "recoveries"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class RecoveryReadiness:
    """상태 관리 → API. 다른 계정의 점유 정보와 사용자 ID 를 담지 않는다."""

    node_id: str
    recovery_id: str | None
    status: RecoveryStatus
    blockers: tuple[RecoveryBlocker, ...]
    evidence: tuple[RecoveryEvidence, ...]
    checked_at: datetime

    def __post_init__(self) -> None:
        for name in ("blockers", "evidence"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class RecoveryApprovalResult:
    recovery_id: str
    outcome: RecoveryOutcome
    reason_code: RecoveryReason
    readiness: RecoveryReadiness
    completed_at: datetime | None = None
