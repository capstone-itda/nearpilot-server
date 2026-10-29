-- core-speaker-volume: 스피커 음량 제어 (FR-04, FR-09).
-- node.speaker_controls는 스피커 기기가 발표한 음량 제어 정보 JSON이다. 발표가 없으면 NULL이다.
-- node_command.speaker는 판정이 정한 스피커 음량 JSON이다. 인자 없는 명령은 NULL이다.

ALTER TABLE node ADD COLUMN speaker_controls TEXT;
ALTER TABLE node_command ADD COLUMN speaker TEXT;
