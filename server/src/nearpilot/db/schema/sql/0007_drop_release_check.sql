-- 센서 기반 반납 검사를 제외한다 (2026-10-26 결정, FR-20). finish_close는 검사 ID와 근거를 저장하지 않는다.
-- 배포한 운영 데이터가 없어서 이전 절차 없이 열을 지운다 (specs/db-execution-store/spec.md 가정).

ALTER TABLE use_session DROP COLUMN check_id;
ALTER TABLE use_session DROP COLUMN check_evidence;
