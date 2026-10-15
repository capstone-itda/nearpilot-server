"""`db/repositories/` 공통 헬퍼. 시각 변환, ID 생성, JSON 인코딩, 트랜잭션.

시각은 UTC ISO 8601 문자열로 저장한다 (spec §6 결정). 내부 ID는 UUID4다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from nearpilot.shared.config import Settings
from nearpilot.shared.errors import StoreNotApplied, StoreOutcomeUnknown


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


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """`BEGIN IMMEDIATE` 트랜잭션 하나로 여러 문장을 원자적으로 묶는다 (FR-19).

    저장 장애는 `StoreNotApplied`(변경 없음 확실)와 `StoreOutcomeUnknown`(반영 여부 불명)으로
    구분한다 (`ExecutionStore` 전체의 약속). 쓰기 잠금을 얻지 못했거나 롤백까지 끝났으면
    아무것도 바뀌지 않은 것이 확실하다. `COMMIT` 자체가 실패하면 반영 여부를 알 수 없다.
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
