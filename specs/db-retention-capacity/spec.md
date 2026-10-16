# 보존기간 정리와 중복 방지 한도 (`RetentionStore`, `link_request` 한도 검사)

- 상태: 완료
- 담당: paiwhy (DB)
- 요구사항: FR-21, FR-22
- 따르는 결정: #164, #165 (#169로 반영됨)

## 1. 왜

- #169로 `shared`에 `RetentionStore` Protocol과 `CleanupReport`, `RequestLink.capacity_exceeded`, `retention_cleanup_interval_sec` 설정이 들어갔다. 구현이 없으면 서버 조립이 `RetentionStore`를 부를 대상이 없고, 중복 방지 항목은 무한히 쌓인다.
- `db-execution-store` spec은 이 작업을 `db-recovery-retention`이라는 이름의 후속 spec으로 미뤄 뒀다("보존기간 정리, 완료 20분 뒤 `request_id` 비우기, 중복 방지 10만 건 한도", #15). 이 spec이 그 후속 작업이다. 브랜치 이름을 따라 spec 경로는 `db-retention-capacity`로 둔다.
- `requirements.md` FR-22는 "명령 중복 방지 항목(`node_command`)의 보존기간과 정리 기준"을 별도로 미결로 남겨 뒀다. 이 spec은 그 미결을 정하지 않는다. `use_session`·`audit_log`·`request.request_id`만 다룬다.

## 2. 무엇

- 입력: 현재 시각(`RetentionStore.cleanup(now)`의 `now`). 설정값(`use_history_retention_days`, `audit_retention_days`, `request_dedup_ttl_sec`, `dedup_entry_limit`). 기존 `link_request(req, input_digest)` 호출.
- 출력: `CleanupReport(cleared_requests, deleted_sessions, deleted_audit_rows)`. `RequestLink(..., capacity_exceeded=bool)`.
- 바뀌는 상태:
  - `request.request_id`를 `NULL`로 비운다(`cleared_requests`). `request` 행 자체는 지우지 않는다.
  - 보존기간이 지난 `use_session` 행을 지운다(`deleted_sessions`). 지우기 전에 그 행을 참조하는 `node_command.use_id`, `recovery.use_id`를 `NULL`로 뗀다.
  - 보존기간이 지난 `audit_log` 행을 지운다(`deleted_audit_rows`).
  - `link_request`가 새 요청을 연결할 때, 외부 `request_id`가 있는 요청 행 수가 한도 이상이면 `request_id` 없이 행을 만든다.
- 약속:
  - "진행 중이거나 미해결"인 기록은 기간이 지나도 지우지 않는다(6절에서 정확한 기준을 정한다).
  - `audit_log` 행은 JSONL로 내보낸 것이 확인된 뒤에만 지운다. 내보내기 경로가 없으면(비활성) 지우지 않는다.
  - 한 번의 `cleanup()` 호출은 `BEGIN IMMEDIATE` 트랜잭션 하나로 묶는다.
  - `link_request`의 외부 요청 수 세기와 삽입은 `BEGIN IMMEDIATE` 트랜잭션 하나로 묶는다. 같은 요청의 재전송에는 한도와 관계없이 기존 결과를 반환한다.
  - `request` 행은 절대 지우지 않는다. `node_command` 행도 지우지 않는다(FR-22 미결, 범위 밖).

## 3. 제약

- `node_command` 행을 지우지 않는다. "명령 중복 방지 항목의 보존기간"은 `requirements.md` FR-22가 이미 미결로 남겼다 — FR-22
- `use_session`·`audit_log`·`request` 삭제·갱신은 진행 중이거나 미해결인 기록을 지우지 않는다 — FR-22, `architecture.md` "이력·감사 보존"
- `request` 행은 지우지 않는다. 중복 방지 보장기간이 지나면 `request_id`만 비운다 — FR-22, `RetentionStore.cleanup` docstring
- `use_session`을 지울 때 그 행을 참조하는 `node_command.use_id`, `recovery.use_id`는 `NULL`로 뗀 뒤에 지운다 — FK(`PRAGMA foreign_keys = ON`)가 참조 중인 행 삭제를 막기 때문이다
- 완료된(`completed_at`이 있는) `recovery` 행만 `use_id`를 뗀다. 미완료 복구가 참조하는 `use_session`은 지우지 않는다 — "미해결" 기록 보호
- `audit_log` 행은 JSONL 내보내기(`#34`)가 그 행까지 반영한 뒤에만 지운다. JSONL 경로가 없으면 `audit_retention_days` 정리를 수행하지 않는다 — NFR-13 "평가 스크립트는 감사 테이블을 읽는다"와 재현성을 위해 유일한 사본을 먼저 잃지 않는다
- `audit_log` 삭제는 `rowid < (SELECT MAX(rowid) FROM audit_log)`를 함께 건다. 가장 큰 `rowid` 행은 보존기간이 지나도 지우지 않는다 — `audit_log.log_id`는 TEXT PK라 `AUTOINCREMENT`가 없다. `AUTOINCREMENT` 없는 테이블에서 가장 큰 `rowid` 행을 지우면 다음 `INSERT`가 그 번호를 그대로 재사용한다(직접 확인: 메모리 DB에서 rowid 1~5 중 5를 지우고 삽입하면 새 행이 rowid 5를 받는다). JSONL 내보내기는 "파일 마지막 줄의 `seq`보다 큰 행만 내보낸다"는 가정으로 동작하므로(`db-audit-jsonl` spec 6절), 재사용된 `rowid`를 가진 새 감사 행은 이미 내보낸 것으로 오인되어 JSONL에서 영구히 빠진다. 가장 큰 `rowid`를 항상 남겨 두면 다음 `INSERT`는 항상 더 큰 새 번호를 받는다
- 한도(`dedup_entry_limit`) 검사는 새 요청을 연결하는 같은 트랜잭션에서 한다. 동시 요청이 한도를 함께 넘지 않는다 — #165, NFR-03
- 같은 `internal_request_id`로 다시 호출하면(기존 요청의 재전송) 한도와 관계없이 기존 결과를 반환한다 — #165
- 시간 값(`use_history_retention_days`, `audit_retention_days`, `request_dedup_ttl_sec`, `dedup_entry_limit`, `retention_cleanup_interval_sec`)을 코드 상수로 쓰지 않는다. `Settings`에서 받는다(이미 #169로 추가됨) — AGENTS.md 코딩 규칙
- `shared`를 바꾸지 않는다. `RetentionStore`, `CleanupReport`, `RequestLink.capacity_exceeded`는 이미 있다 — AGENTS.md 의존 규칙 4

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/db/repositories/repository.py` (`link_request`)
- `server/src/nearpilot/db/repositories/` (새 `RetentionStore` 구현 파일 추가)
- `server/tests/db/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `api/`, `iot/`, `tools/`, `db/schema/`(마이그레이션 없음 — 기존 컬럼·FK로 충분하다), `db/audit/`(읽기 함수 재사용만, 수정 없음)

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `RetentionStore.cleanup` | 제공 | 상태 관리(서버 조립)가 시작할 때와 `retention_cleanup_interval_sec`마다 부른다 (#164) |
| `Repository.link_request`의 `RequestLink.capacity_exceeded` | 제공(기존 계약의 필드 채움) | 판정(CORE) — `capacity_exceeded=True`를 `DENY`·`CAPACITY_EXCEEDED`로 바꾸는 처리는 판정 담당의 몫이다 (#169 PR 설명) |

### 만들지 않는 것
| 후속 작업 | 내용 | 근거 |
|---|---|---|
| — | `node_command`(명령 중복 방지 항목)의 보존기간과 정리 기준 | FR-22 미결. 실제 기기의 명령 기록 저장 방식을 검증한 뒤 정한다 |
| 상태 관리(서버 조립) | 서버 시작 때와 주기마다 `RetentionStore.cleanup`을 실제로 호출하는 배선 | 서버 조립(#88) |
| 판정(CORE) | `RequestLink.capacity_exceeded=True`를 `DENY`·`CAPACITY_EXCEEDED` 판정으로 바꾸는 `core/decision/pipeline.py` 처리 | #169 PR 설명, 판정 담당(eunji719) |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `use_session`이 `CLOSED`이고 보존기간이 지났지만 완료되지 않은(`completed_at IS NULL`) `recovery`가 참조한다 | 지우지 않는다 | `test_FR22_cleanup_skips_session_with_unresolved_recovery` |
| `use_session`이 `RESERVED`/`ACTIVE`/`CLOSING`이다(아직 진행 중) | 보존기간이 지나도 지우지 않는다 | `test_FR22_cleanup_skips_in_progress_session` |
| `use_session`이 `CLOSED`이고 보존기간이 지났고 미해결 복구가 없다 | 지운다. 참조하던 `node_command.use_id`·완료된 `recovery.use_id`는 `NULL`로 바뀐 채 남는다 | `test_FR21_cleanup_deletes_eligible_session_and_detaches_commands` |
| 요청에 연결된 `use_session`이 아직 `CLOSED`가 아니다(또는 `node_command`가 `pending`/`in_progress`다, 또는 미해결 복구가 있다) | 완료로 보지 않는다. `request_id`를 비우지 않고 `audit_log` 행도 지우지 않는다 | `test_FR22_cleanup_skips_unfinished_request` |
| 요청이 완료됐고 완료 시점부터 `request_dedup_ttl_sec`가 지났다 | `request_id`를 `NULL`로 비운다. `internal_request_id`와 연결 기록은 그대로다 | `test_FR22_cleanup_clears_request_id_after_dedup_ttl` |
| 완료되지 않았거나 `request_dedup_ttl_sec`가 안 지난 요청 | `request_id`를 비우지 않는다 | `test_FR22_cleanup_keeps_request_id_within_ttl` |
| `audit_log` 행이 보존기간을 넘었지만 JSONL 내보내기가 아직 그 행까지 못 미쳤다 | 지우지 않는다 | `test_FR22_cleanup_skips_unexported_audit_rows` |
| JSONL 경로가 설정되지 않았다(내보내기 비활성) | `audit_log` 보존기간 정리를 하지 않는다 | `test_FR22_cleanup_skips_audit_retention_without_jsonl` |
| `audit_log` 행이 보존기간을 넘었고 JSONL에 이미 반영됐다 | 지운다 | `test_FR22_cleanup_deletes_exported_audit_rows_past_retention` |
| `audit_log`의 가장 큰 `rowid` 행도 보존기간을 넘었고 JSONL에 반영됐다 | 그 행만 남긴다. 그 뒤 새 감사 행을 쓰고 JSONL을 내보내면 빠짐없이 이어 붙는다 | `test_FR22_cleanup_keeps_max_rowid_so_jsonl_resumes` |
| `cleanup()`을 같은 `now`로 두 번 연속 돌린다 | 두 번째 `CleanupReport`는 `cleared_requests`·`deleted_sessions`·`deleted_audit_rows`가 모두 0이다. DB 상태는 첫 번째 호출 뒤와 같다 | `test_FR22_cleanup_is_idempotent` |
| `cleanup()`을 돌린다 | `rssi_observation`·`collection_run` 행 수는 그대로다 | `test_FR22_cleanup_keeps_collection_observations` |
| `cleanup()` 중 쓰기 잠금을 얻지 못한다 | 아무것도 지우지 않은 채 `StoreNotApplied`를 올린다. 판정과 실행을 막지 않는다(상태 관리가 다음 주기에 재시도) | `test_NFR03_cleanup_lock_failure_raises_store_not_applied` |
| 외부 `request_id`가 있는 요청 행 수가 `dedup_entry_limit` 미만이다 | 평소처럼 `request_id`를 넣어 새 행을 만든다. `capacity_exceeded=False` | `test_FR18_link_request_below_limit_is_unaffected` |
| 행 수가 한도 이상이다(새 요청) | `request_id` 없이 행을 만들고 `RequestLink(is_new=True, capacity_exceeded=True)`를 반환한다. 이 행은 다음 집계에서 빠진다 | `test_FR22_link_request_capacity_exceeded_omits_request_id` |
| 한도 이상인 상태에서 같은 외부 요청을 재전송한다(기존 요청의 재조회) | 한도와 관계없이 기존 결과를 반환한다(`is_new=False`) | `test_FR18_link_request_retransmission_ignores_limit` |
| `request_id`를 비운 요청에 같은 `(user_id, request_id)`가 다시 온다 | 비운 행과의 중복 방지 연결이 끝났으므로 새 `internal_request_id`로 연결한다(`is_new=True`) | `test_FR22_cleared_request_id_links_as_new_request` |
| 같은 요청자·기기·동작의 재조작이 본인 `ACTIVE` 세션에 연결된다(기존 `REOPERATING` 경로) | 한도 검사와 무관하게 기존 동작을 유지한다 | `test_FR19_link_request_reoperating_path_unaffected` |
| 동시에 두 요청이 한도 근처에서 들어온다 | 둘 다 같은 트랜잭션 직렬화로 처리되어 한도를 함께 넘지 않는다 | `test_NFR03_link_request_concurrent_capacity_check` |

- 권한 없는 호출: 해당 없음. `RetentionStore`와 `link_request`는 서버 내부 호출이다.
- 시간 초과: 두지 않는다. `cleanup()`은 한 트랜잭션으로 동기 실행한다.

## 6. 결정

### 요청의 "완료" 조건과 완료 시점

- `request`에는 완료 시각 컬럼이 없다. 아래 조건을 모두 만족하면 "완료"로 본다.
  1. `verdict IS NOT NULL`
  2. 이 요청에 연결된 `use_session`이 있다면 `state = 'CLOSED'`다(대여형이 아니면 조건을 건너뜀, `use_session`이 없는 요청은 즉시 조건 2를 만족한 것으로 본다).
  3. 이 요청에 연결된 `node_command` 중 `status`가 `pending` 또는 `in_progress`인 행이 없다.
  4. `recovery.internal_request_ids`(JSON 배열)에 이 요청 ID가 들어있는 복구 건 중 `completed_at IS NULL`인 행이 없다.
- 완료 시점은 그 요청의 `audit_log` 행 중 `created_at`이 가장 늦은 값으로 근사한다. 관련 `recovery.completed_at`이 더 늦으면 그 값을 쓴다 — 담당자 확인(수락). 매 단계(`Step`)가 감사 행을 남기므로 마지막 감사 행이 그 요청이 실제로 "끝난" 시점과 가장 가깝다. 새 컬럼을 추가하면 `shared`나 스키마 마이그레이션이 필요해 범위를 넘는다. 근사가 틀리는 방향은 항상 "완료 시점을 실제보다 늦게 잡는" 쪽뿐이다(마지막 감사 행은 완료 시점 이전이나 그 순간에 쓰이지, 그 뒤에 쓰이지 않는다) — 중복 방지 보장기간이 실제보다 조금 더 길게 유지되는 안전한 쪽으로만 오차가 생긴다.

### `use_session` 삭제와 참조 분리

- 지우는 조건: `state = 'CLOSED'` AND `closed_at + use_history_retention_days <= now` AND 미완료 복구(`recovery.use_id = use_id AND completed_at IS NULL`)가 없다.
- 지우기 직전에 `UPDATE node_command SET use_id = NULL WHERE use_id = ?`와 `UPDATE recovery SET use_id = NULL WHERE use_id = ? AND completed_at IS NOT NULL`을 실행해 FK 참조를 뗀다. `node_command`·완료된 `recovery` 행 자체는 남는다 — 이유: `node_command`는 이 spec에서 지우지 않기로 했으므로(FR-22 미결), `use_session` 보존기간(7일)이 `node_command`의 어떤 보존 기준보다 먼저 끝나도 FK 위반 없이 두 수명을 독립적으로 둘 수 있다. 두 컬럼 모두 `NOT NULL`이 아니라서(`CREATE TABLE` 확인) 스키마 변경이 필요 없다.
- `use_session`을 참조하는 FK를 전체 스키마(`0001_initial.sql`~`0005_request_kind.sql`)에서 다시 확인했다. `node_command.use_id`와 `recovery.use_id` 둘뿐이다. `approval_usage`는 `command_id`가 평범한 `TEXT` 컬럼이라 `node_command`나 `use_session`을 FK로 참조하지 않는다. `request`도 `use_session`을 참조하지 않는다(반대로 `use_session.internal_request_id`가 `request`를 참조한다). 뗄 FK는 두 곳뿐이다.

### `audit_log` 삭제와 JSONL 확인

- `created_at + audit_retention_days <= now`이고, 그 행의 `internal_request_id`가 `NULL`이거나(인증 실패 등 요청 없는 사건) 위 "완료" 조건을 만족하는 요청에 속하고, `rowid < (SELECT MAX(rowid) FROM audit_log)`이고(3절 "rowid 재사용" 참조), `rowid`가 JSONL에 이미 내보낸 최대 `seq` 이하이면 지운다.
- JSONL의 "내보낸 최대 seq"는 `db/audit/jsonl.py`의 기존 방식(파일 마지막 줄의 `seq`를 매번 다시 읽음)을 그대로 재사용한다. 새 상태를 추가하지 않는다 (`db-audit-jsonl` spec 6절의 "새 상태를 늘리지 않는다"를 따른다).
- `RetentionStore` 구현 생성자는 `jsonl_path: str | None`을 받는다. `None`이면 `audit_log`의 보존기간 정리를 하지 않는다 — 담당자 확인(수락). JSONL이 유일한 사본이므로, 사본이 없으면 원본을 지우지 않는 쪽이 NFR-13의 재현성을 지킨다. 서버 조립(상태 관리, #88)이 `RetentionStore`를 만들 때 `db/audit/`가 이미 쓰고 있는 JSONL 경로를 그대로 생성자에 넘기므로, 실제 운영 배선에서는 `jsonl_path`가 항상 채워져 있고 `audit_log` 정리도 정상적으로 돈다. `jsonl_path`가 없는 경로는 테스트와, JSONL을 아직 연결하지 않은 조립 상태를 위한 안전장치다.

### `link_request` 재구성

- 지금의 "낙관적 단일 `INSERT`, 충돌 시 `IntegrityError`를 잡아 `_link_existing`으로 재조회" 구조를 "`BEGIN IMMEDIATE`로 먼저 기존 행을 조회 → 없으면 한도를 세고 조건부로 `INSERT`" 구조로 바꾼다.
  1. `(user_id, request_id)`로 기존 행을 조회한다. 있으면 기존 `_link_existing`의 digest 비교·재판정 로직을 그대로 쓴다(변경 없음).
  2. 없으면 `SELECT COUNT(*) FROM request WHERE request_id IS NOT NULL`을 센다.
  3. 한도 미만이면 원래 `request_id`로 `INSERT`하고 `capacity_exceeded=False`를 반환한다.
  4. 한도 이상이면 `request_id = NULL`로 `INSERT`하고 `capacity_exceeded=True`를 반환한다.
- 외부에 보이는 반환값과 재조작(`REOPERATING`) 경로는 바꾸지 않는다. 내부에서만 "조회 먼저, 그다음 조건부 삽입"으로 바뀐다 — 이유: 한도 세기와 삽입이 같은 트랜잭션 안에 있어야 동시 요청이 한도를 함께 넘지 않는다(#165, NFR-03). 기존처럼 낙관적 `INSERT` 뒤 `IntegrityError`로 분기하면, 그 단일 문장은 자동 커밋이라 한도 세기를 같은 트랜잭션에 넣을 수 없다. 단순한 쪽(항상 `BEGIN IMMEDIATE`)으로 가기로 담당자가 확인했다.
- 개수 세기가 기존 인덱스를 쓰는지 실제 스키마로 확인했다. `EXPLAIN QUERY PLAN SELECT COUNT(*) FROM request WHERE request_id IS NOT NULL`의 결과는 `SCAN request USING COVERING INDEX request_external_key`다. `request_external_key`는 `(user_id, request_id) WHERE request_id IS NOT NULL` 부분 UNIQUE 인덱스로, 이미 `db-repository` spec이 중복 방지용으로 만들어 뒀다. 새 인덱스 없이 이 커버링 인덱스만으로 테이블 본문을 읽지 않고 개수를 센다 — 요청마다 트랜잭션이 하나 늘어도 개수 세기 자체의 비용은 작다.

## 7. 가정

- `node_command.status`의 `pending`/`in_progress`만 "진행 중"으로 본다. `rejected`·`not_sent`는 이미 종결된 상태로 본다 — 가정: `ExecutionStatus` 열거값의 의미가 바뀌면(팀 결정) 이 spec의 완료 조건도 같이 바꾼다.
- `recovery.internal_request_ids`는 JSON 배열이라 SQL로 바로 인덱스 검색할 수 없다. 이 spec은 완료 판정 시 해당 요청에 연결된 `use_session.use_id`로 먼저 `recovery.use_id`를 찾고, `use_session`이 없는 요청(공용형)은 해당 기기의 미완료 `recovery` 중 `internal_request_ids`에 포함되는지 Python에서 JSON을 파싱해 확인한다 — 가정: 공용형 기기의 복구 빈도가 적어 성능에 문제가 없다. 많아지면 후속 spec에서 인덱스나 보조 테이블을 검토한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-21 | `test_FR21_cleanup_deletes_eligible_session_and_detaches_commands` | 보존기간이 지난 종료 이력을 지우고 참조를 뗀다 |
| FR-21 | 5절 표의 세션 관련 `test_FR21_*`, `test_FR22_*` | 진행 중·미해결 보호 |
| FR-22 | `test_FR22_cleanup_clears_request_id_after_dedup_ttl` | 완료 뒤 중복 방지 보장기간이 지나면 `request_id`만 비운다 |
| FR-22 | `test_FR22_cleanup_keeps_request_id_within_ttl` | 보장기간 안에는 비우지 않는다 |
| FR-22 | `test_FR22_cleanup_deletes_exported_audit_rows_past_retention` | JSONL에 반영된 보존기간 지난 감사 행을 지운다 |
| FR-22 | `test_FR22_cleanup_skips_unexported_audit_rows`, `test_FR22_cleanup_skips_audit_retention_without_jsonl` | 내보내지 않은 행은 지우지 않는다 |
| FR-22 | `test_FR22_cleanup_keeps_max_rowid_so_jsonl_resumes` | 가장 큰 `rowid` 행을 남겨서 새 감사 행이 JSONL에 빠짐없이 이어 붙는다 |
| FR-22 | `test_FR22_cleanup_is_idempotent` | 같은 `now`로 두 번 돌리면 두 번째는 0건이고 DB 상태가 같다 |
| FR-22 | `test_FR22_cleanup_keeps_collection_observations` | `rssi_observation`·`collection_run`은 건드리지 않는다 |
| FR-22 | `test_FR22_cleared_request_id_links_as_new_request` | `request_id`를 비운 뒤 같은 외부 키가 다시 오면 새 내부 요청으로 연결한다 |
| FR-22 | `test_FR22_link_request_capacity_exceeded_omits_request_id` | 한도 이상이면 `request_id` 없이 만들고 플래그를 반환한다 |
| FR-18 | `test_FR18_link_request_below_limit_is_unaffected`, `test_FR18_link_request_retransmission_ignores_limit` | 한도 미만·재전송 동작이 그대로다 |
| FR-19 | `test_FR19_link_request_reoperating_path_unaffected` | 재조작 경로가 한도 검사로 바뀌지 않는다 |
| NFR-03 | `test_NFR03_link_request_concurrent_capacity_check`, `test_NFR03_cleanup_lock_failure_raises_store_not_applied` | 동시성과 잠금 실패 처리 |

- 테스트는 임시 SQLite 파일에 `nearpilot.db.schema.migrate`로 스키마를 만들고, 고정된 행(요청·세션·명령·복구)을 직접 `INSERT`해 경계 상황을 구성한다.
- `cleanup()`의 `now`는 테스트가 주입한 값을 쓴다. 실제 시간을 기다리지 않는다.
