# NearPilot 기기 펌웨어

모든 기기 종류가 함께 쓰는 ESP32 펌웨어다. 계약과 단계는 `specs/firmware-contracts/spec.md`를 따른다.
`node_lab/`은 연결 시험용 별도 프로젝트다 (`specs/core-node-lab/spec.md`).

## 구조

| 경로 | 내용 |
|---|---|
| `lib/np_config/` | USB로 주입한 기기 설정의 검사와 16진수 값 변환 |
| `lib/np_clock/` | 시각 신뢰 판정과 서버 UTC 시각 해석 |
| `lib/np_command/` | MQTT v1 `cmd`의 검사 순서와 명령 내용 비교 필드 |
| `lib/np_ledger/` | 명령 기록, ID 해시 색인과 저장소 인터페이스 `RecordStore` |
| `lib/np_executor/` | 명령 처리 상태 전이 (조회, 검사, 접수 저장, 재검사, 구동, 결과 저장, 재보고) |
| `lib/np_nvs/` | ESP32 NVS로 만든 `RecordStore`. PC 테스트에서는 빌드하지 않는다 |
| `lib/np_beacon/` | iBeacon 제조사 데이터 해석 |
| `lib/np_ready/` | 준비 사유 순서와 재시도 간격 |
| `src/main.cpp` | Wi-Fi, MQTT, BLE, NVS, 시리얼을 모듈에 연결한다 |
| `partitions.csv` | 기본 4MB 배치에서 spiffs를 줄여 명령 기록 전용 `np_records` 파티션(256KB)을 둔다 |
| `test/test_common/` | 모듈 단위 테스트 (Unity). ESP32와 PC에서 실행한다 |
| `test/test_esp32_storage/` | 실제 `np_records` 파티션의 용량과 조회 시간 측정. ESP32에서만 실행한다 |

`lib/`의 모듈은 Arduino 헤더를 쓰지 않는다. 장치 기능은 인터페이스로 넣고 테스트는 fake를 쓴다.
`np_nvs`만 ESP-IDF NVS API를 쓰며 `native` 환경에서 빼 둔다.

## 빌드와 테스트

PlatformIO는 `node_lab/.venv`에 설치한 것을 쓴다. 아래 명령은 `firmware/`에서 실행한다.

```bash
pio run -e esp32dev
pio run -e esp32dev -t upload --upload-port COM6
pio test -e esp32dev --upload-port COM6 --test-port COM6
pio test -e native
```

`native`는 PC에서 실행하며 PATH에 `gcc`·`g++`가 필요하다.
Windows에서는 `winget install BrechtSanders.WinLibs.POSIX.UCRT`로 설치한 MinGW-w64 GCC로 검증했다.

## 설정과 확인

1단계에서는 시리얼 설정 명령과 로그 형식이 node_lab과 같다.
설정 주입은 `python -m tools.node_lab provision`으로, 부팅 확인은 `python -m tools.node_lab.boot_check`로 한다 (`server/tools/node_lab/README.md`).
설정은 두 펌웨어 모두 기본 NVS의 `np-config`에 둔다. 펌웨어를 바꿔 올려도 설정이 유지된다.
명령 기록은 제품 펌웨어가 `np_records` 파티션의 `np-records`에 쓴다. node_lab이 기본 NVS `np-node-lab`에 남긴 기록도 조회해서 다시 구동하지 않는다.
node_lab은 제품 펌웨어의 기록을 모른다. 제품 펌웨어를 쓴 기기에 node_lab을 올리면 이미 쓴 명령 ID를 보내지 않는다.
`np_records` 파티션을 열지 못해도 펌웨어는 지우지 않는다. 부팅 로그 `RECORD_STORE`와 준비 사유 `storage_unavailable`로 알린다.
`test_esp32_storage`는 같은 파티션의 `np-test` 이름 공간을 채웠다가 지운다. 실제 기록이 쌓이기 전에 실행한다.
`include/config.example.h`는 모든 기기가 공유하는 빌드 값이다. 바꿀 때만 `include/config.local.h`로 복사한다.
