-- core-proximity-common-model: 기기별 자동 영점 개정 (FR-16, FR-22, #252).
-- 행 하나가 개정 하나다. 행을 고치거나 지우지 않는다. zero_db 가 NULL 이면 버림 개정이다.
CREATE TABLE auto_zero (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES node (node_id),
    zero_db REAL,
    vendor_model TEXT,
    vendor_firmware TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX auto_zero_node ON auto_zero (node_id, revision);
