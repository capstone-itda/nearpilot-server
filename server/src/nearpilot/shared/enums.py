"""계층 간에 오가는 상태값·판정 결과·사유 코드 (architecture.md §4, §6).

값 문자열은 DB 저장값·감사 로그·API 응답에 그대로 쓰이므로 바꾸면 팀 결정 대상이다.
"""

from enum import StrEnum


# ── 판정 (FR-18) ────────────────────────────────────────────────────


class Verdict(StrEnum):
    OK = "OK"
    BUSY = "BUSY"
    ASK = "ASK"
    DENY = "DENY"
    NEED_APPROVAL = "NEED_APPROVAL"


class Step(StrEnum):
    """판정·실행 단계. 감사 로그 `step` 값과 같다 (architecture.md §6 audit_log)."""

    AUTH = "auth"
    DEDUP = "0"  # ⓪ 중복
    BEACON = "1"  # ① 비콘
    CANDIDATE = "2"  # ② 후보
    PROXIMITY = "3"  # ③ 근접
    POLICY = "4"  # ④ 정책
    DEVICE_OCCUPANCY = "5"  # ⑤ 장치 점유
    USER_OCCUPANCY = "6"  # ⑥ 사용자 점유
    EXEC = "exec"
    CLOSE = "close"
    FAULT = "fault"
    ANNOUNCE = "announce"  # 기기 발표의 신뢰 되돌림 감사 (FR-04, FR-22)
    ADMIN = "admin"  # 관리 쓰기의 감사 (FR-12A, FR-22)


class ReasonCode(StrEnum):
    """판정 사유. 항목을 추가·삭제하는 것은 팀 결정이다."""

    OK = "OK"
    # 인증·입력
    INVALID_TOKEN = "INVALID_TOKEN"
    INVALID_INPUT = "INVALID_INPUT"
    # ⓪
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"  # 같은 request_id, 다른 입력
    # 신규 조작 접수 제한에만 쓴다. 판정은 DENY 와 함께 반환한다 (FR-22).
    # 새 물리 조작 요청에만 쓴다. 재전송·조회·반납·복구는 이 사유로 거부하지 않는다.
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    # ①
    BEACON_NOT_SEEN = "BEACON_NOT_SEEN"  # 원격 요청·관측 부재
    # ②
    NO_CANDIDATE = "NO_CANDIDATE"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"  # 미신뢰·오프라인·FAULT
    # ③
    LOW_CONFIDENCE = "LOW_CONFIDENCE"  # ASK: 사후확률이 임계값 미만
    MOVE_CLOSER = "MOVE_CLOSER"  # ASK: 명시적 지정이지만 근접 확률 부족
    # ④
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_PENDING = "APPROVAL_PENDING"  # 소유자 응답 대기 (FR-03)
    APPROVAL_REJECTED = "APPROVAL_REJECTED"  # 소유자 거부
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"  # 승인 유효시간 만료
    APPROVAL_REQUEST_EXPIRED = "APPROVAL_REQUEST_EXPIRED"  # 대기 신청 만료. 소유자 거부와 구분한다 (FR-03)
    APPROVAL_EXHAUSTED = "APPROVAL_EXHAUSTED"  # 사용 횟수 소진
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"  # 승인과 다른 동작
    # ⑤⑥
    DEVICE_BUSY = "DEVICE_BUSY"
    USER_BUSY = "USER_BUSY"
    NOT_ASSIGNED = "NOT_ASSIGNED"  # 전역 할당에서 미배정 (FR-23)
    # 핸들 (FR-21): 없는 핸들과 타인 핸들을 구분하지 않는다
    USE_NOT_FOUND = "USE_NOT_FOUND"
    # 실행
    # 실행 직전 재검사의 검사 오류에만 쓴다. 판정은 DENY 와 함께 반환한다 (#16).
    # 정상 거부에는 쓰지 않는다. 재검사의 원래 판정과 사유를 그대로 전달한다.
    RECHECK_FAILED = "RECHECK_FAILED"
    # 판정 중 예상하지 못한 예외에 쓴다. 판정은 DENY 와 함께 반환한다 (#25).
    # 정상 거부에는 쓰지 않는다. 호스트에는 일반 안내만 보내고 예외 내용은 감사 기록에만 남긴다.
    INTERNAL_ERROR = "INTERNAL_ERROR"
    COMMAND_IN_PROGRESS = "COMMAND_IN_PROGRESS"  # BUSY: 다른 구동 명령이 진행 중이다. 점유 충돌과 구분한다.
    EXEC_FAILED = "EXEC_FAILED"
    EXEC_UNKNOWN = "EXEC_UNKNOWN"  # 결과 불명 → FAULT 복구


# ── 요청 ────────────────────────────────────────────────────────────


class Action(StrEnum):
    OPEN = "open"
    ON = "on"
    OFF = "off"


class TargetKind(StrEnum):
    NEAREST = "nearest"  # nearest:locker
    EXPLICIT = "explicit"  # locker-1
    ANY = "any"  # any:locker (FR-23)


# ── 계정·노드 (requirements.md §1.3) ──────────────────────────────────────────


class Role(StrEnum):
    VISITOR = "visitor"
    OWNER = "owner"


class Capability(StrEnum):
    LOCKER = "locker"
    LIGHT = "light"
    SPEAKER = "speaker"  # on 은 재생, off 는 정지 (#61)
    # gate/pass 는 향후 확장 예약값 (architecture.md §6)


class Occupancy(StrEnum):
    RENTAL = "rental"  # 대여형: 점유 세션 사용
    SHARED = "shared"  # 공용형: ⑤⑥·세션 생략


class TrustState(StrEnum):
    DISCOVERED = "discovered"
    TRUSTED = "trusted"
    REVOKED = "revoked"


class DeviceState(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    FAULT = "FAULT"


# ── 점유 세션 (FR-19, FR-20) ───────────────────────────────────────────


class SessionState(StrEnum):
    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class EndReason(StrEnum):
    RELEASE = "release"
    LEAVE = "leave"  # 예약값: 현재 계약에서 쓰지 않는다 (architecture.md §6)
    TIMEOUT = "timeout"  # 예약값: 현재 계약에서 쓰지 않는다 (architecture.md §6)
    FAULT = "fault"
    CANCELLED = "cancelled"  # 확실한 미전송 취소 (FR-19)


# ── 정책·승인 (FR-03·14) ────────────────────────────────────────────


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class AccessMode(StrEnum):
    OPEN = "open"
    RESTRICTED = "restricted"


class ApprovalMode(StrEnum):
    ONE_TIME = "one_time"
    TIME_WINDOW = "time_window"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalUsageState(StrEnum):
    RESERVED = "reserved"
    HELD = "held"  # 결과 불명으로 보류
    CONSUMED = "consumed"
    RELEASED = "released"  # 미전송 확정으로 반환


# ── 노드 명령 (architecture.md §4 노드 명령 인터페이스) ─────────────


class CommandOutcome(StrEnum):
    REJECTED = "rejected"  # 검증된 DEVICE_BUSY 접수·실행 전 거부. NOT_SENT 와 구분한다.
    NOT_SENT = "not_sent"  # 확실한 미전송 → 예약·승인 사용분 자동 반환 가능
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # 노드가 실패를 보고
    UNKNOWN = "unknown"  # 시간 초과·연결 끊김. 미실행으로 간주하지 않음 → FAULT


class NodeResultState(StrEnum):
    """기기가 `result` 토픽으로 보고한 상태 (architecture.md §5 MQTT 5절).

    기기 보고 값을 그대로 옮긴다. 서버가 정리한 전송 결과는 `CommandOutcome` 이다.
    """

    ACCEPTED = "accepted"  # 접수 기록을 저장했다. 완료가 아니다.
    RUNNING = "running"  # 물리 동작을 시작했다. 완료가 아니다.
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"  # 기기가 실행·완료 여부를 증명하지 못했다. 서버 타임아웃과 구분한다.
    REJECTED = "rejected"  # 접수 검사에서 거부했다. 사유는 error_code 에 있다.


class RejectCode(StrEnum):
    """`result.state=rejected` 의 `error_code` (architecture.md §5 MQTT 4절).

    기기 통신의 근거다. 외부 응답의 `ReasonCode` 와 다른 값이다.
    """

    SESSION_MISMATCH = "SESSION_MISMATCH"
    COMMAND_EXPIRED = "COMMAND_EXPIRED"
    CLOCK_UNVERIFIED = "CLOCK_UNVERIFIED"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    COMMAND_ID_CONFLICT = "COMMAND_ID_CONFLICT"
    INVALID_MESSAGE = "INVALID_MESSAGE"
    STORAGE_ERROR = "STORAGE_ERROR"
    DEVICE_BUSY = "DEVICE_BUSY"  # 다른 구동 명령이 진행 중이다. 이번 명령의 미접수·미실행이다.


class ActuatorState(StrEnum):
    """`status` 의 `physical_state.actuator_state` (architecture.md §5 MQTT 3절)."""

    IDLE = "idle"
    BUSY = "busy"
    UNKNOWN = "unknown"


class SafetyCheckOutcome(StrEnum):
    """반납 검사 결과 (FR-07, FR-20, architecture.md §4 반납 검사와 종료)."""

    PASSED = "passed"  # 검사 기준 충족. 종료 결정은 CORE 가 한다.
    NOT_READY = "not_ready"  # 종료 조건 미충족 (예: 문 열림). 정상 대기다.
    FAULT = "fault"  # 기기가 고장을 확인했다. 즉시 격리한다.
    UNKNOWN = "unknown"  # 기기가 결과를 확인하지 못했다. 통과로 보지 않는다.


class ExecutionStatus(StrEnum):
    """요청 하나의 저장된 실행 진행 상태 (#16, architecture.md §4).

    `CommandOutcome` 은 IOT 전송 한 번의 결과다. 이 값은 요청 단위의 진행 상태다.
    """

    PENDING = "pending"  # 명령 ID 를 연결했다. 전송을 시작하지 않았다.
    IN_PROGRESS = "in_progress"  # 전송 시작을 기록했다. 결과를 기다린다.
    SUCCEEDED = "succeeded"  # 실행 성공과 저장 반영을 확인했다.
    FAILED = "failed"  # 기기가 실패를 보고했다. FAULT 로 격리한다.
    UNKNOWN = "unknown"  # 실행 여부를 확정하지 못했다. FAULT 로 격리한다.
    NOT_SENT = "not_sent"  # 확실한 미전송으로 취소했다.
    REJECTED = "rejected"  # 검증된 DEVICE_BUSY. 격리하지 않고 이번 사용분만 반환한다.


class StoreOutcome(StrEnum):
    """`ExecutionStore` 사건 반영 결과 (#17). 저장 오류는 `StoreError` 예외로 구분한다."""

    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"  # 같은 사건·같은 내용의 재호출. 재전송 허가가 아니다.
    CONFLICT = "conflict"  # 현재 상태 또는 기존 기록과 충돌한다. 기존 기록을 덮어쓰지 않는다.


class ReserveOutcome(StrEnum):
    """`Repository.reserve_if_free` 결과 (⑤⑥ + 승인 사용분 예약)."""

    RESERVED = "reserved"
    REOPERATING = "reoperating"  # 본인 ACTIVE 세션에 새 요청을 연결했다. 새 세션을 만들지 않는다.
    DEVICE_BUSY = "device_busy"
    USER_BUSY = "user_busy"
    APPROVAL_EXHAUSTED = "approval_exhausted"


class ExecutionKind(StrEnum):
    RENTAL_INITIAL = "rental_initial"
    RENTAL_REOPERATION = "rental_reoperation"
    SHARED = "shared"


class StoreConflictReason(StrEnum):
    COMMAND_IN_PROGRESS = "COMMAND_IN_PROGRESS"
    STATE_CONFLICT = "STATE_CONFLICT"
    CONTENT_CONFLICT = "CONTENT_CONFLICT"
    LINK_MISMATCH = "LINK_MISMATCH"


class RecoveryStatus(StrEnum):
    NOT_READY = "not_ready"
    READY = "ready"
    RECOVERING = "recovering"
    RECOVERED = "recovered"


class RecoveryBlocker(StrEnum):
    NO_RECOVERY_REQUIRED = "NO_RECOVERY_REQUIRED"
    EXECUTION_UNCONFIRMED = "EXECUTION_UNCONFIRMED"
    SAFETY_UNCONFIRMED = "SAFETY_UNCONFIRMED"
    PHYSICAL_STATE_UNSYNCED = "PHYSICAL_STATE_UNSYNCED"
    OFFLINE = "OFFLINE"
    NOT_TRUSTED = "NOT_TRUSTED"
    STORE_OUTCOME_UNKNOWN = "STORE_OUTCOME_UNKNOWN"


class RecoveryOutcome(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    PENDING = "pending"


class RecoveryReason(StrEnum):
    OK = "OK"
    STORE_NOT_APPLIED = "STORE_NOT_APPLIED"
    STORE_OUTCOME_UNKNOWN = "STORE_OUTCOME_UNKNOWN"
    NOT_OWNER = "NOT_OWNER"
    RECOVERY_MISMATCH = "RECOVERY_MISMATCH"
    NOT_READY = "NOT_READY"
    STATE_CONFLICT = "STATE_CONFLICT"


class ApprovalDecisionReason(StrEnum):
    OK = "OK"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_DECIDED = "ALREADY_DECIDED"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    REQUEST_EXPIRED = "REQUEST_EXPIRED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"


class AdminWriteOutcome(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"


class AdminWriteReason(StrEnum):
    OK = "OK"
    BEACON_ALREADY_REGISTERED = "BEACON_ALREADY_REGISTERED"
    ACCOUNT_ALREADY_EXISTS = "ACCOUNT_ALREADY_EXISTS"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    NODE_NOT_FOUND = "NODE_NOT_FOUND"
    PLACE_NOT_FOUND = "PLACE_NOT_FOUND"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    STATE_CONFLICT = "STATE_CONFLICT"
