# ESP32 최소 연결 시험 펌웨어

추가 보드 없이 ESP32 자체를 쓴다.
BLE iBeacon 수신, MQTT 보고, 서버 명령 도착과 처리 로그를 확인한다.
실제 보드 모델과 내장 LED 유무는 아직 확인하지 않았다.
`esp32dev`는 컴파일 대상 예시다. ESP32-C3·S3는 실제 보드에 맞는 PlatformIO 환경으로 바꾼다.

## 지금 할 수 있는 것

보드 없이 펌웨어를 빌드한다. 업로드와 BLE·Wi-Fi·시각 오차 검증은 보드를 받은 뒤 한다.

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install platformio==6.1.18
.venv/Scripts/python -m platformio run
```

위 명령의 작업 폴더는 `firmware/node_lab/`이다.
서버 도구는 `server/tools/node_lab/README.md`를 따른다.

## 비콘을 보내는 앱도 없을 때

보유한 ESP32 중 한 대를 선택 송신기로 쓸 수 있다. 추가 보드 구매를 요구하지 않는다.
송신기에는 아래 환경을 업로드하고 수신 기기에는 기본 `esp32dev` 환경을 업로드한다.

```powershell
.venv/Scripts/python -m platformio run --environment ibeacon
.venv/Scripts/python -m platformio run --environment ibeacon --target upload
```

송신기는 MQTT에 접속하지 않고 `NP_BEACON_UUID`, `NP_BEACON_MAJOR`, `NP_BEACON_MINOR`를 광고한다.
값을 서버 `lab.json`의 비콘과 맞춘다. 기본값은 서로 같다.
수신 확인에는 송신원과 수신 기기가 각각 필요하다. 기기 간 근접 비교에는 수신 기기 2대 이상을 쓴다.
ESP32 한 대만 있으면 비콘 송신과 다른 기기의 실제 수신을 함께 검증할 수 없다.

## 보드를 받은 뒤 설정

모든 기기에 같은 펌웨어를 올린다. 기기별 값은 빌드에 넣지 않는다.

1. 보드 모델을 확인하고 업로드한다. 업로드는 NVS를 지우지 않는다.
2. 처음 부팅한 기기는 `CONFIG_MISSING`을 출력하고 네트워크를 시작하지 않는다.
3. `server/tools/node_lab/README.md`의 "USB 설정 주입"으로 기기 ID, MQTT 계정, Wi-Fi, 서버·시각원 주소와 시각 신뢰 값을 넣는다.
   최대 시각 오차는 서버 `delivery.clock_skew_sec`의 값을 그대로 쓴다.
4. 이후에는 전원을 켜거나 EN을 누르면 자동으로 연결한다.

`include/config.example.h`에는 모든 기기가 공유하는 주기·한도만 있다. 바꿀 때만 `config.local.h`로 복사한다.

```powershell
.venv/Scripts/python -m platformio run --target upload
.venv/Scripts/python -m platformio device monitor
```

최대 시각 오차 `-1`은 미검증이다. 임의로 0으로 바꾸지 않는다.
재동기화 간격과 신뢰 기간 0도 미설정이다. 미설정인 동안 새 출력을 차단한다.
시각 동기화만으로 오차 실측이 끝났다고 보지 않는다.
동기화와 검증된 오차 한도가 모두 있어야 status·RSSI 전송과 새 명령 출력을 허용한다.
시각 신뢰 기간이 지나면 새 출력을 다시 차단한다.

## 확인 로그

| 시리얼 로그 | 뜻 |
|---|---|
| `NODE_START` | 기기 ID, 설정 상태·세대, 시리얼·GPIO 모드, NVS 준비 상태와 재시작 원인이다 |
| `CONFIG_MISSING` | 주입한 설정이 없어 네트워크를 시작하지 않았다 |
| `CONFIG_STAGED`, `CONFIG_SAVED`, `CONFIG_REJECTED`, `CONFIG_STATE` | USB 설정 주입의 단계와 결과다. 비밀값은 바이트 수와 설정 여부만 쓴다 |
| `WIFI_CONNECTING`, `WIFI_CONNECTED`, `WIFI_DISCONNECTED`, `WIFI_RETRY` | Wi-Fi 접속, 끊김 원인 코드와 다음 재시도 간격이다 |
| `CLOCK_SOURCE` | 시각원 주소, 재동기화 간격, 신뢰 기간과 최대 오차다 |
| `CLOCK_SYNCED`, `CLOCK_TRUSTED`, `CLOCK_TRUST_EXPIRED` | 동기화, 신뢰 시작과 신뢰 만료다 |
| `CLOCK_RETRY` | 신뢰가 없는 동안 `NP_RETRY_MAX_MS`마다 SNTP를 다시 요청했다 |
| `MQTT_CONNECT_FAILED` | 브로커 주소, 라이브러리 오류, CONNACK 코드와 다음 재시도 간격이다 |
| `MQTT_DISCONNECTED` | 끊긴 연결 ID와 오류다 |
| `ANNOUNCE_DEFERRED`, `ANNOUNCED` | 시각 신뢰 전 발표 보류와 그 연결의 발표다 |
| `NODE_READY`, `NODE_NOT_READY` | 기기 쪽 연결 조건의 충족과 첫 번째 차단 이유다. 실행 허가가 아니다 |
| `BLE_BEACON` | UUID·major·minor, 평균 RSSI와 표본 수다 |
| `MQTT_CONNECTED` | 새 연결 ID로 접속하고 명령 토픽을 구독했다. 발표는 시각 신뢰 뒤에 한다 |
| `MQTT_REPORT` | 보고 종류, 발행 순번과 MQTT 발행 결과다 |
| `COMMAND_RECEIVED` | 명령 ID를 읽었다 |
| `COMMAND_DUPLICATE` | NVS의 기존 결과를 재보고했다. 다시 출력하지 않는다 |
| `COMMAND_HANDLED` | 처리한 명령 ID·동작과 시리얼·GPIO 모드다 |
| `COMMAND_RESULT` | 서버에 보고한 결과다. 이 최소 구현은 물리 성공을 보고하지 않는다 |
| `STORAGE_BLOCKED` | 기록 손상·한도 때문에 새 출력을 차단했다. 기록은 삭제하지 않는다 |
| `SAFETY_CHECK_UNSUPPORTED` | 실제 센서가 없어 안전 통과 근거를 만들지 않았다 |

기본 `NP_OUTPUT_PIN=-1`은 시리얼 모드다. GPIO를 제어하지 않는다.
내장 LED가 있는 보드라면 회로를 확인하고 핀과 active-high 여부를 설정한다.
임의의 GPIO 번호를 가정하지 않는다. 부팅 핀과 입력 전용 핀은 보드 자료로 확인한다.
GPIO를 썼더라도 제어 신호만으로 실제 켜짐을 증명하지 못하므로 `light_on=null`, 실행 결과는 `unknown`이다.
서버의 FAULT 격리는 이 결과를 보수적으로 반영한 것이다.

## 보존과 범위

- MQTT 3.1.1, QoS·retain과 v1 필드를 기존 계약대로 쓴다.
- NimBLE에서 Apple iBeacon manufacturer data 25바이트를 읽는다.
- RSSI는 약 1초 칸으로 평균한다. 비콘 슬롯과 보고 주기는 설정에서 받는다.
- 연결마다 새 UUID와 토픽별 발행 순번을 쓴다. 연결마다 시각을 신뢰한 뒤 한 번 발표한다.
- Wi-Fi와 MQTT 재시도 간격은 최소값에서 두 배씩 늘어 `NP_RETRY_MAX_MS`에서 멈춘다.
- 설정은 `np-config` 이름공간의 JSON 하나로 저장한다. 명령 기록은 `np-node-lab` 이름공간에 따로 둔다.
- 명령 ID와 내용을 NVS에 저장하고 읽어 확인한 뒤 처리한다.
- 재전송은 저장한 사건 ID·결과·시각을 유지한다. 재부팅 뒤 미완료 기록은 unknown으로 바꾼다.
- NVS 슬롯이 차면 새 명령을 차단한다. 중복 방지 기록을 자동 삭제하지 않는다.
- TLS, 실제 사물함·스피커·센서와 반납 검사는 이 시험 펌웨어의 범위 밖이다.
- 시리얼 로그와 라이브러리 컴파일은 실제 기기의 검증 완료가 아니다.

## 사용한 공식 자료

- [PlatformIO ESP32 환경](https://docs.platformio.org/en/stable/boards/espressif32/esp32dev.html)
- [NimBLE 스캔 예제](https://github.com/h2zero/NimBLE-Arduino/blob/master/examples/NimBLE_Scan_Continuous/NimBLE_Scan_Continuous.ino)
- [MQTT 라이브러리와 QoS API](https://github.com/256dpi/arduino-mqtt)
- [ESP32 Preferences](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/preferences.html)
- [ArduinoJson 7](https://arduinojson.org/v7/api/jsondocument/)
