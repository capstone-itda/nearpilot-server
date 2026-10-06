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


class ReasonCode(StrEnum):
    """판정 사유. 항목을 추가·삭제하는 것은 팀 결정이다."""

    OK = "OK"
    # 인증·입력
    INVALID_TOKEN = "INVALID_TOKEN"
    INVALID_INPUT = "INVALID_INPUT"
    # ⓪
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"  # 같은 request_id, 다른 입력
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
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"  # 유효시간 만료
    APPROVAL_EXHAUSTED = "APPROVAL_EXHAUSTED"  # 사용 횟수 소진
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"  # 승인과 다른 동작
    # ⑤⑥
    DEVICE_BUSY = "DEVICE_BUSY"
    USER_BUSY = "USER_BUSY"
    NOT_ASSIGNED = "NOT_ASSIGNED"  # 전역 할당에서 미배정 (FR-23)
    # 핸들 (FR-21): 없는 핸들과 타인 핸들을 구분하지 않는다
    USE_NOT_FOUND = "USE_NOT_FOUND"
    # 실행
    RECHECK_FAILED = "RECHECK_FAILED"  # 실행 직전 재검사 실패
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
    LEAVE = "leave"
    TIMEOUT = "timeout"
    FAULT = "fault"


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
    NOT_SENT = "not_sent"  # 확실한 미전송 → 예약·승인 사용분 자동 반환 가능
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # 노드가 실패를 보고
    UNKNOWN = "unknown"  # 시간 초과·연결 끊김. 미실행으로 간주하지 않음 → FAULT


class ReserveOutcome(StrEnum):
    """`Repository.reserve_if_free` 결과 (⑤⑥ + 승인 사용분 예약)."""

    RESERVED = "reserved"
    DEVICE_BUSY = "device_busy"
    USER_BUSY = "user_busy"
    APPROVAL_EXHAUSTED = "approval_exhausted"
