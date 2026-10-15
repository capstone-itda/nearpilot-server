# 결정 반영 후속 저장 (`Repository`·`ExecutionStore` 추가 접점)

- 상태: 완료
- 담당: paiwhy (DB)
- 요구사항: FR-03, FR-07, FR-10, FR-12A, FR-14, FR-19, FR-20, FR-21, FR-22
- 따르는 결정: #60 (PR #70), #78·#80·#81 (PR #103), #83 (PR #104), #84 (PR #105), #86 (PR #111)

## 1. 왜

- 팀 결정으로 `shared`에 DB가 제공할 접점이 늘었다. `db/`에는 아직 구현이 없다.
  - `Repository.approvals_in_scope()`가 없으면 정책 검사가 승인 필요 사유를 `APPROVAL_REQUIRED` 하나로 뭉뚱그린다 (#60).
  - `ReserveResult.reserved_at`이 없으면 상태 관리가 실행 인계 전 예약의 만료를 잴 수 없다 (#80).
  - `ExecutionStore.execution_context()`가 없으면 재시작 뒤 늦게 온 결과를 원래 문맥으로 정산할 수 없다 (#83).
  - `Repository.is_place_owner()`가 없으면 복구 조회·승인에서 장소 소유권을 확인할 수 없다 (#84).
  - `ExecutionStore.link_internal_request()`가 없으면 서버가 시작하는 이탈 꺼짐 명령을 저장할 수 없다 (#86).

## 2. 무엇

- 입력:
  - `approvals_in_scope(user_id, node_id)`, `is_place_owner(user_id, node_id)`
  - `reserve_if_free(ReserveRequest)`의 기존 입력
  - `save_decision(Decision, DecisionRecord)`의 `DecisionRecord.settings`
  - `execution_context(command_id)`, `link_internal_request(InternalRequest)`
- 출력:
  - `Sequence[ApprovalRecord]`, `bool`
  - `ReserveResult.reserved_at`
  - `ExecutionContext | None`
  - `StoreResult`
- 바뀌는 상태:
  - 새 마이그레이션으로 `use_session.reserved_at`, `approval_usage.reserved_at`을 추가한다.
  - 새 마이그레이션으로 `request.user_id`를 비울 수 있게 하고 요청 종류 `request_kind` (`visitor`, `server_internal`)를 추가한다. `request`의 적용 설정 전체를 저장할 `settings_json`을 추가한다.
  - `reserve_if_free`가 예약 시각을 저장한다. `save_decision`이 적용 설정 전체를 저장한다.
- 약속:
  - 조회 함수(`approvals_in_scope`, `is_place_owner`, `execution_context`)는 저장소를 바꾸지 않는다.
  - 쓰기 함수 하나는 `BEGIN IMMEDIATE` 트랜잭션 하나다.
  - 같은 내용의 재호출은 `ALREADY_APPLIED`, 다른 내용은 `CONFLICT`다.
  - 응답에 사용자 ID와 점유 주체를 넣지 않는다.

## 3. 제약

- `approvals_in_scope`는 요청자·기기의 승인 이력을 모두 반환한다. 시각·동작·정책 버전으로 거르지 않는다. 만료된 대기 신청도 `status`는 `PENDING`이다 (`interfaces.py`, #60).
- `reserve_if_free`는 예약을 쓴 결과에 저장한 예약 시각을 담는다. 대여형은 `use_session.reserved_at`, 승인이 필요한 공용형은 `approval_usage.reserved_at`에 저장한다. 쓰기가 없는 결과(공용형 `open`, 거부)는 `None`이다 (#80, `architecture.md` §4 "예약과 승인 사용분").
- 같은 요청의 중복 예약은 처음 저장한 예약 시각을 그대로 반환한다 (#80, FR-19).
- `save_decision`은 `DecisionRecord.settings`의 전체 스냅샷을 저장한다. 버전만 저장하지 않는다 (#83).
- `execution_context`는 저장한 요청, 확정 `node_id`, `use_id`·`usage_id`, `execution_kind`, `reserved_at`과 저장한 설정 스냅샷으로 문맥을 만든다. 현재 설정으로 채우지 않는다. 명령이 없으면 `None`이다. 조회 오류는 `StoreError`다 (#83).
- `is_place_owner`는 소유 여부만 반환한다. 소유 계정 ID는 반환하지 않는다. 기기가 없거나 장소에 배치되지 않았으면 `False`다. 조회 오류는 `StoreError`다 (#84, FR-11).
- `link_internal_request`는 사용자 ID와 판정 기록 없이 요청을 저장한다. 적용 설정을 함께 보존한다. 같은 내용은 `ALREADY_APPLIED`, 다른 내용은 `CONFLICT`다 (#86).
- 서버 내부 요청은 이후 `link_command`, `begin_send`, `record_result`, `cancel_not_sent`를 그대로 쓴다. 세션과 사용분이 없으므로 명령, 기기 상태와 감사만 반영한다 (#86).
- 서버 내부 요청은 방문자의 `use_id` 조회와 사용자별 조회에 나타나지 않는다 (#86, FR-21).
- `cancel_not_sent`의 새 사유 `RESERVATION_EXPIRED`, `NODE_NOT_CONNECTED`, `COMMAND_NOT_SENT`는 받은 값을 그대로 저장한다 (#78).
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
| `Repository.approvals_in_scope` | 제공 | 정책 검사 (`core/policy`, DB 담당) |
| `Repository.reserve_if_free`의 `reserved_at` | 제공 | 판정 |
| `Repository.is_place_owner` | 제공 | 상태 관리 (복구 조회·승인) |
| `ExecutionStore.execution_context` | 제공 | 상태 관리 (재시작 뒤 결과 정산) |
| `ExecutionStore.link_internal_request` | 제공 | 상태 관리 (이탈 꺼짐) |

### 만들지 않는 것
| 후속 spec | 내용 | 근거 |
|---|---|---|
| `core-policy-reasons` | `core/policy`에서 `approvals_in_scope`로 승인 필요 사유 7개 구분 | #60 |
| `db-recovery-retention` | 보존기간 정리, 완료 20분 뒤 `request_id` 비우기, 중복 방지 10만 건 한도 | #15 |
| `db-approval-admin` | `AdminRepository`, 수집 회차 쓰기, 활성 비콘 교체 | #30, #41, #42 |
| — | 근거(`evidence`) 저장 형식 | #113 결정 뒤 |
| — | `link_request`의 `INTERNAL_ERROR` 재전송 재판정 설계(판정 칸을 비우고 `is_new=True`로 새 판정을 일으킴) | #76 — 은지(eunji719) 확인 대기, 확인되면 별도 PR |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 승인 이력이 없다 | 빈 목록을 반환한다 | `test_FR14_approvals_in_scope_empty` |
| 대기·거부·만료·소진·다른 동작의 승인이 섞여 있다 | 거르지 않고 모두 반환한다. 사용자 ID 필드가 없다 | `test_FR14_approvals_in_scope_returns_all` |
| 대기 신청이 만료됐다 | `status`는 `PENDING`이고 `pending_until`이 지나 있다 | `test_FR14_approvals_in_scope_expired_pending` |
| 다른 사용자나 다른 기기의 승인이 있다 | 포함하지 않는다 | `test_FR14_approvals_in_scope_scope_only` |
| 대여형 최초 예약을 한다 | `reserved_at`을 저장하고 반환한다 | `test_FR19_reserve_returns_reserved_at` |
| 승인이 필요한 공용형 예약을 한다 | 사용분에 `reserved_at`을 저장하고 반환한다 | `test_FR19_reserve_shared_usage_reserved_at` |
| 공용형 `open` 요청이나 거부 결과다 | `reserved_at`은 `None`이다 | `test_FR19_reserve_no_write_reserved_at_none` |
| 같은 요청으로 예약을 다시 호출한다 | 처음 저장한 `reserved_at`을 반환한다 | `test_FR19_reserve_duplicate_keeps_reserved_at` |
| 판정을 저장한다 | 적용 설정 전체를 저장한다 | `test_FR22_save_decision_stores_settings_snapshot` |
| 명령 ID로 문맥을 조회한다 | 저장한 요청, 대상, 연결, 실행 종류, 예약 시각, 설정 스냅샷으로 만든 문맥을 반환한다 | `test_FR07_execution_context_found` |
| 조회 사이에 현재 설정이 바뀌었다 | 저장한 설정 스냅샷을 반환한다 | `test_FR07_execution_context_uses_saved_settings` |
| 명령이 없다 | `None`을 반환한다 | `test_FR07_execution_context_none` |
| 서버 내부 요청의 명령을 조회한다 | `request`가 `None`이고 `internal_request`를 담은 문맥을 반환한다 | `test_FR20_execution_context_server_internal` |
| 장소 소유자가 조회한다 | `True`다 | `test_FR12A_is_place_owner_true` |
| 다른 계정이 조회한다 | `False`다 | `test_FR12A_is_place_owner_false` |
| 기기가 없거나 장소에 배치되지 않았다 | `False`다 | `test_FR12A_is_place_owner_unplaced` |
| 서버 내부 요청을 저장한다 | 사용자 ID 없이 저장한다. `APPLIED`다 | `test_FR20_link_internal_request_applied` |
| 같은 서버 내부 요청을 다시 저장한다 | `ALREADY_APPLIED`다 | `test_FR20_link_internal_request_idempotent` |
| 같은 ID에 다른 내용으로 저장한다 | `CONFLICT`다 | `test_FR20_link_internal_request_conflict` |
| 서버 내부 요청에 명령을 연결하고 결과를 저장한다 | 세션과 사용분 없이 명령, 기기 상태, 감사만 반영한다 | `test_FR20_server_internal_command_flow` |
| 사용자별 조회를 한다 | 서버 내부 요청이 나타나지 않는다 | `test_FR21_server_internal_hidden` |
| 기존 DB를 새 마이그레이션으로 올린다 | 기존 요청 행이 그대로 남고 `request_kind`는 `visitor`다 | `test_schema_migration_keeps_requests` |

## 6. 결정

- 예약 시각은 대여형 `use_session.reserved_at`, 승인이 필요한 공용형 `approval_usage.reserved_at`에 저장한다 — PR #101 리뷰에서 담당자가 정했다. 공용형 `open`은 저장할 행이 없으므로 `None`이다.
- 예약 시각은 `request.received_at`을 저장한다 — `ReserveRequest`에 시각 필드가 없고, DB가 직접 현재 시각을 읽지 않아 결과가 결정적이다. 수신 시각은 실제 예약보다 조금 이르므로 예약 대기 만료가 이르게 잡히는 안전한 쪽이다.
- `request` 테이블은 새로 만들어 옮긴다. `user_id`를 비울 수 있게 하고 `request_kind`를 추가한다 — SQLite는 기존 컬럼의 NOT NULL을 바꿀 수 없다. 가짜 계정을 만들지 않는다는 #86의 약속을 지킨다.
  - 순서: `PRAGMA foreign_keys=OFF` → 새 테이블 생성 → 행 복사 → 이전 테이블 삭제 → 이름 변경 → 인덱스 재생성 → `PRAGMA foreign_key_check` → `PRAGMA foreign_keys=ON`.
  - `request_kind`가 `visitor`이면 `user_id`가 반드시 있다는 CHECK 제약을 둔다.
- 적용 설정은 `request.settings_json`에 JSON으로 저장한다 — 방문자 요청은 `save_decision`이, 서버 내부 요청은 `link_internal_request`가 쓴다.
- 서버 내부 요청의 원인(`InternalRequestCause`)은 `request.internal_cause`에 저장한다 — 방문자 요청에만 의미 있는 `reason_code`(판정 사유)에 덮어 쓰지 않는다. 방문자 요청은 `NULL`이다. `target_spec`·`input_digest`는 방문자 요청의 근접 대상 지정·중복 방지에만 쓰여 서버 내부 요청에는 의미가 없다. `target_spec`은 `node_id`를 그대로 담아 NOT NULL을 만족시키고, `input_digest`는 빈 문자열이다 — `execution_context`가 서버 내부 요청을 `UseRequest`로 복원하지 않으므로 다시 해석되지 않는다.
- `link_command`는 `request.request_kind`를 먼저 본다 — `server_internal`이면 `node.occupancy`로 판단하지 않고 곧바로 `ExecutionKind.SERVER_INTERNAL`과 `use_id=None`·`usage_id=None`을 쓴다. 공용형 기기(점유 방식 `shared`)에서 시작한 서버 내부 요청을 `SHARED`로 잘못 분류하지 않기 위해서다.

## 7. 가정

- 근거(`evidence`) 저장 형식은 `db-execution-store`의 방식을 그대로 쓴다 — #113
- `db-execution-store`가 이 작업 전에 병합된다. 이 작업은 그 위에 함수를 추가한다.
- #76의 `INTERNAL_ERROR` 재전송 재판정 방식은 은지(eunji719) 확인을 기다린다 — 가정, 은지 확인 대기. 확인되면 별도 PR로 구현한다.
  - `link_request`가 같은 `(user_id, request_id)`·같은 `input_digest`이고 저장된 `reason_code`가 `INTERNAL_ERROR`면, `BEGIN IMMEDIATE` 안에서 이전 판정을 감사에 남기고 판정 칸을 `NULL`로 비운 뒤 `RequestLink(internal_request_id, is_new=True, conflict=False)`를 반환한다 — 새 요청처럼 다시 판정하게 한다. `save_decision`은 바꾸지 않는다.
  - 판정 칸을 비우는 `UPDATE`는 `WHERE reason_code = 'INTERNAL_ERROR'`로 조건부로 한다. 동시에 두 재전송이 오면 한쪽만 조건에 걸려 비우고 `is_new=True`를 받는다. 다른 쪽은 그사이 `verdict`가 `NULL`이 된 행을 보고 기존의 "처리 중" 분기(`is_new=False`, `previous=None`)를 그대로 탄다 — 새 분기를 추가하지 않는다.
  - 이 방식을 고른 이유: `core/decision/pipeline.py`는 `is_new=True`면 바로 전체 판정(`_run`)으로 들어가고, `is_new=False`·`previous=None`이면 "처리 중"으로 보고 재판정 없이 `INTERNAL_ERROR`를 반환한다(161~165줄). `shared`의 `RequestLink` 모양을 바꾸지 않고 이 기존 분기만으로 재판정을 일으키려면, DB가 `is_new` 값 자체로 신호를 보내야 한다. `core/`는 고치지 않는 곳이라 pipeline.py의 분기를 바꿀 수 없다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-03, FR-14 | `test_FR14_approvals_in_scope_*` | 승인 이력 전체 반환과 범위 |
| FR-19 | `test_FR19_reserve_*` | 예약 시각 저장·반환과 중복 예약 |
| FR-07, FR-22 | `test_FR07_execution_context_*`, `test_FR22_save_decision_*` | 설정 스냅샷 보존과 원래 문맥 조회 |
| FR-12A | `test_FR12A_is_place_owner_*` | 소유 여부만 반환 |
| FR-20, FR-21 | `test_FR20_*internal*`, `test_FR21_server_internal_hidden` | 서버 내부 요청의 저장, 실행, 숨김 |
| — | `test_schema_migration_keeps_requests` | 마이그레이션이 기존 데이터를 보존한다 |
