# FAULT 복구 건, 복구 준비 조회, 복구 승인

- 담당: JM-220 (상태 관리)
- 요구사항: NFR-04, NFR-03, FR-12A, FR-19
- 따르는 결정: #17, #20, #84, #89, #92, 2026-10-26 센서 기반 물리 검증 제외 (#196)

## 1. 왜

- 이상을 감지하면 서버는 기기를 `FAULT`로 격리한다. 격리한 기기는 정해진 순서를 마친 뒤에만 `AVAILABLE`로 돌아간다 (NFR-04).
- 소유자는 복구 준비를 조회하고 복구를 승인한다 (FR-12A).
- 2026-10-26 결정으로 복구 조건에서 물리 안전 검사와 물리 상태 동기화를 제외했다. 복구는 현재 연결과 명령 처리 상태를 확인하고 소유자 승인으로 논리적 정산을 한다.
- 구현이 없으면 아래 문제가 생긴다.
  - 격리한 기기를 다시 쓸 수 없다.
  - 실행 여부를 모른 채 승인 사용분을 반환하거나 점유를 해제할 수 있다.
  - 같은 복구 건을 두 번 승인해서 중복 정산할 수 있다 (NFR-03).

## 2. 무엇

- 입력:
  - 새 장애 격리: `node_id`, 관련 요청, 명령, 점유 세션, 근거 (`core-execution`, `core-node-events`, `core-restore`가 부른다)
  - 복구 준비 조회: 인증 계정, `node_id`
  - 복구 승인: 인증 계정, `node_id`, `recovery_id`
  - 현재 연결의 `NodeStatus` (`core-node-events`가 넘긴다)
- 출력:
  - 저장을 확인한 `RecoveryRecord`
  - `RecoveryReadiness`
  - `RecoveryApprovalResult`
- 바뀌는 상태:
  - 복구 건과 근거 (`save_recovery`)
  - 승인 뒤 복구 정산: 복구 승인 기록, 승인 사용분 정산, 점유 종료, 기기 복구, 감사 (`settle_recovery`)
  - 상태 관리 메모리: 저장 대기 중인 복구 건, 기기별 마지막 `status`와 같은 연결의 대기 구간 첫 보고, 저장 결과가 불명인 정산
- 약속:
  - 새 장애 격리마다 복구 건을 하나 만든다. 같은 미해결 장애는 같은 `recovery_id`를 쓴다.
  - 조회는 복구 건을 만들지 않고 저장 함수를 부르지 않는다. 기기 상태를 바꾸지 않고 기기에 요청을 보내지 않는다.
  - 승인 요청마다 조건을 다시 검사한다. 조건이 부족하면 정산하지 않는다.
  - 같은 복구 건의 동시 승인과 반복 승인은 한 번만 정산한다.
  - 복구 승인은 물리 상태의 증명이 아니다.

## 3. 제약

- 복구 순서는 NFR-04를 따른다.
  1. 현재 연결, 신뢰, 온라인 여부와 명령 처리 대기 상태를 확인한다.
  2. 명령 결과와 저장 기록을 대조한다. 결과 불명은 그대로 보존한다.
  3. 소유자가 복구를 승인한다. 서버는 논리 정산과 점유 종료를 원자적으로 저장한다.
- 복구 승인은 접근 승인과 별개다. `decide_approval()`과 `approval_ttl_sec`를 쓰지 않는다 (architecture.md §4 "복구 준비 조회와 복구 승인").
- 승인 주체는 API가 넘긴 인증 계정이다. 요청 본문의 계정 ID, 물리 상태 판정값, 정산 내용을 받지 않는다. 승인 시각은 서버가 기록한다 (NFR-04).
- 조회와 승인 모두 `Repository.is_place_owner(user_id, node_id)`로 기기 장소의 소유권을 검사한다. 소유 계정 ID는 받지 않는다 (#84).
- 소유자가 아닌 계정의 조회는 `NotPlaceOwner`(`PermissionError`)로 거부한다. 승인은 `rejected`와 `NOT_OWNER`이고 준비 상태에 복구 건 ID와 근거를 넣지 않는다.
- 명령 처리 대기 근거는 아래를 모두 만족하는 `status`다 (architecture.md §4 "복구 준비 조회와 복구 승인").
  - 현재 기기 연결(`NodeInfo.session_id`)의 보고다.
  - 마지막 보고의 서버 수신 시각이 `online_ttl_sec` 안이다.
  - 마지막 보고가 명령 처리 대기(`command_state=idle`)다. `busy`와 `unknown`은 근거가 아니다.
  - 복구 건이 생긴 뒤에 받은 보고다.
- MQTT v2의 `status`는 센서 값을 담지 않는다. 복구 조건은 명령 처리 상태만 쓴다 (FR-07).
- 준비 상태는 `not_ready`, `ready`, `recovering`, `recovered`다. 복구 건이 없으면 `not_ready`와 `NO_RECOVERY_REQUIRED`다 (NFR-04).
- `blockers`는 `RecoveryBlocker`로 부족한 조건을 구분한다. 명령 처리 대기 보고가 없으면 `COMMAND_STATE_UNCONFIRMED`다.
- `evidence`는 저장한 명령 결과와 현재 연결의 상태 보고를 참조한다. 임의의 진단 문자열로 근거를 대신하지 않는다.
- 승인 때 아래 조건을 다시 검사한다 (NFR-04).
  - 기기의 장소 소유권
  - 현재 복구 건
  - 명령 결과와 저장 기록의 대조
  - 신뢰 상태와 온라인 여부
  - 현재 연결의 신선한 명령 처리 대기 보고
- 기기 재접속이나 새 장애가 있으면 현재 연결의 보고와 복구 건을 다시 확인한다 (NFR-04).
- 실행 여부 미확인(`EXECUTION_UNCONFIRMED`)은 복구 건의 명령이 아직 `pending`이나 `in_progress`인 경우다. `unknown`으로 저장한 명령은 결과를 확정했지만 실행을 확인하지 못한 것이다.
- 보류·예약한 승인 사용분의 정산은 아래 표를 따른다 (NFR-04).

| 연결된 명령 | 사용분 정산 |
|---|---|
| 없음, 또는 전송을 시도하지 않음 | `released` |
| `failed`, `not_sent`, `rejected` | `released` |
| `succeeded` | `consumed` |
| 전송을 시도한 `unknown` | 정산하지 않는다. 보류(`held`)로 남긴다 |

- 결과 불명을 미실행이나 성공으로 보지 않는다. 결과 불명의 사용분 정산 기준은 미결이다. 기준이 정해질 때까지 보류를 유지하고 복구는 완료한다 (NFR-04).
- 대여형 복구는 끝나지 않은 점유 세션을 `fault` 사유로 종료한다. 종료 시각은 승인 시각이다.
- 정산 저장이 불명이면 같은 복구 건의 재승인은 처음 정산과 같은 종료 시각으로 정산을 다시 만든다. 내용이 같으면 처음 정산을 그대로 다시 보낸다.
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
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/proximity/`, `core/policy/`, `core/allocation/`, `api/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `RecoveryService.readiness`, `approve` | 제공 | API |
| `ExecutionStore.save_recovery`, `recovery`, `settle_recovery`, `execution`, `restore_pending` | 사용 | DB |
| `Repository.nodes`, `session`, `is_place_owner` | 사용 | DB |
| `on_status` | 제공 (`core-node-events`가 상태 보고를 넘긴다) | 상태 관리 |
| `settlement_digest`의 직렬화와 해시 방식 | 제공 (6절). DB는 값을 저장하고 대조만 한다 | DB |

### 만들지 않는 것
- REST 경로 `GET /admin/nodes/{node_id}/recovery`, `POST /admin/nodes/{node_id}/recovery/{recovery_id}/approve`. #89로 상태 관리가 맡지만 담당자가 2026-10-15에 이 작업에서 빼기로 했다.
- 장소 소유권 데이터의 저장 (DB)
- 복구 정산의 원자적 저장 (DB)
- 기기에 보내는 검사 요청. 복구는 기기에 요청하지 않는다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 복구 건이 없는 기기를 조회한다 | `not_ready`와 `NO_RECOVERY_REQUIRED`다. 복구 건을 만들지 않는다 | `test_NFR04_readiness_without_recovery` |
| 소유자가 아닌 계정이 조회한다 | `NotPlaceOwner`로 거부한다. 기기 정보를 넣지 않는다 | `test_NFR04_readiness_not_owner` |
| 소유자가 아닌 계정이 승인한다 | `rejected`와 `NOT_OWNER`다. 복구 건 ID와 근거를 넣지 않는다 | `test_NFR04_approve_not_owner` |
| 명령 처리 대기 보고가 없는 기기를 조회한다 | `COMMAND_STATE_UNCONFIRMED`다. 상태를 바꾸지 않는다 | `test_NFR04_readiness_does_not_change_state` |
| 센서 값이 없거나 이상을 말한다 | 명령 처리 대기이면 조건을 채운다 | `test_NFR04_no_physical_safety_or_sync_condition` |
| 마지막 보고가 `busy`나 `unknown`이다 | `COMMAND_STATE_UNCONFIRMED`다 | `test_NFR04_busy_or_unknown_command_state_blocks` |
| 마지막 보고가 `online_ttl_sec`보다 오래됐다 | `COMMAND_STATE_UNCONFIRMED`다 | `test_NFR04_stale_command_state_blocks` |
| 보고가 복구 건보다 먼저 왔다 | 근거로 쓰지 않는다 | `test_NFR04_status_before_fault_is_not_evidence` |
| 명령 결과에 따라 사용분을 정산한다 | 실패는 반환, 성공은 소진, 전송한 불명은 보류다 | `test_FR19_usage_settlement_follows_execution_result` |
| 결과 불명 명령의 복구를 승인한다 | 복구를 완료한다. 사용분은 `held`로 남긴다 | `test_NFR04_unknown_usage_stays_held_after_recovery` |
| 명령 결과가 확정되지 않았는데 승인한다 | `rejected`와 `NOT_READY`다. 정산하지 않는다 | `test_NFR04_approve_unconfirmed_execution` |
| 기기가 오프라인인데 승인한다 | `rejected`와 `NOT_READY`다 | `test_NFR04_approve_offline` |
| 다른 `recovery_id`로 승인한다 | `rejected`와 `RECOVERY_MISMATCH`다 | `test_NFR04_approve_recovery_mismatch` |
| 같은 복구 건을 두 스레드가 동시에 승인한다 | 정산은 한 번이다 | `test_NFR03_concurrent_recovery_approve_once` |
| 완료한 복구 건을 다시 승인한다 | 기존 완료 결과를 반환한다 | `test_NFR04_repeat_approve_returns_completed` |
| `settle_recovery`가 `StoreOutcomeUnknown`을 던진다 | `pending`과 `STORE_OUTCOME_UNKNOWN`이다. `recovery()`로 확인한다 | `test_NFR04_settle_unknown_pending` |
| `settle_recovery`가 `StoreNotApplied`를 던진다 | `rejected`와 `STORE_NOT_APPLIED`다. 복구 건을 유지한다 | `test_NFR04_settle_not_applied` |
| 승인 직전에 기기가 재접속한다 | 기존 근거를 무효로 한다. 새 연결의 보고를 기다린다 | `test_NFR04_reconnect_invalidates_evidence` |
| 새 장애로 복구 건이 바뀐다 | 그 뒤에 받은 보고만 근거로 쓴다 | `test_NFR04_new_fault_needs_report_after_it` |
| `settle_recovery`가 `CONFLICT`다 | `rejected`와 `STATE_CONFLICT`다 | `test_NFR04_settle_conflict_rejected` |
| 미해결 장애 중 같은 기기가 다시 이상을 보고한다 | 같은 `recovery_id`에 근거를 더한다 | `test_NFR04_same_fault_same_recovery_id` |
| 격리 뒤 `save_recovery`가 실패한다 | 기기 격리를 유지한다. 같은 복구 ID로 저장을 재시도한다 | `test_NFR04_retry_save_recovery_same_id` |
| `save_recovery`의 반영 여부가 불명이다 | 같은 ID로 조회한다. 다른 ID로 중복 생성하지 않는다 | `test_NFR04_save_recovery_unknown_requeries` |
| 복구를 마친 기기가 새로 장애를 낸다 | 새 `recovery_id`를 만든다. 이전 승인을 적용하지 않는다 | `test_NFR04_new_fault_new_recovery_id` |
| 정산 뒤 늦은 성공 결과가 온다 | 완료 정산을 뒤집지 않는다 | `test_NFR04_late_result_after_settle` |

## 6. 결정

- `fault_recovery.py`에 `FaultRecoveryService`를 둔다 — 담당자가 2026-10-15에 선택했다.
- 기기별 공통 재진입 잠금을 주입한다 — 실행·관측과 복구 건 저장을 같은 잠금으로 직렬화한다.
- 외부 호출자가 `tick()`으로 저장 대기를 재시도한다 — 자체 스레드와 새 시간 설정을 만들지 않는다.
- 첫 인계는 `restore_pending()`으로 기존 미완료 복구 건을 확인한다 — 재시작 뒤 같은 장애에 새 ID를 만들지 않는다.
- `record_isolated()`는 격리를 확인한 기기의 대상 연결과 `RecoveryEvidence`를 받는다 — 실행·관측 담당도 같은 복구 건 생성 경로를 사용한다.
- 반납 전용 접점 `record_fault()`와 검사 결과 접점 `on_check_result()`를 없앤다 — 2026-10-26 결정으로 반납 검사와 복구 안전 검사가 없어졌다.
- 저장 충돌이나 조회 실패는 예외로 알리고 저장 대기를 유지한다 — 실패를 완료로 안내하지 않는다.
- DB는 `shared` 계약의 fake로 검증한다 — 실제 저장 구현과 서버 조립은 별도 담당 작업이다.
- 복구 준비 조회와 승인은 `FaultRecoveryService`가 함께 맡는다 — 복구 건 저장 대기와 기기 잠금을 한곳에서 직렬화한다. `RecoveryService` Protocol을 만족한다.
- 보류 사용분은 실패를 반환하고 성공을 소진한다. 전송한 불명은 보류로 남기고 복구는 완료한다 — 담당자가 2026-10-26에 선택했다. 2026-10-15의 "불명 소진" 선택을 대체한다.
- 조회와 승인은 `restore_pending()`의 기기 묶음으로 현재 복구 건과 사용분을 읽는다 — 공동 계약의 일관된 읽기를 재사용한다.
- 명령 처리 대기 근거는 메모리의 현재 연결 보고에서만 채택한다 — 재시작이나 재접속 뒤에는 저장된 과거 근거로 준비 상태를 만들지 않는다.
- 근거 참조는 같은 연결에서 이어진 대기 구간의 첫 보고다. 복구 건보다 먼저 시작한 구간이면 복구 건 뒤의 첫 확인 보고부터 센다 — 같은 대기 구간의 재승인이 같은 정산 요약값을 만든다.
- 조회는 상태 보고 근거를 `save_recovery`로 저장하지 않는다. 승인 정산이 근거를 함께 저장한다 — 1초 주기 보고마다 복구 건을 쓰지 않는다.
- 상태 보고의 근거 ID는 `status:{node_id}:{연결 ID}:{seq}`다 — `NodeStatus`에 사건 ID가 없다.
- `settlement_digest()`는 정규 JSON의 SHA-256 hex다 — 담당자가 2026-10-15에 선택했다. DB는 값을 계산하지 않고 저장·대조만 한다.
  - 넣는 값: 버전 `v=2`, 복구·기기 ID, 예상 기기·복구 상태, 예상 명령·세션 상태, 근거, 사용분 정산, 점유 종료(`use_id`, `closed_at`, `end_reason`)다.
  - 근거는 `evidence_id`, `session_id`, `observed_at`, `execution_outcome`, `command_idle`을 담는다. `v=1`의 `safety_confirmed`, `physical_state_synced`를 뺐다 (2026-10-26).
  - 빼는 값: 승인 계정과 승인 시각이다. 같은 정산을 다시 승인해도 같은 값이 나온다.
  - 정규화: 키를 정렬하고 공백 없이 UTF-8로 쓴다. 근거와 사용분은 ID 순서로 정렬한다. 열거형은 값 문자열로 쓴다. 시각은 UTC ISO 8601로 바꾼다.
  - 거부: 시간대가 없는 시각과 같은 ID가 두 번 나온 근거·사용분은 `ValueError`다.
  - 방식을 바꾸면 `v`를 올린다. 이미 저장한 값과 비교할 때 옛 방식으로 다시 계산하지 않는다.

## 7. 가정

- `settlement_digest`의 방식은 6절에 정했다. #92 A안으로 DB는 이 값을 저장·대조만 하고 DB spec에 같은 방식을 기록한다.
- #84 A안: 조회와 승인마다 `Repository.is_place_owner(user_id, node_id)`로 소유 여부만 확인한다.
- #89: 복구 REST 경로는 상태 관리가 구현하지만 이 spec에 넣지 않는다. 이 spec은 `RecoveryService`만 제공한다.
- `NotPlaceOwner`는 `lifecycle` 안의 예외다. REST 경로가 응답 코드로 바꾼다고 가정한다.
- 새 status 형식은 MQTT v2의 `command_state`로 가정한다 (`iot-messages` 가정, 담당자 확인 2026-10-26).
- 미결 (#196): `COMMAND_STATE_UNCONFIRMED`는 새 `RecoveryBlocker` 값이다. architecture.md §4는 새 사유 값을 미결로 둔다.
- 미결 (#196): 결과 불명 명령의 사용분 정산 기준이다. 정해질 때까지 `held`로 남긴다.
- 미결 (#196): 미해결 복구 건의 이전 절차다. 배포한 운영 데이터가 없어서 이전 없이 새 DB로 시작한다고 가정한다 (담당자 확인, 2026-10-26).

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-04 | `test_NFR04_isolation_creates_recovery_case` | 새 장애 격리마다 `save_recovery`로 복구 건을 만든다 |
| NFR-04 | `test_NFR04_readiness_does_not_change_state` | 조회는 저장 함수를 부르지 않는다 |
| NFR-04 | `test_NFR04_approve_settles_once` | 조건을 갖춘 승인은 `settle_recovery`를 부르고 `completed`를 반환한다 |
| NFR-04 | `test_NFR04_no_physical_safety_or_sync_condition` | 물리 안전·동기화 조건이 없다 |
| NFR-04 | `test_NFR04_command_idle_evidence_from_current_connection` | 현재 연결의 명령 처리 대기 보고를 근거로 쓴다 |
| NFR-04 | `test_NFR04_release_has_no_fault_adapter` | 반납 전용 복구 건 접점이 없다 |
| NFR-03 | `test_NFR03_concurrent_recovery_approve_once` | 동시 승인에도 중복 정산은 0건이다 |
| NFR-03 | `test_NFR03_concurrent_fault_creates_once` | 동시 인계에도 같은 장애의 복구 건을 하나만 만든다 |
| FR-12A | `test_FR12A_recovery_service_uses_authenticated_account` | 승인 주체는 인자로 받은 인증 계정이다 |
| FR-19 | `test_FR19_unconfirmed_usage_not_released` | 복구 건 저장은 점유와 승인 사용분을 풀지 않는다 |
| FR-19 | `test_FR19_usage_settlement_follows_execution_result` | 명령 결과에 따라 사용분을 정산한다 |
| NFR-04 | `test_NFR04_save_recovery_unknown_requeries` | 불명 저장을 같은 ID로 확인한다. 확인하지 못하면 저장 대기를 유지한다 |
| NFR-04 | `test_NFR04_restart_reuses_existing_recovery_id` | 새 서비스도 기존 미완료 복구 ID와 생성 시각을 유지한다 |
| NFR-04 | `test_NFR04_settlement_digest_excludes_approval_account_and_time` | 승인 계정과 승인 시각이 달라도 요약값이 같다 |
| NFR-04 | `test_NFR04_settlement_digest_ignores_input_order_and_time_zone` | 입력 순서와 시간대 표기가 달라도 요약값이 같다 |
| NFR-04 | `test_NFR04_settlement_digest_changes_with_settlement_content` | 대상, 예상 상태, 근거, 사용분, 종료 내용이 다르면 요약값이 다르다 |
| NFR-04 | `test_NFR04_settlement_digest_shared_recovery_has_no_close` | 공용형 복구는 점유 종료 없이 요약값을 만든다 |
| NFR-04 | `test_NFR04_settlement_digest_canonical_format` | `v=2` 정규 JSON 형식을 고정한다 |
| NFR-04 | `test_NFR04_settlement_digest_rejects_ambiguous_input` | 시간대 없는 시각과 중복 ID를 거부한다 |
| NFR-04 | `test_NFR04_recovery_service_contract` | `RecoveryService` Protocol을 만족한다 |
| NFR-04 | `test_NFR04_status_routed_to_recovery` | 기기 이벤트가 상태 보고를 복구 서비스에 넘긴다 |

- 조회·승인 테스트는 `test_recovery_approval.py`에, 복구 건 생성과 요약값 테스트는 `test_fault_recovery.py`에 있다.
