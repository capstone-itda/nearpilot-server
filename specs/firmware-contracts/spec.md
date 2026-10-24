# ESP32 펌웨어의 MQTT v1 연동과 실제 기기 검증

- 상태: 구현 중 (1~6단계 구현 완료. 2~4·6단계 실제 서버 연결 확인 완료. 센서·완료 근거와 7~9단계는 남음)
- 담당: 작업 지시자 (펌웨어). architecture.md §2의 팀 배정은 미결이다 (7절 가정).
- 후속 구현 브랜치: `feat/firmware-contracts`
- 요구사항: FR-04, FR-05, FR-06, FR-07, FR-20, FR-22, NFR-03, NFR-04, NFR-07, NFR-08, NFR-10, NFR-11, NFR-12, NFR-14
- 따르는 결정: docs에 반영한 #19, #37, #38, #61, #140, #141, #142, #143이다.

계약의 근거는 [제품](../../docs/product.md), [요구사항](../../docs/requirements.md), [아키텍처](../../docs/architecture.md)다.
문서의 권위 순서도 이 순서다. 닫힌 이슈와 PR 토론을 계약의 근거로 쓰지 않는다.
이 spec은 기존 참고 문서 다섯 개를 합친 계약이다. 구현은 단계별로 진행하고, 단계마다 6절 결정과 8절 테스트를 갱신한다.

## 1. 왜

서버 계약만으로는 실제 ESP32가 시각, 명령 기록과 센서 근거를 어떻게 제공하는지 확인할 수 없다.
실제 완료와 안전을 증명하지 못하면 서버는 성공과 반납 완료를 확정할 수 없다.
시뮬레이터의 가상 관측만으로는 실제 기기의 시각 오차, 센서 능력과 전원 차단 뒤 동작을 검증할 수 없다.

## 2. 무엇

- 입력: 서버의 MQTT `cmd`와 `safety_check`, BLE iBeacon 관측, 센서 관측, 폐쇄망 시각원과 실행 설정이다.
- 출력: `announce`, `rssi`, `status`, `result`, `safety_check_result`와 허가한 명령의 물리 동작이다.
- 바뀌는 상태: ESP32의 기기 연결 ID, 발행 순번, 시각 신뢰, 명령 기록, 구동 진행과 검사 기록이다.
- 약속:
  - 펌웨어는 ESP32에서 실행하는 프로그램이다. ESP32는 연결한 구동부를 제어하고 센서를 읽는다.
  - 펌웨어는 기존 MQTT v1으로 서버와 연결한다. 허가, 점유 해제와 장애 복구는 서버가 결정한다.
  - 펌웨어는 완료와 안전을 증명한 관측만 보고한다. 확인하지 못한 값을 성공이나 안전으로 만들지 않는다.
  - 펌웨어는 재접속과 재시작 뒤에도 같은 명령으로 다시 구동하지 않는다.
  - 검증하지 않은 시각, 센서 구성과 저장 방식은 미결로 남긴다.

### 시각 동기화

펌웨어는 폐쇄망 안의 시각원으로 동기화한다. 외부 인터넷을 필수로 두지 않는다.
기기별 최대 시각 오차는 실측한 뒤 배포 설정으로 관리한다. 검증 전 값은 미설정이다.
다음 상황에서 펌웨어는 새 명령을 `CLOCK_UNVERIFIED`로 거부한다.

- 아직 동기화하지 않았다.
- 시각원이 바뀌었다.
- 오차 한도 준수를 확인하지 못했다.

펌웨어는 최대 시각 오차를 고려해서 명령 유효 시간을 검사한다.
펌웨어는 물리 동작 직전에도 대상 연결과 유효 시간을 다시 검사한다.
서버도 시각 신뢰 검증 전에는 시각 기반 실행 허가와 안전 확인을 차단한다.

### 반납 근거

펌웨어는 `safety_check`를 받은 뒤 새로 측정한다.
펌웨어는 요청과 같은 `check_id`, 실제 적용한 기준 ID와 버전을 결과에 담는다.

| 기준 ID | v1의 필수 항목 |
|---|---|
| `locker-return` | `door_closed`, `locked` |
| `light-return` | `actuator_idle` |
| `speaker-return` | `actuator_idle` |

`evidence`의 각 항목은 bool인 `value`와 UTC 관측 시각 `observed_at`을 담는다.
필수 항목을 모두 측정하고 모두 참이면 `passed`다. 유효한 항목에 거짓이 있으면 `not_ready`다.
측정에 실패하면 `unknown`이다. 측정하지 못한 bool을 만들지 않는다.
검증 가능한 근거로 고장을 확인하면 기존 `fault` 결과를 쓴다.
일반 상태 보고와 명령 성공은 반납 검사를 대신하지 않는다. 점유 종료는 CORE가 결정한다.

### 실행 완료와 명령 기록

펌웨어는 물리 완료를 증명하고 결과를 저장한 기능만 `succeeded`로 보고한다.
실행 실패를 확인하면 `failed`다. 실행 여부나 완료 여부를 증명하지 못하면 `unknown`이다.
PUBACK, `accepted`, `running`과 제어 신호 송출은 물리 완료의 근거가 아니다.
IR 무드등의 관측 근거가 없으면 `light_on=null`이다. IR 송출만으로 성공을 보고하지 않는다.
스피커의 완료 검증은 관측 수단과 보고 필드를 계약한 뒤 활성화한다.

펌웨어는 접수 기록을 구동 전에 보존한다.
같은 `command_id`로 재접속과 재부팅 뒤 다시 구동하지 않는다.
재시작 뒤 완료를 증명하지 못하면 `unknown`을 보고한다. 자동으로 구동을 재개하지 않는다.
명령·반납 검사 기록은 가득 찼을 때 완료·만료 조건을 만족한 가장 오래된 기록부터 교체한다 (6절).

### 검사 기록과 오류

같은 `check_id`와 같은 요청이 오면 펌웨어는 기존 검사 결과를 반환한다. 새 측정을 시작하지 않는다.
같은 `check_id`의 내용이 다르면 펌웨어는 새로 검사하지 않고 기존 결과를 보존한다.
서버는 충돌 요청의 응답 기한 초과를 확인 불능으로 취급한다.
미지원 기준과 측정 실패는 통과나 정상 대기로 보고하지 않는다.
필수 근거가 없는 결과는 서버가 확인 불능으로 취급한다.
첫 구현은 기존 v1의 확인 불능 경로를 쓴다. 검사 오류 보고 확장을 선행 조건으로 두지 않는다.

### 구현 단계

하드웨어가 ESP32뿐인 동안에는 모든 장치에 공통인 부분만 구현한다.
장치별 구동부·센서와 완료 근거는 하드웨어와 #142 결정 뒤에 4단계 인터페이스에 붙인다.

| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | node_lab에서 실제 기기로 검증한 공통 동작을 `firmware/`의 모듈로 옮기고 단위 테스트 환경을 만든다 | 완료 (8절) |
| 2 | 명령 기록의 상태 전이, 전원 차단 복원, 충돌, 저장 실패와 용량 | 완료 (8절). 2026-10-24 실제 연결에서 재보고 확인 |
| 3 | 장치 기능·점유 방식의 USB 설정과 기능별 동작 검증 | 완료 (8절). 2026-10-24 재주입 뒤 실제 발표·명령 확인 |
| 4 | 구동부·센서 인터페이스와 증명된 성공·실패·불명 결과 | 공통 틀 구현·검증 완료 (8절). 실제 세 기능은 관측 없음과 성공 보고 꺼짐 |
| 5 | `safety_check` 검사 기록, 근거 모델, v1 기준 표와 결과 규칙 | 공통 구현·검증 완료 (8절). 실제 센서 능력은 미검증 |
| 6 | 센서 인터페이스로 만드는 상태 보고 | 공통 구현·검증 완료 (8절). 센서 없는 항목은 명령 처리 상태로 보고 (6절) |
| 7 | 연결 안정성: MQTT 끊김 원인과 watchdog | 구현·실기기 확인 완료 (8절). BLE 표본 감소와 근접 품질의 관계는 남음 |
| 8 | 장시간 시각 오차 측정과 시각 재요청 검증 (#140) | 대기 |
| 9 | 서버와 합친 시나리오 테스트와 검증 보고 | 대기 |

1단계는 동작을 바꾸지 않는다. 시리얼 설정 명령과 로그 형식은 node_lab과 같다.
그래서 `server/tools/node_lab`의 `provision`과 `boot_check`를 그대로 쓴다.
기기 기능은 node_lab처럼 `light`·`shared`로 고정한다. 3단계에서 설정으로 바꾼다.

2단계는 명령 처리를 `np_executor` 모듈로 옮기고 다음을 바꾼다. 시리얼 로그 형식은 유지한다.

- 새 명령 기록은 기록 전용 NVS 파티션에 쓴다 (6절). node_lab이 기본 NVS에 남긴 기록도 계속 조회한다.
- 접수를 저장한 뒤 재검사에서 걸리면 구동하지 않고 `failed`와 그 거부 코드를 보고한다.
  구동하지 않았음을 확인한 실행 실패이기 때문이다 (architecture.md §5 MQTT 5절). 1단계는 `unknown`이었다.
- 기록 하나는 결과의 오류 코드까지 붙여 512바이트 이하다. 넘는 명령은 기록하지 않고 `INVALID_MESSAGE`로 거부한다.
  같은 내용은 언제나 같은 이유로 거부되므로 기록 없이도 재전송에 같은 결과를 준다.
- 명령 슬롯은 400개다. 빈 슬롯이 없으면 6절의 교체 조건을 적용한다. 교체할 기록이 없으면 `STORAGE_ERROR`다.
- 부팅 때 모든 슬롯을 한 번 읽어 명령 ID 해시 색인을 만든다. 조회는 해시가 같은 슬롯만 읽는다.
  읽을 수 없는 기록이 있으면 조회를 `STORAGE_ERROR`로 막는다. 쓰기에 실패하면 색인을 다시 만든다.
- `running`은 발행만 하고 저장하지 않는다. 재시작 뒤 `accepted`로 남은 기록은 모두 `unknown`으로 바꿔 보고한다.
- 결과 저장에 실패하면 결과를 발행하지 않는다. 기록은 `accepted`로 남고 다음 연결의 재보고에서 `unknown`이 된다.

## 3. 제약

- 기기는 연결마다 기능과 점유 방식을 retained `announce`로 발표한다 (FR-04).
- 기기는 비콘 식별자와 RSSI를 약 1초 프레임으로 보고한다 (FR-05).
- 기기는 서버가 허가해서 보낸 `cmd`로만 구동한다 (FR-06, NFR-07).
- 펌웨어는 기존 명령 기록을 연결·만료 검사보다 먼저 조회한다 (FR-06, FR-22, architecture.md §5 MQTT 4절).
- 같은 명령 ID의 내용이 다르면 `COMMAND_ID_CONFLICT`로 거부하고 기존 기록을 보존한다 (FR-06).
- 명령 내용의 비교 필드는 `node_id`, `action`, `target_session_id`, `issued_at`, `expires_at`이다 (FR-06, architecture.md §5 MQTT 4절).
- 다른 명령을 구동 중이면 새 명령을 대기열에 넣지 않고 `DEVICE_BUSY`로 거부한다 (FR-06, NFR-03).
- 펌웨어는 진행 중 확인, 접수와 구동 시작을 직렬화한다 (NFR-03, architecture.md §5 MQTT 4절).
- 펌웨어는 거부한 명령의 ID, 내용과 결과도 보존한다. 재전송에는 같은 결과를 반환한다 (FR-06).
- 접수 기록 저장을 확인하지 못하면 구동하지 않는다. 기존 `STORAGE_ERROR` 거부를 쓴다 (FR-06, architecture.md §5 MQTT 4절).
- 새 명령은 버전, 대상 연결, 기능별 동작과 유효 시간 검사를 통과해야 한다 (FR-06).
- 허용 오차 밖의 미래 발행 시각과 `issued_at >= expires_at`인 명령을 거부한다 (FR-06, architecture.md §5 MQTT 6절).
- 결과 재보고는 기존 사건 ID, 상태와 발생 시각을 유지한다. 발행 순번과 발행 시각은 새로 부여한다 (FR-07).
- 반납 검사 재보고는 기존 사건 ID, 검사 결과와 검사 시각을 유지한다 (FR-07, FR-20).
- `door_closed`, `light_on`, `sensor_fault`는 관측하지 못하면 `null`이다. 구동부 관측이 불명이면 `actuator_state=unknown`이다 (FR-07, architecture.md §5 MQTT 3절).
- 펌웨어의 검사 통과는 서버의 점유 해제와 `FAULT` 복구를 대신하지 않는다 (FR-20, NFR-04).
- 리드 스위치의 문 닫힘 관측만으로 `locked`를 증명했다고 추정하지 않는다 (FR-07, FR-20, architecture.md §4).
- 펌웨어는 실행 결과의 `RejectCode`를 검사 결과에 추가하지 않는다 (FR-07, architecture.md §5 MQTT 3절).
- ESP32와 NimBLE를 쓴다. 시연 장비는 NFR-10의 사물함, LED 조명, 스피커와 IR 무드등을 따른다 (NFR-10, NFR-14).
- MQTT 3.1.1, UTF-8 JSON, 일곱 토픽의 QoS와 retain은 기존 계약을 따른다 (NFR-11, architecture.md §5 MQTT 1~3절).
- MQTT 접속마다 새 UUID인 `session_id`를 만든다. 연결·토픽별 `seq`는 1부터 증가한다 (FR-07, architecture.md §5 MQTT 2절).
- `clean_session=true`로 접속하고 자기 `cmd`와 `safety_check`를 매번 구독한다 (NFR-08, NFR-11).
- 기기별 자격정보와 접속 설정은 실행 설정에서 받는다. 비밀번호를 저장소와 로그에 넣지 않는다 (NFR-08).
- 기기는 독립 AP 안에서 동작한다. 행사장 Wi-Fi와 외부 시각 서버를 필수로 두지 않는다 (NFR-12).
- 시간 값과 임계값은 이름, 단위와 허용 범위가 있는 설정으로 받는다. 미측정 수치를 상수로 넣지 않는다 (requirements.md §1.2).
- 펌웨어는 대여형을 이탈과 대여시간 초과만으로 자동 반납하지 않는다. 켜진 시간만으로 공용형을 끄지 않는다 (FR-20).

## 4. 경계

### 고치는 곳

- `specs/firmware-contracts/spec.md`다.
- `firmware/`다. `firmware/node_lab/`은 `specs/core-node-lab/spec.md`의 범위라서 고치지 않는다.
- 루트 `.gitignore`의 펌웨어 빌드 경로다.

### 고치지 않는 곳

- 항상: `AGENTS.md`의 Gotchas 밖, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 담당자가 2026-10-24에 승인한 기록 교체 계약은 `docs/requirements.md`와 `docs/architecture.md`에 직접 반영한다. 이슈는 만들지 않는다.
- 이 작업: 서버의 IOT 구현, CORE, DB, API, 기기 시뮬레이터, 앱과 대시보드다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| MQTT v1의 `cmd`, `safety_check` | 서버 요청을 받는다 | IOT |
| MQTT v1의 다섯 기기 보고 | 기능, RSSI, 관측, 실행 결과와 검사 결과를 제공한다 | IOT |
| 반납 검사 v1 | 기준 ID·버전별 근거를 제공한다. 종료는 서버가 결정한다 | IOT를 거친 CORE |
| 폐쇄망 시각원과 기기별 최대 오차 | 동기화와 실측 근거를 제공한다. 설정 연결은 #140의 미결을 따른다 | 서버 배포 |

MQTT 연결 구현은 [게이트웨이 spec](../iot-mqtt-gateway/spec.md)이 맡는다.
IOT의 전체 분할은 [구현 계획](../iot-mqtt-gateway/plan.md)을 따른다.

### 만들지 않는 것

- 장치별 구동부·센서 구현과 동작별 완료 근거다. 하드웨어 도착과 #142 결정 뒤에 다룬다.
- 서버의 허가 판정, 점유 종료, 승인 사용분 정산과 소유자 복구 승인이다.
- 새 MQTT 필드, 검사 기준 버전, 검사 오류 코드와 진단 토픽이다.
- 임의의 센서 정확도, 최대 시각 오차와 재동기화 기본값이다.
- 실제 잠금장치, 고보안 출입문, iOS와 디스펜서다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 필수 필드 누락, 타입 오류나 미지원 버전이다 | 구동하지 않는다. 읽을 수 있는 명령 ID에는 기존 거부 형식을 쓴다 | `test_FR06_invalid_message_no_actuation` |
| 서버가 아닌 주체가 명령을 발행한다 | 브로커가 차단한다. 펌웨어는 자기 명령 토픽만 구독한다 | `test_NFR08_only_server_commands` |
| 두 명령이 동시에 온다 | 한 명령만 구동한다. 나머지는 접수와 구동 전에 `DEVICE_BUSY`로 거부한다 | `test_FR06_concurrent_commands_busy` |
| 같은 명령이 재접속·재시작 뒤 다시 온다 | 연결·만료 검사 전에 기존 기록을 찾는다. 다시 구동하지 않는다 | `test_FR06_duplicate_after_restart` |
| 같은 명령 ID의 내용이 다르다 | `COMMAND_ID_CONFLICT`로 거부한다. 기존 기록을 보존한다 | `test_FR06_command_id_conflict` |
| 시각을 검증하지 못했다 | 새 명령을 `CLOCK_UNVERIFIED`로 거부한다. 안전 확인 근거로 쓰지 않는다 | `test_FR07_unverified_clock_blocks_execution` |
| 대상 연결이 다르거나 구동 직전에 만료한다 | 물리 동작을 시작하지 않는다 | `test_FR06_session_and_expiry_rechecked` |
| 구동 전 접수 기록 저장이 실패한다 | `STORAGE_ERROR`로 거부하고 구동하지 않는다 | `test_FR06_receipt_storage_failure` |
| 명령 도중이나 완료 직전에 전원이 꺼진다 | 기록을 복원한다. 완료를 증명하지 못하면 `unknown`을 보고하고 자동 재개하지 않는다 | `test_FR07_power_loss_restores_unknown` |
| 제어 신호만 송출했거나 결과 저장에 실패한다 | `succeeded`를 보고하지 않는다. 완료를 증명하지 못하면 `unknown`이다 | `test_FR07_no_completion_without_evidence` |
| 같은 검사 ID와 같은 요청이 다시 온다 | 기존 결과를 반환한다. 새 측정을 시작하지 않는다 | `test_FR20_duplicate_check_reuses_result` |
| 같은 검사 ID의 내용이 다르다 | 검사하지 않고 기존 결과를 보존한다. 서버는 응답 기한 초과로 취급한다 | `test_FR20_check_id_conflict` |
| 검사 기준이 미지원이거나 센서를 측정하지 못한다 | bool 근거를 꾸미지 않는다. 기존 확인 불능 경로를 유지한다 | `test_FR20_missing_evidence_unconfirmed` |
| 필수 근거가 모두 참이거나 유효한 거짓이 있다 | 각각 `passed`와 `not_ready`로 보고한다. 점유 종료는 서버에 맡긴다 | `test_FR20_fresh_v1_evidence` |
| 검증 가능한 근거로 고장을 확인했다 | 기존 `fault` 결과를 보고한다 | `test_FR07_verified_fault_report` |
| 명령이나 검사 결과를 다시 발행한다 | 사건 ID와 사건 내용을 유지한다. 발행 순번과 발행 시각만 새로 부여한다 | `test_FR07_result_event_identity` |
| 외부 인터넷이나 시각원이 끊긴다 | 외부 인터넷 없이 연결한다. 시각 신뢰를 확인하지 못하면 새 구동을 막는다 | `test_NFR12_local_time_source_disconnect` |
| USB 설정 값이 없거나 범위·문자 규칙을 어긴다 | 저장하지 않고 처음 어긴 키 이름으로 거부한다 | `test_NFR08_config_rejects_invalid_values` |
| 명령의 시각 문자열이 UTC 형식이 아니거나 없는 날짜다 | `INVALID_MESSAGE`로 거부한다 | `test_FR06_rejects_malformed_utc` |
| 명령 기록이 다른 `node_id`의 것인데 설정의 `node_id`를 바꾼다 | 설정을 거부하고 기록을 그대로 둔다 | `test_FR22_ledger_owner_conflict` |
| 센서가 없는 기기가 상태를 보고한다 | 명령 처리 상태로 `idle`·`busy`를 보고하고 `sensor_fault=false`다. 센서가 있으면 센서 값을 쓴다 | `test_FR07_status_without_sensors_uses_command_state` |
| 기록 슬롯이 모자란다 | 완료·만료된 가장 오래된 기록을 교체한다. 교체 대상이 없으면 새 처리를 막는다 | `test_FR22_full_ledger_rejects_without_eviction`, `test_FR22_replaces_oldest_expired_terminal` |
| 명령 기록이 크기 한도를 넘는다 | 기록하지 않고 `INVALID_MESSAGE`로 거부한다. 구동하지 않는다 | `test_FR06_invalid_message_no_actuation` |
| node_lab이 남긴 기록의 명령이 다시 온다 | 기존 기록으로 처리하고 다시 구동하지 않는다 | `test_FR22_legacy_records_block_reexecution` |
| 구동 시작이 실패하거나 관측이 실행 실패를 확인했다 | `failed`를 저장한 뒤 보고한다 | `test_FR07_verified_execution_failure` |
| 완료를 증명했지만 기능별 성공 보고가 꺼져 있다 | `unknown`으로 보고한다 | `test_FR07_completion_requires_enabled_capability` |
| 유효한 완료 근거와 결과 저장을 확인했다 | `succeeded`로 보고한다 | `test_FR07_proven_completion_saved_before_success` |
| 완료 관측이 없거나 오류가 나거나 기한이 없다 | 즉시 `unknown`이다 | `test_FR07_unavailable_observation_is_unknown` |
| 완료 대기 기한을 넘겼다 | `unknown`이며 바쁨을 푼다 | `test_FR07_completion_deadline` |
| 관측이 시작 전이거나 다른 명령이거나 출처가 없다 | 완료 근거로 쓰지 않는다 | `test_FR07_completion_evidence_freshness` |
| 구동 중 같은 명령이 다시 오거나 재접속한다 | 접수를 재보고하고 현재 구동을 유지한다 | `test_NFR03_at_most_once_actuation` |
| 실제 기능에 완료 관측이 없다 | 모든 기능의 성공 보고를 꺼 두고 신호 뒤 `unknown`이다 | `test_NFR10_device_slots_have_no_completion` |

3단계의 사례와 테스트는 다음과 같다.

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_announce_uses_configured_capability` | 모든 기능·점유 방식의 설정 값을 발표 본문에 넣는다 |
| FR-06 | `test_FR06_capability_action_matrix` | 기능별 허용 동작과 검사 순서를 확인한다 |
| NFR-08 | `test_NFR08_config_rejects_invalid_capability` | 기능 누락·타입·허용 값 오류를 키 이름으로 거부한다 |
| NFR-08 | `test_NFR08_config_rejects_invalid_occupancy` | 점유 방식 누락·타입·허용 값 오류를 키 이름으로 거부한다 |
| FR-22 | `test_FR22_changed_capability_preserves_duplicate` | 기능 변경 뒤에도 기존 결과를 재보고하고 구동하지 않는다 |

## 6. 결정

- 기존 참고 문서 다섯 개를 이 spec으로 합친다 — 담당자가 2026-10-16에 #151의 문서를 spec으로 바꾸도록 지시했다.
- 개발 환경, 센서와 저장 방식은 미결로 남긴다 — 담당자가 아직 구성을 정하지 않았다고 답했다.
- 로컬 연결 시험은 `firmware/node_lab/`에서 PlatformIO·Arduino-ESP32·NimBLE·NVS를 쓴다 — 담당자가 2026-10-22에 ESP32만 쓰는 최소 시험 코드를 요청했다. 기본 출력은 시리얼이고 내장 LED GPIO는 선택이다. 운영 센서·완료 근거와 펌웨어 전체의 미결은 유지한다. 범위는 `specs/core-node-lab/spec.md`다.
- 개발 환경은 node_lab에서 실제 기기로 검증한 구성을 쓴다 — 담당자가 2026-10-23에 골랐다. PlatformIO `espressif32@6.12.0`, Arduino-ESP32, NimBLE-Arduino 2.5.0, 256dpi MQTT 2.5.2, ArduinoJson 7.4.2다. 위의 "개발 환경 미결"을 대체한다. 센서와 저장 방식은 계속 미결이다.
- 제품 펌웨어는 `firmware/` 루트의 PlatformIO 프로젝트다 — 담당자가 2026-10-23에 골랐다. node_lab은 시험 도구로 남긴다. 두 프로젝트는 같은 USB 설정 명령과 시리얼 로그 형식을 쓴다.
- 모듈은 `firmware/lib/`의 순수 C++ 모듈과 장치 인터페이스로 나눈다 — 담당자가 2026-10-23에 골랐다. 모듈은 Arduino 헤더를 쓰지 않는다. 저장소는 `RecordStore` 인터페이스로 넣고 테스트는 fake를 쓴다. `src/main.cpp`는 Wi-Fi, MQTT, BLE, NVS와 시리얼을 모듈에 연결한다.
- 단위 테스트는 Unity로 쓰고 같은 테스트를 두 환경에서 실행한다 — 담당자가 2026-10-23에 골랐다. `pio test -e esp32dev`는 실제 ESP32에서, `pio test -e native`는 PC에서 실행한다. native는 호스트 C++ 컴파일러가 필요하다. 테스트 이름에 요구사항 ID를 쓴다.
- 브랜치는 `feat/core-node-lab` 위에서 나눈다 — 담당자가 2026-10-23에 골랐다. 이 브랜치의 PR은 core-node-lab PR 뒤에 올린다.
- 명령 기록은 기록 전용 NVS 파티션에 저장한다 — 담당자가 2026-10-23에 골랐다. 위의 "저장 방식 미결"을 대체한다.
  - 기본 NVS 20KB에는 기록이 약 35개만 들어가서 설정한 64개를 다 담지 못했다.
  - `firmware/partitions.csv`에서 spiffs를 줄여 256KB짜리 `np_records` 파티션을 만든다. 앞쪽 파티션의 위치는 그대로다.
  - 기록은 `np-records` 이름 공간의 키 하나에 하나씩 쓴다. NVS 키 쓰기는 전원이 꺼져도 이전 값이나 새 값 중 하나만 남는다.
  - 용량은 슬롯 수와 기록 크기 한도로 정한다. NVS의 빈 항목 수는 쓰지 않는다.
    실측에서 빈 항목이 440개 남았는데도 기록 하나를 다시 쓰지 못했다. 가득 찬 페이지 끝의 빈 항목이 기록 하나보다 작았다.
  - 512바이트 기록은 18항목이라 126항목 페이지에 7개 들어간다. 64페이지에서 정리용 1페이지와 재작성용 1페이지를 빼면 434개다. 슬롯은 400개로 둔다.
  - 설정은 기본 NVS의 `np-config`에 남는다. node_lab의 `np-node-lab` 기록은 읽고, 이미 있는 기록만 제자리에서 갱신한다.
  - 파티션을 열지 못하면 지우지 않고 `storage_unavailable`로 둔다.
- 명령 처리 상태 전이는 `firmware/lib/np_executor/`에 둔다 — 담당자가 2026-10-23에 골랐다. 검사, 결과 발행, 구동과 시각·ID 생성은 `CommandPort` 인터페이스로 받는다. 테스트는 fake를 쓴다.

- 3단계 설정은 `capability`와 `occupancy`를 필수 문자열로 받는다 — 담당자가 2026-10-23에 확정했다. 기존 설정은 재주입을 요구한다. 기본값을 채우거나 저장된 설정을 지우지 않는다. 재주입 전에는 설정 무효로 새 명령을 받지 않는다.
- 기록이 있어도 기능과 점유 방식 변경을 허용한다 — 담당자가 2026-10-23에 확정했다. `node_id` 변경 차단과 기존 기록 우선 조회를 유지한다. 같은 명령 ID는 변경 후에도 기존 결과만 재보고한다. 서버의 신뢰 재설정은 FR-04를 따른다.
- USB 키 목록만 확장한다 — 담당자가 2026-10-23에 확정했다. 주입 도구 변경은 경계 밖이라 멈추고 이슈 초안을 전달했다. 이슈는 생성하지 않는다. node_lab은 추가 키가 든 설정을 읽지만 발표는 고정이며 설정 재저장 시 추가 키를 없앤다. node_lab은 수정하지 않는다.
- 3단계에서도 모든 기능의 제어 신호 뒤 결과는 `unknown`이다 — 담당자가 2026-10-23에 작업 범위를 정했다. `open`은 시리얼과 선택 GPIO 신호만 낸다. 스피커 완료 검증은 활성화하지 않는다.

- 4단계 명령은 비동기 상태 기계로 처리한다 — 담당자가 2026-10-23에 추천안을 모두 골랐다. `handle()`은 접수와 구동 시작을 처리한다. `loop()`는 짧게 진행과 완료를 확인한다. MQTT 유지와 BLE 관측 보고를 계속한다. 구동 시작을 확인한 뒤에만 `running`을 발행한다. 판정이 끝날 때까지 새 명령은 `DEVICE_BUSY`로 거부한다.
- `running`은 계속 발행만 한다 — 담당자가 2026-10-23에 골랐다. 재시작 뒤 접수 기록은 `unknown`으로 복원한다. 자동 재구동하지 않는다. 같은 실행 중 재접속은 복원과 구분하고 현재 접수를 불명으로 바꾸지 않는다.
- 완료 대기 기한은 기능별 빌드 설정으로 받는다 — 담당자가 2026-10-23에 골랐다. 단위는 밀리초다. 0은 미설정이며 양수만 대기 기한이다. 단조 시각으로 경과 시간을 잰다. 미측정 기본값을 넣지 않는다. 기한 또는 완료 관측이 없으면 즉시 `unknown`이다. USB 설정과 주입 도구는 바꾸지 않는다.
- 시작 전 완료 관측은 인정하지 않는다 — 담당자가 2026-10-23에 보수적인 가정을 골랐다. #142의 구체 완료 조건은 미결이다. 관측은 명령 ID, 단조 관측 시각과 근거 출처를 담는다. 시작 뒤의 명령별 근거만 검사한다. 시각만 새롭다는 이유로 완료를 인정하지 않는다.
- 구동 시작과 완료 관측은 `firmware/lib/np_device/`의 Arduino 없는 장치 인터페이스로 받는다 — 4단계의 구동부는 시작 성공·실패·불명을 구분한다. 관측은 대기, 완료 증명, 실패 확인과 확인 불가를 구분한다. 관측 오류와 기한 초과는 `unknown`이다. 시작 실패 또는 유효한 실패 근거만 `failed`다. 완료 증명은 기능별 활성화와 결과 저장 확인 뒤에만 `succeeded`다.
- 실제 `locker`, `light`, `speaker`는 완료 관측을 제공하지 않는다 — 4단계에서도 기존 시리얼·선택 GPIO 신호만 낸다. 성공 보고는 기능별로 모두 꺼 둔다. 문 닫힘과 IR 송출을 완료 근거로 만들지 않는다.

- 5·6단계는 별도 순수 C++ 검사 모듈과 공통 센서 인터페이스로 구현한다 — 담당자가 2026-10-24에 골랐다. 반납 검사는 요청 뒤 새로 측정한다. 상태 보고는 명령 처리 상태를 물리 관측으로 바꾸지 않는다. 실제 센서는 미설치 상태다.
- 상태 보고는 센서가 없는 항목을 펌웨어 상태로 채운다 — 담당자가 2026-10-24에 골랐다. 위 결정의 상태 보고 부분을 대체한다.
  - 실기기 시험에서 서버 복원은 `actuator_state=idle`과 `sensor_fault=false`인 신선한 상태만 정상으로 봤다 (`server/src/nearpilot/core/lifecycle/restore.py`). 센서 없는 기기는 `unknown`·`null`을 보고해서 실행 가능이 되지 않았다.
  - 구동부를 관측하는 센서가 없으면 `actuator_state`는 펌웨어의 명령 처리 상태다. 처리 중이 아니면 `idle`, 처리 중이면 `busy`다. 구동부가 실제로 멈췄다는 근거는 아니다.
  - 고장을 검출하는 센서가 없으면 `sensor_fault=false`다. 고장 날 센서가 없다는 뜻이다.
  - 센서가 있는 항목은 센서 값을 쓴다. 센서를 읽지 못하면 `unknown`·`null`이고 펌웨어 상태로 채우지 않는다.
  - 반납 검사의 근거(`actuator_idle`)에는 쓰지 않는다. 반납 검사는 계속 요청 뒤의 새 측정만 쓴다.
  - architecture.md §5 MQTT 3절의 "확인할 수 없는 값"을 센서 없는 기기에서는 이렇게 해석한다. 담당자는 하드웨어가 와도 구동 여부를 출력 전류 같은 전기 신호로 볼 가능성이 크다고 했다. 그 관측 수단은 #142의 미결이다.
- 7단계 연결 안정성은 BLE 스캔 비율, MQTT 대기 한도, watchdog과 나눈 재보고로 다룬다 — 담당자가 2026-10-24에 실측 뒤 골랐다.
  - MQTT `-9`는 QoS1 발행 뒤 대기 한도 안에 PUBACK을 못 받아 라이브러리가 연결을 닫은 것이다 (lwmqtt `MISSING_OR_WRONG_PACKET`).
  - 3분씩 잰 결과다. BLE 창 90/100ms는 `status` 전송이 중간값 216ms, 최대 2,003ms였고 `-9`가 1회 났다. 창 30/100ms는 중간값 177ms, 최대 1,903ms였고 끊김이 없었다.
  - Wi-Fi 절전을 끄면 BLE를 켤 때 `abort()`로 재시작을 반복했다. ESP32는 Wi-Fi와 BLE를 함께 쓸 때 절전을 끌 수 없다. 이 방법은 쓰지 않는다.
  - BLE 스캔 창은 30ms, 간격은 100ms다. 같은 간격 안의 RSSI 표본 수 변화는 8절에 실측으로 적는다.
  - MQTT 대기 한도(`NP_MQTT_IO_TIMEOUT_MS`)는 5초다. 실측 최대 2,003ms보다 넉넉하게 둔다.
  - loop task watchdog을 켠다. 기한은 빌드 설정 `NP_WATCHDOG_TIMEOUT_SEC`이고 15초다. 연결 한 번의 최장 대기(TCP 3초와 MQTT 응답 5초)보다 길다. 네트워크 호출 하나가 끝날 때마다 watchdog을 갱신한다. watchdog은 `setup()`의 기록 색인을 만든 뒤 켠다.
  - watchdog 재시작 뒤에도 명령 기록 때문에 다시 구동하지 않는다. 접수 기록은 `unknown`으로 복원한다.
  - 명령·검사 기록 재보고는 loop 한 번에 하나씩 보낸다. 연결이 끊기면 멈추고, 새 연결의 발표 뒤 처음부터 다시 보낸다.
- 검사 기록은 별도 `np_checks` NVS 파티션의 `np-checks` 이름 공간에 보존한다 — 담당자가 2026-10-24에 골랐다. 파티션은 384KiB, 슬롯은 256개, 건당 한도는 1,024바이트다. `spiffs`만 줄이고 기존 파티션 위치를 유지한다. 이론상 96페이지 중 2페이지를 빼고 34항목 기록을 페이지당 3개 넣으면 282개다. 실제 용량과 재작성은 별도 시험으로 확인한다.
- 검사 요청을 측정 전에 저장한다. 비교 필드는 `node_id`, `criteria_id`, `criteria_version`, `target_session_id`, `issued_at`, `expires_at`이다. 저장 확인 실패 때 측정하지 않는다. 결과 저장과 확인 조회를 통과한 뒤에만 발행한다. 재시작 때 측정 전 접수 기록은 근거 없는 `unknown`으로 복원하며 다시 측정하지 않는다.
- 명령과 검사 모두 빈 슬롯을 먼저 쓴다. 가득 차면 저장된 최종 결과가 있고 요청이 만료된 가장 오래된 기록을 교체한다 — 담당자가 2026-10-24에 확정했다. `unknown`도 최종 결과다. 접수·실행 중 기록은 교체하지 않는다. 시각 하한 `now - max_error`로 만료를 확인한다. 보관 중인 중복은 기존 결과를 돌려준다. 교체된 ID의 과거 결과는 보장하지 않으며 오래된 요청을 다시 실행하거나 측정하지 않는다.
- 기록의 최초 저장 UTC 시각을 보존하고 교체 순서로 쓴다. 같은 시각은 슬롯 번호로 정한다. 기존 명령 기록은 `occurred_at`을 쓴다. 기록이나 시각을 읽지 못하면 교체하지 않는다. 교체할 기록이 없으면 명령은 `STORAGE_ERROR`, 검사는 무응답으로 확인 불능이다. 새 요청이 이미 만료됐으면 교체를 위해 기존 기록을 지우지 않는다.
- v1 기준은 기능에 맞는 `locker-return`, `light-return`, `speaker-return`의 버전 1만 지원한다. 센서 관측은 출처, UTC 시각과 단조 시각을 담는다. 요청 뒤의 같은 측정 호출에서 얻은 관측만 반납 근거로 쓴다. 필수 항목 누락·측정 실패는 `unknown`이다. 검증 가능한 고장 관측만 `fault`다. bool을 만들지 않고 실행 거부 코드를 추가하지 않는다.
- 현재 센서 어댑터는 관측을 제공하지 않는다. 상태의 bool은 `null`, 구동부는 `unknown`이다. 반납 검사는 `unknown`과 빈 근거다. 실제 센서 조합과 완료 근거는 기존 미결을 유지한다.

## 7. 가정

이 spec은 docs의 계약을 따른다. 2026-10-24의 기록 교체 결정은 docs에도 반영했다.
다음 항목은 구현 전에 확인한다. 선택하지 않은 방법을 결정으로 취급하지 않는다.

| 미결 | 결정하거나 검증할 내용 | 근거와 진행 경계 |
|---|---|---|
| 구현 담당 | 펌웨어 담당의 팀 배정이다 | architecture.md §2의 미결. 담당자가 2026-10-23에 자신이 펌웨어 담당이라고 밝혔다. 이 배정을 가정하고 진행한다. 팀 배정이 다르게 나도 바뀌는 곳은 `firmware/` 안이다. 개발 환경과 내부 구조는 6절에 정했다 |
| 시각 동기화 | 시각원의 배포 위치, 연결 방법, 시작 절차, 재동기화 간격과 신뢰 상태 관리다 | 미결 — #140. 미검증 상태의 구동 차단은 유지한다 |
| 오차 실측과 설정 | 기기별 측정 조건, 최대 오차, 내부 시간 설정과 배포 설정 연결이다 | 미결 — #140. 공용 설정 이름·범위·값을 이 spec만으로 정하지 않는다 |
| 반납 센서와 측정 | 각 v1 항목의 관측 수단, 측정 순서, 이상 검출 범위와 근거 변환이다 | architecture.md §4·5. 문 닫힘만으로 잠금을 증명하지 않는다 |
| 완료 근거의 신선도 | 시작 전 관측의 인정 조건이다 | 미결 — #142. 시작 전 관측은 인정하지 않는 가정으로 공통 틀을 만든다. 시작 뒤 관측도 명령과 근거 출처를 확인한다 |
| 동작별 완료 관측 | 사물함 `open`, 조명과 스피커 `on`·`off`의 완료 조건과 오류 검출이다 | 미결 — #142. 증명한 기능만 성공 보고를 활성화한다 |
| IR 무드등과 스피커 | IR 무드등 관측 수단과 스피커 완료 근거다. 스피커의 새 보고 필드는 별도 계약이다 | 미결 — #142. 확인 불가를 성공으로 대체하지 않는다 |
| 명령 기록 | 저장 매체, 원자적 저장, 접수·진행·결과 복원과 저장 실패 진단이다 | architecture.md §5 MQTT 4·7절. 구동 전 저장과 재실행 금지는 유지한다. 저장 방식은 6절에 정했다. node_lab은 기본 파티션 표와 `np-node-lab`만 쓰므로 제품 펌웨어의 기록을 모른다. 제품 펌웨어를 쓴 기기에 node_lab을 올리면 이미 쓴 명령 ID를 보내지 않는다 |
| 검사 기록과 진단 | 6절의 NVS·교체 정책과 기존 v1 확인 불능 경로를 쓴다 | architecture.md §4·5. 비밀번호와 임의의 본문 전체를 로그에 남기지 않는다 |
| 검사 오류 보고 확장 | 오류 코드, 필수 근거 예외와 오류 사건 ID 규칙이다 | 미결 — #143. 첫 구현은 기존 확인 불능 경로로 검증한다 |
| 명령 기록의 보존 한도 | 기기 기록은 6절의 완료·만료 조건으로 교체한다 | 담당자가 2026-10-24에 확정했다. 서버의 명령 중복 방지 항목 정리는 FR-22의 미결이다 |

센서 선택, 측정 절차와 저장 방식은 담당자가 고른 뒤 6절에 기록한다.
MQTT 필드, 검사 기준의 뜻, 공용 설정과 다른 영역의 동작을 바꿔야 하면 팀 계약으로 다룬다.
실제 기기의 검증 전에는 시뮬레이터 통과를 실제 기기의 검증 완료로 표시하지 않는다.

## 8. 테스트

아래 첫 표는 전체 구현의 검증 계획이다. 실제 기기 검증은 단계마다 결과를 적는다.
펌웨어 단위 테스트는 `firmware/test/`의 Unity 테스트다 (6절).
서버와 펌웨어를 합친 시나리오 테스트는 `server/tests/integration/`에 둔다. 다른 영역을 바꾸는 작업은 따로 다룬다.

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_announce_each_connection` | 연결마다 기능과 점유 방식을 retained 발표한다 |
| FR-05 | `test_FR05_rssi_frame_metadata` | 비콘별 RSSI, 관측 시각, 기기 연결 ID와 순번을 보존한다 |
| FR-06 | 5절의 `test_FR06_*` | 명령 검증, 직렬화, 구동 전 저장과 재실행 금지를 확인한다 |
| FR-07 | 5절의 `test_FR07_*` | 시각 신뢰, 실제 완료, 불명, 고장과 결과 재보고를 구분한다 |
| FR-20 | 5절의 `test_FR20_*` | 요청 뒤 새 측정, v1 근거, 중복 검사와 확인 불능 동작을 확인한다 |
| FR-22 | `test_FR22_command_records_survive_restart` | 재시작과 서버 외부 요청 기록 정리 뒤에도 명령 중복 방지 기록을 보존한다 |
| NFR-03 | `test_NFR03_at_most_once_actuation` | 같은 명령의 물리 실행은 1회 이하이고 동시에 구동하는 명령은 하나 이하다 |
| NFR-04 | `test_NFR04_unconfirmed_safety_no_release` | 확인 불가를 통과로 만들지 않고 서버의 격리·복구 결정을 유지한다 |
| NFR-07 | `test_NFR07_no_local_authorization` | 펌웨어가 서버 명령 없이 허가와 점유 해제를 결정하지 않는다 |
| NFR-08 | `test_NFR08_only_server_commands` | 기기별 자격정보, 자기 토픽과 서버 전용 명령 권한을 검증한다 |
| NFR-10 | `test_NFR10_measured_device_capabilities` | 실제 장비별 센서 능력과 완료 근거를 검증한다 |
| NFR-11 | `test_NFR11_mqtt_v1_delivery` | MQTT 버전, 본문, QoS, retain, 재구독과 순번을 확인한다 |
| NFR-12 | `test_NFR12_local_time_source_disconnect` | 독립 AP와 시각원 단절 뒤 구동 차단을 확인한다 |
| NFR-14 | `test_NFR14_demo_device_scope` | 모형 사물함, 조명과 스피커의 범위만 구현한다 |

1단계는 옮긴 공통 동작을 다음 단위 테스트로 고정한다.

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-08 | `test_NFR08_config_accepts_valid_values` | 11개 설정 키를 읽고 숫자·시각 값을 변환한다 |
| NFR-08 | `test_NFR08_config_rejects_invalid_values` | 누락, 문자 규칙, 범위, 신뢰 기간과 재동기화 순서를 키 이름으로 거부한다 |
| NFR-08 | `test_NFR08_config_hex_rejects_nul` | 시리얼 16진수 값의 형식 오류와 NUL 바이트를 거부한다 |
| NFR-12 | `test_NFR12_clock_trust_window` | 미설정·미동기화·만료 시각을 신뢰하지 않는다 |
| FR-06 | `test_FR06_rejects_malformed_utc` | 서버 시각 문자열의 형식, 없는 날짜와 UTC가 아닌 시간대를 거부한다 |
| FR-06 | `test_FR06_validate_order` | 형식, 대상 연결, 동작, 시각 신뢰, 유효 시간 순서로 거부 코드를 정한다 |
| FR-06 | `test_FR06_canonical_content_fields` | 명령 내용 비교에 다섯 필드만 쓴다 |
| FR-06 | `test_FR06_ledger_finds_record_after_reload` | 저장한 기록을 새 인스턴스에서 ID로 찾고 빈 슬롯을 알려 준다 |
| FR-06 | `test_FR06_ledger_unreadable_blocks` | 읽을 수 없는 기록이 있으면 저장 오류로 막는다 |
| FR-22 | `test_FR22_ledger_owner_conflict` | 다른 `node_id`의 기록이 있으면 설정 변경을 거부한다 |
| FR-05 | `test_FR05_ibeacon_decode` | iBeacon 제조사 데이터만 UUID·major·minor로 읽는다 |
| FR-04 | `test_FR04_ready_requires_contract_order` | 준비 사유를 설정, 저장소, Wi-Fi, 시각, MQTT, 발표 순서로 정한다 |
| FR-04 | `test_FR04_retry_backoff_caps` | 재시도 간격이 두 배씩 늘고 상한에서 멈춘다 |

3단계 검증 계획:

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_announce_uses_configured_capability` | 모든 기능·점유 방식의 설정 값을 발표 본문에 넣는다 |
| FR-06 | `test_FR06_capability_action_matrix` | 기능별 허용 동작과 검사 순서를 확인한다 |
| NFR-08 | `test_NFR08_config_rejects_invalid_capability` | 기능 누락·타입·허용 값 오류를 키 이름으로 거부한다 |
| NFR-08 | `test_NFR08_config_rejects_invalid_occupancy` | 점유 방식 누락·타입·허용 값 오류를 키 이름으로 거부한다 |
| FR-22 | `test_FR22_changed_capability_preserves_duplicate` | 기능 변경 뒤에도 기존 결과를 재보고하고 구동하지 않는다 |

4단계 검증 계획 (PC와 ESP32의 `test_common`):

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-07 | `test_FR07_proven_completion_saved_before_success` | 근거와 저장 확인 뒤 성공하며 저장 실패 때 성공을 발행하지 않는다 |
| FR-07 | `test_FR07_verified_execution_failure` | 시작 실패와 명령별 실패 관측만 실행 실패로 보고한다 |
| FR-07 | `test_FR07_completion_requires_enabled_capability` | 기능별 성공 보고를 꺼 두면 완료 관측도 성공으로 바꾸지 않는다 |
| FR-07 | `test_FR07_unavailable_observation_is_unknown` | 관측 없음, 오류, 기한 미설정과 시작 불명은 성공이 아니다 |
| FR-07 | `test_FR07_completion_deadline` | fake 단조 시각으로 기한 경계와 바쁨 해제를 확인한다 |
| FR-07 | `test_FR07_completion_evidence_freshness` | 시작 전, 다른 명령과 출처 없는 관측은 완료를 증명하지 못한다 |
| FR-07 | `test_FR07_no_completion_without_evidence` | 제어 신호와 결과 저장 실패는 성공 근거가 아니다 |
| FR-07 | `test_FR07_power_loss_restores_unknown` | 진행 중 전원 차단 뒤 복원은 불명이며 재구동하지 않는다 |
| FR-06 | `test_FR06_concurrent_commands_busy` | 진행 중 새 명령을 거부하고 판정 뒤 바쁨을 푼다 |
| NFR-03 | `test_NFR03_at_most_once_actuation` | 진행 중 중복·재접속으로 다시 구동하거나 조기 불명 처리하지 않는다 |
| NFR-10 | `test_NFR10_device_slots_have_no_completion` | 세 기능의 신호 연결은 완료 관측과 성공 활성화를 제공하지 않는다 |

5·6단계와 기록 교체 검증 계획:

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-20 | `test_FR20_fresh_v1_evidence` | 세 v1 기준의 새 측정, 전부 참과 유효한 거짓을 구분한다 |
| FR-20 | `test_FR20_duplicate_check_reuses_result`, `test_FR20_check_id_conflict` | 재접속·재시작 중복의 사건 보존과 충돌 무응답을 확인한다 |
| FR-20 | `test_FR20_missing_evidence_unconfirmed` | 미지원 기준, 측정 실패, 항목 누락과 오래된 근거는 통과하지 않는다 |
| FR-07 | `test_FR07_verified_fault_report`, `test_FR07_status_uses_sensor_observations` | 고장 근거와 상태 관측을 확인한다. 구동 상태를 추정하지 않는다 |
| FR-07 | `test_FR07_check_storage_and_recovery` | 측정 전 저장, 결과 저장 실패와 재시작 불명 복원을 확인한다 |
| FR-07 | `test_FR07_replay_spreads_over_loop_iterations` | 재보고를 한 번에 하나씩 보내고 끝나면 멈추며, 다시 시작하면 처음부터 보낸다 |
| FR-07 | `test_FR07_status_without_sensors_uses_command_state` | 센서 없는 항목만 명령 처리 상태와 `sensor_fault=false`로 채우고 반납 근거에는 쓰지 않는다 |
| FR-22 | `test_FR22_replaces_oldest_expired_terminal` | 완료·만료된 가장 오래된 기록과 `unknown`을 교체한다 |
| FR-22 | `test_FR22_retention_preserves_pending_and_untrusted` | 진행 중, 미만료, 손상, 미신뢰 시각과 시각 오차 경계에서 기록을 보존한다 |
| FR-06 | `test_FR06_evicted_command_does_not_actuate` | 교체된 과거 요청을 다시 실행하지 않는다 |
| FR-20 | `test_FR20_evicted_check_does_not_measure` | 교체된 과거 검사 요청을 다시 측정하지 않는다 |
| FR-22 | `test_FR22_check_partition_capacity` | 실제 검사 파티션에서 최대 크기의 256건 저장과 재작성을 확인한다 |

1단계 결과 (2026-10-23, ESP32-WROOM-32E):

- `pio test -e esp32dev`: 13개 통과.
- `pio test -e native`: 13개 통과. Windows에서 WinLibs MinGW-w64 GCC 16.2.0(UCRT)을 썼다.
- 제품 펌웨어를 올린 뒤 node_lab에서 주입한 설정(generation 1)을 그대로 읽었다. `boot_check`에서 순서 위반 없이 4.8초에 `NODE_READY`가 됐다.
- node_lab이 남긴 명령 기록을 읽어 같은 사건으로 재보고했다. 서버는 새 연결을 온라인으로 확인했고 `FAULT` 차단은 그대로 유지됐다.
- 연결 직후 MQTT `-9` 끊김이 한 번 있었고 바로 다시 연결됐다. node_lab에서도 본 현상이며 7단계에서 다룬다.

2단계는 명령 처리 상태 전이를 다음 단위 테스트로 고정한다. 마지막 테스트는 실제 ESP32에서만 실행한다.

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-06 | `test_FR06_accepted_then_unknown_without_evidence` | 접수 저장, `accepted`·`running`·`unknown` 순서와 구동 1회를 확인한다 |
| FR-06 | `test_FR06_invalid_message_no_actuation` | 읽을 수 없는 명령은 보고하지 않고, 검사에서 걸린 명령은 거부 기록을 남긴다 |
| FR-06 | `test_FR06_duplicate_after_restart` | 새 인스턴스에서도 같은 명령의 기존 결과만 재보고하고 구동하지 않는다 |
| FR-06 | `test_FR06_command_id_conflict` | 내용이 다른 같은 ID를 거부하고 기존 기록을 바꾸지 않는다 |
| FR-06 | `test_FR06_concurrent_commands_busy` | 구동 중에 받은 새 명령을 `DEVICE_BUSY`로 거부하고 기록한다 |
| FR-06 | `test_FR06_receipt_storage_failure` | 접수 저장 실패와 읽을 수 없는 기록에서 `STORAGE_ERROR`로 거부하고 구동하지 않는다 |
| FR-06 | `test_FR06_session_and_expiry_rechecked` | 접수 뒤 재검사에서 걸리면 구동하지 않고 `failed`와 거부 코드를 보고한다 |
| FR-07 | `test_FR07_power_loss_restores_unknown` | 재시작 뒤 `accepted` 기록을 `unknown`으로 저장·보고하고 다시 구동하지 않는다 |
| FR-07 | `test_FR07_no_completion_without_evidence` | 결과 저장에 실패하면 결과를 발행하지 않고 `accepted` 기록을 남긴다 |
| FR-07 | `test_FR07_result_event_identity` | 재보고가 사건 ID, 상태와 발생 시각을 유지한다 |
| FR-22 | `test_FR22_full_ledger_rejects_without_eviction` | 슬롯이 모자라고 시각 신뢰가 없으면 기존 기록을 보존한다 |
| FR-22 | `test_FR22_legacy_records_block_reexecution` | node_lab 기록을 조회·재보고하고 새 기록은 기록 파티션에만 쓴다 |
| FR-22 | `test_FR22_record_partition_capacity` | 실제 `np_records` 파티션에 512바이트 기록 400개를 쓰고 모두 다시 쓴다. 색인 생성과 조회 시간을 잰다 |

2단계 결과 (2026-10-23, ESP32-WROOM-32E):

- `pio test -e native`: 25개 통과. `pio test -e esp32dev`: `test_common` 25개와 `test_esp32_storage` 1개 통과.
- 첫 측정(여유 공간 검사 방식): 484바이트 기록 441개를 쓴 뒤 빈 항목 440개가 남았지만 마지막 기록을 다시 쓰지 못했다.
  슬롯 2000개를 차례로 읽는 조회는 1.9초 걸렸다. 그래서 크기 한도, 슬롯 수와 색인으로 바꿨다 (6절).
- 바꾼 뒤: 512바이트 기록 400개를 쓰고 모두 다시 썼다. 부팅 색인 생성은 1.4초, 없는 ID 조회는 0.2ms, 있는 ID 조회는 4ms다.
- 제품 펌웨어를 올리자 `RECORD_STORE primary=1 legacy=1`로 두 저장소를 열었다. 기존 설정(generation 1)은 그대로 읽었다.
- 이번에는 PC가 시험 AP 밖에 있어 MQTT와 시각원에 연결하지 못했다. node_lab 기록의 실제 재보고는 다음 연결 시험에서 확인한다.
- 연결마다 모든 기록을 재보고한다. 기록이 수백 개면 연결 직후 발행이 몰린다. 7단계에서 측정한다.

3단계 결과 (2026-10-23, ESP32-WROOM-32E, COM6):

- `pio test -e native`: 30개 통과다. 기존 25개와 3단계 5개다.
- `pio run -e esp32dev`: 통과다. RAM은 57,556바이트, Flash는 1,096,369바이트다.
- `pio test -e esp32dev -f test_common --upload-port COM6 --test-port COM6`: 30개 통과다.
- 서버 전체 `pytest`: 2753 passed, 2 skipped, 1 xfailed다. spec 커밋 전과 구현 뒤에 확인했다.
- 제품 펌웨어를 COM6에 다시 올렸다. `NODE_START node=- config=invalid generation=0 mode=serial_only storage=1`을 확인했다.
- `CONFIG_STATE configured=0 generation=0`과 `NODE_NOT_READY reason=config_missing`을 확인했다. generation 0은 무효 설정을 적용하지 않은 메모리 상태다. 저장 설정을 다시 쓰지 않았다.
- `RECORD_STORE primary=1 legacy=1 slots=400 legacy_slots=64`를 확인했다. 기록 이름 공간과 파티션을 지우지 않았다.
- 기존 설정에 필수 키가 없어 재주입이 필요하다. 담당자 지시에 따라 재주입하지 않았다. 설정 후 실제 MQTT 발표와 명령 실행은 확인하지 못했다.
- 기능별 동작과 `open`의 `unknown` 결과는 공통 테스트에서 확인했다. 실제 구동부와 완료 근거는 4단계다.
- `test_esp32_storage`는 실행하지 않았다. 2단계의 실제 서버 연결 재보고 확인은 이번 범위에서 제외했다.
- 주입 도구 변경은 경계 밖이라 멈췄다. 저장소 이슈 양식의 초안을 대화로 전달했으며 이슈는 생성하지 않았다. node_lab도 수정하지 않았다.
- NimBLE 의존 라이브러리에서 기존 enum 관련 경고가 나왔으나 빌드와 테스트는 통과했다.

4단계 결과 (2026-10-23, ESP32-WROOM-32E, COM6):

- `pio test -e native`: 38개 통과다. 기존 30개와 4단계 8개다.
- `pio test -e esp32dev -f test_common --upload-port COM6 --test-port COM6`: 같은 38개 통과다. 실제 ESP32에서도 구동부와 관측은 fake다.
- `pio run -e esp32dev`와 제품 펌웨어 업로드가 통과했다. 최종 RAM은 57,796바이트, Flash는 1,098,545바이트다.
- 서버 전체 `pytest`: 2753 passed, 2 skipped, 1 xfailed다. 명세 커밋 전과 구현 뒤에 확인했다.
- 완료 증명, 시작 실패, 실패 관측, 성공 보고 꺼짐, 관측 오류와 관측 중단을 확인했다. 기한 미설정과 기한 경계도 확인했다.
- 시작 전·동일 밀리초·미래 관측, 다른 명령, 출처 누락과 단조 시각 역행은 `unknown`이다. 관측 중 기한 초과도 성공이 아니다.
- 성공은 결과 저장과 확인 조회 뒤에만 발행했다. 결과 쓰기와 확인 조회 실패에서는 성공을 발행하지 않았다.
- 결과 쓰기 실패 때 기록은 `accepted`로 남았다. 저장이 회복된 뒤 재보고는 `unknown`이다. 읽을 수 없게 쓴 fake 기록도 성공으로 보고하지 않았다.
- 진행 중 중복과 재접속은 재구동하지 않았다. 현재 접수 기록을 복원으로 오인하지 않았다. 전원 차단을 흉내 낸 새 인스턴스는 `unknown`으로 복원했다.
- 제품 펌웨어를 COM6에 다시 올렸다. `NODE_START node=- config=invalid generation=0 mode=serial_only storage=1 reset_reason=power_on`을 확인했다.
- `RECORD_STORE primary=1 legacy=1 slots=400 legacy_slots=64 free_entries=7936`과 `NODE_NOT_READY reason=config_missing`을 확인했다.
- 설정을 재주입하지 않았다. `np-records`, `np-node-lab`, `np-config` 이름 공간과 `np_records` 파티션을 지우지 않았다. `test_esp32_storage`는 실행하지 않았다.
- 실제 구동부와 완료 센서가 없어 물리 완료는 확인하지 못했다. 세 기능의 성공 보고는 모두 꺼져 있다. 문 닫힘과 IR 송출을 완료 근거로 만들지 않았다.
- 2단계 실제 서버 연결 재보고와 3단계 설정 재주입 뒤 발표·명령은 이번 범위에서 제외했다. 실제 구동 중 MQTT 유지와 BLE 연속 관측은 검증하지 못했다.
- USB 주입 도구, node_lab, 서버 계약과 구현은 바꾸지 않았다. 새 경계 밖 작업과 새 이슈 초안은 없다. #142의 완료 조건과 관측 수단은 미결이다.
- PC와 ESP32의 언어 표준 차이로 첫 ESP32 빌드가 실패했다. C++11 생성자를 명시한 뒤 빌드와 두 환경 테스트를 통과했다. Gotchas에 기록했다.
- NimBLE 의존 라이브러리의 기존 enum 관련 경고는 남아 있다.

5·6단계와 기록 교체 결과 (2026-10-24, ESP32-WROOM-32E, COM6):

- `pio test -e native`: 53개 통과다. 기존 38개와 이번 15개다.
- `pio test -e esp32dev -f test_common --upload-port COM6 --test-port COM6`: 같은 53개 통과다. 구동부와 센서는 fake다.
- `pio test -e esp32dev -f test_check_storage --upload-port COM6 --test-port COM6`: 실제 검사 NVS 용량 시험 1개 통과다.
- 최대 크기 1,024바이트 기록 256건을 모두 저장하고 다시 썼다. 새 Ledger에서도 마지막 ID를 조회했다.
- 제품 펌웨어 빌드와 COM6 업로드가 통과했다. RAM은 57,900바이트, Flash는 1,115,109바이트다.
- 재부팅에서 `RECORD_STORE primary=1 legacy=1 slots=400 legacy_slots=64 free_entries=7936`을 확인했다.
- `CHECK_STORE ready=1 slots=256 record_bytes=1024 free_entries=11968`을 확인했다.
- 기존 설정은 `config=invalid generation=0`이다. 설정을 재주입하지 않았고 `NODE_NOT_READY reason=config_missing`을 확인했다.
- 용량 시험은 `np-check-test`만 채웠다가 지웠다. 운영 이름 공간과 설정은 지우지 않았다. 기존 명령 파티션 용량 시험은 다시 실행하지 않았다.
- 세 v1 기준의 통과·정상 대기, 미지원 기준, 누락·오래된·미래 근거, 출처 누락과 측정 중 만료를 확인했다.
- 문 닫힘을 잠금 근거로 쓰지 않았다. 상태는 센서 관측으로만 만들었다. 미설치 센서의 bool은 `null`, 구동부는 `unknown`이다.
- 같은 검사 ID는 재접속·재시작 뒤에도 같은 사건과 근거를 반환했다. 충돌은 기존 기록을 보존하고 응답하지 않았다.
- 접수 저장 실패 때 측정하지 않았다. 결과 저장과 확인 조회 실패 때 결과를 발행하지 않았다. 접수만 남은 기록은 불명으로 복원했다.
- 완료·만료된 가장 오래된 `unknown`도 교체했다. 접수·실행 중·미만료 기록, 시각 불명과 손상 기록은 교체하지 않았다.
- 교체된 과거 명령과 검사는 재실행·재측정하지 않았다. 가득 찬 기록의 `DEVICE_BUSY`도 저장해 재전송 뒤 구동하지 않았다.
- 문서 선행 커밋 전 서버 전체 테스트는 2735 passed, 20 skipped, 1 xfailed다. 브로커 경로가 없어서 18개 시험이 추가로 생략됐다.
- 최종 서버 전체 테스트는 실제 Mosquitto 경로를 연결해 2753 passed, 2 skipped, 1 xfailed다. 생략한 2개는 Windows에서 쓸 수 없는 파일 이름 사례다.
- 펌웨어 빌드와 함께 실행한 첫 브로커 포함 검증에서 FR-23 동시 요청 시험이 대기 진입의 3초 제한을 넘겼다. 빌드 종료 뒤 같은 전체 명령은 통과했다. 서버 코드는 고치지 않았다.
- NimBLE 의존 라이브러리의 기존 enum 경고는 남아 있다. 자체 모듈의 빌드 오류는 없다.
- 실제 센서, 물리 완료와 실제 MQTT 반납 시나리오는 검증하지 않았다. 2·3단계 연결 확인과 7~9단계는 남는다.

실제 서버 연결 결과 (2026-10-24, ESP32-WROOM-32E, COM6, 펌웨어 c152d82):

- 시험 AP는 휴대폰 핫스팟이고 서버 PC 주소는 `10.107.108.199`다. 새 시험 폴더 `server/var/node-lab-hotspot-2`를 썼다. 기존 시험 DB의 `light-1`은 지난 `unknown`으로 `FAULT`이고, 지우지 않았다.
- 주입 도구가 `fleet.json`의 `light`·`shared`를 포함해 13개 키를 주입했다. 포트를 열 때 보드가 재시작해서 첫 시도는 `CONFIG_READY` 시간 초과였다. 도구가 시작 신호를 다시 보내게 고친 뒤 성공했다 (`specs/core-node-lab/spec.md`).
- `boot_check`: 순서 위반 없이 3.1초에 `NODE_READY`다. `RECORD_STORE primary=1 legacy=1`과 `ANNOUNCED`를 확인했다. node_lab 기록 `d6959eba…`를 `unknown`으로 재보고했다.
- 처음 펌웨어는 상태를 `actuator_state=unknown`, `sensor_fault=null`로 보고했다. 서버 복원은 이를 정상으로 보지 않아 `current_status`로 막았다. 6절의 담당자 결정 뒤 상태는 `idle`·`false`이고 서버는 `ready=true`로 바꿨다.
- `on` 요청은 서버가 허가해서 명령 `9f685841…`을 보냈다. 기기는 `accepted`, `COMMAND_HANDLED physical_completion=unverified`, `running`, `unknown` 순서로 보고했다.
- 서버는 `DENY / EXEC_UNKNOWN`으로 답하고 기기를 `FAULT`로 격리했다. 다음 `on`은 `DENY / NO_CANDIDATE`다. 장애 기기를 다시 실행하지 않았다.
- MQTT 재접속과 EN 재시작 뒤에도 두 기록을 `unknown`으로 재보고했다. `COMMAND_HANDLED`는 다시 나오지 않았다.
- 명령 뒤 MQTT `-9` 끊김이 다시 두 번 있었다. 바로 다시 연결됐다. 7단계에서 다룬다.
- 서버는 새 시험 DB에 없는 옛 명령 결과를 0.5초마다 재인계하며 `StoreError` 경고를 냈다. 서버 IOT·CORE의 동작이라 이 spec 밖이다.
- 반납 검사는 시험 콘솔에 요청 경로가 없고 공용형 조명은 대상이 아니라 확인하지 않았다. 물리 완료와 센서는 미검증이다.
- 원본은 `boot-2.jsonl`, `serial-command-1.jsonl`, `serial-reset-1.jsonl`, `evidence.jsonl`이다 (Git 제외).

7단계 결과 (2026-10-24, ESP32-WROOM-32E, COM6, 핫스팟 AP):

- 각 설정을 올리고 재부팅 뒤 `MQTT_REPORT ... ms=`로 QoS1 `status`의 PUBACK 대기 시간을 쟀다.

| 설정 | 시간 | 중간값 | 상위 5% | 최대 | 1초 초과 | 끊김 |
|---|---|---|---|---|---|---|
| BLE 90/100ms, 대기 2초 (기존) | 3분 | 216ms | 1,069ms | 2,003ms | 10 | `-9` 1회 |
| Wi-Fi 절전 끔 (BLE 90 또는 30) | 3분씩 | — | — | — | — | 부팅 중 `abort()` 재시작 반복 |
| BLE 30/100ms, 대기 2초 | 3분 | 177ms | 697ms | 1,903ms | 7 | 없음 |
| BLE 90/100ms, 대기 5초 | 3분 | 173ms | 2,040ms | 3,398ms | 12 | 없음 |
| BLE 30/100ms, 대기 5초 (최종) | 3분 | 172ms | 536ms | 2,599ms | 1 | 없음 |

- 최종 설정 부팅에서 `WATCHDOG timeout_sec=15 armed=1`을 확인했다.
- 최종 설정 측정은 담당자 요청으로 5분 계획 중 3분에서 멈췄다. 장시간 끊김 빈도는 8단계 장시간 측정과 함께 확인한다.
- watchdog: 20초 뒤 loop를 일부러 멈춘 시험 빌드(커밋하지 않음)에서 15초 뒤 `task_wdt`가 재시작했다. 다음 부팅은 `reset_reason=watchdog`이다.
- RSSI 표본: 시험 비콘은 nRF Connect의 legacy 광고 250ms 간격이다. 2분씩 쟀다.

| BLE 스캔 창 | 표본이 있는 1초 프레임 | 프레임당 표본 중간값 | 최대 |
|---|---|---|---|
| 90/100ms | 105개 (약 88%) | 2 | 6 |
| 30/100ms (최종) | 75개 (약 63%) | 1 | 3 |

- 스캔 창을 줄이자 1초 프레임의 약 37%에 표본이 없었다. 근접 판정 품질과의 관계는 남은 확인이다. 광고 간격을 줄이면 표본이 늘어날 수 있다.
- 같은 2분 동안 최종 설정의 `status` 대기 시간은 중간값 102ms, 최대 1,567ms였고 끊김이 없었다. 재보고 수백 건의 실제 소요 시간은 확인하지 않았다.

실제 기기는 다음 절차로 검증한다.

1. 기기별로 재부팅, 재접속과 장시간 동작 때 최대 시각 오차를 측정한다.
2. 시각원 단절, 시각원 변경과 시계 보정 때 시각 신뢰와 새 명령 거부를 확인한다.
3. 명령을 구동하기 직전에 대상 연결과 만료를 다시 검사하는지 확인한다.
4. 반납 요청 뒤 각 필수 항목을 새로 측정하고 관측 시각을 확인한다.
5. 센서 이상과 측정 실패 때 통과와 거짓 bool을 만들지 않는지 확인한다.
6. 동작별로 제어 신호 송출과 실제 완료를 구분하는지 확인한다.
7. 접수 기록 저장 전후, 구동 중과 완료 직전에 전원을 차단한다.
8. 기록을 복원하고 같은 명령으로 다시 구동하지 않는지 확인한다.
9. 결과 저장 실패 뒤 완료를 증명하지 못하면 `unknown`을 보고하는지 확인한다.
10. 명령·검사 결과를 재발행하고 사건 ID와 사건 내용의 보존을 확인한다.

검증 결과에는 기기 ID, 펌웨어 버전, 센서 구성, 설정, 측정 조건과 관측 근거를 기록한다.
이번 문서 변경은 기존 서버 전체 `pytest`와 문서 상대 링크로 검증한다.
