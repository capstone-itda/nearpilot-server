"""운영 수치 설정 (requirements.md §1.2 수치 설정과 변경 원칙, architecture.md §6).

코드 상수로 흩어 두지 않고 여기 하나로 모은다. 판정 한 건에는 같은 버전을 적용하고
`version` 을 감사 기록에 남긴다.
시간·보존·한도 값은 시연용 초기 기준이다. 실제 검증 결과에 따라 requirements.md 와 함께 바꾼다.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from nearpilot.shared.descriptor import is_name
from nearpilot.shared.frozen import freeze, thaw

# 근접 임계값을 필드로 두는 종류다. 그 밖의 종류는 `thresholds` 에 둔다 (spec core-device-actions 결정 4).
_THRESHOLD_FIELDS = {"locker": "threshold_locker", "light": "threshold_light", "speaker": "threshold_speaker"}

# 인자 상한과 기본값의 초기 기준. 기존 조명·스피커 설정과 같은 값이다 (requirements.md §1.2).
# 64 는 light-1 의 보드 5V 전원에서 흰색 점등을 확인한 값이다.
# 20 은 커패시터 없는 speaker-1 에서 담당자가 정한 값이다. 커패시터를 달기 전에는 올리지 않는다.
_PARAM_MAX = {"light": {"on": {"brightness": 64}}, "speaker": {"on": {"volume": 20}}}
_PARAM_DEFAULT = {"light": {"on": {"brightness": 64, "color": "#FFFFFF"}}, "speaker": {"on": {"volume": 20}}}

# 옛 설정 키 → 새 경로 (requirements.md §1.2 "기존 ... 은 이 형식으로 옮긴다")
LEGACY_PARAM_KEYS = {
    "light_brightness_max": ("param_max", "light", "on", "brightness"),
    "light_default_brightness": ("param_default", "light", "on", "brightness"),
    "light_default_color": ("param_default", "light", "on", "color"),
    "speaker_volume_max": ("param_max", "speaker", "on", "volume"),
    "speaker_default_volume": ("param_default", "speaker", "on", "volume"),
}


def migrate_settings_table(table: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """설정 파일 표나 저장한 스냅샷을 `Settings` 인자로 바꾼다. 옮긴 옛 키 목록도 반환한다.

    - 필드가 아닌 `threshold_{kind}` 키는 `thresholds` 로 모은다.
    - 옛 키 5개는 `param_max`·`param_default` 의 같은 자리로 옮긴다. 같은 표에 새 형식이 함께 있으면 거부한다.
    형식이 틀리면 ValueError 다. 값의 검증은 `Settings` 가 한다.
    """
    out = dict(table)
    thresholds = dict(out.pop("thresholds", None) or {})
    for key in list(out):
        if key.startswith("threshold_") and key not in _FIELD_NAMES:
            kind = key[len("threshold_"):]
            if kind in thresholds:
                raise ValueError(f"{key} 를 두 번 썼다")
            thresholds[kind] = out.pop(key)
    if thresholds:
        out["thresholds"] = thresholds
    legacy = tuple(key for key in LEGACY_PARAM_KEYS if key in out)
    for section in sorted({LEGACY_PARAM_KEYS[key][0] for key in legacy}):
        if section in out:
            raise ValueError(f"{section} 와 옛 키 {', '.join(sorted(legacy))} 를 함께 쓸 수 없다")
    for key in legacy:
        section, kind, action, arg = LEGACY_PARAM_KEYS[key]
        if section not in out:
            out[section] = deepcopy(_PARAM_MAX if section == "param_max" else _PARAM_DEFAULT)
        out[section].setdefault(kind, {}).setdefault(action, {})[arg] = out.pop(key)
    return out, legacy


def _number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def _check_threshold(name: str, value: object) -> None:
    if value is None:
        return
    if not _number(value):
        raise ValueError(f"{name} 는 유한한 숫자 또는 None 이어야 한다")
    if not 0.0 < value <= 1.0:
        raise ValueError(f"{name} 는 (0, 1] 범위여야 한다: {value}")


def _check_param_table(name: str, table: object, leaf: str) -> None:
    """`{kind: {action: {arg: 값}}}` 모양과 이름 규칙을 검사한다."""
    if not isinstance(table, Mapping):
        raise ValueError(f"{name} 는 표여야 한다")
    for kind, actions in table.items():
        if not is_name(kind) or not isinstance(actions, Mapping):
            raise ValueError(f"{name}.{kind} 형식 오류")
        for action, args in actions.items():
            if not is_name(action) or not isinstance(args, Mapping):
                raise ValueError(f"{name}.{kind}.{action} 형식 오류")
            for arg, value in args.items():
                where = f"{name}.{kind}.{action}.{arg}"
                if not is_name(arg):
                    raise ValueError(f"{where} 이름 오류")
                if leaf == "number" and not _number(value):
                    raise ValueError(f"{where} 는 유한한 숫자여야 한다")
                if leaf == "scalar" and not (isinstance(value, (str, bool)) or _number(value)):
                    raise ValueError(f"{where} 는 문자열, 숫자, 불리언이어야 한다")


@dataclass(frozen=True, slots=True)
class Settings:
    version: str = "0"

    # 근접 임계값 (FR-17)
    threshold_locker: float = 0.95
    threshold_light: float = 0.75
    # 스피커 수치는 미결이다. None 은 미설정이며 실행을 허가하지 않는다 (#61).
    threshold_speaker: float | None = None
    # 위 세 종류 밖의 근접 임계값. 설정 파일에서는 `threshold_{kind}` 로 쓴다 (FR-17, #245).
    # 없거나 None 이면 그 종류의 근접 지정과 전역 할당을 허가하지 않는다. 판정은 `threshold_for` 로만 읽는다.
    thresholds: Mapping[str, float | None] = field(default_factory=dict)
    # 이전 설정과 저장된 스냅샷을 읽기 위한 호환 필드다. 명시 지정 판정에는 쓰지 않는다 (FR-17).
    threshold_explicit_locker: float | None = None
    threshold_explicit_light: float | None = None
    threshold_explicit_speaker: float | None = None

    # 관측 (FR-05)
    rssi_frame_sec: float = 1.0

    # 이탈 판단 (FR-20): 연속 미수신 후 유예. 초기 기본값이며 설정으로 바꿀 수 있다.
    # 공용형 기기의 유지 대상자 이탈 판단에 쓴다. 대여형의 점유 해제에는 쓰지 않는다.
    # 재시작 뒤 초기 참여 대기는 두 값의 합이다. 별도 설정을 두지 않는다.
    leave_absent_sec: float = 10.0
    leave_grace_sec: float = 5.0

    # 전역 할당 대기 구간 (FR-23)
    allocation_window_ms: int = 500
    # 전역 할당(`any:{kind}`)을 받는 종류 (FR-23, #245)
    any_allowed_kinds: tuple[str, ...] = ("locker",)

    # ── 시간 설정 (시연용 초기 기준) ──
    beacon_recent_sec: float = 3.0  # ① 최근 비콘 기준 T: 서버가 받은 유효 관측의 최근 구간 (FR-15)
    anchor_recent_sec: float = 3.0  # 근접 참여 기기의 최근 구간: 서버가 받은 마지막 rssi 보고부터 (FR-09, #252)
    order_similar_db: float = 3.0  # 거리 선택자 경계의 "비슷함" 기준: 정렬 값 차이 dB (FR-09, #255)
    online_ttl_sec: float = 15.0  # 온라인 유지 시간: 유효한 기기 메시지의 마지막 수신부터 (FR-13)
    status_report_sec: float = 1.0  # 기기 상태 보고 주기
    reservation_wait_sec: float = 10.0  # 예약 대기 제한: 예약부터 전송 시작까지 (FR-19)
    exec_timeout_sec: float = 3.0  # 실행 응답 제한: 전송 시도 뒤 결과 대기 (FR-07)
    rental_duration_sec: float = 3600.0  # 대여 기본 시간: 자동 반납 기준이 아니라 초과시간 계산 기준 (FR-20)
    approval_ttl_sec: int = 3600  # 승인 기본 유효시간: 승인 시점부터. 접근 승인에만 쓴다 (FR-14)
    approval_pending_ttl_sec: int = 600  # 승인 대기 만료: 신청 시점부터 (FR-03)

    # ── 보존·한도 설정 (시연용 초기 기준, FR-21·FR-22) ──
    use_history_retention_days: int = 7  # 본인 종료 이력 조회: 종료 뒤부터
    audit_retention_days: int = 30  # 상세 실행·감사 기록 보존. 진행 중·미해결 기록은 지우지 않는다
    request_dedup_ttl_sec: int = 1200  # 외부 요청의 중복 방지 보장기간: 최종 처리 완료 뒤부터
    dedup_entry_limit: int = 100_000  # 신규 조작 접수 제한: 서버 전체 중복 방지 항목 수
    retention_cleanup_interval_sec: int = 60  # 정리 주기: 서버 조립이 시작할 때와 이 간격마다 부른다 (#164)

    # ── 동작 인자 (FR-09, #245) ──
    # `{kind: {action: {arg: 값}}}`. 상한은 선언의 maximum 과 작은 값을 쓴다. 기본값은 앞선 성공 명령이 없을 때 쓴다.
    param_max: Mapping[str, Mapping[str, Mapping[str, int | float]]] = field(
        default_factory=lambda: deepcopy(_PARAM_MAX))
    param_default: Mapping[str, Mapping[str, Mapping[str, Any]]] = field(
        default_factory=lambda: deepcopy(_PARAM_DEFAULT))

    def threshold_for(self, kind: str | None) -> float | None:
        """근접 지정과 전역 할당의 임계값. 없으면 None 이고 허가하지 않는다 (FR-17). 명시 지정에는 쓰지 않는다."""
        if kind is None:
            return None
        name = _THRESHOLD_FIELDS.get(kind)
        return getattr(self, name) if name is not None else self.thresholds.get(kind)

    def param_caps(self, kind: str, action: str) -> Mapping[str, int | float]:
        """인자별 서버 상한. 없으면 빈 맵이다 (FR-09)."""
        return self.param_max.get(kind, {}).get(action, {})

    def param_defaults(self, kind: str, action: str) -> Mapping[str, Any]:
        """생략한 인자의 기본값. 없으면 빈 맵이다 (FR-09)."""
        return self.param_default.get(kind, {}).get(action, {})

    def __post_init__(self) -> None:
        object.__setattr__(self, "thresholds", freeze(self.thresholds))
        object.__setattr__(self, "param_max", freeze(self.param_max))
        object.__setattr__(self, "param_default", freeze(self.param_default))
        object.__setattr__(self, "any_allowed_kinds", freeze(self.any_allowed_kinds))
        for name in (
            "threshold_locker", "threshold_light", "rssi_frame_sec",
            "leave_absent_sec", "leave_grace_sec", "allocation_window_ms",
            "beacon_recent_sec", "anchor_recent_sec", "order_similar_db",
            "online_ttl_sec", "status_report_sec",
            "reservation_wait_sec", "exec_timeout_sec",
            "rental_duration_sec", "approval_ttl_sec", "approval_pending_ttl_sec",
            "use_history_retention_days", "audit_retention_days",
            "request_dedup_ttl_sec", "dedup_entry_limit", "retention_cleanup_interval_sec",
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{name} 는 숫자여야 한다: {v!r}")
            if isinstance(v, float) and not isfinite(v):
                raise ValueError(f"{name} 는 유한한 숫자여야 한다: {v}")
            if name in (
                "allocation_window_ms", "approval_ttl_sec", "approval_pending_ttl_sec",
                "use_history_retention_days", "audit_retention_days",
                "request_dedup_ttl_sec", "dedup_entry_limit", "retention_cleanup_interval_sec",
            ) and not isinstance(v, int):
                raise ValueError(f"{name} 는 정수여야 한다: {v}")
        _check_threshold("threshold_speaker", self.threshold_speaker)
        if not isinstance(self.thresholds, Mapping):
            raise ValueError("thresholds 는 표여야 한다")
        for kind, v in self.thresholds.items():
            if not is_name(kind) or kind in _THRESHOLD_FIELDS:
                raise ValueError(f"threshold_{kind} 는 쓸 수 없는 종류 이름이다")
            _check_threshold(f"threshold_{kind}", v)
        if not isinstance(self.any_allowed_kinds, tuple) or not all(map(is_name, self.any_allowed_kinds)):
            raise ValueError("any_allowed_kinds 는 종류 이름의 목록이어야 한다")
        if len(set(self.any_allowed_kinds)) != len(self.any_allowed_kinds):
            raise ValueError("any_allowed_kinds 에 같은 종류가 두 번 있다")
        _check_param_table("param_max", self.param_max, "number")
        _check_param_table("param_default", self.param_default, "scalar")
        for kind, actions in self.param_default.items():
            for action, args in actions.items():
                caps = self.param_caps(kind, action)
                for arg, value in args.items():
                    if _number(value) and arg in caps and value > caps[arg]:
                        raise ValueError(f"param_default.{kind}.{action}.{arg} 는 param_max 이하여야 한다")
        for name, general in (
            ("threshold_explicit_locker", "threshold_locker"),
            ("threshold_explicit_light", "threshold_light"),
            ("threshold_explicit_speaker", "threshold_speaker"),
        ):
            v = getattr(self, name)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v):
                raise ValueError(f"{name} 는 유한한 숫자 또는 None 이어야 한다")
            if getattr(self, general) is None:
                raise ValueError(f"{general} 를 설정하기 전에는 {name} 를 설정할 수 없다")
            if not 0.0 < v < getattr(self, general):
                raise ValueError(f"{name} 는 0 보다 크고 {general} 보다 작아야 한다")
        for name in ("threshold_locker", "threshold_light"):
            v = getattr(self, name)
            if not 0.0 < v <= 1.0:
                raise ValueError(f"{name} 는 (0, 1] 범위여야 한다: {v}")
        for name in (
            "rssi_frame_sec",
            "leave_absent_sec",
            "beacon_recent_sec",
            "anchor_recent_sec",
            "order_similar_db",
            "online_ttl_sec",
            "status_report_sec",
            "reservation_wait_sec",
            "exec_timeout_sec",
            "rental_duration_sec",
            "approval_ttl_sec",
            "approval_pending_ttl_sec",
            "use_history_retention_days",
            "audit_retention_days",
            "request_dedup_ttl_sec",
            "dedup_entry_limit",
            "retention_cleanup_interval_sec",
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
        if self.anchor_recent_sec < self.rssi_frame_sec:
            raise ValueError("anchor_recent_sec 는 rssi_frame_sec 이상이어야 한다")
        # 상태 보고 주기보다 짧은 온라인 유지 시간은 정상 기기를 오프라인으로 만든다
        if self.online_ttl_sec < self.status_report_sec:
            raise ValueError("online_ttl_sec 는 status_report_sec 이상이어야 한다")


_FIELD_NAMES = frozenset(Settings.__dataclass_fields__)


def settings_to_data(settings: Settings) -> dict[str, Any]:
    """감사 기록에 남길 설정 전체 (#83). JSON 으로 바로 쓸 수 있는 값이다."""
    return {name: thaw(getattr(settings, name)) for name in Settings.__dataclass_fields__}


def settings_from_data(data: Mapping[str, Any]) -> Settings:
    """저장한 설정 스냅샷을 복원한다. 옛 형식은 새 형식으로 옮긴다 (FR-22). 현재 설정으로 채우지 않는다."""
    table, _ = migrate_settings_table(data)
    if "any_allowed_kinds" in table:
        table["any_allowed_kinds"] = tuple(table["any_allowed_kinds"])
    return Settings(**table)
