# 기기 원격 재시작

- 담당: 작업 지시자 (상태 관리·IOT·API·펌웨어)
- 요구사항: FR-12A, NFR-04
- 따르는 결정: #235 (A안. 서버가 현재 연결에 MQTT `restart`를 보내고 기기가 확인한 뒤 재시작한다)

## 1. 왜

- 기기가 멈추거나 설정을 다시 읽어야 할 때 사람이 전원을 뽑고 다시 꽂아야 한다. 시연 중에는 기기에 다가갈 수 없는 경우가 있다.
- 재시작은 실행 명령이 아니다. 판정 ⓪~⑥을 거치지 않고, 장소 소유자만 관리 기능으로 요청한다.

## 2. 무엇

- 입력: 소유자 토큰으로 부르는 `POST /admin/nodes/{node_id}/restart`, 운영 명령 `nearpilot restart NODE_ID --token-file PATH`
- 출력:
  - REST: 발행하면 `202` `{"node_id", "restart_id", "expires_at"}`. 거부는 `403` `NOT_OWNER`, `409` `NODE_OFFLINE`, `503` `NOT_SENT`
  - MQTT: `nearpilot/node/{node_id}/restart` (QoS 1, retain 없음) `{schema_version, node_id, restart_id, target_session_id, issued_at, expires_at}`
- 바뀌는 상태:
  - 서버: 없음. 점유, 명령 기록, 복구 건을 바꾸지 않는다. 서버 로그에 요청자, 기기, 결과를 남긴다.
  - 펌웨어: 마지막으로 처리한 `restart_id`(NVS)
- 약속:
  - `SENT`는 브로커에 발행했다는 뜻이다. 재시작 완료가 아니다. 재시작 뒤 기기는 새 기기 연결 ID로 발표한다.
  - 기기는 ID, 현재 연결, 시각 신뢰, 유효 시간, 새 `restart_id`를 모두 확인한 뒤에만 재시작한다 (architecture.md §5 MQTT 8절).
  - 처리 중인 명령은 결과 저장과 보고를 마친 뒤 재시작한다.

## 3. 제약

- 장소 소유권은 `Repository.is_place_owner`로 검사한다. 소유자가 아니면 기기 연결 여부를 알리지 않는다 (NFR-08).
- 응답과 MQTT 본문에 사용자 ID, 토큰을 넣지 않는다. REST 응답에 기기 연결 세션 ID를 넣지 않는다 (NFR-08).
- 유효 시간은 실행 응답 제한 `exec_timeout_sec`이다. 코드 상수로 두지 않는다 (AGENTS.md 코딩 규칙).
- 서버는 재시작 요청을 자동으로 다시 보내지 않는다.
- MCP와 대시보드에는 열지 않는다. 대시보드는 보기 전용이다 (FR-12A).
- api는 db를 직접 부르지 않는다. 상태 관리의 `NodeAdminService`만 부른다 (AGENTS.md 의존 규칙 1).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/shared/` (`enums.py` `RestartOutcome`, `models.py` `RestartResult`, `interfaces.py` `NodeRestarter`·`NodeAdminService`)
- `server/src/nearpilot/iot/` (`messages/` `encode_restart`, `mqtt/connection.py` 발행 종류, `mqtt/delivery.py` 현재 연결 조회, `mqtt/restart.py` 추가)
- `server/src/nearpilot/core/lifecycle/node_admin.py` (추가)
- `server/src/nearpilot/api/rest/nodes.py` (추가)
- `server/src/nearpilot/runtime/` (`assembly.py`, `app.py`)
- `server/src/nearpilot/cli/` (`restart.py` 추가, `__init__.py`)
- `server/tools/node_sim/config.py` (브로커 ACL만), `server/config/mosquitto.acl.example`, `server/config/README.md`
- `firmware/` (`lib/np_command/`, `src/main.cpp`, `test/test_common/`, `README.md`)
- `server/tests/`

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: `app/`, `dashboard/`, `api/mcp/`, 시뮬레이터 동작, `firmware/node_lab/`

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeRestarter.restart(node_id, restart_id)` (architecture.md §4 "기기 원격 재시작") | 제공 (IOT), 사용 (상태 관리) | IOT·상태 관리 |
| `NodeAdminService.restart(user_id, node_id)` | 제공 (상태 관리), 사용 (API) | API |
| MQTT `restart` (architecture.md §5 MQTT 3절·8절) | 제공 (서버), 사용 (펌웨어) | 펌웨어 |
| REST `POST /admin/nodes/{node_id}/restart` (architecture.md §5 REST) | 제공 (API), 사용 (운영 명령) | 운영 명령 |

### 만들지 않는 것

- 재시작 완료 확인 (서버는 새 연결로 확인한다. 운영 명령은 기다리지 않는다)
- MCP 도구, 대시보드 버튼, 앱 화면
- 시뮬레이터의 재시작 처리
- BLE 기기 설정과 mDNS (#234)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 장소 소유자가 아니다 | `403` `NOT_OWNER`. 발행하지 않는다 | `test_FR12A_restart_rejects_non_owner`, `test_FR12A_restart_route_maps_outcomes` |
| 현재 연결의 신선한 상태 보고가 없다 | `409` `NODE_OFFLINE`. 발행하지 않는다 | `test_NFR04_restarter_node_offline` |
| MQTT 구독 준비 전이거나 발행 결과를 모른다 | `503` `NOT_SENT`. 다시 보내지 않는다 | `test_NFR04_restarter_not_sent` |
| 토큰이 없거나 틀렸다 | `401` `INVALID_TOKEN` | `test_FR12A_restart_route_requires_token` |
| 기기가 받은 요청의 연결 ID, 기기 ID가 다르다 | 무시하고 시리얼 로그만 남긴다 | 펌웨어 `test_NFR04_restart_rejects_other_session` |
| 시각을 신뢰하지 않거나 만료됐다 | 무시한다 | 펌웨어 `test_NFR04_restart_requires_valid_window` |
| 같은 `restart_id`가 다시 왔다 | 다시 재시작하지 않는다 | 펌웨어 `test_NFR04_restart_ignores_handled_id` |
| 명령 처리 중에 요청이 왔다 | 처리를 마친 뒤 재시작한다 | 실제 기기 확인 (펌웨어 loop) |

## 6. 결정

- 상태 관리 서비스는 `core/lifecycle/node_admin.py`의 `NodeAdminService`다. 복구 서비스처럼 시작 뒤 늦게 연결하는 REST 래퍼로 부른다.
- 현재 연결은 IOT `ReportDelivery`가 마지막 신선한 상태 보고로 확인한 연결이다. 수신 뒤 `online_ttl_sec`가 지나면 없다고 본다 — 명령 전송과 같은 기준이다.
- `issued_at`은 IOT가 발행 직전에 정한다. `NodeAdminService`가 응답에 넣는 `expires_at`은 호출 전 시각에 `exec_timeout_sec`를 더한 값이다. 기기의 실제 만료는 응답 값보다 늦거나 같다 — 계약 `restart(node_id, restart_id)`를 그대로 쓴다.
- 발행 결과를 모르는 경우(`PublishUncertain`)도 `NOT_SENT`로 알린다. 같은 요청을 다시 하면 새 `restart_id`가 나가고, 이미 재시작한 기기는 연결 ID가 달라 무시한다.
- 펌웨어 검사는 `lib/np_command`의 `validateRestart`다. 명령 검사와 같은 시각 규칙을 쓴다.
- 펌웨어는 `restart_id`를 설정 NVS 이름공간(`np-config`)의 `restart` 키에 저장한다. 저장에 실패하면 재시작하지 않는다.
- 운영 명령은 `nearpilot recover`의 토큰 파일·서버 주소·HTTP 처리를 그대로 쓴다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-04 | `tests/iot/test_iot_restart.py` | 본문 형식, 현재 연결 지정, 유효 시간, 오프라인·발행 실패 |
| FR-12A | `tests/core/lifecycle/test_node_admin.py` | 소유권 검사, 새 `restart_id`, 결과 전달, 로그 |
| FR-12A | `tests/api/test_rest_node_restart.py` | 인증, 상태 코드, 응답에 세션 ID가 없다 |
| FR-12A | `tests/cli/test_cli_restart.py` | 토큰 파일, 성공·거부 종료 코드 |
| NFR-04 | `tests/integration/test_server_real_broker.py` | 실제 Mosquitto에서 기기 ACL로 `restart`를 받는다 |
| NFR-04 | `firmware/test/test_common/test_command.cpp` | 기기 측 재시작 검사 |
