-- iot-device-descriptor: 기기 선언과 선언 해시 (#245, FR-04).
-- node.descriptor는 검증한 기기 선언 JSON, node.descriptor_hash는 kind·occupancy·actions의 SHA-256이다.
-- 기존 행은 Python 후처리(nearpilot.db.schema._backfill_descriptor)가 v2 템플릿으로 채우고
-- light_controls·speaker_controls 열을 지운다. node.capability 열은 종류 이름을 담는다. 이름은 0011이 kind로 바꾼다.

ALTER TABLE node ADD COLUMN descriptor TEXT;
ALTER TABLE node ADD COLUMN descriptor_hash TEXT;
