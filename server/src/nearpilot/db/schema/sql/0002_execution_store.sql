-- db-execution-store: finish_close/close_result의 검사 근거, settle_recovery의 정산 내용을
-- 저장할 컬럼을 추가한다 (specs/db-execution-store/spec.md §6 결정). 0001_initial.sql은 고치지 않는다.

ALTER TABLE use_session ADD COLUMN check_id TEXT;
ALTER TABLE use_session ADD COLUMN check_evidence TEXT NOT NULL DEFAULT '{}';

-- RecoverySettlement의 expected_node_state, expected_recovery_status, expected_commands,
-- expected_sessions, usage_settlements, use_id, closed_at, end_reason을 한 번에 직렬화해 둔다.
-- settlement_digest, approved_by_user_id, approved_at, completed_at은 0001_initial.sql의 기존 컬럼을 쓴다.
ALTER TABLE recovery ADD COLUMN settlement_json TEXT;
