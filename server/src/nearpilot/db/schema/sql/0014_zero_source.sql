-- core-proximity-common-model: 영점 행의 출처 (FR-16, FR-22, #307).
-- measured 는 소유자가 고정 거리에서 잰 측정 영점이고, auto 는 장소의 관측으로 찾은 자동 영점이다.
-- 버림 행은 auto 다 (다음 영점을 자동으로 찾는다). 이전 행은 모두 auto 다.
ALTER TABLE auto_zero ADD COLUMN source TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto', 'measured'));
