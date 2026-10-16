# DB 스키마와 저장소 (`Repository`, `AccountLookup`, `AuditLog`)

- 담당: paiwhy (DB)
- 후속 상태: #165 용량 집계와 승인 신청 예외를 이 fix PR에서 구현한다.
- 후속 초안 작성: JM-220이다.
- 요구사항: FR-01, FR-03, FR-05, FR-13, FR-14, FR-15, FR-19, FR-21, FR-22, NFR-03, NFR-08
- 따르는 결정: #15, #17, #20, #24, #27, #28, #29, #31, #32, #33, #34, #39, #40, #41, #42, #44, #54, #165

## 1. 왜

- `shared/interfaces.py`에는 DB가 제공할 `Repository`, `AccountLookup`, `AuditLog`가 있다. `db/`에는 구현이 없다.
- 구현이 없으면 판정은 ⓪ 중복 검사, ① 비콘 조회, ② 후보 조회, ④ 정책 조회, ⑤⑥ 점유 예약을 fake로만 실행한다.
- 구현이 없으면 API는 토큰으로 계정을 식별하지 못한다.
- 구현이 없으면 소유자는 접근 승인을 신청받거나 결정하지 못한다.
- 테이블이 없으면 DB의 후속 작업(`ExecutionStore`, 정책 검사, 관리 기능 쓰기, 복구)도 시작하지 못한다.

## 2. 무엇

- 입력: `shared`의 값 객체 (`UseRequest`, `Decision`, `DecisionRecord`, `RssiFrame`, `NodeAnnounce`, `ReserveRequest`, `ApprovalRequest`, `ApprovalDecisionRequest`, `AuditEvent`)
- 출력: `shared`의 값 객체 (`RequestLink`, `BeaconKey`, `NodeInfo`, `PolicyRevision`, `Approval`, `ApprovalRequestResult`, `ApprovalDecisionResult`, `ReserveResult`, `StoreResult`, `UseSession`, `Account`)
- 바뀌는 상태:
  - SQLite 파일 하나에 이 작업과 후속 작업이 쓸 테이블을 모두 만든다. `node_command`는 후속 작업이 쓸 컬럼까지 지금 만든다.
  - `request`, `rssi_observation`, `node`, `use_session`, `approval`, `approval_usage`, `audit_log` 행이 생기거나 바뀐다.
- 약속:
  - `Repository`, `AccountLookup`, `AuditLog`의 모든 함수를 구현한다. 시그니처는 `shared`와 같다.
  - `reserve_if_free()`는 같은 `internal_request_id`의 기존 예약이 없고 본인 기기가 `ACTIVE`이면 `REOPERATING`과 기존 `use_id`를 반환한다. 그 외에는 ⑤⑥ 검사, `RESERVED` 생성, 승인 사용분 예약을 `BEGIN IMMEDIATE` 트랜잭션 하나에서 처리한다.
  - 동시 요청이 같은 기기를 예약하면 한 요청만 `RESERVED`를 받는다.
  - 같은 `internal_request_id`로 다시 예약하면 기존 예약을 반환한다. 새 행을 만들지 않는다.
  - `create_approval_request()`는 같은 요청자·기기·동작·정책 버전에 유효한 `pending` 신청이 있으면 기존 신청을 반환한다. `decide_approval()`은 `pending`·정책 버전·대기 만료를 조건부로 검사하고 결정을 한 번만 반영한다.
  - 스키마는 후속 작업이 쓸 테이블과 필드를 미리 포함한다. 후속 작업은 함수만 추가한다.

### 만드는 테이블

| 테이블 | 이 작업에서 쓰는가 | 비고 |
|---|---|---|
| `user`, `beacon`, `place`, `node` | 읽기, 발표 반영 | 계정당 활성 비콘은 하나다 (6절 #42). `node`에는 `anchor_params`를 두지 않는다 (6절 #40) |
| `access_policy`, `policy_revision` | 읽기 | 정책 변경 쓰기는 후속 작업이다 (#30) |
| `approval` | 읽기, 신청·결정 쓰기 | `create_approval_request`·`decide_approval`로 쓴다 (#28) |
| `approval_usage` | 예약 | 소진·보류·반환은 후속 작업이다 (#17) |
| `request` | 연결, 판정 저장 | 외부 `request_id`는 비울 수 있다 (7절 #15) |
| `rssi_observation` | 저장, 조회 | `label` 컬럼을 두지 않는다. `collection_id`로 수집 회차를 식별한다 (6절 #41) |
| `collection_run` | 진행 중 회차 조회 | `label`, `started_at`, `ended_at`을 둔다. 회차 쓰기는 후속 작업이다 (#41) |
| `use_session` | 예약, 조회 | `rental_due_at`, `closed_at`, 생성 당시 종료 정책 |
| `node_command` | 컬럼 전체를 만든다. 행 쓰기는 안 한다 | `ExecutionStore`(`db-execution-store`)가 쓴다. 지금 전체 컬럼을 만들어 후속 마이그레이션을 없앤다 |
| `recovery` | 만들기만 한다 | 복원 조회와 복구 정산 후속 작업 (#17, #20) |
| `audit_log` | 추가 | JSONL 내보내기는 후속 작업이다 (#34) |

## 3. 제약

- DB 쓰기 경로는 이 저장소 하나다. 대여형의 ⑤⑥, `RESERVED` 생성, 승인 사용분 예약은 `BEGIN IMMEDIATE` 하나에서 처리한다 (`AGENTS.md` 의존 규칙 5, FR-19).
- 동시 요청과 재전송에서 이중 점유는 0건이다 (NFR-03).
- 본인 `ACTIVE` 세션에 재조작을 요청하면 `reserve_if_free()`는 `REOPERATING`을 반환하고 새 세션을 만들지 않는다. 이번 요청의 승인 사용분만 새로 예약한다 (FR-19, FR-21).
- 본인 `ACTIVE` 세션이 있어도 기기가 `FAULT`로 격리됐으면 `reserve_if_free()`는 `DEVICE_BUSY`를 반환한다. 세션은 `ACTIVE`로 보존되고 기기만 격리될 수 있기 때문이다 (FR-19, NFR-04).
- 최초 대여 요청이 `ACTIVE`가 된 뒤 같은 `internal_request_id`로 다시 예약을 호출하면 `reserve_if_free()`는 본인 재조작 검사보다 기존 예약 조회를 먼저 해서 기존 `use_id`로 `RESERVED`를 반환한다 (FR-19).
- `input_digest`는 판정이 계산해서 넘긴다. DB는 값을 저장하고 비교만 한다 (#24).
- 다른 계정에 이미 있는 `(uuid, major, minor)`는 저장하지 않는다 (FR-01).
- 토큰 원문은 저장하지 않는다. `token_hash`만 저장하고 UNIQUE로 둔다 (NFR-08, §1.3).
- retained 기능 발표는 `last_seen_at`을 바꾸지 않는다. 오래된 시각은 `last_seen_at`을 바꾸지 않는다 (FR-13).
- `ONLINE`과 `OFFLINE`은 저장하지 않는다. `last_seen_at`과 TTL로 계산한다 (FR-13).
- 신뢰한 기기가 다른 기능이나 점유 방식으로 발표하면 신뢰 상태를 `discovered`로 되돌리고, 같은 트랜잭션에서 감사 로그를 `Step.ANNOUNCE`로 남긴다 (FR-13, FR-22).
- 과거 `policy_revision` 행은 바꾸지 않는다 (FR-14).
- `create_approval_request()`와 `decide_approval()`은 승인 하나의 상태를 조건부로만 바꾼다. 이미 반영한 결정을 다시 쓰지 않는다 (FR-14).
- 승인 허용 횟수는 `reserved`, `held`, `consumed` 사용분을 합쳐서 검사한다 (`architecture.md` §6).
- 같은 외부 키에 다른 입력이 오면 기존 요청을 덮어쓰지 않는다 (FR-22).
- `save_decision()`은 `DecisionRecord`의 명시 필드를 `request` 행의 `model`, `calib`, `policy_ver`, `config_ver`, `decision_context`에 저장한다 (FR-22).
- 초과시간은 저장하지 않는다. `rental_due_at`과 `closed_at`만 저장한다 (FR-19).
- 등록된 활성 비콘의 프레임은 기기 신뢰 여부와 관계없이 저장한다. 신뢰로 거르는 일은 근접 추정이 한다 (FR-05, #31, #39).
- `rssi_observation`에는 `(node_id, session_id, seq, beacon)` UNIQUE를 둔다. 같은 조합은 한 번만 저장한다 (FR-05).
- 수집 회차 조회는 `started_at <= frame_ts < ended_at`로 한다. 시작은 포함하고 끝은 포함하지 않는다. 회차 찾기와 관측 저장은 같은 트랜잭션에서 한다 (FR-05, #41).
- `node`에는 근접 파라미터(`anchor_params`)를 저장하지 않는다. 장소별 모델 파일이 보존한다 (#40).
- 임계값과 시간 값은 코드 상수로 쓰지 않는다. 설정에서 받는다 (`AGENTS.md`).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/db/schema/`
- `server/src/nearpilot/db/repositories/`
- `server/src/nearpilot/db/audit/`
- `server/tests/db/`
- #165 후속 검증: `server/tests/integration/`이다.

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/` (`core/policy` 포함), `api/`, `iot/`, `tools/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `Repository` 전체 | 제공 | 판정, 근접 추정, 상태 관리 |
| `AccountLookup.account_by_token_hash` | 제공 | 판정 (`api/auth`) |
| `AuditLog.append` | 제공 | 판정, 상태 관리 |

### 만들지 않는 것
후속 spec이 맡는다. 테이블은 이 작업에서 만든다.

| 후속 spec | 내용 | 근거 |
|---|---|---|
| `db-policy-check` | `core/policy`의 `PolicyChecker.check()` | #23, PR #45 |
| `db-execution-store` | 예약 이후 사건별 저장 (`ExecutionStore`), `close_result()`, 기기 거부 결과 저장 | #17 |
| `db-approval-admin` | 관리 기능 쓰기 (`AdminRepository`), 수집 회차 쓰기, 활성 비콘 교체 | #30, #41, #42 |
| `db-recovery-retention` | 복원 조회, 복구 정산, 보존기간 정리, 중복 방지 10만 건 한도 | #15, #17, #20 |

- 감사 로그의 JSONL 내보내기 (#34)
- 평가 스크립트 (`tools/evaluation/`)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 같은 `(계정, request_id)`와 같은 `input_digest`가 다시 온다 | `is_new=False`, `conflict=False`다. 판정이 저장돼 있으면 `previous`에 담는다. | `test_FR22_link_request_resend` |
| 같은 `(계정, request_id)`에 다른 `input_digest`가 온다 | `conflict=True`다. 기존 행을 바꾸지 않는다. | `test_FR22_link_request_conflict` |
| 다른 계정이 같은 `request_id`를 보낸다 | 새 내부 요청을 만든다. 다른 계정의 판정을 돌려주지 않는다. | `test_FR22_link_request_other_user` |
| 외부 `request_id`를 비운 요청과 같은 `request_id`가 다시 온다 | 새 내부 요청을 만든다. 비운 행은 그대로 둔다. | `test_FR22_link_request_after_clear` |
| 같은 `internal_request_id`에 이미 다른 판정이 저장돼 있다 | `StoreResult(CONFLICT, CONTENT_CONFLICT)`를 반환한다. 기존 판정을 바꾸지 않는다. | `test_FR22_save_decision_conflict` |
| 같은 `internal_request_id`에 같은 판정을 다시 저장한다 | `StoreResult(ALREADY_APPLIED)`를 반환한다. | `test_FR22_save_decision_already_applied` |
| 두 요청이 같은 대여형 기기를 동시에 예약한다 | 한 요청만 `RESERVED`다. 나머지는 `DEVICE_BUSY`다. | `test_FR19_reserve_concurrent` |
| 같은 `internal_request_id`로 예약을 다시 호출한다 | 기존 `use_id`와 `usage_id`로 `RESERVED`를 반환한다. 행을 새로 만들지 않는다. | `test_FR19_reserve_idempotent` |
| 최초 대여 요청이 `ACTIVE`가 된 뒤 같은 `internal_request_id`로 다시 호출한다 | 재조작 검사보다 기존 예약 조회를 먼저 한다. `REOPERATING`이 아니라 기존 `use_id`로 `RESERVED`를 반환한다. | `test_FR19_reserve_resend_after_active` |
| 본인 `ACTIVE` 세션이 있는 기기에 재조작을 요청한다 | `REOPERATING`과 기존 `use_id`를 반환한다. 새 세션을 만들지 않는다. 승인이 있으면 이번 요청의 사용분만 예약한다. | `test_FR19_reserve_reoperating` |
| 본인 `ACTIVE` 세션이 있지만 기기가 `FAULT`로 격리됐다 | `REOPERATING`이 아니라 `DEVICE_BUSY`를 반환한다. 사용분을 예약하지 않는다. | `test_FR19_reserve_reoperating_fault` |
| 계정이 같은 `exclusive_group`의 다른 기기를 점유 중이다 | `USER_BUSY`를 반환한다. 아무 행도 만들지 않는다. | `test_FR19_reserve_user_busy` |
| 승인 남은 횟수가 0이다 | `APPROVAL_EXHAUSTED`를 반환한다. 세션과 사용분을 만들지 않는다. | `test_FR19_reserve_approval_exhausted` |
| 대상 기기가 `AVAILABLE`이 아니다 (`FAULT` 포함) | `DEVICE_BUSY`를 반환한다. | `test_FR19_reserve_not_available` |
| 등록하지 않았거나 비활성인 비콘의 RSSI가 온다 | 저장하지 않는다. 예외를 던지지 않는다. | `test_FR05_save_rssi_unknown_beacon` |
| 등록하지 않은 기기의 RSSI가 온다 | 저장하지 않는다. 예외를 던지지 않는다. | `test_FR05_save_rssi_unknown_node` |
| 비콘에 진행 중인 수집 회차가 있을 때 RSSI가 온다 | 관측 행에 그 회차 ID를 함께 저장한다. | `test_FR05_save_rssi_collection_run` |
| 관측 시각이 수집 회차의 끝 시각과 같다 | 끝은 미포함이다. 그 회차에 연결하지 않는다. | `test_FR05_save_rssi_collection_run_end_exclusive` |
| 같은 `(node_id, session_id, seq, beacon)`의 RSSI가 다시 온다 | 한 번만 저장한다. 예외를 던지지 않는다. | `test_FR05_save_rssi_duplicate` |
| 저장된 값보다 오래된 시각으로 `touch_node()`를 호출한다 | `last_seen_at`을 바꾸지 않는다. | `test_FR13_touch_node_older` |
| 신뢰한 기기가 다른 기능이나 점유 방식으로 발표한다 | 신뢰 상태를 `discovered`로 되돌린다. 같은 트랜잭션에서 감사 로그를 남긴다. | `test_FR13_announce_changed` |
| 정책이 없는 기기로 `current_policy()`를 호출한다 | `LookupError`를 던진다. | `test_FR14_current_policy_missing` |
| 승인의 동작, 정책 버전, 요청자가 다르거나 만료됐다 | `valid_approval()`이 `None`을 반환한다. | `test_FR14_valid_approval_scope` |
| 같은 요청자·기기·동작·정책 버전에 유효한 `pending` 신청이 있다 | `create_approval_request()`가 새 신청을 만들지 않고 기존 신청을 반환한다. | `test_FR14_create_approval_request_dedup` |
| 만료된 `pending` 신청 뒤 다시 신청한다 | 새 신청을 만든다. | `test_FR14_create_approval_request_after_expiry` |
| `pending`이 아니거나 정책 버전이 다른 승인을 결정한다 | 반영하지 않고 사유를 구분해 반환한다. | `test_FR14_decide_approval_invalid` |
| 대기 만료된 `pending` 승인을 결정한다 | 반영하지 않고 `REQUEST_EXPIRED`를 반환한다. | `test_FR14_decide_approval_request_expired` |
| 같은 승인 ID·결정자·결정을 다시 요청한다 | `ALREADY_APPLIED`로 기존 결과를 반환한다. 기록을 다시 쓰지 않는다. | `test_FR14_decide_approval_idempotent` |
| 같은 승인에 다른 결정자나 반대 결정을 요청한다 | `ALREADY_DECIDED`를 반환하고 기존 기록을 보존한다. | `test_FR14_decide_approval_conflicting` |
| 한 계정에 활성 비콘을 둘 넣는다 | 제약 위반이다. 두 번째 행을 저장하지 않는다. | `test_FR01_one_active_beacon` |
| 없는 `use_id`나 다른 계정의 `use_id`로 소유권을 묻는다 | `False`를 반환한다. 두 경우를 구분하지 않는다. | `test_FR21_owner_matches` |
| 같은 `event_id`로 감사 기록을 다시 쓴다 | 한 번만 저장한다. 예외를 던지지 않는다. | `test_FR22_audit_duplicate_event` |
| 다른 스레드에서 동시에 호출한다 | 스레드마다 연결을 따로 쓴다. 쓰기 경합은 `busy_timeout` 안에서 기다린다. | `test_NFR03_threads` |

## 6. 결정

- 시각은 UTC ISO 8601 문자열로 저장한다 — SQLite에 날짜 타입이 없고, 문자열 비교로 순서를 정할 수 있다.
- 연결은 스레드마다 하나씩 연다. 연결마다 WAL, `foreign_keys=ON`, `busy_timeout`을 설정한다 — FastAPI가 `def` 엔드포인트를 스레드풀에서 실행하기 때문이다.
- 트랜잭션은 직접 연다 (`isolation_level=None`, `BEGIN IMMEDIATE`) — Python `sqlite3`의 자동 트랜잭션은 시작 시점이 불명확하다.
- 스키마 버전은 `PRAGMA user_version`으로 관리한다. DDL은 `db/schema/`의 SQL 파일에 둔다 — 마이그레이션 도구를 추가하지 않는다.
- 내부 ID (`internal_request_id`, `use_id`, `usage_id`)는 DB가 UUID4 문자열로 만든다.
- `nodes()`는 신뢰, 온라인, `FAULT`, 장소로 거르지 않고 모든 기기를 반환한다 — 후보 조건은 판정의 ②가, 장소 범위는 근접 추정이 `place_id`로 정한다.
- `reserve_if_free()`는 같은 `internal_request_id`의 기존 예약 조회 → 본인 `ACTIVE` 세션 확인 → ⑤ 기기 점유 → ⑥ 사용자 점유 → 승인 남은 횟수 순서로 검사한다.
  - 기존 예약 조회를 가장 먼저 한다 — 최초 대여 요청이 실행 성공으로 `ACTIVE`가 된 뒤 같은 `internal_request_id`로 재전송되면, 본인 `ACTIVE` 세션 확인이 이를 재조작으로 착각해 `REOPERATING`을 반환하면 안 된다. 멱등 재호출은 항상 기존 예약을 그대로 반환한다.
  - 본인 `ACTIVE` 세션이 있어도 기기 `device_state`가 `ACTIVE`가 아니면(`FAULT` 등) `REOPERATING`이 아니라 `DEVICE_BUSY`를 반환한다 — `isolate_device()`는 점유 세션을 바꾸지 않고 기기만 `FAULT`로 격리하므로, 세션은 `ACTIVE`로 남아도 기기는 격리된 상태일 수 있다. `interfaces.py`의 "타인 점유 또는 RESERVED·CLOSING·FAULT는 DEVICE_BUSY"는 본인 재조작에도 똑같이 적용한다.
  - `exclusive_group`이 NULL이면 ⑥을 검사하지 않는다.
- `valid_approval()`에 조건을 만족하는 승인이 여럿이면 `valid_until`이 가장 늦은 승인을 반환한다. 같으면 `approval_id` 순이다.
- `link_request()`는 감사 기록을 쓰지 않는다 — 충돌 이벤트는 판정이 `AuditLog.append()`로 남긴다.
- 같은 `internal_request_id`에 다른 판정을 저장하면 기존 판정을 보존하고 `StoreResult(CONFLICT, CONTENT_CONFLICT)`를 반환한다. 같은 판정이면 `StoreResult(ALREADY_APPLIED)`를 반환하고 아무것도 바꾸지 않는다 — `interfaces.py`의 `save_decision` docstring이 이미 값 객체로 구분하라고 정했다. 예외로 던지지 않는다.
- `upsert_announced_node()`의 신뢰 되돌림 감사는 `Step.ANNOUNCE`를 쓴다 — `architecture.md` §4 "기기 발표와 관측의 저장"이 이미 정했다.
- `create_approval_request()`와 `decide_approval()`은 CORE가 넘긴 값을 그대로 조건 검사에 쓴다. 승인 권한 검사는 CORE가 하고 DB는 조건부 갱신만 한다.
- `approval` 테이블에는 `architecture.md`가 명시하지 않은 구현 컬럼으로 `created_at`, `pending_until`을 둔다 — `create_approval_request()`의 멱등 판단과 만료 계산에 필요하다 (`architecture.md` §6 "저장 필드와 스키마는 구현할 때 구체화한다").
- `node_command`는 `CommandRecord`의 모든 필드에 대응하는 컬럼을 지금 만든다 — 후속 작업이 컬럼을 추가하면서 마이그레이션하지 않도록 한다. 행 쓰기 함수는 `db-execution-store`가 만든다.
- 테스트 데이터는 `tests/db/`의 fixture가 SQL로 직접 넣는다 — `AdminRepository`의 DB 구현은 아직 없다. `#30`의 결정은 `Protocol`까지만 반영됐고, 구현은 `db-approval-admin`이 한다.
- 공용형 기기는 `reserve_if_free()`에서 ⑤⑥과 세션 생성을 건너뛴다. 승인 사용분만 예약한다. `approval_id`가 `None`이면 아무것도 쓰지 않고 `RESERVED`를 반환한다 (#27).
- 기간형이고 `max_uses`가 NULL이어도 요청별 사용분 행을 만든다. 횟수 검사만 생략한다 (#27).
- `node`에 `anchor_params` 컬럼을 두지 않는다. `NodeInfo`에서 필드 자체가 빠졌다. 근접 파라미터는 장소별 모델 파일이 보존하고, DB는 판정에 적용한 모델·보정 버전만 기록한다 (#40, PR #54).
- `rssi_observation`은 `collection_id`로 수집 회차를 연결한다. 회차는 `collection_run` 테이블(`collection_id`, `beacon_id`, `label`, `started_at`, `ended_at`)에 둔다. `label`은 관측이 아니라 회차에 있다 (#41, PR #54).
- 계정당 활성 비콘은 하나다. DB는 `(user_id) WHERE is_active=1` 부분 UNIQUE 인덱스로 막는다. `beacons_of()`는 활성 비콘 0개 또는 1개만 반환한다 (#42, PR #54).

### #173에서 승인한 변경 경계

- 요청 목적 필드는 `shared/models.py`와 기존 `Repository.link_request` 문서에 반영한다.
- `db/schema/sql/0006_request_purpose.sql`에서 목적 컬럼만 추가한다. 기존 FK와 외부 키 범위는 유지한다.
- 평가 도구의 기대 스키마 버전은 같은 PR에서 6으로 올린다.
- 공동 계약 문서와 관련 기존 spec을 같은 PR에서 맞춘다.
- 위 목록은 4절의 기존 고치지 않는 곳에서 이 fix에만 허용한 예외다.

### #165 후속: 요청 용량의 원자적 집계

- #174가 DB 한도 집계를 구현했다. 이 fix는 승인 신청의 외부 키 보존과 목적별 집계를 보완한다.
- 입력과 출력은 기존 `UseRequest`, `input_digest`와 `RequestLink`를 유지한다.
- 기존 요청을 먼저 식별한다. 재전송에는 한도와 관계없이 기존 연결과 판정을 반환한다.
- 새 조작 요청의 집계와 연결은 같은 `BEGIN IMMEDIATE` 트랜잭션에서 한다.
- DB는 외부 `request_id`가 있고 목적이 `use` 또는 미분류인 요청 행을 센다.
- 승인 신청의 `purpose="access"`는 한도 검사와 집계에서 제외한다. 외부 키는 유지한다.
- 외부 ID를 비운 요청 행과 감사 행은 집계하지 않는다.
- 한도 이상이면 외부 `request_id`가 NULL인 요청 행을 만든다.
- 반환값은 `RequestLink(is_new=True, capacity_exceeded=True)`다. 새 내부 요청 ID도 반환한다.
- 한도 미만이면 기존 요청 연결을 만들고 플래그는 거짓으로 반환한다.
- 정규화 입력과 판정·감사의 내부 참조를 유지한다. DB가 거부 판정을 대신 만들지 않는다.
- 가득 찼을 때 만료 항목을 먼저 정리한다 — 담당자가 2026-10-16에 선택했다.
- 정리는 기존 `db-retention-capacity`의 만료 ID 보호 기준을 재사용한다.
- `request.purpose`를 nullable 컬럼으로 추가한다. 기존 행은 NULL로 남겨 목적을 추측하지 않는다.
- 기존 미분류 행은 집계에 포함하고 기존 재전송은 그대로 반환한다.
- 새 행의 목적이 다른 같은 외부 키는 입력 충돌이다. 외부 키의 유일성 범위를 나누지 않는다.
- 기존 오류 거부 재판정과 예약의 멱등성을 유지한다.
- 명령 항목의 새 삭제·한도 정책은 넣지 않는다.

## 7. 가정

- `request.request_id`는 NULL을 허용한다. 보장기간 뒤 외부 ID를 비우는 구현은 `db-retention-capacity`가 맡는다 — #15, #164.
- #165 구현은 DB 정리의 최종 완료·보호 관계 기준을 따른다. 기준은 머지된 `db-retention-capacity`를 그대로 재사용한다.
- 담당자가 같은 요청 연결 함수에 목적을 전달하는 안을 승인했다. `UseRequest.purpose`는 `use` 또는 `access`다.
- 마이그레이션 전 행은 목적을 복원할 근거가 없다. NULL을 유지하고 기존 키와 digest의 재전송 보장을 유지한다.
- `recovery` 테이블은 복구 건 ID, 기기와 명령·세션 연결, 근거 참조, 승인 기록, 정산 결과, 정산 내용 요약값을 담는다 — #17, #20

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-01 | `test_FR01_beacon_unique`, `test_FR01_one_active_beacon` | 비콘 식별자 고유성, 계정당 활성 비콘 하나 |
| FR-05 | `test_FR05_save_rssi_*`, `test_FR05_rssi_since` | 등록 비콘만 저장하고, 회차 ID를 붙이고, 중복을 막고, 시각 범위로 조회한다 |
| FR-13 | `test_FR13_*` | 발표와 `touch_node()`의 갱신 규칙, 신뢰 되돌림의 감사 기록 |
| FR-14 | `test_FR14_*` | 현재 정책 리비전 조회, 승인 범위와 남은 횟수, 승인 신청 중복·만료, 결정의 조건부 갱신과 멱등성 |
| FR-15 | `test_FR15_beacons_of_active_only` | 활성 비콘만 반환한다 |
| FR-19 | `test_FR19_reserve_*` | 원자적 예약, 멱등성, 재조작(`REOPERATING`), ⑤⑥과 승인 횟수 검사, 공용형 처리 |
| FR-21 | `test_FR21_owner_matches` | 본인 `use_id`만 `True`다 |
| FR-22 | `test_FR22_*` | 외부 키 연결, 판정 저장, 감사 기록 중복 처리 |
| NFR-03 | `test_NFR03_reserve_stress` | 스레드 여러 개가 같은 기기를 반복 예약해도 이중 점유가 0건이다 |
| NFR-08 | `test_NFR08_account_by_token_hash` | 해시로만 조회하고 원문 토큰 컬럼이 없다 |

- 다른 계층의 구현 없이 SQLite 임시 파일만으로 확인한다.
- NFR-03의 2,000회 측정은 `tools/evaluation/`이 맡는다. 이 작업의 테스트는 원자성만 확인한다.

### #165 후속 검증 계획

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-22 | `test_FR22_capacity_link_counts_only_external_ids` | 집계 대상과 제외 대상이다 |
| FR-22 | `test_FR22_capacity_link_creates_unkeyed_request` | 한도 거부도 내부 요청 행을 가진다 |
| FR-22 | `test_FR22_capacity_cleanup_before_rejection` | 만료 항목을 정리한 뒤 한도를 판정한다 |
| NFR-03 | `test_NFR03_capacity_count_and_link_are_atomic` | 동시 새 요청이 한도 경계를 넘지 않는다 |
| FR-18, FR-22 | `test_FR22_capacity_link_keeps_existing_replay` | 한도에서 재전송과 입력 충돌을 유지한다 |
| FR-19 | `test_FR19_capacity_retry_keeps_single_reservation` | 기존 오류 재판정이 예약을 중복하지 않는다 |
| FR-03, FR-22 | `test_FR03_capacity_link_keeps_access_request_key` | 한도에서도 승인 신청의 키와 재전송을 유지한다 |

- CORE의 플래그 검사와 승인 신청의 실제 DB 통합 검증을 같은 fix 작업에서 마친다.
- 스키마 5에서 목적이 미분류인 기존 요청을 보존하는 마이그레이션을 검증한다.
