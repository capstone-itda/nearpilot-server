# FR-08 토큰으로 계정 식별

- 담당: eunji719 (판정)
- 요구사항: FR-08, NFR-08
- 따르는 결정: #29

## 1. 왜

- MCP 도구는 모두 토큰으로 식별한 계정으로 동작한다. 판정, 조회, 반납의 `user_id`가 여기서 나온다.
- 토큰을 확인하는 곳이 없으면 호스트가 보낸 값을 그대로 믿거나, 토큰이 없거나 틀린 호출이 도구까지 들어간다.
- 토큰 원문이 로그나 오류에 남으면 다른 사람이 같은 계정으로 호출할 수 있다.

## 2. 무엇

- 입력: 호스트가 보낸 `Authorization` 헤더 값 (없을 수 있다)
- 출력: 계정 (`Account`: `user_id`, `role`)
- 바뀌는 상태: 없음
- 약속:
  - 헤더가 `Bearer <토큰>` 형식이고 해시가 저장된 해시와 맞으면 그 계정을 반환한다.
  - 헤더가 없거나, 형식이 틀리거나, 맞는 계정이 없으면 `INVALID_TOKEN`으로 거부한다. 세 경우를 호출자가 구분할 수 없다.
  - 계정 조회가 실패하면 `INVALID_TOKEN`이 아니라 `INTERNAL_ERROR`로 거부한다. 계정 조회 실패는 기본값 거부다.
  - 거부 오류에는 토큰 원문과 계정 정보를 넣지 않는다 (NFR-08).
  - 같은 헤더 값에는 같은 결과를 반환한다 (FR-10, NFR-09).

| 상황 | 결과 |
|---|---|
| `Authorization: Bearer <유효한 토큰>` | `Account` |
| 헤더 없음, 빈 값 | `INVALID_TOKEN` |
| `Bearer`가 아닌 방식, 토큰이 비었거나 공백을 포함한다 | `INVALID_TOKEN` (계정 조회를 하지 않는다) |
| 맞는 해시가 없다 | `INVALID_TOKEN` |
| 계정 조회가 예외를 낸다 | `INTERNAL_ERROR` |

## 3. 제약

- 토큰은 서버가 만든 32바이트 무작위 값이고 서버는 SHA-256 해시만 저장한다. 해시는 `shared/tokens.py`의 `hash_token`을 쓴다. 다른 해시 함수를 두지 않는다 (FR-08, NFR-08).
- 계정은 `AccountLookup.account_by_token_hash()`로만 찾는다. api가 db를 직접 부르는 유일한 경로다 (`AGENTS.md` 의존 규칙 1).
- 토큰을 정규화하지 않는다. 공백을 지우거나 대소문자를 바꾸지 않는다 (`hash_token`).
- 토큰 원문을 로그, 오류 메시지, 반환 값에 쓰지 않는다 (NFR-08).
- 서버가 `user_id`를 호스트에 돌려주지 않는다. 이 작업은 `Account`를 api 안으로만 반환한다 (FR-11).
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/auth/`
- `server/tests/api/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `db/`, `iot/`, `api/mcp/`, `api/rest/`, `api/ws/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `AccountLookup.account_by_token_hash` | 사용 | DB |
| `hash_token` (`shared/tokens.py`) | 사용 | 공동 |
| 인증 함수 (`Authenticator.authenticate`) | 제공 | API (`api/mcp`, `api/rest`) |

### 만들지 않는 것
- FastAPI 의존성과 HTTP 401 응답 변환. 엔드포인트에 연결하는 일은 `api-mcp` spec에서 한다.
- 토큰 발급과 계정 등록 (FR-01, 관리 API)
- 인증 실패의 감사 기록. 7절을 따른다.
- 역할(`owner`/`visitor`)에 따른 인가. 이 작업은 계정만 식별한다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 헤더가 없다 | `INVALID_TOKEN`. 계정 조회를 하지 않는다 | `test_FR08_헤더가_없으면_INVALID_TOKEN이고_조회하지_않는다` |
| `Bearer` 대신 다른 방식이다 | `INVALID_TOKEN`. 계정 조회를 하지 않는다 | `test_FR08_Bearer가_아니면_INVALID_TOKEN이고_조회하지_않는다` |
| 토큰이 비었거나 공백을 포함한다 | `INVALID_TOKEN`. 계정 조회를 하지 않는다 | `test_FR08_토큰이_비었거나_공백을_포함하면_INVALID_TOKEN` |
| 체계 이름의 대소문자가 다르다 (`bearer`) | 유효한 토큰이면 계정을 반환한다 | `test_FR08_Bearer_체계_이름은_대소문자를_구분하지_않는다` |
| 맞는 해시가 없다 | `INVALID_TOKEN` | `test_FR08_등록되지_않은_토큰은_INVALID_TOKEN` |
| 유효한 토큰이다 | `Account`를 반환한다. 조회에는 `hash_token(토큰)`을 넘긴다 | `test_FR08_유효한_토큰은_해시로_조회해서_계정을_반환한다` |
| 계정 조회가 예외를 낸다 | `INTERNAL_ERROR`. 예외 내용을 오류에 넣지 않는다 | `test_FR08_계정_조회가_실패하면_INTERNAL_ERROR` |
| 거부 오류를 문자열로 만든다 | 토큰 원문이 없다 | `test_NFR08_거부_오류에_토큰_원문이_없다` |
| 같은 헤더를 반복해서 보낸다 | 같은 결과를 반환한다 | `test_FR08_같은_헤더에는_같은_결과를_반환한다` |
| 토큰을 정규화하지 않는다 | 앞뒤 공백이나 대소문자가 다른 토큰은 다른 값이다 | `test_FR08_토큰을_정규화하지_않는다` |

## 6. 결정

- 토큰 형식은 `Authorization: Bearer <토큰>` 하나다 — MCP와 OAuth의 표준 형식이고 세 호스트가 공통으로 지원한다. 다른 형식(`X-Api-Key` 등)은 받지 않는다. 담당자가 정했다.
  - 체계 이름 `Bearer`는 대소문자를 구분하지 않는다 (RFC 7235). 체계 이름과 토큰 사이는 공백 하나다.
  - 토큰은 공백 뒤의 나머지 전부다. 비어 있거나 공백 문자를 포함하면 형식 오류다. 서버가 발급하는 토큰(`secrets.token_urlsafe`)에는 공백이 없다.
- 범위는 프레임워크 무관 인증 클래스까지다 — 엔드포인트 구조(MCP SDK 조립 방식)는 `api-mcp`에서 정한다. 담당자가 정했다.
- 구조: `api/auth/authenticator.py`의 `Authenticator` 클래스 하나가 `authenticate(authorization: str | None) -> Account`를 제공한다. `AccountLookup`은 생성자로 받는다. 거부는 `AuthenticationError` 예외로 알린다. 예외는 `reason`(`ReasonCode`)만 갖는다.

## 7. 가정

- 계정 조회 예외는 `AuthenticationError(INTERNAL_ERROR)`로 바꾼다. 조회 장애를 토큰 오류로 보여 주면 호스트가 토큰을 다시 발급하려 하기 때문이다.
- 헤더 값의 앞뒤 공백 제거는 HTTP 서버가 한다. 이 작업은 받은 값을 그대로 해석한다.
- 미결: 인증 실패의 감사 기록. 감사 테이블의 `auth` 단계는 있지만(`Step.AUTH`) api는 db를 직접 호출하지 않는다 (의존 규칙 1). 기록을 누가 어떤 접점으로 하는지 팀이 정해야 한다. 정해질 때까지 이 작업은 기록하지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-08 | 5절의 `test_FR08_*` | 유효한 토큰만 계정을 반환하고, 그 밖은 `INVALID_TOKEN` 또는 `INTERNAL_ERROR`로 거부한다 |
| NFR-08 | `test_NFR08_거부_오류에_토큰_원문이_없다` | 오류에 토큰 원문이 없다 |

- 구현은 `server/tests/api/test_auth.py`다.
- 다른 계층의 구현 없이 fake만으로 확인한다.
