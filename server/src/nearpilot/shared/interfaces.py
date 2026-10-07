"""계층 간 인터페이스 (architecture.md §4).

각 계층은 상대 계층의 구현이 아니라 이 Protocol 에만 의존한다 (의존 규칙 4).
그래서 상대가 아직 없어도 가짜 구현(fake)으로 개발·테스트할 수 있다.

모든 함수는 동기 함수다. SQLite 와 paho-mqtt(스레드)가 동기 방식이고, FastAPI 는 `def`
엔드포인트를 스레드풀에서 실행하므로 이벤트 루프를 막지 않는다. 동시 요청의 이중 점유는
`Repository.reserve_if_free()` 의 `BEGIN IMMEDIATE` 트랜잭션이 막는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from nearpilot.shared.enums import (
    Action,
    ApprovalUsageState,
    Capability,
    DeviceState,
    EndReason,
    SessionState,
)
from nearpilot.shared.models import (
    Account,
    Approval,
    AuditEvent,
    BeaconKey,
    CommandResult,
    Decision,
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

    # 상태 전이 — 현재 상태가 expected 일 때만 바꾸고 성공 여부를 돌려준다 (조건부 갱신)
    def transition_session(
        self,
        use_id: str,
        expected: SessionState,
        new: SessionState,
        end_reason: EndReason | None = None,
    ) -> bool: ...

    def transition_device(self, node_id: str, expected: DeviceState, new: DeviceState) -> bool: ...

    def settle_approval_usage(
        self, usage_id: str, expected: ApprovalUsageState, new: ApprovalUsageState
    ) -> bool: ...

    def session(self, use_id: str) -> UseSession | None: ...

    def session_owner_matches(self, use_id: str, user_id: str) -> bool: ...  # FR-21

    # 명령 기록 — 재시작 후에도 보존
    def create_command(self, internal_request_id: str, node_id: str, action: Action) -> NodeCommand: ...

    def record_command_result(self, result: CommandResult) -> None: ...


@runtime_checkable
class AuditLog(Protocol):
    """DB → CORE 감사 로그 (FR-22). 테이블 + JSONL."""

    def append(self, event: AuditEvent) -> None: ...


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
