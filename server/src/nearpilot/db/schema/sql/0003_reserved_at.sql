-- db-contract-followups: reserve_if_free가 저장한 예약 시각을 보존할 컬럼을 추가한다 (#80).
-- 대여형은 use_session.reserved_at, 승인이 필요한 공용형은 approval_usage.reserved_at에 저장한다.
-- 쓰기가 없는 결과(공용형 open, 거부)는 저장할 행이 없어 reserved_at이 없다.

ALTER TABLE use_session ADD COLUMN reserved_at TEXT;
ALTER TABLE approval_usage ADD COLUMN reserved_at TEXT;
