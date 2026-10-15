-- db-contract-followups: 판정에 적용한 설정 전체 스냅샷을 저장할 컬럼을 추가한다 (#83).
-- save_decision이 방문자 요청에 쓴다. 서버 내부 요청은 link_internal_request가 쓴다 (#86, 5단계).

ALTER TABLE request ADD COLUMN settings_json TEXT;
