# 대시보드 조회 서비스와 모니터링 발행

- 담당: dnnals (대시보드)
- 요구사항: FR-07, FR-11, FR-12A, FR-12B, FR-20, NFR-07, NFR-08
- 따르는 결정: #168, 2026-10-27 담당자 결정 (평가 리포트를 지표마다 합친다), 2026-10-23 팀 대면 논의 (조회는 CORE 조회 서비스, 설정은 선택 섹션 `[dashboard]`), 2026-10-24 담당자 결정 (6절: 대시보드에 필요한 서버 쪽 일을 이 작업에 모두 넣는다, 발행은 커밋된 행을 따라 읽어서 한다), 2026-10-25 팀 확인 (6절의 다른 역할 폴더 작업과 발행 방식, 2절 형식 변경 `accessEnd`·`rejected`·`period`, `[dashboard]` 키, 기기 이름 쓰기 `set_node_name`, `evaluation`·`confusion` 선택 키, architecture §2 미결 닫기)

## 1. 왜

- `feat/api-dashboard`가 만든 모니터링 WebSocket과 조회 REST는 서버에 붙어 있지 않다. `runtime/app.py`는 MCP 앱만 붙이고 `[dashboard]` 설정이 없다. `nearpilot serve`로 띄운 서버에는 `/dashboard/...`가 없다.
- `DashboardQueries`와 `MonitorSnapshot`의 구현이 테스트 fake뿐이다. 장소 목록, 점유 목록, 승인 목록, 감사 목록, 이력, 비콘 수신을 읽는 저장소 메서드가 없다.
- `MonitorFeed.publish`를 부르는 곳이 없다. 붙여도 스트림은 연결 직후의 지금 상태 뒤로 조용하다.
- 형식 표의 키 몇 개는 원천이 없다. 기기 이름(`node`에 이름 열이 없다), 약한 신호 기준, 장소별 모델·보정 버전(모델을 읽은 뒤 버린다), 혼동 행렬.

## 2. 무엇

- 입력:
  - DB의 커밋된 행: 장소, 기기, 계정, 활성 비콘, RSSI 관측, 요청, 점유, 명령, 승인, 감사 로그.
  - 서버가 시작할 때 읽은 근접 모델, MQTT 연결 상태, 공용 조명의 유지 대상자, 평가 리포트 파일(`var/reports/*.csv`).
  - 설정 `[dashboard]` (선택 섹션).
- 출력:
  - `DashboardQueries` 9개와 `MonitorSnapshot.current_events`의 구현. 결과는 architecture.md §5 "대시보드 조회"와 "WebSocket" 형식이다.
  - 모니터링 이벤트 12종을 `MonitorFeed.publish`로 넘기는 주기 작업 (이하 "따라 읽기").
  - `[dashboard]`가 있으면 `nearpilot serve`의 서버에 대시보드 라우터가 MCP 앱보다 먼저 붙는다.
- 바뀌는 상태:
  - DB: 마이그레이션 `0006`이 `node.name`을 더한다. 시드 `[[nodes]]`가 `name`을 받는다. 그 밖의 쓰기는 없다.
  - 메모리: 따라 읽기의 커서(테이블별 마지막 rowid), 기기 상태·비콘 수신·승인 상태의 마지막 값.
- 약속:
  - 조회와 따라 읽기는 DB에 쓰지 않는다 (FR-12A). 판정과 실행의 결과를 바꾸지 않는다 (NFR-07).
  - 따라 읽기는 커밋된 행만 읽는다. 되돌려진 쓰기는 발행하지 않는다. 같은 테이블의 행은 커밋 순서(rowid)대로 발행한다.
  - 조회 결과와 이벤트에 사용자 ID를 넣지 않는다. 사람은 `Subject`로만 넘긴다. 자유 문자열(감사 `text`, 단계 `detail`, 이력 `outcome`, 점유 `sessionNote`, 점검 `note`, 타임라인 `text`, 지표 문구)은 코드와 값으로 만들고 감사 payload의 문자열을 그대로 옮기지 않는다 (FR-11, NFR-08).
  - `[dashboard]`가 없으면 라우터, 조회 서비스, 따라 읽기를 만들지 않는다. 서버의 다른 동작은 지금과 같다.

### 형식 변경 (FR-12A)

- 신뢰도의 `evaluation`은 선택 키, 그 안의 `confusion`도 선택 키로 바꾼다. 평가 리포트가 없으면 `evaluation`을 뺀다. 리포트에는 혼동 행렬이 없어 `confusion`은 늘 뺀다 (6절 결정).
- 요청 한 건에 선택 키 `accessEnd`(`approved`, `denied`, `expired`)를 더하고, `accessExpiredAt`은 `expired`일 때만 낸다 (6절 그 밖의 규칙).
- WebSocket `exec_result`의 `result`에 `rejected`를 더한다 (기기가 실행 전에 거절, 격리 없음. 5절).
- 대시보드 조회 경로 목록에 이력 `period`의 두 형식을 적는다 (6절).
- architecture.md §5 형식 표·경로 목록과 `api/ws/schema.py`를 함께 고친다.

### 설정 `[dashboard]`

| 키 | 뜻 |
|---|---|
| `queue_max_events`, `batch_max_events`, `auth_timeout_sec`, `audit_limit_max` | `DashboardSettings` (`feat/api-dashboard`) |
| `weak_rssi_dbm` | 장소 목록의 `weakRssiDbm`. 유한한 수 |
| `tail_interval_sec` | 따라 읽기 간격. 0보다 큰 유한한 수 |

- 키가 빠지거나 값이 틀리면 `ConfigError`다. 모르는 키도 `ConfigError`다 (다른 섹션과 같다).

## 3. 제약

- 사용자 ID, 토큰, 계정 이름을 조회 결과, 이벤트, 로그에 쓰지 않는다 (FR-11, NFR-08).
- 계층끼리는 `shared`의 접점으로만 닿는다. 조회 서비스는 새 접점 `DashboardReadStore`로 DB를 읽는다 (AGENTS.md 의존 규칙 4).
- 조회는 읽기 트랜잭션 하나에서 읽는다. 쓰기 경로(`BEGIN IMMEDIATE`)를 쓰지 않는다 (NFR-03).
- 간격, 기준값, 한도는 설정에서 받는다. 시간 기준(최근 비콘, 온라인 유지, 대여 시간)은 `Settings`의 값을 쓴다 (AGENTS.md 코딩 규칙).
- 판정 값, 사유 코드, 단계, 상태 이름은 `shared/enums.py`의 값만 쓴다.
- 따라 읽기의 실패(DB 오류, 형식 오류)는 그 주기만 건너뛰고 로그를 남긴다. 판정과 실행에 예외가 가지 않는다 (NFR-07).
- 종료할 때 열린 DB 연결이 남지 않는다. 조회는 `CallGate`가 닫히면 받지 않는다.

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/dashboard/` (새 폴더: 조회 서비스, 문구, 따라 읽기)
- `server/src/nearpilot/db/repositories/` (대시보드 읽기 구현, 시드를 쓰는 `admin_repository.py`의 기기 이름), `db/schema/sql/0006_node_name.sql`, `server/tools/evaluation/db.py`의 `EXPECTED_SCHEMA_VERSION` (마이그레이션 수와 같게 올린다), `execution_store.py`의 `execution_context` (아래 결정의 예약 시각)
- `server/src/nearpilot/runtime/` (설정 `[dashboard]`, 조립, 앱에 라우터 붙이기, 모델 보관)
- `server/src/nearpilot/core/lifecycle/shared_light.py`: 유지 대상자 읽기 메서드 하나를 더한다
- `server/src/nearpilot/cli/seed.py`와 시드를 저장하는 `core/admin/`: `[[nodes]]`의 선택 키 `name`
- `server/src/nearpilot/api/ws/schema.py`: `evaluation`, `confusion`, `accessEnd` 선택 키와 `exec_result`의 `rejected`
- `server/src/nearpilot/shared/interfaces.py`, `shared/models.py`: `DashboardReadStore`와 그 행 모델, `AdminRepository`·`AdminService`의 `set_node_name`(시드의 기기 이름)을 더하기만 한다
- `server/tests/` 해당 계층, `server/tests/integration/`
- `server/config/server.example.toml`, `server/config/README.md`, `server/config/seed/example.toml`
- `docs/architecture.md` §2 담당(조회 서비스 폴더와 담당), §4 모니터링 발행·대시보드 조회 계약과 관리 기능의 DB 쓰기 표(기기 이름), §5 형식 표와 대시보드 조회 경로 목록, §6 `node` 엔터티의 `name` (2026-10-24 담당자 결정)
- `specs/api-dashboard/spec.md`의 발행 주체 행과 `MonitorFeed` 독스트링: 발행을 따라 읽기로 바꾼 결정에 맞춘다

### 고치지 않는 곳
- 항상: `docs/` (위 절은 제외), `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/` (위 추가는 제외), `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/allocation/`, `core/policy/`, `core/proximity/`, `iot/`, `api/mcp/`, `api/auth/`, `api/rest/`, `dashboard/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `DashboardQueries`, `MonitorSnapshot` | 제공 | 대시보드 (`api/ws`, `api/rest`) |
| `MonitorFeed.publish` | 사용 | 대시보드 (`api/ws` 허브) |
| `DashboardReadStore` (새 접점) | 사용 (CORE), 제공 (DB) | DB |
| `create_dashboard_router` | 사용 (조립) | 대시보드 |
| `SharedLightService` 유지 대상자 읽기 | 사용 | 상태 관리 |
| `MqttConnection.ready` | 사용 (조립이 넘긴다) | 상태 관리 (IOT) |
| 평가 리포트 CSV (`tools/evaluation/report.py`) | 읽기 | DB (평가 도구) |

### 만들지 않는 것
- 대시보드의 서버 어댑터 (`dashboard/`). `fix/dashboard-places`에서 이 브랜치 위에 만든다.
- 앱용 REST(계정, 승인 결정). 승인 결정 이벤트(`approved`, `denied`)는 결정이 DB에 저장되면 따라 읽기가 발행하므로, 앱 REST가 생기면 따로 붙일 일이 없다.
- 평가 도구의 혼동 행렬.
- 장소별로 거르는 조회와 스트림 (`feat/api-dashboard` 6절 결정).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `[dashboard]`가 없다 | 라우터를 붙이지 않는다. `/dashboard/places`는 MCP 앱으로 가고 대시보드 응답이 아니다. 따라 읽기 작업이 없다 | `test_FR12A_대시보드_섹션이_없으면_라우터를_붙이지_않는다` |
| `[dashboard]`의 키가 빠지거나 값이 틀리거나 모르는 키 | `ConfigError` | `test_FR12A_대시보드_설정이_틀리면_ConfigError` |
| 장소에 근접 모델이 없거나 수집 전용 모드 | `modelVersion`, `calibrationVersion`은 `"없음"`. 점검 `model`도 그 장소는 `없음` | `test_FR12A_모델이_없는_장소는_버전이_없음` |
| 기기 이름이 없다 (옛 DB, 시드에 `name` 없음) | `nodeId`를 이름으로 쓴다 | `test_FR12A_이름이_없는_기기는_ID를_이름으로_쓴다` |
| 장소에 속하지 않은 기기 (`place_id` 없음) | 장소 목록에 넣지 않는다 | `test_FR12A_장소가_없는_기기는_장소_목록에_없다` |
| 요청이 판정 중이다 (`verdict` 없음) | 이력에 넣지 않는다. 요청 한 건은 `verdict` 없이, 닿지 않은 단계는 `pending` | `test_FR12A_판정_중인_요청은_이력에_없고_단계는_pending` |
| 서버 내부 요청 | 이력, 요청 한 건, `mcp_call`에 넣지 않는다. 명령과 상태 전이는 발행한다 | `test_FR12B_서버_내부_요청은_도구_호출로_발행하지_않는다` |
| `request_access` 요청 | 이력과 요청 한 건(`tool`은 `request_use`만)에 넣지 않는다. 승인 신청(`approval` `requested`)과 감사는 발행한다 | `test_FR12A_접근_신청_요청은_이력에_없다` |
| 감사 payload에 `user_id`나 계정 이름이 있다 (관리 기록) | 문구에 넣지 않는다 | `test_FR11_감사_문구에_사용자_ID를_넣지_않는다` |
| 감사 payload에 사유 코드가 없거나 `ReasonCode` 값이 아니다 (`IN_PROGRESS`) | 6절 규칙으로 정한 코드를 쓴다 | `test_FR12A_감사_행의_코드는_사유_코드다` |
| 평가 리포트가 없거나 읽을 수 없다 | `evaluation`을 뺀다. 읽기 실패는 로그만 | `test_FR12A_평가_리포트가_없으면_evaluation을_뺀다` |
| 가장 늦은 리포트에 일부 지표만 있다 | 없는 지표는 그 지표가 있는 더 이른 리포트의 행을 쓴다 | `test_FR12A_평가는_지표마다_가장_늦은_리포트를_쓴다` |
| 판정 수가 0이다 | 지연 시간은 `None`, `counts`는 다섯 값 모두 0 | `test_FR12A_판정이_없으면_지연은_None` |
| 대여가 없는 기기의 `rentals/{node_id}` | `None` (API가 404) | `test_FR12A_대여가_없으면_None` |
| 없는 요청의 `requests/{id}` | `None` | `test_FR12A_없는_요청은_None` |
| `period`가 `today`도 `HH:MM~HH:MM`도 아니다 | `ValueError` (API가 500). 6절 형식 | `test_FR12A_이력_기간_형식` |
| 조회 중 DB 오류 | 예외를 올린다 (API가 500) | `test_FR12A_조회_중_DB_오류는_예외를_올린다` |
| 서버가 닫히는 중 조회 | `NotAccepting`을 올린다 (API가 500). 종료가 열린 연결을 기다리지 않는다 | `test_FR12A_종료_중_조회는_받지_않는다` |
| 따라 읽기를 시작한 순간 | 이미 있는 행은 발행하지 않는다 (지금 상태는 스트림 연결 직후에 따로 보낸다) | `test_FR12B_따라_읽기는_시작_뒤의_행만_발행한다` |
| 따라 읽기 중 DB 오류나 형식이 틀린 행 | 그 주기만 건너뛰거나 그 행만 버리고 로그를 남긴다. 다음 주기는 이어 간다 | `test_NFR07_따라_읽기_실패는_다음_주기에_이어_간다` |
| 같은 기기 상태가 다시 쓰인다 | 바뀌지 않았으면 `transition`을 발행하지 않는다 | `test_FR12B_상태가_같으면_전이를_발행하지_않는다` |
| 대여 시간을 넘긴다 | 한 번 `transition` (`to` 지금 상태, `overdue` 참) | `test_FR12B_대여_시간을_넘기면_한_번_발행한다` |
| 승인 대기가 기한을 넘긴다 | 한 번 `approval` `expired` | `test_FR12B_승인_대기가_만료되면_한_번_발행한다` |
| 비콘 수신이 끊기거나 다시 잡힌다 | 바뀔 때만 `beacon` | `test_FR12B_비콘_수신이_바뀔_때만_발행한다` |
| 명령 결과가 `rejected` (기기가 실행 전에 거절, 격리 없음) | `exec_result`의 `rejected` | `test_FR12B_거절된_명령은_거절로_발행한다` |
| 요청 한 건의 앞선 승인이 아직 대기 중 (대기 기한 전 `pending`) | `accessEnd`와 `accessExpiredAt`을 뺀다 | `test_FR12A_요청_한_건의_승인_끝_까닭` |
| 보내지 않고 취소된 명령 (`not_sent`) | `command`도 `exec_result`도 발행하지 않는다. 전송을 시작한 뒤 미전송 증명으로 취소되면, 그 사이 낸 `command`는 거두지 않고 `exec_result`를 내지 않는다 | `test_FR12B_보내지_않은_명령은_발행하지_않는다` |
| 비활성 비콘이나 모르는 비콘의 관측 | 저장되지 않으므로 발행할 것이 없다 | (저장소 규칙, 테스트 없음) |
| 보존 정리로 오래된 감사·이력이 지워졌다 | 남은 행만 돌려준다 | `test_FR12A_정리된_행은_조회에_없다` |
| 연결 직후 지금 상태 | 기기마다 `transition`, 활성 비콘의 사람마다 `beacon`, 대기 중인 승인마다 `approval` `requested`, 진행 중인 요청의 이벤트 | `test_FR12B_지금_상태는_기기_비콘_승인_진행_중_요청이다` |

## 6. 결정

- 2026-10-26 센서 기반 물리 검증 제외 결정을 대시보드 조회 문구에 적용한다 (FR-07, FR-12A, FR-12B, FR-20).
  - 감사 로그, 이력과 대여 타임라인의 성공은 `제어 신호 출력 완료`로 쓴다. 기존 `열림`·`켜짐`·`꺼짐` 문구를 대체한다.
  - `CLOSING` 메모는 `점유 해제 중`, 종료 완료 기록은 `반납 완료 · 점유 해제`로 쓴다.
  - 조회 형식과 판정, 실행 결과 값은 바꾸지 않는다. `command_state`를 기기·점유 상태에서 추정하지 않는다.

- 대시보드에 필요한 서버 쪽 일은 다른 역할의 폴더여도 이 작업에 넣는다 (2026-10-24 담당자 결정) — 4절 고치는 곳의 `db/`, `runtime/`, `core/lifecycle/shared_light.py`, `core/admin/`, `cli/seed.py`, `shared/`가 그 범위다. 대시보드가 실제 서버로 동작해야 끝나는 작업이라, 다른 담당의 일정을 기다리지 않는다. 2026-10-25 팀이 확인했다.
- 조회 서비스, 문구, 따라 읽기는 `core/dashboard/`에 두고 대시보드 담당이 맡는다 — architecture.md §2의 미결을 2026-10-24 담당자가 정했다. 대시보드만 쓰는 읽기라 판정·상태 관리의 서비스와 섞지 않는다.
- 발행은 사건이 난 자리에서 하지 않고, 따라 읽기가 커밋된 행을 읽어 한다 (2026-10-23 팀 결정의 "사건이 난 영역이 그 자리에서 넘긴다"를 바꾼다, 2026-10-24 담당자 결정) — 감사 행과 상태 쓰기는 DB 트랜잭션 안에 있어 그 자리에서 발행하면 되돌려진 쓰기를 알린다. 저장 결과가 새 상태를 돌려주지 않고, 사람 역할과 내부 요청 ID를 모르는 자리가 많다. 따라 읽기는 다른 영역 코드를 고치지 않고 커밋 순서를 지킨다. 화면까지 최대 `tail_interval_sec`만큼 늦다.
- 되살린 실행 문맥은 예약이 없는 요청(서버 내부, 승인 없는 공용형)에서 `reserved_at`을 비운다 (2026-10-24, 대시보드 시나리오에서 찾은 결함) — 판정이 넘긴 문맥은 이 둘에 `reserved_at`이 없는데(`specs/state-management/core-execution.md` 제약) 명령 행에는 받은 시각이 남아, 결과 인계가 "같은 내부 요청의 실행 문맥이 다르다"로 거부되고 재시도를 반복했다. 공용 조명의 실행 결과가 대시보드에 늦게 보이지 않게 고친다.
- 따라 읽는 행과 이벤트:
  - 요청(방문자) → `mcp_call`. `request_access`는 발행하지 않는다.
  - 감사 → `audit`. 요청의 판정이 저장되면 그 요청의 `step`(닿은 단계까지), `posterior`(③을 지났으면), `verdict`를 차례로 발행한다.
  - 명령 → 보내기 시작하면 `command`, 결과가 저장되면 `exec_result`. 결과가 저장되거나 명령이 없는 판정이면 `mcp_result`.
  - 기기 → `device_state`가 바뀌면 `transition`. `FAULT`면 `fault.cause`.
  - RSSI 관측 → 사람마다 그 주기의 기기별 마지막 dBm으로 `rssi` 하나. 활성 비콘마다 수신 여부가 바뀌면 `beacon`.
  - 승인 → 새 행은 `requested`, 상태가 `approved`·`denied`로 바뀌면 그 값, 대기가 기한을 넘기면 `expired`.
- 판정 값은 판정 단계가 저장한 값(`request.verdict`)이다. 실행 결과는 이력 `outcome`·`code`와 `exec_result`로 보인다 — 대시보드 설계가 `OK` 판정 뒤 실행 결과를 따로 그린다. 스트림과 이력이 같은 값을 보인다.
- `request_use`와 `request_access`는 요청의 첫 감사 행 단계로 가린다. 첫 행이 ④이면 `request_access`다 — `request_use`의 판정 흐름은 ⓪ 또는 ①부터 감사를 남기고, `request_access`는 ④만 남긴다 (`core/decision/access.py`). 요청 행에는 도구를 가리는 열이 없다. 따라 읽기는 첫 감사 행이 나온 뒤 `mcp_call`을 발행한다.
- 단계 결과: 판정이 멈춘 단계(마지막 판정 행이 남은 단계)는 `stop`, 그 앞은 `pass`, 뒤는 `skip`. OK면 닿은 단계는 `pass`이고, 공용형은 ⑤⑥이 `skip`이다. ⓪은 감사 행이 없어도 ①이 있으면 `pass`이고 시각은 요청을 받은 시각이다. ⑤⑥은 감사 행 하나를 같이 쓰므로 그 행에서 멈추면 사유로 나눈다 (`DEVICE_BUSY`는 ⑤ `stop`, `USER_BUSY`·`APPROVAL_EXHAUSTED`는 ⑤ `pass`·⑥ `stop`). ④에서 나온 `APPROVAL_EXHAUSTED`는 ④ `stop`이다. 판정 중이면 닿지 않은 단계는 `pending`이다. 서버 오류로 판정 행이 없으면 닿은 마지막 단계에서 멈춘 것으로 본다. 오류 뒤 다시 판정한 요청은 마지막 재판정(⓪ `link_request_retry` 행) 뒤의 행만 본다. 요청 한 건 조회와 스트림이 같은 함수로 만든다.
- 감사 행 `code`: payload의 `reason` 또는 `result`가 `ReasonCode` 값이면 그 값. 실행 결과는 `succeeded` → `OK`, `failed`·`rejected` → `EXEC_FAILED`, `unknown` → `EXEC_UNKNOWN`. 판정 중(`IN_PROGRESS`)은 `COMMAND_IN_PROGRESS`. 그 밖은 `OK`.
- `fault.cause`: 격리 근거 `device_result`의 `unknown` → `EXEC_UNKNOWN`, 그 밖의 결과 → `EXEC_FAILED`, 다른 근거(`sensor_fault`, `announce_changed` 등) → `TARGET_UNAVAILABLE`. 근거는 기기마다 마지막 고장 근거 행이다 (`isolate_device`, 결과 `unknown`·`failed`인 `record_result`). `rejected` 결과는 기기를 격리하지 않으므로 근거가 아니다.
- 문구는 `core/dashboard/text.py` 한 곳에서 만든다. 지금 대시보드 목 데이터와 같은 짧은 한국어다 (`제어 신호 출력 완료`, `실행 안 함`, `결과 불명 → 고장 격리`, `근접 통과 0.91`, `비콘 수신 없음`).
- 이력 `period`는 `today`(서버 시간대의 오늘 0시부터) 또는 `HH:MM~HH:MM`(서버 시간대 오늘의 구간)이다 — 대시보드 이력 화면의 두 선택지다. 시각 비교는 UTC로 한다.
- 지연 시간과 `respondedAt`은 요청을 받은 시각부터 그 요청의 마지막 감사 행(반납 완료 행은 뺀다)까지다 — 응답 시각을 저장하지 않는다. 반납 완료 행은 대여를 만든 요청의 ID를 달고 반납할 때 붙어서, 넣으면 반납이 늦을수록 지연이 길어진다. 중앙값과 p95는 시연 세션(서버 시작 뒤)의 판정만으로 센다. `since`는 서버가 시작한 시각이다.
- `doubleOccupancy`는 같은 기기에 끝나지 않은 점유가 둘 이상인 기기 수다 (보통 0).
- 점유 목록: 대여형은 끝나지 않은 `use_session`, 공용형은 `SharedLightService`의 유지 대상자 (2026-10-24 담당자 결정). `startedAt`은 대여형은 예약 시각(없으면 받은 시각), 공용형은 유지 대상자의 마지막 요청 시각이다. `overdueMinutes`는 `ReleaseService`와 같은 규칙(대여 만료 시각부터, 0 이상)이고 0이면 뺀다. `sessionNote`는 기기가 `FAULT`면 `고장으로 보존`, 기기나 세션이 `CLOSING`이면 `점유 해제 중`, 초과면 `대여 시간 초과`.
- 승인 분류: 대기는 `pending`이고 대기 기한 전, 유효는 `approved`이고 유효 기한 전이며 남은 횟수가 있음, 나머지는 끝남. `endReason`은 `RepositoryPolicyChecker`와 같은 규칙이다. `remainingMinutes`는 대기면 대기 기한까지, 유효면 유효 기한까지다.
- 점검: `connected`는 판정 ②와 같은 규칙(마지막 수신이 `online_ttl_sec` 안이고 세션이 있음). 비콘은 활성 비콘이 있는 사람만 (2026-10-24 담당자 결정). `lastSeenSec`은 그 비콘의 마지막 관측부터, 없으면 `None`. `note`는 `beacon_recent_sec`을 넘기거나 관측이 없으면 `최근 신호 없음`. `server.database`는 `SELECT 1`의 성공, `mqtt`는 연결 준비 상태, `model`은 장소마다 `{모델} · {보정}`을 ` / `로 이은 문자열이다.
- 평가: 설정 파일 폴더의 부모 아래 `var/reports/`(CLI `dirs_for`와 같은 폴더)의 CSV를 모두 읽고, 지표(`metric` 열의 값)마다 그 지표가 있는 리포트 가운데 `generated_at`이 가장 늦은 것의 행을 쓴다 (2026-10-24 담당자 결정, 2026-10-27 담당자 결정으로 지표별 합치기로 바꿈). 일부 지표만 잰 리포트가 가장 늦어도 다른 지표가 화면에서 사라지지 않는다. 한 리포트에 같은 지표 행이 여러 개면 그 리포트의 그 행들을 모두 쓴다. 지표 순서는 가장 늦은 리포트의 행 순서이고, 그 리포트에 없는 지표는 다음으로 늦은 리포트의 행 순서로 뒤에 붙인다. 지표 행마다 `id`는 합친 순서의 1부터의 순번, `name`은 지표 이름의 한국어(`앞 기기 오선택률`, `보정 오차`, `전환 시간`, `흔들림`), `pass`는 통과 여부(비면 `None`), `label`은 `통과`·`✕ 미통과`·`미측정`, `sample`은 시행 수, `sampleNote`는 리포트의 비고다. `date`는 합친 행을 낸 리포트 가운데 가장 늦은 `generated_at`의 날짜다.
- 그 밖의 규칙 (2026-10-25 검토 1 반영):
  - 요청 한 건의 `accessRequestedAt`은 같은 사람·기기·동작으로 그 요청보다 먼저 만든 승인 가운데 가장 늦은 것의 `created_at`이다. `accessEnd`는 그 승인이 끝난 까닭이다: `approved`, `denied`, 대기가 아닌 `pending`(승인 분류와 같이 기한이 지났거나 기한이 없음)은 `expired`, 대기 중이면 없다. `accessExpiredAt`은 `expired`일 때만 그 `pending_until`이다 — 승인·거절 시각은 저장하지 않는다 (2026-10-25 실제 시연 뒤 담당자 위임 결정: 대기 중인 신청을 대시보드가 만료로 그렸다).
  - 공용 유지 대상자의 점유 행 `session`은 `ACTIVE`다 (유지 대상자는 켜 둔 조명을 쓰는 중이다).
  - 이력 기간 `HH:MM~HH:MM`은 같은 날 안이다. 끝이 시작보다 늦지 않으면(자정을 넘는 구간 포함) `ValueError`다.
  - 지금 상태의 진행 중인 요청은 서버가 시작한 뒤 받은 요청만 본다. 그 전의 미완료 요청은 복원이 다룬다.
  - 사람 역할을 찾지 못한 방문자 요청(계정이 지워짐)은 `Subject`를 만들 수 없어 `mcp_call`과 판정 이벤트를 내지 않고 명령만 낸다.
  - 문구는 `text.py` 한 곳에 둔다 (평가 지표 이름과 통과 문구 포함).
- 기기 이름은 `node.name` 열(마이그레이션 `0006`)과 시드 `[[nodes]].name`으로 받는다. 없으면 `nodeId`를 쓴다 (2026-10-24 담당자 결정). 기기 발표로 새로 생긴 기기는 이름이 없다.
- 장소별 모델·보정 버전은 서버가 시작할 때 읽은 모델을 `Runtime`이 보관해 조회 서비스에 넘긴다. 없으면 `"없음"`.
- 조회 서비스와 따라 읽기는 `Runtime`을 만들 때 만들고, 서버가 시작한 뒤에야 생기는 값(모델 버전, 연결 상태, 유지 대상자, 시작 시각)은 늦게 읽는 함수로 넘긴다. 시작 전에는 연결 상태가 거짓, 유지 대상자가 빈 목록이다 — 라우터를 MCP 앱보다 먼저 붙여야 하는데 그때는 서비스가 아직 없다. 조회는 접수 문(`CallGate`)을 지나고, 따라 읽기는 주기 작업의 종료 순서를 따르므로 지나지 않는다.
- 오류 뒤 같은 요청 ID로 다시 판정한 요청은 새 감사 행만 `audit`으로 발행한다. 명령을 연결하기 전에 취소된 방문자 요청은 `mcp_result`가 없어 따라 읽기가 계속 추적한다 (주기마다 한 건을 더 읽는다). 형식이 틀린 요청 행은 그 요청만 버린다.
- 조회는 `CallGate.enter()` 안에서 `ThreadDatabase.scope()` 하나와 읽기 트랜잭션 하나로 읽는다 — 종료가 조회를 기다리고, 한 응답 안의 값이 같은 순간이다.
- 따라 읽기는 `PeriodicTask`(`runtime/tasks.py`) 하나다. 시작할 때 테이블마다 마지막 rowid를 커서로 잡는다. 기기·비콘·승인의 마지막 값은 메모리에 둔다.
- 평가 리포트와 근접 모델의 혼동 행렬은 만들지 않는다. 형식의 `evaluation`, `confusion`을 선택 키로 바꾼다 (2026-10-24 담당자 결정).

## 7. 가정

- 감사 payload의 키(`reason`, `result`, `posteriors`, `threshold`, `node_id`, `event`, `outcome`, `evidence`)는 지금 코드의 모양이다. 판정·상태 관리가 바꾸면 문구와 단계 복원을 함께 고친다. 테스트가 실제 판정 흐름으로 만든 감사 행을 쓴다.
- 두 사람이 같은 시연 PC에서 대시보드를 연다 (`feat/api-dashboard` 6절). 조회 빈도는 화면 갱신 몇 초 간격이라 읽기 트랜잭션이 쓰기를 늦추지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-11, NFR-08 | 5절의 `test_FR11_*`, `test_FR11_조회와_이벤트에_사용자_ID가_없다` | 실제 DB로 만든 조회 9종과 이벤트 12종의 직렬화에 사용자 ID와 계정 이름이 없다 |
| FR-12A | 5절의 `test_FR12A_*` | 조회 9종의 원천, 분류, 문구, 빈 값 |
| FR-12A | `test_FR12A_조회_9종이_형식_검사를_통과한다` | 실제 DB의 시나리오(대여, 공용, 승인, 거부, 고장)에서 조회 결과가 `api/ws/schema.py` 검사를 통과한다 |
| FR-12A | `test_FR12A_요청_한_건의_단계는_판정_흐름과_같다` | OK, ASK(③), BUSY(⑤), USER_BUSY(⑥), 공용 OK, ⓪ 한도 초과의 단계 결과 |
| FR-12A | `test_FR12A_충돌_재전송은_기존_요청의_단계를_바꾸지_않는다` | 충돌 재전송은 기존 요청의 단계를 바꾸지 않는다 |
| FR-12B | 5절의 `test_FR12B_*` | 따라 읽기의 이벤트 종류, 순서, 바뀔 때만 |
| FR-12B | `test_FR12B_요청_하나의_이벤트가_순서대로_나간다` | `mcp_call` → `step`… → `posterior` → `verdict` → `command` → `exec_result` → `mcp_result` |
| FR-12A, FR-12B | `test_FR12B_서버에서_대시보드까지_이어진다` (`tests/integration/`) | `[dashboard]`가 있는 조립 서버에서 MCP `request_use` 하나가 스트림 이벤트로 오고, 조회 REST 9개가 200이다 |
| NFR-07 | `test_NFR07_따라_읽기_실패는_다음_주기에_이어_간다`, `test_NFR07_발행_실패가_판정을_바꾸지_않는다` | 따라 읽기와 허브의 실패가 판정·실행 결과에 닿지 않는다 |

2026-10-26 표시 문구 변경 검증: `server/`에서 전체 `pytest`를 실행했다.
실제 Mosquitto를 포함해 3,661개 통과, 기존 건너뜀 2개와 예상 실패 1개다.
수정한 Python 테스트 함수 목록은 이전 커밋과 같다.
