# 앱용 승인 REST

- 담당: eunji719 (앱)
- 요구사항: FR-03, FR-14, NFR-07, NFR-08
- 따르는 결정: #183, #186

## 1. 왜

앱의 승인 화면과 알림이 부를 서버 경로가 없다. CORE의 `ApprovalService`는 있지만 HTTP로 열려 있지 않아서, 소유자는 대기 승인을 보거나 결정할 수 없다 (FR-03).

## 2. 무엇

- 입력: Bearer 토큰. 승인 ID. `POST /approvals/{approval_id}/decision`의 결정(`approved` 또는 `denied`)
- 출력: `GET /approvals`의 대기 승인 목록, `GET /approvals/{approval_id}`와 결정의 승인 한 건, 거부 사유 코드
- 바뀌는 상태: 결정이 `ApprovalService.decide`로 승인 상태와 `valid_until`을 반영한다. 조회는 상태를 바꾸지 않는다
- 약속:
  - 세 경로는 토큰으로 계정을 식별해 `ApprovalService`의 `pending_for`, `get`, `decide`에 하나씩 넘긴다 (#186).
  - CORE가 반환한 사유를 바꾸지 않고 상태 코드와 `{"error": "<사유 코드>"}`로 옮긴다 (NFR-07).
  - 같은 결정의 재전송은 `200`과 기존 결과다 (FR-03).
  - 응답에 요청자 정보와 사용자 ID를 넣지 않는다 (FR-11, NFR-08).

## 3. 제약

- 경로, 본문, 성공 응답과 거부 형식은 `architecture.md` §5 REST "승인"과 "공통 규칙"을 따른다 (#183).
- 인증은 `Authorization: Bearer <토큰>`이다. 토큰이 없거나 틀리면 `401`과 `{"error": "INVALID_TOKEN"}`이다 (#183).
- API는 DB를 직접 부르지 않는다. 계정 식별만 `Authenticator`로 한다 (AGENTS.md 의존 규칙 1).
- 승인 권한, 대기 상태, 정책 버전은 CORE가 검증한다. API는 판정하지 않는다 (FR-03, NFR-07).
- 토큰 원문은 로그와 오류에 쓰지 않는다 (NFR-08).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/rest/`
- `server/tests/api/`
- `server/tests/integration/` (실제 DB 시나리오 테스트 하나)

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/runtime/`, `server/src/nearpilot/api/auth/`, `server/src/nearpilot/core/`, `app/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `ApprovalService.pending_for`, `get`, `decide` | 사용 | CORE (`core/approval/`) |
| `Authenticator.authenticate` | 사용 | 판정 (`api/auth/`) |
| `build_approval_router(approvals, authenticator)` | 제공 | 상태 관리 (서버 조립이 FastAPI 앱에 붙인다) |
| `GET /approvals`, `GET /approvals/{approval_id}`, `POST /approvals/{approval_id}/decision` | 제공 | 앱 |

### 만들지 않는 것
- 서버 조립(`runtime/`)에 라우터를 붙이는 일과 조립의 `OwnerApprovalService` 생성. 상태 관리가 맡는다.
- 앱의 목록 응답 읽기 정리 (`app-owner-approval` spec 7절 가정). 앱 fix에서 한다.
- 승인 알림 전달 (FR-03 미결)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 토큰이 없거나 틀리다 | `401`과 `INVALID_TOKEN`. 서비스를 부르지 않는다 | `test_FR03_requires_token` |
| 계정 조회가 실패했다 | `500`과 `INTERNAL_ERROR` | `test_FR03_lookup_failure_is_internal_error` |
| 승인 권한이 없는 계정이 목록을 조회했다 | `200`과 빈 배열 | `test_FR03_list_returns_pending_approvals` |
| 없는 승인 | `404`와 `NOT_FOUND` | `test_FR03_reasons_map_to_status` |
| 권한이 없는 승인 | `403`과 `NOT_AUTHORIZED` | `test_FR03_reasons_map_to_status` |
| 이미 결정, 정책 버전 불일치, 대기 만료 | `409`와 CORE의 사유 | `test_FR03_reasons_map_to_status` |
| 같은 결정의 재전송 | `200`과 기존 승인 | `test_FR03_decide_same_twice_returns_200` |
| 본문이 JSON 객체가 아니거나 `decision`이 `approved`·`denied`가 아니다 | `400`과 `INVALID_INPUT`. 서비스를 부르지 않는다 | `test_FR03_decide_rejects_invalid_body` |
| 서비스가 예외를 냈다 | `500`과 `INTERNAL_ERROR`. 예외 내용은 응답에 넣지 않는다 | `test_FR03_service_error_is_internal_error` |

## 6. 결정

- 라우터는 `build_approval_router(approvals, authenticator)`가 만드는 FastAPI `APIRouter`다 — 계정 라우터(`build_account_router`)와 같은 방식이다. 서버 조립이 MCP 마운트보다 먼저 붙인다.
- `GET /approvals`는 JSON 배열로 반환한다 — §5 REST는 "승인 요청 목록"으로만 정했다. 앱은 배열과 `{"approvals": [...]}`를 모두 읽는다.
- 승인 한 건의 키는 `approval_id`, `node_id`, `action`, `status`, `created_at`, `pending_expires_at`, `valid_until`이다. `pending_expires_at`은 `OwnerApproval.pending_until`이다. 시각은 시간대를 포함한 ISO 8601 문자열이고, 없으면 `null`이다.
- 사유와 상태 코드: `OK`·`ALREADY_APPLIED`는 `200`, `NOT_FOUND`는 `404`, `NOT_AUTHORIZED`는 `403`, `ALREADY_DECIDED`·`POLICY_VERSION_MISMATCH`·`REQUEST_EXPIRED`는 `409`다 (§5 REST "승인").
- 본문 형식 오류는 FastAPI의 `422` 대신 `400`과 `INVALID_INPUT`이다 — 계정 REST와 같다.
- 서비스 호출은 동기 함수라 `asyncio.to_thread`로 부른다 — 계정 REST, MCP 도구와 같다.

## 7. 가정

- 서버 조립이 `OwnerApprovalService`를 만들어 라우터에 넘긴다 — 상태 관리가 조립에 반영한다. 그 전까지 이 경로는 실행 중인 서버에 없다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-03 | `test_FR03_list_returns_pending_approvals` | 토큰 계정으로 `pending_for`를 부르고 배열을 반환한다 |
| FR-03 | `test_FR03_get_returns_approval` | 승인 한 건의 키와 시각 형식 |
| FR-03 | `test_FR03_decide_passes_choice` | 결정을 `ApprovalService.decide`에 넘기고 승인 한 건을 반환한다 |
| FR-03 | `test_FR03_decide_same_twice_returns_200` | `ALREADY_APPLIED`는 `200`이다 |
| FR-03, NFR-07 | `test_FR03_reasons_map_to_status` | 사유마다 상태 코드와 사유를 그대로 옮긴다 |
| FR-03 | `test_FR03_requires_token` | 토큰이 없으면 `401`이다 |
| FR-03 | `test_FR03_lookup_failure_is_internal_error` | 계정 조회 실패는 `500`이다 |
| FR-03 | `test_FR03_decide_rejects_invalid_body` | 잘못된 본문은 `400`이다 |
| FR-03 | `test_FR03_service_error_is_internal_error` | 서비스 예외는 `500`이다 |
| FR-14, NFR-08 | `test_NFR08_responses_hide_requester` | 응답에 요청자와 사용자 ID가 없다 |
| FR-03 | `test_FR03_approval_rest_with_real_db` (integration) | 실제 DB와 `OwnerApprovalService`로 목록, 승인, 재전송, 권한 없음 |
