"""`db/repositories/` 공통 헬퍼. 시각 변환, ID 생성, JSON 인코딩, 트랜잭션.

시각은 UTC ISO 8601 문자열로 저장한다 (spec §6 결정). 내부 ID는 UUID4다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from nearpilot.shared.config import Settings
from nearpilot.shared.errors import StoreNotApplied, StoreOutcomeUnknown
from nearpilot.shared.models import LightControls, LightSetting

logger = logging.getLogger(__name__)


def new_id() -> str:
    return str(uuid4())


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_default(value: Any) -> str:
    """CORE가 넘기는 `evidence` 등에 `datetime`이 섞여 있어도 저장할 수 있게 한다.

    문자열로 바뀐 뒤에는 다시 `datetime`으로 복원하지 않는다. 같은 사건인지 비교할 때는
    호출 쪽 값도 `to_json`으로 한 번 거쳐서 같은 문자열로 맞춰 비교한다.
    """
    if isinstance(value, datetime):
        return iso(value)
    raise TypeError(f"{type(value).__name__} 는 JSON으로 직렬화할 수 없다")


def to_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value) if value else {}, ensure_ascii=False, default=_json_default)


def from_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    return json.loads(value)


def settings_to_json(settings: Settings) -> str:
    """판정에 적용한 설정 전체를 저장한다. 버전만 저장하지 않는다 (#83)."""
    return json.dumps(asdict(settings), ensure_ascii=False)


def settings_from_json(value: str) -> Settings:
    """저장한 설정 스냅샷을 그대로 복원한다. 현재 설정으로 채우지 않는다 (#83)."""
    return Settings(**json.loads(value))


def light_to_json(light: LightSetting | None) -> str | None:
    """명령의 조명 설정 열 값. 키 순서를 고정해서 같은 설정은 같은 문자열이다 (FR-09)."""
    return None if light is None else json.dumps(light.to_json(), sort_keys=True)


def light_from_json(value: str | None) -> LightSetting | None:
    return None if value is None else LightSetting.from_json(json.loads(value))


def light_controls_to_json(controls: LightControls | None) -> str | None:
    """발표한 조명 제어 정보 열 값 (FR-04). 없으면 NULL 이다."""
    if controls is None:
        return None
    return json.dumps({"brightness_max": controls.brightness_max, "color": controls.color}, sort_keys=True)


def light_controls_from_json(value: str | None) -> LightControls | None:
    if value is None:
        return None
    data = json.loads(value)
    return LightControls(data["brightness_max"], data["color"])


class _JsonlExporter(Protocol):
    def flush(self) -> None: ...


# sqlite3.Connection은 약한 참조를 만들 수 없다(C 확장 타입). 강한 참조로 등록하고
# unregister_jsonl_export()로 명시적으로 해제한다 (#34, spec 6절 결정 — 구현 중 발견).
_jsonl_exporters: dict[sqlite3.Connection, _JsonlExporter] = {}


def register_jsonl_export(conn: sqlite3.Connection, exporter: _JsonlExporter) -> None:
    """이 연결의 커밋마다 `exporter.flush()`를 부르게 등록한다 (#34)."""
    _jsonl_exporters[conn] = exporter


def unregister_jsonl_export(conn: sqlite3.Connection) -> None:
    """등록을 해제한다. 연결을 닫을 때 호출자가 부른다 (#34)."""
    _jsonl_exporters.pop(conn, None)


def _export_after_commit(conn: sqlite3.Connection) -> None:
    exporter = _jsonl_exporters.get(conn)
    if exporter is None:
        return
    try:
        exporter.flush()
    except Exception as error:  # noqa: BLE001 - 내보내기 실패가 저장을 막지 않는다 (#34)
        logger.warning("감사 로그 JSONL 내보내기 실패: %s", error)


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """`BEGIN IMMEDIATE` 트랜잭션 하나로 여러 문장을 원자적으로 묶는다 (FR-19).

    저장 장애는 `StoreNotApplied`(변경 없음 확실)와 `StoreOutcomeUnknown`(반영 여부 불명)으로
    구분한다 (`ExecutionStore` 전체의 약속). 쓰기 잠금을 얻지 못했거나 롤백까지 끝났으면
    아무것도 바뀌지 않은 것이 확실하다. `COMMIT` 자체가 실패하면 반영 여부를 알 수 없다.
    `COMMIT` 성공 뒤에는 이 연결에 등록된 JSONL 내보내기가 있으면 부른다 (#34).
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        raise StoreNotApplied(str(error)) from error
    try:
        yield
    except BaseException as original:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as rollback_error:
            raise StoreOutcomeUnknown(str(rollback_error)) from original
        raise
    else:
        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError as error:
            raise StoreOutcomeUnknown(str(error)) from error
        _export_after_commit(conn)


@contextmanager
def deferred_transaction(conn: sqlite3.Connection):
    """읽기 전용 조회 여러 번을 하나의 일관된 스냅숏으로 묶는다 (`restore_pending`, FR-19).

    쓰지 않으므로 `BEGIN IMMEDIATE`로 쓰기 잠금을 기다릴 필요가 없다. 끝나면 항상 롤백한다.
    """
    conn.execute("BEGIN DEFERRED")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")
