-- core-dashboard-queries: 대시보드가 보여 줄 기기 이름을 저장할 열을 추가한다 (FR-12A).
-- 이름은 시드 `[[nodes]].name`이 AdminRepository.set_node_name으로 쓴다. 기존 기기와 기기 발표로 새로 생긴
-- 기기는 NULL이다. 발표(upsert_announced_node)는 이 열을 쓰지 않는다.

ALTER TABLE node ADD COLUMN name TEXT;
