# ESP32 펌웨어의 MQTT v2 연동과 실제 기기 검증

- 상태: 구현 중 (1~7단계와 MQTT v2 전환 구현 완료. 8·9단계는 남음)
- 담당: 작업 지시자 (펌웨어). architecture.md §2의 팀 배정은 미결이다 (7절 가정).
- 후속 구현 브랜치: `feat/firmware-contracts`
- 요구사항: FR-04, FR-05, FR-06, FR-07, FR-20, FR-22, NFR-03, NFR-04, NFR-07, NFR-08, NFR-10, NFR-11, NFR-12, NFR-14
- 따르는 결정: docs에 반영한 #19, #37, #38, #61, #140, #195(2026-10-26 센서 기반 물리 검증 제외)다. #196은 미결이다 (7절).

계약의 근거는 [제품](../../docs/product.md), [요구사항](../../docs/requirements.md), [아키텍처](../../docs/architecture.md)다.
문서의 권위 순서도 이 순서다. 닫힌 이슈와 PR 토론을 계약의 근거로 쓰지 않는다.
이 spec은 기존 참고 문서 다섯 개를 합친 계약이다. 구현은 단계별로 진행하고, 단계마다 6절 결정과 8절 테스트를 갱신한다.

## 1. 왜

서버 계약만으로는 실제 ESP32가 시각, 명령 기록과 제어 신호 출력을 어떻게 다루는지 확인할 수 없다.
서버는 기기가 출력을 마치고 결과를 저장했다는 보고로 명령 완료를 확인한다 (architecture.md §5 MQTT 5절).
시뮬레이터만으로는 실제 기기의 시각 오차, 재접속과 전원 차단 뒤 동작을 검증할 수 없다.

## 2. 무엇

- 입력: 서버의 MQTT `cmd`, BLE iBeacon 관측, 폐쇄망 시각원과 실행 설정이다.
- 출력: `announce`, `rssi`, `status`, `result`와 허가한 명령의 제어 신호 출력이다.
- 바뀌는 상태: ESP32의 기기 연결 ID, 발행 순번, 시각 신뢰, 명령 기록과 명령 처리 상태다.
- 약속:
  - 펌웨어는 ESP32에서 실행하는 프로그램이다. ESP32는 연결한 구동부에 제어 신호를 출력한다.
  - 펌웨어는 MQTT v2(`schema_version=2`)로 서버와 연결한다. v1 본문은 거부한다. 허가, 점유 해제와 장애 복구는 서버가 결정한다.
  - 펌웨어는 제어 신호 출력을 마치고 결과를 저장한 명령만 `succeeded`로 보고한다. 실제 상태를 관측하지 않는다.
  - `status`는 명령 처리 상태만 보고한다. 문 닫힘, 켜짐, 센서 고장과 반납 검사 결과는 없다.
  - 펌웨어는 재접속과 재시작 뒤에도 같은 명령으로 다시 구동하지 않는다.
  - 검증하지 않은 시각 값은 미결로 남긴다.

### 시각 동기화

펌웨어는 폐쇄망 안의 시각원으로 동기화한다. 외부 인터넷을 필수로 두지 않는다.
기기별 최대 시각 오차는 실측한 뒤 배포 설정으로 관리한다. 검증 전 값은 미설정이다.
다음 상황에서 펌웨어는 새 명령을 `CLOCK_UNVERIFIED`로 거부한다.

- 아직 동기화하지 않았다.
- 시각원이 바뀌었다.
- 오차 한도 준수를 확인하지 못했다.

펌웨어는 최대 시각 오차를 고려해서 명령 유효 시간을 검사한다.
펌웨어는 물리 동작 직전에도 대상 연결과 유효 시간을 다시 검사한다.
서버도 시각 신뢰 검증 전에는 시각 기반 실행 허가를 차단한다.

### 반납

펌웨어는 반납 검사를 받거나 보내지 않는다. 2026-10-26 결정으로 `safety_check`·`safety_check_result` 토픽이 없다.
점유 종료는 서버가 명시적 반납 요청, 본인 점유, 미완료 명령과 장애로 판단한다 (FR-20).
상태 보고와 명령 성공은 물리 안전의 근거가 아니다.

### 실행 완료와 명령 기록

펌웨어는 제어 신호 출력을 마치고 결과를 저장한 명령만 `succeeded`로 보고한다.
출력이 일어나지 않았음을 확인하면 `failed`다. 출력 여부나 결과를 확정하지 못하면 `unknown`이다.
PUBACK, `accepted`와 `running`은 완료가 아니다. 실제 상태 관측이 없다는 이유로 완료된 출력을 `unknown`으로 바꾸지 않는다.
IR 송출, 서보 제어 신호와 스피커 제어 신호는 같은 원칙을 따른다 (architecture.md §5 MQTT 5절).

펌웨어는 접수 기록을 구동 전에 보존한다.
같은 `command_id`로 재접속과 재부팅 뒤 다시 구동하지 않는다.
재시작 뒤 결과가 없는 접수 기록은 `unknown`으로 저장하고 보고한다. 자동으로 구동을 재개하지 않는다.
명령 기록은 가득 찼을 때 완료·만료 조건을 만족한 가장 오래된 기록부터 교체한다 (6절).

### 구현 단계

하드웨어가 ESP32뿐인 동안에는 모든 장치에 공통인 부분만 구현한다.
장치별 구동부는 하드웨어가 온 뒤 `np_device`의 출력 인터페이스에 붙인다.

| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | node_lab에서 실제 기기로 검증한 공통 동작을 `firmware/`의 모듈로 옮기고 단위 테스트 환경을 만든다 | 완료 (8절) |
| 2 | 명령 기록의 상태 전이, 전원 차단 복원, 충돌, 저장 실패와 용량 | 완료 (8절). 2026-10-24 실제 연결에서 재보고 확인 |
| 3 | 장치 기능·점유 방식의 USB 설정과 기능별 동작 검증 | 완료 (8절). 2026-10-24 재주입 뒤 실제 발표·명령 확인 |
| 4 | 구동부 인터페이스와 성공·실패·불명 결과 | 완료 (8절). 2026-10-26 결정으로 출력 완료 기준으로 바꿈 |
| 5 | `safety_check` 검사 기록과 v1 기준 | 2026-10-26 결정으로 제거 (6절) |
| 6 | 상태 보고 | 완료. 2026-10-26 결정으로 `command_state`만 보고 (6절) |
| 7 | 연결 안정성: MQTT 끊김 원인과 watchdog | 완료 (8절). 비콘 광고 100ms 조건. 근접 판정 품질 실측은 남음 |
| 8 | 장시간 시각 오차 측정과 시각 재요청 검증 (#140) | 대기 |
| 9 | 서버와 합친 시나리오 테스트와 검증 보고 | 대기 |
| v2 | MQTT v2 전환: 출력 완료 성공, `command_state`, 검사·센서 모듈 제거 (2026-10-26 결정) | 완료 (8절) |

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
- `status`는 `observed_at`과 `command_state`(`idle`, `busy`, `unknown`)만 담는다. 실제 상태와 센서 고장 값은 담지 않는다 (FR-07, architecture.md §5 MQTT 3절).
- 펌웨어의 상태 보고는 서버의 점유 해제와 `FAULT` 복구를 대신하지 않는다 (FR-20, NFR-04).
- ESP32와 NimBLE를 쓴다. 시연 장비는 NFR-10의 사물함, LED 조명, 스피커와 IR 무드등을 따른다 (NFR-10, NFR-14).
- MQTT 3.1.1, UTF-8 JSON, 여섯 토픽의 QoS와 retain은 계약을 따른다. 모든 메시지는 `schema_version=2`다 (NFR-11, architecture.md §5 MQTT 1~3절).
- MQTT 접속마다 새 UUID인 `session_id`를 만든다. 연결·토픽별 `seq`는 1부터 증가한다 (FR-07, architecture.md §5 MQTT 2절).
- `clean_session=true`로 접속하고 자기 `cmd`를 매번 구독한다. LED 표시가 있는 빌드는 자기 `occupancy`도 구독한다 (NFR-08, NFR-11).
- `occupancy`는 표시에만 쓴다. 명령 접수, 구동, 상태 보고와 서버의 점유 판단을 바꾸지 않는다 (FR-19, architecture.md §5 MQTT 3절).
- 기기별 자격정보와 접속 설정은 실행 설정에서 받는다. 비밀번호를 저장소와 로그에 넣지 않는다 (NFR-08).
- 기기는 독립 AP 안에서 동작한다. 행사장 Wi-Fi와 외부 시각 서버를 필수로 두지 않는다 (NFR-12).
- 시간 값과 임계값은 이름, 단위와 허용 범위가 있는 설정으로 받는다. 미측정 수치를 상수로 넣지 않는다 (requirements.md §1.2).
- 펌웨어는 대여형을 이탈과 대여시간 초과만으로 자동 반납하지 않는다. 켜진 시간만으로 공용형을 끄지 않는다 (FR-20).

## 4. 경계

### 고치는 곳

- `specs/firmware-contracts/spec.md`다.
- `firmware/`다. `firmware/node_lab/`은 `specs/core-node-lab/spec.md`의 범위다.
  MQTT v2 전환 fix PR은 두 spec의 시험 펌웨어와 시리얼 로그 해석 도구(`server/tools/node_lab/boot_check.py`)를 함께 고친다.
- 루트 `.gitignore`의 펌웨어 빌드 경로다.

### 고치지 않는 곳

- 항상: `AGENTS.md`의 Gotchas 밖, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 담당자가 2026-10-24에 승인한 기록 교체 계약은 `docs/requirements.md`와 `docs/architecture.md`에 직접 반영한다. 이슈는 만들지 않는다.
- 이 작업: 서버의 IOT 구현, CORE, DB, API, 기기 시뮬레이터, 앱과 대시보드다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| MQTT v2의 `cmd` | 서버 요청을 받는다 | IOT |
| MQTT v2의 `occupancy` | 대여형 기기의 점유 표시를 받는다 | IOT |
| MQTT v2의 네 기기 보고 | 기능, RSSI, 명령 처리 상태와 실행 결과를 제공한다 | IOT |
| 폐쇄망 시각원과 기기별 최대 오차 | 동기화와 실측 근거를 제공한다. 설정 연결은 #140의 미결을 따른다 | 서버 배포 |

MQTT 연결 구현은 [게이트웨이 spec](../iot-mqtt-gateway/spec.md)이 맡는다.
IOT의 전체 분할은 [구현 계획](../iot-mqtt-gateway/plan.md)을 따른다.

### 만들지 않는 것

- 장치별 구동부 구현이다. 하드웨어 도착 뒤에 다룬다.
- 센서 관측, 물리 완료 증명과 반납 검사다. 2026-10-26 결정으로 범위에서 제외했다.
- 서버의 허가 판정, 점유 종료, 승인 사용분 정산과 소유자 복구 승인이다.
- architecture.md §5 MQTT 3절 밖의 새 필드와 진단 토픽이다.
- 임의의 최대 시각 오차와 재동기화 기본값이다.
- 실제 잠금장치, 고보안 출입문, iOS와 디스펜서다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 필수 필드 누락, 타입 오류나 미지원 버전이다 | 구동하지 않는다. 읽을 수 있는 명령 ID에는 기존 거부 형식을 쓴다 | `test_FR06_invalid_message_no_actuation` |
| `schema_version=1` 명령이 온다 | `INVALID_MESSAGE`로 거부한다. 새 뜻으로 재해석하지 않는다 | `test_FR06_validate_order` |
| 서버가 아닌 주체가 명령을 발행한다 | 브로커가 차단한다. 펌웨어는 자기 명령 토픽만 구독한다 | `test_NFR08_only_server_commands` |
| 두 명령이 동시에 온다 | 한 명령만 구동한다. 나머지는 접수와 구동 전에 `DEVICE_BUSY`로 거부한다 | `test_FR06_concurrent_commands_busy` |
| 같은 명령이 재접속·재시작 뒤 다시 온다 | 연결·만료 검사 전에 기존 기록을 찾는다. 다시 구동하지 않는다 | `test_FR06_duplicate_after_restart` |
| 같은 명령 ID의 내용이 다르다 | `COMMAND_ID_CONFLICT`로 거부한다. 기존 기록을 보존한다 | `test_FR06_command_id_conflict` |
| 시각을 검증하지 못했다 | 새 명령을 `CLOCK_UNVERIFIED`로 거부한다 | `test_FR07_unverified_clock_blocks_execution` |
| 대상 연결이 다르거나 구동 직전에 만료한다 | 물리 동작을 시작하지 않는다 | `test_FR06_session_and_expiry_rechecked` |
| 구동 전 접수 기록 저장이 실패한다 | `STORAGE_ERROR`로 거부하고 구동하지 않는다 | `test_FR06_receipt_storage_failure` |
| 명령 도중이나 결과 저장 전에 전원이 꺼진다 | 기록을 복원한다. 결과가 없으면 `unknown`을 보고하고 자동 재개하지 않는다 | `test_FR07_power_loss_restores_unknown` |
| 결과 저장이나 확인 조회에 실패한다 | `succeeded`를 발행하지 않는다. 기록은 `accepted`로 남고 복원에서 `unknown`이 된다 | `test_FR07_output_completion_saved_before_success` |
| 명령 결과를 다시 발행한다 | 사건 ID와 사건 내용을 유지한다. 발행 순번과 발행 시각만 새로 부여한다 | `test_FR07_result_event_identity` |
| 외부 인터넷이나 시각원이 끊긴다 | 외부 인터넷 없이 연결한다. 시각 신뢰를 확인하지 못하면 새 구동을 막는다 | `test_NFR12_local_time_source_disconnect` |
| USB 설정 값이 없거나 범위·문자 규칙을 어긴다 | 저장하지 않고 처음 어긴 키 이름으로 거부한다 | `test_NFR08_config_rejects_invalid_values` |
| 명령의 시각 문자열이 UTC 형식이 아니거나 없는 날짜다 | `INVALID_MESSAGE`로 거부한다 | `test_FR06_rejects_malformed_utc` |
| 명령 기록이 다른 `node_id`의 것인데 설정의 `node_id`를 바꾼다 | 설정을 거부하고 기록을 그대로 둔다 | `test_FR22_ledger_owner_conflict` |
| 기기가 상태를 보고한다 | `command_state`만 보낸다. 처리 중이면 `busy`, 아니면 `idle`, 명령 기록 저장소를 열지 못하면 `unknown`이다 | `test_FR07_status_reports_command_state` |
| 기록 슬롯이 모자란다 | 완료·만료된 가장 오래된 기록을 교체한다. 교체 대상이 없으면 새 처리를 막는다 | `test_FR22_full_ledger_rejects_without_eviction`, `test_FR22_replaces_oldest_expired_terminal` |
| 명령 기록이 크기 한도를 넘는다 | 기록하지 않고 `INVALID_MESSAGE`로 거부한다. 구동하지 않는다 | `test_FR06_invalid_message_no_actuation` |
| node_lab이 남긴 기록의 명령이 다시 온다 | 기존 기록으로 처리하고 다시 구동하지 않는다 | `test_FR22_legacy_records_block_reexecution` |
| 출력이 일어나지 않았음을 확인했다 | `failed`를 저장한 뒤 보고한다 | `test_FR07_verified_execution_failure` |
| 제어 신호 출력을 마치고 결과 저장을 확인했다 | `succeeded`로 보고한다 | `test_FR07_output_completion_saved_before_success` |
| 출력 여부를 확정하지 못했다 | `unknown`을 저장해 보고하고 바쁨을 푼다 | `test_FR07_unconfirmed_output_is_unknown` |
| 처리 중 같은 명령이 다시 오거나 재접속한다 | 현재 처리를 유지하고 접수 기록을 불명으로 바꾸지 않는다 | `test_NFR03_at_most_once_actuation` |
| 세 기능이 시리얼·선택 GPIO 출력을 쓴다 | 출력 함수가 끝나면 출력 완료다. 출력 함수가 없으면 불명이다 | `test_NFR10_signal_output_completes` |
| 사물함 서보 각도를 출력한다 | 각도를 0~180°로 자르고 펄스 폭과 PWM duty로 바꾼다 | `test_NFR10_servo_pulse_for_angle` |
| 조명 원형 LED에 `on`·`off`가 온다 | `on`은 밝기만큼의 흰색, `off`는 꺼짐이다 | `test_NFR10_light_ring_color` |
| 사물함에 `close`가 온다 | `open`과 같은 검사를 거쳐 허용한다. 조명·스피커의 `close`는 `UNSUPPORTED_ACTION`이다 | `test_FR06_capability_action_matrix` |
| 점유 표시가 온다 | `AVAILABLE`은 파란색, `RESERVED`·`ACTIVE`·`CLOSING`·`FAULT`는 빨간색이다 | `test_FR19_occupancy_display_colors` |
| 다른 기기, 다른 버전이나 모르는 상태의 점유 표시가 온다 | 무시하고 마지막 색을 유지한다 | `test_FR19_occupancy_display_ignores_invalid_messages` |

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
- 3단계에서도 모든 기능의 제어 신호 뒤 결과는 `unknown`이다 — 담당자가 2026-10-23에 작업 범위를 정했다. 아래 2026-10-26 결정이 결과 부분을 대체한다.

- 4단계의 완료 관측 틀(비동기 완료 대기, 기능별 성공 보고 스위치, 완료 대기 기한, 관측 신선도)과 5·6단계의 반납 검사·센서 모듈은 아래 2026-10-26 결정으로 제거했다. 근거는 #195다.

- 7단계 연결 안정성은 BLE 스캔 비율, MQTT 대기 한도, watchdog과 나눈 재보고로 다룬다 — 담당자가 2026-10-24에 실측 뒤 골랐다.
  - MQTT `-9`는 QoS1 발행 뒤 대기 한도 안에 PUBACK을 못 받아 라이브러리가 연결을 닫은 것이다 (lwmqtt `MISSING_OR_WRONG_PACKET`).
  - 3분씩 잰 결과다. BLE 창 90/100ms는 `status` 전송이 중간값 216ms, 최대 2,003ms였고 `-9`가 1회 났다. 창 30/100ms는 중간값 177ms, 최대 1,903ms였고 끊김이 없었다.
  - Wi-Fi 절전을 끄면 BLE를 켤 때 `abort()`로 재시작을 반복했다. ESP32는 Wi-Fi와 BLE를 함께 쓸 때 절전을 끌 수 없다. 이 방법은 쓰지 않는다.
  - BLE 스캔 창은 30ms, 간격은 100ms다. 같은 간격 안의 RSSI 표본 수 변화는 8절에 실측으로 적는다.
  - MQTT 대기 한도(`NP_MQTT_IO_TIMEOUT_MS`)는 5초다. 실측 최대 2,003ms보다 넉넉하게 둔다.
  - loop task watchdog을 켠다. 기한은 빌드 설정 `NP_WATCHDOG_TIMEOUT_SEC`이고 15초다. 연결 한 번의 최장 대기(TCP 3초와 MQTT 응답 5초)보다 길다. 네트워크 호출 하나가 끝날 때마다 watchdog을 갱신한다. watchdog은 `setup()`의 기록 색인을 만든 뒤 켠다.
  - watchdog 재시작 뒤에도 명령 기록 때문에 다시 구동하지 않는다. 접수 기록은 `unknown`으로 복원한다.
  - 명령·검사 기록 재보고는 loop 한 번에 하나씩 보낸다. 연결이 끊기면 멈추고, 새 연결의 발표 뒤 처음부터 다시 보낸다.
- BLE 스캔 30%로 줄어든 RSSI 표본은 비콘 광고 간격 100ms로 보완한다 — 담당자가 2026-10-24에 BLE 스캔 50% 대신 골랐다.
  - 시험·시연 비콘은 legacy 광고, 연결·스캔 응답 없음, 간격 100ms다. ESP32는 extended 광고를 받지 못한다.
  - 펌웨어 설정은 바꾸지 않는다. 광고 간격은 비콘 쪽 조건이다.
  - 사용자 앱의 광고는 main의 `specs/app-beacon-broadcast/spec.md`가 `ADVERTISE_MODE_LOW_LATENCY`(Android 약 100ms)로 정했다. 이 결정과 맞는다. 앱이 이보다 느리게 광고하면 표본이 줄어 근접 판정이 `ASK`로 떨어질 수 있다.
- 검사 기록은 별도 `np_checks` NVS 파티션의 `np-checks` 이름 공간에 보존한다 — 담당자가 2026-10-24에 골랐다. 파티션은 384KiB, 슬롯은 256개, 건당 한도는 1,024바이트다. `spiffs`만 줄이고 기존 파티션 위치를 유지한다. 이론상 96페이지 중 2페이지를 빼고 34항목 기록을 페이지당 3개 넣으면 282개다. 실제 용량과 재작성은 별도 시험으로 확인한다. 아래 2026-10-26 결정으로 없앴다.
- 검사 요청을 측정 전에 저장한다. 비교 필드는 `node_id`, `criteria_id`, `criteria_version`, `target_session_id`, `issued_at`, `expires_at`이다. 저장 확인 실패 때 측정하지 않는다. 결과 저장과 확인 조회를 통과한 뒤에만 발행한다. 재시작 때 측정 전 접수 기록은 근거 없는 `unknown`으로 복원하며 다시 측정하지 않는다.
- 명령 기록은 빈 슬롯을 먼저 쓴다. 가득 차면 저장된 최종 결과가 있고 요청이 만료된 가장 오래된 기록을 교체한다 — 담당자가 2026-10-24에 확정했다. `unknown`도 최종 결과다. 접수·실행 중 기록은 교체하지 않는다. 시각 하한 `now - max_error`로 만료를 확인한다. 보관 중인 중복은 기존 결과를 돌려준다. 교체된 ID의 과거 결과는 보장하지 않으며 오래된 요청을 다시 실행하지 않는다.
- 기록의 최초 저장 UTC 시각을 보존하고 교체 순서로 쓴다. 같은 시각은 슬롯 번호로 정한다. 기존 명령 기록은 `occurred_at`을 쓴다. 기록이나 시각을 읽지 못하면 교체하지 않는다. 교체할 기록이 없으면 `STORAGE_ERROR`다. 새 요청이 이미 만료됐으면 교체를 위해 기존 기록을 지우지 않는다.

- 모든 MQTT 메시지를 `schema_version=2`로 보내고 받는다 — 담당자가 2026-10-26에 골랐다 (서버 `specs/iot-messages/spec.md`와 같다). 값은 `np_command.h`의 `SCHEMA_VERSION` 하나다. v1 명령은 `INVALID_MESSAGE`로 거부한다.
- 제어 신호 출력 함수가 끝나면 출력 완료로 보고 결과 저장·확인 조회 뒤 `succeeded`를 발행한다 — 담당자가 2026-10-26에 골랐다. 완료 관측을 기다리는 비동기 틀은 남기지 않는다.
  - `np_device`의 `Actuator::output()`은 바로 반환하며 `Completed`, `Failed`, `Unknown` 중 하나다. 시리얼·선택 GPIO 출력은 세 기능이 함께 쓰는 `SignalActuator`이고 항상 `Completed`다.
  - `Executor::handle()`은 접수 저장, 재검사, `running` 발행, 출력과 결과 저장을 한 번에 처리한다. `poll()`과 기능별 완료 대기 기한 설정(`NP_*_COMPLETION_TIMEOUT_MS`)은 없다.
  - `running`은 출력 직전에 발행하고 저장하지 않는다. 출력 중 전원이 꺼지면 접수 기록이 남아 복원에서 `unknown`이다.
  - 처리 중 도착한 명령은 MQTT 수신 콜백이 표시한 대로 `DEVICE_BUSY`로 거부한다.
- 반납 검사 모듈(`np_safety`), 센서 모듈(`np_sensor`)과 검사 기록을 제거한다 — 2026-10-26 결정. `safety_check`를 구독하지 않는다.
- 검사 전용 `np_checks` 파티션을 없애고 그 자리를 spiffs에 돌린다 — 담당자가 2026-10-26에 골랐다. `np_records`와 그 앞 파티션의 위치는 그대로라 명령 기록과 설정은 남는다. spiffs는 `0x2D0000`부터 1,152KiB다.
- `status.command_state`는 명령을 처리하는 중이면 `busy`, 아니면 `idle`이다. 명령 기록 저장소를 열지 못하면 `unknown`이다 — 담당자가 2026-10-26에 골랐다. 2026-10-24의 "센서 없는 항목을 펌웨어 상태로 채운다" 결정을 대체한다.
  - 출력은 `handle()` 안에서 끝나므로 주기 `status`는 대부분 `idle`이다. `busy`는 처리 중 발행한 보고에만 나온다.
- node_lab 시험 펌웨어도 같은 계약으로 바꾼다 — 출력 직전 재검사에서 걸리면 `failed`와 거부 코드, 출력을 마치고 저장하면 `succeeded`다. `status`는 같은 `command_state`를 보낸다.
- `light-1`의 실제 출력은 GPIO18의 WS2812B 원형 LED 16개다 — 담당자가 2026-10-27에 골랐다. 같은 날 고른 GPIO23 HIGH/LOW 출력을 대체한다.
  - `esp32dev-light-1` 빌드에서 `NP_LIGHT_RING_PIN=18`을 지정하고 `adafruit/Adafruit NeoPixel@1.12.3`을 쓴다. 개수와 밝기는 `NP_LIGHT_RING_COUNT`(16), `NP_LIGHT_RING_LEVEL`(64)이다. 밝기는 locker 점유 표시와 같다. 기본 `esp32dev`는 시리얼 모드다.
  - 허가한 새 `on`은 16개를 모두 흰색으로 켜고, `off`는 모두 끈다. 색은 담당자가 정하지 않아 흰색으로 둔다. 색 계산은 `np_device`의 `lightRingRgb()`다.
  - 부팅하면 링을 끈다. 부팅과 기존 결과 재보고는 이전 `on`을 다시 출력하지 않는다.
  - 서버가 허가한 새 명령의 기존 검사와 저장 경로를 통과한 뒤에만 링 출력을 바꾼다. 출력 함수와 명령 결과 계약은 유지한다. 실제 점등은 관측하지 않는다 (FR-07).
  - 부팅 로그 `LIGHT_RING_OUTPUT pin=18 count=16 level=64 initial=off`로 설정을 남긴다. `NODE_START`의 출력 방식은 `light_ring`이다.
  - 링 전원은 5V, GND는 ESP32와 공통이다. DIN은 3.3V 신호로 구동한다.
- `locker-1`의 실제 출력은 GPIO13의 서보다 — 담당자가 2026-10-27에 골랐다. 잠금 0°, 열림 90°다.
  - 제품 펌웨어의 `esp32dev-locker-1` 빌드 환경에서 `NP_SERVO_PIN=13`을 지정한다. 각도와 펄스 범위는 `config.example.h`의 `NP_SERVO_*` 값이다.
  - 서보 신호는 LEDC 50Hz, 16비트 duty로 낸다. 0~180°를 500~2,400µs로 바꾼다. 추가 라이브러리는 쓰지 않는다.
  - 부팅하면 잠금 각도를 낸다. 허가한 새 `open`은 열림 각도, 새 `close`는 잠금 각도 신호를 내고 바로 `Completed`다. 서보가 실제로 도달한 각도는 관측하지 않는다 (FR-07).
  - 자동으로 닫지 않는다. 열림 각도는 `close`나 재부팅까지 유지한다 — 담당자가 2026-10-27에 열림 유지 3초의 자동 되돌리기를 없앴다. 재부팅과 기존 결과 재보고는 다시 열지 않는다.
  - 서보 전원은 USB가 아닌 외부 전원으로 공급한다. 외부 전원과 ESP32의 GND를 묶는다.
- `locker-1`은 GPIO19의 WS2812B LED 스틱 8개로 점유 표시를 보인다 — 담당자가 2026-10-27에 골랐다. 서버 계약도 담당자가 직접 정했다 (architecture.md §5 MQTT 3절).
  - `esp32dev-locker-1` 빌드에서 `NP_LED_PIN=19`를 지정하고 `adafruit/Adafruit NeoPixel@1.12.3`을 쓴다. 개수와 밝기는 `NP_LED_COUNT`, `NP_LED_LEVEL`이다.
  - 부팅하면 LED를 끈다. 유효한 `occupancy`를 받으면 8개를 모두 같은 색으로 칠한다. 잘못된 표시는 무시하고 마지막 색을 유지한다.
  - 해석은 `lib/np_occupancy`가 맡는다. 판정·명령 경로와 공유하지 않는다.
  - LED 스틱 전원은 외부 5V에서 받고 GND를 공통으로 둔다. DIN은 3.3V 신호로 구동한다.
- `speaker-1`의 실제 출력은 UART2의 DFPlayer Mini 호환 모듈이다 — 담당자가 2026-10-27에 골랐다. 스피커는 3W 8Ω를 모듈의 SPK1·SPK2에 연결한다.
  - 제품 펌웨어의 `esp32dev-speaker-1` 빌드 환경에서 `NP_SPEAKER_TX_PIN=17`, `NP_SPEAKER_RX_PIN=16`을 지정한다. TX는 1kΩ을 거쳐 모듈 RX에 연결한다.
  - 라이브러리 없이 DFPlayer 명령 프레임(10바이트, 응답 요청 없음)을 9600bps로 보낸다. 모듈의 응답을 기다리지 않는다. 프레임과 체크섬은 `lib/np_device`가 만든다.
  - 허가한 새 `on`은 SD의 `NP_SPEAKER_FOLDER` 폴더(기본 `01/`)를 반복 재생(`0x17`)하고 바로 `Completed`다. `off`는 정지(`0x16`)다. 실제 소리는 관측하지 않는다 (FR-07).
  - 재생 중에 새 `on`을 받으면 명령을 다시 보내지 않고 `Completed`다. 듣던 곡을 끊지 않는다. 재생 상태는 RAM에만 두고 부팅하면 정지로 시작한다. `off`는 상태와 관계없이 정지를 보낸다.
  - 부팅하면 모듈 준비 시간(`NP_SPEAKER_BOOT_MS`)을 기다린 뒤 볼륨(`NP_SPEAKER_VOLUME`, 기본 30)과 정지를 보낸다. ESP32만 재시작해도 이전 재생을 멈춘다. 재부팅과 기존 결과 재보고는 다시 재생하지 않는다.
  - MQTT나 Wi-Fi가 끊겨도 재생을 유지한다. 펌웨어는 서버 명령으로만 출력을 바꾼다 (FR-20).
  - 모듈 전원은 ESP32 5V 핀에서 받고, 모듈 VCC·GND 사이에 1000µF를 둔다. 상태등은 달지 않는다.
  - SD 카드는 FAT32이고 파일은 `01/001.mp3`, `002.mp3`처럼 둔다.

## 7. 가정

이 spec은 docs의 계약을 따른다. 2026-10-24의 기록 교체 결정은 docs에도 반영했다.
다음 항목은 구현 전에 확인한다. 선택하지 않은 방법을 결정으로 취급하지 않는다.

| 미결 | 결정하거나 검증할 내용 | 근거와 진행 경계 |
|---|---|---|
| 구현 담당 | 펌웨어 담당의 팀 배정이다 | architecture.md §2의 미결. 담당자가 2026-10-23에 자신이 펌웨어 담당이라고 밝혔다. 이 배정을 가정하고 진행한다. 팀 배정이 다르게 나도 바뀌는 곳은 `firmware/` 안이다. 개발 환경과 내부 구조는 6절에 정했다 |
| 시각 동기화 | 시각원의 배포 위치, 연결 방법, 시작 절차, 재동기화 간격과 신뢰 상태 관리다 | 미결 — #140. 미검증 상태의 구동 차단은 유지한다 |
| 오차 실측과 설정 | 기기별 측정 조건, 최대 오차, 내부 시간 설정과 배포 설정 연결이다 | 미결 — #140. 공용 설정 이름·범위·값을 이 spec만으로 정하지 않는다 |
| MQTT 버전과 status 형식 | 메시지별 새 버전 번호, `status`의 명령 처리 상태 필드 형식과 구버전 전환 절차다 | 미결 — #196. 모든 메시지 v2, `command_state` 하나, 전환 기간 없는 v1 거부로 가정한다 (서버 `specs/iot-messages/spec.md` "가정"과 같다). 결정이 달라도 바뀌는 곳은 `np_command`의 버전 상수와 `main.cpp`의 `status`다 |
| 명령 기록 | 저장 매체, 원자적 저장, 접수·진행·결과 복원과 저장 실패 진단이다 | architecture.md §5 MQTT 4·7절. 구동 전 저장과 재실행 금지는 유지한다. 저장 방식은 6절에 정했다. node_lab은 기본 파티션 표와 `np-node-lab`만 쓰므로 제품 펌웨어의 기록을 모른다. 제품 펌웨어를 쓴 기기에 node_lab을 올리면 이미 쓴 명령 ID를 보내지 않는다 |
| 명령 기록의 보존 한도 | 기기 기록은 6절의 완료·만료 조건으로 교체한다 | 담당자가 2026-10-24에 확정했다. 서버의 명령 중복 방지 항목 정리는 FR-22의 미결이다 |

저장 방식과 구동부 연결은 담당자가 고른 뒤 6절에 기록한다.
MQTT 필드, 공용 설정과 다른 영역의 동작을 바꿔야 하면 팀 계약으로 다룬다.
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
| FR-07 | 5절의 `test_FR07_*` | 시각 신뢰, 출력 완료, 실패, 불명, 명령 처리 상태와 결과 재보고를 구분한다 |
| FR-22 | `test_FR22_command_records_survive_restart` | 재시작과 서버 외부 요청 기록 정리 뒤에도 명령 중복 방지 기록을 보존한다 |
| NFR-03 | `test_NFR03_at_most_once_actuation` | 같은 명령의 물리 실행은 1회 이하이고 동시에 구동하는 명령은 하나 이하다 |
| NFR-04 | `test_NFR04_unknown_result_kept` | 출력 불명을 성공으로 만들지 않고 서버의 격리·복구 결정을 유지한다 |
| NFR-07 | `test_NFR07_no_local_authorization` | 펌웨어가 서버 명령 없이 허가와 점유 해제를 결정하지 않는다 |
| NFR-08 | `test_NFR08_only_server_commands` | 기기별 자격정보, 자기 토픽과 서버 전용 명령 권한을 검증한다 |
| NFR-10 | `test_NFR10_signal_output_completes` | 장비별 제어 신호 출력과 출력 완료 보고를 검증한다 |
| NFR-11 | `test_NFR11_mqtt_v2_delivery` | MQTT 버전, 본문, QoS, retain, 재구독과 순번을 확인한다 |
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

4단계 검증 계획은 MQTT v2 전환에서 다음으로 바꿨다 (PC와 ESP32의 `test_common`):

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-07 | `test_FR07_output_completion_saved_before_success` | 출력 완료와 결과 저장 확인 뒤 성공하며, 저장·확인 조회 실패 때 성공을 발행하지 않는다 |
| FR-07 | `test_FR07_verified_execution_failure` | 출력이 일어나지 않았음을 확인한 결과만 실행 실패로 보고한다 |
| FR-07 | `test_FR07_unconfirmed_output_is_unknown` | 출력 여부를 모르면 `unknown`이고 바쁨을 푼다 |
| FR-07 | `test_FR07_power_loss_restores_unknown` | 진행 중 전원 차단 뒤 복원은 불명이며 재구동하지 않는다 |
| FR-07 | `test_FR07_status_reports_command_state` | `idle`, 처리 중 `busy`, 저장소 불가 `unknown`을 보고한다 |
| FR-06 | `test_FR06_concurrent_commands_busy` | 처리 중 새 명령을 거부하고 처리 뒤 바쁨을 푼다 |
| FR-06 | `test_FR06_validate_order` | v1 명령을 `INVALID_MESSAGE`로 거부한다 |
| NFR-03 | `test_NFR03_at_most_once_actuation` | 처리 중 중복·재접속으로 다시 구동하거나 조기 불명 처리하지 않는다 |
| NFR-10 | `test_NFR10_signal_output_completes` | 세 기능의 신호 출력은 출력 함수가 끝나면 완료다 |

5·6단계의 검사·센서 테스트(`test_FR20_*`, `test_FR07_verified_fault_report`, `test_FR07_status_uses_sensor_observations`, `test_FR07_check_storage_and_recovery`, `test_FR22_check_partition_capacity` 등)는 모듈과 함께 지웠다.
기록 교체 테스트(`test_FR22_replaces_oldest_expired_terminal`, `test_FR22_retention_preserves_pending_and_untrusted`, `test_FR06_evicted_command_does_not_actuate`)와 `test_FR07_replay_spreads_over_loop_iterations`는 명령 기록에 남는다.

1단계 결과 (2026-10-23, ESP32-WROOM-32E):

- `pio test -e esp32dev`: 13개 통과.
- `pio test -e native`: 13개 통과. Windows에서 WinLibs MinGW-w64 GCC 16.2.0(UCRT)을 썼다.
- 제품 펌웨어를 올린 뒤 node_lab에서 주입한 설정(generation 1)을 그대로 읽었다. `boot_check`에서 순서 위반 없이 4.8초에 `NODE_READY`가 됐다.
- node_lab이 남긴 명령 기록을 읽어 같은 사건으로 재보고했다. 서버는 새 연결을 온라인으로 확인했고 `FAULT` 차단은 그대로 유지됐다.
- 연결 직후 MQTT `-9` 끊김이 한 번 있었고 바로 다시 연결됐다. node_lab에서도 본 현상이며 7단계에서 다룬다.

2단계는 명령 처리 상태 전이를 다음 단위 테스트로 고정한다. 마지막 테스트는 실제 ESP32에서만 실행한다.

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-06 | `test_FR06_accepted_running_then_succeeded` | 접수 저장, `accepted`·`running`·`succeeded` 순서와 구동 1회를 확인한다 (v2 전환 뒤) |
| FR-06 | `test_FR06_invalid_message_no_actuation` | 읽을 수 없는 명령은 보고하지 않고, 검사에서 걸린 명령은 거부 기록을 남긴다 |
| FR-06 | `test_FR06_duplicate_after_restart` | 새 인스턴스에서도 같은 명령의 기존 결과만 재보고하고 구동하지 않는다 |
| FR-06 | `test_FR06_command_id_conflict` | 내용이 다른 같은 ID를 거부하고 기존 기록을 바꾸지 않는다 |
| FR-06 | `test_FR06_concurrent_commands_busy` | 구동 중에 받은 새 명령을 `DEVICE_BUSY`로 거부하고 기록한다 |
| FR-06 | `test_FR06_receipt_storage_failure` | 접수 저장 실패와 읽을 수 없는 기록에서 `STORAGE_ERROR`로 거부하고 구동하지 않는다 |
| FR-06 | `test_FR06_session_and_expiry_rechecked` | 접수 뒤 재검사에서 걸리면 구동하지 않고 `failed`와 거부 코드를 보고한다 |
| FR-07 | `test_FR07_power_loss_restores_unknown` | 재시작 뒤 `accepted` 기록을 `unknown`으로 저장·보고하고 다시 구동하지 않는다 |
| FR-07 | `test_FR07_output_completion_saved_before_success` | 결과 저장에 실패하면 결과를 발행하지 않고 `accepted` 기록을 남긴다 |
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

- 스캔 창을 줄이자 1초 프레임의 약 37%에 표본이 없었다. 그래서 광고 간격을 100ms로 줄여 다시 쟀다 (BLE 30/100ms).

| 비콘 광고 | 시간 | 표본이 있는 1초 프레임 | 프레임당 표본 중간값 | 최대 |
|---|---|---|---|---|
| 250ms | 2분 | 약 63% | 1 | 3 |
| 100ms | 30초 | 약 87% (26/30) | 2 | — |
| 100ms | 2분 | 약 71% | 1 | 9 |

- 2분 집계는 주변의 다른 iBeacon 프레임을 포함할 수 있다. 같은 2분 동안 `status` 대기 시간은 최대 2,112ms였고 `-9` 끊김은 없었다.
- 재부팅 직후 `MQTT_CONNECT_FAILED -3`(TCP 연결 실패)이 3회 있은 뒤 연결됐다. Wi-Fi 연결 직후의 재시도로 보이며 원인은 확인하지 않았다.
- 광고 100ms에서 근접 판정 품질(사물함 0.95 기준 도달률, 2초 전환)은 실측하지 않았다. 실측 모델을 만들 때 확인한다.
- 광고 간격을 바꾸려고 새 광고를 만들었을 때 iBeacon 제조사 데이터가 빠져 30초 동안 수신이 0건이었다. 기존 광고를 복제한 뒤 간격만 바꾼다.
- 같은 2분 동안 최종 설정의 `status` 대기 시간은 중간값 102ms, 최대 1,567ms였고 끊김이 없었다. 재보고 수백 건의 실제 소요 시간은 확인하지 않았다.

MQTT v2 전환 결과 (2026-10-26, PC와 빌드만):

- `pio test -e native`: 40개 통과다. 검사·센서 테스트 12개와 완료 관측 테스트를 지우고 출력 완료·명령 처리 상태 테스트를 더했다.
- `pio run -e esp32dev`: 통과다. RAM은 57,636바이트, Flash는 1,098,593바이트다. node_lab `esp32dev` 빌드도 통과했다.
- ESP32에서의 `test_common` 실행, 새 파티션 표 업로드와 실제 서버 연결은 하지 않았다. 다음 실제 기기 시험에서 확인한다.
- 이전 시험 기록의 `physical_completion=unverified`와 `unknown` 결과는 v1 동작이다. 새 계약의 성공으로 재해석하지 않는다.

MQTT v2 실제 기기 시험 (2026-10-26~27, ESP32-WROOM-32E, COM6, 펌웨어 `5fb89a2`):

- `pio test -e esp32dev -f test_common --upload-port COM6 --test-port COM6`: 40개 통과다. 모듈의 구동부와 저장 실패는 fake로 확인한다.
- 제품 펌웨어 빌드와 업로드가 통과했다. RAM은 57,636바이트, Flash는 1,098,593바이트다.
- 업로드는 새 파티션 표를 `0x8000`에 쓰고 해시를 확인했다. `np_records` 위치는 유지하고 없앤 검사 파티션의 자리는 `spiffs`로 쓴다.
- 재부팅에서 `node=light-1 config=loaded generation=1 mode=serial_only storage=1`을 확인했다. 전체 Flash와 NVS를 지우지 않았다.
- 이전 서버 주소로는 연결하지 못했다. 새 시험 폴더 `server/var/node-lab-v2/`를 만들고 서버·시각원 주소 `10.106.106.199`와 시험 MQTT 계정을 다시 주입했다. 기존 시험 DB는 보존했다.
- Windows 재동기화 뒤 실제 NTP 응답은 leap 0, stratum 5다. ESP32의 동기화와 재동기화를 확인했다.
- 오차 미설정(`-1`)에서는 NTP 동기화도 시작하지 않았다. 명령 서버를 끈 채 이전 실측 한도 1초를 넣고 새 오차를 측정했다.
- 150초 측정에서 유효 표본은 40개다. USB 지연을 포함한 오차 구간의 최대 절댓값은 0.3045443초다. 여유 0.25초를 더한 값은 0.5545443초다.
- 새 실측과 여유를 포함하는 1초를 서버와 기기의 연결 시험 한도로 유지했다. 재동기화 간격은 60초, 신뢰 기간은 120초다. 운영용 장시간 오차 보증은 아니다.
- 새 연결의 `announce`는 v2와 `light/shared`다. `status`는 v2와 `command_state=idle`이며 실제 상태·센서 필드가 없다. 서버는 `ready=true`로 확인했다.
- 정상 연결 시험의 `boot_check` 요약은 2.941초에 `NODE_READY`다. 순서 위반은 없다. `RECORD_STORE primary=1 legacy=1`과 watchdog 적용도 확인했다.
- 이전 명령 두 건은 기존 사건 ID·발생 시각과 `unknown`을 유지해서 재보고했다. 새 DB에 없는 명령이라 기존 `StoreError` 재인계 경고가 발생했다. 기록을 지우거나 성공으로 바꾸지 않았다.
- 실제 시험 iBeacon의 RSSI로 CORE가 `on`과 `off`를 허가했다. 두 명령 모두 MQTT v2 QoS1으로 `accepted`·`running`·`succeeded`를 차례로 보고했다.
- 각 명령에서 `COMMAND_HANDLED ... output=serial_only output_completed=1`은 한 번이다. 서버 응답은 모두 `OK / OK`이며 DB의 명령 상태는 `succeeded`다.
- 성공은 시리얼 제어 신호 출력 완료다. 시리얼 출력 외의 구동부와 물리 상태는 검증하지 않았다. 근접 모델은 합성 `synthetic-1`이며 실측 품질 검증은 아니다.
- 첫 재부팅 후 30초 관찰에서는 시각 동기화를 기다렸다. 다음 재부팅의 60초 관찰은 `NODE_READY`와 두 성공 결과의 재보고를 확인했다.
- 재보고는 새 연결 ID를 쓰고 기존 성공 사건 ID·발생 시각을 유지했다. 두 명령의 `COMMAND_HANDLED`는 다시 나오지 않았다. 이번 시험은 명령 재전송과 실제 출력 중 전원 차단을 포함하지 않는다.
- 기록 커밋 전 서버 전체 `pytest`는 3,661개 통과, 2개 생략, 1개 예상 실패다 (2026-10-27). 실제 Mosquitto를 포함했다. 펌웨어와 서버 코드는 바꾸지 않았다.
- 원본은 새 시험 폴더의 `ntp-check.jsonl`, `provision.jsonl`, `clock-bootstrap.json`, `clock-measurement-enabled.jsonl`, `hardware-trace.jsonl`, `evidence.jsonl`, `verified-summary.json`, `reboot-trace*.jsonl`이다. 주소 변경 전 부팅 로그는 `server/var/node-lab-hotspot-2/boot-v2-preflight.jsonl`이다. 모두 Git에서 제외한다.

| 동작 | 명령 ID | 성공 사건 ID |
|---|---|---|
| `on` | `89678619-40f5-4832-8f7a-db0296580e63` | `fddb86b1-f7f8-45fb-9798-e9ab378eccc6` |
| `off` | `853abe21-8fef-4950-8fa5-95569c90d39c` | `a458c414-d632-40bf-8b5a-3005c81d5529` |

### 2026-10-27 light-1 GPIO23 출력 설정

- 담당자가 GPIO23, `on` HIGH, `off` LOW를 골랐다. 제품 펌웨어에 `esp32dev-light-1` 빌드 환경을 더했다.
- `pio run -e esp32dev-light-1`과 COM6 업로드를 통과했다. RAM은 57,636바이트, Flash는 1,100,797바이트다.
- 기본 `esp32dev` 빌드도 통과했다. RAM은 57,636바이트, Flash는 1,098,593바이트다. 기기에는 GPIO 출력 빌드를 유지했다.
- `pio test -e native`의 공통 테스트 40개가 통과했다. 허가 검사, 중복 실행 방지, 출력 완료와 결과 저장 순서를 확인했다.
- ESP32 부팅 로그는 `GPIO_OUTPUT pin=23 on_level=1 off_level=0 initial_level=0`이다.
- `NODE_START`는 `node=light-1`, `generation=3`, `mode=optional_gpio`, `storage=1`이다. 기존 설정과 기록 저장소를 유지했다.
- 재시작 뒤 `CLOCK_SYNCED`를 확인했다. 실제 NTP 응답은 leap 0, stratum 5다.
- 새 연결 `8cf202b7-456f-47ee-8fea-e36c88c019a1`의 MQTT v2 `status`와 RSSI를 수신했다. 명령 처리 상태는 `idle`이다.
- 서버 전체 `pytest`는 3,646개 통과, 17개 생략, 1개 예상 실패다. 서버 코드는 바꾸지 않았다.
- 이번 작업에서 새 `on`·`off` 명령을 실행하거나 핀 전압과 LED 점등을 측정하지 않았다. 부팅 로그는 출력 설정의 확인이다.
- 부팅 원본은 `server/var/node-lab-v2/gpio23-boot-1793057308221563400.json`이다. 동기화 원본은 `gpio23-connect-1793057378062379400.json`이다. 두 파일은 Git에서 제외한다.

### 2026-10-27 locker-1 GPIO13 서보 출력 설정

- 담당자가 GPIO13, 잠금 0°, 열림 90°, 열림 유지 3초를 골랐다. 제품 펌웨어에 `esp32dev-locker-1` 빌드 환경을 더했다.
- `pio run -e esp32dev-locker-1` 컴파일을 통과했다. RAM은 57,780바이트, Flash는 1,105,505바이트다. 우리 코드의 컴파일 경고는 없다.
- 기본 `esp32dev`와 `esp32dev-light-1` 빌드도 통과했다.
- `pio test -e native`의 공통 테스트 42개가 통과했다.
- 담당자의 지시로 COM7의 사물함 노드에 `esp32dev-locker-1`을 업로드했다. 기록한 영역의 해시 검증을 통과했다.
- 서보 전원은 USB로 공급하지 않으므로 시리얼 부팅 로그 `SERVO_OUTPUT`과 실제 서보 동작은 이번 작업에서 확인하지 않았다.
- 서버 전체 `pytest`는 3,646개 통과, 17개 생략, 1개 예상 실패다. 서버 코드는 바꾸지 않았다.

### 2026-10-27 locker-1 GPIO19 점유 표시 LED

- 담당자가 GPIO19의 WS2812B 8개, 비점유 파란색, 점유 빨간색을 골랐다. 서버의 retained `occupancy` 발행과 함께 구현했다.
- `pio run`으로 `esp32dev`, `esp32dev-light-1`, `esp32dev-locker-1`을 모두 컴파일했다. locker-1은 RAM 57,932바이트, Flash 1,119,241바이트다. 우리 코드의 컴파일 경고는 없다.
- `pio test -e native`의 공통 테스트 44개가 통과했다.
- 서버 전체 `pytest`는 main 최신 위에서 3,675개 통과, 19개 생략, 1개 예상 실패다. 실제 Mosquitto로 실행한 `tests/iot/test_broker_acl.py` 11개도 통과했다.
- 담당자의 지시로 COM7의 사물함 노드에 업로드했다. 해시 검증을 통과했다. 시험 브로커 ACL에 `occupancy` 권한을 더한 뒤 node_lab으로 실기 시험을 했다.
  - 부팅 로그는 `LED_OUTPUT pin=19 count=8 level=64 initial=off`, 접속 로그는 `subscribed=cmd,occupancy`다.
  - 서버 상태가 `ACTIVE`일 때 `OCCUPANCY_DISPLAY pin=19 state=occupied`가 나왔다.
  - 2026-10-27 08:00:00 UTC 반납 뒤 LED가 파란색으로 바뀌었다. 08:00:29 `open`은 허가되어 `succeeded`로 끝났고, LED가 빨간색으로 바뀌었다. 08:00:48 반납 뒤 다시 파란색이 됐다.
  - 서보가 90°로 열린 뒤 3초 뒤 0°로 돌아오는 것을 담당자가 눈으로 확인했다. 이번 시험에서는 시리얼을 기록하지 않아 `SERVO_RELOCKED` 로그는 남지 않았다.
  - LED 색과 서보 각도는 담당자의 관찰이다. 서버의 성공 판단에는 쓰지 않는다 (architecture.md §5 MQTT 7절).
  - 시험 설정은 `threshold_locker = 0.8`이다. 요구사항의 초기값 0.95보다 낮춘 연결 시험이다.

### 2026-10-27 light-1 GPIO18 원형 LED 출력

- 담당자가 GPIO18의 WS2812B 원형 LED 16개와 밝기 64를 골랐다. 색은 정하지 않아 흰색으로 두었다. 위의 GPIO23 출력을 대체한다.
- `pio run`으로 `esp32dev`, `esp32dev-light-1`, `esp32dev-locker-1`을 모두 컴파일했다. light-1은 RAM 57,804바이트, Flash 1,112,389바이트다.
- `pio test -e native`의 공통 테스트 44개가 통과했다. `test_NFR10_light_ring_color`를 포함한다.
- 새 보드(ESP32-D0WD-V3, MAC `1c:8f:57:39:5b:d4`)를 COM9에 연결하고 업로드했다. 해시 검증을 통과했다.
  - 첫 부팅은 `LIGHT_RING_OUTPUT pin=18 count=16 level=64 initial=off`, `NODE_START node=- config=missing mode=light_ring`이다.
  - `server/var/node-lab-v2`의 값으로 USB 설정을 넣었다. `NODE_START node=light-1 config=loaded generation=1 mode=light_ring`을 확인했다.
- node-lab-v2 시험 서버로 실기 시험을 했다. 시험 설정의 조명 임계값은 0.75다.
  - 11:25 UTC에 `on`(`dc83d6d8-6d82-40f6-8953-b0a25ca7c57b`)과 `off`(`76b55edb-fcd8-435b-8b2c-a0451079a388`)가 허가되어 `succeeded`로 끝났다. 시리얼의 `COMMAND_HANDLED ... output=light_ring`을 같은 명령 ID로 연결했다.
  - 11:28 UTC에 담당자가 콘솔에서 `on`, `off`, `on`, `off`를 보냈다. 네 명령이 모두 허가되어 `succeeded`로 끝났다. 명령 ID는 `e339f52b-0958-4122-98a2-17c2fe1a2ac7`, `c68fa4b7-8e9d-4038-9001-16cd744ede27`, `73d10a14-2826-4aa3-be8d-0eab7ce08dd9`, `5b0da97c-5cbe-4eda-9e14-cfbb0726a452`다.
  - 담당자가 `on`에서 링이 켜지고 `off`에서 꺼지는 것을 눈으로 확인했다. 관찰은 서버의 성공 판단에 쓰지 않는다 (architecture.md §5 MQTT 7절).
- 서버 전체 `pytest`는 4,209개 통과, 23개 생략, 1개 예상 실패, 1개 실패다. 실패한 `tests/tools/test_eval_metric_safety.py::test_NFR04_metric6_runs_on_harness`는 전체 실행에서 두 번 연속 실패했다. 단독 실행과 `tests/tools` 실행에서는 통과했다. 이 작업은 서버 코드를 바꾸지 않았다.
- 원본은 `server/var/node-lab-v2`의 `evidence.jsonl`, `provision.jsonl`, `ring-serial.log`다. Git에서 제외한다.

실제 기기는 다음 절차로 검증한다 (architecture.md §5 MQTT 7절).

1. 기기별로 재부팅, 재접속과 장시간 동작 때 최대 시각 오차를 측정한다.
2. 시각원 단절, 시각원 변경과 시계 보정 때 시각 신뢰와 새 명령 거부를 확인한다.
3. 명령을 구동하기 직전에 대상 연결과 만료를 다시 검사하는지 확인한다.
4. 허가한 명령만 제어 신호를 출력하고, 출력 완료 뒤 결과를 저장하고 `succeeded`를 보고하는지 확인한다.
5. 접수 기록 저장 전후와 출력 중에 전원을 차단한다.
6. 기록을 복원하고 같은 명령으로 다시 구동하지 않는지 확인한다.
7. 결과 저장 실패 뒤 `succeeded`를 보고하지 않고 다음 복원에서 `unknown`을 보고하는지 확인한다.
8. 명령 결과를 재발행하고 사건 ID와 사건 내용의 보존을 확인한다.
9. 서버가 v2 `status`의 `command_state=idle`로 복원·복구를 진행하는지 확인한다.

검증 결과에는 기기 ID, 펌웨어 버전, 설정, 측정 조건과 관측 근거를 기록한다.
