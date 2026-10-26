# 재시작 복원

- 담당: JM-220 (상태 관리)
- 요구사항: FR-19, FR-20, NFR-04
- 따르는 결정: #15, #17, #78, #81, #82, #83, #88, 2026-10-26 센서 기반 물리 검증 제외 (#196)

## 1. 왜

- 서버가 재시작하면 메모리의 실행, 반납, 타이머 상태가 사라진다.
- 끝나지 않은 명령과 점유 세션을 확인하지 않고 새 실행을 허가하면 중복 실행이나 이중 점유가 생긴다 (FR-19).
- DB는 `restore_pending()`으로 기기별 복원 묶음을 반환한다. 복원 뒤의 동작은 상태 관리가 정한다 (architecture.md §4 "재시작 복원 조회").
- 저장소에는 복원 뒤 동작의 구현이 없다.

## 2. 무엇

- 입력: `ExecutionStore.restore_pending()`이 반환한 `RestoreBundle` 목록, 재시작 뒤의 기기 보고
- 출력: 기기별 복원 상태. `ExecutionReadiness.can_execute(node_id)`로 판정과 `core-execution`에 제공한다 (#82).
- 바뀌는 상태:
  - 전송을 시작하지 않은 예약 중 예약 대기 제한을 넘긴 예약의 취소 (`cancel_not_sent(expected=PENDING, RESERVATION_EXPIRED)`)
  - 결과를 확정하지 못한 명령의 기기 `FAULT`와 복구 건
  - `CLOSING` 세션의 논리적 종료 재개
  - 상태 관리 메모리의 기기별 복원 상태
- 약속:
  - 기기별 복원과 현재 상태 확인을 마치기 전에는 그 기기의 새 실행과 재사용을 허가하지 않는다.
  - 저장한 시각을 재시작 시각으로 덮어쓰지 않는다.
  - 전송 시작 기록만으로 실행과 미전송을 판단하지 않는다.
  - 결과가 불명인 명령에 새 명령을 자동으로 보내지 않는다.

## 3. 제약

- `restore_pending()`의 오류를 빈 목록으로 바꾸지 않는다. 조회가 실패하면 모든 기기의 새 실행을 막는다 (FR-19 재시작 복원).
- 복원 묶음의 기록별 처리는 아래 표를 따른다 (FR-19, FR-20, NFR-04).

| 기록 | 처리 |
|---|---|
| 전송을 시작하지 않은 명령 (`pending`) | 예약 시각부터 `reservation_wait_sec`가 지났으면 `cancel_not_sent(expected=PENDING, RESERVATION_EXPIRED)`로 취소한다. 지나지 않았으면 실행을 막고, 지난 뒤의 `tick()`이나 상태 보고에서 취소한다 |
| 명령을 연결하기 전의 예약 요청 | `RestoreBundle.requests`의 `reserved_at`으로 남은 대기 시간을 계산한다. 명령 부재를 처리 완료로 보지 않는다. 만료하면 같은 `cancel_not_sent(expected=PENDING, RESERVATION_EXPIRED)`로 취소한다. 명령을 만들지 않는다 (#81) |
| 전송을 시작했고 결과가 없는 명령 (`in_progress`) | 실행 여부가 불명이다. `record_result(UNKNOWN)`으로 격리하고 복구 건을 만든다 |
| `RESERVED` 세션 | 연결된 요청과 명령의 처리를 따른다. 명령이 없으면 예약 요청의 만료 처리를 따른다 |
| `ACTIVE` 세션 | 점유를 유지한다. `rental_due_at`을 바꾸지 않는다 |
| `CLOSING` 세션 | 저장된 과거 근거로 종료하지 않는다. 현재 연결 확인 뒤 `core-release`의 `resume_close`로 같은 논리 조건을 다시 검사하고 종료 저장을 재개한다 |
| 미해결 `FAULT` 기기와 미완료 복구 건 | 기존 복구 건을 복원한다. 복구 건 저장 전에 중단됐다면 누락된 복구 건을 보완한다. 현재 유효한 근거를 다시 확인한다 |
| 승인 사용분 | 연결된 요청·명령·복구 건의 처리를 따른다. 명령이 없는 공용형 예약도 포함한다. 사용분만 따로 반환하지 않는다 |

- 서버 중단 시간은 예약 대기와 대여시간·초과시간 계산에 포함한다 (FR-20 재시작).
- 대여시간 초과만으로 점유를 종료하지 않는다 (FR-20).
- 서버 중단 중 비콘을 받지 못한 시간으로 이탈을 확정하지 않는다 (FR-20).
- 서버는 과거 명령 결과를 복원한다. 현재 연결과 명령 처리 상태는 신선한 보고로 다시 확인한다 (FR-19).
- 기기의 현재 상태 확인은 재시작 뒤 현재 연결의 신선한 `status` 수신이다. 확인 전에는 복원 상태를 끝내지 않는다 (architecture.md §5 MQTT 6절).
- 오프라인, `FAULT`, 현재 상태를 확인하지 못한 기기에는 공용 조명 규칙만으로 새 명령을 보내지 않는다 (FR-20).
- 공용 조명의 유지 대상자를 복원하지 않는다. 기기별 복원을 마친 뒤 `core-shared-light`에 관측 재개를 알린다 (FR-20).
- 공용 조명의 규칙은 대여형 점유 세션의 복원을 생략하지 않는다 (FR-19).
- 만료 취소의 근거는 처음 만든 내용을 보존한다. `StoreOutcomeUnknown`이면 명령은 `execution()`으로 확인하고, 확인하지 못하면 같은 근거로 재시도한다. `CONFLICT`이면 실행 차단을 유지하고 예외로 알린다.
- `can_execute()`는 `ExecutionReadiness` 계약을 따른다. 최초 복원 조회가 실패했거나 현재 연결의 정상 보고를 확인하지 못한 기기는 `False`다 (architecture.md §4 "재시작 복원 조회").

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
| `ExecutionStore.restore_pending`, `record_result`, `save_recovery`, `recovery` | 사용 | DB |
| `ReleaseService.resume_close` | 사용 (`CLOSING` 세션의 종료 재개) | 상태 관리 |
| `ExecutionStore.cancel_not_sent` | 사용 (예약 만료 취소, 명령 미연결 요청 포함) | DB |
| `ExecutionReadiness.can_execute` | 제공 | 판정, `core-execution` |

### 만들지 않는 것
- 복원 묶음의 조회 (DB)
- 반납 종료의 판단과 저장 (`core-release`)
- 복구 정산 (`core-fault-recovery`)
- 근접 추정의 HMM 복원 (근접 추정)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `restore_pending()`이 예외를 던진다 | 모든 기기의 새 실행을 막는다. 빈 목록으로 보지 않는다 | `test_FR19_restore_failure_blocks_all` |
| `pending` 명령의 예약 대기 제한이 서버 중단 중에 지났다 | `cancel_not_sent(expected=PENDING, RESERVATION_EXPIRED)`로 취소한다 | `test_FR19_pending_expired_during_downtime_cancelled` |
| `pending` 명령의 예약 대기 제한이 아직 남았다 | 실행을 막는다. 제한이 지난 뒤 취소한다 | `test_FR19_pending_waits_then_expires_after_restart` |
| 예약 뒤 명령 연결 전에 서버가 중단됐다 | 요청의 예약 시각으로 만료를 처리한다. 명령 없이 취소해서 세션과 사용분을 정리한다 | `test_FR19_restore_request_without_command_cancelled` |
| 공용형 예약에 요청과 사용분만 있다 | 점유 세션 없이 요청의 만료 처리를 따른다 | `test_FR19_restore_request_without_command_cancelled` |
| 만료 취소의 저장 결과가 불명이다 | 같은 근거로 재시도한다. 확인 전에는 실행을 막는다 | `test_FR19_expiry_cancel_unknown_retries_same_evidence` |
| 만료 취소가 `CONFLICT`다 | 실행 차단을 유지하고 예외로 알린다 | `test_FR19_expiry_cancel_conflict_stays_blocked` |
| 기기 격리 뒤 복구 건 저장 전에 서버가 중단됐다 | 격리를 유지하고 누락된 복구 건을 보완한다 | `test_NFR04_restore_fault_without_recovery` |
| `in_progress` 명령이 있다 | 새 명령을 보내지 않는다. `UNKNOWN`으로 격리하고 복구 건을 만든다 | `test_FR19_restore_in_progress_isolated` |
| `in_progress` 명령의 늦은 성공 결과가 재시작 뒤 온다 | 원래 실행 문맥으로 `record_result`에 넘긴다. `FAULT`를 자동으로 해제하지 않는다 (#83) | `test_FR07_restore_late_success_keeps_fault` |
| `ACTIVE` 세션의 `rental_due_at`이 서버 중단 중에 지났다 | 점유를 유지한다. 초과시간에 중단 시간을 포함한다 | `test_FR20_restore_overdue_kept` |
| `CLOSING` 세션이 있다 | 현재 연결 확인 뒤 논리적 종료를 다시 저장한다 | `test_FR20_restore_closing_resumes_logical_close` |
| 복원 묶음에 과거 검사 근거가 있다 | 근거로 쓰지 않고 같은 논리 조건으로 종료한다 | `test_FR20_restore_closing_ignores_saved_check_evidence` |
| 기기가 재시작 뒤 `status`를 보내지 않는다 | 복원 상태를 끝내지 않는다. 새 실행을 막는다 | `test_FR19_restore_blocks_new_execution_until_confirmed` |
| 미완료 복구 건이 있다 | 같은 `recovery_id`로 복원한다. 새 복구 건을 만들지 않는다 | `test_NFR04_restore_keeps_recovery_id_and_evidence_without_trusting_it` |
| 저장한 생성 시각과 기준 시각이 있다 | 재시작 시각으로 바꾸지 않는다 | `test_FR20_pending_uses_stored_time_and_settings_snapshot` |

## 6. 결정

- `restore.py`에 `RestoreService`를 둔다 — 담당자가 2026-10-15에 제안한 구성을 선택했다.
- 시계, 설정 조회와 공통 기기 재진입 잠금을 주입한다 — 반납·복구·관측과 같은 잠금을 쓴다.
- `restore()`가 최초 조회를 맡고 외부 `tick()`이 실패와 미완료 기기를 재시도한다 — 자체 스레드와 새 시간 설정을 만들지 않는다.
- `on_status()`는 검증을 마친 현재 연결의 보고만 받는다 — 발표와 과거 결과로 복원을 끝내지 않는다.
- 현재 연결과 온라인 유지 시간을 다시 확인하는 `can_execute()`를 `ExecutionReadiness` 구현으로 제공한다 — #82로 `shared`에 계약이 생겼다. 서비스 자체가 Protocol을 만족한다.
- `is_restored()`와 `blockers()`로 내부 복원 상태를 읽는다 — 실행 가능 여부와 반납 종료 재개를 구분한다.
- `FaultRecoveryService.record_isolated`, `ReleaseService.resume_close`, `SharedLightService.resume`를 연결한다 — 기존 상태 관리 기능을 재사용한다.
- 공용 조명에는 `resume()` 뒤 확인한 보고를 `on_status()`로 넘긴다 — 초기 참여 대기는 실제 인계 시각부터 잰다.
- 재시작의 불명 근거와 확정 시각을 보존한다 — 저장 재시도에서 사건 내용을 바꾸지 않는다.
- 같은 상태 보고를 다시 인계하면 실패한 작업을 재시도한다 — 중복 보고가 복원을 중단하지 않는다.
- 복원 완료 뒤 새 요청이 만든 `RESERVED`는 실행 인계에 허용한다 — 재시작 당시의 미완료 예약과 구분한다.
- 복구 정산의 완료 기록은 식별자로 다시 조회한다 — 완료한 건의 불명 명령을 계속 차단하지 않는다.
- 조회·저장·인계가 실패하면 예외로 알리고 실행 차단을 유지한다 — 한 기기의 실패 뒤에도 다른 기기는 재시도한다.
- 예약 만료 취소는 기기 처리 안에서 바로 한다 — 모든 만료 예약을 취소한 뒤 현재 기록으로 기기를 다시 확인한다. 남은 예약이 있으면 실행 차단을 유지한다.
- 명령 미연결 요청의 취소 완료는 메모리에 표시한다 — 복원 묶음은 재조회하지 않으므로 같은 요청을 다시 취소하지 않는다.

## 7. 가정

- #82 A안: 판정은 ②에서 `can_execute()`가 `False`인 기기를 후보에서 뺀다. 판정 쪽 연결은 판정 담당과 서버 조립이 한다.
- #78 A안: 예약 대기 제한 만료 취소의 사유 코드는 `RESERVATION_EXPIRED`다.
- #81 A안: 명령 미연결 요청도 `cancel_not_sent(expected=PENDING)`로 취소한다. DB는 예약과 사용분만 정리한다. 임의로 명령을 만들지 않는다.
- #88 A안: 상태 관리가 서버를 조립한다. 복원은 서버 시작 때 게이트웨이 구독 뒤, 요청 수신 전에 한 번 실행한다. 진입점과 조립 모듈은 이 spec에 넣지 않는다.
- #83 A안: 재시작 뒤 늦은 결과는 `ExecutionStore.execution_context`로 만든 원래 문맥으로 `ExecutionService.on_result`에 넘긴다. 연결은 `core-node-events`와 서버 조립이 한다.
- `in_progress` 명령을 `record_result(UNKNOWN)`으로 저장하는 근거는 서버의 재시작이다. 기기가 보고한 `unknown`과 구분하도록 근거에 출처를 남긴다 (FR-07).
- 신선한 보고의 검증은 IOT가 맡는다고 가정한다. 이 서비스는 재시작 뒤 수신, 현재 연결과 온라인 유지 시간을 추가로 확인한다.
- 명령 처리 상태가 `busy`나 `unknown`인 보고로 새 실행을 열지 않는다. 현재 연결의 명령 처리 대기(`idle`) 보고를 기다린다.
- 예약 대기 제한은 최초 조회 때 받은 설정을 보존한다. 저장한 예약 시각으로 만료 여부를 구분한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-19 | `test_FR19_restore_blocks_new_execution_until_confirmed` | 복원과 확인 전에는 그 기기의 전송을 시작하지 않는다 |
| FR-20 | `test_FR20_restore_does_not_restore_light_holders` | 공용 조명의 유지 대상자를 복원하지 않는다 |
| NFR-04 | `test_NFR04_restore_keeps_recovery_id_and_evidence_without_trusting_it` | 기존 복구 건과 근거를 보존한다. 과거 근거로 실행을 열지 않는다 |
| FR-19 | `test_FR19_unknown_store_without_commit_retries_same_event` | 저장 불명 재시도에 같은 시각과 근거를 쓴다 |
| FR-19 | `test_FR19_restore_request_without_command_cancelled` | 명령 미연결 예약을 명령 없이 만료 취소한다 |
| FR-19 | `test_FR19_pending_expired_during_downtime_cancelled` | 서버 중단 중 만료한 `pending` 명령을 `RESERVATION_EXPIRED`로 취소한다 |
| FR-19 | `test_FR19_restore_service_is_execution_readiness` | 복원 서비스가 `ExecutionReadiness`를 만족한다 |
| FR-07 | `test_FR07_restore_late_success_keeps_fault` | 재시작 뒤 늦은 성공이 `FAULT`를 해제하지 않는다 |
| FR-19 | `test_FR19_new_reservation_after_restore_can_reach_execution` | 복원 완료 뒤 정상 예약의 실행 인계를 허용한다 |
| FR-19 | `test_FR19_same_status_redelivery_retries_failed_handover` | 같은 보고의 재인계에서 실패한 인계를 재시도한다 |
| FR-20 | `test_FR20_restore_overdue_kept` | 점유와 기준 시각을 유지하고 서버 중단 시간을 초과시간에 포함한다 |
| FR-20 | `test_FR20_restore_closing_resumes_logical_close` | 현재 연결 확인 뒤 논리적 종료를 다시 저장한다 |
| NFR-04 | `test_NFR04_completed_recovery_rechecks_current_status` | 복구 정산 완료 기록과 현재 보고를 확인한다 |
| NFR-03 | `test_NFR03_concurrent_restore_settles_once` | 동시 복원에서도 불명 저장과 복구 건 생성을 중복하지 않는다 |

- DB와 IOT는 `shared` 계약의 fake로 대신한다. 반납·복구·공용 조명·실행 서비스 연결도 검증한다.
- 실제 서버 시작과 MQTT 연결은 이 spec의 검증에 넣지 않는다.
