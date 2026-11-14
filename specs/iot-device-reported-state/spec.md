# 외부 API 기기의 보고 상태 조회

- 담당: JM-220 (IOT)
- 요구사항: FR-09, FR-11, FR-07, NFR-07, NFR-08
- 따르는 결정: #289

## 1. 왜

호스트가 기기의 현재 값을 모른다. 그래서 "소리 조금 올려 줘" 같은 상대 조작을 고정 수치로 실행한다 (#289).

## 2. 무엇

- 입력: 호스트의 `get_device_state(node_id)` 호출
- 출력: 응답에 `reported`가 생긴다.
  - SmartThings 바인딩 기기의 `reported`는 `{키: {"value": 값, "observed_at": ISO 시각 또는 null}}`이다.
  - MQTT 기기와 조회에 실패한 기기의 `reported`는 `null`이다.
- 바뀌는 상태: 없음. IOT가 조회 결과를 메모리에 잠깐 캐시한다.
- 약속:
  - 보고 상태는 참고 정보다. 판정, 허가, 실행 성공의 근거가 아니다 (FR-07, NFR-07).
  - 응답에 SmartThings `deviceId`와 계정 정보를 넣지 않는다 (FR-11, NFR-08).

## 3. 제약

- 키는 기기가 선언한 동작이 바꾸는 값의 이름이다. 선언 합성과 같은 규칙(동작의 `requires` capability가 모두 있음)으로 고른 동작의 값만 담는다 (FR-09, #289).
- 관측 시각은 SmartThings의 속성별 `timestamp`다. 서버는 오래된 값을 빼지 않는다 (#289).
- 서버는 `get_device_state`를 부를 때만 SmartThings를 읽는다. `reported_state_cache_sec` 동안 결과를 재사용한다 (§1.2, #289).
- `find_available_devices`는 보고 상태를 담지 않는다 (#289).
- 가시성 검사를 통과한 기기만 SmartThings를 읽는다 (FR-09).
- 조회 실패는 캐시하지 않는다 (architecture.md "보고 상태 조회").
- 시간 값은 설정에서 받는다 (AGENTS.md).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/smartthings/` (`actions.py`, `transport.py`, `http.py`, 새 `reported.py`)
- `server/src/nearpilot/shared/models.py`: `ReportedValue`, `ReportedState`만 더한다 (#289).
- `server/src/nearpilot/shared/interfaces.py`: `ReportedStateReader`만 더한다 (#289).
- `server/src/nearpilot/shared/config.py`: `reported_state_cache_sec`만 더한다 (#289).
- `server/src/nearpilot/core/decision/query.py`, `core/decision/core_service.py`: 보고 상태를 응답에 담고 리더를 넘긴다.
- `server/src/nearpilot/runtime/assembly.py`: 리더 조립
- `server/src/nearpilot/api/mcp/server.py`: `get_device_state` 도구 설명만 고친다.
- `server/src/nearpilot/api/mcp/response.py`: 상태 응답의 허용 키에 `reported`(값과 관측 시각)만 더한다 (FR-11).
- `server/tests/iot/`, `server/tests/core/decision/`, `server/tests/api/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: 판정 파이프라인, 실행 서비스, 명령 게이트웨이의 확인 경로, DB

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| 보고 상태 인터페이스 (`ReportedStateReader`) | 제공 | CORE (기기 조회) |
| `get_device_state` MCP 응답 | 제공 (CORE 결과를 그대로 전달) | API |

### 만들지 않는 것

- `set_volume`·`set_mute`의 결과 불명 처리(refresh)는 별도 fix 작업이다.
- MQTT 기기의 보고 상태는 만들지 않는다.
- 대시보드 표시는 만들지 않는다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| SmartThings 조회가 실패한다 | `reported`는 `null`이다. 다음 조회는 다시 읽는다 | `test_FR09_transport_error_returns_none` |
| 캐시 시간 안에 다시 조회한다 | SmartThings를 다시 읽지 않는다 | `test_FR09_reported_cache_within_setting` |
| 응답에 속성이 없거나 `timestamp`가 없다 | 그 키를 빼거나 `observed_at`을 `null`로 둔다 | `test_FR09_missing_attribute_or_timestamp` |
| 동작 표 밖의 속성이나 `requires`를 채우지 못한 동작의 속성이 응답에 있다 | 담지 않는다 | `test_FR09_only_declared_actions_reported` |
| 리더가 다른 기기의 보고 상태를 돌려준다 | `reported`는 `null`이다 | `test_FR09_reported_for_other_node_is_ignored` |
| 보고 상태에 허용 밖의 키가 섞인다 | API가 값과 관측 시각만 남긴다 | `test_FR09_mcp_get_device_state_passes_reported` |
| MQTT 기기를 조회한다 | `reported`는 `null`이다 | `test_FR09_mqtt_node_reported_null` |
| 비공개 기기를 권한 없이 조회한다 | 기존처럼 `TARGET_UNAVAILABLE`이고 SmartThings를 읽지 않는다 | `test_FR09_hidden_node_does_not_read_reported` |
| 리더가 예외를 던진다 | `reported`는 `null`이고 나머지 응답은 그대로다 | `test_FR09_reader_error_keeps_state` |

## 6. 결정

- 보고 키는 `SmartThingsAction.reported`(키, capability, 속성)로 동작 표에 둔다 — 확인 속성과 같은 출처를 한곳에서 관리한다. 키는 `on`·`off` `power`, `set_volume` `volume`, `set_mute` `mute`, `set_picture_mode` `picture_mode`, `set_sound_mode` `sound_mode`, `set_channel` `channel`, `launch_app` `app`(`tvChannel.tvChannelName`)이다.
- 조회는 `GET /devices/{id}/components/main/status` 한 번이다 — capability마다 부르지 않아 호출 한도를 적게 쓴다.
- 캐시는 `SmartThingsReportedState` 인스턴스(기기 하나)에 둔다. 시계는 주입한 단조 시계를 쓴다.
- 담을 동작은 선언 합성과 같은 규칙으로 고른다. 동작의 `requires` capability가 응답에 모두 있으면 담는다 — 인터페이스(`read(node_id)`)를 바꾸지 않고, IOT가 저장소를 읽지 않는다. 선언과 응답의 capability가 같으므로 선언한 동작과 같다.
- 전체 상태 조회는 `SmartThingsTransport`에 더하지 않고 별도 `SmartThingsStatusSource`로 둔다 — 기존 명령 경로의 fake와 계약을 바꾸지 않는다.
- 노드별 리더 라우팅은 `ReportedStateRouter`(바인딩한 node_id → 리더)로 둔다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `test_FR09_reported_values_with_observed_at` | SmartThings 응답의 값과 `timestamp`를 키별 보고 상태로 바꾼다 |
| FR-09 | `test_FR09_reported_cache_within_setting` | 캐시 시간 안에는 다시 읽지 않고, 지나면 다시 읽는다 |
| FR-09 | `test_FR09_transport_error_returns_none` | 조회 실패는 `None`이고 캐시하지 않는다 |
| FR-09 | `test_FR09_missing_attribute_or_timestamp` | 없는 속성은 빼고, 없는 `timestamp`는 `null`이다 |
| FR-09 | `test_FR09_device_state_includes_reported` | `get_device_state`가 보고 상태를 키별 값과 관측 시각으로 담는다 |
| FR-09 | `test_FR09_zero_cache_reads_every_time` | 캐시 시간이 0이면 매번 읽는다 |
| FR-09 | `test_FR09_router_returns_none_for_unbound_node` | 바인딩하지 않은 기기는 `None`이다 |
| FR-09 | `test_FR09_reported_for_other_node_is_ignored` | 다른 기기의 보고 상태를 담지 않는다 |
| FR-09 | `test_FR09_http_component_status_path` | 구성요소 전체 상태를 `GET .../components/main/status`로 읽는다 |
| FR-09 | `test_FR09_http_component_status_invalid_body` | 형식이 다른 응답은 조회 실패다 |
| FR-09 | `test_FR09_only_declared_actions_reported` | `requires`를 채운 동작의 키만 담는다 |
| FR-09 | `test_FR09_mqtt_node_reported_null` | 리더가 없는 기기는 `null`이다 |
| FR-09 | `test_FR09_hidden_node_does_not_read_reported` | 가시성 검사 실패 시 리더를 부르지 않는다 |
| FR-09 | `test_FR09_reader_error_keeps_state` | 리더 예외가 나머지 응답을 막지 않는다 |
| FR-11 | `test_FR11_reported_has_no_device_id` | 응답에 SmartThings `deviceId`가 없다 |
| FR-09 | `test_FR09_mcp_get_device_state_passes_reported` | MCP 도구 응답에 `reported`가 그대로 실린다 |
