# 예약 이후 저장과 복원·복구 (`ExecutionStore`)

- 담당: paiwhy (DB)
- 요구사항: FR-06, FR-07, FR-19, FR-20, FR-21, FR-22, NFR-03, NFR-04
- 따르는 결정: #17, #19, #20, #35, #37, #38, #78, #81, #84, #92, #113, PR #50, PR #51, PR #103, PR #104, PR #105, 2026-10-26 센서 기반 물리 검증 제외 (#196)

## 1. 왜

- `shared/interfaces.py`에는 상태 관리(`core/lifecycle`)가 쓸 `ExecutionStore`가 있다. `db/`에는 구현이 없다.
- 구현이 없으면 상태 관리의 `release.py`, `fault_recovery.py`가 fake 저장소로만 동작하고 실제 DB로 통합 테스트를 할 수 없다.
- 구현이 없으면 실행 인계(`link_command`~`record_result`)와 복구 정산(`settle_recovery`)은 아직 아무 구현도 없어 시작하지 못한다.

## 2. 무엇

- 입력: `ExecutionStore` 각 함수의 인자 (`Action`, `ExecutionStatus`, `ReasonCode`, `CommandOutcome`, `datetime`, `RecoveryRecord`, `RecoverySettlement`, `Mapping` 근거 등 `shared`의 값 객체)
- 출력: `StoreResult`, `CommandRecord`, `CloseResult`, `Sequence[RestoreBundle]`, `RecoveryRecord`
- 바뀌는 상태:
  - `node_command`, `use_session`, `approval_usage`, `node.device_state`, `recovery`, `audit_log` 행
  - 새 마이그레이션으로 `recovery`에 `settlement_json` 컬럼을 추가한다. `0001_initial.sql`은 고치지 않는다.
  - `0007_drop_release_check.sql`로 `use_session`의 `check_id`·`check_evidence` 컬럼을 지운다 (2026-10-26 결정).
- 약속:
  - `ExecutionStore`의 함수 13개(`link_command`, `begin_send`, `cancel_not_sent`, `record_result`, `isolate_device`, `begin_close`, `finish_close`, `close_result`, `execution`, `restore_pending`, `save_recovery`, `recovery`, `settle_recovery`)를 모두 구현한다.
  - 쓰기 함수 하나는 `BEGIN IMMEDIATE` 트랜잭션 하나다. 조회 전용 함수(`close_result`, `execution`, `recovery`, `restore_pending`)는 읽기만 한다.
  - 결과는 `APPLIED`, `ALREADY_APPLIED`, `CONFLICT` 중 하나다. 저장 오류는 `StoreNotApplied`·`StoreOutcomeUnknown`으로 구분한다.

## 3. 제약

- 함수 하나가 트랜잭션 하나다. 사건에 딸린 기기·세션·명령·승인 사용분·감사 기록을 함께 반영한다 (`interfaces.py` `ExecutionStore`).
- 같은 사건·같은 내용의 재호출은 `ALREADY_APPLIED`다. 다른 내용은 `CONFLICT`이고 기존 기록을 덮어쓰지 않는다 (FR-19).
- 저장 장애는 `StoreNotApplied`(변경 없음 확실)와 `StoreOutcomeUnknown`(반영 여부 불명)으로 예외를 구분한다 (`errors.py`).
- `begin_send`와 `cancel_not_sent`가 경합하면 한쪽만 성공한다. 취소된 명령은 전송을 시작할 수 없다 (FR-19).
- `begin_send`와 `begin_close`는 같은 기기의 다른 미완료 구동 명령을 원자적으로 검사한다. 경합은 `CONFLICT`·`COMMAND_IN_PROGRESS`, `CLOSING`·`FAULT`는 `CONFLICT`·`STATE_CONFLICT`다 (`interfaces.py`).
- `record_result`는 명령 하나에 결과를 한 번만 확정한다. `SUCCEEDED`는 최초 대여의 기기·세션을 `ACTIVE`로 두고 재조작은 기존 점유를 보존한다. `FAILED`·`UNKNOWN`은 기기 `FAULT`와 사용분 `held`다. `REJECTED`는 이번 사용분만 `released`, 최초 대여는 이번 예약을 `cancelled`로 종료하고 기기를 `FAULT`로 두지 않는다 (`architecture.md` §4 "예약 이후 저장", "구동 중 추가 명령 제한의 저장").
- 늦은 성공으로 `FAULT`를 해제하지 않는다. 늦은 결과와 복구 재호출은 중복 정산과 완료 상태의 되돌림을 만들지 않는다 (FR-07, `architecture.md` §4).
- `cancel_not_sent`는 기대 상태(`expected`)가 다르면 `CONFLICT`다. 재조작 취소는 기존 점유와 기존 사용분을 건드리지 않는다 (`interfaces.py`, FR-19 본인 재조작).
- `isolate_device`는 기기만 `FAULT`로 둔다. 점유 세션과 승인 사용분은 바꾸지 않는다 (NFR-04).
- `finish_close(use_id, closed_at, end_reason)`는 세션 `CLOSED`, 기기 `AVAILABLE`, `closed_at`과 감사를 함께 저장한다. 검사 ID와 센서 근거를 받지 않는다 (FR-20).
- `finish_close`는 세션과 기기가 `CLOSING`이고 종료 사유가 `begin_close`의 값과 같은지, 같은 기기에 `in_progress` 구동 명령이 없는지 같은 트랜잭션에서 검사한다. 구동 명령이 있으면 `CONFLICT`·`COMMAND_IN_PROGRESS`다.
- 이미 `CLOSED`인 세션에 같은 종료 사유로 다시 부르면 `ALREADY_APPLIED`이고 처음 종료 시각을 유지한다. 다른 종료 사유는 `CONFLICT`·`CONTENT_CONFLICT`다.
- `close_result`는 `CLOSED` 세션의 `use_id`, `closed_at`, `end_reason`을 반환한다. 반납 완료인지는 호출자가 종료 사유로 확인한다.
- `restore_pending`은 일관된 읽기 시점에 조회한다. 조회 오류를 빈 목록으로 바꾸지 않는다 (FR-19 재시작 복원).
- `settle_recovery`는 연결과 예상 상태를 검사하고 승인, 근거, 사용분 정산, 점유 종료, 기기 복구, 감사를 한 트랜잭션에서 커밋한다. 같은 정산은 `settlement_digest`로 비교한다 (NFR-04, `architecture.md` §4 "복구 건과 복구 정산").
- `cancel_not_sent`는 `node_command` 행이 없어도 `expected=PENDING`으로 호출할 수 있다. 이때는 예약·사용분 정리만 반영하고 `StoreResult.command`는 `None`이다 (6절 결정, `specs/state-management/core-restore.md`).
- 응답에서 사용자 ID와 점유 주체를 지운다. `CommandRecord`, `StoreResult`, `RestoreBundle`에는 그 필드가 원래 없다 (FR-11, FR-21, NFR-08).
- 임계값과 시간 값은 코드 상수로 쓰지 않는다. 설정에서 받는다 (`AGENTS.md`).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/db/repositories/`
- `server/src/nearpilot/db/schema/`
- `server/tests/db/`
- `server/tests/integration/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/` (`core/policy` 포함), `api/`, `iot/`, `tools/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `ExecutionStore` 전체 | 제공 | 상태 관리 (`core/lifecycle`) |

### 만들지 않는 것
후속 spec이 맡는다.

| 후속 spec | 내용 | 근거 |
|---|---|---|
| `db-recovery-retention` | 보존기간 정리, 완료 20분 뒤 `request_id` 비우기, 중복 방지 10만 건 한도 | #15 |
| `db-approval-admin` | `AdminRepository`, 수집 회차 쓰기, 활성 비콘 교체 | #30, #41, #42 |
| `db-contract-followups` | `ExecutionStore.execution_context` 구현 | #83 |
| `db-contract-followups` | `ExecutionStore.link_internal_request` 구현 | #86 |
| `db-contract-followups` | `reserve_if_free()`가 `ReserveResult.reserved_at`에 실제 예약 시각을 저장·반환 | #80 |
| 상태 관리 (`core-fault-recovery`) | `RecoveryService.readiness`·`approve`, `settlement_digest` 계산 | #20 |
| — | 감사 로그 JSONL 내보내기 | #34 |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 새 요청을 처음 명령에 연결한다 | `APPLIED`와 새 `CommandRecord`(`PENDING`)를 반환한다 | `test_FR18_link_command_creates` |
| 같은 `internal_request_id`로 다시 연결한다 | `ALREADY_APPLIED`와 기존 `CommandRecord`를 반환한다. 새 행을 만들지 않는다 | `test_FR18_link_command_idempotent` |
| `cancel_not_sent`로 이미 취소된 요청에 `link_command`가 온다 | `CONFLICT`다. 명령 행을 만들지 않는다 | `test_FR19_link_command_after_cancel_conflict` |
| `PENDING` 명령을 전송 시작한다 | `APPLIED`와 `IN_PROGRESS`로 바뀐 `CommandRecord`를 반환한다 | `test_FR19_begin_send_applied` |
| 같은 명령으로 전송 시작을 다시 부른다 | `ALREADY_APPLIED`다. 재전송 허가가 아니다 | `test_FR19_begin_send_idempotent` |
| 같은 기기의 다른 명령이 결과를 확정하지 못했다 | `CONFLICT`와 `COMMAND_IN_PROGRESS`다 | `test_FR19_begin_send_command_in_progress` |
| 기기가 `CLOSING`이거나 `FAULT`다 | `CONFLICT`와 `STATE_CONFLICT`다 | `test_FR19_begin_send_state_conflict` |
| 이미 취소(`NOT_SENT`)된 명령을 전송 시작한다 | `CONFLICT`다 | `test_FR19_begin_send_after_cancel_conflict` |
| 두 스레드가 같은 명령에 `begin_send`와 `cancel_not_sent`를 동시에 부른다 | 한쪽만 `APPLIED`다 | `test_NFR03_begin_send_cancel_race` |
| `link_command` 전에 예약만 있는 요청을 `expected=PENDING`으로 취소한다 | `APPLIED`다. `StoreResult.command`는 `None`이다 | `test_FR19_cancel_not_sent_without_command` |
| 같은 요청의 취소를 다시 호출한다 | `ALREADY_APPLIED`다 | `test_FR19_cancel_not_sent_idempotent` |
| 이미 `IN_PROGRESS`인데 `expected=PENDING`으로 취소한다 | `CONFLICT`다 | `test_FR19_cancel_not_sent_state_conflict` |
| 최초 대여의 예약을 취소한다 | 기기 `AVAILABLE`, 세션 `CLOSED`·`cancelled`다 | `test_FR19_cancel_not_sent_initial_rental` |
| 재조작의 명령을 취소한다 | 기존 점유와 `rental_due_at`, 기존 대여의 사용분을 보존한다. 이번 요청의 사용분만 반환한다 | `test_FR19_cancel_not_sent_reoperation_scope` |
| 이미 결과가 있는 명령을 취소한다 | `CONFLICT`다 | `test_FR19_cancel_not_sent_result_exists_conflict` |
| 최초 대여가 성공해서 `SUCCEEDED`가 된다 | 기기·세션 `ACTIVE`, `rental_due_at`, 사용분 `consumed`다 | `test_FR19_record_result_succeeded_initial` |
| 재조작이 성공한다 | 기존 점유와 `rental_due_at`을 보존한다. 이번 사용분만 `consumed`다 | `test_FR19_record_result_succeeded_reoperation` |
| 기기가 `FAILED`를 보고한다 | 기기 `FAULT`, 사용분 `held`, 세션 보존이다 | `test_FR07_record_result_failed_isolates` |
| 결과가 `UNKNOWN`이다 | 기기 `FAULT`, 사용분 `held`다 | `test_FR07_record_result_unknown_isolates` |
| 최초 대여가 검증된 `DEVICE_BUSY`로 `REJECTED`된다 | 이번 예약 `cancelled`, 사용분 `released`다. 기기를 `FAULT`로 두지 않는다 | `test_FR19_record_result_rejected_initial` |
| 재조작이 `REJECTED`된다 | 기존 점유를 보존한다. 이번 사용분만 `released`다 | `test_FR19_record_result_rejected_reoperation` |
| 같은 명령에 같은 결과를 다시 보고한다 | `ALREADY_APPLIED`다 | `test_FR07_record_result_idempotent` |
| 같은 명령에 다른 결과가 상충한다 | `CONFLICT`다. 기존 결과를 보존한다 | `test_FR07_record_result_conflict` |
| `FAILED`로 확정한 뒤 늦은 `SUCCEEDED`가 온다 | `CONFLICT`다. `FAULT`를 해제하지 않는다 | `test_FR07_record_result_late_success_keeps_fault` |
| 센서 이상으로 기기를 격리한다 | `APPLIED`와 `FAULT`다. 세션·사용분은 그대로다 | `test_NFR04_isolate_device_applied` |
| 이미 `FAULT`인 기기를 다시 격리한다 | `ALREADY_APPLIED`다. 새 근거는 감사에 남긴다 | `test_NFR04_isolate_device_already_fault` |
| `ACTIVE` 세션의 반납을 시작한다 | `APPLIED`와 `CLOSING`이다 | `test_FR20_begin_close_applied` |
| 다른 미완료 구동 명령이 있는데 반납을 시작한다 | `CONFLICT`와 `COMMAND_IN_PROGRESS`다 | `test_FR19_begin_close_command_in_progress` |
| 같은 `use_id`로 반납 시작을 다시 부른다 | `ALREADY_APPLIED`다 | `test_FR20_begin_close_idempotent` |
| 논리적 종료를 저장한다 | `CLOSED`·`AVAILABLE`·`closed_at`·종료 사유를 함께 반영한다 | `test_FR20_finish_close_applied` |
| 종료 기록에 검사 열이 있다 | 없다. `check_id`·`check_evidence`를 지웠다 | `test_FR20_finish_close_has_no_check_columns` |
| 같은 `use_id`·같은 종료 사유로 다시 호출한다 | `ALREADY_APPLIED`다. 처음 종료 시각을 유지한다 | `test_FR20_finish_close_idempotent_keeps_first_closed_at` |
| 다른 종료 사유로 호출한다 | `CONFLICT`다 | `test_FR20_finish_close_conflict_different_reason` |
| 종료 직전에 구동 명령이 진행 중이다 | `CONFLICT`와 `COMMAND_IN_PROGRESS`다. `CLOSING`을 유지한다 | `test_FR19_finish_close_command_in_progress` |
| 기기·세션 상태가 저장 사이에 바뀌었다 | `CONFLICT`다 | `test_FR20_finish_close_state_conflict` |
| 완료 기록을 조회한다 | `CloseResult`를 반환한다 | `test_FR20_close_result_found` |
| 기기의 마지막 성공 명령을 조회한다 | 전송 시작이 가장 늦은 `succeeded` 명령을 반환한다. 없으면 `None`이다 | `test_FR20_last_succeeded_uses_send_order` |
| 취소로 끝난 세션의 기록을 조회한다 | 종료 사유 `cancelled`로 반환한다 | `test_FR20_close_result_reports_other_end_reason` |
| 완료 기록이 없다 | `None`을 반환한다. 미반영의 증거로 쓰지 않는다 | `test_FR20_close_result_none` |
| `execution()`으로 명령을 다시 조회한다 | 저장된 `CommandRecord`를 반환한다. 없으면 `None`이다 | `test_FR19_execution_lookup` |
| 복원 묶음을 조회한다 | 미완료 요청·명령·세션·사용분·`FAULT` 기기·복구 건을 기기별로 일관된 시점에 반환한다 | `test_FR19_restore_pending_bundle` |
| 명령에 연결되기 전에 멈춘 예약이 있다 | 복원 묶음의 `requests`에 포함한다. 명령 부재를 완료로 보지 않는다 | `test_FR19_restore_pending_includes_unlinked_request` |
| 점유 세션이 없는 공용형 사용분이 있다 | 복원 묶음에 포함한다 | `test_FR19_restore_pending_includes_shared_usage` |
| 새 복구 건을 저장한다 | `APPLIED`다 | `test_NFR04_save_recovery_applied` |
| 같은 내용을 다시 저장한다 | `ALREADY_APPLIED`다 | `test_NFR04_save_recovery_idempotent` |
| 같은 `recovery_id`에 다른 대상 연결로 저장한다 | `CONFLICT`다. 기존 연결을 바꾸지 않는다 | `test_NFR04_save_recovery_conflict` |
| 복구 건을 조회한다 | 저장된 `RecoveryRecord`를 반환한다. 없으면 `None`이다 | `test_NFR04_recovery_lookup` |
| 조건을 갖춘 복구 정산을 커밋한다 | `APPLIED`다. 승인 기록, 사용분 정산, 점유 종료, 기기 `AVAILABLE`, 완료 감사를 함께 반영한다 | `test_NFR04_settle_recovery_applied` |
| 같은 `recovery_id`·같은 논리 정산을 다시 요청한다 | `ALREADY_APPLIED`다. 기존 완료 결과를 반환한다 | `test_NFR04_settle_recovery_idempotent` |
| 다른 연결·요약값·예상 상태로 정산을 요청한다 | `CONFLICT`다 | `test_NFR04_settle_recovery_conflict` |
| 공용형 복구를 정산한다 | 점유 세션을 만들거나 종료하지 않는다 | `test_NFR04_settle_recovery_shared_no_session` |
| 두 스레드가 같은 복구 건을 동시에 정산한다 | 한쪽만 커밋한다. 중복 정산은 0건이다 | `test_NFR03_settle_recovery_concurrent_once` |
| 상태 관리의 `release.py`를 실제 SQLite 저장소로 돌린다 | fake 없이 논리적 반납 흐름이 끝난다 | `test_integration_release_with_sqlite_store` (`tests/integration/`) |
| DB가 잠겨서 쓰기를 시작하지 못한다 | `StoreNotApplied`를 던진다. 아무것도 바뀌지 않는다 | `test_NFR03_store_not_applied_on_busy` |
| `restore_pending` 조회가 실패한다 | 빈 목록이 아니라 예외를 던진다 | `test_FR19_restore_pending_error_raises` |

## 6. 결정

- `cancel_not_sent`는 `node_command` 행이 없어도 허용한다 — `specs/state-management/core-restore.md`가 명령 연결 전에 멈춘 예약도 취소 대상이라고 명시했다. 이때 예약(`use_session`)·사용분(`approval_usage`) 정리와 감사 기록만 반영하고, `StoreResult.command`는 `None`이다. #81(PR #103)로 결정됐다.
- 같은 `internal_request_id`가 취소된 뒤 `link_command`로 다시 오면 `CONFLICT`를 반환하고 명령 행을 만들지 않는다 — 취소된 요청에 새 명령이 뒤늦게 연결되면 미전송 확정이 무의미해진다. 담당자가 2026-10-15에 확정했다.
- 같은 취소를 다시 호출하면 `ALREADY_APPLIED`다 — `ExecutionStore` 전체의 재호출 규칙과 같다. 담당자가 2026-10-15에 확정했다.
- `recovery` 정산 내용은 컬럼을 늘리지 않고 JSON TEXT 컬럼 `settlement_json` 하나에 저장한다 — `RecoverySettlement`의 `expected_node_state`, `expected_recovery_status`, `expected_commands`, `expected_sessions`, `usage_settlements`, `use_id`, `closed_at`, `end_reason`을 한 번에 직렬화해서 보존한다. `settlement_digest`, `approved_by_user_id`, `approved_at`, `completed_at`은 `0001_initial.sql`의 기존 컬럼을 그대로 쓴다. 담당자 지시.
- `0002_execution_store.sql`이 더한 `use_session.check_id`, `check_evidence`는 `0007_drop_release_check.sql`로 지운다 — 2026-10-26 결정으로 반납 검사를 제외했다. 이전 마이그레이션은 고치지 않는다 (`db/schema/__init__.py`의 `PRAGMA user_version` 마이그레이션 규칙).
- `finish_close`의 재호출 판단은 종료 사유로 한다 — 검사 ID가 없어졌다. 종료 시각이 달라도 같은 사유이면 처음 기록을 유지한다.
- 마이그레이션이 7개가 되어 `server/tools/evaluation/db.py`의 `EXPECTED_SCHEMA_VERSION`도 7로 올린다 — `test_NFR13_expected_schema_version_matches_migrations`가 둘을 대조한다.
- 기존 점유 세션·미완료 반납의 이전 절차는 미결 (#196)이다. 배포한 운영 데이터가 없어서 검사 열을 이전 없이 지운다고 가정한다 (담당자 확인, 2026-10-26).
- `settlement_digest`의 해시·직렬화 방식은 상태 관리가 계산해서 넘긴다. DB는 비어 있지 않은 임의의 문자열로 저장하고 문자열 비교만 한다 — `architecture.md` §4 "복구 건과 복구 정산"이 이미 계산 책임을 상태 관리에 두었다. 이 선택은 DB 구현을 막지 않는다.
  - 상태 관리가 정한 방식(#92): 정규 JSON(키 정렬, 공백 없음, UTF-8)으로 직렬화한 뒤 SHA-256을 적용한다. 값은 소문자 hex 64자다. `core/lifecycle/fault_recovery.settlement_digest()`가 계산한다. DB는 이 값을 계산하지 않고 `recovery.settlement_digest` 컬럼에 저장·대조만 한다.
- `SqliteExecutionStore`를 `db/repositories/execution_store.py`에 둔다. `SqliteRepository`와 같은 `sqlite3.Connection`을 받는다 — DB 쓰기 경로는 하나다 (`AGENTS.md` 의존 규칙 5).
- `CommandRecord.reserved_at`은 `request.received_at`을 그대로 쓴다. 새 컬럼을 두지 않는다 — ⓪~⑥ 판정과 `reserve_if_free()`는 같은 호출 안에서 동기적으로 끝나므로 "예약 시각"과 "요청 수신 시각"은 실질적으로 같다. `use_session`과 `request`에는 애초에 별도의 예약 시각 컬럼이 없다(`0001_initial.sql`). `link_command`는 `now`를 인자로 받지 않으므로(`interfaces.py`) 호출 시각을 대신 쓸 수도 없다. #80으로 DB가 실제 예약 시각을 `ReserveResult.reserved_at`에 저장해 돌려주기로 했다. 이번 작업은 임시로 수신 시각을 쓰고, 반영은 `db-contract-followups`에서 한다.
- `link_command`의 실행 종류 판단은 `node.occupancy`를 가장 먼저 보고, 대여형이면 이번 요청의 점유 세션 유무 → 기기의 `ACTIVE` 세션 유무 순서로만 본다. `approval_usage` 행의 존재는 보지 않는다 — `open` 정책의 재조작은 승인 사용분을 예약하지 않아 행이 없으므로(#27), 사용분로 판단하면 공용형과 구분하지 못한다.
- `restore_pending`은 `BEGIN IMMEDIATE`가 아니라 새로 만든 `BEGIN DEFERRED`(`deferred_transaction`)로 여러 조회를 하나의 스냅숏으로 묶는다 — 읽기만 하므로 쓰기 잠금을 잡아 다른 트랜잭션을 기다리게 할 필요가 없다.
- "명령 연결 전의 예약 요청"은 `request.verdict = 'OK'`이고 `node_command`에 연결이 없으며, **세션이 `RESERVED`이거나 승인 사용분이 `reserved`인, 예약이 남은 요청**으로 찾는다 — 판정(`core/decision/pipeline.py`)이 실행 인계보다 먼저 `save_decision()`으로 `request.node_id`를 저장하므로, 명령이 생기기 전에도 대상 기기를 알 수 있다. `verdict = 'OK'`만으로는 `open` 정책의 공용형 성공(#27, 쓰기 없음)까지 포함돼 복원할 예약이 없는 요청이 섞인다. 이미 `cancel_not_sent`로 취소된 요청도 세션·사용분 상태가 `RESERVED`·`reserved`가 아니게 되므로 이 조건에서 자연히 빠진다.
- `RestoredSession.created_at`은 세션을 만든 `internal_request_id`의 `request.received_at`을 쓴다. `use_session`에는 생성 시각 컬럼이 없다 — `CommandRecord.reserved_at`과 같은 근거다.
- `RestoredSession.evidence`는 채우지 않는다(기본값 `{}`) — "종료 사유와 관련 근거"는 `CLOSED`된 세션에만 의미가 있는데, `restore_pending`은 `RESERVED`·`ACTIVE`·`CLOSING`만 돌려준다.
- `recovery.evidence` 컬럼은 `RecoveryEvidence`의 필드(`evidence_id`, `session_id`, `observed_at`, `execution_outcome`, `command_idle`)를 그대로 담은 JSON 배열로 저장한다 — `save_recovery`(7단계)가 쓸 형식을 `restore_pending`의 읽기 쪽에서 먼저 정했다. `restore_pending`은 `completed_at IS NULL`인 행만 돌려주므로 그 호출에서는 `RecoveryRecord.completion`이 항상 `None`이다. `recovery()`는 `completed_at`이 있으면 `completion`을 채운다.
- `save_recovery`는 대상 연결(`node_id`, `use_id`, `internal_request_ids`, `command_ids`, `created_at`)이 같은지만 보고 다르면 `CONFLICT`·`LINK_MISMATCH`다. 내용(`status`, `evidence`)이 같으면 `ALREADY_APPLIED`, 다르면 갱신하고 `APPLIED`다. 이미 완료된(`completed_at` 있음) 복구 건은 새 근거로 덮어쓰지 않고 `ALREADY_APPLIED`와 기존 완료 결과를 반환한다 — "완료 뒤의 새 보고로 당시 근거를 덮어쓰지 않는다"(architecture.md §4).
- `settle_recovery`는 완료 여부 → 대상 연결(`node_id`, `use_id`) → 기기 상태 → 복구 건 상태 → 명령별 예상 상태 → 세션별 예상 상태 → 사용분별 예상 상태 순서로 검사한다. 하나라도 다르면 `CONFLICT`다(연결 불일치는 `LINK_MISMATCH`, 나머지는 `STATE_CONFLICT`). 완료된 복구 건은 `settlement_digest`로만 비교한다 — 같으면 `ALREADY_APPLIED`, 다르면 `CONFLICT`·`CONTENT_CONFLICT`다.
- `settle_recovery`가 기기를 복구하는 목표 상태는 점유 방식과 관계없이 `AVAILABLE`이다 — 공용형은 `reserve_if_free()`가 `device_state`를 건드리지 않는 것과 같은 이유로, 대여형의 상태 머신(`RESERVED`·`ACTIVE`·`CLOSING`) 밖에서는 `AVAILABLE`이 기본값이다.
- `settle_recovery`의 완료 시각(`completed_at`)은 DB가 커밋 시점의 시각으로 만든다. `RecoverySettlement.approved_at`(소유자 승인 시각)과는 다른 값이다.
- `settle_recovery`의 감사 기록은 `Step.FAULT`를 쓴다 — `isolate_device`와 같은 장애·복구 범주다. `internal_request_id`·`command_id` 없이 `recovery_id`로 식별한다.

## 7. 가정

- #84는 PR #105로 결정됐다(`Repository.is_place_owner()`). 상태 관리의 `RecoveryService`가 아직 구현 전이라 `core/lifecycle` 통합 테스트는 하지 않는다 — `ExecutionStore` 호출만으로 `settle_recovery`를 검증한다.
- 가정 — #113: A안(근거에는 JSON 기본 타입만 넣는다. 시각은 ISO 문자열로 넣는다)으로 정해졌다. 저장 함수(`cancel_not_sent`·`record_result`·`finish_close`)의 직접 반환값은 이번 호출의 `evidence`를 그대로 돌려주고, 재조회 비교는 양쪽을 정규화한 JSON으로 맞춰서 한다(`_evidence_matches()`). `execution()`·`close_result()` 같은 재조회 함수 자체가 돌려주는 값은 여전히 문자열화돼 있다 — `core/lifecycle/release.py`의 `_matches()`(355줄)와 상태 관리의 `StoreOutcomeUnknown` 뒤 재조회 비교가 영향받을 수 있다. SQLite로 재현했다 (`test_FR19_execution_evidence_with_datetime_mismatches_after_requery`, `test_FR20_close_result_evidence_with_datetime_mismatches_after_requery`). 상태 관리가 `evidence`에서 `datetime`을 빼도록 바꿀 때까지 지금 방식을 유지한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-06, FR-18 | `test_FR18_link_command_*` | 명령 연결의 멱등성과 취소 뒤 연결 충돌 |
| FR-07 | `test_FR07_record_result_*` | 실패·불명·늦은 결과·중복 결과의 격리와 보류 |
| FR-19 | `test_FR19_*` | 전송 시작·취소의 경합과 멱등성, 재조작 범위, 복원 조회 |
| FR-20 | `test_FR20_*` | 반납 시작·종료의 원자성과 검사 근거 보존 |
| FR-21, NFR-08 | 전체 | 응답 모델에 사용자 ID·점유 주체 필드가 없다 (타입 검사로 대신한다) |
| NFR-03 | `test_NFR03_*` | 전송·취소·복구 정산의 동시 호출에서 이중 반영이 없다 |
| NFR-04 | `test_NFR04_*` | 격리·복구 건 저장·복구 정산의 원자성과 멱등성 |

- `tests/db/`는 fake 없이 SQLite 임시 파일로 확인한다. `tests/integration/`은 상태 관리의 `release.py`(또는 `fault_recovery.py`)를 실제 저장소로 돌린다.
