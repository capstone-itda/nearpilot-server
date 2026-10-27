"""계층 간 인터페이스 (architecture.md §4).

각 계층은 상대 계층의 구현이 아니라 이 Protocol 에만 의존한다 (의존 규칙 4).
그래서 상대가 아직 없어도 가짜 구현(fake)으로 개발·테스트할 수 있다.

모든 함수는 동기 함수다. SQLite 와 paho-mqtt(스레드)가 동기 방식이고, FastAPI 는 `def`
엔드포인트를 스레드풀에서 실행하므로 이벤트 루프를 막지 않는다. 동시 요청의 이중 점유는
`Repository.reserve_if_free()` 의 `BEGIN IMMEDIATE` 트랜잭션이 막는다.
예약 이후의 상태 변경은 `ExecutionStore` 의 사건별 함수 하나가 트랜잭션 하나다 (#17).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from nearpilot.shared.enums import (
    Action,
    ApprovalStatus,
    Capability,
    CommandOutcome,
    EndReason,
    ExecutionStatus,
    ReasonCode,
    Role,
    TrustState,
    Verdict,
)
from nearpilot.shared.models import (
    RecoveryRecord,
    RecoverySettlement,
    RestoreBundle,
    RecoveryReadiness,
    RecoveryApprovalResult,
    ApprovalRequest,
    ApprovalRequestResult,
    ApprovalDecisionRequest,
    ApprovalDecisionResult,
    OwnerApproval,
    OwnerApprovalResult,
    DecisionRecord,
    AccountRegistration,
    BeaconRegistration,
    PlaceAssignment,
    AdminWriteResult,
    AccountIssue,
    PolicySettings,
    PolicyWriteResult,
    Account,
    Approval,
    ApprovalRecord,
    AuditEvent,
    BeaconKey,
    CleanupReport,
    CloseResult,
    CommandRecord,
    CommandResult,
    Decision,
    ExecutionContext,
    ExecutionResult,
    InternalRequest,
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
    StoreResult,
    UseRequest,
    UseSession,
    MonitorEvent,
    DashboardApproval,
    DashboardAudit,
    DashboardBeacon,
    DashboardCommand,
    DashboardNode,
    DashboardPlace,
    DashboardRequest,
    DashboardRssi,
    DashboardSession,
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

    `internal_request_id` 가 있으면 남은 횟수에서 그 요청의 승인 사용분을 뺀다. 자기 사용분은 이미
    확보한 몫이라 새 사용으로 세지 않는다. 같은 요청의 재판정(#76)과 실행 직전 재검사가 넘긴다.
    없으면 모든 사용분을 센다.
    """

    def check(
        self, user_id: str, node_id: str, action: Action, now: datetime,
        *, internal_request_id: str | None = None,
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

    def save_decision(self, decision: Decision, record: DecisionRecord) -> StoreResult:
        """판정과 적용값을 internal_request_id 별로 한 번 저장한다 (FR-22).

        같은 판정·기록은 ALREADY_APPLIED, 다른 내용은 CONFLICT·CONTENT_CONFLICT다.
        외부 request_id 보존기간과 관계없이 기존 기록을 보존한다. 저장 장애는 StoreError 로 알린다.
        """
        ...

    # ① 비콘
    def beacons_of(self, user_id: str) -> Sequence[BeaconKey]:
        """본 계정의 활성 비콘만 반환한다. 개수는 0 또는 1 이다 (FR-01, FR-15, #42).

        비활성 비콘의 등록 이력은 반환하지 않는다. 활성 비콘이 없으면 빈 시퀀스다.
        여러 활성 비콘을 발견하면 저장 불변 조건 위반이다. 임의로 하나를 골라 반환하지 않는다.
        """
        ...

    def save_rssi(self, frame: RssiFrame) -> None:
        """등록된 활성 비콘의 프레임만 저장한다. 그 밖의 비콘은 저장하지 않는다 (FR-05).

        `(node_id, session_id, seq, beacon)` 이 같은 프레임은 한 번만 저장한다.
        수집 회차와 라벨은 tools/data_collection 이 기록한다 (#41). IOT 입력에는 담지 않는다.
        DB 는 비콘과 frame_ts 로 수집 구간을 찾아 저장한 관측에 collection_id 를 연결한다.
        구간은 시작 이상·끝 미만이다. 운영 관측에는 collection_id 가 없다.
        수집 회차에 연결된 관측은 운영 관측 보존기간으로 삭제하지 않는다.
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
        self, user_id: str, node_id: str, action: Action, policy_version: int, now: datetime,
        *, internal_request_id: str | None = None,
    ) -> Approval | None:
        """시간상 유효한 승인 하나를 반환한다. 없으면 `None` 이다 (FR-14).

        `Approval.remaining_uses` 는 허용 횟수에서 reserved·held·consumed 사용분을 뺀 값이다(무제한이면
        `None`). `internal_request_id` 가 있으면 그 요청의 사용분은 세지 않는다 (#76).
        `approvals_in_scope()` 와 `reserve_if_free()` 의 한도 검사는 바꾸지 않는다.
        """
        ...

    def approvals_in_scope(self, user_id: str, node_id: str) -> Sequence[ApprovalRecord]:
        """요청자·기기의 승인 이력을 모두 반환한다 (#60, FR-03, FR-14).

        시각·동작·정책 버전으로 거르지 않는다. 사유 선택은 core/policy 가 한다.
        조회만 한다. 저장소를 바꾸지 않고 승인 사용분을 예약하지 않는다.
        """
        ...

    def create_approval_request(self, req: ApprovalRequest) -> ApprovalRequestResult:
        """요청자·기기·동작·정책 버전이 같고 유효한 pending 이 있으면 기존 신청을 반환한다 (FR-03).

        새 신청의 pending_until 은 created_at + settings.approval_pending_ttl_sec 다.
        중복 호출은 시각을 연장하지 않는다. 만료한 신청은 새 신청을 막지 않는다.
        """
        ...

    def decide_approval(self, req: ApprovalDecisionRequest) -> ApprovalDecisionResult:
        """pending·현재 정책 버전·대기 만료를 조건부로 검사하고 결정 하나만 반영한다 (FR-03).

        승인 권한은 CORE 가 검사한다. approved 의 valid_until 은 decided_at 에 생성 당시
        정책 리비전의 approval_ttl_sec 를 더한다. 이미 결정·버전 불일치·대기 만료를 구분한다.
        권한 검사 뒤 같은 승인 ID·결정자·결정의 재호출은 ALREADY_APPLIED 다.
        호출 시각은 비교하지 않는다. applied 는 False, succeeded 는 True 다.
        기존 상태와 valid_until 을 반환한다. 결정·유효시간·감사를 다시 쓰지 않는다.
        반대 결정 또는 다른 결정자의 호출은 ALREADY_DECIDED 다. 기존 기록을 보존한다.
        """
        ...

    # 승인 권한자의 조회 (#186) — 저장소를 바꾸지 않는다
    def pending_approvals_for(self, user_id: str, now: datetime) -> Sequence[OwnerApproval]:
        """user_id 가 승인 권한자인 pending 승인 가운데 대기 만료 전인 것을 반환한다 (FR-03).

        승인 권한자는 승인이 만들어진 정책 리비전의 approver_user_id 다. 생성 시각, 승인 ID 순이다.
        """
        ...

    def approval(self, approval_id: str) -> OwnerApproval | None:
        """승인 한 건. 없으면 None 이다. 권한은 검사하지 않는다 (#186)."""
        ...

    def is_approver(self, user_id: str, approval_id: str) -> bool:
        """승인이 만들어진 정책 리비전의 approver_user_id 가 user_id 인지 반환한다 (FR-14, #186).

        승인이 없거나 리비전에 승인 권한자가 없으면 False 다. 권한자 ID 는 반환하지 않는다.
        """
        ...

    # ⑤⑥ — BEGIN IMMEDIATE 한 트랜잭션 (FR-19)
    def reserve_if_free(self, req: ReserveRequest) -> ReserveResult:
        """⑤⑥ 검사와 승인 사용분 예약은 BEGIN IMMEDIATE 하나에서 한다 (FR-19).

        본인 ACTIVE 점유이면 요청·계정·기기 연결을 검사하고 REOPERATING 과 기존 use_id 를 반환한다.
        공용형은 ⑤⑥·세션 생성을 생략하고 승인 사용분만 예약한다. open·승인 없음은 쓰기 없이 RESERVED 다.
        무제한 기간형 승인도 요청별 사용분을 만든다. 횟수 검사만 생략한다.
        이번 요청의 사용분만 예약한다. 새 세션을 만들거나 rental_due_at 을 바꾸지 않는다.
        타인 점유 또는 RESERVED·CLOSING·FAULT 는 DEVICE_BUSY 다. 같은 요청의 중복은 기존 연결을 반환한다.
        """
        ...

    # 조회 — 예약 이후의 쓰기는 ExecutionStore 로만 한다
    def session(self, use_id: str) -> UseSession | None: ...

    def session_owner_matches(self, use_id: str, user_id: str) -> bool: ...  # FR-21

    def is_place_owner(self, user_id: str, node_id: str) -> bool:
        """기기가 속한 장소의 소유 계정이 user_id 인지 반환한다 (FR-12A, #84).

        복구 준비 조회와 복구 승인마다 CORE 가 부른다. 소유 계정 ID 는 반환하지 않는다.
        기기가 없거나 장소에 배치되지 않았으면 False 다. 조회 오류는 StoreError 로 알린다.
        """
        ...


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

    def link_internal_request(self, req: InternalRequest) -> StoreResult:
        """서버 내부 요청을 사용자 ID 와 판정 기록 없이 저장한다 (#86). 키: internal_request_id

        같은 내용은 ALREADY_APPLIED, 다른 내용은 CONFLICT 다. 적용 settings 를 함께 보존한다.
        이후 link_command·begin_send·record_result·cancel_not_sent 를 그대로 쓴다.
        세션과 사용분이 없으므로 명령, 기기 상태와 감사만 반영한다.
        방문자의 use_id 조회(FR-21)와 호스트 응답에 나타나지 않는다.
        """
        ...

    def begin_send(self, command_id: str) -> StoreResult:
        """PENDING → IN_PROGRESS. APPLIED 일 때만 전송을 시작한다. 키: command_id

        같은 기기의 다른 미완료 구동 명령, 기기·세션 상태와 요청 연결을 원자적으로 검사한다.
        다른 명령과의 경합은 CONFLICT·COMMAND_IN_PROGRESS, CLOSING·FAULT 는 CONFLICT·STATE_CONFLICT 다.
        같은 명령의 중복 시작은 ALREADY_APPLIED 이며 재전송 허가가 아니다. 취소된 명령은 CONFLICT 다.
        """
        ...

    def cancel_not_sent(
        self,
        internal_request_id: str,
        expected: ExecutionStatus,
        reason: ReasonCode,
        evidence: Mapping[str, Any],
    ) -> StoreResult:
        """확실한 미전송 취소. 현재 상태가 expected 가 아니거나 결과가 있으면 CONFLICT.

        최초 대여: 기기 AVAILABLE, 세션 CLOSED·cancelled. 재조작: 기존 점유·rental_due_at 보존.
        공통: 이번 요청의 사용분 released, 명령 NOT_SENT, 감사. 기존 대여의 사용분은 바꾸지 않는다.
        재검사 실패와 예약 대기 제한은 expected=PENDING, IOT 의 NOT_SENT 증명은 expected=IN_PROGRESS 다.
        명령을 연결하지 않은 요청도 expected=PENDING 으로 취소한다 (#81). 예약과 사용분만 정리하고 명령을 만들지 않는다.
        reason: 재검사 거부는 재검사의 사유, 검사 오류는 RECHECK_FAILED, 예약 대기 제한은 RESERVATION_EXPIRED,
        현재 연결 없음은 NODE_NOT_CONNECTED, IOT 미전송 증명은 COMMAND_NOT_SENT 다 (#78).
        예약 뒤 판정 오류는 INTERNAL_ERROR 다 (#80).
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
        """실행 결과를 명령 하나에 한 번만 확정한다. outcome 은 SUCCEEDED·FAILED·UNKNOWN·REJECTED 다. NOT_SENT 는 cancel_not_sent 로 반영한다.

        SUCCEEDED: 최초 대여는 기기·세션 ACTIVE 와 rental_due_at. 재조작은 기존 점유·rental_due_at 보존.
        공통으로 이번 요청의 사용분 consumed 와 감사를 반영한다.
        REJECTED: 검증된 DEVICE_BUSY 의 미접수·미실행 근거와 상충 여부를 검사한다.
        이번 요청의 사용분만 released 로 둔다. 최초 대여는 이번 예약을 cancelled 로 종료한다.
        재조작은 기존 점유·rental_due_at 을 보존한다. 기기를 FAULT 로 두지 않는다.
        FAILED·UNKNOWN: 명령 결과, 기기 FAULT, 사용분 held, 세션 보존, 감사.
        이미 다른 결과가 있으면 CONFLICT 다. 늦은 성공으로 FAULT 를 해제하지 않는다.
        """
        ...

    def isolate_device(self, node_id: str, evidence: Mapping[str, Any]) -> StoreResult:
        """명령 결과가 아닌 이상으로 기기를 FAULT 로 격리한다 (NFR-04). 키: node_id

        발표 내용 변경 등이 해당한다. 점유 세션과 승인 사용분은 바꾸지 않는다.
        이미 FAULT 이면 ALREADY_APPLIED 를 반환하고 새 근거를 감사 기록에 남긴다.
        """
        ...

    def begin_close(self, use_id: str, reason: EndReason) -> StoreResult:
        """대여형 기기·세션 ACTIVE → CLOSING. 키: use_id

        다른 미완료 구동 명령이 있으면 CONFLICT·COMMAND_IN_PROGRESS 로 반납 시작을 보류한다.
        전송 시작과 반납 시작의 검사·쓰기를 원자적으로 한다. CLOSING 뒤에는 새 전송을 막는다.
        """
        ...

    def finish_close(self, use_id: str, closed_at: datetime, end_reason: EndReason) -> StoreResult:
        """CORE 가 논리적 종료를 결정한 뒤 세션 CLOSED, 기기 AVAILABLE, closed_at, 종료 기록. 키: use_id

        기기와 세션이 CLOSING 이고 종료 사유가 begin_close 의 값과 같은지 같은 트랜잭션에서 검사한다.
        같은 use_id·같은 종료 사유의 재호출은 ALREADY_APPLIED 와 기존 종료 시각, 다른 내용은 CONFLICT 다.
        검사 ID 와 센서 근거를 받지 않는다. 반납 완료는 문 닫힘과 잠금의 증명이 아니다 (FR-20).
        """
        ...

    def close_result(self, use_id: str) -> CloseResult | None:
        """반납의 저장된 완료 기록을 조회한다 (FR-20). 키: use_id

        finish_close 의 반영 여부가 불명일 때 쓴다. CLOSED 가 아니면 None 이다.
        None 은 미반영의 증거가 아니다. 저장이 진행 중일 수 있다.
        """
        ...

    def execution(self, internal_request_id: str) -> CommandRecord | None:
        """반영 여부가 불명일 때 다시 조회한다."""
        ...

    def last_succeeded(self, node_id: str) -> CommandRecord | None:
        """기기에서 가장 늦게 전송을 시작한 `succeeded` 명령. 키: node_id

        공용형의 마지막 제어 상태(`on`·`off`)를 판단한다. 실제 켜짐의 관측이 아니다 (FR-20).
        성공 명령이 없으면 None 이다.
        """
        ...

    def execution_context(self, command_id: str) -> ExecutionContext | None:
        """명령 ID 로 판정이 인계한 원래 실행 문맥을 조회한다 (#83). 키: command_id

        원래 UseRequest, 확정 node_id, use_id·usage_id, execution_kind, reserved_at 과
        save_decision 이 저장한 적용 Settings 의 전체 스냅샷으로 만든다. 현재 설정으로 채우지 않는다.
        재시작 뒤 늦은 결과의 정산에 쓴다. 명령이 없으면 None 이다. 조회 오류는 StoreError 로 알린다.
        """
        ...


    def restore_pending(self) -> Sequence[RestoreBundle]:
        """미완료 요청·명령·세션·사용분·복구 건과 FAULT 기기를 일관된 읽기 시점에 조회한다.

        공용형의 사용분도 반환한다. 조회 오류를 빈 목록으로 바꾸지 않고 저장한 시각을 보존한다.
        복원·현재 상태 확인 전에는 새 실행과 재사용을 허가하지 않는다.
        """
        ...

    def save_recovery(self, record: RecoveryRecord) -> StoreResult:
        """복구 건과 근거를 저장한다. 대상 연결은 바꾸지 않고 완료 근거는 덮어쓰지 않는다.

        같은 내용은 ALREADY_APPLIED, 다른 대상 연결은 CONFLICT 다. 점유·사용분·기기 상태는 풀지 않는다.
        """
        ...

    def recovery(self, recovery_id: str) -> RecoveryRecord | None:
        """복구 건과 완료 결과를 조회한다. None 만으로 미반영을 단정하지 않는다."""
        ...

    def settle_recovery(self, settlement: RecoverySettlement) -> StoreResult:
        """연결·예상 상태를 검사하고 승인·근거·사용분·점유 종료·기기 복구·감사를 함께 커밋한다.

        finish_close 를 거치지 않는다. 복구 근거로 점유를 종료한다.
        같은 recovery_id·논리 정산이면 ALREADY_APPLIED 와 기존 완료 결과를 반환한다.
        다른 연결·요약값·예상 상태는 CONFLICT 다. 늦은 결과로 완료 정산을 뒤집지 않는다.
        """
        ...


@runtime_checkable
class AuditLog(Protocol):
    """DB → CORE 감사 로그 (FR-22). 테이블이 원본이고 JSONL 은 커밋 뒤 사본이다.

    호출자가 UUID event_id 를 만든다. DB 는 같은 ID 를 한 번만 저장한다.
    JSONL 실패는 저장 오류가 아니다. 커밋 순서로 내보내고 원본 테이블에서 다시 만들 수 있다.
    평가 스크립트는 테이블을 읽는다.
    """

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


@runtime_checkable
class ExecutionReadiness(Protocol):
    """상태 관리 제공, 판정 사용. 기기별 새 실행 가능 여부 (#82).

    재시작 복원을 마치고 현재 연결의 정상 보고를 확인한 기기만 True 다.
    최초 복원 조회가 실패했거나 현재 연결을 확인하지 못했으면 False 다.
    판정은 ②에서 False 인 기기를 후보에서 뺀다. 상태 관리는 전송 직전에도 다시 검사한다.
    """

    def can_execute(self, node_id: str) -> bool: ...


# ── CORE 내부 (decision ↔ proximity) ────────────────────────────────


@runtime_checkable
class ProximityEstimator(Protocol):
    """관측 모델 + HMM (FR-16). 교체 가능한 인터페이스로 분리한다 (requirements.md §1.2)."""

    def observe(self, frame: RssiFrame, *, node: NodeInfo) -> None:
        """호출자가 조회한 관측 기기와 RSSI 프레임으로 HMM 필터를 갱신한다 (FR-16).

        node_id 가 다르거나 node.trust_state 가 TRUSTED 가 아니면 프레임을 쓰지 않는다.
        프레임의 최신성은 FR-05 계약을 따른다. DB 관측 저장과 근접 추정 사용 여부는 별개다.
        """
        ...

    def posterior(
        self, beacons: Sequence[BeaconKey], candidates: Sequence[NodeInfo], at: datetime,
        *, place_nodes: Sequence[NodeInfo],
    ) -> ProximityResult:
        """장소 전체 상태로 계산한 후보별 사후확률. 후보가 없으면 빈 posteriors.

        판정이 Repository.nodes() 로 읽은 해당 장소의 전체 기기를 place_nodes 로 넘긴다.
        TRUSTED 기기는 FAULT·오프라인·다른 기능이어도 전체 상태에 넣고 '어느 기기 앞도 아님'을 더한다.
        후보 확률을 다시 정규화하지 않는다. 후보 밖 확률을 후보에 더하지 않는다. 합은 1 이하다.
        후보와 전체 기기는 같은 장소의 조회다. 관계가 맞지 않으면 실행 허가에 쓸 결과를 만들지 않는다.
        beacons 는 Repository.beacons_of() 가 반환한 본 계정의 활성 비콘 하나다 (#42).
        여러 비콘의 확률을 합치거나 최댓값으로 고르지 않는다. 인자 타입은 Sequence 를 유지한다.

        판정은 명시 지정 요청에도 ② 의 전체 후보를 넘긴다 (#26). 지정 대상은 인자로 받지 않는다.
        같은 입력에는 지정 여부와 상관없이 같은 출력을 낸다 (NFR-09).
        판정이 `posteriors[지정 대상]` 을 읽어 임계값과 비교한다.
        """
        ...


# ── IOT 제공 ────────────────────────────────────────────────────────


@runtime_checkable
class NodeCommandGateway(Protocol):
    """IOT → CORE 노드 명령 (FR-06).

    결과는 NOT_SENT / SUCCEEDED / FAILED / UNKNOWN / REJECTED 으로 구분한다.
    REJECTED 는 검증된 DEVICE_BUSY 접수·실행 전 거부다. 다른 거부는 실패·불명 규칙을 따른다.
    시간 초과·연결 끊김은 UNKNOWN 이며 미실행으로 간주하지 않는다.
    같은 command_id 로만 재전송하고 새 ID 로 자동 재시도하지 않는다.
    """

    def send(self, command: NodeCommand, timeout_sec: float) -> CommandResult: ...


# ── CORE 제공 ───────────────────────────────────────────────────────


@runtime_checkable
class NodeEventSink(Protocol):
    """CORE 가 구현하고 IOT 가 호출한다. 검증을 마친 MQTT 메시지만 넘긴다.

    점유 해제와 장애 상태 변경은 CORE 가 결정한다.
    인계가 예외로 실패하면 IOT 는 실패를 기록하고 다시 인계한다. 중복 반영은 CORE 와 DB 가 막는다.
    """

    def on_announce(self, announce: NodeAnnounce) -> None: ...

    def on_rssi(self, frame: RssiFrame) -> None: ...

    def on_status(self, status: NodeStatus) -> None: ...

    def on_result(self, result: NodeResult) -> None:
        """기기가 보고한 실행 결과. 결과 대기 중과 결과 대기가 끝난 뒤의 보고를 모두 받는다 (FR-07).

        과거 명령의 결과는 정산과 감사에만 쓴다. 현재 온라인과 명령 처리 상태를 갱신하지 않는다.
        """
        ...


@runtime_checkable
class CoreService(Protocol):
    """CORE → API 기능 (FR-09). MCP 도구 6개에 대응한다.

    user_id 는 API 가 토큰으로 식별해 넘긴다. 반환값에는 user_id·점유 주체가 없다.
    관리 기능(계정 등록·노드 신뢰·정책·승인 처리·FAULT 복구)은 여기에 넣지 않는다.
    계정·비콘 등록, 장소 배치, 기기 신뢰, 정책, 종료 정책은 `AdminService` 가 맡는다.
    """

    def find_available_devices(self, user_id: str) -> Sequence[Mapping[str, Any]]: ...

    def get_device_state(self, user_id: str, node_id: str) -> Mapping[str, Any]: ...

    def request_access(self, user_id: str, node_id: str, action: Action, request_id: str) -> Decision: ...

    def request_use(self, req: UseRequest) -> Decision: ...

    def release_use(self, user_id: str, use_id: str) -> Decision: ...

    def get_use_status(self, user_id: str, use_id: str) -> Mapping[str, Any]: ...


@runtime_checkable
class RecoveryService(Protocol):
    """상태 관리 → API 복구 접점 (NFR-04). API 는 인증 계정만 넘긴다.

    조회·승인 모두 장소 소유권을 검사한다. 조회는 복구 건을 만들거나 정산하지 않는다.
    승인은 현재 복구 건·온라인·신뢰·명령 결과와 현재 연결의 명령 처리 상태를 다시 검사한다.
    저장 미반영은 rejected, 저장 불명은 pending 이다. 완료 기록 확인 뒤에만 completed 다.
    """

    def readiness(self, user_id: str, node_id: str) -> RecoveryReadiness: ...

    def approve(self, user_id: str, node_id: str, recovery_id: str) -> RecoveryApprovalResult: ...


@runtime_checkable
class ApprovalService(Protocol):
    """CORE → API 승인 접점 (FR-03, #186). `CoreService` 와 따로 둔다.

    user_id 는 API 가 토큰으로 식별해 넘긴다. 승인 권한, 대기 상태, 정책 버전은 CORE 가 검증한다.
    반환값에는 요청자 정보가 없다 (NFR-08). API 는 결과를 바꾸지 않는다 (NFR-07).
    """

    def pending_for(self, user_id: str) -> Sequence[OwnerApproval]: ...

    def get(self, user_id: str, approval_id: str) -> OwnerApprovalResult: ...

    def decide(self, user_id: str, approval_id: str, decision: ApprovalStatus) -> OwnerApprovalResult: ...


@runtime_checkable
class AdminRepository(Protocol):
    """DB → 관리 기능·시드 스크립트·수집 도구의 쓰기 접점 (FR-01, FR-12A, FR-16).

    API 가 직접 호출하지 않는다. 인증·권한 검사는 CORE 관리 기능이 맡는다.
    각 쓰기는 감사와 함께 한 트랜잭션으로 반영한다. FAULT 복구는 ExecutionStore 로 한다.
    """

    def register_account(self, req: AccountRegistration) -> AdminWriteResult: ...

    def register_beacon(self, req: BeaconRegistration) -> AdminWriteResult:
        """다른 계정의 동일 BeaconKey 는 BEACON_ALREADY_REGISTERED 로 거부한다 (FR-01).

        계정당 활성 비콘은 하나 이하다. is_active=True 이면 같은 계정의 다른 비콘을 끈다.
        중복 검사·기존 비콘 비활성화·등록 또는 활성화·감사는 한 트랜잭션으로 반영한다.
        거부하거나 저장에 실패하면 기존 활성 비콘을 유지한다.
        is_active=False 인 등록은 같은 계정의 다른 비콘을 끄지 않는다.
        """
        ...

    def set_node_trust(self, node_id: str, trust_state: TrustState) -> AdminWriteResult:
        """신뢰 상태만 바꾼다. device_state·점유 세션은 바꾸지 않는다."""
        ...

    def assign_place(self, req: PlaceAssignment) -> AdminWriteResult:
        """장소를 등록하고 기기를 연결한다. 점유 상태는 바꾸지 않는다."""
        ...

    def set_policy(self, revision: PolicyRevision, expected_version: int | None) -> AdminWriteResult:
        """새 리비전 추가와 current_version 갱신은 원자적이다. 과거 리비전은 바꾸지 않는다.

        expected_version 은 현재 버전의 기대값이다. 정책 최초 등록이면 None 이다.
        """
        ...

    def set_release_policy(self, node_id: str, release_policy: Mapping[str, Any]) -> AdminWriteResult:
        """node.release_policy 만 바꾼다. 감사와 한 트랜잭션이다. 없는 기기는 NODE_NOT_FOUND 다.
        release_policy 가 Mapping 이 아니면 TypeError 다.

        점유 세션은 생성 당시 값을 보존하므로 소급하지 않는다.
        """
        ...

    def set_node_name(self, node_id: str, name: str) -> AdminWriteResult:
        """node.name 만 바꾼다. 감사와 한 트랜잭션이다 (set_release_policy 와 같다). 없는 기기는 NODE_NOT_FOUND 다.
        name 이 비었거나 공백뿐이면 ValueError 다.
        """
        ...

    def start_collection(
        self, collection_id: str, beacon: BeaconKey, label: str, started_at: datetime
    ) -> AdminWriteResult:
        """진행 중인 수집 회차를 만든다. ended_at 은 None 이다 (FR-16, #131).

        collection_id 는 tools/data_collection 이 만든다. label 은 기기 앞(node_id) 또는 "none" 이다.
        같은 비콘의 진행 중 회차나 끝난 회차와 구간이 겹치면 COLLECTION_OVERLAP 으로 거부한다.
        끝 시각과 다음 시작 시각이 같으면 겹치지 않는다.
        등록되지 않은 비콘은 BEACON_NOT_FOUND, 없는 node_id 라벨은 NODE_NOT_FOUND,
        이미 있는 collection_id 는 STATE_CONFLICT 로 거부한다.
        비활성 비콘은 BEACON_NOT_FOUND 로 거부한다 (#138).
        """
        ...

    def end_collection(self, collection_id: str, ended_at: datetime) -> AdminWriteResult:
        """진행 중인 수집 회차에 끝 시각을 기록한다 (FR-16, #131).

        ended_at 이 started_at 보다 뒤가 아니면 INVALID_INTERVAL 로 거부한다.
        없는 회차는 COLLECTION_NOT_FOUND, 이미 끝난 회차는 STATE_CONFLICT 로 거부한다.
        구간은 started_at <= frame_ts < ended_at 이다. 관측 연결은 save_rssi 가 한다.
        """
        ...


@runtime_checkable
class AdminService(Protocol):
    """관리 기능 (FR-01, FR-12A, FR-13, FR-14). 읽기는 `Repository`, 쓰기는 `AdminRepository` 로 한다.

    계정·비콘 등록은 행위자 검사가 없다 — 첫 소유자를 만드는 부트스트랩이다.
    장소·신뢰·정책·종료 정책은 행위자가 장소 소유자여야 한다. 아니면 아무것도 쓰지 않고
    `AdminWriteReason.NOT_PLACE_OWNER` 로 거부한다.
    """

    def register_account(self, user_id: str, name: str, role: Role) -> AccountIssue:
        """토큰을 만들어 해시로 저장하고 원문을 돌려준다. 원문은 APPLIED 일 때만 있다 (FR-01, #29)."""
        ...

    def register_beacon(self, req: BeaconRegistration) -> AdminWriteResult: ...

    def assign_place(self, actor_user_id: str, req: PlaceAssignment) -> AdminWriteResult: ...

    def set_node_trust(
        self, actor_user_id: str, node_id: str, trust_state: TrustState
    ) -> AdminWriteResult: ...

    def set_policy(self, actor_user_id: str, settings: PolicySettings) -> PolicyWriteResult:
        """현재 정책과 내용이 같으면 쓰지 않는다. 다르면 새 리비전을 추가한다 (FR-14)."""
        ...

    def set_release_policy(
        self, actor_user_id: str, node_id: str, release_policy: Mapping[str, Any]
    ) -> AdminWriteResult: ...

    def set_node_name(self, actor_user_id: str, node_id: str, name: str) -> AdminWriteResult:
        """권한 검사는 set_release_policy 와 같다 (행위자가 기기가 속한 장소의 소유자여야 한다)."""
        ...


@runtime_checkable
class RetentionStore(Protocol):
    """DB → 서버 조립(상태 관리) 보존기간 정리 접점 (FR-21, FR-22, #164).

    서버 조립이 시작할 때와 settings.retention_cleanup_interval_sec 마다 부른다.
    """

    def cleanup(self, now: datetime) -> CleanupReport:
        """보존기간이 지난 종료 이력과 상세 감사 행을 지운다.

        최종 처리 완료 뒤 request_dedup_ttl_sec 가 지난 요청의 외부 request_id 만 비운다.
        진행 중이거나 미해결인 기록은 기간만으로 지우지 않는다. 요청 행은 지우지 않는다.
        수집 회차에 연결된 관측은 지우지 않는다. 쓰기 하나는 BEGIN IMMEDIATE 하나다.
        """
        ...


# ── 대시보드 모니터링 (FR-12A, FR-12B, 2026-10-23 팀 결정) ─────────────


@runtime_checkable
class MonitorFeed(Protocol):
    """API → 대시보드 따라 읽기 (커밋된 행을 읽어 넘긴다, architecture.md §4). 모니터링 이벤트를 받는다 (FR-12B).

    자유 문자열(detail, 감사 text)과 맵 키에 사용자 ID 를 넣지 않는다 — API 는 키 단위로만 거른다 (FR-11).
    """

    def publish(self, event: MonitorEvent) -> None:
        """어느 스레드에서 불러도 막지 않고 예외를 올리지 않는다 (NFR-07)."""
        ...


@runtime_checkable
class MonitorSnapshot(Protocol):
    """CORE 조회 서비스 → API. 스트림 연결 직후 보낼 지금 상태 (FR-12B)."""

    def current_events(self) -> Sequence[MonitorEvent]:
        """지금 상태(진행 중인 요청, 기기 상태, 비콘)를 이벤트로 돌려준다."""
        ...


@runtime_checkable
class DashboardQueries(Protocol):
    """CORE 조회 서비스 → API. 대시보드 조회 REST 9개의 원천 (FR-12A).

    키는 architecture.md §5 "대시보드 조회" 형식 표다. 행의 사람은 키 "subject" 에 Subject 로 담는다. 시각은 시간대가 있는 datetime 이다.
    API 는 Subject 를 이름으로 바꾸고 형식 밖의 키를 버린다. 허용 키의 값은 그대로 나가므로, 자유 문자열과 맵 키에 사용자 ID 를 넣지 않는다 (FR-11).
    """

    def places(self) -> Mapping[str, Any]:
        """등록된 장소 전체. 다른 조회처럼 소유자별로 거르지 않는다. placeId 와 nodeId 는 목록 전체에서 한 번씩만 나온다."""
        ...

    def uses(self) -> Sequence[Mapping[str, Any]]: ...
    def approvals(self) -> Mapping[str, Any]: ...
    def audit(self, limit: int) -> Sequence[Mapping[str, Any]]: ...
    def history(self, period: str, verdict: Verdict | None, node_id: str | None) -> Sequence[Mapping[str, Any]]: ...
    def request(self, request_id: str) -> Mapping[str, Any] | None: ...
    def trust(self) -> Mapping[str, Any]: ...
    def rental(self, node_id: str) -> Mapping[str, Any] | None: ...
    def health(self) -> Mapping[str, Any]: ...


@runtime_checkable
class DashboardReadStore(Protocol):
    """DB → CORE 대시보드 조회 서비스. 읽기만 한다 (FR-12A). 목록은 아래 정렬 순서다.

    쓰지 않고 트랜잭션도 열지 않는다 — 읽기 트랜잭션은 부르는 쪽이 연다. 현재 시각을 읽지 않는다.
    분류와 계산(점유, 승인 기한, 단계 결과)은 하지 않고 저장된 값을 그대로 돌려준다.
    `seq` 는 그 표의 rowid 이고, 따라 읽기가 커서로 쓴다. 비어 있는 ID 목록은 질의하지 않고 빈 결과다.
    """

    def places(self) -> Sequence[DashboardPlace]:
        """place_id 순."""
        ...

    def nodes(self) -> Sequence[DashboardNode]:
        """node_id 순. 장소 없는 기기도 포함한다."""
        ...

    def roles(self, user_ids: Iterable[str]) -> Mapping[str, Role]:
        """없는 ID 는 빠진다."""
        ...

    def open_sessions(self) -> Sequence[DashboardSession]:
        """끝나지 않은 점유 세션(RESERVED, ACTIVE, CLOSING). node_id, reserved_at 순."""
        ...

    def approvals(self) -> Sequence[DashboardApproval]:
        """created_at, approval_id 순."""
        ...

    def requests(self, since: datetime | None, until: datetime | None) -> Sequence[DashboardRequest]:
        """received_at 이 [since, until) 안인 요청. None 이면 그 끝을 열어 둔다. received_at 내림차순."""
        ...

    def request(self, internal_request_id: str) -> DashboardRequest | None: ...

    def requests_after(self, seq: int) -> Sequence[DashboardRequest]:
        """rowid > seq, rowid 순."""
        ...

    def requests_by_ids(self, ids: Iterable[str]) -> Sequence[DashboardRequest]:
        """rowid 순."""
        ...

    def commands_of(self, internal_request_ids: Iterable[str]) -> Sequence[DashboardCommand]:
        """rowid 순."""
        ...

    def commands_of_use(self, use_id: str) -> Sequence[DashboardCommand]:
        """node_command.use_id 가 use_id 인 명령. rowid 순."""
        ...

    def commands_by_ids(self, command_ids: Iterable[str]) -> Sequence[DashboardCommand]:
        """rowid 순."""
        ...

    def audit_recent(self, limit: int) -> Sequence[DashboardAudit]:
        """rowid 내림차순으로 limit 개. 모르는 step 값이 있으면 ValueError 다."""
        ...

    def audit_after(self, seq: int) -> Sequence[DashboardAudit]:
        """rowid > seq, rowid 순. 모르는 step 값이 있으면 ValueError 다."""
        ...

    def audit_of(self, internal_request_ids: Iterable[str]) -> Sequence[DashboardAudit]:
        """rowid 순. 모르는 step 값이 있으면 ValueError 다."""
        ...

    def fault_evidence(self, node_ids: Iterable[str]) -> Mapping[str, DashboardAudit]:
        """기기마다 마지막 고장 근거 감사 행. 근거 행은 payload event 가 "isolate_device" 이거나,
        "record_result" 이고 outcome 이 unknown·failed 인 행이다 (rejected 는 기기를 격리하지 않으므로 근거가 아니다).
        행의 기기는 payload node_id → internal_request_id 의 request.node_id → command_id 의 node_command.node_id 로
        찾는다 (events.audit_node 와 같다). rowid 가 가장 큰 행 하나. 없는 기기는 빠진다. 빈 목록은 질의하지 않는다."""
        ...

    def rssi_after(self, seq: int) -> Sequence[DashboardRssi]:
        """rowid > seq, rowid 순."""
        ...

    def active_beacons(self) -> Sequence[DashboardBeacon]:
        """user_id 순."""
        ...

    def cursors(self) -> Mapping[str, int]:
        """{"request", "audit_log", "rssi_observation"} 의 최대 rowid. 행이 없으면 0."""
        ...

    def ping(self) -> None:
        """SELECT 1. 실패하면 sqlite3.Error 다."""
        ...
