# 정책 검사 (`PolicyChecker`)

- 상태: 작성 중
- 담당: paiwhy (DB)
- 요구사항: FR-03, FR-14, FR-18
- 따르는 결정: #23, PR #45, PR #52, #15

## 1. 왜

- `shared/interfaces.py`에는 판정(core/decision)이 쓸 `PolicyChecker`가 있다. `core/policy/`에는 구현이 없다.
- 구현이 없으면 판정 파이프라인의 ④(정책 검사)는 fake로만 동작한다. `NEED_APPROVAL`과 `DENY`를 실제 정책·승인 상태로 판단하지 못한다.

## 2. 무엇

- 입력: `user_id`, `node_id`, `Action`, `now`
- 출력: `PolicyCheckResult` (`verdict`, `reason`, `policy_version`, `approval_id`)
- 바뀌는 상태: 없음. 저장소를 바꾸지 않는다. 승인 사용분을 예약하지 않는다.
- 약속:
  - 정책이 없는 기기는 `DENY`를 반환한다. `policy_version`은 `None`이다.
  - `open` 정책은 `OK`를 반환한다. `approval_id`는 `None`이다.
  - `restricted` 정책에서 시간상 유효하고 남은 횟수가 있는 승인이 있으면 `OK`와 그 `approval_id`를 반환한다.
  - `restricted` 정책에서 시간상 유효하지만 남은 횟수가 0인 승인이 있으면 `NEED_APPROVAL`/`APPROVAL_EXHAUSTED`를 반환한다.
  - 그 외 모든 경우(유효한 승인이 전혀 없음 — 승인이 없거나, 대기 중이거나, 거부됐거나, 시간 만료됐거나, 정책 버전이 바뀌었거나, 다른 동작의 승인만 있는 경우)는 지금은 `NEED_APPROVAL`/`APPROVAL_REQUIRED`로 뭉뚱그린다. `Repository`에 승인 이력 조회 접점이 없어서 더 세분화할 수 없다 (7절 가정, #60).
  - 검사 결과로 다른 기기를 고르지 않는다. 입력받은 `node_id`만 검사한다 (FR-18).

## 3. 제약

- `core/policy`는 `core` 계층이다. SQL을 직접 쓰지 않고 `shared.Repository` Protocol에만 의존한다 (`AGENTS.md` 의존 규칙 4).
- 정책 모드, 승인 범위, 정책 버전 비교 규칙은 `PolicyChecker.check()` 안에 둔다. 판정(core/decision)은 순서와 중단만 맡는다 (`interfaces.py` `PolicyChecker` docstring).
- ③에서 확정한 대상만 검사한다. 검사 결과로 다른 기기를 고르지 않는다 (FR-18).
- 저장소를 바꾸지 않는다. 승인 사용분 예약은 `reserve_if_free()`의 몫이다 (`interfaces.py`).
- 반환하는 `verdict`는 `OK`, `NEED_APPROVAL`, `DENY` 중 하나다 (`PolicyCheckResult` docstring).
- `NEED_APPROVAL`의 사유는 `APPROVAL_REQUIRED`, `APPROVAL_PENDING`, `APPROVAL_REQUEST_EXPIRED`, `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`, `APPROVAL_EXHAUSTED`, `ACTION_NOT_ALLOWED` 중 하나다 (`PolicyCheckResult` docstring).
- 정책 변경은 진행 중인 점유에 소급하지 않는다. `valid_approval()`에 현재 정책 버전을 넘겨서, 버전이 바뀐 승인은 자동으로 무효 취급된다 (FR-14).
- 결과에 사용자 ID 필드를 넣지 않는다 (FR-11, `PolicyCheckResult` docstring).
- 승인이 필요 없거나 유효한 승인이 없으면 `approval_id`는 `None`이다 (`PolicyCheckResult` docstring).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/policy/`
- `server/tests/core/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/lifecycle/`, `core/proximity/`, `core/allocation/`, `api/`, `db/`, `iot/`, `tools/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `PolicyChecker.check` | 제공 | 판정 (core/decision) |
| `Repository.current_policy` | 사용 | DB |
| `Repository.valid_approval` | 사용 | DB |

### 만들지 않는 것
후속 작업이 맡는다.

| 후속 작업 | 내용 | 근거 |
|---|---|---|
| (미정, 이슈 #60 결정 뒤) | `APPROVAL_PENDING`·`APPROVAL_REQUEST_EXPIRED`·`APPROVAL_REJECTED`·`APPROVAL_EXPIRED`·`ACTION_NOT_ALLOWED` 세분화. `Repository.approvals_in_scope()` 등 새 조회 접점이 필요하다 | #60 |

- `Repository` 구현 자체 — `db-repository`에서 함 (완료)
- 승인 사용분 예약, 승인 신청·결정 저장 — `reserve_if_free()`, `create_approval_request()`, `decide_approval()`의 몫 (완료)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 정책이 없는 기기를 검사한다 | `DENY`를 반환한다. `policy_version`은 `None`이다 | `test_FR14_check_no_policy` |
| `open` 정책인 기기를 검사한다 | `OK`를 반환한다. `approval_id`는 `None`이다 | `test_FR14_check_open` |
| `restricted` 정책에서 유효하고 남은 횟수가 있는 승인이 있다 | `OK`와 그 `approval_id`를 반환한다 | `test_FR14_check_valid_approval` |
| `restricted` 정책에서 유효하지만 남은 횟수가 0인 승인이 있다 | `NEED_APPROVAL`/`APPROVAL_EXHAUSTED`를 반환한다 | `test_FR14_check_exhausted` |
| `restricted` 정책에서 승인이 전혀 없다 | `NEED_APPROVAL`/`APPROVAL_REQUIRED`를 반환한다 | `test_FR14_check_no_approval` |
| `restricted` 정책에서 `valid_approval()`이 `None`을 반환하는 그 외 모든 경우(대기·거부·만료·버전바뀜·동작다름은 구분하지 않는다) | `NEED_APPROVAL`/`APPROVAL_REQUIRED`를 반환한다 | `test_FR14_check_falls_back_to_required` |
| 승인이 여럿이어도 `valid_approval()`이 고른 승인 하나만 본다 | 추가 조회를 하지 않는다. 반환값을 그대로 쓴다 | `test_FR14_check_uses_single_valid_approval` |
| 입력 `node_id` 외의 기기를 검사하지 않는다 | `Repository`에 `node_id` 인자만 그대로 전달한다. 다른 기기를 조회하지 않는다 | `test_FR18_check_does_not_substitute_node` |
| 어떤 경우에도 저장소를 바꾸지 않는다 | fake `Repository`에 쓰기 메서드가 없어도 `check()`가 예외 없이 끝난다 | `test_FR14_check_does_not_write` |

## 6. 결정

- 정책 없음과 정책이 허용하지 않는 경우 모두 `ReasonCode.POLICY_DENIED`를 쓴다 — `AccessMode`는 `open`·`restricted` 둘뿐이고 `restricted`는 항상 `OK`나 `NEED_APPROVAL`로 이어지므로, `DENY`는 정책이 아예 없는 경우뿐이다. 두 경우를 가리키는 별도 사유 코드가 `enums.py`에 없다.
- `Repository.current_policy()`가 `LookupError`를 던지면 정책 없음으로 본다 — `db-repository`의 `current_policy()` 구현이 정책 없는 기기에 `LookupError`를 던지기로 결정했다.
- 유효 승인의 소진 여부는 `Approval.remaining_uses == 0`으로 판단한다. `valid_approval()`은 시간상 유효성만 보고 남은 횟수는 보지 않으므로, `core/policy`가 직접 확인한다 — FR-14 "서버는 사용 횟수를 넘은 요청을... 거부한다"를 `core/policy`가 맡는다. `remaining_uses`가 `None`이면 무제한이므로 소진이 아니다.
- 장소 소유자·승인자 본인의 요청도 특별 취급하지 않는다. 일반 요청자와 같은 절차로 검사한다 — 문서에 근거가 없어 암묵적 권한 상승을 넣지 않는 쪽을 택했다 (4-3, 가정으로 유지).
- 승인이 여럿이어도 `valid_approval()`이 반환한 승인 하나만 쓴다. 여러 승인 중 어느 것을 "사유" 판단에 쓸지 고르는 규칙은 세분화 작업(이슈 #60)의 몫이다.

## 7. 가정

- `NEED_APPROVAL`의 사유 중 `APPROVAL_PENDING`, `APPROVAL_REQUEST_EXPIRED`, `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`, `ACTION_NOT_ALLOWED`는 지금 구분하지 않는다. `Repository.valid_approval()`은 "유효한 승인 또는 `None`"만 반환해서, `None`일 때의 이유를 알 수 없다. 승인 이력을 돌려주는 새 조회 접점이 필요하며 이는 `shared` 변경이라 팀 결정이 필요하다. 담당자가 이슈 #60을 올렸다. 결정이 나오면 후속 작업에서 `approvals_in_scope()` 등으로 세분화한다 — #60
- 장소 소유자·승인자 본인이 `restricted` 기기를 쓸 때도 승인이 필요한지는 문서에 없다. 특별 취급 없이 진행한다. 담당자 확인 필요 — 미결

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-14 | `test_FR14_check_*` | 정책 없음, `open`, 유효 승인, 소진, 승인 없음, 그 외 뭉뚱그림 |
| FR-18 | `test_FR18_check_does_not_substitute_node` | 입력받은 `node_id`만 검사하고 다른 기기를 고르지 않는다 |

- fake `Repository`로 확인한다. 1번 작업(`db-repository`)의 실제 구현이 없어도 테스트가 끝난다.
