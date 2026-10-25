"""대시보드 API 설정 (FR-12A, FR-12B). 값은 서버 설정의 선택 섹션 `[dashboard]` 에서 받는다 (2026-10-23 팀 결정)."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class DashboardSettings:
    """대시보드 API 설정. 기본값을 두지 않는다 — 값은 서버 설정에서 받는다 (AGENTS.md 코딩 규칙)."""

    queue_max_events: int  # 연결마다 쌓아 둘 수 있는 이벤트 수
    batch_max_events: int  # 메시지 하나에 담는 이벤트 수
    auth_timeout_sec: float  # 연결 뒤 인증 메시지를 기다리는 시간
    audit_limit_max: int  # 감사 조회 한 번의 최대 행 수

    def __post_init__(self) -> None:
        for name in ("queue_max_events", "batch_max_events", "audit_limit_max"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"{name} 는 1 이상의 정수여야 한다: {v!r}")
        v = self.auth_timeout_sec
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"auth_timeout_sec 는 숫자여야 한다: {v!r}")
        if (isinstance(v, float) and not isfinite(v)) or v <= 0:
            raise ValueError(f"auth_timeout_sec 는 0 보다 큰 유한한 숫자여야 한다: {v!r}")
