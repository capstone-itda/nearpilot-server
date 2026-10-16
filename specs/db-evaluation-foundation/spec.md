# 평가 기반 (`tools/evaluation`) — 읽기 전용 DB 연결과 결과 형식

- 상태: 작성 중
- 담당: paiwhy (DB)
- 요구사항: FR-22, NFR-13
- 따르는 결정: 없음

## 1. 왜

- NFR-13은 "평가 표와 그래프는 스크립트로 만든다. 평가 스크립트는 감사 테이블을 읽는다"고 요구한다. 지금 `server/tools/evaluation/`은 `.gitkeep`만 있는 빈 디렉터리다. 읽기 전용 연결과 보고서 형식이 없으면 어떤 지표 스크립트도 안전하게 결과를 낼 수 없다.
- 평가 스크립트가 운영 DB 파일을 실수로 바꾸면(쓰기, 마이그레이션) 판정에 영향을 줄 수 있다. 연결 하나부터 "절대 쓰지 않는다"를 보장해야 그 위에 지표별 스크립트를 안전하게 쌓을 수 있다.
- 지표마다 표·그래프·신뢰구간을 각자 다르게 만들면 보고서 형식이 지표마다 달라진다. 결과 형식을 한 번만 만들어 모든 지표 스크립트가 재사용한다.
- 지표 1·2·4·8·3·5·6·9처럼 이 기반 위에서 돌아갈 후속 스크립트들이, 각자 DB를 여는 방법과 보고서 형식을 새로 정하지 않아도 되게 한다.

## 2. 무엇

- 입력:
  - SQLite DB 파일 경로
  - 이 도구가 기대하는 스키마 버전(코드 상수, 6절 결정)
  - 보고서로 낼 표 데이터: 지표별 행(표본 수·실패 수 등 포함), 생성 시각
- 출력:
  - 읽기 전용 `sqlite3.Connection`을 돌려주는 함수
  - 보고서 CSV 파일(표 원본)
  - 보고서 Markdown 파일(발표용). 같은 표 데이터로 CSV와 Markdown을 함께 만든다
- 바뀌는 상태: 없음. DB는 읽기만 한다. 보고서 파일만 만든다.
- 약속:
  - DB 연결은 `sqlite3.connect("file:<경로>?mode=ro", uri=True)`로 연다. `nearpilot.db.schema.connect`는 쓰지 않는다(3절).
  - 연결을 열면 즉시 `PRAGMA user_version`을 확인한다. 기대하는 버전과 다르면 연결을 닫고 오류를 낸다.
  - 보고서는 CSV와 Markdown 두 가지로 낸다. 머리말에 입력 DB 경로, 스키마 버전, git 커밋, 표본 수, 생성 시각을 적는다.
  - 표본 중 실패가 0건인 지표는 그 지표 행에 Clopper-Pearson 95% 신뢰구간 상한을 같이 적는다.
  - PNG 그래프는 만들지 않는다(4절 "만들지 않는 것").

## 3. 제약

- `nearpilot.db.schema.connect`를 쓰지 않는다. 그 함수는 `PRAGMA user_version`을 보고 마이그레이션 DDL(`executescript`)을 실행할 수 있는 쓰기 연결이다. 평가 스크립트가 운영 DB 파일을 열 때 의도치 않게 스키마를 바꾸면 안 된다(요청자 지시, NFR-13).
- DB 연결은 `sqlite3.connect("file:<경로>?mode=ro", uri=True)`로 연다. SQLite가 커널 수준에서 쓰기를 거부하므로, 코드에 버그가 있어도 DB 파일이 바뀌지 않는다(요청자 지시).
- 연결을 연 직후 `PRAGMA user_version`을 확인한다. 이 도구가 기대하는 버전과 다르면 오류를 내고 연결을 닫는다. 쿼리를 먼저 보내지 않는다 — 다른 스키마를 기대하며 쿼리를 날리면 조용히 틀린 결과나 알아보기 어려운 `sqlite3.OperationalError`(없는 컬럼 등)를 낼 수 있다.
- `Repository`·`AccountLookup` 같은 `shared` 인터페이스를 거치지 않는다. SQL을 직접 쓴다 (NFR-13 "평가 스크립트는 감사 테이블을 읽는다").
- DB에 쓰지 않는다. `INSERT`/`UPDATE`/`DELETE`/DDL 문장을 실행하지 않는다(읽기 전용 연결이 강제하지만, 코드에서도 시도하지 않는다).
- 보고서 머리말에는 입력 DB 경로, 스키마 버전, git 커밋, 표본 수, 생성 시각을 적는다. 이 다섯 값을 빠짐없이 적는다(requirements.md §4 "입력, 시나리오, 버전, 표본 수를 보존한다").
- 표본 중 실패가 0건이면 95% 신뢰구간 상한을 함께 적는다(requirements.md §4).
- `shared`를 바꾸지 않는다(AGENTS.md 의존 규칙 4).
- `server/pyproject.toml`을 바꾸지 않는다. 그래프 의존성(matplotlib)은 이슈 초안 5가 결정되고 별도 후속 feat PR로 추가된 뒤에만 쓸 수 있다(4절 "만들지 않는 것").

## 4. 경계

### 고치는 곳
- `server/tools/evaluation/`
- `server/tests/tools/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/`, `server/tools/data_collection/`, `server/tools/node_sim/`, `server/src/nearpilot/db/schema/`(연결·마이그레이션 함수를 쓰지 않는다. 고치지도 않는다)

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| 없음 | — | 이 작업은 SQLite 파일을 읽기 전용으로 직접 연다. `shared` Protocol이나 다른 영역의 계약을 쓰거나 제공하지 않는다 |

### 만들지 않는 것
| 후속 작업 | 내용 | 근거 |
|---|---|---|
| `specs/db-evaluation-split` (가칭) | 수집 회차 조회(구간 변경 없음), 중단 회차 제외 기준, 회차 60/20/20 분할(고정 시드, 라벨별 계층화, JSON 파일 저장) | 범위를 "읽기 전용 DB 연결 + 결과 형식"으로 좁혔다(요청자 지시). `architecture.md` §4 RSSI 수집 회차 미결(#41, #131, #138)을 이 후속 spec이 정한다 |
| `specs/db-evaluation-dataset` (가칭) | 공개용 데이터셋 추출(계정 ID 제거, `beacon_id` 익명화, `audit_log.payload_json` 사용자 ID 재귀 삭제) | 위와 같음(요청자 지시) |
| `specs/db-evaluation-server-metrics` | 지표 3(부하 생성기·측정), 5, 6(node_sim), 9(완전 탐색 재사용), 지표 7의 서버 판정 지연 | 이 spec, 이슈 초안 2·3 결정 뒤 |
| `specs/db-evaluation-proximity-scaffold` | 지표 1, 2, 4, 8의 틀과 합성 데이터 테스트 | 이 spec, `db-evaluation-split`, 이슈 초안 1 결정 뒤 |
| `specs/db-evaluation-report` (선택) | 9개 지표를 묶는 발표용 리포트 | 위 spec들 완료 뒤 |
| — | PNG 그래프 생성과 `matplotlib` 의존성 추가 | `server/pyproject.toml`을 바꿔야 해서 이번 범위 밖(요청자 지시). 이슈 초안 5 결정 뒤 후속 feat PR이 추가한다 |
| — | 지표 2·8의 비교 기준 구현(이슈 초안 1), 지표 3의 부하 생성기(이슈 초안 2) | 각 결정 뒤 해당 후속 spec |
| — | 실패가 1건 이상인 지표 행의 신뢰구간 계산 | 요청자 지시. `1 - (0.025)^(1/n)`은 실패 0건에서만 성립하는 닫힌 식이다. 1건 이상은 `scipy.stats.beta`의 역함수가 필요해 이 spec이 피하기로 한 의존성을 다시 끌어온다 |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| DB 파일이 없거나 열 수 없다 | 보고서를 만들지 않는다. 오류를 쓰고 종료 코드 1로 끝낸다 | `test_NFR13_missing_db_file_is_rejected` |
| `PRAGMA user_version`이 기대하는 버전과 다르다 | 연결을 닫는다. 쿼리를 보내지 않는다. 오류를 쓰고 종료 코드 1로 끝낸다 | `test_NFR13_schema_version_mismatch_is_rejected` |
| 읽기 전용 연결에 쓰기를 시도한다(방어 확인) | SQLite가 `sqlite3.OperationalError`로 거부한다. DB 파일은 바뀌지 않는다 | `test_NFR13_readonly_connection_rejects_writes` |
| `db/schema/`의 마이그레이션 파일 수가 `EXPECTED_SCHEMA_VERSION`과 어긋난다 | `nearpilot.db.schema.migrate`로 만든 DB의 `PRAGMA user_version`과 상수가 달라 테스트가 실패한다(코드 버그가 아니라 둘 다 고쳐야 한다는 신호) | `test_NFR13_expected_schema_version_matches_migrations` |
| 지표 행의 표본 중 실패가 0건이다 | 그 행에 `1 - (0.025)^(1/n)` 식으로 Clopper-Pearson 95% 신뢰구간 상한을 적는다 | `test_NFR13_zero_failure_reports_confidence_bound` |
| 표본 30건 중 실패가 0건이다 | 상한이 약 0.1157이다 | `test_NFR13_zero_failure_confidence_bound_matches_known_value` |
| 지표 행의 실패가 1건 이상이다 | 신뢰구간을 계산하지 않는다(범위 밖, 4절) | `test_NFR13_nonzero_failure_omits_confidence_bound` |
| git 작업 디렉터리에 커밋되지 않은 변경이 있다(`git status --porcelain`이 비어 있지 않다) | 머리말의 git 커밋 값 뒤에 `-dirty`를 붙인다 | `test_NFR13_report_header_marks_dirty_working_tree` |
| git 커밋을 확인할 수 없다(`.git` 없음, `git` 명령 없음 등) | 보고서 생성을 막지 않는다. 머리말의 git 커밋 칸에 "unknown"을 적고 경고를 남긴다 | `test_NFR13_report_header_falls_back_when_git_unavailable` |
| 보고서 출력 경로에 이미 파일이 있다 | `--force`를 주지 않으면 덮어쓰지 않고 오류로 끝낸다 | `test_NFR13_existing_output_requires_force` |
| 출력 디렉터리에 쓰지 못한다(권한) | DB 조회 전에 먼저 확인한다. 쓰지 못하면 아무 파일도 만들지 않고 종료 코드 1로 끝낸다 | `test_NFR13_unwritable_output_dir_is_rejected_before_any_write` |

- 권한 없는 호출: 해당 없음. 이 도구는 DB 파일이 있는 서버 PC에서 실행하고 인증하지 않는다(`tools/data_collection`과 같다).
- 동시 요청: 해당 없음. 평가 스크립트는 읽기 전용 연결 하나로 한 번 실행한다. 운영 중인 서버의 쓰기와 동시에 실행해도 읽기 전용이라 충돌하지 않는다.
- 시간 초과: 두지 않는다. 쿼리와 보고서 작성은 동기로 끝까지 실행한다.

## 6. 결정

### DB 연결

- `sqlite3.connect("file:<경로>?mode=ro", uri=True)`로 연다 — 요청자 지시. `nearpilot.db.schema.connect`는 마이그레이션(DDL 실행)이 가능한 쓰기 연결이라 쓰지 않는다. `mode=ro` URI 연결은 SQLite 자체가 쓰기 문장을 거부해서, 코드 버그로 인한 실수까지 막는다.
- 연결 직후 `PRAGMA user_version`을 읽어 이 도구가 기대하는 버전과 비교한다. 다르면 즉시 연결을 닫고 오류를 낸다 — 요청자 지시.
- 기대하는 버전은 평가 도구 코드의 상수로 둔다(`EXPECTED_SCHEMA_VERSION = 5`) — 요청자 확인. `db/schema/`의 마이그레이션 파일 수를 셀 수도 있지만, 그러면 `db/schema/`의 비공개 내부(`_migration_files()`)에 의존하게 되고 "`db/schema/`를 쓰지 않는다"는 이번 결정과 어긋난다. 상수로 두면 `db/schema/`에 새 마이그레이션이 추가될 때마다 평가 도구 쪽 상수도 같이 올려야 한다는 유지보수 부담이 남는다.
  - 이 어긋남을 자동으로 잡아내는 테스트를 둔다: `nearpilot.db.schema.migrate`로 임시 DB를 만들고, 그 DB의 `PRAGMA user_version`이 `EXPECTED_SCHEMA_VERSION`과 같은지 확인한다(8절 `test_NFR13_expected_schema_version_matches_migrations`). 이 테스트 코드는 `db/schema`를 써도 된다 — 요청자 지시. "이 작업은 `db/schema`를 쓰지 않는다"는 평가 도구 코드 자체의 제약이고, 테스트가 그 상수의 정확성을 검증하는 것은 별개다. `db/schema/`에 마이그레이션이 추가되고 이 상수를 같이 올리지 않으면 이 테스트가 실패해서 바로 드러난다.

### 결과 형식

- 보고서는 CSV(표 원본)와 Markdown(발표용)을 같은 표 데이터로 함께 만든다. PNG 그래프는 만들지 않는다 — 요청자 지시(4절).
- 보고서 머리말에 입력 DB 경로, 스키마 버전, git 커밋, 표본 수, 생성 시각을 적는다 — 요청자 지시.
- git 커밋은 실행 시점에 `git rev-parse HEAD`를 서브프로세스로 호출해 얻는다 — 요청자 확인. 실패하면(`.git`이 없거나 `git`을 찾을 수 없음) 예외를 올리지 않고 "unknown"을 적은 뒤 경고 로그를 남긴다.
  - `git status --porcelain` 결과가 비어 있지 않으면(커밋되지 않은 변경이 있다) 커밋 값 뒤에 `-dirty`를 붙인다 — 요청자 지시. 예: `a1b2c3d-dirty`. 두 서브프로세스 호출(`rev-parse`, `status`) 중 하나라도 실패하면 git 커밋 칸 전체를 "unknown"으로 쓴다(부분적으로 `-dirty`만 못 붙이는 애매한 상태를 만들지 않는다).
- 지표 행은 `trials`(표본 수)·`failures`(실패 수) 필드를 선택적으로 가진다. `failures == 0`인 행에는 같은 행에 Clopper-Pearson 95% 신뢰구간 상한 칸을 덧붙인다. 두 필드가 없는 행(예: 수치 집계가 아닌 행)은 신뢰구간을 적지 않는다 — 요청자 확인.
  - 실패 0건일 때 양측 95% Clopper-Pearson 신뢰구간의 상한은 `1 - (0.025)^(1/n)`이다(`n`은 `trials`) — 요청자 지시. `scipy.stats.beta`를 쓰지 않고 이 식으로 직접 계산한다. 실패가 0건일 때는 하한이 항상 0이라 양측 구간이 상한 하나로 정리되는 닫힌 식이다.
  - 실패가 1건 이상인 행의 신뢰구간 계산은 이번 범위 밖이다(4절 "만들지 않는 것") — 요청자 지시. 그 경우는 `1 - (0.025)^(1/n)` 같은 닫힌 식이 없고 `scipy.stats.beta`의 역함수가 필요한데, 이는 이 spec이 피하기로 한 바로 그 의존성이다.

## 7. 가정

- 이 도구가 기대하는 스키마 버전 상수는 `db-repository` spec이 만든 마이그레이션 파일 수(현재 5)와 같다고 가정한다 — 가정: `db/schema/`에 새 마이그레이션이 머지되면 이 상수를 올리는 작업이 빠질 수 있다. `test_NFR13_expected_schema_version_matches_migrations`(6절·8절)가 그 어긋남을 잡아내므로, 빠져도 조용히 틀린 결과를 내지는 않는다.
- matplotlib 추가 여부(이슈 초안 5)는 아직 담당자에게 제출되지 않았다 — 가정: 제출·결정 전까지 이 spec의 보고서는 CSV·Markdown만 만든다. 결정이 A든 B든 이 spec의 경계 안에서는 바뀌지 않는다. 결정이 나면 그래프 추가는 후속 feat PR로 한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-13 | `test_NFR13_opens_connection_readonly` | `mode=ro` URI로 연다. `PRAGMA user_version`이 기대 버전과 같으면 연결을 그대로 돌려준다 |
| NFR-13 | `test_NFR13_readonly_connection_rejects_writes` | 반환한 연결로 `INSERT`를 시도하면 `sqlite3.OperationalError`가 난다 |
| NFR-13 | `test_NFR13_schema_version_mismatch_is_rejected` | 기대 버전과 다르면 오류를 내고 연결을 닫는다 |
| NFR-13 | `test_NFR13_expected_schema_version_matches_migrations` | `db/schema/`로 만든 DB의 `PRAGMA user_version`이 `EXPECTED_SCHEMA_VERSION`과 같다 |
| NFR-13 | `test_NFR13_report_header_has_required_fields` | 보고서 머리말에 DB 경로·스키마 버전·git 커밋·표본 수·생성 시각이 모두 있다 |
| NFR-13 | `test_NFR13_report_written_as_csv_and_markdown` | 같은 표 데이터로 CSV와 Markdown 파일을 함께 만든다 |
| NFR-13 | `test_NFR13_zero_failure_reports_confidence_bound` | 실패 0건인 행에 `1 - (0.025)^(1/n)` 식으로 계산한 상한이 있다 |
| NFR-13 | `test_NFR13_zero_failure_confidence_bound_matches_known_value` | `n=30`, 실패 0건이면 상한이 약 0.1157이다 |
| NFR-13 | `test_NFR13_nonzero_failure_omits_confidence_bound` | 실패가 있으면 신뢰구간을 계산하지 않는다 |
| NFR-13 | `test_NFR13_report_header_marks_dirty_working_tree` | 커밋되지 않은 변경이 있으면 git 커밋 값 뒤에 `-dirty`가 붙는다 |
| NFR-13 | `test_NFR13_report_header_falls_back_when_git_unavailable` | git 커밋을 못 구하면 "unknown"을 적고 보고서 생성은 계속한다 |
| NFR-13 | 5절 표의 나머지 `test_NFR13_*` | 예외 상황 |

- 테스트는 임시 SQLite 파일에 직접 `CREATE TABLE`과 `PRAGMA user_version = <기대 버전>`을 실행해 스키마를 흉내 낸다. 이 평가 도구 코드 자체는 `nearpilot.db.schema.migrate`를 쓰지 않는다(이 도구가 그 함수에 의존하지 않음을 테스트도 보장한다). 다만 `test_NFR13_expected_schema_version_matches_migrations`는 상수의 정확성을 검증하는 목적으로 `db/schema`를 직접 쓴다(6절 결정).
- git 커밋을 흉내 내는 테스트는 서브프로세스 호출 함수를 주입해 실제 git을 실행하지 않는다.
