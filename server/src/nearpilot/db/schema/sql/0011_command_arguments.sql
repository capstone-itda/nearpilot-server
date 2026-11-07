-- core-device-actions: 선언 기반 동작 인자 (#245, FR-09).
-- node_command.arguments는 판정이 정한 동작 인자 JSON이다. 인자 없는 명령은 NULL이다.
-- 기존 node_command.light·speaker는 키(brightness, color, volume)가 arguments와 같아서 그대로 옮기고 지운다.
-- node.capability 열은 기기 선언의 종류 이름이라 kind로 바꾼다.

ALTER TABLE node_command ADD COLUMN arguments TEXT;
UPDATE node_command SET arguments = COALESCE(light, speaker);
ALTER TABLE node_command DROP COLUMN light;
ALTER TABLE node_command DROP COLUMN speaker;
ALTER TABLE node RENAME COLUMN capability TO kind;
