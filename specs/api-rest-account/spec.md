# 앱용 계정 REST

- 담당: eunji719 (앱)
- 요구사항: FR-01, FR-11, NFR-08
- 따르는 결정: #183

## 1. 왜

앱이 계정을 만들고 MCP 연결용 토큰을 받을 경로가 없다. 방문자는 시드 스크립트 없이 계정과 비콘을 등록할 수 없다 (FR-01).

## 2. 무엇

- 입력: `POST /accounts`의 이름. `PUT /me/beacon`의 Bearer 토큰과 비콘 식별자 `(uuid, major, minor)`
- 출력: 새 `visitor` 계정의 토큰 원문. 등록한 활성 비콘. 거부 사유 코드
- 바뀌는 상태: `AdminService`를 거쳐 저장한 계정과 비콘
- 약속:
  - `POST /accounts`는 `visitor` 계정을 만들고 토큰 원문을 이 응답에서만 한 번 보낸다 (FR-01).
  - `PUT /me/beacon`은 토큰 계정의 활성 비콘을 등록한다. 같은 계정의 다른 비콘은 비활성화한다 (FR-01).
  - 응답에 사용자 ID와 비콘 ID를 넣지 않는다 (FR-11, NFR-08).

## 3. 제약

- 경로, 본문, 성공 응답과 거부 형식은 `architecture.md` §5 REST "계정"과 "공통 규칙"을 따른다 (#183).
- 인증은 `Authorization: Bearer <토큰>`이다. 토큰이 없거나 틀리면 `401`과 `{"error": "INVALID_TOKEN"}`이다 (#183).
- API는 요청 본문의 계정 ID를 쓰지 않는다. 계정은 토큰으로만 정한다 (#183).
- 쓰기는 `AdminService.register_account()`와 `register_beacon()`으로만 한다. API는 DB를 직접 부르지 않는다 (AGENTS.md 의존 규칙 1).
- 거부 사유 코드는 `AdminWriteResult.reason`을 그대로 쓴다 (NFR-07).
- 토큰 원문은 로그와 오류에 쓰지 않는다 (NFR-08).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/rest/`
- `server/tests/api/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/runtime/`, `server/src/nearpilot/api/auth/`, `server/src/nearpilot/core/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `AdminService.register_account`, `register_beacon` | 사용 | 근접 추정 (`core/admin/`) |
| `Authenticator.authenticate` | 사용 | 판정 (`api/auth/`) |
| `build_account_router(admin, authenticator)` | 제공 | 상태 관리 (서버 조립이 FastAPI 앱에 붙인다) |

### 만들지 않는 것
- 서버 조립(`runtime/`)에 라우터를 붙이는 일과 조립의 `AdminService` 생성. 상태 관리가 맡는다.
- 승인 REST (`/approvals`). CORE의 승인 조회·결정 인터페이스가 없어서 따로 한다.
- 토큰 재발급, 계정 삭제, 비콘 조회

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `name`이 없거나, 문자열이 아니거나, 공백뿐이거나, 64자를 넘는다 | `400`과 `INVALID_INPUT`. 계정을 만들지 않는다 | `test_FR01_create_account_rejects_invalid_name` |
| 본문이 JSON 객체가 아니다 | `400`과 `INVALID_INPUT` | `test_FR01_create_account_rejects_non_object_body` |
| 계정 등록이 거부됐다 | `409`와 `AdminWriteResult.reason`. 토큰을 보내지 않는다 | `test_FR01_create_account_conflict_returns_reason` |
| `PUT /me/beacon`에 토큰이 없거나 틀리다 | `401`과 `INVALID_TOKEN`. 비콘을 등록하지 않는다 | `test_FR01_put_beacon_requires_token` |
| 계정 조회가 실패했다 | `500`과 `INTERNAL_ERROR` | `test_FR01_put_beacon_lookup_failure_is_internal_error` |
| `uuid` 형식이 틀리거나 `major`·`minor`가 0~65535 정수가 아니다 | `400`과 `INVALID_BEACON`. 비콘을 등록하지 않는다 | `test_FR01_put_beacon_rejects_invalid_beacon` |
| 다른 계정이 이미 같은 비콘을 등록했다 | `409`와 `BEACON_ALREADY_REGISTERED` | `test_FR01_put_beacon_duplicate_returns_reason` |
| 같은 계정이 같은 비콘을 다시 등록했다 | 같은 비콘 ID로 다시 등록해서 활성화한다. `200` | `test_FR01_put_beacon_same_key_reuses_beacon_id` |
| `AdminService`가 예외를 냈다 | `500`과 `INTERNAL_ERROR`. 예외 내용은 응답에 넣지 않는다 | `test_FR01_admin_exception_is_internal_error` |

## 6. 결정

- 라우터는 `build_account_router(admin, authenticator)`가 만드는 FastAPI `APIRouter`다 — 서버 조립이 `app.include_router()`로 붙인다. MCP 앱은 `/`에 마운트돼 있어서 라우터를 마운트보다 먼저 붙여야 한다.
- 계정 ID는 `user-` + 무작위 UUID hex다 — 서버가 만들고 응답에 넣지 않는다 (#183).
- 비콘 ID는 `beacon-` + SHA-256(계정 ID, uuid, major, minor)의 앞 24자다 — 같은 계정이 같은 비콘을 다시 등록하면 같은 ID가 돼서 DB가 거부하지 않고 다시 활성화한다. 다른 비콘이면 새 ID다.
- `name`은 앞뒤 공백을 지운 1~64자다 — 표시용 이름이고 계정을 구분하지 않는다.
- 본문 형식 오류는 FastAPI의 `422` 대신 `400`과 사유 코드로 반환한다 — 공통 규칙의 `{"error": ...}` 형식을 지킨다. `INVALID_BEACON`은 §5 REST가 정한 코드라 `shared`의 `ReasonCode`에 없어서 `api/rest/` 상수로 둔다.
- `AdminService` 호출은 동기 함수라 `asyncio.to_thread`로 부른다 — MCP 도구와 같다.

## 7. 가정

- 담당자가 앱용 REST를 맡는다 — `architecture.md` §2에는 미결이다. 담당자가 팀에 알렸다.
- 서버 조립이 `AdminService`를 만들어 라우터에 넘긴다 — 상태 관리가 조립에 반영한다. 그 전까지 이 경로는 실행 중인 서버에 없다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-01 | `test_FR01_create_account_issues_visitor_token` | `visitor` 계정을 만들고 `201`과 토큰만 반환한다 |
| FR-01 | `test_FR01_create_account_rejects_invalid_name` | 잘못된 이름을 `400`으로 거부한다 |
| FR-01 | `test_FR01_create_account_rejects_non_object_body` | 객체가 아닌 본문을 `400`으로 거부한다 |
| FR-01 | `test_FR01_create_account_conflict_returns_reason` | 등록 거부를 `409`와 사유로 반환한다 |
| FR-01 | `test_FR01_put_beacon_registers_active_beacon` | 토큰 계정에 활성 비콘을 등록하고 소문자 uuid로 반환한다 |
| FR-01 | `test_FR01_put_beacon_requires_token` | 토큰 없는 요청을 `401`로 거부한다 |
| FR-01 | `test_FR01_put_beacon_lookup_failure_is_internal_error` | 계정 조회 실패를 `500`으로 반환한다 |
| FR-01 | `test_FR01_put_beacon_rejects_invalid_beacon` | 잘못된 비콘을 `400`으로 거부한다 |
| FR-01 | `test_FR01_put_beacon_duplicate_returns_reason` | 다른 계정의 비콘을 `409`로 거부한다 |
| FR-01 | `test_FR01_put_beacon_same_key_reuses_beacon_id` | 같은 비콘의 재등록이 같은 비콘 ID를 쓴다 |
| FR-01 | `test_FR01_admin_exception_is_internal_error` | 서비스 예외를 `500`으로 반환한다 |
| FR-11, NFR-08 | `test_FR11_responses_hide_user_and_beacon_ids` | 응답에 계정 ID와 비콘 ID가 없다 |
| FR-01 | `test_FR01_account_rest_with_real_admin_service` (integration) | 실제 DB와 `AdminService`로 등록한 토큰이 계정 조회에 쓰인다 |
