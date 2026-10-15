# FR-09 release_use·get_use_status 도구

- 담당: eunji719 (판정)
- 요구사항: FR-09, FR-11, FR-21
- 따르는 결정: #29

## 1. 왜

- 호스트가 사물함을 열고 나면 반납과 사용 상태 조회가 필요하다. 지금은 `request_use` 도구만 있다.
- 상태 관리는 `release_use`와 `get_use_status`를 이미 제공한다. 호스트가 부를 입구가 없다.
- 두 함수의 결과를 그대로 호스트에 보내면 필드가 늘 때 내부 값이 새고, 타인의 `use_id`에 대한 답으로 존재 여부가 드러날 수 있다.

## 2. 무엇

- 입력: 도구 `release_use(use_id)`, `get_use_status(use_id)`. 계정은 `request_use`와 같이 토큰으로 정한다.
- 출력:
  - `release_use`: `verdict`, `reason`, `node_id`, `use_id`, `request_id`, `message`. `request_id`가 없으면 `null`이다.
  - `get_use_status`: `use_id`, `state`, `rental_due_at`, `overtime_sec`, `closed_at`, `reason`. 시각은 ISO 8601 문자열이다. 값이 없으면 `null`이다. 성공이면 `reason`이 `null`이다.
- 바뀌는 상태: 없음. `release_use`가 시작하는 반납은 상태 관리가 정한 대로다.
- 약속:
  - 두 도구 모두 토큰 계정과 `use_id`로 `CoreService`를 부른다. 판정과 조회 결과를 바꾸지 않는다 (NFR-07).
  - 없는 `use_id`와 타인의 `use_id`는 같은 응답이다. `release_use`는 `DENY` / `USE_NOT_FOUND`, `get_use_status`는 `reason`이 `USE_NOT_FOUND`다 (FR-21).
  - 인자가 틀리거나 서비스가 예외를 내도 같은 모양의 응답을 반환한다.

| 상황 | `release_use` | `get_use_status` |
|---|---|---|
| 본인 `use_id` | 서비스의 판정 | 상태 값, `reason`은 `null` |
| 없는 `use_id`, 타인의 `use_id` | `DENY` / `USE_NOT_FOUND` | `reason`은 `USE_NOT_FOUND`, 나머지는 `null` |
| `use_id`가 비었다 | `DENY` / `INVALID_INPUT`. 서비스를 부르지 않는다 | `reason`은 `INVALID_INPUT`. 서비스를 부르지 않는다 |
| 서비스가 예외를 낸다 | `DENY` / `INTERNAL_ERROR` | `reason`은 `INTERNAL_ERROR` |

## 3. 제약

- 응답에는 허용 목록의 필드만 담는다. 사용자 ID, 점유 주체, 내부 요청 ID를 담지 않는다 (FR-11, NFR-08).
- 계정은 토큰으로만 정한다. 인자로 받은 값을 계정으로 쓰지 않는다 (FR-08).
- 없는 핸들과 타인의 핸들을 구분하지 않는다 (FR-21). 응답 모양과 문구가 같다.
- 동기 서비스는 이벤트 루프를 막지 않도록 스레드에서 실행한다.
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

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
| `CoreService.release_use` | 사용 | 상태 관리 |
| `CoreService.get_use_status` | 사용 | 상태 관리 |
| 도구 `release_use`, `get_use_status` | 제공 | AI 호스트 |

### 만들지 않는 것
- 나머지 도구 3개 (`find_available_devices`, `get_device_state`, `request_access`)
- 소유권 검사와 반납 판단. 상태 관리가 한다 (FR-21).
- 서버 진입점과 조립 (#88)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 도구 목록을 조회한다 | `release_use`와 `get_use_status`가 있다 | `test_FR09_도구_목록에_release_use와_get_use_status가_있다` |
| `release_use`를 호출한다 | 토큰 계정과 `use_id`로 서비스를 부르고 판정 응답을 반환한다 | `test_FR09_release_use는_토큰_계정과_use_id로_서비스를_부른다` |
| `get_use_status`를 호출한다 | 토큰 계정과 `use_id`로 서비스를 부르고 허용 목록의 값을 반환한다 | `test_FR09_get_use_status는_토큰_계정과_use_id로_서비스를_부른다` |
| 시각 값이 있다 | ISO 8601 문자열로 반환한다 | `test_FR09_get_use_status는_시각을_ISO_문자열로_반환한다` |
| 없는 `use_id`다 | 2절 표의 응답이다 | `test_FR21_없는_use_id는_USE_NOT_FOUND` |
| 타인의 `use_id`다 | 없는 `use_id`와 같은 응답이다 | `test_FR21_타인의_use_id는_없는_use_id와_같은_응답` |
| `use_id`가 비었다 | `INVALID_INPUT`. 서비스를 부르지 않는다 | `test_FR09_use_id가_비면_INVALID_INPUT이고_서비스를_부르지_않는다` |
| 서비스가 예외를 낸다 | `INTERNAL_ERROR`. 예외 내용이 없다 | `test_FR09_서비스_예외는_INTERNAL_ERROR` |
| 서비스 결과에 허용 목록 밖의 값이 있다 | 응답에 나가지 않는다 | `test_FR11_허용_목록_밖의_값은_응답에_없다` |
| 인자의 사용자 ID | 계정으로 쓰지 않는다 | `test_FR09_인자의_사용자_ID는_계정으로_쓰지_않는다` |

## 6. 결정

- 새 도구는 `api/mcp/server.py`의 `build_app`에 같은 방식으로 등록한다. 계정 확인(`scope`의 계정)과 스레드 실행은 도구 세 개가 공통 함수를 쓴다 — 도구마다 인증 확인이 달라지지 않게 하려는 것이다.
- `get_use_status`의 응답 모양은 성공과 실패가 같다 — 호스트가 모양으로 존재 여부를 알 수 없게 하려는 것이다(FR-21). 변환은 `api/mcp/response.py`의 함수 하나가 한다.
- `host_response`는 `request_id`가 빈 문자열이면 `null`로 낸다. `release_use`의 판정에는 요청 ID가 없다.

## 7. 가정

- `get_use_status`가 반환하는 `state`는 점유 세션 상태(`RESERVED`, `ACTIVE`, `CLOSING`, `CLOSED`)의 문자열 값이다. 호스트에 보여 줘도 되는 값인지는 requirements FR-09에 따른다(본인의 점유 세션과 종료 이력).
- `overtime_sec`는 초 단위 숫자다. 서비스가 계산한 값을 그대로 낸다.
- 미결: FR-09의 '요청 핸들'이 `use_id`와 다른 값인지 (`api-mcp` spec 7절과 같다).

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | 5절의 `test_FR09_*` | 두 도구가 토큰 계정으로 서비스를 부르고 응답을 반환한다 |
| FR-21 | `test_FR21_*` | 없는 핸들과 타인의 핸들을 구분하지 않는다 |
| FR-11 | `test_FR11_허용_목록_밖의_값은_응답에_없다` | 내부 값이 나가지 않는다 |

- 구현은 `server/tests/api/test_mcp_release.py`다.
- HTTP 요청은 ASGI 앱에 직접 보낸다. 서비스와 계정 조회는 fake로 대신한다.
