# 비활성 비콘은 수집 회차를 시작하지 못함 (`AdminRepository.start_collection`)

- 상태: 작성 중
- 담당: paiwhy (DB)
- 요구사항: FR-16, FR-01
- 따르는 결정: #138 (PR #145)

## 1. 왜

- `start_collection`은 지금 비콘이 등록돼 있으면 활성 여부와 상관없이 `APPLIED`를 돌려준다. `save_rssi`는 비활성 비콘의 프레임을 저장하지 않으므로, 비활성 비콘의 회차는 성공해도 관측이 0건으로 끝난다 (#138).

## 2. 무엇

- 입력: `start_collection(collection_id, beacon, label, started_at)`의 기존 입력.
- 출력: `AdminWriteResult(outcome, reason)`. 기존과 같다.
- 바뀌는 상태: 없음. 비활성 비콘이면 `collection_run` 행을 만들지 않는다.
- 약속: 비콘이 비활성이면 `REJECTED`, `BEACON_NOT_FOUND`로 거부한다 (#138, `shared/interfaces.py` docstring).

## 3. 제약

- 등록되지 않은 비콘과 같은 사유(`BEACON_NOT_FOUND`)를 쓴다. 새 사유를 추가하지 않는다 (#138 결정).
- 다른 거부 사유(`NODE_NOT_FOUND`, `COLLECTION_OVERLAP`, `STATE_CONFLICT`)의 순서와 조건은 바꾸지 않는다.

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/db/repositories/`
- `server/tests/db/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `api/`, `iot/`, `tools/`, `server/src/nearpilot/db/schema/` (마이그레이션 없음)

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `AdminRepository.start_collection`의 비활성 비콘 거부 | 제공 | 근접 추정 (`tools/data_collection`, dnnals) |

### 만들지 않는 것
- 비콘이 꺼질 때 진행 중인 회차를 자동으로 끝내는 것

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 비활성 비콘으로 수집 회차를 시작한다 | `REJECTED`, `BEACON_NOT_FOUND`. `collection_run` 행이 생기지 않는다 | `test_FR16_start_collection_inactive_beacon_rejected` |
| 활성 비콘으로 시작한다 | 지금처럼 `APPLIED`다 | `test_FR16_start_collection_active_beacon_applied` |
| 새 비콘 등록으로 꺼진 예전 비콘으로 시작한다 | `REJECTED`, `BEACON_NOT_FOUND` | `test_FR16_start_collection_after_beacon_swap` |
| 진행 중인 회차의 비콘이 도중에 꺼졌다 | `end_collection`은 그대로 `APPLIED`로 끝낼 수 있다 | `test_FR16_end_collection_after_beacon_deactivated` |

## 6. 결정

- 없음

## 7. 가정

- 없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-16, FR-01 | `test_FR16_start_collection_*_beacon_*` | 비활성 비콘 거부와 활성 비콘의 기존 동작 유지 |
