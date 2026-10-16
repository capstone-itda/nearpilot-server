# 운영 CLI (`nearpilot`)

- 담당: dnnals (근접 추정)
- 요구사항: FR-01, FR-05, FR-12A, FR-13, FR-14, FR-16, FR-22, NFR-06, NFR-13
- 따르는 결정: #29, #30, #40, #41, #88, #131, #138
- 예외: 담당자가 2026-10-17 대화에서 이 작업에 한해 AGENTS.md의 PR 분리 규칙을 무시하기로 했다. 머지된 spec(서버 조립, 수집 도구)의 동작 변경, `shared` 계약 추가, `docs/` 수정을 이 feat PR 하나에 넣는다.

## 1. 왜

- 수집, 시드, 분할, 모델 적합, 평가를 사람이 실행할 진입점이 없다. 계산 함수(`fit_model`, `run_session`, `AdminRepository`)는 있지만 Python 코드를 직접 짜야 부를 수 있다.
- 서버는 모델 파일이 하나도 없으면 시작하지 않는다. 수집하려면 서버가 RSSI를 저장해야 하므로 첫 모델을 만들 방법이 없다.
- 시드 스크립트는 `AdminService`를 거쳐야 하는데 `AdminService`가 없다. 기기의 `release_policy`를 쓰는 관리 접점도 없다. 그래서 시연 환경을 테스트 헬퍼의 SQL로만 만들 수 있다.
- 회차 단위 분할과 지표 8의 평가가 없어서 모델이 기준을 넘는지 확인할 수 없다.

## 2. 무엇

- 입력: 서버 설정 파일, 사람이 쓴 시드·수집 계획·배치·적합 설정 파일, DB의 수집 회차와 관측.
- 출력: `nearpilot` 명령과 하위 명령, 분할 결과 파일, 모델 파일, 평가 보고서(CSV, Markdown).
- 바뀌는 상태: 관리 쓰기(계정, 비콘, 신뢰, 장소, 정책, 종료 정책)와 수집 회차. 모두 기존 `AdminRepository` 경로와 감사로 저장한다.
- 약속:
  - `pip install -e .` 뒤 `nearpilot <하위 명령>`으로 실행한다. 하위 명령은 `serve`, `seed`, `collect run`, `collect list`, `collect close`, `split`, `fit`, `eval`이다.
  - `nearpilot serve --collect-only`는 모델 없이 시작하고 RSSI와 기기 보고를 평소처럼 저장한다. MCP 엔드포인트를 열지 않아서 기기 실행 요청을 받지 않는다.
  - 수집 → 분할 → 적합 → 평가 → 서버 재시작으로 실측 모델이 판정에 쓰인다.

## 3. 제약

- 시드와 수집 도구는 SQL을 직접 실행하지 않는다. 시드는 `AdminService`를 거쳐 `AdminRepository`로 쓴다 (architecture.md §4 "관리 기능의 DB 쓰기", #30).
- `collect list`, 분할, 적합, 평가는 DB를 읽기 전용 연결(`tools/evaluation/db.open_readonly`)로만 연다 (NFR-13).
- 같은 회차의 프레임은 서로 다른 분할에 섞지 않는다 (FR-16, requirements.md §4).
- 모델 파일은 같은 버전을 덮어쓰지 않는다 (architecture.md §4 "근접 모델 파일과 버전", #40).
- 토큰은 `secrets.token_urlsafe(32)`로 만들고 `shared/tokens.hash_token`의 해시만 저장한다. 원문은 발급할 때 한 번만 화면에 쓴다 (FR-01, #29).
- `collect-only`에서도 CORE의 허가 없이 MQTT 실행 명령을 보내지 않는다. MCP가 없으므로 판정 요청 자체가 없다 (FR-06).
- 시간 값과 기준 수치는 코드 상수로 쓰지 않는다. 설정 파일에서 받는다 (AGENTS.md).
- 시연 노트북이 Windows다. 경로 구분자로 `/`와 `\`를 모두 인식한다 (architecture.md §9).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/cli/` (새 폴더): 명령 분기, 파일 인자 해석, 하위 명령
- `server/src/nearpilot/core/admin/` (새 폴더): `AdminService` 구현
- `server/src/nearpilot/core/proximity/`: 배치 파일·적합 설정 읽기
- `server/src/nearpilot/shared/interfaces.py`: `AdminService` Protocol, `AdminRepository.set_release_policy`
- `server/src/nearpilot/shared/models.py`, `enums.py`: `AccountIssue`, `PolicySettings`, `PolicyWriteResult`, `AdminWriteReason.NOT_PLACE_OWNER`
- `server/src/nearpilot/db/repositories/admin_repository.py`: `set_release_policy`
- `server/src/nearpilot/runtime/`: `collect_only` 시작 경로
- `server/tools/evaluation/`: 회차 조회, 분할, 지표 8 일부 계산
- `server/pyproject.toml`: `[project.scripts]`, 패키지에 `tools` 포함
- `server/config/`: 예시 파일(`seed/`, `plans/`, `layouts/`, `fit/`), README, `server.example.toml`의 실행 안내 줄
- `docs/architecture.md`, `AGENTS.md`: 진입점, AdminService 담당, 디렉터리 표, 실행 명령
- `specs/core-server-assembly/spec.md`, `specs/core-proximity-data-collection/spec.md`: 이 작업이 바꾼 진입점·수집 전용·기록 경로 문장
- 테스트: `server/tests/cli/`, `server/tests/core/admin/`, `server/tests/core/proximity/`, `server/tests/db/`, `server/tests/shared/`(Protocol에 메서드를 더해 기존 fake 한 줄), `server/tests/tools/`, `server/tests/integration/`

### 고치지 않는 곳

- 항상: `server/src/nearpilot/shared/`의 기존 계약 시그니처, `.github/`
- 이 작업: 판정 파이프라인(`core/decision/`), 근접 계산(`filter.py`, `fit.py`의 적합 식), MQTT 메시지, 기존 MCP 도구, DB 스키마(마이그레이션 추가 없음)

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `AdminRepository` (기존 메서드 + `set_release_policy`) | 사용, `set_release_policy` 제공 | DB |
| `Repository.nodes`, `current_policy`, `is_place_owner` | 사용 (AdminService 읽기) | DB |
| `AdminService` | 제공 | 관리 기능, 시드 |
| `run_session`, `CollectionPlan` | 사용 | 근접 추정 (`tools/data_collection`) |
| `fit_model`, `write_model_file`, `compute_posteriors`, `load_model` | 사용 | 근접 추정 |
| `open_readonly`, `write_report` | 사용 | DB (`tools/evaluation`) |
| `load_config`, `Runtime`, `create_app` | 사용, `collect_only` 추가 | 상태 관리 (서버 조립) |

### 만들지 않는 것

| 후속 작업 | 내용 | 근거 |
|---|---|---|
| — | 지표 8의 이동 뒤 대상 전환 시간, 정지 시 깜빡임 감소 | 이동 회차 수집과 비교 기준이 미결 (requirements.md §4) |
| — | 지표 1~7, 9의 평가 | `tools/evaluation` 후속 spec |
| — | 관리 REST, GUI | 관리 쓰기 REST는 두지 않는다 (FR-12A). GUI는 CLI 사용 뒤 정한다 |
| — | 공개용 데이터셋 추출 | `db-evaluation-dataset` (가칭) |
| — | 점유 중인 기기의 장소·정책 변경 제한 | architecture.md 미결 |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 파일 인자에 `/`나 `\`가 없는데 기본 폴더에 파일이 없다 | 찾은 경로와 기본 폴더의 파일 목록을 쓰고 종료 코드 2 | `test_FR16_cli_missing_default_file_lists_candidates` |
| 파일 인자가 경로다 | 실행 폴더 기준으로 읽는다. 기본 폴더를 보지 않는다 | `test_FR16_cli_path_argument_uses_cwd` |
| `split PLACE`나 `fit --version`에 구분자, `.`, `..`가 있다 | 설정·파일·DB를 읽기 전에 종료 코드 2 | `test_FR16_split_cli_place_must_be_a_name`, `test_FR16_fit_version_must_be_a_file_name`, `test_FR16_split_cli_checks_place_before_anything_else`, `test_FR16_fit_checks_version_before_reading_anything` |
| `collect list --fit`이 가리키는 파일이 없거나, `--summary --place` 없이 `--fit`을 줬다 | 회차를 읽기 전에 종료 코드 2 | `test_FR16_collect_list_checks_fit_option_first` |
| 설정 파일이 없거나 잘못됐다 | 사유를 쓰고 종료 코드 2. DB 파일을 만들지 않는다 | `test_FR16_cli_invalid_config_creates_nothing` |
| `serve --collect-only` | 모델을 읽지 않고 시작한다. MQTT 보고와 RSSI를 저장한다. `/mcp`가 없다 | `test_FR05_collect_only_starts_without_models`, `test_FR06_collect_only_has_no_mcp` |
| `serve`(일반)에서 모델 표가 비었다 | 기존대로 시작하지 않는다 | `test_FR16_serve_requires_models_without_collect_only` |
| `serve`가 시작에 실패한다 (MQTT 접속, 포트 등) | 종료 코드 1 | `test_FR16_serve_startup_failure_exits_1` |
| 시드 파일의 형식이 틀리다 (모르는 키, 빠진 키, 잘못된 enum) | 아무것도 쓰지 않고 잘못된 필드를 모두 쓴다. 종료 코드 2 | `test_FR12A_seed_invalid_file_writes_nothing` |
| 시드를 두 번 실행한다 | 계정은 "이미 있음", 같은 정책은 "변경 없음"이다. 비콘, 장소, 신뢰, 종료 정책은 같은 값으로 다시 반영한다(DB 내용은 같고 다시 반영한 항목만 감사 행이 는다). 새 토큰을 만들지 않는다 | `test_FR12A_seed_is_rerunnable` |
| 시드의 기기가 DB에 없다 (아직 발표 안 됨) | 그 기기 항목을 거부 사유와 함께 쓰고 종료 코드 1. 앞서 반영한 항목은 유지한다 | `test_FR13_seed_unknown_node_reports_and_continues` |
| 시드 `--dry-run` | DB에 쓰지 않고 반영할 항목만 쓴다 | `test_FR12A_seed_dry_run_writes_nothing` |
| 다른 계정에 등록된 비콘 | `BEACON_ALREADY_REGISTERED`를 쓴다. 기존 활성 비콘을 유지한다 | `test_FR01_seed_duplicate_beacon_rejected` |
| AdminService에 장소 소유자가 아닌 행위자가 기기 신뢰·정책·종료 정책을 쓴다 | 쓰지 않고 거부한다 | `test_FR12A_admin_rejects_non_owner` |
| 정책이 바뀌었다 | 현재 버전을 기대값으로 새 리비전을 추가한다. 내용이 같으면 쓰지 않는다 | `test_FR14_admin_policy_new_revision_only_when_changed` |
| `set_release_policy` | `release_policy`만 바꾸고 감사와 한 트랜잭션. 없는 기기는 `NODE_NOT_FOUND` | `test_FR22_set_release_policy_audited`, `test_FR13_set_release_policy_unknown_node` |
| 토큰 발급 | 원문은 한 번만 출력하고 DB에는 해시만 있다 | `test_FR01_seed_token_printed_once_hash_stored` |
| `collect run`, `collect close`, `collect list`에서 DB 파일이 없다 | DB와 폴더를 만들지 않고 종료 코드 2 | `test_FR16_collect_requires_existing_db`, `test_FR16_collect_list_without_database_is_input_error` |
| `collect run`에 `--record`가 없다 | `var/records/<계획 파일 이름>-<UTC 날짜>.jsonl`에 덧붙인다 | `test_FR16_collect_default_record_path` |
| `collect close` | 진행 중 회차의 끝 시각을 기록한다 | `test_FR16_collect_close_ends_open_run` |
| `collect list --open` | 끝 시각이 없는 회차만 쓴다 | `test_FR16_collect_list_open_only` |
| `collect list --summary` | 라벨별 닫힌 회차 수, 프레임 수, 분할 가능 여부(최소 회차 수 이상)를 쓴다 | `test_FR16_collect_list_summary` |
| 분할: 진행 중이거나 프레임이 없거나 장소를 정할 수 없는 회차 | 분할에서 뺀다. 뺀 회차 ID와 사유를 쓴다 | `test_NFR13_split_excludes_open_and_empty_runs` |
| 분할: 같은 시드 | 같은 결과 파일 내용 | `test_NFR13_split_deterministic_by_seed` |
| 분할: 라벨의 회차가 최소 회차 수보다 적다 | 분할하지 않고 부족한 라벨을 쓴다. 종료 코드 1 | `test_NFR13_split_rejects_too_few_runs` |
| 분할: 결과 파일이 이미 있다 | 덮어쓰지 않는다. 종료 코드 1 | `test_NFR13_split_does_not_overwrite` |
| 분할: 회차 하나가 두 분할에 들어간다 | 생길 수 없다. 모든 회차는 정확히 한 분할에 있다 | `test_FR16_split_run_in_exactly_one_part` |
| `fit`: 분할 결과의 장소와 배치의 장소가 다르다 | 쓰지 않고 종료 코드 2 | `test_FR16_fit_place_mismatch_rejected` |
| `fit`: 같은 버전 모델 파일이 있다 | 쓰지 않고 종료 코드 1 | `test_FR16_fit_existing_version_not_overwritten` |
| `fit`: `FitError` | 사유를 쓰고 종료 코드 1. 파일을 만들지 않는다 | `test_FR16_fit_error_writes_nothing` |
| `fit`: 배치의 기기 중 학습 회차에 라벨이 없는 기기가 있다 | 적합하지 않고 그 기기를 쓴다. 종료 코드 1 | `test_FR16_fit_missing_label_fails` |
| `fit --dry-run` | 앵커 값과 격자 손실을 쓰고 파일은 만들지 않는다 | `test_FR16_fit_dry_run_writes_nothing` |
| `eval`: 시험셋만 쓴다 | 학습·검증 회차의 프레임을 읽지 않는다 | `test_NFR06_eval_uses_test_runs_only` |
| `eval`: 오선택률과 ECE | 합성 데이터에서 손으로 계산한 값과 같다 | `test_NFR06_eval_misselection_and_ece` |
| `eval`: 측정하지 않는 항목 | 보고서에 "미측정"으로 쓴다 | `test_NFR06_eval_reports_unmeasured_items` |
| `eval`: 보고서가 이미 있다 | `--force` 없이는 쓰지 않는다 | `test_NFR13_eval_report_requires_force` |
| DB 경로의 폴더 이름에 공백, `#`, `%`, `?`가 있다 | 읽기 전용 연결이 같은 파일을 연다 | `test_FR16_cli_read_db_path_with_special_characters` |
| `config/`의 예시 파일 | 각 형식의 읽기 함수로 읽힌다 | `test_FR16_config_examples_load` |
| 수집 → 분할 → 적합 → 평가 전체 | 시뮬레이션한 수집 데이터로 모델 파일과 보고서가 생기고, 그 모델로 서버가 시작한다 | `test_FR16_ops_pipeline_end_to_end` |

## 6. 결정

### 명령과 파일

- 명령 이름은 `nearpilot` 하나다. `pyproject.toml`의 `[project.scripts]`로 설치한다 — 짧고 패키지 이름과 같다. `near`는 NEAR Protocol의 CLI와 겹친다.
- `--config` 기본값은 실행 폴더 기준 `config/server.toml`이다. 모든 하위 명령이 같은 설정에서 DB 경로, 모델 폴더, `Settings`를 읽는다.
- 파일 인자 규칙: 인자에 `/`나 `\`가 없으면 확장자를 포함한 파일명이다. 명령별 기본 폴더에서 찾는다. 기본 폴더는 설정 파일이 있는 폴더 기준이다. 구분자가 있으면 실행 폴더 기준 경로다 — 어디서 실행해도 같은 기본 파일을 찾고, 직접 준 경로는 셸 자동완성과 맞는다.
- 기본 폴더: 시드 `config/seed/`, 수집 계획 `config/plans/`, 배치 `config/layouts/`, 적합 설정 `config/fit/`, 분할 결과 `var/splits/`, 수집 기록 `var/records/`, 보고서 `var/reports/`. `config/`는 사람이 쓰고 `var/`는 명령이 만든다. `var/`는 설정 파일 폴더의 부모 기준이다(`server/var/`).
- 형식: 배치·수집 계획·분할 결과는 JSON, 시드·적합 설정은 TOML이다 — 기계가 만드는 구조는 JSON, 사람이 주석을 달며 고치는 파일은 `server.toml`과 같은 TOML. 수집 계획은 기존 JSON을 유지한다.
- `tools/`를 패키지에 포함한다 (`packages = ["src/nearpilot", "tools"]`) — 기존 `tools.*` import를 바꾸지 않는다.
- 종료 코드: 0 성공, 1 실행 중 거부·실패, 2 입력 오류, 130 Ctrl+C. 기존 수집 도구와 같다. 결과·모델·보고서·기록 파일을 쓰다 난 `OSError`(폴더를 만들 수 없음 등)는 2다.
- `--config`는 하위 명령 앞뒤 어디에 써도 된다.
- `load_config`에 `require_models`를 둔다. `serve`(일반)만 `[proximity.models]`가 비지 않아야 한다. 다른 명령과 `--collect-only`는 모델 표가 없거나 비어도 된다 — 첫 모델 전에도 수집·시드·분할을 할 수 있다.
- pytest `pythonpath`에 `"."`를 더해 테스트가 `tools`를 import한다.
- 읽기 전용 DB는 `cli/common.open_read_db`로 연다. DB 파일이 없거나 스키마 버전이 다르면 종료 코드 2다. 경로를 퍼센트 인코딩하고 Windows 드라이브 경로는 `/C:/…`로 바꿔 `open_readonly`의 URI에 넘긴다 — `tools/evaluation/db.py`는 고치지 않는다.
- 파일 오류 문구는 `{종류} 파일 오류: 필드 경로: 사유`다 (종료 코드 2).

### serve

- `nearpilot serve`는 기존 `python -m nearpilot`과 같은 서버를 띄운다. 다른 점은 아래 세 가지다: `--config` 기본값, `var/` 폴더 만들기, 시작 실패의 종료 코드 1. `python -m nearpilot`은 그대로 둔다.
- `--collect-only`는 설정의 `[proximity.models]`를 읽지 않고 빈 모델로 근접 추정기를 만든다. FastAPI 앱에 MCP 앱을 마운트하지 않는다. 나머지 시작 순서(DB, 복원, MQTT, 정리, 주기 작업)는 같다 — 판정 코드를 건드리지 않고, 요청이 도달하지 않으므로 실행이 일어날 수 없다.
- `--collect-only`는 명령 옵션으로만 켠다. 설정 파일에 두지 않는다 — 운영 서버가 실수로 수집 모드로 뜨지 않게 한다.
- `--collect-only`가 아니면 `[proximity.models]`가 비었을 때 기존처럼 시작하지 않는다.
- `serve`는 DB와 감사 JSONL의 부모 폴더를 만든다 — 새로 받은 `server/`에서 첫 명령이 `serve`여도 시작한다.
- uvicorn이 시작 실패로 `sys.exit(3)`을 부르면 종료 코드 1로 바꾼다. Ctrl+C는 uvicorn이 정상 종료로 처리해 0이다.

### AdminService와 seed

- `AdminService` Protocol을 `shared/interfaces.py`에 두고 구현은 `core/admin/service.py`에 둔다. 읽기는 `Repository`, 쓰기는 `AdminRepository`로 한다.
- 계정 등록(`register_account`)은 토큰을 만들어 해시로 저장하고 원문을 돌려준다. 계정과 비콘 등록은 행위자 검사가 없다 — 첫 소유자를 만드는 부트스트랩이다 (FR-01 "서버가 한 번 등록").
- 행위자 검사에 걸리면 `AdminWriteReason.NOT_PLACE_OWNER`(새 값, `REJECTED`)로 거부한다.
- 장소 배치는 행위자가 그 장소의 소유자여야 한다. 기기 신뢰, 정책, 종료 정책은 행위자가 기기 장소의 소유자여야 한다 (`is_place_owner`). 그래서 시드는 계정 → 비콘 → 장소 배치 → 기기마다 신뢰·종료 정책 → 정책 순으로 반영한다(신뢰·종료 정책·정책 모두 장소 배치 뒤다).
- 이미 있는 장소를 다른 소유자로 다시 배치하는 요청은 서비스 검사를 지나 DB가 `CONFLICT`/`STATE_CONFLICT`로 거부한다. 쓰기는 일어나지 않는다.
- `AdminRepository.set_release_policy(node_id, release_policy)`를 추가한다. `node.release_policy`만 바꾸고 `Step.ADMIN` 감사와 한 트랜잭션이다. 진행 중 점유 세션은 생성 당시 값을 보존하므로 소급하지 않는다.
- 정책은 `current_policy`와 내용이 같으면 쓰지 않는다. 다르면 현재 버전을 `expected_version`으로 새 리비전을 추가한다. 정책이 없으면 버전 1로 등록한다. `policy_id`는 `pol-<node_id>`다.
- 시드 파일 TOML: `[[accounts]]`(user_id, name, role), `[[beacons]]`(beacon_id, user_id, uuid, major, minor, active), `[[places]]`(place_id, owner, name, nodes), `[[nodes]]`(node_id, trust, release_policy), `[[policies]]`(node_id, visibility, mode, approver, approval_mode, approval_ttl_sec, max_uses). 모르는 키는 거부한다. 선택 키: `beacons.active`(기본 true), `nodes.trust`, `nodes.release_policy`(없으면 건드리지 않는다), `policies.approver`, `approval_ttl_sec`, `max_uses`.
- 파일 검사는 키 중복, 두 장소에 든 기기, 어느 `[[places]].nodes`에도 없는 `[[nodes]]`·`[[policies]]` 기기를 입력 오류(2)로 거부한다. 다른 계정 참조(`beacons.user_id` 등)는 DB에 이미 있는 계정일 수 있어 DB에 맡긴다.
- `seed`는 DB 파일이 없으면 만든다 — 서버를 띄우기 전에도 계정과 장소를 넣을 수 있다. DB를 읽는 다른 명령(collect, split, fit, eval)은 DB가 없으면 2다.
- 시드는 파일 전체를 검사한 뒤 쓴다. 항목 하나가 거부되면 사유를 쓰고 나머지를 계속한다. 거부가 하나라도 있으면 종료 코드 1이다 — 기기 발표를 기다리는 동안 계정과 장소는 먼저 넣고 다시 실행할 수 있다.
- 다시 실행할 때 이미 있는 계정은 `ACCOUNT_ALREADY_EXISTS`를 "이미 있음"으로 쓰고 토큰을 출력하지 않는다. `AccountIssue.token`은 repr에서 뺀다.
- `--dry-run`은 파일 검사만 하고 DB를 열지 않는다.

### collect

- `collect run`과 `collect close`는 기존 `tools/data_collection/cli.main`에 설정의 DB로 만든 `SqliteAdminRepository`를 넘긴다.
- `--record`가 없으면 `var/records/<계획 파일 이름(확장자 제외)>-<UTC YYYYMMDD>.jsonl`이다. `nearpilot collect`가 이 경로를 계산해 기존 `tools/data_collection/cli.main`에 넘긴다. 수집 도구 코드는 바꾸지 않는다.
- `collect run`과 `collect close`는 DB 파일이 없으면 수집 도구를 부르지 않는다. `collect run`은 계획 파일을 찾은 뒤, 기록 폴더를 만들기 전에 확인한다.
- `--fit`은 `--summary --place`와 함께일 때만 받고, 회차를 읽기 전에 찾는다.
- `collect list`는 읽기 전용 연결로 `collection_run`과 연결된 관측 수를 읽는다. `--place`, `--label`, `--open`, `--summary`, `--fit`을 받는다. `--summary --place`는 적합 설정의 `min_runs_per_label`과 비교한 분할 가능 여부를 쓴다. 기본 적합 설정(`<장소>.toml`)이 없으면 그 칸을 `-`로 쓰고 안내한다. `--fit`으로 직접 준 파일이 없으면 다른 명령처럼 종료 코드 2다.
- 적합 설정 검사 하한: `[split].min_runs_per_label`은 3 이상(학습·검증·시험에 하나씩), 비율 합의 허용 오차는 1e-9다.
- 회차의 장소: 라벨이 기기면 그 기기의 `place_id`다. 라벨이 `none`이면 회차에 연결된 관측의 기기들이 속한 장소다. 여러 장소의 기기가 섞이면 장소를 정할 수 없는 회차로 보고 분할에서 뺀다.

### split

- 분할 로직은 `tools/evaluation/split.py`에 둔다 (DB 담당 폴더, 담당자 지시로 이 작업에서 만든다).
- 대상: 장소의 닫힌 회차 중 연결된 관측이 1개 이상인 회차. 장소를 정할 수 없는 회차는 그 사유로, 진행 중·관측 없는 회차는 각 사유로 제외 목록에 쓴다.
- 라벨마다 회차 ID를 정렬하고 `random.Random(f"{seed}:{label}")`로 섞은 뒤 학습·검증·시험 비율로 나눈다 — 회차 수가 같은 라벨이 같은 순열로 섞이지 않고, 라벨이 늘어도 다른 라벨의 배정이 바뀌지 않는다. 각 몫은 `floor(n × 비율)`이고(비율은 파일에 쓴 십진수 그대로 `Fraction`으로 계산) 남는 회차는 학습 → 검증 → 시험 순으로 하나씩 더한다. 검증과 시험은 최소 1개다(모자라면 학습에서 옮긴다).
- 비율과 라벨별 최소 회차 수는 적합 설정 TOML의 `[split]`(`ratios = [0.6, 0.2, 0.2]`, `min_runs_per_label = 5`)에서 받는다. 지표 8의 "위치별 독립 수집 ≥ 5회"가 기본 예시 값이다.
- 결과 JSON: `place_id`, `seed`, `ratios`, `created_at`, `db_path`, `train`/`validation`/`test`(회차 ID 목록), `excluded`(회차 ID와 사유), `labels`(라벨 → 학습·검증·시험 회차 수). 같은 입력·시드면 `created_at` 줄 말고 바이트까지 같다. 기본 파일 이름은 `<장소>-seed<시드>.json`이다.
- `split`은 적합 설정 파일 이름을 `--fit FILE`로 받는다. 기본은 `<장소>.toml`이다. `--seed` 기본값은 42다.

### fit

- `nearpilot fit LAYOUT --split FILE --version NAME [--fit FILE] [--calib-version NAME] [--dry-run]`. `--fit` 기본은 `<배치의 place_id>.toml`이다.
- 배치 JSON: `place_id`, `nodes`(node_id → `x`, `y`, `front_x`, `front_y`), `none_points`([[x, y], …], 선택, 기본 빈 목록). `PlaceLayout`으로 읽는다. 모르는 키는 거부한다.
- 적합 설정 TOML `[fit]`: `calib_version`(선택), `min_distance_m`, `taus`, `stays`, `window_secs`. 후보 목록은 모두 비어 있으면 안 된다.
- `calib_version`은 `--calib-version` → `[fit].calib_version` → `--version` 값 순으로 처음 있는 것이다.
- 분할 파일의 학습·검증 회차를 읽기 전용 DB에서 `LabeledRun`으로 만든다(`tools/evaluation/runs.load_labeled_runs`). 프레임은 회차에 연결된 관측이다. 비콘은 회차의 `beacon_id`로 찾는다. `bin_sec`는 설정의 `Settings.rssi_frame_sec`다. 시험 회차는 읽지 않는다.
- 배치의 기기 중 학습 회차에 라벨이 없는 기기가 있으면 적합하지 않는다 — 앵커가 셋 이상이면 `fit_model`이 다른 회차로 적합해 버린다.
- 출력: 앵커별 `A`, `n`, `sigma`, `none_rssi`, `none_sigma` 표, 조합별 손실 표(손실 순 상위 10개와 선택), 작성한 경로, `[proximity.models]`에 넣을 줄 안내. 서버 설정 파일은 고치지 않는다.

### eval

- `nearpilot eval PLACE/VERSION --split FILE [--fit FILE] [--out NAME] [--force]`.
- 시험 회차만 읽는다. 평가 시각은 `validation_log_loss`와 같다(`started_at + window_sec`부터 `bin_sec` 간격, `ended_at` 이하). 시각마다 `compute_posteriors`에 모델 파일의 모든 기기를 후보로 넘긴다 — 명령이 배치 파일을 받지 않는다.
- 정면 오선택률: 기기 라벨 회차의 평가 시각 중, 기기 상태 중 사후확률이 가장 큰 기기가 라벨과 다른 시각의 비율.
- ECE: 모든 시험 회차의 평가 시각에서 전체 상태(기기들과 none = 1 − 합) 중 최대 확률을 신뢰도로, 그 상태가 라벨과 같은지를 정답으로 둔다. 신뢰도 [0, 1]을 같은 폭 10구간으로 나눠 `Σ (구간 표본 수 / 전체) × |구간 정확도 − 구간 평균 신뢰도|`.
- 통과 기준은 적합 설정 TOML `[acceptance]`(`max_misselection`, `max_ece`)에서 받는다.
- 보고서 행: `metric`, `value`, `limit`, `passed`, `trials`, `failures`(오선택률만), `note`. 전환 시간과 깜빡임 행은 `value`를 비우고 `note = "미측정"`이다. 기기 라벨 시험 회차가 없으면 오선택률 행의 `value`, `passed`, `failures`를 비우고 `note = "기기 라벨 시험 회차 없음"`이다 — `write_report`가 `trials = 0`에서 신뢰구간을 계산하지 않게 한다.
- 보고서의 모든 행에 `place_id`, `model_version`, `calib_version`, `split_file`, `fit_file`을 남긴다 (requirements.md §4 "입력, 시나리오, 버전, 표본 수를 보존한다").
- `PLACE/VERSION`의 빈 조각, `.`, `..`, `\`는 입력 오류다. 기준에 못 미쳐도 종료 코드 0이다. `write_report`로 `var/reports/<이름>.csv`, `.md`를 쓴다. 기본 이름은 `<장소>-<버전>-<UTC YYYYMMDD>`다.

### docs

- `docs/architecture.md`: 디렉터리 표에 `cli/`, `core/admin/`, `config/` 하위 폴더, 테스트의 `cli/`를 넣는다. 담당 표에 AdminService(근접 추정 담당이 이 작업에서 만듦)를 넣고 "담당을 배정한 뒤 만든다"를 지운다. 진입점 미결을 `nearpilot serve`(`src/nearpilot/cli/`)와 조립 모듈 경로(`src/nearpilot/runtime/`)로 바꾼다. 관리 쓰기 표에 종료 정책 쓰기를 넣는다.
- `AGENTS.md`: 검증 명령의 서버 실행 진입점 줄을 `nearpilot serve`로 바꾼다.

## 7. 가정

- 지표 8의 세부 측정 정의는 미결이다 (architecture.md §4 "RSSI 수집 회차"). 정면 오선택률과 ECE를 6절 "eval"의 정의로 계산한다. 정의가 정해지면 `tools/evaluation`의 계산 함수만 고친다.
- 수집 회차의 조회 접점은 미결이다 (architecture.md §4 "RSSI 수집 회차"). 이 작업은 `tools/evaluation/runs.py`(`list_runs`, `load_labeled_runs`)를 조회 접점으로 가정하고, 회차의 장소 규칙을 6절 "collect"로 정한다. 정해진 접점이 다르면 `runs.py`와 그 호출만 바꾼다.
- 중단된 회차(Ctrl+C, `collect close`로 닫은 회차)를 빼는 규칙은 `specs/core-proximity-data-collection` 4절의 후속 작업이다. 이 작업은 닫힌 회차를 모두 분할 대상으로 본다.
- 무드등 장소의 수집 라벨은 미결이다 (#61). 기기가 하나인 장소도 같은 규칙으로 분할·적합한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-01 | `test_FR01_seed_token_printed_once_hash_stored` | 토큰 원문은 출력에만, DB에는 해시 |
| FR-01 | `test_FR01_seed_duplicate_beacon_rejected` | 다른 계정의 비콘 거부, 기존 활성 유지 |
| FR-05 | `test_FR05_collect_only_starts_without_models` | 모델 없이 시작하고 RSSI 저장 |
| FR-06 | `test_FR06_collect_only_has_no_mcp` | `/mcp` 요청이 도달하지 않음 |
| FR-12A | `test_FR12A_seed_invalid_file_writes_nothing` | 잘못된 시드는 아무것도 쓰지 않음 |
| FR-12A | `test_FR12A_seed_is_rerunnable` | 두 번 실행해도 같은 상태, 토큰 재발급 없음 |
| FR-12A | `test_FR12A_seed_dry_run_writes_nothing` | dry-run은 쓰지 않음 |
| FR-12A | `test_FR12A_admin_rejects_non_owner` | 소유자가 아닌 행위자의 장소 쓰기 거부 |
| FR-13 | `test_FR13_seed_unknown_node_reports_and_continues` | 없는 기기만 거부, 나머지 반영, 종료 코드 1 |
| FR-13 | `test_FR13_set_release_policy_unknown_node` | 없는 기기는 `NODE_NOT_FOUND` |
| FR-14 | `test_FR14_admin_policy_new_revision_only_when_changed` | 같은 정책은 건너뜀, 다른 정책은 새 리비전 |
| FR-16 | `test_FR16_cli_missing_default_file_lists_candidates` | 기본 폴더 안내 |
| FR-16 | `test_FR16_cli_path_argument_uses_cwd` | 경로 인자는 실행 폴더 기준, `\` 포함 |
| FR-16 | `test_FR16_cli_invalid_config_creates_nothing` | 설정 오류에서 DB를 만들지 않음 |
| FR-16 | `test_FR16_serve_requires_models_without_collect_only` | 일반 serve는 모델 필수 |
| FR-16 | `test_FR16_serve_startup_failure_exits_1` | 시작 실패는 종료 코드 1 |
| FR-16 | `test_FR16_cli_read_db_path_with_special_characters` | 특수문자 경로의 읽기 전용 연결 |
| FR-16 | `test_FR16_collect_requires_existing_db` | DB 없으면 아무것도 만들지 않음 |
| FR-16 | `test_FR16_collect_close_ends_open_run` | 진행 중 회차 닫기 |
| FR-16 | `test_FR16_collect_default_record_path` | 기록 파일 기본 경로 |
| FR-16 | `test_FR16_collect_list_open_only` | 진행 중 회차만 |
| FR-16 | `test_FR16_collect_list_summary` | 라벨별 요약 |
| FR-16 | `test_FR16_split_run_in_exactly_one_part` | 회차가 한 분할에만 |
| FR-16 | `test_FR16_fit_place_mismatch_rejected` | 장소 불일치 거부 |
| FR-16 | `test_FR16_fit_existing_version_not_overwritten` | 같은 버전 덮어쓰지 않음 |
| FR-16 | `test_FR16_fit_error_writes_nothing` | `FitError`에서 파일 없음 |
| FR-16 | `test_FR16_fit_dry_run_writes_nothing` | dry-run은 파일 없음 |
| FR-16 | `test_FR16_fit_missing_label_fails` | 학습 라벨이 없는 기기 거부 |
| FR-16 | `test_FR16_config_examples_load` | 예시 파일이 읽힘 |
| FR-16 | `test_FR16_ops_pipeline_end_to_end` | 수집 데이터 → 분할 → 적합 → 평가 → 서버 시작 |
| FR-22 | `test_FR22_set_release_policy_audited` | 종료 정책 변경이 감사와 한 트랜잭션 |
| NFR-06 | `test_NFR06_eval_uses_test_runs_only` | 시험 회차만 읽음 |
| NFR-06 | `test_NFR06_eval_misselection_and_ece` | 손으로 계산한 값과 같음 |
| NFR-06 | `test_NFR06_eval_reports_unmeasured_items` | 미측정 항목 표시 |
| NFR-13 | `test_NFR13_split_excludes_open_and_empty_runs` | 진행 중·빈 회차 제외 |
| NFR-13 | `test_NFR13_split_deterministic_by_seed` | 같은 시드 같은 결과 |
| NFR-13 | `test_NFR13_split_rejects_too_few_runs` | 회차 부족 거부 |
| NFR-13 | `test_NFR13_split_does_not_overwrite` | 결과 파일 덮어쓰지 않음 |
| NFR-13 | `test_NFR13_eval_report_requires_force` | 보고서 덮어쓰기 보호 |
