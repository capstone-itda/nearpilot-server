# 승인 조회·결정 서비스

- 담당: eunji719 (앱)
- 요구사항: FR-03, FR-14, NFR-07, NFR-08
- 따르는 결정: #186

## 1. 왜

앱의 승인 화면과 알림은 있지만, 승인 REST가 부를 CORE 접점이 없다. `Repository`에는 결정 반영(`decide_approval`)만 있고, 승인 권한자의 대기 목록과 승인 한 건을 조회할 수 없다 (FR-03).

## 2. 무엇

- 입력: 인증한 계정 ID. 승인 ID. 결정(`approved` 또는 `denied`)
- 출력: `ApprovalService`의 `pending_for`, `get`, `decide`. 승인 권한자의 만료되지 않은 대기 승인 목록, 승인 한 건, 결정 결과와 `ApprovalDecisionReason`
- 바뀌는 상태: `decide`가 `Repository.decide_approval`로 승인 상태와 `valid_until`을 반영한다. 조회는 저장소를 바꾸지 않는다
- 약속:
  - 승인 권한, 대기 상태, 정책 버전은 CORE가 검증한다. 권한이 없는 계정의 결정은 저장소에 보내지 않는다 (FR-03).
  - 같은 결정의 재호출은 기존 결과를 성공으로 반환한다. 재호출에서도 권한을 검사한다 (FR-03).
  - 반환값에 요청자 정보를 넣지 않는다 (NFR-08).
  - 결과를 바꾸지 않고 REST가 그대로 옮길 수 있는 사유 코드로 반환한다 (NFR-07).

## 3. 제약

- `ApprovalService`는 `CoreService`와 따로 둔다. MCP 도구 계약을 바꾸지 않는다 (#186).
- 결정은 `Repository.decide_approval`로만 반영한다. 쓰기 경로를 새로 만들지 않는다 (AGENTS.md 의존 규칙 5).
- 승인 한 건은 REST 응답에 필요한 `approval_id`, `node_id`, `action`, `status`, `created_at`, 대기 만료 시각, `valid_until`을 담는다 (architecture.md §5 REST "승인").
- 승인 권한은 승인이 만들어진 정책 리비전의 `approver_user_id`다 (architecture.md §6 `policy_revision`, FR-14).
- 현재 시각은 주입한 시계에서 받는다. 시간 값은 코드 상수로 쓰지 않는다.

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/approval/`
- `server/src/nearpilot/shared/interfaces.py`, `shared/models.py`: `ApprovalService`, `Repository`의 승인 조회 세 함수, 반환 모델 두 개를 더하기만 한다
- `server/src/nearpilot/db/repositories/repository.py`: 승인 조회 세 함수를 더하기만 한다
- `server/tests/core/`, `server/tests/db/`, `server/tests/shared/`, `server/tests/integration/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/` (위 추가는 제외), `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/api/`, `server/src/nearpilot/runtime/`, `server/src/nearpilot/core/decision/`, `core/policy/`, DB 스키마

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `ApprovalService.pending_for`, `get`, `decide` (§4 "승인 요청과 소유자 결정") | 제공 | API (`api/rest/`), 상태 관리 (서버 조립) |
| `Repository.decide_approval` | 사용 | DB |
| `Repository.pending_approvals_for`, `approval`, `is_approver` (이 작업이 더한다) | 사용 | DB |

### 만들지 않는 것
- 승인 REST (`/approvals`). 후속 spec(`api-rest-approval`)이 맡는다.
- 서버 조립에 `ApprovalService`를 만들고 라우터에 넘기는 일. 상태 관리가 맡는다.
- 새 승인 요청의 알림 전달 (FR-03 미결)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 승인 권한이 없는 계정이 목록을 조회했다 | 빈 목록 | `test_FR03_pending_for_only_approver` |
| 대기 만료가 지났거나 이미 결정한 승인 | 목록에 넣지 않는다 | `test_FR03_pending_excludes_expired_and_decided` |
| 없는 승인 ID를 조회하거나 결정했다 | `NOT_FOUND`. 저장소에 결정을 보내지 않는다 | `test_FR03_get_unknown_is_not_found`, `test_FR03_decide_unknown_is_not_found` |
| 승인 권한이 없는 계정이 조회하거나 결정했다 | `NOT_AUTHORIZED`. 승인 내용을 반환하지 않고, 저장소에 결정을 보내지 않는다 | `test_FR03_get_by_non_approver_is_not_authorized`, `test_FR03_decide_by_non_approver_is_not_authorized` |
| 같은 결정을 다시 보냈다 | `ALREADY_APPLIED`와 기존 승인 | `test_FR03_decide_same_twice_is_already_applied` |
| 반대 결정, 정책 버전 불일치, 대기 만료 | 저장소의 사유를 그대로 반환한다 | `test_FR03_decide_passes_store_reason`, `test_FR03_real_db_decide_reasons` |
| 결정 값이 `approved`·`denied`가 아니다 | `ValueError`. 저장소에 보내지 않는다 | `test_FR03_decide_rejects_pending_value` |
| 저장소 조회가 실패했다 | `StoreError`를 그대로 올린다. API가 `INTERNAL_ERROR`로 바꾼다 | `test_FR03_store_error_propagates` |
| 정책 리비전에 승인 권한자가 없다 (`approver_user_id`가 없음) | 누구도 권한이 없다 | `test_FR14_no_approver_means_no_authority` |

## 6. 결정

- 경계 밖 폴더(`shared/`, `db/repositories/`)까지 이 작업에 넣는다 (담당자 결정, 팀 확인) — #186이 정한 접점을 만들어야 승인 REST를 붙일 수 있다. `shared`에는 더하기만 하고 기존 계약을 바꾸지 않는다.
- 반환 모델은 `OwnerApproval`과 `OwnerApprovalResult`다 — `ApprovalRecord`는 `node_id`가 없고 `core/policy`가 쓰므로 바꾸지 않는다. `OwnerApproval`은 승인 ID, 기기 ID, 동작, 승인 상태, 생성 시각, 대기 만료 시각, 유효 만료 시각만 담는다. `OwnerApprovalResult`는 `ApprovalDecisionReason`과 승인 한 건(없으면 `None`)을 담는다.
- 저장소 조회는 셋이다 — `pending_approvals_for(user_id, now)`(대기 목록), `approval(approval_id)`(한 건), `is_approver(user_id, approval_id)`(권한). 권한 검사를 `is_place_owner`처럼 bool로 따로 두어 승인 권한자 ID를 반환값에 넣지 않는다.
- 권한은 승인이 만들어진 정책 리비전의 `approver_user_id`로 본다 — 정책이 바뀐 뒤의 결정은 어차피 `POLICY_VERSION_MISMATCH`다. 새 리비전의 승인 권한자는 옛 승인을 결정할 권한이 없다.
- 대기 목록은 `pending`이고 대기 만료 시각이 지나지 않은 승인이다. 정책 버전이 바뀐 승인도 넣는다 — 문서가 "만료되지 않은 대기 승인"으로만 정했다. 결정하면 `POLICY_VERSION_MISMATCH`가 된다.
- 대기 목록은 생성 시각, 승인 ID 순이다.
- 검사 순서는 존재(`NOT_FOUND`), 권한(`NOT_AUTHORIZED`), 결정 반영이다. 권한이 없으면 승인 내용을 반환하지 않는다.
- `decide`는 결과에 결정 뒤의 승인 한 건을 다시 읽어 담는다 — REST의 성공 응답이 승인 한 건이다. 실패해도 승인이 있으면 현재 상태를 담는다.
- 구현은 `core/approval/service.py`의 `OwnerApprovalService(repo, clock)`다.

## 7. 가정

- 서버 조립이 `OwnerApprovalService`를 만든다. 조립의 `Repository`는 `ThreadDatabase`의 범위 위임을 쓰므로 Protocol에 더한 함수가 자동으로 위임된다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-03 | `test_FR03_approval_service_contract` (shared) | `ApprovalService`와 저장소 조회 세 함수의 시그니처 |
| FR-03 | `test_FR03_approval_service_is_separate_from_core_service` (shared) | `CoreService`에 승인 함수를 더하지 않는다 |
| NFR-08 | `test_NFR08_owner_approval_has_no_requester` (shared) | 반환 모델에 요청자 필드가 없다 |
| FR-03 | `test_FR03_implementations_follow_protocols` (shared) | 구현이 Protocol을 따른다 |
| FR-03 | `test_FR03_pending_for_only_approver` (db) | 승인 권한자의 대기 승인만 반환한다 |
| FR-03 | `test_FR03_pending_excludes_expired_and_decided` (db) | 만료·결정한 승인을 뺀다 |
| FR-03 | `test_FR03_pending_order` (db) | 생성 시각, 승인 ID 순이다 |
| FR-03 | `test_FR03_approval_by_id` (db) | 한 건을 읽고, 없으면 `None`이다 |
| FR-14 | `test_FR14_is_approver_uses_approval_revision` (db) | 승인의 정책 리비전 권한자만 `True`다 |
| FR-14 | `test_FR14_no_approver_means_no_authority` (db) | 권한자가 없는 리비전은 모두 `False`다 |
| FR-03 | `test_FR03_pending_for_passes_clock` (core) | 주입한 시계로 대기 목록을 조회한다 |
| FR-03 | `test_FR03_get_returns_approval` (core) | 권한자는 `OK`와 승인을 받는다 |
| FR-03 | `test_FR03_get_unknown_is_not_found` (core) | 없는 승인은 `NOT_FOUND`다 |
| FR-03 | `test_FR03_get_by_non_approver_is_not_authorized` (core) | 권한이 없으면 승인 내용 없이 `NOT_AUTHORIZED`다 |
| FR-03 | `test_FR03_decide_sends_request_with_clock` (core) | 결정자, 결정, 시계 시각을 저장소에 보낸다 |
| FR-03 | `test_FR03_decide_unknown_is_not_found` (core) | 없는 승인은 저장소에 보내지 않는다 |
| FR-03 | `test_FR03_decide_by_non_approver_is_not_authorized` (core) | 권한이 없으면 저장소에 보내지 않는다 |
| FR-03 | `test_FR03_decide_same_twice_is_already_applied` (core) | 같은 결정의 재호출도 권한을 검사하고 기존 승인을 성공으로 받는다 |
| FR-03 | `test_FR03_decide_passes_store_reason` (core) | 저장소의 실패 사유를 바꾸지 않는다 |
| FR-03 | `test_FR03_decide_rejects_pending_value` (core) | `pending` 결정은 `ValueError`다 |
| FR-03 | `test_FR03_store_error_propagates` (core) | 저장소 오류를 삼키지 않는다 |
| FR-03 | `test_FR03_real_db_approve_flow` (integration) | 실제 DB에서 대기 목록, 승인, 재전송, 목록에서 사라짐 |
| FR-03 | `test_FR03_real_db_decide_reasons` (integration) | 실제 DB에서 반대 결정, 정책 버전 불일치, 대기 만료를 구분한다 |
