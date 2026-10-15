# 승인 필요 사유 구분 (`PolicyChecker`)

- 상태: 완료
- 담당: paiwhy (DB)
- 요구사항: FR-03, FR-14, FR-18
- 따르는 결정: #23, #60 (PR #70)

## 1. 왜

- 지금 `PolicyChecker.check()`는 유효한 승인이 없으면 `APPROVAL_EXHAUSTED`를 뺀 모든 경우를 `APPROVAL_REQUIRED` 하나로 반환한다 (`specs/core-policy-check/spec.md` 7절 가정).
- 그래서 판정과 호스트는 "승인 대기 중", "거부됨", "만료됨", "다른 동작의 승인만 있음"을 구분하지 못한다. 방문자는 이미 신청했는데도 다시 신청하라는 안내를 받는다.
- #60으로 `Repository.approvals_in_scope()`가 생겼고 DB 구현도 main에 들어왔다 (PR #134).

## 2. 무엇

- 입력: `check(user_id, node_id, action, now)`. 기존과 같다.
- 출력: `PolicyCheckResult(verdict, reason, policy_version, approval_id)`. 기존과 같다.
- 바뀌는 상태: 없음. 저장소를 바꾸지 않는다.
- 약속:
  - `NEED_APPROVAL`의 사유를 7개(`APPROVAL_REQUIRED`, `ACTION_NOT_ALLOWED`, `APPROVAL_PENDING`, `APPROVAL_REQUEST_EXPIRED`, `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`, `APPROVAL_EXHAUSTED`)로 구분한다.
  - 정책 없음(`DENY`), `open`(`OK`), 유효 승인(`OK`와 `approval_id`)의 결과는 바뀌지 않는다.

## 3. 제약

- `core` 계층이므로 SQL을 쓰지 않는다. `Repository` Protocol(`current_policy`, `valid_approval`, `approvals_in_scope`)만 쓴다 (`AGENTS.md` 의존 규칙 4).
- 유효한 승인이 없을 때 사유는 아래 순서로 고른다 (`architecture.md` §4 "승인 저장 인터페이스", #60).
  1. 현재 정책 버전의 승인만 본다. 없으면 `APPROVAL_REQUIRED`다.
  2. 그중 요청 동작과 같은 승인만 본다. 없으면 `ACTION_NOT_ALLOWED`다.
  3. 만료되지 않은 `pending` 승인(`now < pending_until`)이 있으면 `APPROVAL_PENDING`이다.
  4. 없으면 생성 시각이 가장 늦은 승인으로 정한다. 만료된 `pending`도 포함한다.
     - `pending`: `APPROVAL_REQUEST_EXPIRED`
     - `denied`: `APPROVAL_REJECTED`
     - `approved`이고 유효시간이 지났다: `APPROVAL_EXPIRED`
     - `approved`이고 남은 횟수가 0이다: `APPROVAL_EXHAUSTED`
- 사유를 고르는 일로 다른 기기를 고르거나 승인 사용분을 예약하지 않는다 (FR-18, `interfaces.py`).
- 정책 변경은 진행 중인 점유에 소급하지 않는다 (FR-14).
- 결과에 사용자 ID를 넣지 않는다 (FR-11).
- 승인 조회가 실패하면 예외를 그대로 올린다. 판정이 `INTERNAL_ERROR`로 처리한다 (#25).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/policy/`
- `server/tests/core/` (파일 이름에 policy를 넣는다)

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `db/`, 다른 `core/` 폴더, `api/`, `iot/`, `tools/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `PolicyChecker.check` | 제공 | 판정 (④), 상태 관리 (공용 조명 유지 권한, #87) |
| `Repository.approvals_in_scope` | 사용 | DB |

### 만들지 않는 것
| 후속 | 내용 | 근거 |
|---|---|---|
| — | 호스트 응답 문구 | 판정이 정한다 |
| — | 공용 조명 유지 권한의 해석 | 상태 관리가 한다 (#87) |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 정책이 없다 | `DENY`, `policy_version=None` (기존과 같음) | `test_FR14_policy_missing_deny` |
| `open` 정책이다 | `OK` (기존과 같음) | `test_FR14_policy_open_ok` |
| 유효하고 남은 횟수가 있는 승인이 있다 | `OK`와 `approval_id` (기존과 같음) | `test_FR14_policy_valid_approval_ok` |
| 승인 이력이 전혀 없다 | `APPROVAL_REQUIRED` | `test_FR14_reason_required_no_history` |
| 이전 정책 버전의 승인만 있다 | `APPROVAL_REQUIRED` | `test_FR14_reason_required_old_version` |
| 현재 버전이지만 다른 동작의 승인만 있다 | `ACTION_NOT_ALLOWED` | `test_FR14_reason_action_not_allowed` |
| 만료되지 않은 대기 신청이 있다 | `APPROVAL_PENDING` | `test_FR14_reason_pending` |
| 예전 거부와 새 대기 신청이 함께 있다 | 대기가 우선한다. `APPROVAL_PENDING` | `test_FR14_reason_pending_over_older_denied` |
| 가장 최근 신청이 대기 만료됐다 | `APPROVAL_REQUEST_EXPIRED` | `test_FR14_reason_request_expired` |
| 가장 최근 신청이 거부됐다 | `APPROVAL_REJECTED` | `test_FR14_reason_rejected` |
| 가장 최근 승인이 유효시간이 지났다 | `APPROVAL_EXPIRED` | `test_FR14_reason_expired` |
| 가장 최근 승인이 횟수를 다 썼다 | `APPROVAL_EXHAUSTED` | `test_FR14_reason_exhausted` |
| 가장 최근 승인이 유효시간도 지났고 횟수도 0이다 | `APPROVAL_EXPIRED` (시간을 먼저 본다) | `test_FR14_reason_expired_before_exhausted` |
| 예전 승인은 만료, 최근 신청은 거부다 | 가장 최근 것으로 `APPROVAL_REJECTED` | `test_FR14_reason_latest_wins` |
| 생성 시각이 같은 승인이 여럿이다 | `approval_id` 순으로 정렬해 결과가 항상 같다 | `test_FR14_reason_tie_deterministic` |
| `approvals_in_scope` 조회가 실패한다 | 예외를 그대로 올린다 | `test_FR14_reason_lookup_error_raises` |
| 결과에 사용자 ID 필드가 있는가 | 없다 | `test_FR11_policy_result_has_no_user` |

## 6. 결정

- 유효한 승인의 `OK`는 지금처럼 `valid_approval()`로 판단한다. `approvals_in_scope()`는 유효한 승인이 없을 때만 부른다 — 기존 동작과 테스트를 그대로 유지하고, 정상 경로의 조회 횟수가 늘지 않는다.
- `approved`이고 유효시간도 지났고 횟수도 0이면 `APPROVAL_EXPIRED`를 고른다 — 다시 승인받아야 하는 직접 원인이 시간이다. 횟수를 새로 받아도 기간이 지나 있으면 쓸 수 없다.
- 생성 시각이 같으면 `approval_id` 오름차순으로 정렬해 마지막 것을 고른다 — 같은 입력에 같은 결과를 낸다 (FR-18).

## 7. 가정

- 없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-14 | `test_FR14_reason_*`, `test_FR14_policy_*` | 사유 7개의 선택 규칙과 기존 결과 유지 |
| FR-18 | `test_FR14_reason_tie_deterministic` | 같은 입력에 같은 결과 |
| FR-11 | `test_FR11_policy_result_has_no_user` | 결과에 사용자 ID가 없다 |

- fake `Repository`로 확인한다. 가능하면 `tests/integration/`에 실제 SQLite 저장소로 사유 2~3개를 확인하는 테스트를 하나 둔다.
