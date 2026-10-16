# 보존기간이 지난 기록의 정리

- 상태: 구현 전 초안. DB 담당자의 세부 선택 뒤 구현과 같은 feat PR에서 반영한다.
- 담당: DB 담당 (구현 배정 미결). 초안 작성은 JM-220이다.
- 요구사항: FR-21, FR-22, NFR-03, NFR-13
- 따르는 결정: #164. 공동 계약은 main의 `RetentionStore`를 따른다.
- 담당자 선택: 2026-10-16 대화에서 진행 중 기록 보호와 DB·JSONL 보존 기준 일치를 선택했다.

## 1. 왜

- 보존기간 설정과 공동 접점은 있지만 DB 정리 구현은 없다.
- 정리가 없으면 외부 요청 ID의 보장기간이 지나도 같은 ID를 새 요청으로 사용할 수 없다.
- 종료 이력과 감사 기록도 보존기간에 따라 정리되지 않는다.

## 2. 무엇

- 입력: 서버의 현재 시각, 적용한 `Settings`, SQLite 연결과 감사 JSONL 경로다.
- 출력: `RetentionStore.cleanup(now)`의 `CleanupReport`다.
- 바뀌는 상태:
  - 최종 완료 뒤 보장기간이 지난 요청의 외부 `request_id`를 비운다.
  - 보존기간이 지난 종료 이력과 상세 감사 행을 지운다.
  - JSONL 사본에도 같은 보존 기준을 적용한다.
- 약속:
  - 요청 행과 내부 요청 ID는 지우지 않는다.
  - 진행 중인 사용·명령·반납·복구에 연결된 기록은 보호한다.
  - 완전히 끝난 기록만 정리한다.
  - 수집 회차에 연결된 관측은 이 정리로 지우지 않는다.
  - 보고서는 `cleared_requests`, `deleted_sessions`, `deleted_audit_rows`를 반환한다.
  - 여러 번 호출해도 이미 끝낸 정리를 중복 집계하지 않는다.

## 3. 제약

- DB 쓰기는 기존 저장 경로와 `BEGIN IMMEDIATE`를 쓴다 (FR-22, NFR-03).
- 외부 요청 ID를 비워도 내부 참조와 판정 재현 근거를 보존한다 (FR-22, NFR-13).
- 진행 중·미해결 기록은 기간이나 용량만으로 지우지 않는다 (FR-22).
- 본인 종료 이력의 조회 기간은 기존 설정을 따른다 (FR-21).
- 감사 테이블이 원본이다. JSONL 실패로 DB 저장을 실패했다고 반환하지 않는다 (FR-22).
- DB 커밋과 파일 갱신을 하나의 원자적 트랜잭션이라고 설명하지 않는다 (FR-22).
- JSONL 정리가 실패하면 경고를 남긴다. 이후 재시도에서 사본을 보존 기준에 맞춘다 (FR-22).
- 관계 변경이나 새 공동 계약이 필요하면 팀 결정 전 구현을 멈춘다 (`AGENTS.md` 불확실성 처리).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/db/repositories/`의 정리 구현이다.
- `server/src/nearpilot/db/audit/`의 보존기간 연결이다.
- `server/tests/db/`
- JSONL 동작을 바꾸면 기존 `specs/db-audit-jsonl/spec.md`도 같은 PR에서 맞춘다.

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 이 작업: CORE, API, IOT, 도구, DB 엔터티 관계다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `RetentionStore.cleanup` | 제공 | 서버 조립 |
| `CleanupReport` | 제공 | 서버 조립 |
| 보존기간을 담은 `Settings` | 사용 | shared |
| 감사 JSONL 사본 | 기존 DB 계약 유지 | 평가 도구 |

### 만들지 않는 것

- 시작 시 호출과 주기 호출은 `core-server-assembly`가 맡는다.
- 한도 집계와 용량 거부는 기존 DB·판정 spec의 fix가 맡는다.
- 명령 중복 방지 항목의 삭제 정책은 아직 팀 미결이다.
- 운영 관측의 새 삭제 주기는 만들지 않는다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 종료 이력의 보존기간이 지났다 | 보호 조건을 확인한 이력만 지운다 | `test_FR21_cleanup_expired_history` |
| 최종 완료 요청의 중복 방지 보장기간이 지났다 | 요청 행은 유지하고 외부 ID만 비운다 | `test_FR22_cleanup_clears_only_external_id` |
| 오래된 요청에 진행 중 명령·반납·복구가 연결돼 있다 | 외부 ID와 필요한 이력·감사 기록을 유지한다 | `test_FR22_cleanup_protects_unresolved_graph` |
| 수집 회차에 연결된 관측이 오래됐다 | 관측을 유지한다 | `test_FR22_cleanup_keeps_collection_observations` |
| 정리와 새 요청·결과 저장이 동시에 온다 | 트랜잭션 안의 현재 상태로 보호 여부를 검사한다 | `test_NFR03_cleanup_races_request_and_settlement` |
| DB 정리가 실패했다 | 기존 저장 오류 계약을 따른다. 성공 보고서를 만들지 않는다 | `test_FR22_cleanup_store_failure` |
| DB 커밋 뒤 JSONL 갱신이 실패했다 | DB 결과를 유지한다. 경고를 남기고 사본 갱신을 재시도한다 | `test_FR22_cleanup_jsonl_failure_recovers` |
| 감사 행을 모두 지운 뒤 새 행을 쓴다 | JSONL 커서 때문에 새 행을 건너뛰지 않는다 | `test_FR22_cleanup_jsonl_cursor_after_empty_table` |
| 정리 뒤 재시작하거나 JSONL을 다시 만든다 | 남은 감사 행이 순서대로 한 번씩 들어간다 | `test_FR22_cleanup_jsonl_restart_and_rebuild` |

## 6. 결정

- 정리는 DB가 맡고 조립은 호출만 한다 — #164가 책임과 접점을 정했다.
- 주기는 `Settings.retention_cleanup_interval_sec`를 쓴다 — 기본값은 60초다.
- 진행 중 기록은 모두 보호한다 — 담당자가 2026-10-16에 선택했다.
- JSONL과 DB의 보존 기준을 맞춘다 — 담당자가 2026-10-16에 선택했다.
- 정리 결과는 기존 세 필드를 쓴다 — 새 공동 결과 모델을 만들지 않는다.

## 7. 가정

- DB의 세부 구현은 아직 확정하지 않았다. 아래 선택 전에는 삭제 코드를 구현하지 않는다.
  - 최종 완료 시점을 기존 저장 기록에서 구하는 기준이다.
  - 진행 중·미해결 상태와 보호할 참조 관계의 정확한 목록이다.
  - 참조가 남은 종료 이력의 보존과 삭제 순서다.
  - JSONL의 `rowid` 커서, 전체 삭제와 실패 뒤 재시도를 함께 보장하는 방식이다.
- DB 담당자는 선택지를 확인한 뒤 6절에 내부 결정을 기록한다.
- 기존 JSONL spec의 최대 `rowid` 보존 가정은 정리 알고리즘의 증명이 아니다.
- 관계나 공동 계약을 바꿔야 하면 별도 팀 결정이 선행한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-21 | `test_FR21_cleanup_expired_history` | 종료 이력의 보존기간이다 |
| FR-22 | `test_FR22_cleanup_*` | 외부 ID 비우기, 보호 관계와 JSONL 복구다 |
| NFR-03 | `test_NFR03_cleanup_races_request_and_settlement` | 동시 저장에서 진행 중 기록을 지우지 않는다 |
| NFR-13 | `test_FR22_cleanup_jsonl_restart_and_rebuild` | 남은 감사 기록의 재구성이다 |

- 테스트 이름은 구현 계획이다. 초안에는 삭제 코드와 새 테스트를 넣지 않는다.
- DB 담당자의 내부 결정과 보호 관계 검증 뒤에만 초안을 구현 완료로 바꾼다.