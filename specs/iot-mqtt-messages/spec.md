# MQTT 계약 v1 페이로드 스키마

- 상태: 작성 중
- 담당: JM-220 (상태 관리)
- 요구사항: FR-04, FR-05, FR-06, FR-07, NFR-08
- 따르는 결정: #19, #31, #32, #37, #38

## 1. 왜

- 서버와 기기는 MQTT 계약 v1의 JSON 본문으로 통신한다 (architecture.md §5 MQTT 2절, 3절).
- 저장소에는 본문을 검증하고 `shared` 모델로 바꾸는 코드가 없다. `iot/messages/`는 비어 있다.
- 검증 코드가 없으면 아래 문제가 생긴다.
  - 필수 필드가 없거나 타입이 틀린 보고를 CORE에 넘길 수 있다.
  - 관측 시각을 서버 수신 시각으로 바꿔 넘길 수 있다.
  - 서버가 계정 정보나 점유 주체를 기기에 보낼 수 있다.
- 게이트웨이(`iot-mqtt-gateway`)와 시뮬레이터(`iot-node-sim`)는 이 스키마를 함께 쓴다.

## 2. 무엇

- 입력:
  - 기기 → 서버: 토픽 종류와 UTF-8 JSON 본문 (`announce`, `rssi`, `status`, `result`, `safety_check_result`)
  - 서버 → 기기: `NodeCommand`, `SafetyCheckRequest`
- 출력:
  - 기기 → 서버: `shared` 모델 (`NodeAnnounce`, `RssiFrame` 목록, `NodeStatus`, `NodeResult`, `SafetyCheckResult`) 또는 거부 사유
  - 서버 → 기기: `cmd`, `safety_check` 본문
- 바뀌는 상태: 없음. 순수 변환 함수다.
- 약속:
  - 계약 v1의 필수 필드, 타입, 값 범위를 모두 검사한다.
  - 거부한 본문은 모델로 만들지 않는다. 거부 사유를 구분해서 반환한다.
  - 관측 시각과 서버 수신 시각을 따로 담는다. 서버 수신 시각은 호출자가 넘긴다.
  - 서버가 만든 본문에는 사용자 ID, 외부 요청 ID, 승인 정보, 점유 주체가 없다.

## 3. 제약

- 모든 본문은 `schema_version: 1`과 `node_id`를 담는다. 지원하지 않는 버전은 거부한다 (architecture.md §5 MQTT 2절).
- 기기 → 서버 본문은 `session_id`, `seq`, `sent_at`을 담는다. `seq`는 1 이상의 정수다 (§5 MQTT 2절).
- 시각은 RFC 3339 UTC 문자열이다. 시간대가 UTC가 아닌 문자열은 거부한다 (§5 MQTT 2절).
- ID는 비어 있지 않은 문자열이다. `event_id`는 UUID 문자열이다 (§5 MQTT 2절, 3절).
- RSSI의 `uuid`는 하이픈을 포함한 소문자 UUID 문자열이다. `major`와 `minor`는 0~65535의 정수다. `rssi`는 유한한 수다 (§5 MQTT 3절).
- RSSI 프레임 하나에 같은 비콘 조합이 두 번 있으면 프레임 전체를 거부한다 (§5 MQTT 3절).
- 빈 `observations` 배열은 허용한다. 빈 배열은 빈 `RssiFrame` 목록이다 (§5 MQTT 3절).
- `physical_state`는 네 필드를 모두 담는다. `door_closed`, `light_on`, `sensor_fault`는 bool 또는 null이다. `actuator_state`는 `idle`, `busy`, `unknown` 중 하나다 (§5 MQTT 3절).
- `status`의 `safe` 필드는 모델에 옮기지 않는다 (architecture.md §4 "기기 보고의 인계", FR-07).
- 알 수 없는 추가 필드는 모델의 판정 필드에 옮기지 않는다. 진단 정보만 `detail`에 둔다 (§5 MQTT 2절).
- `result`의 `state`와 `error_code`는 바꾸지 않고 `NodeResult`에 담는다 (architecture.md §4 "기기 보고의 인계").
- `cmd`의 `action`은 기능에 맞아야 한다. `open`은 사물함, `on`과 `off`는 조명이다 (§5 MQTT 3절).
- `cmd`의 `expires_at`은 `issued_at`보다 뒤여야 한다. `safety_check`도 같다 (§5 MQTT 6절).
- 직렬화한 `cmd` 본문은 같은 `NodeCommand`에 대해 항상 같다. 재전송은 내용을 바꾸지 않는다 (§5 MQTT 4절).
- 스키마 코드는 `shared` 모델에만 의존한다. paho-mqtt와 CORE에 의존하지 않는다 (AGENTS.md 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/iot/messages/`
- `server/tests/iot/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `iot/mqtt/`, `core/`, `db/`, `api/`, `tools/node_sim/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| MQTT 계약 v1 본문 (architecture.md §5 MQTT 2절, 3절) | 제공 | 펌웨어, 시뮬레이터 |
| `NodeAnnounce`, `RssiFrame`, `NodeStatus`, `NodeResult`, `SafetyCheckResult` | 사용 (모델 생성) | 공동 |
| `NodeCommand`, `SafetyCheckRequest` | 사용 (본문 생성) | 공동 |
| `RejectCode` | 사용 | 공동 |

### 만들지 않는 것
- 브로커 연결, 구독, 발행 (`iot-mqtt-gateway`)
- 기기 식별, 현재 연결, 순번, 최신성, 중복 검사 (`iot-mqtt-gateway`). 이 검사에는 상태가 필요하다.
- 기기 쪽 명령 접수와 거부 (`iot-node-sim`)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 본문이 JSON 객체가 아니다 | 형식 오류로 거부한다 | `test_FR07_reject_non_object_payload` |
| `schema_version`이 1이 아니다 | 지원하지 않는 버전으로 거부한다 | `test_FR07_reject_unsupported_version` |
| 필수 필드가 없거나 타입이 틀렸다 | 필드 이름을 담아 거부한다 | `test_FR07_reject_missing_field` |
| 시각에 시간대가 없거나 UTC가 아니다 | 거부한다 | `test_FR07_reject_non_utc_time` |
| RSSI 프레임에 같은 비콘이 두 번 있다 | 프레임 전체를 거부한다 | `test_FR05_reject_duplicate_beacon_in_frame` |
| `major`가 65535를 넘거나 `rssi`가 무한대다 | 프레임 전체를 거부한다 | `test_FR05_reject_out_of_range_beacon` |
| `status`에 `safe: true`가 있다 | 모델에 옮기지 않는다 | `test_FR07_status_ignores_safe_field` |
| `result.state`가 `rejected`이고 `error_code`가 `DEVICE_BUSY`다 | 값을 바꾸지 않고 `NodeResult`에 담는다 | `test_FR06_result_keeps_device_busy` |
| `result.state`가 목록 밖의 값이다 | 거부한다 | `test_FR07_reject_unknown_result_state` |
| 사물함에 `on` 명령을 직렬화한다 | 직렬화를 거부한다 | `test_FR06_reject_action_capability_mismatch` |
| `expires_at`이 `issued_at`과 같거나 이르다 | 직렬화를 거부한다 | `test_FR06_reject_expired_command` |

## 6. 결정

## 7. 가정

- 미결: 시각 오차 허용값 (architecture.md §5 MQTT 6절). 스키마는 형식만 검사한다. 시각 범위 검사는 게이트웨이가 한다.
- 미결: `evidence`의 필수 항목 (architecture.md §4 "반납 검사와 종료"). 스키마는 `evidence`가 객체인지만 검사한다.
- 미결: 같은 `check_id`에 다른 내용이 온 충돌을 펌웨어가 보고하는 형식. 스키마는 이 형식을 정의하지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_parse_announce` | 기능과 점유 방식을 `NodeAnnounce`로 바꾼다 |
| FR-05 | `test_FR05_parse_rssi_frame` | 관측 하나가 `RssiFrame` 하나다. `frame_ts`와 `received_at`을 따로 담는다 |
| FR-06 | `test_FR06_serialize_cmd` | `cmd` 본문에 계정 정보가 없다. 같은 명령은 같은 본문이다 |
| FR-07 | `test_FR07_parse_result` | `state`, `error_code`, `event_id`, `occurred_at`을 그대로 담는다 |
| FR-07 | `test_FR07_parse_safety_check_result` | `check_id`, 검사 기준, `outcome`, `evidence`를 담는다 |
| FR-07 | `test_FR07_serialize_safety_check` | 검사 기준 ID와 버전, 대상 연결, 만료 시각을 담는다 |
| NFR-08 | `test_NFR08_outbound_has_no_account_fields` | 서버 → 기기 본문에 사용자 ID와 점유 주체가 없다 |
