"""운영 수치 설정 (requirements.md §1.2 수치 설정과 변경 원칙, architecture.md §6).

코드 상수로 흩어 두지 않고 여기 하나로 모은다. 판정 한 건에는 같은 버전을 적용하고
`version` 을 감사 기록에 남긴다.
시간·보존·한도 값은 시연용 초기 기준이다. 실제 검증 결과에 따라 requirements.md 와 함께 바꾼다.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class Settings:
    version: str = "0"

    # 근접 임계값 (FR-17)
    threshold_locker: float = 0.95
    threshold_light: float = 0.75

    # 관측 (FR-05)
    rssi_frame_sec: float = 1.0

    # 이탈 판단 (FR-20): 연속 미수신 후 유예. 초기 기본값이며 설정으로 바꿀 수 있다.
    # 공용 조명의 유지 대상자 이탈 판단에 쓴다. 대여형의 점유 해제에는 쓰지 않는다.
    # 재시작 뒤 초기 참여 대기는 두 값의 합이다. 별도 설정을 두지 않는다.
    leave_absent_sec: float = 10.0
    leave_grace_sec: float = 5.0

    # 전역 할당 대기 구간 (FR-23)
    allocation_window_ms: int = 500

    # ── 시간 설정 (시연용 초기 기준) ──
    beacon_recent_sec: float = 3.0  # ① 최근 비콘 기준 T: 서버가 받은 유효 관측의 최근 구간 (FR-15)
    online_ttl_sec: float = 15.0  # 온라인 유지 시간: 유효한 기기 메시지의 마지막 수신부터 (FR-13)
    status_report_sec: float = 1.0  # 기기 상태 보고 주기
    reservation_wait_sec: float = 10.0  # 예약 대기 제한: 예약부터 전송 시작까지 (FR-19)
    exec_timeout_sec: float = 3.0  # 실행 응답 제한: 전송 시도 뒤 결과 대기 (FR-07)
    rental_duration_sec: float = 3600.0  # 대여 기본 시간: 자동 반납 기준이 아니라 초과시간 계산 기준 (FR-20)
    approval_ttl_sec: int = 3600  # 승인 기본 유효시간: 승인 시점부터. 접근 승인에만 쓴다 (FR-14)
    approval_pending_ttl_sec: int = 600  # 승인 대기 만료: 신청 시점부터 (FR-03)

    # ── 반납 검사 시간 설정 (시연용 초기 기준, FR-07·FR-20) ──
    safety_report_max_age_sec: float = 3.0  # 안전 보고 허용 나이: 검사 시각부터
    safety_check_timeout_sec: float = 3.0  # 반납 검사 응답 기한: 검사 요청의 전송 시작부터
    safety_check_retry_sec: float = 1.0  # 반납 재검사 대기 간격
    safety_unconfirmed_limit_sec: float = 30.0  # 연속 안전 확인 불능 제한: 넘으면 FAULT 격리

    # ── 보존·한도 설정 (시연용 초기 기준, FR-21·FR-22) ──
    use_history_retention_days: int = 7  # 본인 종료 이력 조회: 종료 뒤부터
    audit_retention_days: int = 30  # 상세 실행·감사 기록 보존. 진행 중·미해결 기록은 지우지 않는다
    request_dedup_ttl_sec: int = 1200  # 외부 요청의 중복 방지 보장기간: 최종 처리 완료 뒤부터
    dedup_entry_limit: int = 100_000  # 신규 조작 접수 제한: 서버 전체 중복 방지 항목 수

    def __post_init__(self) -> None:
        for name in (
            "threshold_locker", "threshold_light", "rssi_frame_sec",
            "leave_absent_sec", "leave_grace_sec", "allocation_window_ms",
            "beacon_recent_sec", "online_ttl_sec", "status_report_sec",
            "reservation_wait_sec", "exec_timeout_sec",
            "rental_duration_sec", "approval_ttl_sec", "approval_pending_ttl_sec",
            "safety_report_max_age_sec", "safety_check_timeout_sec",
            "safety_check_retry_sec", "safety_unconfirmed_limit_sec",
            "use_history_retention_days", "audit_retention_days",
            "request_dedup_ttl_sec", "dedup_entry_limit",
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{name} 는 숫자여야 한다: {v!r}")
            if isinstance(v, float) and not isfinite(v):
                raise ValueError(f"{name} 는 유한한 숫자여야 한다: {v}")
            if name in (
                "allocation_window_ms", "approval_ttl_sec", "approval_pending_ttl_sec",
                "use_history_retention_days", "audit_retention_days",
                "request_dedup_ttl_sec", "dedup_entry_limit",
            ) and not isinstance(v, int):
                raise ValueError(f"{name} 는 정수여야 한다: {v}")
        for name in ("threshold_locker", "threshold_light"):
            v = getattr(self, name)
            if not 0.0 < v <= 1.0:
                raise ValueError(f"{name} 는 (0, 1] 범위여야 한다: {v}")
        for name in (
            "rssi_frame_sec",
            "leave_absent_sec",
            "beacon_recent_sec",
            "online_ttl_sec",
            "status_report_sec",
            "reservation_wait_sec",
            "exec_timeout_sec",
            "rental_duration_sec",
            "approval_ttl_sec",
            "approval_pending_ttl_sec",
            "safety_report_max_age_sec",
            "safety_check_timeout_sec",
            "safety_check_retry_sec",
            "safety_unconfirmed_limit_sec",
            "use_history_retention_days",
            "audit_retention_days",
            "request_dedup_ttl_sec",
            "dedup_entry_limit",
        ):
            v = getattr(self, name)
            if v <= 0:
                raise ValueError(f"{name} 는 양수여야 한다: {v}")
        if self.leave_grace_sec < 0:
            raise ValueError(f"leave_grace_sec 는 0 이상이어야 한다: {self.leave_grace_sec}")
        if self.allocation_window_ms < 0:
            raise ValueError(f"allocation_window_ms 는 0 이상이어야 한다: {self.allocation_window_ms}")
        # 관련 시간값 일관성: 관측 주기보다 짧은 미수신 판단은 의미가 없다
        if self.leave_absent_sec < self.rssi_frame_sec:
            raise ValueError("leave_absent_sec 는 rssi_frame_sec 이상이어야 한다")
        if self.beacon_recent_sec < self.rssi_frame_sec:
            raise ValueError("beacon_recent_sec 는 rssi_frame_sec 이상이어야 한다")
        # 상태 보고 주기보다 짧은 온라인 유지 시간은 정상 기기를 오프라인으로 만든다
        if self.online_ttl_sec < self.status_report_sec:
            raise ValueError("online_ttl_sec 는 status_report_sec 이상이어야 한다")
        # 응답 기한보다 짧은 확인 불능 제한은 첫 검사의 응답을 기다리지 못한다
        if self.safety_unconfirmed_limit_sec < self.safety_check_timeout_sec:
            raise ValueError("safety_unconfirmed_limit_sec 는 safety_check_timeout_sec 이상이어야 한다")
