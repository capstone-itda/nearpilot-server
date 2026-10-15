# FR-09·FR-18 실제 DB와 판정의 통합 시나리오

- 담당: eunji719 (판정), DB와 공동
- 요구사항: FR-03, FR-09, FR-10, FR-11, FR-14, FR-18, FR-19, NFR-03
- 따르는 결정: #23, #24, #27

## 1. 왜

- 판정 코드(`DecisionPipeline`, `AccessRequestService`)는 지금까지 fake 저장소로만 검증했다. 실제 DB(`SqliteRepository`), 정책 검사(`RepositoryPolicyChecker`)와 맞물리는지 확인하지 못했다.
- fake는 실제 DB의 제약을 모른다. 요청 행의 FK, 같은 키 재전송, 점유 충돌, 승인 신청의 중복 방지가 판정의 가정과 다르면 서버가 뜬 뒤에야 드러난다.

## 2. 무엇

- 입력: 없다. 테스트만 추가한다.
- 출력: `server/tests/integration/`의 시나리오 테스트
- 바뀌는 상태: 없다. 메모리 SQLite를 테스트마다 새로 만든다.
- 약속: 판정과 DB를 실제로 연결해서 아래 시나리오가 성공한다. 실패하는 시나리오가 있으면 코드를 고치지 않고 원인을 기록해서 담당자에게 알린다.
  - 실제 DB: `SqliteRepository`, `RepositoryPolicyChecker`
  - 대신하는 것: 근접 추정, 실행(`ExecutionService`), 감사 로그, 기기 복원 여부 (`ExecutionReadiness`)

## 3. 제약

- 코드는 `server/tests/integration/`에만 둔다. 기존 코드를 고치지 않는다. 시나리오가 코드의 결함을 드러내면 테스트를 그대로 두고 담당자에게 알린다.
- 테스트 데이터는 SQL로 직접 넣는다. `AdminRepository`가 아직 없다 (`tests/db`와 같다).
- 시각은 고정한다. 난수와 실제 시계를 쓰지 않는다 (NFR-09).
- 다른 계층 구현을 새로 만들지 않는다. 대신하는 것은 `shared`의 Protocol을 만족하는 fake로 한다.

## 4. 경계

### 고치는 곳
- `server/tests/integration/`
- `specs/core-integration-real-db/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/` 전체

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `SqliteRepository` (`Repository`) | 사용 | DB |
| `RepositoryPolicyChecker` (`PolicyChecker`) | 사용 | DB (`core/policy`) |
| `DecisionPipeline`, `AccessRequestService` | 사용 | 판정 |

### 만들지 않는 것
- 실제 실행(`ExecutionService`)과 MQTT 게이트웨이를 붙인 시나리오. 서버 조립(#88) 뒤에 한다.
- `any:locker` 시나리오. 대기 구간 PR(#118)이 머지된 뒤에 한다.
- 부하·동시성 시험 (지표 3)

## 5. 예외 상황

| 시나리오 | 기대 | 테스트 |
|---|---|---|
| 공개 `open` 기기에 새 대여를 요청한다 | `OK`와 `use_id`. DB에 `RESERVED` 점유 세션과 판정 기록이 있다 | `test_FR18_새_대여는_OK이고_점유_세션을_만든다` |
| 같은 `request_id`로 다시 보낸다 | 기존 판정을 반환한다. 세션이 하나뿐이다 | `test_FR10_재전송은_기존_판정을_반환한다` |
| 같은 `request_id`에 다른 입력이다 | `REQUEST_ID_CONFLICT`. 기존 판정을 덮어쓰지 않는다 | `test_FR10_같은_키_다른_입력은_충돌이다` |
| 다른 방문자가 점유된 기기를 요청한다 | `BUSY` / `DEVICE_BUSY` | `test_FR19_점유된_기기는_다른_방문자에게_BUSY` |
| 승인이 필요한 기기를 승인 없이 요청한다 | `NEED_APPROVAL` / `APPROVAL_REQUIRED`. 점유 세션이 없다 | `test_FR14_승인_없이는_NEED_APPROVAL` |
| `request_access`를 호출한다 | `NEED_APPROVAL` / `APPROVAL_PENDING`. DB에 `pending` 신청이 하나 있다 | `test_FR03_request_access는_pending_신청을_만든다` |
| 대기 중에 `request_access`를 다시 호출한다 | 새 신청이 생기지 않는다 | `test_FR03_대기_중_재요청은_신청을_늘리지_않는다` |
| 소유자가 승인한 뒤 `request_use`를 호출한다 | `OK`. 승인 사용분이 예약된다 | `test_FR03_승인_뒤_request_use는_OK이고_사용분을_예약한다` |
| 승인이 필요 없는 기기에 `request_access`를 호출한다 | `OK`, `use_id` 없음. 신청이 없다 | `test_FR09_승인이_필요_없으면_신청하지_않는다` |
| 비공개 기기에 소유자가 아닌 계정이 `request_access`를 호출한다 | `TARGET_UNAVAILABLE`. 신청이 없다 | `test_FR14_비공개_기기는_없는_기기와_같다` |
| 호스트 응답 | 사용자 ID, 승인 ID가 없다 | `test_FR11_호스트_응답에_내부_값이_없다` |

## 6. 결정

- 공통 fixture와 데이터 넣기는 `server/tests/integration/_world.py` 하나에 둔다. 담당자가 정했다 — `tests/db/conftest.py`는 DB 테스트 전용이라 가져다 쓰지 않는다.
- 시나리오 파일은 `test_decision_real_db.py` 하나다. 담당자가 정했다 — 같은 세계(계정, 비콘, 기기)를 여러 시나리오가 공유한다.
- 근접 추정 fake는 기기별 확률을 고정해서 돌려준다. 임계값은 설정의 `threshold_locker`다.

## 7. 가정

- 실제 DB에 요청 행을 만드는 곳은 `link_request`다. 판정이 부르는 순서(⓪ 먼저)가 DB의 FK 제약을 만족한다고 가정하고 확인한다.
- 승인 결정은 `Repository.decide_approval`로 시나리오 안에서 반영한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-18·FR-19 | `test_FR18_*`, `test_FR19_*` | 새 대여, 점유 충돌, 승인 필요 |
| FR-10 | `test_FR10_*` | 재전송, 충돌 |
| FR-03·FR-09·FR-14 | `test_FR03_*`, `test_FR09_*`, `test_FR14_*` | 승인 요청, 승인 뒤 사용 |
| FR-11 | `test_FR11_호스트_응답에_내부_값이_없다` | 호스트 응답 |

- 구현은 `server/tests/integration/test_decision_real_db.py`다.
