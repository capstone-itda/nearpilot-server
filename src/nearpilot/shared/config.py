"""운영 수치 설정 (PRD §2 수치 설정과 변경 원칙, architecture.md §9).

코드 상수로 흩어 두지 않고 여기 하나로 모은다. 판정 한 건에는 같은 버전을 적용하고
`version` 을 감사 기록에 남긴다. 값이 None 인 항목은 PRD §7 미결이다.
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

    # 이탈 판단 (FR-20): 연속 미수신 후 유예
    leave_absent_sec: float = 10.0
    leave_grace_sec: float = 5.0

    # 전역 할당 대기 구간 (FR-23)
    allocation_window_ms: int = 500

    # ── PRD §7 미결 ──
    beacon_recent_sec: float | None = None  # ① 비콘 최근 수신 T
    online_ttl_sec: float | None = None  # 노드 ONLINE 판단 TTL
    exec_timeout_sec: float | None = None  # 실행 결과 대기 제한
    rental_timeout_sec: float | None = None  # 대여 기본 타임아웃
    approval_ttl_sec: int | None = None  # 승인 기본 유효시간

    def __post_init__(self) -> None:
        optional = {
            "beacon_recent_sec", "online_ttl_sec", "exec_timeout_sec",
            "rental_timeout_sec", "approval_ttl_sec",
        }
        for name in (
            "threshold_locker", "threshold_light", "rssi_frame_sec",
            "leave_absent_sec", "leave_grace_sec", "allocation_window_ms",
            "beacon_recent_sec", "online_ttl_sec", "exec_timeout_sec",
            "rental_timeout_sec", "approval_ttl_sec",
        ):
            v = getattr(self, name)
            if v is None and name in optional:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{name} 는 숫자여야 한다: {v!r}")
            if isinstance(v, float) and not isfinite(v):
                raise ValueError(f"{name} 는 유한한 숫자여야 한다: {v}")
            if name in ("allocation_window_ms", "approval_ttl_sec") and not isinstance(v, int):
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
            "exec_timeout_sec",
            "rental_timeout_sec",
            "approval_ttl_sec",
        ):
            v = getattr(self, name)
            if v is not None and v <= 0:
                raise ValueError(f"{name} 는 양수여야 한다: {v}")
        if self.leave_grace_sec < 0:
            raise ValueError(f"leave_grace_sec 는 0 이상이어야 한다: {self.leave_grace_sec}")
        if self.allocation_window_ms < 0:
            raise ValueError(f"allocation_window_ms 는 0 이상이어야 한다: {self.allocation_window_ms}")
        # 관련 시간값 일관성: 관측 주기보다 짧은 미수신 판단은 의미가 없다
        if self.leave_absent_sec < self.rssi_frame_sec:
            raise ValueError("leave_absent_sec 는 rssi_frame_sec 이상이어야 한다")
        if self.beacon_recent_sec is not None and self.beacon_recent_sec < self.rssi_frame_sec:
            raise ValueError("beacon_recent_sec 는 rssi_frame_sec 이상이어야 한다")
