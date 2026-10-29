"""평가 스크립트의 읽기 전용 DB 연결 (specs/db-evaluation-foundation/spec.md §6 "DB 연결").

`nearpilot.db.schema.connect`는 쓰지 않는다. 그 함수는 `PRAGMA user_version`을 보고 마이그레이션
DDL을 실행할 수 있는 쓰기 연결이다. 평가 스크립트가 운영 DB 파일을 열 때 의도치 않게 스키마를
바꾸면 안 된다.
"""

from __future__ import annotations

import sqlite3

# `db/schema/sql/*.sql` 마이그레이션 파일 수와 같다. `db/schema/`에 새 마이그레이션이 추가되면
# 이 값도 같이 올린다 — `test_NFR13_expected_schema_version_matches_migrations`가 어긋남을 잡아낸다.
EXPECTED_SCHEMA_VERSION = 9


class SchemaVersionMismatch(RuntimeError):
    """DB의 `PRAGMA user_version`이 이 도구가 기대하는 버전과 다르다."""


def open_readonly(
    db_path: str, *, expected_version: int = EXPECTED_SCHEMA_VERSION
) -> sqlite3.Connection:
    """`db_path`를 읽기 전용 URI 연결로 연다. 스키마 버전을 확인한 뒤에만 돌려준다.

    `file:<경로>?mode=ro`는 SQLite 자체가 쓰기 문장을 거부하는 연결이다. 연결 직후
    `PRAGMA user_version`을 확인해서, 다른 스키마를 기대하며 쿼리를 날리다 조용히 틀린 결과를
    내는 상황을 막는다. 버전이 다르면 연결을 닫고 `SchemaVersionMismatch`를 올린다.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != expected_version:
            raise SchemaVersionMismatch(
                f"DB 스키마 버전 {version}이 이 도구가 기대하는 {expected_version}과 다르다: {db_path}"
            )
    except BaseException:
        conn.close()
        raise
    return conn
