# 근접 수집 도구 (`tools/data_collection`)

- 담당: dnnals (근접 추정)
- 요구사항: FR-16
- 따르는 결정: #41, #131, #138

## 1. 왜

- 근접 모델을 실데이터로 적합하고 평가하려면 라벨이 붙은 관측이 필요하다 (지표 8).
- DB는 비콘과 관측 시각으로 수집 회차를 찾아 관측에 회차 ID를 연결한다. 회차를 만드는 도구가 없어서 연결할 회차가 없다.
- "앞"의 거리 기준은 근접 추정 구현 담당이 spec에서 정한다 (FR-16). 기준이 없으면 수집 라벨의 뜻이 정해지지 않는다.
- 수집 방법이 사람마다 다르면 회차 단위 분할과 평가가 의미를 잃는다.

## 2. 무엇

- 입력:
  - 수집 계획 파일: 장소, 비콘, 자리 잡는 시간, 회차 길이, 회차 목록(라벨과 서는 위치 안내)
  - 수집하는 사람의 키 입력: 자리를 잡았다는 Enter, 멈춤
  - `AdminRepository` 구현체
- 출력:
  - `AdminRepository.start_collection`과 `end_collection` 호출
  - 회차 기록 파일(JSONL): 회차마다 회차 ID, 라벨, 서는 위치 안내, 시작 시각, 끝 시각, 결과
  - 화면 안내와 종료 코드
- 바뀌는 상태: DB의 `collection_run` 행(`AdminRepository`를 거친다). 회차 기록 파일.
- 약속:
  - 계획 파일을 모두 검사한 뒤 첫 회차를 만든다. 계획 파일이 잘못됐으면 아무 회차도 만들지 않는다.
  - 회차 하나마다 `start_collection` 한 번과 `end_collection` 한 번을 부른다.
  - 시작한 회차는 끝 시각을 기록한다. 기록하지 못하면 회차 ID를 화면에 남긴다. 세션이 회차를 마치고 처리한 경우에는 기록 파일에도 `end_failed`로 남긴다.
  - DB가 거부하면 사유를 화면에 쓰고 남은 회차를 실행하지 않는다.
  - 라벨은 계획 파일의 값을 그대로 쓴다.

## 3. 제약

- 수집 회차는 `AdminRepository`로만 기록한다. SQL을 실행하지 않는다 (#131).
- MQTT 브로커를 구독하지 않는다. 관측 연결은 DB가 한다 (#41).
- `RssiFrame`과 MQTT 메시지에 회차 ID나 라벨을 넣지 않는다 (#41).
- 라벨은 기기 앞(`node_id`) 또는 `none`이다 (FR-16).
- 회차 ID는 이 도구가 만든다 (#131).
- 수집 구간은 `started_at <= frame_ts < ended_at`이다. 이 도구는 `started_at`과 `ended_at`만 정한다 (#41).
- 시간 값을 코드 상수로 쓰지 않는다. 계획 파일에서 받는다 (AGENTS.md 코딩 규칙).
- `shared`의 모델과 Protocol에만 의존한다 (AGENTS.md 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/tools/data_collection/`
- `server/tests/tools/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/`, `server/tools/node_sim/`, `server/tools/evaluation/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `AdminRepository.start_collection`, `AdminRepository.end_collection` | 사용 | DB |
| `AdminWriteResult`, `AdminWriteOutcome`, `AdminWriteReason` | 사용 | DB |
| `BeaconKey` | 사용 | 공용 모델 |
| 수집 회차의 라벨과 구간 (`collection_run`) | 제공 | DB의 `tools/evaluation` |

### 만들지 않는 것
| 후속 작업 | 내용 | 근거 |
|---|---|---|
| (서버 실행 진입점 뒤) | `python -m tools.data_collection` 진입점에서 DB 경로로 `SqliteAdminRepository`(#136)를 만드는 배선 | #88 |
| `feat/core-proximity-calibration` | 수집한 관측으로 모델 적합, 운영 모델 파일의 `none_points` | FR-16, 지표 8 |
| DB의 `tools/evaluation` | 회차 단위 분할, 평가, 중단된 회차를 평가에서 빼는 규칙 | FR-16, 지표 8 |

- 수집 회차의 조회와 구간 변경 — 미결이다. 평가 도구 spec에서 맞춘다 (`architecture.md` §4 RSSI 수집 회차).
- 실제 장소의 수집 계획 파일 — 기기 배치가 정해진 뒤 수집하는 사람이 만든다. 이 작업은 테스트용 계획 파일만 만든다.
- 다른 PC나 폰에서 수집을 조작하는 기능 — 관리 API가 필요하다. 이 도구는 DB 파일이 있는 서버 PC에서 실행한다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 계획 파일이 없거나 JSON이 아니다 | 회차를 만들지 않는다. 사유를 쓰고 종료 코드 2로 끝낸다 | `test_FR16_invalid_plan_rejected_before_any_write` |
| 계획 파일의 필드가 빠졌거나 형식이 틀리다 | 회차를 만들지 않는다. 잘못된 필드를 쓰고 종료 코드 2로 끝낸다 | `test_FR16_invalid_plan_rejected_before_any_write` |
| 회차 목록이 비었다 | 회차를 만들지 않는다. 종료 코드 2로 끝낸다 | `test_FR16_invalid_plan_rejected_before_any_write` |
| 라벨이 빈 문자열이거나 앞뒤 공백이 있다 | 회차를 만들지 않는다. 종료 코드 2로 끝낸다 | `test_FR16_invalid_plan_rejected_before_any_write` |
| `--start-at`이 회차 수 범위 밖이다 | 회차를 만들지 않는다. 종료 코드 2로 끝낸다 | `test_FR16_start_at_out_of_range_is_rejected` |
| `start_collection`이 거부한다 (`BEACON_NOT_FOUND`(등록되지 않았거나 비활성인 비콘, #138), `NODE_NOT_FOUND`, `COLLECTION_OVERLAP`, `STATE_CONFLICT`) | 사유를 쓴다. 남은 회차를 실행하지 않는다. 종료 코드 1로 끝낸다 | `test_FR16_start_rejected_stops_session` |
| `start_collection`이 예외를 던진다 | 예외와 회차 ID, `close` 명령을 쓴다. 기록 파일에 쓰지 않는다. 남은 회차를 실행하지 않는다. 종료 코드 1로 끝낸다 | `test_FR16_start_error_stops_session` |
| `end_collection`이 거부하거나 예외를 던진다 | 회차 ID와 사유를 쓴다. 기록 파일에 `end_failed`로 남긴다. `close` 명령을 안내한다. 종료 코드 1로 끝낸다 | `test_FR16_end_failure_keeps_collection_id` |
| 회차 도중에 Ctrl+C를 누른다 | 그 시각으로 `end_collection`을 부른다. 기록 파일에 `aborted`로 남긴다. `end_collection`이 실패하면 `end_failed`로 남긴다. 남은 회차를 실행하지 않는다. 종료 코드 130으로 끝낸다 | `test_FR16_interrupt_during_run_ends_collection` |
| 자리 잡기 전이나 자리 잡는 시간에 Ctrl+C를 누르거나 `q`를 입력하거나 입력이 끝난다(EOF) | 회차를 만들지 않는다. Ctrl+C는 종료 코드 130, `q`와 EOF는 0으로 끝낸다 | `test_FR16_quit_before_start_writes_nothing` |
| 기록 파일에 쓰지 못한다 | 회차를 시작하기 전에 확인한다. 쓰지 못하면 회차를 만들지 않고 종료 코드 2로 끝낸다 | `test_FR16_unwritable_record_is_rejected_before_any_write` |
| 회차를 마친 뒤 기록 파일에 쓰지 못한다 | 회차 ID와 오류를 쓴다. 남은 회차를 실행하지 않는다. 종료 코드 1로 끝낸다 | `test_FR16_record_write_failure_stops_session` |
| 세션이 잡지 않은 시점(예: `start_collection` 반환 직후)에 Ctrl+C를 누른다 | 멈췄다고 쓰고 종료 코드 130으로 끝낸다. 회차 ID는 `start_collection`을 부르기 전에 화면에 있다 | `test_FR16_cli_interrupt_returns_130`, `test_FR16_cli_interrupt_leaking_from_session_returns_130` |
| 같은 비콘으로 도구 두 개를 함께 실행한다 | 뒤에 시작한 회차를 DB가 `COLLECTION_OVERLAP`으로 거부한다. 도구는 거부와 같이 처리한다 | `test_FR16_start_rejected_stops_session` |
| `AdminRepository`를 만드는 함수가 예외를 던진다 | 예외를 쓰고 종료 코드 1로 끝낸다 | `test_FR16_cli_repository_factory_error` |
| `close`로 이미 끝난 회차를 다시 닫는다 | DB의 `STATE_CONFLICT`를 쓰고 종료 코드 1로 끝낸다 | `test_FR16_close_reports_rejection` |

- 권한 없는 호출: 해당 없음. 이 도구는 서버 PC에서 실행하고 인증하지 않는다.
- 시간 초과: 이 도구는 DB 호출에 시간 제한을 두지 않는다. DB 호출이 예외를 던지면 위 표를 따른다.

## 6. 결정

### "앞"의 거리 기준

- 라벨 "기기 X 앞"은 방문자가 모델 파일의 기기 X 앞 위치(`front_x`, `front_y`)에서 반경 0.3 m 안에 서 있다는 뜻이다 — 모델 파일이 이미 기기마다 앞 위치 점을 가진다. 라벨과 모델이 같은 위치를 가리키고, 기기마다 따로 정할 값이 없다.
- 라벨 `none`은 방문자가 모든 기기 앞 위치에서 1 m 이상 떨어져 있다는 뜻이다 — 근접 추정기 spec의 합성 파일 규칙("기기 앞 위치보다 1 m 이상 뒤에 점을 둔다")과 같다.
- 앞 위치에서 0.3 m 초과 1 m 미만인 곳에서는 수집하지 않는다 — 라벨이 애매한 위치를 학습과 평가에서 뺀다.
- 이웃한 두 기기 앞 위치의 간격은 0.6 m 이상이어야 한다 — 간격이 더 좁으면 두 원이 겹쳐 한 위치에 라벨이 둘이 된다. 기기 배치가 이 조건을 어기면 수집하지 않고 담당자에게 알린다.

### 수집 방법

- 기기 앞 라벨은 기기마다 5회차 이상 수집한다 — 지표 8의 "위치별 독립 수집 ≥ 5회"를 따른다.
- 기기 앞 회차의 서는 점은 회차마다 바꾼다 — 공개 데이터 점검에서 한 점만 학습하면 모델이 과신했다. 서는 점은 앞 위치, 앞 위치에서 좌우와 앞뒤로 0.2 m인 네 점, 모두 다섯 점이다.
- `none` 라벨은 5회차 이상 수집한다. 위치는 회차마다 바꾼다 — 위치 종류는 기기 뒤 1~3 m, 기기 사이의 뒤쪽, 장소 밖이다. 근접 추정기 테스트에서 "기기 뒤 1~3 m"가 오선택이 컸던 위치다.
- 회차 사이에는 자리를 떠났다가 다시 선다 — 회차가 서로 독립이어야 회차 단위 분할이 의미를 가진다.
- 같은 라벨의 회차를 연이어 두지 않도록 계획 파일을 짠다 — 시간에 따른 환경 변화가 한 라벨에 몰리지 않는다. 도구는 이 순서를 검사하지 않는다.
- 폰의 휴대 자세는 계획 파일의 서는 위치 안내에 쓴다. 기본은 손에 들고 화면이 위를 향하는 자세다.

### 도구

- 수집 계획 파일(JSON)을 따라 회차를 차례로 실행한다 — 회차 길이와 순서가 사람 손에 달리지 않는다.
- 계획 파일은 아래 필드를 담는다.
  - `place_id`: 기록용 장소 ID. DB 호출에 쓰지 않는다.
  - `beacon`: `uuid`, `major`, `minor`
  - `settle_sec`: Enter 뒤 회차를 시작하기 전까지 기다리는 시간. 0 이상이다.
  - `run_sec`: 회차 길이. 0보다 크다.
  - `runs`: 회차 목록. 회차마다 `label`과 서는 위치 안내 `spot`(빈 문자열이 아닌 문자열)을 담는다.
- 계획 파일의 모르는 키는 거부한다 — 필드 이름의 오타를 읽을 때 찾는다. `major`와 `minor`는 0 이상 65535 이하의 정수다 (iBeacon 필드 범위).
- 회차 하나는 아래 순서로 실행한다.
  1. 회차 번호, 라벨, 서는 위치 안내를 화면에 쓴다.
  2. Enter를 기다린다. `q`를 입력하거나 입력이 끝나면(EOF) 세션을 끝낸다.
  3. `settle_sec`를 기다린다.
  4. 회차 ID를 만들어 화면에 쓰고, 현재 시각을 `started_at`으로 `start_collection`을 부른다 — 부른 직후에 프로세스가 멈춰도 화면의 ID로 `close`할 수 있다.
  5. `run_sec`를 기다린다.
  6. 현재 시각을 `ended_at`으로 `end_collection`을 부른다.
  7. 기록 파일에 한 줄을 덧붙인다.
- 회차 ID는 `col-`과 UUID4 hex 32자를 붙인 값이다 — 수집 PC가 여럿이어도 겹치지 않는다. 테스트는 ID 생성 함수를 주입한다.
- 시각은 UTC 기준 시각이 붙은 `datetime`이다. 시계, 대기, 입력, 출력, ID 생성 함수를 주입받는다 — 테스트가 실제 시간을 기다리지 않는다.
- `start_collection`이 예외를 던지면 회차 ID와 `close` 명령을 화면에 쓰고, 기록 파일에는 쓰지 않는다 — DB에 회차가 생겼는지 알 수 없다. 생겼다면 `close`로 닫는다.
- DB가 거부하면 남은 회차를 실행하지 않는다 — 원인(비콘 미등록, 겹침)을 고치지 않고 계속하면 같은 거부가 반복된다.
- 회차 기록 파일은 JSONL이다. 한 줄은 `index`, `collection_id`, `place_id`, `beacon`, `label`, `spot`, `started_at`, `ended_at`, `status`를 담는다. `status`는 `done`, `aborted`, `end_failed` 중 하나다. `end_failed`면 `ended_at`은 `null`이다 — `collection_run`에는 서는 위치 안내가 없다. 위치별 분석과 중단 회차 확인에 기록 파일을 쓴다.
- 회차를 시작하기 전에 기록 파일을 덧붙이기 모드로 연다 — 열지 못하면 DB에 회차를 만들기 전에 멈춘다.
- `--start-at N`으로 N번째 회차부터 실행한다. N은 1부터 센다 — 중단한 세션을 이어서 수집한다. 범위는 `AdminRepository`를 만들기 전에 검사한다 — 잘못된 인자로 DB 파일을 만들지 않는다.
- 명령은 `run PLAN --record PATH [--start-at N]`과 `close --collection-id ID` 두 개다. `--record`는 기본값이 없다 — 기록 파일을 어디에 둘지 수집하는 사람이 정한다.
- Ctrl+C로 중단한 회차의 `end_collection`이 실패하면 `status`는 `end_failed`, 종료 코드는 130이다.
- `close --collection-id ID` 명령은 현재 시각으로 `end_collection`을 부른다 — 끝 시각을 기록하지 못한 회차가 남으면 같은 비콘의 다음 회차가 모두 겹침으로 거부된다.
- CLI는 표준 라이브러리 `argparse`로 만든다 — 운영체제에 의존하지 않는다.
- CLI 함수는 `AdminRepository`를 만드는 함수를 인자로 받는다 — DB 파일 경로와 연결을 만드는 방법이 미결이다 (#88). `python -m` 진입점의 배선은 후속 작업이다. 테스트는 fake를 쓴다.
- 테스트는 `server/tests/tools/conftest.py`에서 `server/`를 `sys.path`에 넣어 `tools` 패키지를 가져온다 — `pyproject.toml`의 `pythonpath`는 `src`뿐이고, 이 작업은 `pyproject.toml`을 고치지 않는다.

## 7. 가정

- 기기 시계와 서버 시계의 차이는 `settle_sec`보다 작다 — 미결 (시각 동기화와 최대 오차, `architecture.md` §5). 회차 시각은 서버 시계로 정하고 구간 판정은 기기의 `frame_ts`로 한다. 차이가 크면 구간 앞뒤의 관측이 다른 위치의 관측일 수 있다.
- 끝 시각을 기록하지 못한 회차와 중단한 회차를 평가에서 뺄지는 평가 도구가 정한다 — 미결 (수집 회차의 조회와 구간 변경, `architecture.md` §4). 이 도구는 기록 파일에 `status`를 남긴다.
- 무드등 장소의 수집 라벨은 다른 장소와 같다 — 미결 (#61). 무드등 장소에는 기기가 하나라서 라벨은 그 기기 앞 또는 `none`이다. 결정이 달라도 계획 파일만 바뀐다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-16 | `test_FR16_plan_file_is_loaded` | 계획 파일의 필드를 읽는다. 정수 시간 값도 받는다 |
| FR-16 | `test_FR16_runs_record_collections_in_order` | 회차마다 `start_collection`과 `end_collection`을 한 번씩 순서대로 부른다. 비콘, 라벨, 시각, 회차 ID가 맞다 |
| FR-16 | `test_FR16_run_timing_uses_plan_values` | 시작 시각은 Enter 뒤 `settle_sec`, 끝 시각은 시작 뒤 `run_sec`다 |
| FR-16 | `test_FR16_record_file_lines` | 기록 파일의 한 줄이 회차 하나와 같다 |
| FR-16 | `test_FR16_start_at_resumes_from_index` | `--start-at`의 회차부터 실행한다 |
| FR-16 | `test_FR16_collection_id_format` | 기본 회차 ID가 `col-`과 hex 32자다 |
| FR-16 | `test_FR16_close_ends_collection` | `close`가 현재 시각으로 `end_collection`을 부른다 |
| FR-16 | 5절 표의 `test_FR16_*` | 예외 상황 |

- 테스트는 `AdminRepository`의 fake를 쓴다. fake는 호출을 기록하고 정해 둔 결과를 반환한다.
- 시계, 대기, 입력은 가짜 함수로 바꾼다. 테스트는 실제 시간을 기다리지 않는다.
