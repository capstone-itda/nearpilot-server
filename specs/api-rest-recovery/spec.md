# FAULT 복구 REST와 복구 명령

- 담당: dnnals (관리·복구 API, 복구 스크립트)
- 요구사항: FR-12A, NFR-04, NFR-07, NFR-08
- 따르는 결정: #20, #89, 2026-10-26 담당자 결정 (관리·복구 API와 복구 스크립트의 담당을 dnnals로 정함)

## 1. 왜

기기가 `FAULT`로 격리되면 소유자가 풀 경로가 없다. 상태 관리의 `RecoveryService`(`FaultRecoveryService`)는 있지만 HTTP로 열려 있지 않고, 소유자가 부를 스크립트도 없다. 실행 중인 서버에서 `GET /admin/nodes/{node_id}/recovery`는 404다 (FR-12A, NFR-04).

## 2. 무엇

- 입력: 소유자의 Bearer 토큰, `node_id`, 복구 승인의 `recovery_id`
- 출력: 복구 준비 상태(상태, 부족한 조건, 근거, 확인 시각), 복구 승인 결과(`completed`, `pending`) 또는 거부 사유
- 바뀌는 상태: 복구 승인이 `RecoveryService.approve`로 복구 정산을 저장한다. 조회는 상태를 바꾸지 않는다
- 약속:
  - 두 경로는 토큰으로 계정을 식별해 `RecoveryService.readiness`, `approve`에 하나씩 넘긴다. 결과를 바꾸지 않는다 (NFR-07, architecture.md §4 "복구 준비 조회와 복구 승인").
  - 응답에 사용자 ID, 점유 주체, 기기 연결 세션 ID를 넣지 않는다 (FR-11, NFR-08).
  - 실행 중인 서버가 두 경로를 연다. 수집 전용 서버에서도 연다.
  - 소유자는 `nearpilot recover status`와 `nearpilot recover approve`로 두 경로를 부른다. 명령은 DB를 열지 않고 REST만 쓴다.

## 3. 제약

- 경로는 `GET /admin/nodes/{node_id}/recovery`, `POST /admin/nodes/{node_id}/recovery/{recovery_id}/approve`다 (architecture.md §5 REST).
- 인증은 `Authorization: Bearer <토큰>`이다. 토큰이 없거나 틀리면 `401`과 `{"error": "INVALID_TOKEN"}`이다 (architecture.md §5 REST "공통 규칙").
- 거부와 실패의 본문은 `{"error": "<사유 코드>"}`다. 사유 코드는 상태 관리가 반환한 값을 그대로 쓴다 (NFR-07).
- API는 DB를 직접 부르지 않는다. 계정 식별만 `Authenticator`로 한다 (AGENTS.md 의존 규칙 1).
- 요청 본문에서 계정 ID, 물리 상태, 정산 내용을 받지 않는다. 승인 시각은 서버가 정한다 (architecture.md §4).
- 장소 소유권, 준비 판단, 재검사는 상태 관리가 한다. API는 판정하지 않는다 (NFR-04, NFR-07).
- 토큰 원문은 로그, 오류, 명령 출력에 쓰지 않는다 (NFR-08).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/rest/` (새 라우터)
- `server/src/nearpilot/runtime/` (라우터 연결, 시작 뒤 복구 서비스 연결)
- `server/src/nearpilot/cli/` (새 하위 명령 `recover`)
- `server/tests/api/`, `server/tests/integration/`, `server/tests/cli/`
- `server/config/README.md` (복구 명령 사용법)
- `docs/requirements.md`, `docs/architecture.md` (관리·복구 API와 복구 스크립트 담당의 `미결`을 2026-10-26 결정으로 바꾼다)

### 고치지 않는 곳
- 항상: `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/core/`, `server/src/nearpilot/db/`, `server/src/nearpilot/api/auth/`, `server/src/nearpilot/iot/`, `firmware/`, `dashboard/`, `app/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `RecoveryService.readiness`, `approve` | 사용 | 상태 관리 (`core/lifecycle/fault_recovery.py`) |
| 조회의 소유권 없음 예외 (`NotPlaceOwner`, `PermissionError`의 하위 클래스) | 사용 (`PermissionError`로 받는다) | 상태 관리 |
| `Authenticator.authenticate` | 사용 | 판정 (`api/auth/`) |
| `build_recovery_router(recovery, authenticator)` | 제공 | 서버 조립 |
| `GET /admin/nodes/{node_id}/recovery`, `POST /admin/nodes/{node_id}/recovery/{recovery_id}/approve` | 제공 | 소유자 스크립트 |

### 만들지 않는 것
- 대시보드의 복구 화면. 대시보드는 복구 화면을 두지 않는다 (architecture.md §4).
- 복구 준비 판단과 정산 규칙의 변경. 센서 제외 전환을 맡은 담당이 상태 관리 spec에서 한다.
- 결과 불명 명령의 승인 사용분 정산 기준. 보류를 유지한다 (NFR-04).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 토큰이 없거나 틀리다 | `401`과 `INVALID_TOKEN`. 서비스를 부르지 않는다 | `test_FR12A_recovery_requires_token` |
| 계정 조회가 실패했다 | `500`과 `INTERNAL_ERROR` | `test_FR12A_recovery_lookup_failure_is_internal_error` |
| 조회 계정이 기기 장소의 소유자가 아니다 (서비스가 `PermissionError`를 낸다) | `403`과 `NOT_OWNER` | `test_FR12A_readiness_not_owner_is_403` |
| 복구 건이 없다 | `200`, `status: not_ready`, `blockers: ["NO_RECOVERY_REQUIRED"]`, `recovery_id: null` | `test_FR12A_readiness_returns_owner_view` |
| 승인 결과가 `rejected` | 사유별 상태 코드와 `{"error": 사유}`: `NOT_OWNER` 403, `RECOVERY_MISMATCH`·`NOT_READY`·`STATE_CONFLICT` 409, `STORE_NOT_APPLIED` 503 | `test_NFR04_approve_reasons_map_to_status` |
| 승인 결과가 `pending` (저장 결과 불명, 정산 진행 중) | `202`와 결과 본문. 완료를 안내하지 않는다 | `test_NFR04_approve_pending_is_202` |
| 같은 복구 건의 반복 승인 | `200`과 기존 완료 결과 | `test_NFR04_approve_completed_twice_returns_200` |
| 승인 요청에 본문이 있다 | 본문을 읽지 않는다. 계정은 토큰으로만 정한다 | `test_NFR04_approve_ignores_body` |
| 서비스가 예외를 냈다 | `500`과 `INTERNAL_ERROR`. 예외 내용은 응답에 넣지 않는다 | `test_FR12A_recovery_service_error_is_internal_error` |
| 서버가 아직 시작을 마치지 않아 복구 서비스가 연결되지 않았다 | 래퍼가 `RuntimeError`를 내고 라우터는 `500`과 `INTERNAL_ERROR` | `test_FR12A_runtime_recovery_unbound_is_internal_error` |
| 명령: 토큰 파일이 없거나 비었다 | 종료 코드 2, 요청을 보내지 않는다 | `test_FR12A_recover_cli_token_file_errors` |
| 명령: 서버에 연결할 수 없다 | 종료 코드 1과 연결 실패 안내 | `test_FR12A_recover_cli_connection_error` |
| 명령: 서버가 거부했다 | 종료 코드 1과 사유 코드 | `test_FR12A_recover_cli_rejected` |

## 6. 결정

- 라우터는 `build_recovery_router(recovery, authenticator)`가 만드는 FastAPI `APIRouter`다 — 계정·승인 라우터와 같은 방식이다. 서버 조립이 MCP 마운트보다 먼저 붙인다.
- 복구 서비스는 서버 시작(`Runtime._build`) 때 만들어지고 라우터는 그 전에 붙는다. 조립은 시작 전에 늦게 연결하는 래퍼(`Runtime.recovery`)를 라우터에 넘기고, `_build`에서 실제 서비스를 연결한다. 호출은 다른 REST처럼 접수 문과 DB 연결 범위 안에서 한다. 연결 전 호출은 `RuntimeError`이고 라우터는 다른 서비스 예외처럼 `500`과 `INTERNAL_ERROR`로 응답한다. uvicorn은 시작을 마친 뒤에 소켓을 열므로 운영에서는 닿지 않는다.
- 준비 조회 응답 키: `node_id`, `recovery_id`, `status`, `blockers`(사유 코드 문자열 배열), `evidence`, `checked_at`. `evidence` 항목 키는 `observed_at`, `execution_outcome`, `command_idle`이다 — 기기 연결 세션 ID(`session_id`)와 그것을 담은 근거 ID(`evidence_id`)는 사용자에게 쓸모없는 내부 식별자라 뺀다 (architecture.md §4, NFR-08). 시각은 시간대를 포함한 ISO 8601 문자열이고, 없는 값은 `null`이다.
- 승인 성공 응답 키: `recovery_id`, `outcome`, `reason_code`, `completed_at`, `readiness`(준비 조회와 같은 형식). `completed`는 `200`, `pending`은 `202`다.
- 거부(`rejected`)는 공통 규칙의 `{"error": "<사유 코드>"}`만 보낸다. 부족한 조건은 준비 조회로 본다. 사유별 상태 코드는 5절 표와 같다. `STORE_NOT_APPLIED`는 변경 없이 다시 시도할 수 있는 저장 오류라 `503`이다.
- 승인은 본문을 읽지 않는다 — 내부 입력은 인증 계정, `node_id`, `recovery_id`뿐이다 (architecture.md §4).
- 서비스 호출은 동기 함수라 `asyncio.to_thread`로 부른다 — 다른 REST와 같다.
- 조회의 소유권 없음은 `PermissionError`로 받는다 — API는 core의 예외 클래스를 import하지 않는다 (AGENTS.md 의존 규칙 4). `NotPlaceOwner`는 `PermissionError`의 하위 클래스다.
- 복구 명령은 `nearpilot recover status <node_id>`와 `nearpilot recover approve <node_id> <recovery_id>`다. 토큰은 `--token-file PATH`(필수)로 받는다 — 명령줄 인자는 셸 기록에 남는다. 서버 주소는 `--server URL`이고, 없으면 설정 파일의 `[http]` `host`·`port`로 `http://{host}:{port}`를 만든다(`host`가 `0.0.0.0`이면 `127.0.0.1`). 설정은 `load_config(require_models=False)`로 읽는다.
- 명령의 종료 코드: 준비 조회 성공 0 (상태가 `not_ready`여도 0), 승인 `completed` 0, 승인 `pending`과 거부와 연결 실패 1, 입력 오류 2. 출력은 사람이 읽는 한국어 줄이다. `status`는 상태, 복구 ID, 부족한 조건을 쓰고, `ready`이면 실행할 `approve` 명령 한 줄을 함께 쓴다.
- HTTP 호출은 표준 라이브러리 `urllib.request`로 한다 — 새 의존성을 더하지 않는다.

## 7. 가정

- 운영 데이터가 없으므로 기존 복구 건의 이전은 다루지 않는다 (2026-10-26 담당자 결정).

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-12A | `test_FR12A_readiness_returns_owner_view` | 토큰 계정과 `node_id`로 `readiness`를 부르고 키·시각 형식을 반환한다. 복구 건이 없으면 `not_ready`와 `NO_RECOVERY_REQUIRED` |
| FR-12A | `test_FR12A_readiness_not_owner_is_403` | `PermissionError`는 `403`과 `NOT_OWNER` |
| FR-12A | `test_FR12A_recovery_requires_token` | 두 경로 모두 토큰이 없으면 `401`이고 서비스를 부르지 않는다 |
| FR-12A | `test_FR12A_recovery_lookup_failure_is_internal_error` | 계정 조회 실패는 `500` |
| FR-12A | `test_FR12A_recovery_service_error_is_internal_error` | 서비스 예외는 `500`, 응답에 예외 문구가 없다 |
| NFR-04 | `test_NFR04_approve_passes_ids` | 토큰 계정, 경로의 `node_id`, `recovery_id`를 `approve`에 넘기고 `completed`는 `200` |
| NFR-04 | `test_NFR04_approve_pending_is_202` | `pending`은 `202`와 결과 본문 |
| NFR-04 | `test_NFR04_approve_completed_twice_returns_200` | 같은 복구 건의 반복 승인은 `200`과 같은 완료 결과 |
| NFR-04, NFR-07 | `test_NFR04_approve_reasons_map_to_status` | 거부 사유마다 상태 코드와 사유를 그대로 옮긴다 |
| NFR-04 | `test_NFR04_approve_ignores_body` | 본문의 계정·정산 값을 쓰지 않는다 |
| NFR-08 | `test_NFR08_recovery_responses_hide_internal_ids` | 응답에 사용자 ID, 점유 주체, `session_id`가 없다 |
| FR-12A | `test_FR12A_runtime_recovery_rest` (integration) | 실제 Runtime에서 두 경로가 열린다. 수집 전용과 대시보드 설정 조합 모두 |
| FR-12A | `test_FR12A_runtime_recovery_unbound_is_internal_error` (integration) | 시작 전 래퍼 호출은 `RuntimeError`, 라우터 응답은 `500` |
| NFR-04 | `test_NFR04_runtime_recovery_flow` (integration) | 결과 불명으로 격리된 기기를 준비 조회 → `ready` → 승인 → `completed` → 기기 `AVAILABLE`로 복구한다 |
| FR-12A | `test_FR12A_recover_cli_status` | `status` 출력과 종료 코드, `Authorization` 헤더에 토큰 파일 내용을 쓴다 |
| FR-12A | `test_FR12A_recover_cli_approve` | `approve`의 `completed` 0, `pending` 1 |
| FR-12A | `test_FR12A_recover_cli_rejected` | 거부는 종료 코드 1과 사유 코드 |
| FR-12A | `test_FR12A_recover_cli_token_file_errors` | 토큰 파일이 없거나 비면 2 |
| FR-12A | `test_FR12A_recover_cli_connection_error` | 연결 실패는 1 |
| FR-12A | `test_FR12A_recover_cli_server_from_config` | `--server`가 없으면 설정의 `[http]`로 주소를 만든다 |
