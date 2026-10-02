"""계층 사이에서 주고받는 값 객체 (architecture.md §4 도메인 모델).

모두 불변(frozen)이다. Mapping 필드는 복사하고 내부 JSON 컨테이너도 불변으로 만든다.
DB 행을 그대로 옮긴 것이 아니라 계층 간에 필요한 필드만 담는다.
DB 스키마의 전체 필드는 architecture.md §9 와 db 계층이 정의한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nearpilot.shared.enums import (
    AccessMode,
    Action,
    ApprovalMode,
    ApprovalStatus,
    Capability,
    CommandOutcome,
    DeviceState,
    EndReason,
    Occupancy,
    ReasonCode,
    ReserveOutcome,
    Role,
    SessionState,
    Step,
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
    """노드 하나가 약 1초 동안 본 비콘 하나의 RSSI."""

    node_id: str
    beacon: BeaconKey
    rssi: float  # dBm
    frame_ts: datetime


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
    """`reserve_if_free` 입력. ⑤⑥ 검사·RESERVED 생성·승인 사용분 예약을 한 트랜잭션에서."""

    internal_request_id: str
    user_id: str
    node_id: str
    action: Action
    approval_id: str | None  # 승인이 필요 없으면 None
    expires_at: datetime | None
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
    use_id: str
    internal_request_id: str
    node_id: str
    state: SessionState
    expires_at: datetime | None
    end_reason: EndReason | None = None


# ── 노드 이벤트·명령 (IOT ↔ CORE) ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class NodeAnnounce:
    node_id: str
    capability: Capability
    occupancy: Occupancy
    received_at: datetime


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """하트비트·안전 상태. [결정 필요] 장치별 안전 센서 조합 (PRD §7)."""

    node_id: str
    received_at: datetime
    safe: bool | None  # None = 안전 미확인
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _freeze(self.detail))


@dataclass(frozen=True, slots=True)
class NodeCommand:
    command_id: str
    node_id: str
    action: Action


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    outcome: CommandOutcome
    safe: bool | None = None
    reported_at: datetime | None = None


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
