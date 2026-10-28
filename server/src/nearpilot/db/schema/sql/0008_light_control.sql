-- core-light-control: 조명 밝기·색 제어 (FR-04, FR-09).
-- node.light_controls는 조명 기기가 발표한 조명 제어 정보 JSON이다. 발표가 없으면 NULL이다.
-- node_command.light는 판정이 정한 조명 설정 JSON이다. 인자 없는 명령은 NULL이다.

ALTER TABLE node ADD COLUMN light_controls TEXT;
ALTER TABLE node_command ADD COLUMN light TEXT;
