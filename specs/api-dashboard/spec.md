# 대시보드 모니터링 WebSocket과 조회 REST

- 담당: dnnals (대시보드)
- 요구사항: FR-11, FR-12A, FR-12B, NFR-07, NFR-08
- 따르는 결정: #168, 2026-10-23 팀 대면 논의 (대시보드 접점은 shared, 발행은 사건이 난 영역, 조회는 CORE 조회 서비스, 설정은 선택 섹션 `[dashboard]`)

## 1. 왜

- 대시보드(`dashboard/`)는 목 어댑터로만 움직인다. 서버에는 모니터링 스트림(FR-12B)과 대시보드 조회 REST(FR-12A)가 없다. `api/ws/`와 `api/rest/`는 빈 패키지다.
- 모니터링 메시지 형식, 사람 이름을 유지하는 기간, 조회 REST의 경로와 응답 형식이 미결이라 대시보드 서버 어댑터를 만들 수 없다.
- 서버가 사람마다 이름을 붙여 스트림과 조회에 같게 보내야 한다 (architecture.md §5). 사용자 ID를 지우는 곳이 정해지지 않으면 이벤트를 만드는 영역마다 지우는 규칙이 달라진다 (FR-11, NFR-08).

## 2. 무엇

- 입력:
  - 이벤트를 만드는 영역이 넘기는 모니터링 이벤트 (`MonitorEvent`). 사람은 사용자 ID와 역할(`Subject`)로 담긴다.
  - 대시보드의 WebSocket 연결과 조회 요청. 소유자 토큰으로 인증한다.
- 출력:
  - WebSocket `/dashboard/stream`: 메시지 하나는 `{"v": 1, "events": [...]}`다. 연결 직후 첫 메시지는 지금 상태를 다시 보낸 이벤트 묶음이다.
  - 조회 REST `GET /dashboard/...` 9개: 장소, 점유, 승인, 감사 로그, 이력, 요청 한 건, 신뢰도 지표, 대여 한 건, 점검.
  - 이벤트와 응답의 모양은 대시보드의 `dashboard/src/model/events.ts`, `types.ts`와 같다. 키는 camelCase다.
- 바뀌는 상태: 서버 저장 상태는 바꾸지 않는다. 메모리에 사람 이름표(사용자 ID → 이름)와 연결별 대기열만 둔다.
- 약속:
  - 사용자 ID를 지우는 곳은 하나다. 허브와 조회 라우터가 `Subject`를 사람 필드(`personId`, `pseudonym`, `letter`, `role`)로 바꾸고, 형식 표의 허용 키만 남긴다. 이벤트를 만드는 영역이 넘긴 그 밖의 키는 버린다 (FR-11, NFR-08).
  - 같은 사람은 스트림과 조회에서 같은 `personId`와 이름이다. 방문자는 처음 나타난 순서로 `방문자 A`, `방문자 B`, …이고, 소유자는 `소유자`다. 이름은 서버 프로세스가 끝날 때까지 유지한다.
  - 이벤트 발행(`MonitorHub.publish`)은 어느 스레드에서 불러도 막지 않고 예외를 올리지 않는다. 모니터링의 실패는 판정과 실행을 바꾸지 않는다 (NFR-07).
  - 조회 REST는 `GET`만 둔다. 대시보드는 서버에 쓰지 않는다 (FR-12A).
  - 연결 함수 `create_dashboard_router(...)`가 FastAPI 라우터를 만든다. 서버 앱에 붙이는 일은 조립이 한다.

### 메시지 형식 (FR-12B)

시각은 모두 ISO 8601 문자열(밀리초, 시간대 포함, 예 `2026-10-23T14:03:05.120+09:00`)이다. 사람 필드는 `personId`(불투명한 `p1`, `p2`, …), `pseudonym`, `letter`(방문자만), `role`(`visitor`, `owner`)이다.

| `type` | 허용 키 (모든 이벤트에 `type`, `at`) | 사람 필드 |
|---|---|---|
| `beacon` | `seen` | 모두 |
| `rssi` | `rssi` (기기 ID → dBm) | `personId` |
| `mcp_call` | `requestId`, `tool`, `target` (`kind`, `capability`, `nodeId`), `action` | `personId` |
| `step` | `requestId`, `step` (0~6), `result` (`pass`, `stop`), `detail` | 없음 |
| `posterior` | `requestId`, `posteriors` (기기 ID → 확률), `threshold` | 없음 |
| `verdict` | `requestId`, `verdict`, `reason`, `nodeId` | 없음 |
| `command` | `requestId`, `nodeId`, `action` | 없음 |
| `exec_result` | `requestId`, `nodeId`, `result` (`succeeded`, `failed`, `unknown`) | 없음 |
| `mcp_result` | `requestId` | 없음 |
| `transition` | `nodeId`, `to`, `overdue`, `fault` (`cause`, `action`) | 없음 |
| `approval` | `requestId`, `nodeId`, `kind` (`requested`, `approved`, `rejected`, `expired`) | `personId` |
| `audit` | `row` (`id`, `at`, `step`, `nodeId`, `text`, `code`) | 없음 |

- `requestId`는 서버의 내부 요청 ID다. 호스트가 보낸 `request_id`가 아니다.
- 값이 없는 선택 키는 보내지 않는다.

### 조회 REST (FR-12A)

| 경로 | 응답 (`types.ts`) | 사람 필드 |
|---|---|---|
| `GET /dashboard/place` | `PlaceInfo` | 없음 |
| `GET /dashboard/uses` | `UseRow[]` | `personId` |
| `GET /dashboard/approvals` | `Approvals` | 행마다 `personId` |
| `GET /dashboard/audit?limit=` | `AuditRow[]` (최근 것부터) | 없음 |
| `GET /dashboard/history?period=&verdict=&nodeId=` | `HistoryRow[]` | `personId` |
| `GET /dashboard/requests/{request_id}` | `RequestProgress` | `personId` |
| `GET /dashboard/trust` | `TrustReport` | 없음 |
| `GET /dashboard/rentals/{node_id}` | `RentalInfo` | `personId` |
| `GET /dashboard/health` | `Health` | `beacons`의 행마다 `personId` |

- 오류 응답은 `{"error": "<사유 코드>"}`다 (architecture.md §5 REST 공통 규칙, #184). 조회 경로가 받지 않는 메서드(405)와 없는 경로(404)는 FastAPI 기본 본문이다.

## 3. 제약

- 사용자 ID, 계정 정보, 토큰을 메시지, 응답, 로그에 쓰지 않는다 (FR-11, NFR-08).
- 스트림 연결과 조회는 소유자(`Role.OWNER`) 토큰만 받는다. 토큰으로 계정을 찾는 일은 `api/auth`의 `Authenticator`가 한다 (의존 규칙 1).
- 다른 계층의 코드를 부르지 않는다. 이벤트와 조회 결과는 `shared`의 접점(`MonitorFeed`, `MonitorSnapshot`, `DashboardQueries`)으로만 받는다 (의존 규칙 4).
- 대기열 크기, 묶음 크기, 인증 기한, 감사 조회 한도는 코드 상수로 쓰지 않는다. `DashboardSettings`로 받는다 (AGENTS.md 코딩 규칙).
- 동기 접점은 이벤트 루프를 막지 않도록 스레드에서 부른다.
- 판정 값, 사유 코드, 단계, 상태 이름은 `shared/enums.py`의 값만 쓴다.

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/ws/`
- `server/src/nearpilot/api/rest/` (대시보드 조회만)
- `server/tests/api/`
- `server/src/nearpilot/shared/models.py`, `shared/interfaces.py`: 대시보드 접점(`Subject`, `MonitorEvent`, `MonitorFeed`, `MonitorSnapshot`, `DashboardQueries`)을 더하기만 한다 (2026-10-23 팀 결정)
- `docs/architecture.md` §2 담당, §4 영역 간 계약, §5 REST와 WebSocket, `docs/requirements.md` FR-12A·FR-12B의 미결 두 줄 (2026-10-23 팀 결정과 대시보드 담당 결정)

### 고치지 않는 곳
- 항상: `docs/` (위 절은 제외), `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/` (위 두 파일의 추가는 제외), `server/pyproject.toml`, `.github/`
- 이 작업: `api/mcp/`, `api/auth/`, `core/`, `db/`, `iot/`, `runtime/`, `cli/`, `dashboard/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `Authenticator.authenticate` | 사용 | 판정 (`api/auth`) |
| `MonitorFeed.publish` | 제공 | 이벤트가 난 영역: 판정, 상태 관리, IOT, MCP 도구 (2026-10-23 팀 결정) |
| `MonitorSnapshot`, `DashboardQueries` | 사용. 테스트는 fake | CORE 조회 서비스 (2026-10-23 팀 결정) |
| `create_dashboard_router` | 제공 | 상태 관리 (조립) |
| 모니터링 WebSocket, 대시보드 조회 REST | 제공 | 대시보드 |

### 만들지 않는 것
- 이벤트를 발행하는 코드 (판정, 상태 관리, IOT, MCP 도구), CORE 조회 서비스와 저장소의 읽기 메서드, 서버 앱에 라우터를 붙이는 조립과 설정 섹션 `[dashboard]`. 각 영역의 후속 작업이다 (architecture.md §4).
- 앱용 REST (계정, 승인)와 `FAULT` 복구 REST
- 대시보드의 서버 어댑터 (`dashboard/`)
- 장소별로 스트림과 조회를 거르는 것 (7절 가정)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 이벤트에 허용 목록 밖의 키, `user_id`, 사람 객체가 있다 | 버린다. 사람은 사람 필드로만 나간다 | `test_FR11_이벤트에서_허용_목록_밖의_키와_사용자_ID를_버린다` |
| 조회 결과에 허용 목록 밖의 키가 있다 | 버린다 | `test_FR11_조회_응답에서_허용_목록_밖의_키와_사용자_ID를_버린다` |
| 같은 사람이 스트림과 조회에 나온다 | 같은 `personId`와 이름 | `test_FR12B_같은_사람은_스트림과_조회에서_같은_이름` |
| 방문자 여럿과 소유자 | 처음 나타난 순서로 `방문자 A`, `B`, …, 소유자는 `소유자`, 26명 뒤는 `방문자 AA` | `test_FR12B_방문자는_나타난_순서로_글자를_받고_소유자는_소유자` |
| 알 수 없는 `type`, 필수 키가 없음, 값이 틀림 | 그 이벤트만 버리고 로그를 남긴다. 발행자에게 예외를 올리지 않는다 | `test_FR12B_형식이_틀린_이벤트는_버리고_발행자에게_예외를_올리지_않는다` |
| 연결이 없을 때 발행 | 아무것도 하지 않는다 | `test_FR12B_연결이_없으면_발행은_아무것도_하지_않는다` |
| 다른 스레드에서 발행 | 연결된 모든 클라이언트에 발행 순서대로 간다 | `test_FR12B_다른_스레드의_발행이_모든_연결에_순서대로_간다` |
| 연결 직후 | 첫 메시지는 `MonitorSnapshot`의 이벤트 묶음이다. 그 뒤 발행한 이벤트가 이어진다 | `test_FR12B_연결하면_지금_상태를_먼저_보낸다` |
| 지금 상태 조회가 예외를 낸다 | 빈 묶음을 첫 메시지로 보내고 스트림을 이어 간다 | `test_FR12B_지금_상태_조회가_실패해도_스트림을_이어_간다` |
| 대기 중인 이벤트가 여럿 | 묶음 크기 한도까지 한 메시지에 담는다 | `test_FR12B_대기_중인_이벤트를_한도까지_한_메시지에_묶는다` |
| 클라이언트가 늦어 대기열이 찬다 | 그 연결만 닫는다 (1013). 다른 연결은 이어진다 | `test_FR12B_대기열이_찬_연결만_닫는다` |
| 인증 메시지 없이 기한이 지남 | 닫는다 (4408) | `test_FR12B_인증_기한이_지나면_닫는다` |
| 토큰이 틀리거나 없다 | 닫는다 (4401) | `test_FR12B_토큰이_틀리면_닫는다` |
| 첫 메시지가 binary 프레임이거나 토큰을 UTF-8로 바꿀 수 없다 (짝 없는 서로게이트) | 닫는다 (4401). 인증 뒤의 binary 프레임과 깨진 텍스트는 버린다 | `test_FR12B_binary_프레임은_인증으로_받지_않는다`, `test_FR12B_토큰이_틀리면_닫는다` |
| 방문자 토큰 | 닫는다 (4403) | `test_FR12B_방문자_토큰은_닫는다` |
| 계정 조회 실패 | 닫는다 (1011) | `test_FR12B_계정_조회가_실패하면_닫는다` |
| 조회 REST: 토큰이 없거나 틀림 | 401 `INVALID_TOKEN`. 조회 접점을 부르지 않는다 | `test_FR12A_토큰이_틀리면_401` |
| 조회 REST: 방문자 토큰 | 403 `POLICY_DENIED`. 조회 접점을 부르지 않는다 | `test_FR12A_방문자_토큰은_403` |
| 조회 REST: 계정 조회 실패 | 500 `INTERNAL_ERROR` | `test_FR12A_계정_조회가_실패하면_500` |
| 요청·대여 한 건이 없다 | 404 `USE_NOT_FOUND` | `test_FR12A_없는_요청과_대여는_404` |
| 조회 접점이 예외를 내거나 형식이 틀린 결과를 준다 | 500 `INTERNAL_ERROR`. 예외 내용은 응답에 쓰지 않는다 | `test_FR12A_조회가_실패하면_500` |
| `limit`이 숫자가 아니거나 1 미만, `period`가 없거나 비었음, `verdict`가 판정 값이 아님 | 400 `INVALID_INPUT`. 조회 접점을 부르지 않는다 | `test_FR12A_조회_인자가_틀리면_400` |
| `limit`이 한도보다 크다 | 한도로 줄여 부른다 | `test_FR12A_감사_조회는_한도로_줄인다` |
| 쓰기 메서드 (`POST`, `PUT`, `PATCH`, `DELETE`) | 405 | `test_FR12A_조회_경로는_GET만_받는다` |

## 6. 결정

- 메시지 형식은 대시보드의 `events.ts`와 같은 이벤트 종류와 키로 한다 — 같은 담당이 만든 화면이 이미 이 모양으로 쌓이고, 서버 어댑터가 모양을 바꾸지 않아도 된다.
- 시각은 화면 문자열(`HH:MM:SS.mmm`)이 아니라 ISO 8601로 보낸다 — 날짜가 바뀌는 이력과 조회에서도 같은 형식을 쓰고, 화면 형식은 어댑터가 정한다.
- 승인 이벤트의 `kind`에 소유자 결정(`approved`, `rejected`)을 더한다 — FR-12B가 소유자 결정을 스트림 항목으로 둔다. 대시보드 리듀서는 아직 `requested`, `expired`만 읽는다 (어댑터 작업에서 맞춘다).
- 사람 이름은 서버 프로세스가 끝날 때까지 유지하고 다시 시작하면 새로 붙인다 — 시연 세션 하나에서 같은 사람이 같은 이름이면 되고, 사용자 ID와 이름의 대응을 저장하지 않는다. `personId`도 같은 기간 동안만 쓰는 불투명한 값이다.
- 소유자가 여럿이어도 이름은 모두 `소유자`다. `personId`로 구분한다.
- 스트림 인증은 연결 뒤 첫 메시지 `{"type": "auth", "token": "..."}`로 한다 — 브라우저 WebSocket은 `Authorization` 헤더를 보낼 수 없고, 주소의 질의 문자열에 토큰을 두면 접속 기록에 남는다.
- 스트림과 조회는 소유자 토큰만 받는다 — 감사 로그와 이력은 운영 정보다. 대시보드는 소유자의 시연 PC에서 연다.
- 닫기 코드: 4401 토큰 오류, 4403 소유자 아님, 4408 인증 기한, 1011 서버 오류, 1013 대기열 넘침 — 대시보드가 다시 연결할지(1013, 1011) 사람에게 알릴지(4401, 4403)를 코드로 고른다.
- 허브는 연결마다 크기가 정해진 대기열을 둔다. 차면 그 연결만 닫는다 — 발행자를 막지 않고, 늦은 화면 하나가 다른 화면과 판정을 늦추지 않는다. 다시 연결하면 지금 상태를 다시 받는다.
- 연결하면 구독을 먼저 등록하고 지금 상태를 읽는다 — 그 사이에 난 이벤트를 잃지 않는다. 같은 이벤트가 지금 상태와 스트림에 두 번 올 수 있다. 대시보드는 이벤트를 차례로 쌓으므로 같은 상태가 된다.
- 영역 사이 접점(`Subject`, `MonitorEvent`은 `shared/models.py`, `MonitorFeed`, `MonitorSnapshot`, `DashboardQueries`는 `shared/interfaces.py`)은 `shared`에 둔다 (2026-10-23 팀 결정). `DashboardSettings`는 API 안의 설정이라 `api/ws/settings.py`에 둔다.
- 도구 호출 이벤트의 `tool`은 MCP 도구 6개를 모두 받고, `target`과 `action`은 선택 키다 — 조회 도구에는 대상이 없다. 대시보드는 `request_use`만 그린다.
- 점검의 비콘 `label`은 서버가 사람 이름으로 채운다 — 조회를 구현하는 영역은 이름을 모른다.
- 요청 한 건의 `steps`는 ⓪~⑥ 일곱 칸이고, 신뢰도의 판정 수(`counts`)는 판정 값 다섯 개를 모두 담는다. 어긋나면 형식 오류다.
- 조회 인자는 직접 읽어 검사한다. 틀리면 400 `INVALID_INPUT`이다. 인증을 인자 검사보다 먼저 한다 — 인증하지 않은 호출이 인자 규칙을 알아내지 못한다.
- 이름표, 허용 목록 검사, 허브, 스트림 엔드포인트, 조회 라우터를 각각 모듈로 나눈다. 이름표와 허용 목록 검사는 스트림과 조회가 함께 쓴다.
- 연결 직후의 지금 상태 메시지는 묶음 크기로 나누지 않는다 — 지금 상태는 한 번에 받아야 대시보드가 같은 순간을 그린다. 그 뒤 메시지는 묶음 크기 이하다.
- null 허용 키(`latencyMedianSec`, `latencyP95Sec`, 지표의 `pass`, 점검 비콘의 `lastSeenSec`)는 값이 없을 때 조회 서비스가 키를 빼지 않고 `None`을 넣는다. 수는 파이썬 `int`·`float`만 받는다 — 형식 검사가 키 누락과 값 없음을 구분한다.
- 스트림은 UTF-8로 바꿀 수 없는 토큰을 계정 조회 전에 토큰 오류(4401)로 닫는다 — 해시에서 실패해 서버 오류(1011)로 닫히면 대시보드가 다시 연결을 되풀이한다.
- 테스트는 ASGI 앱을 직접 부른다 — dev 의존성에 `httpx`가 없다 (`tests/api/_mcp_client.py`와 같은 방식).

## 7. 가정

- 시연은 장소 하나다. 소유자는 스트림과 조회에서 모든 기기를 본다. 장소별로 거르는 일은 장소가 둘 이상이 될 때 정한다 — 미결 (이슈 없음)
- 감사 행의 `text`, 단계의 `detail`에 사용자 ID를 넣지 않는 것은 발행 영역과 조회 서비스가 지킨다. 이 작업은 키 단위로만 거른다 — 미결 (이슈 없음)

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-11, NFR-08 | 5절의 `test_FR11_*` | 허용 목록 밖의 키와 사용자 ID가 나가지 않는다 |
| FR-12B | 5절의 `test_FR12B_*` | 이름, 형식 검사, 발행, 지금 상태, 묶음, 대기열, 인증 |
| FR-12A | 5절의 `test_FR12A_*` | 조회 9개의 인증, 인자, 오류, 쓰기 거부 |
| FR-12A | `test_FR12A_조회_경로_9개가_접점_결과를_형식대로_돌려준다` | 경로마다 접점을 부르고 `types.ts` 모양으로 돌려준다 |
| FR-12B | `test_FR12B_이벤트_종류마다_형식대로_보낸다` | 이벤트 종류 12개가 형식 표의 키로 나간다 |
| NFR-07 | `test_NFR07_발행자가_예외를_받지_않는다` | 허브 내부 오류가 발행자에게 가지 않는다 |
