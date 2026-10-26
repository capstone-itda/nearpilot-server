# NearPilot 기기 펌웨어

2026-10-26에 센서 기반 물리 상태 검증과 반납 검사를 범위에서 제외했다 (`docs/product.md`, `docs/requirements.md`).
이 README와 `specs/firmware-contracts/spec.md`의 모듈·검사 설명은 현재 구현의 기록이다.
센서 제외 구현은 아직 반영하지 않았다. 후속 fix PR에서 코드와 spec을 함께 바꾼다.
새 목표는 제어 신호 출력 완료 보고와 논리적 점유 해제다. 실제 상태와 물리 안전은 검증하지 않는다.

모든 기기 종류가 함께 쓰는 ESP32 펌웨어다. 계약과 단계는 `specs/firmware-contracts/spec.md`를 따른다.
`node_lab/`은 연결 시험용 별도 프로젝트다 (`specs/core-node-lab/spec.md`).

## 구조

| 경로 | 내용 |
|---|---|
| `lib/np_config/` | USB로 주입한 기기 설정의 검사와 16진수 값 변환 |
| `lib/np_clock/` | 시각 신뢰 판정과 서버 UTC 시각 해석 |
| `lib/np_command/` | MQTT v1 `cmd`의 기능별 동작 검사, 검사 순서와 명령 내용 비교 필드 |
| `lib/np_ledger/` | 명령 기록, ID 해시 색인과 저장소 인터페이스 `RecordStore` |
| `lib/np_executor/` | 비동기 명령 상태 전이, 완료 근거 검사, 결과 저장과 재보고 |
| `lib/np_device/` | 구동 시작·완료 관측 인터페이스, 기능별 완료 정책과 관측 없는 세 기능 연결 |
| `lib/np_sensor/` | 새 물리 관측과 신선도 검사, 센서 기반 상태 보고 |
| `lib/np_safety/` | 반납 검사 접수 저장, 새 측정, v1 결과, 중복·충돌과 재시작 복원 |
| `lib/np_nvs/` | ESP32 NVS로 만든 `RecordStore`. PC 테스트에서는 빌드하지 않는다 |
| `lib/np_beacon/` | iBeacon 제조사 데이터 해석 |
| `lib/np_ready/` | 준비 사유 순서와 재시도 간격 |
| `src/main.cpp` | Wi-Fi, MQTT, BLE, NVS, 시리얼을 모듈에 연결한다 |
| `partitions.csv` | spiffs를 줄여 명령 전용 `np_records`(256KiB)와 검사 전용 `np_checks`(384KiB)를 둔다 |
| `test/test_common/` | 모듈 단위 테스트 (Unity). ESP32와 PC에서 실행한다 |
| `test/test_esp32_storage/` | 실제 `np_records` 파티션의 용량과 조회 시간 측정. ESP32에서만 실행한다 |
| `test/test_check_storage/` | 실제 `np_checks` 파티션의 저장·재작성 용량 측정. ESP32에서만 실행한다 |

`lib/`의 모듈은 Arduino 헤더를 쓰지 않는다. 장치 기능은 인터페이스로 넣고 테스트는 fake를 쓴다.
`np_nvs`만 ESP-IDF NVS API를 쓰며 `native` 환경에서 빼 둔다.

## 빌드와 테스트

PlatformIO는 `node_lab/.venv`에 설치한 것을 쓴다. 아래 명령은 `firmware/`에서 실행한다.

```bash
pio run -e esp32dev
pio run -e esp32dev -t upload --upload-port COM6
pio test -e esp32dev -f test_common --upload-port COM6 --test-port COM6
pio test -e esp32dev -f test_check_storage --upload-port COM6 --test-port COM6
pio test -e native
```

`native`는 PC에서 실행하며 PATH에 `gcc`·`g++`가 필요하다.
Windows에서는 `winget install BrechtSanders.WinLibs.POSIX.UCRT`로 설치한 MinGW-w64 GCC로 검증했다.

## 설정과 확인

3단계부터 USB 설정은 기존 11개 키에 `capability`와 `occupancy`를 더한 13개 문자열 키를 요구한다.
`capability`는 `locker`·`light`·`speaker`, `occupancy`는 `rental`·`shared`다. 조합을 추가로 제한하지 않는다.
`CONFIG_BEGIN` → `CONFIG_SET <key> <UTF-8 값의 16진수>` → `CONFIG_COMMIT` 절차를 유지한다.
새 키가 없거나 허용 값이 아니면 키 이름으로 거부한다. 기존 설정에는 기본값을 채우지 않는다.
기존 generation 1 설정은 재주입 전 부팅 시 `config=invalid`, `configured=0`이다. 저장된 설정과 기록은 지우지 않는다.
현재 `python -m tools.node_lab provision`은 새 키를 전달하지 못한다. 도구 변경은 별도 범위다.
부팅 확인 도구와 `CONFIG_STATE` 형식은 유지한다 (`server/tools/node_lab/README.md`).
`announce`는 설정 값을 발표한다. `locker`는 `open`, `light`·`speaker`는 `on`·`off`만 받는다.
모든 구동은 시리얼·선택 GPIO 신호이며 결과는 `unknown`이다. 실제 완료 근거는 #142의 미결이다.
`handle()`은 구동을 시작하고 `poll()`은 `loop()`에서 완료를 확인한다. 완료를 기다리는 동안 새 명령은 `DEVICE_BUSY`다.
`np_device`의 구동부와 완료 관측을 기능별로 교체한다. 관측은 명령 ID, 단조 관측 시각과 근거 출처를 담는다.
시작 전 관측, 다른 명령, 출처 누락과 미래 관측은 완료 근거가 아니다. 문 닫힘과 IR 송출을 성공으로 추정하지 않는다.
`NP_LOCKER_COMPLETION_TIMEOUT_MS`, `NP_LIGHT_COMPLETION_TIMEOUT_MS`, `NP_SPEAKER_COMPLETION_TIMEOUT_MS`는 밀리초 단위다.
0은 미설정이며 양수가 대기 기한이다. 미측정 수치를 넣지 않는다. 관측이나 기한이 없으면 즉시 `unknown`이다.
완료 증명은 기능별 `CompletionPolicy.successEnabled`를 켠 뒤에만 성공으로 보고한다. 실제 세 기능은 모두 꺼져 있다.
완료 결과는 저장과 확인 조회를 통과한 뒤 발행한다. `running`은 저장하지 않는다.
재접속은 현재 구동을 유지한다. 재시작 뒤 접수 기록은 `unknown`이며 자동으로 다시 구동하지 않는다.
기록이 있어도 기능·점유 방식 변경을 허용한다. `node_id` 변경 차단과 기존 명령 결과 재보고를 유지한다.
서버는 기능·점유 방식 변경을 발표한 신뢰 기기를 `discovered`로 되돌린다 (FR-04).
설정은 두 펌웨어 모두 기본 NVS의 `np-config`에 둔다. 펌웨어를 바꿔 올려도 저장 값은 유지되지만 새 필수 키가 없으면 제품 펌웨어가 사용하지 않는다.
node_lab은 추가 키를 무시하고 설정을 읽는다. 발표는 `light/shared`로 고정이며 재저장하면 추가 키를 없앤다.
명령 기록은 제품 펌웨어가 `np_records` 파티션의 `np-records`에 쓴다. node_lab이 기본 NVS `np-node-lab`에 남긴 기록도 조회해서 다시 구동하지 않는다.
node_lab은 제품 펌웨어의 기록을 모른다. 제품 펌웨어를 쓴 기기에 node_lab을 올리면 이미 쓴 명령 ID를 보내지 않는다.
`np_records` 파티션을 열지 못해도 펌웨어는 지우지 않는다. 부팅 로그 `RECORD_STORE`와 준비 사유 `storage_unavailable`로 알린다.
`test_esp32_storage`는 같은 파티션의 `np-test` 이름 공간을 채웠다가 지운다. 실제 기록이 쌓이기 전에 실행한다.
`test_check_storage`는 `np_checks`의 `np-check-test` 이름 공간만 채웠다가 지운다. 운영 검사 기록이 쌓이기 전에 실행한다.
테스트 프로그램을 올린 뒤 제품을 사용할 때는 제품 펌웨어를 다시 올린다. USB 설정을 다시 주입하지 않는다.

## 반납 검사와 상태 보고

`safety_check`는 기능에 맞는 `locker-return`, `light-return`, `speaker-return`의 버전 1을 지원한다.
요청의 비교 필드는 `node_id`, `criteria_id`, `criteria_version`, `target_session_id`, `issued_at`, `expires_at`이다.
같은 ID·같은 요청은 저장된 결과를 반환한다. 내용이 다른 같은 ID에는 응답하지 않고 기존 기록을 보존한다.
검사 접수를 NVS에 저장하고 확인한 뒤 새로 측정한다. 결과도 저장과 확인 조회를 통과한 뒤 발행한다.
측정 전에 재시작했거나 결과 저장에 실패한 접수 기록은 다음 복원에서 `unknown`으로 바꾼다. 다시 측정하지 않는다.
검사 기록은 `np_checks`의 `np-checks`에 최대 256건을 저장한다. 건당 한도는 1,024바이트다.
`NP_MAX_CHECK_RECORDS`, `NP_MAX_CHECK_RECORD_BYTES`가 빌드 설정이다. 늘리기 전에 파티션 용량을 검증한다.
센서는 `Sensors::read(Measurement)`로 연결한다. 반환 관측은 측정 호출 안의 단조 시각, UTC 시각과 출처를 담는다.
리드 스위치의 문 닫힘을 잠금 관측으로 쓰지 않는다. 조명·스피커 v1은 꺼짐이 아니라 구동부 정지를 검사한다.
현재 `NoSensors`는 모든 관측을 불명으로 둔다. 반납 검사 근거와 `door_closed`·`light_on`은 `null`이다.
상태 보고에서 센서가 없는 항목만 펌웨어 상태로 채운다. `actuator_state`는 명령 처리 중이면 `busy`, 아니면 `idle`이고 `sensor_fault`는 `false`다.
이 `idle`은 구동부가 실제로 멈췄다는 근거가 아니다. 센서를 설치하면 `sensesActuator()`·`detectsFaults()`를 켜고 센서 값을 쓴다.
반납 검사는 `unknown`과 빈 근거다. 반납 근거에는 명령 처리 중 여부나 선택 GPIO 값을 쓰지 않는다.

명령·검사 기록은 빈 슬롯을 먼저 쓴다. 가득 차면 완료·만료된 가장 오래된 기록을 새 기록으로 교체한다.
명령의 `unknown`도 최종 결과다. 접수·실행 중 기록은 교체하지 않는다. 만료 확인에는 시각 오차를 뺀 하한을 쓴다.
기존 node_lab 기록은 계속 조회하고 제자리에서 복원하지만, 새 명령의 교체 슬롯으로 쓰지 않는다.
교체할 기록이 없거나 시각을 신뢰하지 못하면 명령은 `STORAGE_ERROR`, 검사는 무응답으로 확인 불능이다.
교체된 ID의 과거 결과 재응답은 보장하지 않는다. 오래된 요청은 연결·만료 검사로 막으며 다시 구동하거나 측정하지 않는다.
`include/config.example.h`는 모든 기기가 공유하는 빌드 값이다. 바꿀 때만 `include/config.local.h`로 복사한다.

## 연결 안정성

Wi-Fi와 BLE는 무선 하나를 시간으로 나눠 쓴다. BLE 스캔 창이 길면 MQTT 확인 응답(PUBACK)이 늦는다.
MQTT `-9`는 QoS1 발행 뒤 `NP_MQTT_IO_TIMEOUT_MS` 안에 PUBACK을 못 받아 라이브러리가 연결을 닫은 것이다.
그래서 BLE 스캔은 100ms 중 30ms만 쓰고, 대기 한도는 5초다. Wi-Fi 절전은 끌 수 없다. BLE와 함께 쓰면 부팅 중 `abort()`가 난다.
`MQTT_REPORT ... ms=`는 발행 하나가 응답까지 걸린 시간이다.
loop task watchdog은 `NP_WATCHDOG_TIMEOUT_SEC`(15초) 동안 갱신이 없으면 기기를 재시작한다. 부팅 로그 `WATCHDOG`에 기한과 적용 여부가 나온다.
watchdog은 네트워크 호출마다 갱신한다. 그래서 기한은 TCP 연결 3초와 MQTT 대기 한도의 합보다 길어야 한다(빌드 검사).
재시작 뒤에도 명령 기록 때문에 다시 구동하지 않는다. 연결 직후 기록 재보고는 loop 한 번에 하나씩 보낸다.
시험·시연 비콘은 legacy 광고 100ms 간격으로 둔다. 스캔 30%에서 250ms 광고는 1초 구간의 약 37%에 표본이 없었다.
