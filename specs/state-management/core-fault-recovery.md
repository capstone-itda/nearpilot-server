# FAULT 복구 건, 복구 준비 조회, 복구 승인

- 상태: 구현 중. 복구 건 생성, 저장 재시도, 정산 요약값 계산의 단위 검증을 마쳤다. 조회·승인은 #84, #85의 코드 반영과 #89를 기다린다.
- 담당: JM-220 (상태 관리)
- 요구사항: NFR-04, NFR-03, FR-12A, FR-19
- 따르는 결정: #17, #20

## 1. 왜

- 이상을 감지하면 서버는 기기를 `FAULT`로 격리한다. 격리한 기기는 정해진 순서를 마친 뒤에만 `AVAILABLE`로 돌아간다 (NFR-04).
- 소유자는 대시보드에서 복구 준비를 조회하고 복구를 승인한다 (FR-12A).
- 저장소에는 복구 건 생성, 준비 판단, 승인 재검사의 구현이 없다.
- 구현이 없으면 아래 문제가 생긴다.
  - 격리한 기기를 다시 쓸 수 없다.
  - 실행 여부를 모른 채 승인 사용분을 반환하거나 점유를 해제할 수 있다.
  - 같은 복구 건을 두 번 승인해서 중복 정산할 수 있다 (NFR-03).

## 2. 무엇

이번 구현은 `lifecycle-review.md` 6절의 두 번째 작업이다.
복구 건 생성과 저장 재시도, `core-release`의 필수 주입 접점을 연결한다.
아래 조회·승인·정산은 전체 spec의 목표다. 이번 구현의 완료 범위에 넣지 않는다.

### 이번 구현의 추상 요약

- 입력: 격리를 확인한 기기, 관련 요청·명령·점유 세션, 기존 보고의 근거 참조, 시계와 기기별 공통 잠금이다.
- 출력: 저장을 확인한 `RecoveryRecord`다. 반납 인계는 저장 확인 뒤 반환한다. 저장 실패는 예외로 알린다.
- 바뀌는 상태: 복구 건과 근거를 `save_recovery`로 저장한다. 메모리는 실패한 복구 건의 저장 대기를 유지한다.
- 약속: 같은 미해결 장애는 같은 복구 ID와 대상 연결을 유지한다. 불명 저장은 같은 ID로 조회한다. 복구 건 저장은 점유·사용분·기기 격리를 풀지 않는다.

### 선행 계약 뒤 구현할 목표

- 입력:
  - 새 장애 격리: `node_id`, 관련 요청, 명령, 점유 세션, 근거 (`core-execution`, `core-node-events`, `core-release`가 부른다)
  - 복구 준비 조회: 인증 계정, `node_id`
  - 복구 승인: 인증 계정, `node_id`, `recovery_id`
  - 기기의 실행 결과, 반납 검사 결과, 물리 관측
- 출력:
  - `RecoveryReadiness`
  - `RecoveryApprovalResult`
- 바뀌는 상태:
  - 복구 건과 근거 (`save_recovery`)
  - 승인 뒤 복구 정산: 복구 승인 기록, 승인 사용분 정산, 점유 종료, 기기 복구, 감사 (`settle_recovery`)
- 약속:
  - 새 장애 격리마다 복구 건을 하나 만든다. 같은 미해결 장애는 같은 `recovery_id`를 쓴다.
  - 조회는 복구 건을 만들지 않고 기기 상태를 바꾸지 않는다.
  - 승인 요청마다 조건을 다시 검사한다. 조건이 부족하면 정산하지 않는다.
  - 같은 복구 건의 동시 승인과 반복 승인은 한 번만 정산한다.

## 3. 제약

- 복구 순서는 NFR-04를 따른다.
  1. 기기별 안전을 확인한다.
  2. 서버 상태와 물리 상태를 동기화한다.
  3. 소유자가 복구를 승인한다.
- 복구 승인은 접근 승인과 별개다. `decide_approval()`과 `approval_ttl_sec`를 쓰지 않는다 (architecture.md §4 "복구 준비 조회와 복구 승인").
- 승인 주체는 API가 넘긴 인증 계정이다. 요청 본문의 계정 ID, 안전 판정값, 정산 내용을 받지 않는다. 승인 시각은 서버가 기록한다 (NFR-04).
- 조회와 승인 모두 기기 장소의 소유권을 검사한다 (§5 REST).
- 준비 상태는 `not_ready`, `ready`, `recovering`, `recovered`다. 복구 건이 없으면 `not_ready`와 `NO_RECOVERY_REQUIRED`다 (NFR-04).
- `blockers`는 `RecoveryBlocker`로 부족한 조건을 구분한다 (architecture.md §4 "실행·복원·복구의 공동 모델").
- `evidence`는 기존 명령, 관측, 검사 기록을 참조한다. 임의의 진단 문자열로 근거를 대신하지 않는다 (§4 "복구 준비 조회와 복구 승인").
- 승인 때 아래 조건을 다시 검사한다 (NFR-04).
  - 기기의 장소 소유권
  - 현재 복구 건
  - 미해결 명령 결과
  - 신뢰 상태와 온라인 여부
  - 최신 안전 근거와 물리 상태 동기화
- 기기 재접속이나 새 장애로 기존 근거가 무효이면 새 유효 근거를 먼저 확인한다 (NFR-04).
- 확인 불가를 미실행으로 보지 않는다. 확인 불가만으로 승인 사용분을 반환하지 않는다 (NFR-04).
- 같은 기기의 복구, 관측, 장애 반영은 같은 기기 잠금으로 직렬화한다 (NFR-04).
- 격리 성공과 복구 건 저장 성공을 따로 확인한다. 이미 `FAULT`여도 복구 건이 없으면 저장을 보완한다 (NFR-04).
- `save_recovery`가 실패하면 같은 장애의 `recovery_id`와 대상 연결을 유지한다. 반영 여부가 불명이면 `recovery(recovery_id)`로 확인한다 (architecture.md §4 "복구 건과 복구 정산").
- 재시작으로 사라진 저장 대기는 `core-restore`의 복원 묶음으로 확인한다. 기존 미완료 복구 건이 있으면 그 ID를 재사용한다 (NFR-04).
- `settlement_digest`는 상태 관리가 논리 정산으로 계산한다. 호출 시각은 넣지 않는다 (architecture.md §4 "복구 건과 복구 정산").
- `settle_recovery` 결과는 아래 표로 바꾼다 (§4 "복구 준비 조회와 복구 승인").

| 저장 결과 | `outcome` | `reason_code` |
|---|---|---|
| `applied`, `already_applied` | `completed` | `OK` |
| `conflict` | `rejected` | `STATE_CONFLICT` 또는 `RECOVERY_MISMATCH` |
| `StoreNotApplied` | `rejected` | `STORE_NOT_APPLIED` |
| `StoreOutcomeUnknown` | `pending` | `STORE_OUTCOME_UNKNOWN` |

- `StoreOutcomeUnknown`이면 `recovery(recovery_id)`로 완료 결과를 확인한다. 조회 결과가 없다는 이유만으로 미반영을 단정하지 않는다 (NFR-04).
- 완료한 복구 건의 재요청에는 소유권을 확인한 뒤 기존 완료 결과를 반환한다. 과거 복구는 현재 재사용 허가가 아니다 (NFR-04).
- 공용형 복구에는 점유 세션을 만들거나 종료하지 않는다 (NFR-04).
- 응답 모델에 점유 주체를 넣지 않는다 (NFR-08).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/lifecycle/`
- `server/tests/core/lifecycle/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/proximity/`, `core/policy/`, `core/allocation/`, `db/`, `api/`, `iot/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `RecoveryService.readiness`, `approve` | 제공 | API |
| `ExecutionStore.save_recovery`, `recovery`, `settle_recovery`, `execution` | 사용 | DB |
| `Repository.nodes`, `session` | 사용 | DB |
| `SafetyCheckGateway.request_check` | 사용 (복구 안전 확인) | IOT |
| `settlement_digest`의 직렬화와 해시 방식 | 제공 (6절). DB는 값을 저장하고 대조만 한다 | DB |

### 만들지 않는 것
- REST 경로 `GET /admin/nodes/{node_id}/recovery`, `POST /admin/nodes/{node_id}/recovery/{recovery_id}/approve` (API)
- 장소 소유권 데이터의 저장 (DB)
- 복구 정산의 원자적 저장 (DB)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 복구 건이 없는 기기를 조회한다 | `not_ready`와 `NO_RECOVERY_REQUIRED`다. 복구 건을 만들지 않는다 | `test_NFR04_readiness_without_recovery` |
| 소유자가 아닌 계정이 조회한다 | 거부한다. 기기 정보를 넣지 않는다 | `test_NFR04_readiness_not_owner` |
| 명령 결과가 불명인데 승인한다 | `rejected`와 `NOT_READY`다. 정산하지 않는다 | `test_NFR04_approve_unconfirmed_execution` |
| 기기가 오프라인인데 승인한다 | `rejected`와 `NOT_READY`다 | `test_NFR04_approve_offline` |
| 다른 `recovery_id`로 승인한다 | `rejected`와 `RECOVERY_MISMATCH`다 | `test_NFR04_approve_recovery_mismatch` |
| 같은 복구 건을 두 스레드가 동시에 승인한다 | 정산은 한 번이다. 둘 다 `completed`이거나 하나는 `pending`이다 | `test_NFR03_concurrent_recovery_approve_once` |
| 완료한 복구 건을 다시 승인한다 | 기존 완료 결과를 반환한다 | `test_NFR04_repeat_approve_returns_completed` |
| `settle_recovery`가 `StoreOutcomeUnknown`을 던진다 | `pending`과 `STORE_OUTCOME_UNKNOWN`이다. `recovery()`로 확인한다 | `test_NFR04_settle_unknown_pending` |
| `settle_recovery`가 `StoreNotApplied`를 던진다 | `rejected`와 `STORE_NOT_APPLIED`다. 복구 건을 유지한다 | `test_NFR04_settle_not_applied` |
| 승인 직전에 기기가 재접속한다 | 기존 근거를 무효로 한다. `NOT_READY`다 | `test_NFR04_reconnect_invalidates_evidence` |
| 미해결 장애 중 같은 기기가 다시 이상을 보고한다 | 같은 `recovery_id`에 근거를 더한다 | `test_NFR04_same_fault_same_recovery_id` |
| 격리 뒤 `save_recovery`가 실패한다 | 기기 격리를 유지한다. 같은 복구 ID로 저장을 재시도한다 | `test_NFR04_retry_save_recovery_same_id` |
| `save_recovery`의 반영 여부가 불명이다 | 같은 ID로 조회한다. 다른 ID로 중복 생성하지 않는다 | `test_NFR04_save_recovery_unknown_requeries` |
| 복구를 마친 기기가 새로 장애를 낸다 | 새 `recovery_id`를 만든다. 이전 승인을 적용하지 않는다 | `test_NFR04_new_fault_new_recovery_id` |
| 정산 뒤 늦은 성공 결과가 온다 | 완료 정산을 뒤집지 않는다 | `test_NFR04_late_result_after_settle` |

## 6. 결정

- `fault_recovery.py`에 `FaultRecoveryService`를 둔다 — 담당자가 2026-10-15에 선택했다.
- 기기별 공통 재진입 잠금을 주입한다 — 반납과 복구 건 저장을 같은 잠금으로 직렬화한다.
- 외부 호출자가 `tick()`으로 저장 대기를 재시도한다 — 자체 스레드와 새 시간 설정을 만들지 않는다.
- 첫 인계는 `restore_pending()`으로 기존 미완료 복구 건을 확인한다 — 재시작 뒤 같은 장애에 새 ID를 만들지 않는다.
- `record_isolated()`는 격리를 확인한 기기의 대상 연결과 `RecoveryEvidence`를 받는다 — 실행·관측 담당도 같은 복구 건 생성 경로를 사용한다.
- `record_fault()`는 반납 서비스의 필수 주입 접점에 연결한다 — 유효한 `fault` 보고의 사건 ID·연결 ID·검사 시각을 근거 참조로 보존한다.
- 확인 불능에 보고 참조가 없으면 근거 목록을 비운다 — 진단 문자열을 안전 근거로 바꾸지 않는다.
- 저장 충돌이나 조회 실패는 예외로 알리고 저장 대기를 유지한다 — 실패를 완료로 안내하지 않는다.
- DB와 IOT는 `shared` 계약의 fake로 검증한다 — 실제 저장·통신 구현과 서버 조립은 별도 담당 작업이다.
- `settlement_digest()`는 정규 JSON의 SHA-256 hex다 — 담당자가 2026-10-15에 선택했다. `shared/models.py`는 직렬화를 구현 담당자가 spec에 정하라고 한다. DB는 값을 계산하지 않고 저장·대조만 한다.
  - 넣는 값: 버전 `v=1`, 복구·기기 ID, 예상 기기·복구 상태, 예상 명령·세션 상태, 근거, 사용분 정산, 점유 종료(`use_id`, `closed_at`, `end_reason`)다.
  - 빼는 값: 승인 계정과 승인 시각이다. 같은 정산을 다시 승인해도 같은 값이 나온다.
  - 정규화: 키를 정렬하고 공백 없이 UTF-8로 쓴다. 근거와 사용분은 ID 순서로 정렬한다. 열거형은 값 문자열로 쓴다. 시각은 UTC ISO 8601로 바꾼다.
  - 거부: 시간대가 없는 시각과 같은 ID가 두 번 나온 근거·사용분은 `ValueError`다.
  - 방식을 바꾸면 `v`를 올린다. 이미 저장한 값과 비교할 때 옛 방식으로 다시 계산하지 않는다.

## 7. 가정

- `settlement_digest`의 방식은 6절에 정했다. #92 A안으로 DB는 이 값을 저장·대조만 하고 DB spec에 같은 방식을 기록한다.
- #84 A안으로 정했다: 조회와 승인마다 `Repository.is_place_owner(user_id, node_id)`로 소유 여부만 확인한다. 코드 반영은 후속 작업이다.
- #85 A안으로 정했다: 점유 세션이 없거나 끝난 기기는 기기의 현재 `release_policy.check_criteria`로 기존 `SafetyCheckRequest`를 보낸다. 현재 연결의 `passed`가 안전 근거, 그 뒤 같은 연결의 정상 `status`가 동기화 근거다. 근거는 architecture.md §4 "복구 준비 조회와 복구 승인"을 따른다. 코드 반영은 후속 작업이다.
- 미결 — #89: 관리 API의 구현 담당 (architecture.md §4 "복구 준비 조회와 복구 승인"). 이 spec은 `RecoveryService`만 제공한다.
- 복구 건 생성과 저장 실패 처리는 기존 계약으로 보완한다. 장소 소유권과 안전 근거의 계약이 정해지기 전에는 조회·승인 경로의 구현을 보류한다. 틀이나 항상 거부하는 구현을 완료로 보지 않는다.
- 장소 소유권과 안전 근거의 이슈 초안은 `fault-recovery-decisions.md`에 있다. 이슈는 #84, #85다.
- FR-12A의 조회·승인과 복구 정산은 선행 계약 뒤 구현한다. 정산 요약값 계산은 먼저 구현했다. 아래 테스트 표의 조회·승인 테스트는 남은 작업이다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-04 | `test_NFR04_isolation_creates_recovery_case` | 새 장애 격리마다 `save_recovery`로 복구 건을 만든다 |
| NFR-04 | `test_NFR04_readiness_does_not_change_state` | 조회는 저장 함수를 부르지 않는다 |
| NFR-04 | `test_NFR04_approve_settles_once` | 조건을 갖춘 승인은 `settle_recovery`를 부르고 `completed`를 반환한다 |
| NFR-03 | `test_NFR03_concurrent_recovery_approve_once` | 동시 승인에도 중복 정산은 0건이다 |
| FR-12A | `test_FR12A_recovery_service_uses_authenticated_account` | 승인 주체는 인자로 받은 인증 계정이다 |
| FR-19 | `test_FR19_unconfirmed_usage_not_released` | 실행 확인 불가만으로 사용분을 반환하지 않는다 |

이번 구현은 복구 건 생성, 같은 장애의 ID 보존, 저장 실패와 불명 조회, 재시작 뒤 ID 재사용을 검증한다.
반납과 복구 건 생성의 연결은 같은 기기 잠금을 쓰는 CORE 단위 테스트로 검증한다.

| 이번 구현의 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-04 | `test_NFR04_isolation_creates_recovery_case` | 격리한 기기에 복구 건과 대상 연결을 저장한다 |
| NFR-04 | `test_NFR04_save_recovery_unknown_requeries` | 불명 저장을 같은 ID로 확인한다. 확인하지 못하면 저장 대기를 유지한다 |
| NFR-04 | `test_NFR04_restart_reuses_existing_recovery_id` | 새 서비스도 기존 미완료 복구 ID와 생성 시각을 유지한다 |
| NFR-04 | `test_NFR04_release_fault_connected_to_recovery_creation` | 반납의 격리 뒤 실제 CORE 복구 건 생성 접점을 부른다 |
| NFR-04 | `test_NFR04_release_unconfirmed_limit_connected_to_recovery` | 확인 불능 제한 뒤 점유를 보존하고 복구 건을 만든다 |
| NFR-03 | `test_NFR03_concurrent_fault_creates_once` | 동시 인계에도 같은 장애의 복구 건을 하나만 만든다 |
| FR-19 | `test_FR19_unconfirmed_usage_not_released` | 복구 건 저장은 점유와 승인 사용분을 풀지 않는다 |
| NFR-04 | `test_NFR04_settlement_digest_excludes_approval_account_and_time` | 승인 계정과 승인 시각이 달라도 요약값이 같다 |
| NFR-04 | `test_NFR04_settlement_digest_ignores_input_order_and_time_zone` | 입력 순서와 시간대 표기가 달라도 요약값이 같다 |
| NFR-04 | `test_NFR04_settlement_digest_changes_with_settlement_content` | 대상, 예상 상태, 근거, 사용분, 종료 내용이 다르면 요약값이 다르다 |
| NFR-04 | `test_NFR04_settlement_digest_shared_recovery_has_no_close` | 공용형 복구는 점유 종료 없이 요약값을 만든다 |
| NFR-04 | `test_NFR04_settlement_digest_canonical_format` | 정규 JSON 형식을 고정한다 |
| NFR-04 | `test_NFR04_settlement_digest_rejects_ambiguous_input` | 시간대 없는 시각과 중복 ID를 거부한다 |

FR-12A의 조회·승인 테스트는 선행 계약 뒤 추가한다.
