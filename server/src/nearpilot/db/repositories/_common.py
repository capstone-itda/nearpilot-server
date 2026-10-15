"""`db/repositories/` 공통 헬퍼. 시각 변환, ID 생성, JSON 인코딩, 트랜잭션.

시각은 UTC ISO 8601 문자열로 저장한다 (spec §6 결정). 내부 ID는 UUID4다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


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


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """`BEGIN IMMEDIATE` 트랜잭션 하나로 여러 문장을 원자적으로 묶는다 (FR-19)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
