# 감사 로그 JSONL 내보내기 (`AuditLog` 사본)

- 상태: 작성 중
- 담당: paiwhy (DB)
- 요구사항: FR-22, NFR-13
- 따르는 결정: #34

## 1. 왜

- `AuditLog` 계약은 "테이블이 원본이고 JSONL은 커밋 뒤 사본"이라고 약속한다 (`interfaces.py`, `architecture.md` §4 "감사 로그의 원본과 JSONL"). 지금 `db/audit`는 테이블만 쓴다. JSONL 사본이 없다.
- 사본이 없으면 평가와 발표에서 감사 기록을 파일로 공개할 수 없다 (NFR-13).

## 2. 무엇

- 입력: 커밋된 `audit_log` 행. 내보낼 JSONL 파일 경로(DB 계층 생성자 인자).
- 출력: JSONL 파일. 한 줄이 감사 행 하나다.
- 바뀌는 상태: JSONL 파일에 줄을 덧붙인다. DB는 바꾸지 않는다.
- 약속:
  - 커밋한 감사 행만, 커밋 순서대로 내보낸다. 커밋 전 행은 쓰지 않는다.
  - JSONL 기록이 실패해도 저장 오류를 내지 않는다. 판정과 실행을 막지 않는다. 다음 내보내기에서 빠진 행을 이어서 쓴다.
  - `audit_log` 테이블에서 JSONL 전체를 다시 만들 수 있다.

## 3. 제약

- 감사 테이블이 원본이다. JSONL은 사본이다 (#34).
- 커밋 순서는 `audit_log`의 `rowid` 순서와 같다 — 모든 쓰기가 `BEGIN IMMEDIATE`나 단일 문장 자동 커밋으로 직렬화되기 때문이다.
- JSONL 실패는 예외를 올리지 않는다. 경고 로그만 남긴다 (`architecture.md` §4).
- 같은 행을 두 번 쓰지 않는다. 행을 건너뛰지 않는다.
- 여러 스레드가 같은 파일에 동시에 쓰지 않는다. 경로마다 잠금 하나를 둔다.
- 줄 하나는 완전한 JSON이다. 중간에 끊긴 마지막 줄은 다음 내보내기 전에 잘라낸다.
- 토큰 원문과 `token_hash`는 감사 payload에 원래 없다. 내보내기는 payload를 바꾸지 않는다 (NFR-08).
- `shared`를 바꾸지 않는다. 파일 경로는 DB 계층 생성자 인자로 받는다.
- `SqliteAuditLog.append()`의 `INSERT`는 단일 문장 자동 커밋이라 `immediate_transaction`을 거치지 않는다. `append()` 끝에서 직접 내보내기를 호출한다 (6절 결정).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/db/audit/`
- `server/src/nearpilot/db/repositories/_common.py` (커밋 뒤 내보내기 연결)
- `server/tests/db/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `api/`, `iot/`, `tools/`, `db/schema/` (마이그레이션 없음)

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `AuditLog.append` | 제공 (기존과 같음) | CORE |
| JSONL 파일 | 제공 | 평가 도구, 발표 자료 |

### 만들지 않는 것
| 후속 | 내용 | 근거 |
|---|---|---|
| — | 보존기간이 지난 감사 행의 정리와 JSONL에서의 처리 | #164 결정 뒤 |
| — | 서버 시작 때 JSONL 경로를 정하고 내보내기를 연결하는 조립 | 서버 조립 (#88) |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 감사 행이 커밋된다 | 같은 내용의 줄이 JSONL 끝에 붙는다 | `test_FR22_jsonl_appends_committed_row` |
| 트랜잭션이 롤백된다 | 그 트랜잭션의 감사 행은 JSONL에 없다 | `test_FR22_jsonl_skips_rolled_back` |
| 여러 쓰기가 이어서 커밋된다 | 줄 순서가 `rowid` 순서와 같다 | `test_FR22_jsonl_commit_order` |
| 같은 `event_id`를 두 번 `append`한다 | 줄은 하나다 | `test_FR22_jsonl_no_duplicate_event` |
| JSONL 파일을 쓸 수 없다 (권한, 디스크) | 저장은 성공한다. 예외가 없다. 경고 로그가 남는다 | `test_FR22_jsonl_failure_does_not_block_store` |
| 실패 뒤 다시 쓸 수 있게 됐다 | 다음 내보내기에서 빠진 행부터 이어 쓴다 | `test_FR22_jsonl_resumes_after_failure` |
| 마지막 줄이 중간에 끊겼다 | 끊긴 줄을 잘라내고 그 행부터 다시 쓴다 | `test_FR22_jsonl_truncated_last_line_recovers` |
| 프로세스를 다시 시작했다 | 파일 마지막 줄에서 이어 쓴다. 중복이 없다 | `test_FR22_jsonl_resume_after_restart` |
| 여러 스레드가 동시에 커밋한다 | 줄이 섞이거나 빠지지 않는다 | `test_NFR03_jsonl_concurrent_writers` |
| 테이블에서 JSONL을 다시 만든다 | 테이블 전체가 `rowid` 순서로 들어간다. 기존 파일은 원자적으로 바뀐다 | `test_FR22_jsonl_rebuild_from_table` |
| 경로를 주지 않았다 | 내보내기를 하지 않는다. 기존 동작과 같다 | `test_FR22_jsonl_disabled_without_path` |
| `immediate_transaction`을 거치지 않는 `SqliteAuditLog.append()`로 커밋한다 | `append()`가 직접 내보내기를 호출해 줄이 붙는다 | `test_FR22_jsonl_append_without_transaction_still_exports` |

## 6. 결정

- 이어 쓸 위치는 JSONL 줄의 `seq`(=`rowid`)다 — 담당자 확인. 각 줄에 `seq`를 넣고, 내보내기 때마다(매번, 캐시하지 않고) 파일의 마지막 줄을 읽어 그 `seq` 다음부터 이어 쓴다. 마지막 줄이 완전한 JSON이 아니면(끊김) 그 줄을 잘라내고 그 앞의 `seq`를 쓴다. 옆 파일이나 DB 테이블에 커서를 두면 새 상태(파일 하나 또는 컬럼 하나)가 생기고 JSONL 파일과의 동기화가 또 다른 원자성 문제가 된다. 매번 파일에서 다시 읽으면 상태를 늘리지 않고, 서로 다른 연결(스레드)의 내보내기 객체가 메모리 커서 없이도 같은 파일을 안전하게 공유할 수 있다.
- 커밋 뒤 내보내기는 `immediate_transaction`의 `COMMIT` 성공 직후에 연결한다 — 담당자 확인. `_common.py`에 연결(`sqlite3.Connection`)마다 내보내기 객체를 약한 참조로 등록해 두고, `COMMIT` 성공 뒤 등록된 내보내기가 있으면 부른다. 저장소 함수 20여 개 모두가 `immediate_transaction`을 쓰므로(`AGENTS.md` 의존 규칙 5) 여기 한 곳만 고치면 전부 덮인다. 새 쓰기 함수가 생겨도 자동으로 덮인다.
- `SqliteAuditLog.append()`의 `INSERT`는 단일 문장 자동 커밋이라 `immediate_transaction`을 거치지 않는다. `append()` 끝에서 가진 내보내기 객체를 직접 불러 같은 보장을 준다.
- 경로마다 잠금은 파일 경로 문자열을 키로 하는 모듈 전역 잠금 테이블에 둔다 — 같은 경로를 가리키는 여러 연결의 내보내기 객체가 잠금을 공유해야 "여러 스레드가 같은 파일에 동시에 쓰지 않는다"를 지킨다. 연결 하나에 잠금을 두면 다른 연결이 같은 파일에 동시에 써서 줄이 섞인다.

## 7. 가정

- 보존기간 정리는 오래된 행부터 지우므로 가장 큰 `rowid`가 재사용되지 않는다 — #164

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-22 | `test_FR22_jsonl_*` | 커밋한 행만, 커밋 순서, 중복·누락 없음, 실패 격리, 재구성 |
| NFR-03 | `test_NFR03_jsonl_concurrent_writers` | 동시 커밋에서 줄이 섞이지 않는다 |
