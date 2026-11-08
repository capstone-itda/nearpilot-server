-- core-distance-selector: 거리 선택자 하위 요청의 상위 요청 연결 (FR-09, FR-22, #253).
-- 하위 요청 행은 외부 request_id 가 없고 selector_parent_id 로 상위 요청을 가리킨다.
ALTER TABLE request ADD COLUMN selector_parent_id TEXT REFERENCES request (internal_request_id);
CREATE INDEX request_selector_parent ON request (selector_parent_id) WHERE selector_parent_id IS NOT NULL;
