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
- 읽기 전에 `refresh.refresh`를 한 번 보낸다. 캐시 안에서는 보내지 않는다 (결정 7).
- 캐시 안에서는 서버 명령으로 확인한 값을 겹쳐 답한다. 결과 불명이 나오면 캐시를 버린다 (결정 8).
- 서버는 `get_device_state`를 부를 때만 SmartThings를 읽는다. `reported_state_cache_sec` 동안 결과를 재사용한다 (§1.2, #289).
- `find_available_devices`는 보고 상태를 담지 않는다 (#289).
- 가시성 검사를 통과한 기기만 SmartThings를 읽는다 (FR-09).
- 조회 실패는 캐시하지 않는다 (architecture.md "보고 상태 조회").
- 시간 값은 설정에서 받는다 (AGENTS.md).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/smartthings/` (`actions.py`, `transport.py`, `http.py`, `gateway.py`, 새 `reported.py`)
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
- 이 작업: 판정 파이프라인, 실행 서비스, 명령 게이트웨이의 확인 경로(확인한 값의 기록만 더한다), DB

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| 보고 상태 인터페이스 (`ReportedStateReader`) | 제공 | CORE (기기 조회) |
| `get_device_state` MCP 응답 | 제공 (CORE 결과를 그대로 전달) | API |

### 만들지 않는 것

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
| 클라우드 값이 기기 값보다 늦다(리모컨으로 음량을 바꿈) | `refresh` 뒤 읽어서 기기 값을 담는다 | `test_FR09_refresh_before_read_gets_device_value` |
| `refresh`를 보내지 못한다(429 등) | 클라우드에 남은 값을 그대로 담는다 | `test_FR09_refresh_failure_still_reads` |
| 캐시 안에서 서버 명령이 값을 바꾸고 확인했다 | SmartThings를 다시 읽지 않고 확인한 값을 그 키에 담는다 | `test_FR09_confirmed_value_overlays_cache` |
| 서버 명령의 결과가 불명이다 | 다음 조회는 캐시를 버리고 다시 읽는다 | `test_FR09_unknown_result_invalidates_cache` |
| 확인한 값보다 나중에 SmartThings를 읽었다 | 읽은 값을 쓴다 | `test_FR09_fresh_read_supersedes_older_confirmed` |
| 캐시 시간이 지났다(리모컨 변경 가능) | 확인한 값이 있어도 다시 읽는다 | `test_FR09_cache_expiry_reads_again_with_confirmed` |

## 6. 결정

- 보고 키는 `SmartThingsAction.reported`(키, capability, 속성)로 동작 표에 둔다 — 확인 속성과 같은 출처를 한곳에서 관리한다. 키는 `on`·`off` `power`, `set_volume` `volume`, `set_mute` `mute`, `set_picture_mode` `picture_mode`, `set_sound_mode` `sound_mode`, `set_channel` `channel`, `launch_app` `app`(`tvChannel.tvChannelName`)이다.
- 조회는 `GET /devices/{id}/components/main/status` 한 번이다 — capability마다 부르지 않아 호출 한도를 적게 쓴다.
- 캐시는 `SmartThingsReportedState` 인스턴스(기기 하나)에 둔다. 시계는 주입한 단조 시계를 쓴다.
- 담을 동작은 선언 합성과 같은 규칙으로 고른다. 동작의 `requires` capability가 응답에 모두 있으면 담는다 — 인터페이스(`read(node_id)`)를 바꾸지 않고, IOT가 저장소를 읽지 않는다. 선언과 응답의 capability가 같으므로 선언한 동작과 같다.
- 전체 상태 조회는 `SmartThingsTransport`에 더하지 않고 별도 `SmartThingsStatusSource`로 둔다 — 기존 명령 경로의 fake와 계약을 바꾸지 않는다.
- 노드별 리더 라우팅은 `ReportedStateRouter`(바인딩한 node_id → 리더)로 둔다.
- 읽기 전에 `refresh.refresh`를 한 번 보낸다 (2026-11-14 담당자 결정). display-1(M5) 실기에서 클라우드의 `audioVolume.volume`은 기기 값이 바뀌어도 갱신되지 않았다(기기 10, 클라우드 8). `refresh` 응답(약 1.4초) 직후의 조회가 기기 값이었다. `observed_at`은 값이 마지막으로 바뀐 시각이라 오래됐다는 이유로 호스트가 값을 버리면 상대 조작을 하지 못한다. 그래서 서버가 조회 순간의 값을 맞추고, 도구 설명은 "필요한 키가 없을 때만 사용자에게 확인한다"로 안내한다. SmartThings 기기당 호출 한도(분당 명령 12, 조회 12)에서 한 번의 조회는 명령 1, 조회 1을 쓴다. `architecture.md` "보고 상태 조회"의 "한 번 읽는다"와 다르다 — docs 반영 이슈 초안을 담당자에게 넘긴다.
- 게이트웨이가 확인한 값을 기기별 `ConfirmedValues`에 기록하고, 리더는 캐시를 읽은 뒤 확인한 값을 같은 키에 겹친다 (2026-11-14 담당자 결정, B안). "볼륨 좀만 더"마다 `get_device_state`가 refresh와 조회(약 1.8초, 명령 1·조회 1)를 쓰지 않게 한다. 캐시 시간은 `reported_state_cache_sec`이고, 지나면 다시 읽어 리모컨 변경을 반영한다. 결과 불명이면 기기 값을 알 수 없으므로 캐시를 버린다. 확인 속성과 보고 키의 속성이 같을 때만 기록한다. 보고 상태는 여전히 실행 성공의 근거가 아니다. `architecture.md` "명령 확인 조회와 보고 상태 조회는 서로의 결과를 쓰지 않는다"와 다르다 — 같은 이슈 초안에 넣는다.

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
| FR-09 | `test_FR09_refresh_before_read_gets_device_value` | 읽기 전에 `refresh`를 보내고, 캐시 안에서는 보내지 않는다 |
| FR-09 | `test_FR09_refresh_failure_still_reads` | `refresh` 실패는 읽기를 막지 않는다 |
| FR-09 | `test_FR09_refresh_requires_command_transport` | 명령을 보낼 수 없는 transport로 `refresh`를 켜면 생성 오류다 |
| FR-09 | `test_FR09_confirmed_value_overlays_cache` | 캐시 안에서 확인한 값을 겹치고 SmartThings를 읽지 않는다 |
| FR-09 | `test_FR09_unknown_result_invalidates_cache` | 결과 불명 뒤에는 다시 읽는다 |
| FR-09 | `test_FR09_fresh_read_supersedes_older_confirmed` | 확인보다 나중에 읽은 값이 이긴다 |
| FR-09 | `test_FR09_cache_expiry_reads_again_with_confirmed` | 캐시 시간이 지나면 다시 읽는다 |
| FR-09 | `test_FR09_success_records_confirmed_value` | 게이트웨이가 확인한 값을 기록한다 |
| FR-09 | `test_FR09_unknown_result_marks_reported_stale` | 결과 불명이면 캐시를 버리라고 표시한다 |
| FR-09 | `test_FR09_not_sent_keeps_reported` | 보내지 못한 명령은 보고 상태를 바꾸지 않는다 |
