# FR-14 승인 필요 사유 7개의 실제 DB 통합 시나리오

- 담당: eunji719 (판정)
- 요구사항: FR-14, FR-18
- 따르는 결정: #23, #60

## 1. 왜

- 승인 필요 사유 7개(`APPROVAL_REQUIRED`, `ACTION_NOT_ALLOWED`, `APPROVAL_PENDING`, `APPROVAL_REQUEST_EXPIRED`, `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`, `APPROVAL_EXHAUSTED`)를 `core-policy-reasons`가 구현했지만, 실제 DB와 판정을 합친 시나리오는 3개만 있다. 나머지 4개(`ACTION_NOT_ALLOWED`, `APPROVAL_REQUEST_EXPIRED`, `APPROVAL_EXPIRED`, `APPROVAL_EXHAUSTED`)는 fake 저장소로만 확인했다.

## 2. 무엇

- 입력: 실제 SQLite 위의 승인 신청, 승인, 시간 경과, 사용
- 출력: `request_use` 판정의 `NEED_APPROVAL`과 사유
- 바뀌는 상태: 없다. 테스트마다 새 메모리 SQLite를 만든다.
- 약속:
  - 사유 7개가 각각 실제 DB 상태에서 나온다. 이미 있는 3개는 `test_decision_real_db.py`가 맡는다.
  - 시간은 요청의 `received_at`으로 움직인다. 시계를 따로 바꾸지 않는다.
  - 사유가 `NEED_APPROVAL`이면 점유 세션이 생기지 않는다.

## 3. 제약

- 코드를 고치지 않는다. 실패하면 코드의 결함이다. 테스트를 맞추지 않고 담당자에게 알린다 (`test_decision_real_db.py`와 같은 원칙).
- 시간 값은 설정(`approval_pending_ttl_sec`)과 정책(`approval_ttl_sec`)에서 읽는다. 테스트에 값을 박지 않는다.

## 4. 경계

### 고치는 곳
- `server/tests/integration/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `PolicyChecker.check` (`RepositoryPolicyChecker`) | 사용 | DB |
| `Repository.approvals_in_scope`, `decide_approval` | 사용 | DB |

### 만들지 않는 것
- 호스트 응답 문구 확인
- 소유자 알림 전달

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 신청이 대기 중이다 | `APPROVAL_PENDING` | `test_FR14_대기_중인_신청은_APPROVAL_PENDING` |
| 대기 신청이 만료됐다 | `APPROVAL_REQUEST_EXPIRED` | `test_FR14_대기_신청이_만료되면_APPROVAL_REQUEST_EXPIRED` |
| 승인의 유효시간이 지났다 | `APPROVAL_EXPIRED` | `test_FR14_승인의_유효시간이_지나면_APPROVAL_EXPIRED` |
| 승인 횟수를 다 썼다 | `APPROVAL_EXHAUSTED` | `test_FR14_횟수를_다_쓰면_APPROVAL_EXHAUSTED` |
| 다른 동작의 승인만 있다 | `ACTION_NOT_ALLOWED` | `test_FR14_다른_동작의_승인만_있으면_ACTION_NOT_ALLOWED` |

## 6. 결정

- 요청 시각에 맞춰 비콘 관측과 기기의 마지막 수신 시각을 다시 넣는다 — 시간이 지나면 ①과 ② 온라인 검사가 먼저 거부해서 ④에 닿지 못한다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-14 | 위 5개 | 사유 4개가 실제 DB 상태에서 나온다. `APPROVAL_PENDING`은 시간 경과 전 대조용이다 |
