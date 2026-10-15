-- db-contract-followups: request.user_id를 비울 수 있게 하고 request_kind를 추가한다 (#86).
-- SQLite는 기존 컬럼의 NOT NULL을 바꿀 수 없어 새 테이블로 옮긴다 (spec §6 결정).
-- 순서: foreign_keys OFF → 새 테이블 생성 → 행 복사 → 이전 테이블 삭제 → 이름 변경 →
-- 인덱스 재생성 → foreign_key_check (db/schema/__init__.py의 migrate()가 한다) → foreign_keys ON.

PRAGMA foreign_keys = OFF;

CREATE TABLE request_new (
    internal_request_id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES user (user_id),
    request_kind TEXT NOT NULL DEFAULT 'visitor' CHECK (request_kind IN ('visitor', 'server_internal')),
    request_id TEXT,
    input_digest TEXT NOT NULL,
    parent_internal_request_id TEXT REFERENCES request_new (internal_request_id),
    node_id TEXT REFERENCES node (node_id),
    target_spec TEXT NOT NULL,
    action TEXT NOT NULL,
    verdict TEXT,
    reason_code TEXT,
    p_max REAL,
    model TEXT,
    calib TEXT,
    policy_ver INTEGER,
    config_ver TEXT,
    decision_context TEXT,
    host_kind TEXT,
    received_at TEXT NOT NULL,
    settings_json TEXT,
    -- 서버 내부 요청의 원인 (InternalRequestCause). 방문자 요청은 NULL이다 (#86).
    internal_cause TEXT,
    -- request_kind가 visitor이면 user_id가 반드시 있다 (spec §6 결정).
    CHECK (request_kind != 'visitor' OR user_id IS NOT NULL)
);

INSERT INTO request_new (
    internal_request_id, user_id, request_kind, request_id, input_digest,
    parent_internal_request_id, node_id, target_spec, action, verdict, reason_code, p_max,
    model, calib, policy_ver, config_ver, decision_context, host_kind, received_at, settings_json
)
SELECT
    internal_request_id, user_id, 'visitor', request_id, input_digest,
    parent_internal_request_id, node_id, target_spec, action, verdict, reason_code, p_max,
    model, calib, policy_ver, config_ver, decision_context, host_kind, received_at, settings_json
FROM request;

DROP TABLE request;
ALTER TABLE request_new RENAME TO request;

CREATE UNIQUE INDEX request_external_key
    ON request (user_id, request_id)
    WHERE request_id IS NOT NULL;

PRAGMA foreign_keys = ON;
