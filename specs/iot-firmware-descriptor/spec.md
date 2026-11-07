# 참조 펌웨어·시뮬레이터의 기기 선언과 접속 설정 키

- 상태: 완료 (실기기 확인은 7절 가정)
- 담당: 작업 지시자 (펌웨어, 시뮬레이터, 기기 접속 설정 묶음, 앱 BLE 설정, 평가 설정 연결 구현)
- 요구사항: FR-04, FR-06, FR-07, FR-12A, NFR-10, NFR-12
- 따르는 결정: #245 (decision PR #246). `architecture.md` §4 "기기 자기 선언의 후속 반영"의 네 번째 단계다. 앞 단계는 `specs/iot-device-descriptor/spec.md`, `specs/core-device-actions/spec.md`, `specs/dashboard-device-descriptor/spec.md`다.

## 1. 왜

- 팀의 ESP32 펌웨어는 공개 규칙의 참조 구현인데, 아직 v2 `announce`(`capability`, `occupancy`, `controls`)를 보낸다. 서버는 전환 기간이라 v2를 템플릿으로 바꿔 받지만, v2 수신을 끝내려면 팀 기기가 v3 선언을 발표해야 한다.
- 펌웨어는 동작과 인자를 기능 이름(`capability`)으로 분기해 검사한다. 공개 규칙은 기기가 자기 선언으로 다시 검사하라고 한다.
- 기기 접속 설정(BLE·USB)에 `capability`와 `occupancy`가 남아 있다. 공개 규칙의 설정 키는 접속 정보와 Wi-Fi뿐이다.
- 시뮬레이터는 v2만 발표하고, 사물함 `close`와 인자를 받지 못한다. 서버가 보내는 v3 `cmd`를 시험할 기기가 없다.

## 2. 무엇

- 입력: 펌웨어 빌드 설정의 선언 선택, 서버의 v3 `cmd`(`action`, `arguments?`), 시뮬레이터 기기 목록의 `descriptor`, 평가 설정의 `kind`.
- 출력: 펌웨어와 시뮬레이터의 v3 `announce`(`descriptor`). 선언에 없는 동작의 `UNSUPPORTED_ACTION`, 인자 스키마 밖 인자의 `UNSUPPORTED_PARAMETER`. 접속 설정 묶음과 BLE·USB 설정 줄에서 `capability`·`occupancy`가 빠진다.
- 바뀌는 상태: 펌웨어의 명령 기록 내용에 `arguments`가 들어간다. 저장한 접속 설정의 `capability`·`occupancy`는 읽지 않는다.
- 약속: 팀 기기의 선언은 v2 템플릿과 선언 해시가 같다. 펌웨어를 올려도 소유자가 다시 신뢰하지 않아도 된다. `vendor`만 바뀌므로 서버는 신뢰를 유지하고 감사에 남긴다.

## 3. 제약

- `announce`와 `cmd`는 `schema_version` 3이다. 그 밖의 메시지(`status`, `rssi`, `result`, `time_request`, `time`, `occupancy`, `restart`)는 2를 유지한다 (`device-protocol.md` 머리말, `architecture.md` §5 MQTT 2절).
- 기기는 접속할 때마다 시각을 맞춘 뒤 선언을 retained로 발표한다. `announce` 전체는 2048바이트 이하다 (`device-protocol.md` 2·4절).
- 선언은 펌웨어에 내장한다. 서버는 기기의 동작이나 점유 방식을 기기에 넣지 않는다 (`device-protocol.md` 1절).
- 기기는 `action`이 자기 선언에 없으면 구동 전에 `UNSUPPORTED_ACTION`, `arguments`가 자기 선언의 인자 스키마를 벗어나면 `UNSUPPORTED_PARAMETER`로 거부한다 (FR-06, `architecture.md` §5 MQTT 3절).
- 명령 접수 순서와 기록 규칙(중복 조회 먼저, 내용 비교, `DEVICE_BUSY`, 거부 기록 보존, 시작 직전 재검사)은 그대로다. 같은 명령의 내용은 `node_id`, `action`, `target_session_id`, `issued_at`, `expires_at`, `arguments`다. `arguments`가 없는 명령과 있는 명령은 내용이 다르다 (FR-06, `architecture.md` §5 MQTT 4절).
- 서버는 `arguments`를 바꾸지 않고 보내고, 기기는 명령 내용만으로 출력을 정한다. 조명 채널 출력은 `색 채널 값 × brightness ÷ 255`(내림)다. 재생 중인 스피커는 새 `on`의 음량만 바꾸고 재생을 이어 간다 (`architecture.md` §5 MQTT 3절).
- 펌웨어의 출력 상한(`NP_LIGHT_RING_MAX_LEVEL`, `NP_SPEAKER_MAX_VOLUME`)은 선언과 별개로 출력 직전에 다시 지킨다 (NFR-10).
- 설정 키는 `node_id`, `mqtt_user`, `mqtt_password`, `wifi_ssid`, `wifi_password`, `mqtt_host`, `mqtt_port`다. `capability`와 `occupancy`를 두지 않는다 (`architecture.md` §5 "BLE 기기 설정").
- 기존 기기의 MQTT 계정, 포트, 비밀번호와 명령 기록은 그대로 둔다 (Gotcha).
- 시뮬레이터는 서버와 같은 PC 시계를 쓴다. 성공은 가상 제어 신호 출력 완료와 결과 저장이다 (FR-07).
- 외부 서비스 없이 독립 AP 안에서 동작한다 (NFR-12).

## 4. 경계

### 고치는 곳

- `firmware/` (`firmware/node_lab/`은 제외)
- `server/tools/node_sim/`, `server/tools/evaluation/`, `server/tools/node_lab/`의 `provision.py`·`setup.py`·`session.py`, `server/tools/final_demo.py`
- `server/src/nearpilot/shared/models.py`의 `ProvisioningBundle`, `server/src/nearpilot/runtime/provisioning.py`, `server/src/nearpilot/api/rest/nodes.py`
- `server/config/`의 예시·템플릿과 `README.md`
- `app/`의 BLE 설정 묶음과 설정 줄
- 각 영역의 테스트, `specs/firmware-contracts/spec.md`·`specs/iot-node-simulator/spec.md`·`specs/iot-ble-provisioning/spec.md`의 바뀐 부분 표시

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: 서버의 판정·IOT·DB·대시보드, `firmware/node_lab/` 시험 펌웨어, `server/tools/node_lab/`의 시험 펌웨어용 부분(`boot_check.py` 등). 시험 펌웨어는 v2 발표를 유지하고 v2 수신 종료 fix PR에서 정리한다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `announce` v3 `descriptor` | 제공 | 펌웨어·시뮬레이터 → IOT |
| `cmd` v3 `action`, `arguments` | 사용 | IOT → 펌웨어·시뮬레이터 |
| `ProvisioningBundle`, `GET /admin/nodes/{node_id}/provisioning` | 제공 | 상태 관리·API → 앱 |
| BLE·USB 설정 줄 (`CONFIG_SET`) | 사용 | 앱·`nearpilot provision` → 펌웨어 |
| `shared.descriptor`의 `parse_descriptor`, `validate_arguments`, `legacy_descriptor` | 사용 | shared → 시뮬레이터·평가·정합성 시험 |

### 만들지 않는 것

- 적합성 시험 도구 — 시뮬레이터를 참조 구현으로 맞춘 뒤 별도 이슈 (`architecture.md` §4)
- v2 발표 수신의 종료와 `firmware/node_lab/`의 v3 전환 — 팀 기기를 모두 올린 뒤 fix PR
- 실기기에 올린 발표·명령·거부 확인 — 담당자가 실기기 시험을 뒤로 미뤘다 (7절)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 내장 선언이 형식·이름·역할·스키마 규칙을 어김, 지원하지 않는 `pattern` | 부팅 때 거부. 발표하지 않고 시리얼에 사유를 남긴다 | `test_FR04_descriptor_load_rejects_invalid` |
| 발표 본문이 2048바이트를 넘음 | 발표하지 않고 시리얼에 남긴다 | `test_FR04_announce_body_within_limit` |
| v2 `cmd` | `INVALID_MESSAGE` | `test_FR06_command_version_three_only` |
| 선언에 없는 동작 | `UNSUPPORTED_ACTION` | `test_FR06_action_outside_descriptor_rejected` |
| 스키마 밖 인자 (범위, 타입, `pattern`, 모르는 키, `arguments`가 객체가 아님) | `UNSUPPORTED_PARAMETER` | `test_NFR10_arguments_outside_schema_rejected` |
| 인자 일부만 옴 (`required: []`) | 받는다. 생략한 밝기는 기본 밝기, 생략한 색은 흰색, 생략한 음량은 그대로 (결정 5) | `test_FR09_partial_arguments_actuation` |
| 소문자 색 `#00ff00` | 스키마를 통과하고 같은 색으로 출력 | `test_FR09_lowercase_color_accepted` |
| 키 순서만 다른 `arguments`로 재전송 | 같은 내용. 기존 결과만 보고 | `test_FR22_arguments_join_content` |
| 키만 다르고 값이 같은 `arguments` | 다른 내용. `COMMAND_ID_CONFLICT` | `test_FR22_arguments_join_content` |
| 저장한 설정에 `capability`·`occupancy`가 남음 | 무시하고 설정을 읽는다 | `test_FR04_stored_config_with_old_keys_loads` |
| BLE·USB로 `capability`를 넣음 | `CONFIG_REJECTED reason=unknown_key` | `test_FR04_config_keys_exclude_kind` |
| 시뮬레이터가 선언에 없는 동작·스키마 밖 인자를 받음 | `UNSUPPORTED_ACTION`·`UNSUPPORTED_PARAMETER` | `test_FR06_sim_rejects_outside_descriptor` |
| 시뮬레이터가 v2 `cmd`를 받음 | `INVALID_MESSAGE` | `test_FR06_expired_or_wrong_session_rejected` |
| 기기 목록의 `descriptor`가 템플릿 이름도 선언 객체도 아님 | 설정 오류 | `test_FR04_sim_descriptor_config` |
| 평가 설정에 옛 `capability` 키, 템플릿 없는 `kind`·`occupancy` 조합 | 설정 오류 | `test_NFR13_eval_config_rejects_bad_values` |
| 기기 목록에 옛 `capability`·`occupancy`나 시뮬레이터 `descriptor`가 있음 | 묶음은 접속 정보뿐이다. 다른 키는 무시한다 | `test_FR12A_fleet_source_ignores_device_kind` |
| 시뮬레이터 사물함이 `close`를 받음, 선언 객체의 인자를 받음 | 구동한다. 다른 인자로 같은 ID는 충돌 | `test_FR06_sim_accepts_close_and_arguments` |

## 6. 결정

1. 펌웨어 선언은 `firmware/lib/np_descriptor/np_descriptors.h`의 상수 문자열이다. 빌드 없는 PC 테스트도 같은 헤더를 읽는다. 빌드 환경이 `-D NP_DESCRIPTOR=<상수>`로 하나를 고른다 — `esp32dev-locker-1`은 사물함, `esp32dev-light-1`은 원형 LED 조명, `esp32dev-speaker-1`은 DFPlayer 스피커, 시리얼 출력만 하는 기본 `esp32dev`는 인자 없는 조명이다. 선언 내용은 같은 기기가 v2로 발표하던 제어 정보의 템플릿과 같다 — 선언 해시가 같아 다시 신뢰하지 않아도 된다. 정합성은 pytest가 헤더를 읽어 확인한다.
2. 펌웨어의 `vendor`는 `{"name": "nearpilot", "model": <종류>, "firmware": "v3"}`다 — 템플릿과 `firmware`만 달라 서버는 신뢰를 유지하고 감사에 한 번 남긴다.
3. 펌웨어는 부팅 때 내장 선언을 `lib/np_descriptor`로 읽어 형식 버전, 이름 규칙, 점유 방식, 역할, 허용 키워드, 인자 타입과 `pattern` 지원 여부를 검사한다. 실패하면 발표하지 않는다. 조명·스피커 빌드는 선언의 `maximum`이 펌웨어 출력 상한 이하인지도 확인한다 — 잘못 만든 선언이 서버의 신뢰 판단에 올라가지 않게 한다.
4. `pattern`은 펌웨어 안의 작은 정규식 부분집합으로 검사한다. `^`, `$`, 문자, `\` 이스케이프, `.`, `[...]`(범위, `^` 부정), 수량자 `?`, `*`, `+`, `{n}`, `{n,}`, `{n,m}`이다. 앵커가 없으면 JSON Schema대로 부분 검색이다. 부분집합 밖의 문법은 선언 검사에서 거부한다 — `std::regex`보다 플래시를 덜 쓰고 잘못된 패턴에서 멈추지 않는다 (담당자 결정).
5. 인자를 받는 기기는 생략한 인자를 이렇게 정한다. 조명 `on`: 밝기는 `NP_LIGHT_RING_LEVEL`, 색은 흰색. 스피커 `on`: 음량을 바꾸지 않는다 — `required`가 빈 선언이라 일부 인자도 유효하고, 공개 규칙은 생략한 인자의 출력을 기기가 정하게 한다.
6. 색 인자는 대·소문자 16진수를 모두 받는다 — 선언의 `pattern`이 둘 다 허용한다. 서버는 대문자로 보낸다.
7. 명령 기록의 내용은 v2와 같은 다섯 필드 뒤에 `arguments`가 있을 때만 키로 정렬한 `[키, 값]` 쌍의 배열을 붙인다 — 키 순서와 상관없이 같은 인자는 같고, 키 이름이 다르면 다르다. v2로 받은 기록과 v3 재전송은 내용이 달라 `COMMAND_ID_CONFLICT`가 된다. 펌웨어를 올리는 동안 같은 명령이 유효 시간 안에 다시 오는 일은 실제로 없다 (담당자 결정).
8. 펌웨어는 v3 `cmd`만 받는다. 서버는 기기가 마지막으로 발표한 버전으로 보내므로 v3 발표 뒤의 v2 `cmd`는 형식 오류다. `restart`는 v2를 유지한다.
9. 펌웨어 설정 키는 7개다. NVS에 남은 `capability`·`occupancy`는 읽지 않고 지우지도 않는다. 설정 줄의 `capability`는 모르는 키로 거부한다 — 앱과 서버를 같은 PR에서 바꾼다.
10. 시뮬레이터 기기 목록의 `descriptor`는 템플릿 이름(`locker`, `light`, `speaker`) 또는 선언 객체다. 템플릿 이름은 v2 시뮬레이터가 발표하던 것과 같은 선언(사물함 `rental`, 조명·스피커 `shared`, 인자 없음)이다 — 시뮬레이터를 v3로 바꿔도 기존 시험 DB의 신뢰가 유지된다. 시뮬레이터는 선언으로 동작과 인자를 검사하고(`shared.descriptor.validate_arguments`), 사물함 `close`도 받는다.
11. 평가 설정의 `[[nodes]]`는 `capability` 대신 `kind`다. `kind`와 `occupancy`는 템플릿이 있는 조합(사물함 `rental`, 조명·스피커 `shared`)만 받는다 — 평가의 가상 기기도 템플릿 선언을 발표한다.
12. 연결 시험 도구(`tools/node_lab`)의 `fleet.json`은 `descriptor` 템플릿 이름을 쓰고, USB 주입 값에서 `capability`·`occupancy`를 뺀다. `session.py`는 저장한 기기의 `kind`로 시험 대상을 확인한다 — PR ②에서 `NodeInfo.capability`가 빠졌는데 이 도구가 아직 읽고 있었다. Mosquitto가 없는 PC에서는 이 경로의 통합 시험이 건너뛰어져 드러나지 않았다 (구현 중 발견).
13. 기존 통합 시험의 v2 기기 흉내는 시뮬레이터 연결로 v2 발표를 보내서 한다. 조명 밝기·스피커 음량 흐름 시험은 시뮬레이터 선언을 인자 있는 템플릿으로 바꾸고 v3로 다시 발표한다 — 시뮬레이터는 v3 `cmd`만 실행한다 (결정 8과 같다).

## 7. 가정

- 실기기(light-1, speaker-1, 사물함)에 올린 v3 발표·명령·거부 확인은 담당자가 뒤로 미뤘다. 이 PR은 ESP32 빌드(네 빌드 환경)까지 확인했다.
- 펌웨어 PC 단위 테스트(`pio test -e native`)는 이 작업 PC에 C++ 컴파일러가 없어 실행하지 못했다. 담당자 PC나 ESP32(`pio test -e esp32dev -f test_common`)에서 실행해야 한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04, NFR-10 | `server/tests/tools/test_firmware_descriptors.py` | 펌웨어 헤더의 선언이 공개 규칙을 지키고, 선언 해시가 같은 기기의 v2 템플릿과 같다. 발표 2048바이트, `pattern`이 펌웨어 부분집합 안, 빌드 환경마다 선언 하나, 선언 상한과 출력 상한 |
| FR-04, FR-06, NFR-10 | `firmware/test/test_common/test_descriptor.cpp` | 선언 읽기, 인자 스키마 검사, 정규식 부분집합 |
| FR-06, FR-22 | `firmware/test/test_common/test_command.cpp` | v3 버전, 선언 기반 동작·인자 검사, 내용 비교, 출력 요청 |
| FR-04 | `firmware/test/test_common/test_config.cpp` | 설정 키 7개, 옛 키가 남은 설정 |
| FR-04, FR-06 | `server/tests/iot/test_node_sim.py` | 시뮬레이터 v3 발표, 선언 기반 검사, `close`, 기기 목록의 `descriptor` |
| FR-04, FR-06 | `server/tests/iot/test_node_sim_broker.py` | 실제 브로커에서 시뮬레이터의 v3 발표와 명령 (Mosquitto가 있을 때) |
| FR-04, FR-06, FR-09 | `server/tests/integration/test_descriptor_announce_flow.py`, `test_light_control_flow.py`, `test_speaker_volume_flow.py` | v3 시뮬레이터와 v2 발표 기기가 같은 서버에서 동작, 인자가 v3 `arguments`로 기기에 닿는다 |
| FR-04 | `server/tests/tools/test_node_lab_provision.py`, `test_node_lab_setup.py` | USB 주입 키 7개, 시험 폴더 `fleet.json`의 선언 |
| FR-12A | `server/tests/api/test_rest_node_provisioning.py`, `server/tests/integration/test_runtime_provisioning.py` | 묶음에 `capability`·`occupancy`가 없다 |
| FR-12A | `app/.../ConfigLinesTest.kt` | 설정 줄과 묶음 해석에 `capability`·`occupancy`가 없다 |
| NFR-10 | `server/tests/tools/test_eval_server_config.py` | 평가 설정의 `kind` |
