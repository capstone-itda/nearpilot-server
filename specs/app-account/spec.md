# 앱 계정 등록과 서버 연결

- 담당: eunji719 (앱)
- 요구사항: FR-01, NFR-08, NFR-12
- 따르는 결정: #183, 2026-10-26 팀 결정 (독립 AP 안 HTTP 허용)

## 1. 왜

앱은 비콘을 송출하지만 서버에 계정과 비콘을 등록하지 못한다. 방문자는 AI 호스트에 넣을 토큰을 받을 수 없고, 비콘 값을 시드 파일로 따로 옮겨야 한다 (FR-01).

## 2. 무엇

- 입력: 서버 주소. 방문자 이름. 이미 받은 토큰(소유자는 시드 스크립트가 출력한 토큰). 비콘 탭의 비콘 식별자
- 출력: `POST /accounts`와 `PUT /me/beacon` 호출. 화면에 보여 주는 토큰, AI 호스트 연결 정보(MCP 주소와 토큰), 비콘의 서버 등록 상태, 실패 사유와 할 일
- 바뀌는 상태: 폰에 저장한 서버 주소, 토큰, 서버에 마지막으로 등록한 비콘 식별자
- 약속:
  - 방문자가 이름을 넣고 계정을 만들면 앱은 토큰을 저장하고, 지금 송출하는 비콘을 바로 서버에 등록한다 (FR-01).
  - 앱은 서버에 등록한 비콘과 지금 송출하는 비콘이 다르면 "서버 등록 필요"로 표시하고 다시 등록할 수 있게 한다 (FR-01).
  - 앱은 서버 거부 사유를 그대로 보여 주지 않고, 사유마다 사용자가 할 일을 안내한다.
  - 앱은 토큰을 기본으로 가려서 보여 준다. 사용자가 누를 때만 보이거나 복사한다 (NFR-08).

## 3. 제약

- 경로, 본문, 응답과 거부 형식은 `architecture.md` §5 REST "계정"과 "공통 규칙"을 따른다 (#183).
- 인증은 `Authorization: Bearer <토큰>`이다 (#183).
- 토큰 원문은 `POST /accounts` 응답에서만 한 번 온다. 앱이 잃어버리면 다시 받을 수 없다 (#183).
- 앱은 인터넷 없이 독립 AP 안의 서버 주소로만 통신한다 (NFR-12).
- 앱은 사용자 ID와 비콘 ID를 받지도 보여 주지도 않는다 (FR-11, NFR-08).

## 4. 경계

### 고치는 곳
- `app/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `POST /accounts`, `PUT /me/beacon` (§5 REST "계정") | 사용 | API (`api/rest/`) |
| MCP 주소 `/mcp`와 Bearer 토큰 (FR-08) | 안내만 한다 | AI 호스트 |

### 만들지 않는 것
- 소유자 승인 화면과 알림 (FR-03). 후속 spec(`app-owner-approval`)이 맡는다.
- 토큰 재발급, 계정 삭제. 경로가 없다.
- 서버에 라우터를 붙이는 일. 상태 관리가 맡는다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 서버 주소가 비었거나 호스트가 없다 | 저장하지 않고 입력 오류를 표시한다 | `FR01_rejects_invalid_server_address` |
| 서버 주소에 `http://`가 없다 | `http://`를 붙여 저장한다. 끝의 `/`는 지운다 | `FR01_normalizes_server_address` |
| 서버에 연결하지 못했다 (주소 오류, 다른 Wi-Fi, 서버 꺼짐, 시간 초과) | "서버에 연결하지 못했어요"와 주소·Wi-Fi를 확인하라는 안내를 표시한다. 토큰과 상태를 바꾸지 않는다 | `FR01_network_error_is_reported`, `FR01_reason_messages_tell_what_to_do` |
| 이름이 비었거나 64자를 넘는다 | 서버에 보내지 않고 입력 오류를 표시한다 | `FR01_rejects_invalid_name_before_request` |
| 계정 만들기가 `409`로 거부됐다 | 사유별 안내를 표시하고 토큰을 저장하지 않는다 | `FR01_create_account_rejection_keeps_reason` |
| 비콘 등록이 `BEACON_ALREADY_REGISTERED`로 거부됐다 | 다른 계정이 쓰는 값이라고 알리고, 비콘 탭에서 새 값을 만들라고 안내한다 | `FR01_reason_messages_tell_what_to_do` |
| 비콘 등록이 `401 INVALID_TOKEN`이다 | 토큰이 맞지 않는다고 알리고, 토큰을 다시 넣거나 새 계정을 만들라고 안내한다 | `FR01_put_beacon_sends_bearer_token`, `FR01_reason_messages_tell_what_to_do` |
| 서버가 JSON이 아닌 응답이나 모르는 형식을 보냈다 | 서버 응답 오류로 표시한다 | `FR01_unexpected_response_is_reported` |
| Android 17에서 로컬 네트워크 권한을 거부했다 | 요청하지 않고, 설정에서 허용하라고 안내한다 | 수동 확인 |
| 토큰 지우기를 눌렀다 | 확인 창을 띄운다. 토큰은 다시 받을 수 없다고 알린다 | 수동 확인 |
| 송출 중에 비콘 값이 바뀌었다 | 비콘 탭 규칙대로 송출 중에는 값을 바꿀 수 없다 | 수동 확인 |

## 6. 결정

- 화면은 아래 탭 두 개로 나눈다: "비콘"(송출), "계정"(서버 주소, 계정, 토큰, AI 호스트 연결 정보) — 승인 탭은 후속 spec이 더한다.
- HTTP는 Android 기본 `HttpURLConnection`, JSON은 `org.json`으로 다룬다 — 외부 라이브러리 없이 경로 두 개를 부른다. 단위 테스트에는 `org.json:json`을 테스트 의존성으로 넣는다. 연결·읽기 제한 시간은 5초다.
- 요청·응답 변환(`AccountApi`)은 전송(`HttpTransport`)과 분리해 가짜 전송으로 JVM 테스트한다.
- 계정을 만들면 이어서 지금 비콘을 `PUT /me/beacon`으로 등록한다. 비콘 등록이 실패해도 토큰은 저장한다 — 토큰은 다시 받을 수 없다.
- 서버에 마지막으로 등록한 식별자를 저장해서 지금 비콘과 비교한다.
- 서버 주소는 `http://`와 `https://`를 모두 받는다. 독립 AP의 서버는 인증서가 없을 수 있어서 평문 HTTP를 허용한다 (`usesCleartextTraffic`).
- Android 17(API 37)부터는 사설 IP 접속에 `ACCESS_LOCAL_NETWORK` 런타임 권한이 필요하다. 서버를 부르기 직전에 요청하고, 거부하면 설정에서 허용하라고 안내한다 — 권한이 없으면 연결이 시간 초과로만 끝나서 원인을 알 수 없다.
- 연결 실패는 메서드, 경로, 예외 종류만 로그에 남긴다 — 토큰이 든 헤더와 본문은 남기지 않는다 (NFR-08).
- 토큰은 앱 전용 SharedPreferences에 저장한다 — 다른 앱은 읽을 수 없다.
- AI 호스트 연결 정보는 `{서버 주소}/mcp`와 토큰을 복사하게 한다.

## 7. 가정

- 없음. 서버 조립은 계정 라우터를 붙인다. 독립 AP 안에서 HTTP로 토큰을 보내는 것은 NFR-08이 허용한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-01 | `FR01_normalizes_server_address` | 주소에 `http://`를 붙이고 끝 `/`를 지운다 |
| FR-01 | `FR01_rejects_invalid_server_address` | 비었거나 호스트 없는 주소를 거부한다 |
| FR-01 | `FR01_create_account_posts_name_and_returns_token` | `POST /accounts`에 이름을 보내고 토큰을 받는다 |
| FR-01 | `FR01_rejects_invalid_name_before_request` | 잘못된 이름은 요청하지 않는다 |
| FR-01 | `FR01_create_account_rejection_keeps_reason` | 거부를 상태 코드와 사유로 돌려준다 |
| FR-01 | `FR01_put_beacon_sends_bearer_token` | `PUT /me/beacon`에 Bearer 토큰과 비콘을 보낸다 |
| FR-01 | `FR01_network_error_is_reported` | 연결 실패를 네트워크 오류로 돌려준다 |
| FR-01 | `FR01_unexpected_response_is_reported` | 형식이 틀린 응답을 서버 응답 오류로 돌려준다 |
| FR-01 | `FR01_reason_messages_tell_what_to_do` | 사유마다 할 일이 있는 안내 문장이 있다 |
| FR-01 | `FR01_beacon_sync_state` | 등록한 비콘과 지금 비콘이 다르면 등록 필요다 |
| FR-01 | 수동: 계정 라우터만 붙인 임시 서버와 에뮬레이터 | 계정을 만들고 비콘이 "서버에 등록됨"이 된다 (확인함) |
| FR-01 | 수동: 실제 서버 | 받은 토큰으로 MCP에 연결된다. 미확인 (앱 빌드 환경 없음) |
