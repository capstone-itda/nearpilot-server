-- 승인 신청은 조작 한도에서 제외하되 기존 외부 키를 유지한다 (FR-03, FR-22).
-- 기존 행은 목적을 알 수 없으므로 NULL로 두고 보수적으로 한도에 포함한다.
ALTER TABLE request ADD COLUMN purpose TEXT
    CHECK (purpose IS NULL OR purpose IN ('use', 'access'));
