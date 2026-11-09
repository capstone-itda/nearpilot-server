# SmartThings capability 조회로 기기 선언 합성

- 담당: 작업 지시자 (iot 영역)
- 요구사항: FR-04, FR-09, NFR-07
- 따르는 결정: #259 (decision PR #261, #262)

## 1. 왜

- SmartThings 클라우드 기기(1차 대상: 삼성 스마트 모니터 M5)는 MQTT `announce`로 스스로 선언을 발표하지 못한다.
- 서버가 이 기기를 신뢰·판정 대상으로 다루려면 FR-04가 요구하는 `DeviceDescriptor`가 있어야 한다. 지금은 이 기기의 선언을 만들 방법이 없다.

## 2. 무엇

- 입력: SmartThings `GET /v1/devices/{deviceId}` 응답에서 뽑은 capability id 목록과 제작사 정보(이름, 모델, 펌웨어 버전 또는 없음).
- 출력: `DeviceDescriptor`(종류 `display`, 점유 방식 `shared`). 지원하는 capability만큼 동작을 담는다.
- 바뀌는 상태: 없음. 이 작업은 순수 함수만 만든다. DB 저장, `on_announce` 호출, 네트워크 호출은 다음 작업(`iot-smartthings-adapter`)이 맡는다.
- 약속: 반환한 `DeviceDescriptor`는 `shared/descriptor.py`의 `parse_descriptor`가 만든 값과 똑같은 검증을 통과한 값이다. 이 작업은 `shared`를 고치지 않고 그 검증을 그대로 쓴다.

## 3. 제약

- 종류 이름은 `display`다. 텔레비전과 모니터를 함께 가리킨다 (`architecture.md` §4 "SmartThings 실행 어댑터").
- 점유 방식은 `shared`다. `off`는 `role: deactivate`로 선언한다. 정확히 하나만 선언해야 한다 (`device-protocol.md` 3절).
- `switch` capability가 없으면 선언을 만들지 않고 거부한다. `deactivate` 동작(`off`) 없이는 `shared` 선언이 성립하지 않는다 (`device-protocol.md` 3절).
- 선언할 동작 이름은 이름 규칙(`^[a-z][a-z0-9_]*$`, 32바이트 이하)을 지킨다 (`device-protocol.md` 4절, #262).
- `audioVolume`, `audioMute`, `mediaInputSource` capability는 SmartThings 조회 결과에 있을 때만 그 동작을 선언에 더한다. 없으면 그 동작을 뺀다 (`architecture.md` §4 "선언 합성").
- `custom.picturemode`(`set_picture_mode`)는 1차 범위에 넣지 않는다. M5 실기 capability 조회로 확인되면 후속 작업이 더한다 (`requirements.md` FR-07).
- `custom.launchapp`, `samsungvd.remoteControl`은 선언하지 않는다. 확인할 상태가 없다 (`requirements.md` FR-07).
- `vendor.firmware`가 빈 값이면 고정 문자열 `unknown`을 쓴다. 빈 문자열은 선언 검증을 통과하지 못한다 (`requirements.md` FR-04).
- 선언 해시는 `kind`, `occupancy`, `actions` 전체로 계산한다. `vendor`는 넣지 않는다. 이 규칙은 `shared/descriptor.py`가 이미 구현하고 있으므로 이 작업은 그 함수를 그대로 부른다 (FR-04).
- 판정은 바꾸지 않는다. 이 작업은 판정 분기를 추가하지 않는다 (NFR-07).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/smartthings/` (새 폴더)
- `server/tests/iot/`

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `db/`, `api/`, 대시보드, OAuth, SmartThings REST 호출, `NodeCommandGateway`, CLI, `Settings`. 모두 `iot-smartthings-adapter`가 맡는다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `shared.descriptor.parse_descriptor`, `DeviceDescriptor` | 사용 | shared |

### 만들지 않는 것

- SmartThings `GET /v1/devices/{deviceId}` 실제 호출 — `iot-smartthings-adapter`
- `NodeEventSink.on_announce` 호출, `nearpilot smartthings link` CLI, OAuth — `iot-smartthings-adapter`
- `set_picture_mode` 동작 — M5 실기 확인 뒤 후속 fix

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `switch` capability 없음 | `UnsupportedDeviceError`(선언을 만들지 않음) | `test_FR04_missing_switch_capability_rejected` |
| `audioVolume`·`audioMute`·`mediaInputSource`가 모두 없음 | `on`·`off`만 선언 | `test_FR04_minimal_capabilities_declares_switch_only` |
| `audioVolume`만 있음 | `on`·`off`·`set_volume`만 선언 | `test_FR04_audio_volume_adds_set_volume` |
| 전체 capability(4개) 있음 | 동작 6개(`on`·`off`·`set_volume`·`mute`·`unmute`·`set_input_source`) 선언 | `test_FR04_full_capabilities_declares_all_actions` |
| capability id 목록에 모르는 값이 섞임(예: `custom.launchapp`) | 무시하고 나머지로 선언 | `test_FR04_unknown_capability_ignored` |
| capability id 중복 | 한 번만 반영 | `test_FR04_duplicate_capability_id_deduplicated` |
| `firmware`가 빈 문자열이거나 없음 | `vendor.firmware`가 `"unknown"` | `test_FR04_missing_firmware_defaults_to_unknown` |
| 선언 전체 검증(해시, 상한, 이름 규칙) | `parse_descriptor`와 같은 결과 | `test_FR04_result_passes_parse_descriptor_contract` |

## 6. 결정

1. 선언할 동작 이름은 `on`, `off`, `set_volume`, `mute`, `unmute`, `set_input_source`로 쓴다 (#262). SmartThings `capability.command` 매핑(`switch.on`, `audioVolume.setVolume`, `audioMute.mute`/`audioMute.unmute`, `mediaInputSource.setInputSource`)은 모듈 상수 테이블로 둔다. 실제 REST 호출(`iot-smartthings-adapter`)은 이 테이블을 그대로 읽는다 — 매핑을 두 곳에 중복해서 적지 않는다.
2. `mute`와 `unmute`는 토글 하나가 아니라 인자 없는 동작 둘로 나눈다. SmartThings `audioMute` capability의 명령이 `mute()`/`unmute()` 두 개이기 때문이다.
3. `off`에만 `role: deactivate`를 붙인다. `on`, `set_volume`, `mute`, `unmute`, `set_input_source`는 역할이 없는 일반 동작이다. `device-protocol.md` 3절 규칙대로 이 동작들은 모두 "새 점유를 시작하는 일반 동작"(공용형의 켜기 요청)으로 취급된다 — 조명의 "인자를 바꾸는 켜기 요청도 켜기 요청이다"(FR-20)와 같은 일반 규칙이고 이 작업이 새로 만드는 규칙이 아니다.
4. capability id 비교는 정확한 문자열 일치만 본다. 대소문자를 바꾸거나 접두사를 추론하지 않는다 — SmartThings capability id는 고정된 문자열이라 다른 처리가 필요 없다.
5. 지원하지 않는 capability id가 섞여 있어도 오류로 보지 않는다. 그 capability만 무시한다 — SmartThings 기기는 이 작업이 모르는 capability를 더 가질 수 있고, 그 존재 자체는 문제가 아니다.

## 7. 가정

- `set_volume`의 인자 스키마(`volume`: `integer`, `minimum` 0, `maximum` 100)는 SmartThings `audioVolume` capability의 공개 속성 범위를 따른다. M5 실기 조회(`architecture.md` §4 "구현 전 확인") 전에 세운 값이라 실제와 다를 수 있다. 다르면 fix PR로 고친다 — #259.
- `set_input_source`의 `inputSource` 인자는 `type: string`만 두고 `enum`을 넣지 않는다. 실제 입력 소스 목록을 M5 조회 전에는 모른다. 조회 뒤 `enum`을 채우는 것은 후속 fix다 — #259.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `tests/iot/test_smartthings_descriptor.py` | capability id 집합으로 선언 합성, `switch` 없으면 거부, 지원 capability만 동작에 반영, `vendor.firmware` 기본값 |
| FR-09 | `tests/iot/test_smartthings_descriptor.py` | 선언한 동작 이름이 이름 규칙을 지켜 `parse_descriptor`를 통과한다 |
