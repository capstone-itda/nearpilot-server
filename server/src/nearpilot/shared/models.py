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
from math import fsum, isclose, isfinite
from typing import Any

from nearpilot.shared.config import Settings, is_light_color
from nearpilot.shared.enums import (
    ApprovalDecisionReason,
    AdminWriteOutcome,
    AdminWriteReason,
    ApprovalUsageState,
    ExecutionKind,
    InternalRequestCause,
    StoreConflictReason,
    RecoveryStatus,
    RecoveryBlocker,
    RecoveryOutcome,
    RecoveryReason,
    AccessMode,
    Action,
    CommandState,
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
class LightControls:
    """조명 기기가 발표한 조명 제어 정보 (FR-04, FR-09).

    brightness_max 는 펌웨어가 지키는 채널별 출력 상한이다. color 는 색 지원 여부다.
    """

    brightness_max: int
    color: bool

    def __post_init__(self) -> None:
        if isinstance(self.brightness_max, bool) or not isinstance(self.brightness_max, int):
            raise ValueError("brightness_max 는 정수여야 한다")
        if not 1 <= self.brightness_max <= 255:
            raise ValueError("brightness_max 는 1~255 여야 한다")
        if not isinstance(self.color, bool):
            raise ValueError("color 는 불리언이어야 한다")


@dataclass(frozen=True, slots=True)
class LightSetting:
    """조명 `on` 의 밝기와 색 (FR-09). 각 값은 없을 수 있다.

    방문자 입력(`UseRequest.light`)은 받은 값만 담는다. 서버가 정한 값(`NodeCommand.light`)은 둘 다 담는다.
    밝기는 0~255 정수, 색은 대문자 `#RRGGBB` 다. 기기별 상한 검사는 판정이 한다.
    """

    brightness: int | None = None
    color: str | None = None

    def __post_init__(self) -> None:
        b = self.brightness
        if b is not None and (isinstance(b, bool) or not isinstance(b, int) or not 0 <= b <= 255):
            raise ValueError("brightness 는 0~255 정수여야 한다")
        if self.color is not None:
            if not is_light_color(self.color):
                raise ValueError("color 는 #RRGGBB 여야 한다")
            object.__setattr__(self, "color", self.color.upper())

    @property
    def complete(self) -> bool:
        return self.brightness is not None and self.color is not None

    def to_json(self) -> dict[str, Any]:
        """저장과 비교에 쓰는 정규화 값. 없는 값은 넣지 않는다."""
        out: dict[str, Any] = {}
        if self.brightness is not None:
            out["brightness"] = self.brightness
        if self.color is not None:
            out["color"] = self.color
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> LightSetting | None:
        if data is None:
            return None
        if not isinstance(data, Mapping) or set(data) - {"brightness", "color"}:
            raise ValueError(f"조명 설정 형식 오류: {data!r}")
        return cls(data.get("brightness"), data.get("color"))


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
    session_id: str | None = None  # 현재 기기 연결 ID. 확인한 연결이 없으면 None
    session_first_seen_at: datetime | None = None  # 현재 연결의 첫 수신 시각
    light_controls: LightControls | None = None  # 발표한 조명 제어 정보. 없으면 인자를 받지 않는다 (FR-09)

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_policy", _freeze(self.release_policy))


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
class ApprovalRecord:
    """요청자·기기의 승인 이력 한 건 (#60). core/policy 가 승인 필요 사유를 고르는 데 쓴다.

    사용자 ID 필드를 두지 않는다. 만료된 pending 도 status 는 PENDING 이다.
    """

    approval_id: str
    action: Action
    policy_version: int
    status: ApprovalStatus
    created_at: datetime
    pending_until: datetime  # 대기 신청 만료 시각
    valid_until: datetime | None  # 승인 전이면 None
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
    light: LightSetting | None = None  # 조명 `on` 의 받은 인자. 인자가 없으면 None (FR-09)


@dataclass(frozen=True, slots=True)
class RequestLink:
    """⓪ 결과: 외부 키를 내부 요청 ID 에 연결한 결과."""

    internal_request_id: str
    is_new: bool
    conflict: bool  # 같은 키, 다른 입력
    previous: Decision | None = None  # 이미 판정이 끝난 요청이면 그 결과
    # 중복 방지 항목이 dedup_entry_limit 이상이면 True다 (#165). 이 요청은 request_id를
    # 비운 채 연결돼 한도 집계에서 빠진다. 판정은 DENY·CAPACITY_EXCEEDED로 거부한다.
    capacity_exceeded: bool = False


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
    posteriors: Mapping[str, float]  # node_id → 장소 전체 상태의 확률. 후보 합 ≤ 1, 다시 정규화하지 않는다.
    window_start: datetime
    window_end: datetime
    model_version: str
    calib_version: str

    def __post_init__(self) -> None:
        for probability in self.posteriors.values():
            if isinstance(probability, bool) or not isinstance(probability, (int, float)):
                raise ValueError("사후확률은 숫자여야 한다")
            if not isfinite(probability) or not 0.0 <= probability <= 1.0:
                raise ValueError("사후확률은 [0, 1] 범위의 유한한 값이어야 한다")
        total = fsum(self.posteriors.values())
        if total > 1.0 and not isclose(total, 1.0):
            raise ValueError("후보 사후확률의 합은 1 이하다")
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
    공용형도 이 입력으로 승인 사용분을 확보한다. DB 가 occupancy 로 ⑤⑥·세션 생성을 생략한다.
    공용형 open 정책이고 approval_id 가 None 이면 쓰기 없이 RESERVED 를 반환한다.
    무제한 기간형 승인도 요청별 사용분을 만든다. 횟수 검사만 생략한다.
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
    """공용형 성공은 RESERVED 와 use_id=None. 재조작 성공은 REOPERATING 과 기존 use_id 다."""

    outcome: ReserveOutcome
    use_id: str | None = None  # 공용형은 세션이 없으므로 None
    usage_id: str | None = None  # 승인 사용분 ID
    # DB 가 저장한 예약 시각 (#80). 판정이 ExecutionContext.reserved_at 으로 그대로 넘긴다.
    # 쓰기가 없는 결과(공용형 open·승인 없음, 거부)는 None 이다.
    reserved_at: datetime | None = None


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


# ── 실행 인계와 사건별 저장 (#16, #17) ──────────────────────────────


@dataclass(frozen=True, slots=True)
class InternalRequest:
    """서버가 방문자 요청 없이 시작한 명령 요청 (#86). 사용자 ID 가 없다.

    판정 ⓪~⑥을 거치지 않으므로 판정 기록이 없다. settings 는 시작할 때 적용한 설정이다.
    방문자의 use_id 조회(FR-21)와 호스트 응답에 나타나지 않는다.
    """

    internal_request_id: str
    node_id: str
    action: Action
    cause: InternalRequestCause
    settings: Settings
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """판정 → 상태 관리 실행 인계 (#16). CORE 내부 값이며 호스트 응답이 아니다."""

    internal_request_id: str
    request: UseRequest | None  # 토큰 계정, 외부 요청 ID, 원래 대상 지정과 동작. 서버 내부 요청은 None
    # SERVER_INTERNAL 일 때만 있다 (#86). 키워드로만 받는다.
    internal_request: InternalRequest | None = field(default=None, kw_only=True)
    node_id: str  # 확정한 물리 대상. 재검사와 실행이 바꾸지 않는다.
    use_id: str | None  # 공용형이면 None
    usage_id: str | None  # 승인이 필요 없으면 None
    settings: Settings  # 판정에 적용한 설정. 실행과 대여시간 계산도 이 값을 쓴다.
    execution_kind: ExecutionKind = ExecutionKind.RENTAL_INITIAL
    # ReserveResult.reserved_at 을 그대로 담는다 (#80). 예약 대기는 이 시각부터 잰다.
    # 인계 시각부터 다시 세지 않는다. DB 쓰기가 없는 공용형 open 요청은 None 이다.
    reserved_at: datetime | None = None
    # 판정이 정한 조명 설정. 조명 제어 정보를 발표한 기기의 `on` 에만 있고 두 값을 모두 담는다 (FR-09).
    light: LightSetting | None = None

    def __post_init__(self) -> None:
        internal = self.execution_kind == ExecutionKind.SERVER_INTERNAL
        no_session = self.execution_kind in (ExecutionKind.SHARED, ExecutionKind.SERVER_INTERNAL)
        if no_session != (self.use_id is None):
            raise ValueError("공용형은 use_id 가 없고 대여형은 use_id 가 있어야 한다")
        if internal != (self.request is None) or internal != (self.internal_request is not None):
            raise ValueError("서버 내부 요청만 방문자 요청 없이 internal_request 를 담는다")
        if self.internal_request is not None and (
            self.usage_id is not None
            or self.internal_request.internal_request_id != self.internal_request_id
            or self.internal_request.node_id != self.node_id
        ):
            raise ValueError("서버 내부 요청의 연결이 다르다")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """상태 관리 → 판정 실행 결과 (#16). 사용자 ID·점유 주체 필드를 두지 않는다."""

    internal_request_id: str
    status: ExecutionStatus
    command_id: str | None = None  # 명령을 연결하기 전에 막혔으면 None
    use_id: str | None = None  # 호스트에는 SUCCEEDED 일 때만 반환한다
    recheck: Decision | None = None  # 재검사가 전송을 막았을 때 그 판정
    # 재검사 밖의 이유로 실행을 막았을 때의 사유 (#79). recheck 와 함께 쓰지 않는다.
    # 판정은 recheck 가 있으면 그 판정을, 없으면 이 사유로 DENY 사유를 정한다.
    # 값: COMMAND_IN_PROGRESS(구동 경합), DEVICE_BUSY(CLOSING·FAULT 상태 충돌),
    # TARGET_UNAVAILABLE(복원 미완료), NODE_NOT_CONNECTED, RESERVATION_EXPIRED, COMMAND_NOT_SENT
    reason: ReasonCode | None = None

    def __post_init__(self) -> None:
        if self.reason is not None:
            if self.recheck is not None:
                raise ValueError("중단 사유는 재검사 판정이 없을 때만 쓴다")
            if self.status == ExecutionStatus.SUCCEEDED:
                raise ValueError("성공한 실행에는 중단 사유가 없다")


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
    light: LightSetting | None = None  # 명령에 담을 조명 설정. 재전송과 재시작 뒤에도 같다 (FR-09)

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
    반납 완료는 논리적 점유 해제다. 문 닫힘과 잠금의 증명이 아니다.
    """

    use_id: str
    closed_at: datetime
    end_reason: EndReason


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
    light_controls: LightControls | None = None  # 조명 기기만 담는다. 신뢰 상태를 바꾸지 않는다 (FR-04)


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """기기 상태 보고. 펌웨어의 명령 처리 상태만 담는다 (FR-07, MQTT v2).

    실제 상태와 센서 고장 값은 담지 않는다. 반납 종료의 근거가 아니다.
    """

    node_id: str
    received_at: datetime
    session_id: str
    seq: int
    sent_at: datetime
    observed_at: datetime  # 보고 기준 시각
    command_state: CommandState
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
    light: LightSetting | None = None  # `cmd.parameters`. 서버가 정한 밝기와 색을 모두 담는다 (FR-09)


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



# ── 감사 (FR-22) ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """감사 테이블의 원본 사건. 호출자는 UUID 를 만들고 DB 는 event_id 중복을 막는다.

    ExecutionStore 내부 사건의 ID 는 DB 가 만든다. JSONL 은 커밋 뒤의 사본이다.
    """

    event_id: str
    step: Step
    payload: Mapping[str, Any]  # 입력·결과·버전·임계값·후보 확률
    internal_request_id: str | None = None  # 인증 실패 등은 None
    command_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """`RetentionStore.cleanup()` 결과 (#164). 사용자 ID 필드를 두지 않는다."""

    cleared_requests: int  # 외부 request_id를 비운 요청 수
    deleted_sessions: int  # 지운 종료 이력(점유 세션) 수
    deleted_audit_rows: int  # 지운 상세 감사 행 수


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
    command_idle: bool | None = None  # 현재 연결의 status 가 명령 처리 대기(idle)를 보고했다


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


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    requester_user_id: str
    node_id: str
    action: Action
    policy_id: str
    policy_version: int
    internal_request_id: str
    created_at: datetime
    settings: Settings


@dataclass(frozen=True, slots=True)
class ApprovalRequestResult:
    approval_id: str
    status: ApprovalStatus
    created_at: datetime
    pending_until: datetime
    is_new: bool


@dataclass(frozen=True, slots=True)
class ApprovalDecisionRequest:
    approval_id: str
    decided_by_user_id: str
    decision: ApprovalStatus
    decided_at: datetime

    def __post_init__(self) -> None:
        if self.decision not in (ApprovalStatus.APPROVED, ApprovalStatus.DENIED):
            raise ValueError("소유자 결정은 approved 또는 denied 다")


@dataclass(frozen=True, slots=True)
class ApprovalDecisionResult:
    """applied 는 이번 호출의 갱신 여부다. 같은 결정의 재호출도 성공이다 (FR-03)."""

    applied: bool
    reason: ApprovalDecisionReason
    status: ApprovalStatus | None
    valid_until: datetime | None

    @property
    def succeeded(self) -> bool:
        """새 결정 반영과 같은 결정의 재호출을 성공으로 구분한다."""
        return self.reason in (ApprovalDecisionReason.OK, ApprovalDecisionReason.ALREADY_APPLIED)


@dataclass(frozen=True, slots=True)
class OwnerApproval:
    """승인 권한자에게 보이는 승인 한 건 (#186, FR-03). 요청자 정보를 담지 않는다 (NFR-08)."""

    approval_id: str
    node_id: str
    action: Action
    status: ApprovalStatus
    created_at: datetime
    pending_until: datetime | None  # 대기 신청 만료 시각
    valid_until: datetime | None  # 승인 전이면 None


@dataclass(frozen=True, slots=True)
class OwnerApprovalResult:
    """`ApprovalService.get`·`decide` 의 결과 (#186). 권한이 없거나 승인이 없으면 approval 은 None 이다."""

    reason: ApprovalDecisionReason
    approval: OwnerApproval | None

    @property
    def succeeded(self) -> bool:
        return self.reason in (ApprovalDecisionReason.OK, ApprovalDecisionReason.ALREADY_APPLIED)


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """판정에 적용한 값의 불변 스냅샷 (FR-22). 근접 전에 끝났으면 버전·구간은 None 이다."""

    settings: Settings
    model_version: str | None = None
    calib_version: str | None = None
    policy_version: int | None = None
    posteriors: Mapping[str, float] = field(default_factory=dict)
    window_start: datetime | None = None
    window_end: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "posteriors", _freeze(self.posteriors))


@dataclass(frozen=True, slots=True)
class AccountRegistration:
    user_id: str
    name: str
    role: Role
    token_hash: str


@dataclass(frozen=True, slots=True)
class BeaconRegistration:
    beacon_id: str
    user_id: str
    beacon: BeaconKey
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class PlaceAssignment:
    place_id: str
    owner_user_id: str
    name: str
    node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_ids", _freeze(self.node_ids))


@dataclass(frozen=True, slots=True)
class AdminWriteResult:
    outcome: AdminWriteOutcome
    reason: AdminWriteReason


@dataclass(frozen=True, slots=True)
class AccountIssue:
    """`AdminService.register_account` 결과 (FR-01, #29). 토큰 원문은 발급한 이 호출에서만 받는다.

    로그나 오류 출력에 원문이 실리지 않게 token 은 repr 에서 뺀다.
    """

    result: AdminWriteResult
    token: str | None = field(repr=False)  # APPLIED 일 때만 토큰 원문. 그 밖에는 None


@dataclass(frozen=True, slots=True)
class PolicySettings:
    """`AdminService.set_policy` 입력 (FR-14). 정책 ID 와 버전은 서비스가 정한다."""

    node_id: str
    visibility: Visibility
    mode: AccessMode
    approver_user_id: str | None
    approval_mode: ApprovalMode
    approval_ttl_sec: int | None
    max_uses: int | None  # 기간형 무제한이면 None


@dataclass(frozen=True, slots=True)
class PolicyWriteResult:
    """`AdminService.set_policy` 결과 (FR-14). 내용이 같아 쓰지 않은 호출은 APPLIED 와 changed=False 다."""

    result: AdminWriteResult
    version: int | None  # 호출 뒤 현재 정책 버전. 정책이 없으면 None
    changed: bool  # 새 리비전을 추가했으면 True


# ── 대시보드 모니터링 (FR-12A, FR-12B, 2026-10-23 팀 결정) ─────────────


@dataclass(frozen=True, slots=True)
class Subject:
    """이벤트·조회 행의 사람. 사용자 ID 는 API 밖으로 나가지 않는다 (FR-11). repr 에도 쓰지 않는다 (NFR-08)."""

    user_id: str = field(repr=False)
    role: Role


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    """발행 영역이 넘기는 모니터링 이벤트 하나. fields 의 키는 architecture.md §5 WebSocket 형식 표의 camelCase 키다."""

    type: str
    at: datetime
    fields: Mapping[str, Any]
    subject: Subject | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", _freeze(self.fields))


# ── 대시보드 읽기 행 (FR-12A, FR-12B, specs/core-dashboard-queries) ─────
# `DashboardReadStore` 가 돌려주는 DB 행이다. 시각은 시간대가 있는 datetime 이고, seq 는 그 표의 rowid 다.
# 분류와 계산은 CORE 조회 서비스가 한다 — 여기에는 저장된 값을 그대로 담는다.


@dataclass(frozen=True, slots=True)
class DashboardPlace:
    place_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DashboardNode:
    node_id: str
    name: str | None  # node.name. 없으면 None
    place_id: str | None
    capability: Capability
    occupancy: Occupancy
    trust_state: TrustState
    device_state: DeviceState
    last_seen_at: datetime | None
    session_id: str | None


@dataclass(frozen=True, slots=True)
class DashboardSession:
    """끝나지 않은 use_session (RESERVED, ACTIVE, CLOSING)."""

    use_id: str
    internal_request_id: str
    user_id: str
    node_id: str
    state: SessionState
    reserved_at: datetime | None
    rental_due_at: datetime | None
    received_at: datetime  # 연결된 request.received_at


@dataclass(frozen=True, slots=True)
class DashboardApproval:
    approval_id: str
    node_id: str
    requester_user_id: str
    internal_request_id: str
    action: Action
    status: ApprovalStatus
    created_at: datetime
    pending_until: datetime | None
    valid_until: datetime | None
    mode: ApprovalMode  # policy_revision.approval_mode (approval 의 policy_id, policy_version)
    used_count: int  # approval.used_count
    remaining_uses: int | None  # Repository 의 남은 횟수와 같은 규칙. max_uses 가 없으면 None


@dataclass(frozen=True, slots=True)
class DashboardRequest:
    seq: int  # request.rowid
    internal_request_id: str
    user_id: str | None  # 서버 내부 요청은 None
    kind: str  # request.request_kind ('visitor' | 'server_internal')
    target_spec: str
    action: Action
    verdict: Verdict | None
    reason_code: ReasonCode | None
    node_id: str | None
    decision_context: Mapping[str, Any] | None  # 없으면 None
    received_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_context", _freeze(self.decision_context))


@dataclass(frozen=True, slots=True)
class DashboardCommand:
    seq: int  # node_command.rowid
    command_id: str
    internal_request_id: str
    node_id: str
    action: Action
    status: ExecutionStatus
    use_id: str | None
    send_started_at: datetime | None
    outcome: str | None  # CommandOutcome 값 문자열 그대로
    occurred_at: datetime | None
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze(self.evidence))


@dataclass(frozen=True, slots=True)
class DashboardAudit:
    seq: int  # audit_log.rowid
    log_id: str
    internal_request_id: str | None
    command_id: str | None
    step: Step
    payload: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True, slots=True)
class DashboardRssi:
    seq: int  # rssi_observation.rowid
    node_id: str
    user_id: str  # beacon.user_id
    rssi: float
    received_at: datetime


@dataclass(frozen=True, slots=True)
class DashboardBeacon:
    """활성 비콘 (is_active = 1)."""

    user_id: str
    beacon_id: str
    last_seen_at: datetime | None  # 그 비콘의 rssi_observation.received_at 최댓값. 관측이 없으면 None
