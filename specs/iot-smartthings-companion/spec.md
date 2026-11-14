# 동반 기기로 SmartThings 기기를 거리 기반으로 실행

- 담당: 작업 지시자 (상태 관리)
- 요구사항: FR-04, FR-05, FR-06, FR-07, FR-09, FR-17, FR-20, NFR-07, NFR-08
- 따르는 결정: #271 (decision PR #272), #259, #270(실기 기록)

## 1. 왜

- SmartThings 기기(1차 대상: 삼성 스마트 모니터 M5)는 RSSI를 보내지 않는다. 그래서 거리 선택자와 근접 지정의 후보가 되지 못한다.
- #271은 상용 기기 옆에 붙인 팀 기기(동반 기기)가 상용 기기의 동작을 선언하게 했다. 하지만 지금 서버는 모든 명령을 MQTT로만 보낸다.
- core는 기기 보고가 없는 성공을 결과 불명으로 기록한다. 그래서 SmartThings 어댑터를 연결하면 성공할 때마다 기기가 `FAULT`로 격리된다.
- 머지된 SmartThings 동작 표, 인자 형식, 확인 방식은 #270 실기 결과와 다르다. 실제 HTTP 전송도 없다.
- `exec_timeout_sec`가 전역값 하나뿐이다. 그래서 클라우드 왕복과 `refresh` 뒤 재조회가 3초 안에 끝나지 않는다.

## 2. 무엇

- 입력:
  - 동반 기기의 `announce`(종류 `display`, 점유 방식 `shared`), RSSI, `status`
  - 판정을 통과한 `NodeCommand`
  - 서버 설정 `[smartthings]`의 바인딩과 접근 토큰 파일
- 출력:
  - 바인딩한 `node_id`의 명령: SmartThings REST 명령 호출과 상태 조회. 성공하면 `confirmation`을 담은 `CommandResult`
  - 그 밖의 명령: 지금처럼 MQTT
- 바뀌는 상태:
  - core는 확인 근거가 있는 보고 없는 성공을 `succeeded`로 저장한다.
  - 명령의 `expires_at`은 대상 기기 종류의 실행 응답 제한으로 계산한다.
- 약속:
  - 동반 기기가 `near:display:1:1`, `nearest:display`의 후보가 된다. 판정과 근접 모델은 바꾸지 않는다.
  - 확인하지 못한 결과는 지금처럼 `unknown`이다.
  - 토큰과 `deviceId`는 호스트 응답, 로그, 감사에 남기지 않는다.

## 3. 제약

- 판정은 바꾸지 않는다. 라우팅 게이트웨이는 `NodeCommandGateway` 계약만 구현한다 (NFR-07, `architecture.md` "동반 기기").
- 바인딩이 없는 `node_id`의 명령은 지금의 `MqttCommandGateway`로 보낸다. MQTT 경로의 동작은 바뀌지 않는다 (`architecture.md` "동반 기기").
- 동작 8개와 SmartThings 명령, 인자, 성공 확인은 `requirements.md` FR-07 "SmartThings 클라우드 기기의 결과 확인" 표를 따른다 (#271).
- 동작 표는 `iot/smartthings/actions.py` 한 곳에 둔다. 선언 합성, 명령 전송, 펌웨어 선언 시험이 이 표를 읽는다 (`iot-smartthings-descriptor` 결정 1).
- 접수 응답과 `refresh`의 완료 응답은 성공 근거가 아니다. 상태 조회 값만 근거다 (FR-07).
- `set_channel`의 `up`·`down`은 같은 `command_id`로 다시 와도 다시 보내지 않는다 (FR-07, #271).
- 화면 모드와 소리 모드는 기기가 반환한 ID와 이름의 대응에서 이름을 골라 보낸다 (FR-07, #270).
- 요청과 응답 본문은 UTF-8로 쓰고 읽는다 (`architecture.md` "명령 전송과 결과 확인").
- core는 `report`가 없고 `confirmation`이 있는 `SUCCEEDED`만 성공으로 받는다. 근거 없는 성공, 실패, 거부는 결과 불명이다 (`architecture.md` "기기 명령 인터페이스").
- 종류별 실행 응답 제한은 설정 `exec_timeout_sec.{kind}`에서 받는다. 재정의가 없는 종류는 `exec_timeout_sec`를 쓴다 (`requirements.md` §1.2).
- 확인 조회 주기와 HTTP 요청 제한 시간은 설정에서 받는다. 코드 상수로 쓰지 않는다 (`AGENTS.md`).
- 토큰 값과 `deviceId`는 로그, 감사, 오류 메시지에 남기지 않는다 (NFR-08).
- 동반 기기의 펌웨어 선언은 서버 동작 표와 동작 이름·인자 스키마가 같아야 한다 (`architecture.md` "동반 기기").
- 펌웨어 선언은 펌웨어 패턴 부분집합(대안 `|`과 묶음 `()` 없음)과 발표 2048바이트 상한을 지킨다 (`device-protocol.md`).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/` (`smartthings/`, 새 `routing.py`)
- `server/src/nearpilot/core/lifecycle/execution.py`
- `server/src/nearpilot/shared/models.py`: `CommandResult.confirmation` 필드만 더한다 (#271).
- `server/src/nearpilot/shared/config.py`: `exec_timeout_sec.{kind}` 재정의만 더한다 (#271).
- `server/src/nearpilot/shared/interfaces.py`: `ExecutionStore.record_result`의 `isolate` 인자만 더한다 (결정 23, #276).
- `server/src/nearpilot/db/repositories/execution_store.py`, `dashboard_reads.py`, `server/src/nearpilot/core/dashboard/text.py`: 격리하지 않은 결과의 저장과 문구만 고친다 (결정 23, #276).
- `server/src/nearpilot/runtime/config.py`, `runtime/assembly.py`: `[smartthings]` 절과 라우팅 조립
- `server/config/server.example.toml`, `server/config/README.md`: `[smartthings]` 절 설명
- `firmware/lib/np_descriptor/np_descriptors.h`, `firmware/platformio.ini`: 동반 기기 선언과 빌드 env
- `server/tools/node_sim/device.py`: 발행 인코딩만 고친다 (결정 20, 담당자 지시)
- `specs/iot-smartthings-descriptor/spec.md`, `specs/iot-smartthings-adapter/spec.md`: 동작 표가 바뀐 부분만 고친다.
- `server/tests/iot/`, `server/tests/core/lifecycle/`, `server/tests/core/dashboard/`, `server/tests/db/`, `server/tests/shared/`, `server/tests/tools/`, `server/tests/integration/` (설정 시험은 `test_runtime_config.py`)

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/proximity/`, `db/`, `api/`, 대시보드, 앱, `iot/mqtt/`. 펌웨어의 명령 실행 코드(`main.cpp`, `np_command`, `np_executor`)

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeCommandGateway.send` | 제공(라우팅, SmartThings) | CORE(상태 관리) |
| `CommandResult.confirmation` | 더함 | shared |
| `Settings.exec_timeout_for(kind)` | 더함 | shared |
| `RepositoryProtocol.nodes()` (기기 종류 조회) | 사용 | DB |
| `shared.descriptor.parse_descriptor` | 사용 | shared |

### 만들지 않는 것

- OAuth 인가 코드 교환, 토큰 자동 갱신, `nearpilot smartthings link` — 후속 `feat/iot-smartthings-oauth`. 이 작업은 토큰 파일을 읽기만 한다.
- 동반 기기가 없는 SmartThings 기기의 조립(선언 합성 호출, 하트비트 주기 작업) — 후속 작업. 순수 코드는 남긴다.
- 바인딩과 동반 기기 선언의 불일치를 기동 때 검사하는 기능 — 시험으로만 맞춘다.
- `exec_timeout_sec.{kind}`를 기기 재시작(`node_admin`, `iot/mqtt/restart.py`)에 적용하는 일 — 재시작은 ESP32에 보내므로 전역값을 그대로 쓴다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 바인딩한 `node_id`의 명령 | SmartThings 게이트웨이로 보낸다. MQTT로 보내지 않는다 | `test_FR06_routing_bound_node_goes_to_smartthings` |
| 바인딩 없는 `node_id`의 명령 | MQTT 게이트웨이로 보낸다 | `test_FR06_routing_unbound_node_goes_to_mqtt` |
| 동작 표에 없는 동작 | `NOT_SENT` | `test_FR06_unknown_action_not_sent` (기존) |
| 연결 실패나 4xx로 명령을 보내지 못함 | `NOT_SENT` | `test_FR06_http_connect_error_not_sent`, `test_FR06_http_client_error_not_sent` |
| 명령 요청의 응답 시간 초과나 5xx | 보냈을 수 있다. 확인 조회를 계속하고, 확인하지 못하면 `UNKNOWN` | `test_FR07_ambiguous_post_keeps_confirming` |
| `set_channel`의 `channel`이 번호도 `up`·`down`도 아님 | 보내지 않고 `NOT_SENT` | `test_FR06_invalid_channel_not_sent` |
| `set_mute`의 `state`가 `muted` | `audioMute.mute`를 보내고 `mute`가 `muted`인지 확인 | `test_FR07_set_mute_selects_command` |
| `set_volume`·`set_mute` 뒤 클라우드 값이 옛 값으로 남음 | 확인 조회마다 `refresh`를 먼저 보낸다. 갱신된 값이 요청한 값이면 `SUCCEEDED` | `test_FR07_volume_and_mute_confirmed_after_refresh` |
| 화면 모드 ID의 이름을 대응표에서 찾음 | 이름을 인자로 보낸다. `refresh` 뒤 현재 모드를 확인 | `test_FR07_picture_mode_sends_name_from_map` |
| 대응표가 아직 `null` | `refresh` 뒤 다시 읽는다. 끝내 없으면 보내지 않고 `NOT_SENT` | `test_FR07_mode_map_missing_not_sent` |
| 상대 명령(`up`) 뒤 값이 바뀜 | `SUCCEEDED`, `confirmation`에 전후 값 | `test_FR07_channel_up_confirmed_by_change` |
| 상대 명령을 같은 `command_id`로 다시 보냄 | 다시 보내지 않고 처음 결과를 돌려준다 | `test_FR06_relative_command_not_resent` |
| 절대 명령을 같은 `command_id`로 다시 보냄 | 다시 보내고 확인한다(기존 규칙) | `test_FR06_same_command_id_same_content_resends` (기존) |
| `launch_app` 뒤 `tvChannelName`이 앱 식별자와 같음 | `SUCCEEDED` | `test_FR07_launch_app_confirmed_by_other_attribute` |
| 응답 제한 안에 확인하지 못함 | `UNKNOWN` | `test_FR07_timeout_without_confirmation_is_unknown` (기존) |
| 상태 조회가 요청한 숫자를 다른 표현(15 → 15.0)으로 돌려줌 | `SUCCEEDED` | `test_FR07_volume_reported_as_float_succeeds` |
| 상태 조회가 문자열 `"15"`, bool, 다른 숫자를 돌려줌 | `UNKNOWN` | `test_FR07_volume_other_value_is_unknown` |
| 확인하지 못하고 `UNKNOWN`을 돌려줌 | 마지막 관측 값과 기대값을 `node_id`·`command_id`와 함께 warning 로그에 남긴다. `deviceId`는 남기지 않는다 | `test_FR07_unconfirmed_logs_observed_value_without_device_id` |
| 한글 인자와 한글 응답 | UTF-8 바이트로 보내고 UTF-8로 읽는다 | `test_FR07_http_utf8_round_trip` |
| 토큰 파일이 없거나 형식이 틀림 | 보내지 않고 `NOT_SENT`. 오류 메시지에 토큰과 경로 밖 값을 담지 않는다 | `test_NFR08_missing_token_not_sent_without_secret` |
| 401 응답 | `NOT_SENT` | `test_FR06_http_unauthorized_not_sent` |
| core가 `confirmation`이 있는 `SUCCEEDED`를 받음 | `succeeded`로 저장하고 기기를 격리하지 않는다 | `test_FR07_server_confirmed_success_recorded` |
| core가 보고도 `confirmation`도 없는 `SUCCEEDED`를 받음 | 지금처럼 `unknown`과 `FAULT` | `test_FR07_unconfirmed_success_still_unknown` |
| core가 `confirmation`이 있는 `FAILED`를 받음 | `unknown` | `test_FR07_confirmation_ignored_for_failed` |
| core가 바인딩한 동반 기기의 공용형 실행에서 `unknown`·`failed`를 받음 | 결과는 `unknown`·`failed`로 저장하고 응답한다. 기기를 `FAULT`로 두지 않고 복구 건을 만들지 않는다. 사용분은 `consumed`, 근거에 `isolated: false` | `test_FR07_external_api_device_unconfirmed_not_fault` |
| 격리하지 않은 결과 뒤 같은 동반 기기에 새 명령 | 그대로 실행한다 | `test_FR07_external_api_device_next_command_runs_after_unknown` |
| 바인딩 밖 기기(MQTT)의 `unknown` | 지금처럼 `FAULT` | `test_FR07_unlisted_device_still_fault` |
| 동반 기기의 대여형 실행 | 지금처럼 격리 대상 | `test_FR07_external_api_rental_still_fault` |
| db가 `isolate=False`로 결과를 받음 | 결과·감사만 저장, 기기 상태 그대로, 사용분 `consumed` | `test_FR07_record_result_unisolated_keeps_device_state` |
| 대시보드가 `isolated: false` 결과를 그림 | `결과 불명 · 격리 안 함`, `실행 실패 · 격리 안 함`. 고장 근거로 고르지 않는다 | `test_FR07_격리하지_않은_결과는_격리_안_함으로_쓴다` |
| `display` 기기의 명령 | `expires_at`과 `send`의 제한 시간이 `exec_timeout_sec.display` | `test_FR07_exec_timeout_per_kind` |
| 재정의 없는 종류의 명령 | 전역 `exec_timeout_sec` | `test_FR07_exec_timeout_falls_back_to_global` |
| `exec_timeout_sec.{kind}`가 0 이하이거나 숫자가 아님 | `Settings` 생성 시 `ValueError` | `test_FR07_exec_timeout_override_validated` |
| `[smartthings]` 바인딩의 `node_id`·`deviceId`가 비었거나 중복 | 설정 오류 | `test_FR06_smartthings_config_rejects_bad_bindings` |
| 펌웨어 동반 기기 선언과 서버 동작 표 비교 | 동작 이름, 역할, 인자 스키마가 같다 | `test_FR04_display_firmware_matches_server_actions` |
| 펌웨어 동반 기기 선언의 발표 크기 | 2048바이트 이하 | `test_FR04_firmware_announce_within_limit` (기존 매개변수에 더함) |
| 시뮬레이터가 동반 기기 선언을 발표 | 가장 긴 기기 ID로도 2048바이트 이하이고 서버 디코더가 받는다 | `test_FR04_simulator_display_announce_within_limit` |
| 동반 기기에 거리 선택자로 요청(시뮬레이터 + 가짜 SmartThings) | 실행 `succeeded`, 기기 `FAULT` 아님 | `test_FR05_companion_display_near_selector_succeeds` |
| 유지 대상자가 모두 이탈 | 서버가 `off`를 SmartThings로 보낸다 | `test_FR20_companion_display_off_on_leave` |

## 6. 결정

1. 라우팅 게이트웨이는 `node_id → 게이트웨이` 표를 받는다. CORE에는 게이트웨이 하나만 넘긴다 — CORE의 `ExecutionService`와 조립 순서를 바꾸지 않는다.
2. 한 선언 동작이 인자 값에 따라 SmartThings 명령을 고르는 규칙은 동작 표의 함수 하나(`plan(arguments)`)로 둔다. 이 함수가 명령, 위치 인자 배열, 확인 방식, 기대값을 돌려준다. 고를 수 없는 인자면 `None`이고 게이트웨이는 `NOT_SENT`를 돌려준다 — 예외 분기를 게이트웨이에 흩지 않는다.
3. 확인 방식은 `equals`(같은 capability 속성이 기대값과 같음), `changed`(전송 전 값에서 바뀜), `equals_other`(다른 capability 속성이 기대값과 같음) 세 가지다.
4. `refresh`가 필요한 동작은 확인 조회마다 `refresh.refresh`를 먼저 보낸다. `refresh` 전송 실패는 무시하고 조회를 계속한다.
5. `changed` 동작은 전송 전 값을 `refresh` 뒤에 읽는다. 전송 전 값을 읽지 못하면 보내지 않고 `NOT_SENT`다 — 비교할 값이 없으면 성공을 확인할 수 없다.
6. `changed` 동작의 결과는 `command_id`로 저장한다. 같은 `command_id`가 다시 오면 저장한 결과를 돌려준다. 저장은 프로세스 메모리에만 둔다 — 서버를 다시 시작하면 CORE의 미확정 명령은 이미 복구 규칙을 따른다.
7. `set_channel`의 `channel`은 문자열 하나다. 숫자 1~4자리면 `setTvChannel`, `up`·`down`이면 `channelUp`·`channelDown`이다. 펌웨어 패턴에 대안 `|`가 없어서 스키마에는 `maxLength: 4`만 둔다. 인자를 둘로 나누면 판정이 생략한 인자를 마지막 성공 명령으로 채워(FR-09) 두 인자가 함께 오게 된다.
8. 화면 모드와 소리 모드는 전송 직전에 `supportedPictureModesMap`·`supportedSoundModesMap`을 읽고 ID의 이름을 고른다. 대응표가 `null`이면 `refresh` 뒤 한 번 더 읽는다. 확인은 현재 모드 값이 ID나 그 이름과 같으면 성공이다.
9. `launch_app`의 앱 표(`youtube` → 인자 `["", "YouTube"]`, 식별자 `9Ur5IzDKqV.TizenYouTube`)는 동작 표에 둔다. #270 실기 값이다.
10. `CommandResult.confirmation`은 `{"source": "smartthings_state", "capability", "attribute", "value"}`다. `changed`면 `before`도 담는다. 값은 감사에 남으므로 토큰과 `deviceId`를 담지 않는다.
11. core는 확인 근거를 결과 사건의 근거(`evidence`)에 `{"source": "server_state_confirmed", "confirmation": ...}`로 남긴다. 사건 키는 지금의 서버 결과 키 `server:{command_id}`를 쓴다.
12. 종류별 실행 응답 제한은 `Settings.exec_timeouts: Mapping[str, float]`에 두고 `exec_timeout_for(kind)`로 읽는다. 설정 파일에서는 문서의 이름 그대로 따옴표 키 `"exec_timeout_sec.display" = 10.0`로 쓴다. TOML은 숫자 `exec_timeout_sec`와 표 `exec_timeout_sec.display`를 함께 둘 수 없기 때문이다. 초기값 `display` 10초(`requirements.md` §1.2)는 `Settings`의 기본값이고, 설정 파일의 키는 그 위에 더한다. 평가 도구는 예시 설정의 `[settings]`를 `Settings`에 그대로 넘기므로 예시 설정에는 이 키를 주석으로만 둔다.
13. `ExecutionService`는 명령을 만들 때 저장한 기기의 종류로 제한 시간을 정한다. `send`에 넘기는 제한 시간은 그 명령의 `expires_at - issued_at`이다 — 한 시도 안에서 두 값이 어긋나지 않는다.
14. HTTP 오류 분류: 연결 실패, 401, 그 밖의 4xx는 "보내지 못함"(`NOT_SENT`)이다. 응답 시간 초과와 5xx는 "보냈을 수 있음"이라 확인 조회를 계속한다. 명령 응답 본문의 상태 값은 쓰지 않는다 (FR-07).
15. 접근 토큰 파일은 JSON 객체이고 `access_token` 필드를 읽는다. 요청마다 파일을 다시 읽는다 — 후속 OAuth 작업이 같은 파일을 교체 저장하면 재시작 없이 반영된다. PAT도 같은 형식으로 넣는다. `[smartthings]`의 키는 `token_file`, `confirm_poll_sec`(확인 조회 간격), `request_timeout_sec`(HTTP 요청 제한 시간), `[[smartthings.bindings]]`(`node_id`·`device_id`)이고 모두 필수다.
16. 동작 표가 바뀌어서 선언 합성(`descriptor.py`)도 새 표를 쓴다. `set_input_source`, `mute`, `unmute`는 표에서 빠진다. 합성은 계속 `switch`가 없으면 거부한다.
17. 펌웨어 동반 기기 선언 상수는 `DISPLAY_SMARTTHINGS_DESCRIPTOR`다. 빌드 env는 출력 핀이 없는 `esp32dev-display-1`이다. 펌웨어 실행 코드는 고치지 않는다 — 동반 기기에는 명령이 가지 않는다.
18. 동작 8개의 선언은 가장 긴 기기 ID(64바이트)로도 발표 2048바이트를 넘지 않게 줄인다 (구현 중 확인: 처음 설명으로는 2252바이트였다). 인자 없는 동작의 스키마는 `{"type": "object", "additionalProperties": false}`로 쓰고 빈 `properties`·`required`를 쓰지 않는다. 설명은 짧게 쓴다(예: `켜기`, `채널 번호나 up·down`). 서버 동작 표와 펌웨어 선언이 같은 값을 쓴다. 인자의 `required`는 줄이지 않는다 — 생략한 인자는 FR-09 규칙으로 채운다.
19. 명령을 보내기 전의 조회(대응표, `changed`의 전송 전 값)가 실행 응답 제한을 다 쓰면 보내지 않고 `NOT_SENT`다 — 보내지 않았으므로 확정할 수 있다.
20. 시뮬레이터(`tools/node_sim/device.py`)와 시험의 fake Paho(`tests/integration/_runtime.py`)는 펌웨어처럼 공백 없는 UTF-8 JSON으로 보고를 보낸다. 서버는 발표 상한을 이 바이트 수로 잰다. 기본 `json.dumps`(ASCII 이스케이프, 공백)로는 동반 기기 선언의 발표가 2228바이트라 서버가 거부했다.
21. `display`의 근접 임계값(`threshold_display`)은 `Settings` 기본값으로 두지 않는다. 운영 설정 `[settings]`에 적어야 근접 지정(`near:display:…`)이 실행된다. 없으면 명시 지정(`display-1`)만 된다 (FR-17, #245). 결정 12와 같은 이유로 예시 설정에는 주석으로만 둔다. 키가 없으면 판정 ② 후보 단계에서 `INVALID_INPUT`이다.
22. `equals`·`equals_other`의 "요청한 값과 같다"는 숫자(bool 제외)끼리는 값으로 비교한다. 15와 15.0은 같다. 그 밖에는 타입까지 같아야 한다 — 문자열 `"15"`는 15가 아니다. 상태를 확인하지 못해 `UNKNOWN`을 돌려주기 직전에 마지막 관측 값과 기대값을 warning 로그에 남긴다. 실기의 이상값을 진단하기 위해서다. 결과 근거(`evidence`)에는 넣지 않는다 — 근거는 core가 만든다 (#276).
23. 외부 API 기기(`[smartthings]` 바인딩의 `node_id`)의 공용형·서버 내부 실행에서 결과가 `unknown`·`failed`면 core는 기기를 `FAULT`로 격리하지 않는다 (#276 담당자 결정). 외부 기기는 점유가 없고 명령이 멱등이라 다시 보내도 위험이 작고, SmartThings 클라우드가 정상 동작에도 기대와 다른 값을 돌려주는 일이 있어서다. 결과는 성공으로 꾸미지 않고 `unknown`·`failed` 그대로 저장·응답한다.
    - 판정은 core가 한다: 조립(`runtime/assembly.py`)이 바인딩 목록을 `ExecutionService(non_isolating_nodes=...)`로 넘기고, core가 `ExecutionStore.record_result(..., isolate=False)`와 근거의 `"isolated": false`를 함께 정한다. iot는 결과를 바꾸지 않는다.
    - db는 `isolate=False`면 결과와 감사(`isolated: false`)만 남기고 사용분을 `consumed`로 둔다(전송을 시도했다). 기기 상태와 점유 세션은 건드리지 않는다.
    - 대여형 실행은 지금 규칙(격리)을 따른다. 재시작 복원(`restore.py`)과 통신·저장 장애의 `isolate_device`도 바꾸지 않는다.
24. `set_volume`·`set_mute`도 확인 조회마다 `refresh`를 먼저 보낸다 (2026-11-14 담당자 결정). display-1(M5) 실기에서 음량 명령은 실행됐지만 `audioVolume.volume`이 전날 값으로 남아 결과가 `unknown`이었다. `refresh`를 보내자 3초 안에 실제 값으로 바뀌었다. 성공 확인의 기준(속성이 요청한 값과 같다)은 그대로다 — `architecture.md`의 "상태 반영이 늦는 동작은 확인 조회 전에 `refresh.refresh`를 보낸다"를 따른다.

## 7. 가정

- `supportedPictureModesMap`·`supportedSoundModesMap`의 값은 `{"id", "name"}` 객체의 목록이라고 가정한다. #270은 ID와 이름의 대응을 기록했지만 원문 형식을 남기지 않았다. 다르면 실기 점검에서 fix로 고친다 — #270.
- 화면 모드의 현재 값(`pictureMode`)은 ID나 이름 중 하나라고 가정한다. 그래서 둘 다 성공으로 본다 — #270.
- 명령 응답 시간 초과는 SmartThings가 명령을 받았을 수 있는 상황으로 본다. 그래서 `NOT_SENT`로 확정하지 않는다 — FR-07.
- 화면·소리 모드 ID는 32자 이하 문자열이라고 가정한다(`maxLength: 32`). #270은 ID 형식을 남기지 않았다. 다르면 실기 점검에서 fix로 고친다 — #270.
- 결정 23은 `requirements.md` FR-07 "실행 실패나 결과 불명이면 서버는 기기를 `FAULT`로 격리한다"와 NFR-04의 예외다. docs 반영은 decision PR로 한다 (#276). 그 전까지 이 spec이 예외의 근거다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `tests/tools/test_firmware_descriptors.py`, `tests/iot/test_smartthings_descriptor.py`, `tests/iot/test_node_sim.py` | 펌웨어 동반 기기 선언과 서버 동작 표의 일치, 발표 크기, 새 표로 합성 |
| FR-05 | `tests/integration/test_smartthings_companion_flow.py` | 동반 기기가 거리 선택자로 실행된다 |
| FR-06 | `tests/iot/test_routing_gateway.py`, `tests/iot/test_smartthings_gateway.py`, `tests/iot/test_smartthings_http.py` | 라우팅, 미전송 분류, 상대 명령 재전송 금지 |
| FR-07 | `tests/iot/test_smartthings_gateway.py`, `tests/core/lifecycle/` 실행 결과 시험, `tests/shared/test_config.py` | 확인 방식 3종, `refresh`, 대응표, `confirmation` 수용, 종류별 응답 제한 |
| FR-20 | `tests/integration/test_smartthings_companion_flow.py` | 이탈하면 `off`를 SmartThings로 보낸다 |
| NFR-08 | `tests/iot/test_smartthings_http.py` | 토큰과 `deviceId`가 오류·로그에 남지 않는다 |
