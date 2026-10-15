-- db-repository: 전체 스키마 (specs/db-repository/spec.md 참조)
-- 후속 작업(db-policy-check, db-execution-store, db-approval-admin, db-recovery-retention)이
-- 쓸 테이블과 컬럼을 여기서 모두 만든다. 후속 작업은 함수만 추가한다.

CREATE TABLE user (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL
);

CREATE TABLE beacon (
    beacon_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES user (user_id),
    uuid TEXT NOT NULL,
    major INTEGER NOT NULL,
    minor INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (uuid, major, minor)
);

CREATE INDEX beacon_by_user ON beacon (user_id);

-- 계정당 활성 비콘은 하나다 (FR-01, 7절 #42).
CREATE UNIQUE INDEX beacon_one_active_per_user
    ON beacon (user_id)
    WHERE is_active = 1;

CREATE TABLE place (
    place_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES user (user_id),
    name TEXT NOT NULL
);

CREATE TABLE node (
    node_id TEXT PRIMARY KEY,
    place_id TEXT REFERENCES place (place_id),
    capability TEXT NOT NULL,
    occupancy TEXT NOT NULL,
    trust_state TEXT NOT NULL,
    device_state TEXT NOT NULL,
    exclusive_group TEXT,
    release_policy TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT,
    session_id TEXT,
    session_first_seen_at TEXT
);

CREATE TABLE access_policy (
    policy_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL UNIQUE REFERENCES node (node_id),
    current_version INTEGER NOT NULL
);

CREATE TABLE policy_revision (
    policy_id TEXT NOT NULL REFERENCES access_policy (policy_id),
    version INTEGER NOT NULL,
    visibility TEXT NOT NULL,
    mode TEXT NOT NULL,
    approver_user_id TEXT REFERENCES user (user_id),
    approval_ttl_sec INTEGER,
    approval_mode TEXT NOT NULL,
    max_uses INTEGER,
    PRIMARY KEY (policy_id, version)
);

-- 접근 승인. create_approval_request/decide_approval이 이 작업에서 쓴다.
-- created_at, pending_until은 architecture.md가 명시하지 않은 구현 컬럼이다 (spec §6 결정).
CREATE TABLE approval (
    approval_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES node (node_id),
    requester_user_id TEXT NOT NULL REFERENCES user (user_id),
    internal_request_id TEXT NOT NULL,
    approved_by_user_id TEXT REFERENCES user (user_id),
    action TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    pending_until TEXT,
    valid_until TEXT,
    used_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (policy_id, policy_version) REFERENCES policy_revision (policy_id, version)
);

-- create_approval_request의 중복 신청 검사(요청자·기기·동작·정책 버전)에 쓴다.
CREATE INDEX approval_scope ON approval (requester_user_id, node_id, action, policy_version);

CREATE TABLE approval_usage (
    usage_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL REFERENCES approval (approval_id),
    internal_request_id TEXT NOT NULL,
    command_id TEXT,
    state TEXT NOT NULL
);

CREATE INDEX approval_usage_by_approval ON approval_usage (approval_id);
CREATE INDEX approval_usage_by_request ON approval_usage (internal_request_id);

-- 진행 중 수집 회차 조회만 이 작업에서 쓴다. 회차 쓰기는 db-approval-admin이 한다 (#41).
CREATE TABLE collection_run (
    collection_id TEXT PRIMARY KEY,
    beacon_id TEXT NOT NULL REFERENCES beacon (beacon_id),
    label TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX collection_run_by_beacon ON collection_run (beacon_id, started_at);

-- label은 rssi_observation에 두지 않는다. 연결한 수집 회차(collection_run.label)에서 읽는다 (#41).
CREATE TABLE rssi_observation (
    obs_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES node (node_id),
    beacon_id TEXT NOT NULL REFERENCES beacon (beacon_id),
    rssi REAL NOT NULL,
    frame_ts TEXT NOT NULL,
    received_at TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    collection_id TEXT REFERENCES collection_run (collection_id),
    UNIQUE (node_id, session_id, seq, beacon_id)
);

CREATE INDEX rssi_observation_by_beacon_time ON rssi_observation (beacon_id, received_at);

CREATE TABLE request (
    internal_request_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES user (user_id),
    request_id TEXT,
    input_digest TEXT NOT NULL,
    parent_internal_request_id TEXT REFERENCES request (internal_request_id),
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
    received_at TEXT NOT NULL
);

-- 외부 request_id는 값이 있는 행에만 UNIQUE다. 비운 행(NULL)은 여러 개 있을 수 있다 (FR-22).
CREATE UNIQUE INDEX request_external_key
    ON request (user_id, request_id)
    WHERE request_id IS NOT NULL;

CREATE TABLE use_session (
    use_id TEXT PRIMARY KEY,
    internal_request_id TEXT NOT NULL REFERENCES request (internal_request_id),
    user_id TEXT NOT NULL REFERENCES user (user_id),
    node_id TEXT NOT NULL REFERENCES node (node_id),
    state TEXT NOT NULL,
    end_reason TEXT,
    rental_due_at TEXT,
    closed_at TEXT,
    release_policy TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX use_session_by_node ON use_session (node_id);
CREATE INDEX use_session_by_user ON use_session (user_id);
CREATE INDEX use_session_by_request ON use_session (internal_request_id);

-- CommandRecord의 모든 필드에 대응하는 컬럼을 지금 만든다(담당자 지시 3).
-- 행 쓰기 함수는 db-execution-store가 만든다. 이 작업은 테이블만 만든다.
CREATE TABLE node_command (
    command_id TEXT PRIMARY KEY,
    internal_request_id TEXT NOT NULL REFERENCES request (internal_request_id),
    node_id TEXT NOT NULL REFERENCES node (node_id),
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    execution_kind TEXT NOT NULL,
    use_id TEXT REFERENCES use_session (use_id),
    usage_id TEXT REFERENCES approval_usage (usage_id),
    reserved_at TEXT,
    send_started_at TEXT,
    send_attempted INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    occurred_at TEXT,
    evidence TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX node_command_by_request ON node_command (internal_request_id);
CREATE INDEX node_command_by_node ON node_command (node_id);

-- 복구 건. 이 작업은 테이블만 만든다 (#17, #20).
CREATE TABLE recovery (
    recovery_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES node (node_id),
    use_id TEXT REFERENCES use_session (use_id),
    internal_request_ids TEXT NOT NULL DEFAULT '[]',
    command_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    approved_by_user_id TEXT REFERENCES user (user_id),
    approved_at TEXT,
    settlement_digest TEXT,
    completed_at TEXT
);

CREATE INDEX recovery_by_node ON recovery (node_id);

CREATE TABLE audit_log (
    log_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    internal_request_id TEXT REFERENCES request (internal_request_id),
    command_id TEXT,
    step TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX audit_log_by_request ON audit_log (internal_request_id);
