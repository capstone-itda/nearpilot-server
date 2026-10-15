# FR-08·FR-09 MCP 엔드포인트와 request_use 도구

- 상태: 구현 중
- 담당: eunji719 (판정)
- 요구사항: FR-08, FR-09, FR-10, FR-11
- 따르는 결정: #29

## 1. 왜

- 판정과 실행 인계는 있지만 호스트가 부를 입구가 없다. AI 호스트는 `/mcp`의 도구로만 서버를 부른다.
- 입구가 토큰을 확인하지 않으면 토큰이 없거나 틀린 호출이 판정까지 들어간다.
- 판정 결과를 그대로 호스트에 보내면 내부 값(근접 확률, 내부 요청 ID)이 새고, 필드가 늘 때 사용자 ID가 새어 나갈 수 있다.

## 2. 무엇

- 입력: `/mcp`로 오는 MCP Streamable HTTP 요청과 `Authorization: Bearer <토큰>` 헤더. 도구 `request_use`의 인자는 `target_spec`, `action`, `request_id`다.
- 출력: `request_use`의 호스트 응답. `verdict`, `reason`, `node_id`, `use_id`, `request_id`, `message`만 담는다. 값이 없으면 `null`이다.
- 바뀌는 상태: 없음. 판정과 실행이 바꾸는 상태는 각 영역이 정한 대로다.
- 약속:
  - 토큰이 없거나 무효하면 MCP 요청을 처리하지 않고 거부한다. `initialize`와 도구 목록도 같다.
  - 계정은 토큰으로만 정한다. 인자로 받은 값을 계정으로 쓰지 않는다.
  - `request_use`는 `RequestUseService`의 판정을 호스트 응답으로 바꿔서 반환한다.
  - 도구 인자 오류, 서버 오류도 `DENY` 응답으로 반환한다. 예외 내용은 응답에 넣지 않는다.
  - 호스트 종류와 상관없이 같은 요청에는 같은 응답을 반환한다 (FR-10).

| 상황 | 응답 |
|---|---|
| 토큰이 없다, 형식이 틀리다, 맞는 계정이 없다 | HTTP 401, `WWW-Authenticate: Bearer`, 본문 `{"error": "INVALID_TOKEN"}` |
| 계정 조회가 실패했다 | HTTP 500, 본문 `{"error": "INTERNAL_ERROR"}` |
| `target_spec`, `action`, `request_id` 형식이 틀리다 | 도구 응답 `DENY` / `INVALID_INPUT` |
| 서비스가 예외를 낸다 | 도구 응답 `DENY` / `INTERNAL_ERROR` |
| 정상 | 판정 응답 (`verdict`, `reason`, ...) |

## 3. 제약

- 응답에는 허용 목록의 필드만 담는다. 사용자 ID, 점유 주체, 내부 요청 ID, 근접 확률(`p_max`)을 담지 않는다 (FR-11, NFR-08). `Decision`에 필드가 늘어도 자동으로 나가지 않는다.
- 토큰 원문을 로그와 응답에 쓰지 않는다 (NFR-08). 검증은 `Authenticator`를 쓴다.
- 판정을 바꾸지 않는다. 응답 변환만 한다 (NFR-07).
- 시간은 `received_at`에 서버가 넣는다. 인자로 받지 않는다. 시계는 주입한다 (NFR-09).
- 동기 서비스는 이벤트 루프를 막지 않도록 스레드에서 실행한다 (`shared/interfaces.py` 머리말).
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4). api는 db를 직접 호출하지 않는다. 계정 식별은 `Authenticator`를 거친다 (의존 규칙 1).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/mcp/`
- `server/tests/api/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `db/`, `iot/`, `api/auth/`, `api/rest/`, `api/ws/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `CoreService.request_use` | 사용 | CORE (판정) |
| `Authenticator.authenticate` | 사용 | API (`api/auth`) |
| `/mcp` Streamable HTTP, 도구 `request_use` | 제공 | AI 호스트 |

### 만들지 않는 것
- 나머지 도구 5개 (`find_available_devices`, `get_device_state`, `request_access`, `release_use`, `get_use_status`). 후속 spec에서 같은 응답 변환과 인증 위에 얹는다.
- 서버 진입점, `CoreService`와 서비스 조립, 서버 실행 (#88)
- HTTPS 종단. 배포 환경이 맡는다 (NFR-08).
- 인증 실패의 감사 기록 (`api-auth` spec 7절의 미결)
- `any:locker` 전역 할당 (FR-23)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 토큰이 없다 | 401. MCP를 처리하지 않는다 (`initialize` 포함) | `test_FR08_토큰이_없으면_401이고_MCP를_처리하지_않는다` |
| 토큰이 무효하다 | 401 `INVALID_TOKEN` | `test_FR08_무효한_토큰은_401` |
| 계정 조회가 실패했다 | 500 `INTERNAL_ERROR` | `test_FR08_계정_조회_실패는_500` |
| 유효한 토큰으로 도구 목록을 조회한다 | `request_use`가 있다 | `test_FR09_도구_목록에_request_use가_있다` |
| `request_use`를 호출한다 | 토큰 계정과 인자로 `UseRequest`를 만들어 서비스에 넘긴다 | `test_FR09_request_use는_토큰_계정으로_요청을_만든다` |
| 인자로 다른 사용자 ID를 넘긴다 | 계정으로 쓰지 않는다 | `test_FR09_인자의_사용자_ID는_계정으로_쓰지_않는다` |
| `target_spec`, `action`, `request_id`가 틀리다 | `DENY` / `INVALID_INPUT`. 서비스를 부르지 않는다 | `test_FR09_인자가_틀리면_INVALID_INPUT이고_서비스를_부르지_않는다` |
| 서비스가 예외를 낸다 | `DENY` / `INTERNAL_ERROR`. 예외 내용이 없다 | `test_FR09_서비스_예외는_INTERNAL_ERROR` |
| 판정에 내부 값이 있다 | 응답에 사용자 ID, 내부 요청 ID, `p_max`가 없다 | `test_FR11_응답에_내부_값이_없다` |
| 같은 요청을 다시 보낸다 | 같은 `request_id`로 서비스를 부른다 | `test_FR09_재전송은_같은_request_id로_서비스를_부른다` |
| 서로 다른 토큰이 같은 `request_id`를 보낸다 | 각자의 계정으로 서비스를 부른다 | `test_FR09_다른_계정의_같은_request_id는_각자의_계정으로_전달한다` |

## 6. 결정

- 토큰 검증은 ASGI 미들웨어(`api/mcp/middleware.py`)가 `/mcp` 앞에서 모든 HTTP 요청에 한다 — FR-08이 토큰이 없거나 무효한 호출을 거부하라고 하므로 `initialize`와 도구 목록도 막는다. 담당자가 정했다. 검증한 계정은 요청 `scope`의 `nearpilot.account`에 담고, 도구가 이 값으로 계정을 정한다.
- 전송은 stateless + JSON 응답이다(`stateless_http=True`, `json_response=True`) — 요청마다 토큰으로 계정을 정하므로 서버가 세션을 유지할 필요가 없고 재시작에 안전하다. 담당자가 정했다.
- 호스트 응답은 허용 목록의 필드만 내보낸다(`verdict`, `reason`, `node_id`, `use_id`, `request_id`, `message`). 담당자가 정했다. 변환은 `api/mcp/response.py`의 함수 하나가 한다.
- 구조: `api/mcp/server.py`의 `build_app(core, authenticator, clock)`이 도구를 등록한 MCP 앱에 미들웨어를 씌워서 ASGI 앱을 반환한다. 서비스는 스레드에서 호출한다(`asyncio.to_thread`).
- 도구는 `target_spec`(문자열), `action`(문자열), `request_id`(문자열)를 받는다. `TargetSpec.parse`, `Action`으로 해석하고 실패하면 `INVALID_INPUT`이다. `request_id`는 비어 있으면 안 된다.

## 7. 가정

- `host_kind`는 `None`으로 넘긴다. 판정은 `host_kind`를 입력 비교에 쓰지 않는다 (`core-decision-pipeline` 6절).
- `parent_internal_request_id`는 `None`으로 넘긴다. 재질문·승인 뒤 재요청을 원래 요청과 연결하는 방법이 FR-09에 인자로 정해져 있지 않다. 미결: 도구가 연결 정보를 받는지, 서버가 계정의 직전 요청으로 추정하는지 팀이 정해야 한다.
- 미결: FR-09의 '본인의 요청 핸들'이 `use_id`와 다른 값인지. 이 작업은 `use_id`와 호스트가 보낸 `request_id`를 돌려준다.
- 요청 크기와 속도 제한은 MCP SDK의 기본값을 쓴다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-08 | `test_FR08_*` | 토큰이 없거나 무효하면 MCP를 처리하지 않는다 |
| FR-09 | `test_FR09_*` | `request_use`가 토큰 계정과 인자로 서비스를 부르고 판정 응답을 반환한다 |
| FR-10 | `test_FR10_호스트_이름과_상관없이_같은_응답`  | 호스트 종류와 상관없이 같은 응답이다 |
| FR-11 | `test_FR11_응답에_내부_값이_없다` | 사용자 ID, 내부 요청 ID, `p_max`가 없다 |

- 구현은 `server/tests/api/test_mcp.py`다.
- HTTP 요청은 ASGI 앱에 직접 보낸다. 서비스와 계정 조회는 fake로 대신한다.
