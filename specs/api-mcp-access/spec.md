# FR-09 request_access 도구

- 담당: eunji719 (판정)
- 요구사항: FR-08, FR-09, FR-11
- 따르는 결정: #25

## 1. 왜

- `CoreService.request_access`는 `AccessRequestService`로 구현했지만 호스트가 부를 입구가 없다. 제한 기기의 방문자가 MCP로 승인을 요청할 수 없다.
- 서비스의 `Decision`을 그대로 호스트에 보내면 필드가 늘 때 내부 값이 샌다. 승인 요청이 실행 허가로 오해될 수 있다.

## 2. 무엇

- 입력: 도구 `request_access(node_id, action, request_id)`. 계정은 `request_use`와 같이 토큰으로 정한다.
- 출력: `verdict`, `reason`, `node_id`, `use_id`, `request_id`, `message`. `use_id`는 항상 `null`이다. 값이 없는 필드는 `null`이다.
- 바뀌는 상태: 없음. 승인 신청의 저장은 서비스가 정한 대로다.
- 약속:
  - 토큰 계정과 인자로 `CoreService.request_access`를 부르고, 서비스의 판정을 허용 목록의 필드로만 반환한다. 판정을 바꾸지 않는다 (NFR-07).
  - 승인 요청은 실행 허가가 아니다. 응답에 `use_id`가 없다.
  - 인자가 틀리거나 서비스가 예외를 내도 같은 모양의 응답을 반환한다.

| 상황 | 응답 |
|---|---|
| 서비스가 판정을 반환한다 | 판정의 `verdict`, `reason`, `node_id`, `request_id`, `message` |
| `node_id`나 `request_id`가 비었다 | `DENY` / `INVALID_INPUT`. 서비스를 부르지 않는다 |
| `action`이 `open`, `close`, `on`, `off`가 아니다 | `DENY` / `INVALID_INPUT`. 서비스를 부르지 않는다 |
| 서비스가 예외를 낸다 | `DENY` / `INTERNAL_ERROR`. 예외 내용이 없다 |

## 3. 제약

- 응답에는 허용 목록의 필드만 담는다. 사용자 ID, 승인 ID, 정책 정보, 내부 요청 ID를 담지 않는다 (FR-11, NFR-08).
- 계정은 토큰으로만 정한다. 인자로 받은 값을 계정으로 쓰지 않는다 (FR-08).
- 동기 서비스는 이벤트 루프를 막지 않도록 스레드에서 실행한다.
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/mcp/`
- `server/tests/api/`
- `specs/api-mcp-access/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `db/`, `iot/`, `api/auth/`, `api/rest/`, `api/ws/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `CoreService.request_access` | 사용 | 판정 (`core-access-request`) |
| 도구 `request_access` | 제공 | AI 호스트 |

### 만들지 않는 것
- 승인 요청 목록 조회와 소유자의 승인·거부 (REST, 앱)
- 소유자 앱 알림 (FR-03, 전달 수단 미결)
- 서버 진입점과 조립 (#88)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 도구 목록을 조회한다 | `request_access`가 있다. 도구 6개가 모두 있다 | `test_FR09_도구_목록에_request_access가_있다` |
| `request_access`를 호출한다 | 토큰 계정, `node_id`, 동작, `request_id`로 서비스를 부르고 판정을 반환한다 | `test_FR09_request_access는_토큰_계정과_인자로_서비스를_부른다` |
| 승인 대기 판정이다 | `NEED_APPROVAL` / `APPROVAL_PENDING`, `use_id`는 `null` | `test_FR09_승인_대기_판정을_반환한다` |
| 서비스가 `use_id`가 있는 판정을 반환한다 | 응답의 `use_id`는 `null`이다 | `test_FR09_승인_요청_응답에_use_id가_없다` |
| `node_id`나 `request_id`가 비었다 | `INVALID_INPUT`. 서비스를 부르지 않는다 | `test_FR09_인자가_비면_INVALID_INPUT이고_서비스를_부르지_않는다` |
| `action`이 잘못됐다 | `INVALID_INPUT`. 서비스를 부르지 않는다 | `test_FR09_action이_잘못되면_INVALID_INPUT` |
| 서비스가 예외를 낸다 | `INTERNAL_ERROR`. 예외 내용이 없다 | `test_FR09_서비스_예외는_INTERNAL_ERROR` |
| 서비스 결과에 허용 목록 밖의 값이 있다 | 응답에 나가지 않는다 | `test_FR11_허용_목록_밖의_값은_응답에_없다` |
| 인자의 사용자 ID | 계정으로 쓰지 않는다 | `test_FR08_인자의_사용자_ID는_계정으로_쓰지_않는다` |
| 토큰이 없거나 틀렸다 | 서비스를 부르지 않는다 | `test_FR08_토큰이_없으면_서비스를_부르지_않는다` |

## 6. 결정

- 도구는 `api/mcp/server.py`의 `build_app`에 같은 방식으로 등록한다. 응답 변환은 `host_response`를 쓴다. 담당자가 정했다 — 다른 도구와 응답 모양을 같게 해서 호스트가 한 가지 모양만 다루게 한다.
- 응답의 `use_id`는 API가 `null`로 고정한다. 담당자가 정했다 — 서비스가 값을 담아도 승인 요청이 실행 허가로 보이지 않게 한다.

## 7. 가정

- `request_id`가 빈 문자열이면 서비스를 부르지 않고 `INVALID_INPUT`으로 거부한다 — `request_use`와 같다.
- `node_id`의 형식 검사는 서비스가 한다 (`nearest:`·`any:` 거부 포함). API는 빈 값만 거른다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | 5절의 `test_FR09_*` | 도구가 토큰 계정으로 서비스를 부르고 응답을 반환한다 |
| FR-08 | `test_FR08_*` | 계정은 토큰으로만 정한다 |
| FR-11 | `test_FR11_허용_목록_밖의_값은_응답에_없다` | 내부 값이 나가지 않는다 |

- 구현은 `server/tests/api/test_mcp_access.py`다.
- HTTP 요청은 ASGI 앱에 직접 보낸다. 서비스와 계정 조회는 fake로 대신한다. `FakeCore`에 `request_access`를 추가한다 (`tests/api/_mcp_client.py`).
