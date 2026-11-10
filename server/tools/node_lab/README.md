# ESP32 관측과 명령 연결 시험

2026-10-26에 센서 기반 물리 검증과 반납 검사를 범위에서 제외했다 (`docs/requirements.md` FR-07·FR-20).
서버 쪽 시험 도구와 시험 펌웨어(`firmware/node_lab`)는 MQTT v2를 쓴다. 상태 표시는 `command_state`이고 검사 기준과 검사 ACL을 쓰지 않는다.
이전 v1 시험 펌웨어를 올린 기기의 보고는 서버가 거부한다. 펌웨어를 다시 올린다.
`HARDWARE_VALIDATION.md`의 과거 실측과 `KNOWN_ISSUES.md`의 기존 관찰은 보존한다.

앱, 로그인 화면, REST, WebSocket 없이 기존 서버를 직접 부른다.
서버의 허가와 실행 직전 검사는 유지한다. 시험 폴더는 운영 DB와 분리한다.

## 보드 없이 지금 확인하기

`server/`에서 실행한다. 서버의 개발 의존성과 Mosquitto가 필요하다.

```powershell
python -m tools.node_lab init --mode sim
python -m tools.node_lab demo
```

`init`은 `var/node-lab/`에 설정, 기기별 비밀번호, ACL과 합성 모델을 만든다.
기존 폴더가 있으면 덮어쓰지 않는다. 재생성하려면 `--dir var/node-lab-2`로 새 경로를 쓴다.
`demo`는 임시 로컬 브로커와 가상 기기 2대를 시작한다.
실제 HMM으로 확률을 계산하고, 확률이 가장 큰 기기를 지정해 `on`, `off`를 요청한다. 기기가 둘이라서 `nearest:`는 쓰지 않는다 (아래 근접 지정 설명).
CORE가 허가한 명령만 기존 MQTT 경로로 보낸다.

## 화면과 로그 읽기

같은 결과를 화면과 `var/node-lab/evidence.jsonl`에 쓴다.
관측은 `lab.json`의 `sample_sec`마다 하고 모두 파일에 쓴다. 화면의 `node_observation`, `proximity`, `proximity_pending`은 `console_sec`마다 한 번만 보인다. `console_sec`가 없으면 매 관측을 보인다. 나머지 사건과 `watch` 입력의 결과는 항상 보인다.

| event | 확인한 내용 |
|---|---|
| `node_configured` | 등록한 시험 기기의 발표를 받아 장소·신뢰·정책을 연결했다 |
| `inventory_reloaded` | 새 기기를 기존 복원 경로로 읽도록 시험 Runtime을 재시작했다 |
| `node_readiness` | 연결 ID, 온라인, 신뢰, 장소, `FAULT`, 실행 가능 여부·차단 사유가 바뀌었다. 바뀐 때만 쓴다 |
| `node_observation` | RSSI, 관측·수신 시각, 명령 처리 상태, 실행 가능 여부와 차단 사유다 |
| `proximity_pending` | 유효한 현재 비콘 관측이 아직 없다 |
| `proximity` | 기기별 사후확률, 가장 높은 기기, 임계값과 모델 버전이다 |
| `decision` | 서버의 허가·재질문·거부와 사유다 |
| `command_result` | 요청 ID, 명령 ID와 저장한 실행 결과다 |
| `server_diagnostic` | 기존 서버가 보고를 거부하거나 인계를 재시도한 이유다 |
| `demo_passed` | 가상 기기의 관측과 on/off가 연결됐다. 실제 보드 검증은 아니다 |

`audit.jsonl`은 기존 서버의 감사 기록이고 `broker.log`는 브로커 오류다.
기존 CORE의 결과 재인계에서 ValueError 경고가 관측되었다. 성공 저장과 인계 경고를 구분한다.
재현과 후속 작업 초안은 `KNOWN_ISSUES.md`에 있다. 시험 도구가 경고를 숨기거나 CORE를 고치지 않는다.
화면의 확률은 합성 모델 기준이다. 실제 정확도를 뜻하지 않는다.
`frame_ts`와 `received_at`은 RSSI 시각이고 `status_observed_at`과 `status_received_at`은 상태 보고의 시각이다.
명령 직후 보이는 이전 상태 보고를 새 명령의 완료 근거로 쓰지 않는다.
RSSI 보고 수신, 근접 확률 변화, 명령 수신과 실제 물리 성공을 구분한다.

## 콘솔로 직접 호출하기

첫 터미널에서 브로커를 실행한다.

```powershell
python -m tools.node_lab broker
```

두 번째 터미널에서 기기 시뮬레이터를 실행한다. 실제 ESP32가 있으면 이 명령을 생략한다.

```powershell
python -m tools.node_sim run --config var/node-lab/fleet.json
```

세 번째 터미널에서 콘솔을 실행한다. `demo`와 동시에 실행하지 않는다.

```powershell
python -m tools.node_lab run
```

콘솔에서 아래 명령을 쓴다. 관측 로그는 자동으로 계속 나온다.

```text
watch
on
off
open
close
release
recover locker-1
quit
```

`on`과 `off`는 `nearest:light`를 요청한다. 비콘이 없거나 확률이 낮으면 실행하지 않는다.
`open`은 `nearest:locker`를 요청한다. 허가되면 대여가 생기고 `decision`에 `use_id`가 나온다.
`close`는 `nearest:locker`의 잠금 신호를 요청한다. 본인 대여 중일 때만 실행하고 대여는 유지한다. 대여가 없으면 `USE_NOT_FOUND`다.
`release`는 콘솔에서 마지막으로 연 대여를 반납한다. `release <use_id>`로 지정해도 된다. 반납은 서버의 논리적 점유 해제이며 문 닫힘을 확인하지 않는다.
`on light-1`처럼 명시 지정도 입력할 수 있다.
`recover <node_id>`는 시험 장소 소유자(`owner_id`)로 `FAULT` 복구를 승인한다. 운영의 `nearpilot recover`와 같은 복구 서비스를 부른다. 먼저 준비 상태(`recovery_readiness`)를 남기고, `ready`일 때만 승인한 결과(`recovery_approval`)를 남긴다. DB를 직접 고치지 않는다.
다만 명시 지정 임계값은 미결이므로 기본 설정은 명시 실행을 허가하지 않는다.
시험 도구가 실행되면서 발표한 기기만 초기 복원 목록에 없으면 실행이 차단된다.
도구는 첫 발표 후 Runtime을 정상 재시작해서 이 목록을 읽는다. 진행 중인 명령은 종료 절차가 기다린다.

## 실제 ESP32를 받은 뒤

기본 펌웨어와 빌드는 저장소의 `firmware/node_lab/README.md`를 따른다.
추가 보드는 필요 없다. 기본 출력은 시리얼 로그다.

1. 서버 PC와 ESP32를 같은 AP에 연결한다.
2. 별도 시험 폴더를 만든다. 아래 주소를 서버 PC의 AP 주소로 바꾼다.

```powershell
python -m tools.node_lab init --mode hardware --dir var/node-lab-hardware --host 192.168.0.10 --node light-1 --node light-2
```

   기기 ID의 접두사가 기능을 정한다. `light-`는 공용형 조명, `locker-`는 대여형 사물함이다. 사물함은 제품 펌웨어(`firmware/`의 `esp32dev-locker-1`)를 쓴다.
3. 실제 비콘의 UUID·major·minor를 `lab.json`에 넣는다. 처음 생성할 때 `--uuid`, `--major`, `--minor`로 지정해도 된다.
4. 시각은 기기가 MQTT로 서버에 맞춘다 (#236). `server.toml`의 `[delivery]` 시각 값(오차 0.5초, 신뢰 120초, 재동기화 60초)을 확인한다.
5. 모든 기기에 같은 펌웨어를 올리고, 아래 "USB 설정 주입"으로 기기마다 ID·접속 정보·네트워크를 넣는다.
6. 첫 터미널에서 `broker --dir var/node-lab-hardware`, 두 번째에서 `run --dir var/node-lab-hardware`를 실행한다.
7. iBeacon을 송출하고 ESP32 시리얼의 `BLE_BEACON`과 서버의 `node_observation`을 비교한다.
   송출 앱이 없으면 보유 ESP32 한 대를 `firmware/node_lab/`의 선택 `ibeacon` 환경으로 쓴다.
8. 위치를 바꾸면서 `proximity`의 확률 변화를 본다. 기기 간 선택에는 앵커 2대 이상을 쓴다.
9. 콘솔에서 `on`을 입력한다. `decision=OK`, ESP32의 `COMMAND_RECEIVED`·`COMMAND_HANDLED`, 서버의 `command_result`를 같은 명령 ID로 연결한다.

기기는 `time_request`를 보내고 서버의 `time` 응답으로 시각을 맞춘다. 왕복 시간의 절반이 오차 상한이다.
오차 상한을 넘는 응답은 버리고, 시각을 신뢰하기 전에는 보고를 보내지 않고 새 출력을 차단한다.
NTP 서버와 기기별 오차 실측은 필요 없다. Windows 방화벽의 MQTT 포트 허용은 환경에서 준비한다. 최종 시연 구성(`tools.final_demo`)은 방화벽 규칙 스크립트 `firewall.ps1`을 함께 만든다.

합성 모델은 `models/synthetic-2.json`이다. 저장소의 공통 합성 모델(`config/final-demo/models/synthetic-2.json`)을 그대로 복사하며, 기기와 좌표를 모른다.
실측 모델을 쓸 때는 `server.toml`의 `[proximity]` `model_root`와 `model_version`을 바꾼다. 모델은 장소와 상관없이 하나다.
기기가 둘이면 자동 영점을 찾지 못한다. 그래서 근접 지정(`nearest:`)은 보수적 사후확률로 판정해서, 기기 바로 앞의 강한 신호가 아니면 `ASK`가 난다.

## USB 설정 주입

모든 기기에 같은 펌웨어를 쓴다. 기기 ID, MQTT 계정, Wi-Fi, 서버 주소, 기능과 점유 방식(키 9개)은 USB로 한 번 넣는다. 시각 값은 넣지 않는다 (#236).
기기는 이 값을 명령 기록과 다른 NVS 영역에 보존한다. 재부팅과 펌웨어 재업로드 뒤에도 남는다.
`erase_flash`는 명령 기록도 지우므로 쓰지 않는다.

1. 시험 폴더에 `.env.wifi.json`을 만든다. 내용은 `{"ssid": "...", "password": "..."}`다. Git에서 제외된다.
2. `server/`에서 `nearpilot provision`으로 실행한다. pyserial이 필요하다(`pip install pyserial`). 시리얼 모니터와 함께 포트를 열지 않는다.

```powershell
nearpilot --dir var/node-lab-hardware provision --node light-1 --port COM6
```

   기존 `server/tools/node_lab/provision.py`도 같은 인자로 쓸 수 있다.

| 값 | 읽는 곳 |
|---|---|
| 기기 ID, MQTT 사용자명 | `--node`. 시험 폴더 `lab.json`의 `nodes`에 있어야 한다 |
| MQTT 비밀번호 | 시험 폴더의 `.env.<기기 ID>` |
| Wi-Fi | `.env.wifi.json` 또는 `--wifi-file` |
| MQTT 주소·포트 | `server.toml`의 `[mqtt]` |

- 제품 펌웨어(`firmware/`)만 지원한다. node_lab 펌웨어는 옛 시각 키를 요구하므로 이 도구로 주입하지 않는다.
- 기기가 모르는 키를 `unknown_key`로 거부하면 저장하지 않고 실패한다.
- 기기의 종류와 점유 방식은 주입하지 않는다. 펌웨어가 내장 선언으로 발표한다 (#245). 기기는 시험 폴더 `fleet.json`에 있어야 한다.

- 비밀번호는 명령 인자로 받지 않는다. `provision.jsonl`과 화면에는 비밀값과 SSID의 바이트 수만 쓴다.
- 기기는 값을 검사한 뒤 한 번에 저장하고 다시 읽어 확인한다. 그다음 재시작한다. 도구는 `CONFIG?` 응답으로 저장 값을 대조한다.
- 명령 기록이 있는 기기의 ID를 바꾸면 기기가 거부한다(`node_id_change_with_records`). 기록을 지워서 우회하지 않는다.
- 서버 주소가 바뀌면 같은 명령으로 다시 주입한다. 재시작한 기기는 새 연결에서 시각을 다시 맞춘 뒤에만 시각을 신뢰한다.
- 이 인증은 기기별 MQTT 계정이다. 앱·호스트 토큰과 다르다. 새 토큰을 만들지 않는다.

## 부팅과 준비 확인

```powershell
.\firmware\node_lab\.venv\Scripts\python.exe server/tools/node_lab/boot_check.py --port COM6 --output server/var/node-lab-hardware/boot-1.jsonl --duration 90 --reset --stop-when-ready
```

`--reset`은 USB 신호로 EN 재시작을 대신한다. 전원 재투입과 EN 버튼은 `--reset` 없이 실행한 뒤 직접 누른다.
도구는 마지막 부팅의 단계 로그를 읽고 아래 순서를 검사한다. 순서가 다르거나 준비에 도달하지 못하면 종료 코드 1이다.

1. `NODE_START` (설정 상태, 재시작 원인)
2. `WIFI_CONNECTED`
3. `MQTT_CONNECTED` (새 연결 ID와 구독)
4. `CLOCK_SYNCED` → `CLOCK_TRUSTED` (MQTT 시각 응답. 제품 펌웨어는 MQTT 연결 뒤에 맞춘다)
5. `ANNOUNCED` (그 연결에서 시각 신뢰 뒤)
6. `NODE_READY`

`--duration`을 길게 주면 이후의 끊김과 회복 시간도 `recoveries`에 남긴다.
기기의 `NODE_READY`는 연결 조건이다. 서버의 실행 가능 여부는 콘솔의 `node_readiness`로 확인한다.
`FAULT`인 기기는 연결이 준비돼도 `ready=false`, `fault=true`다. 재부팅과 재주입으로 풀리지 않는다.

## USB 시각 실측

`clock_probe.py`는 pyserial이 설치된 Python으로 실행한다. PlatformIO 가상환경에 pyserial이 있다.
Arduino 시리얼 모니터와 함께 포트를 열지 않는다. 저장소 루트에서 실행한다.

```powershell
.\firmware\node_lab\.venv\Scripts\python.exe server/tools/node_lab/clock_probe.py --port COM6 --output server/var/node-lab-hardware/clock-measurement.jsonl --duration 150 --interval 3 --max-rtt 0.8 --jump-tolerance 0.01 --max-sync-age 120 --margin 0.25 --reset
```

기기는 `CLOCK? <32자리 식별자>`에 현재 시각, 동기화 여부와 경과 시간을 응답한다.
도구는 요청 직전과 응답 직후의 서버 시각으로 기기 오차의 구간을 계산한다.
왕복 지연 초과, 서버 시계 점프와 미동기화 표본은 거부한다.
`measurement_summary`는 관측 구간의 최대 절댓값과 명시한 여유의 합을 제안한다.
도구는 서버나 펌웨어 설정을 자동으로 바꾸지 않는다.
예시 간격과 여유는 진단용 입력이다. 운영용 검증 값이 아니다.
같은 조건의 짧은 연결 시험과 운영용 장시간 검증을 구분한다.
실측한 파일은 시험 폴더에 보존한다.
제품 펌웨어는 MQTT 시각 응답의 왕복 시간으로 오차 상한을 정한다 (#236). 이 실측은 그 상한이 실제 오차를 덮는지 확인하는 진단이다.

## 펌웨어의 실행 결과

최소 펌웨어는 시리얼 기록과 선택 GPIO 출력을 마치고 결과를 저장하면 `succeeded`를 보고한다.
성공은 제어 신호 출력 완료다. LED의 실제 켜짐은 확인하지 않는다.
출력 직전 재검사에서 걸리면 `failed`와 거부 코드, 재시작으로 결과가 없으면 `unknown`이다.
`unknown` 뒤 서버가 기기를 `FAULT`로 격리하는 결과는 예상 동작이다. 운영 DB의 FAULT를 수동 SQL로 풀지 않는다.
v1 시험에서 `unknown`으로 격리된 시험 DB는 새 `--dir`로 별도 시험 DB를 만들어 다시 시작한다.
반납 검사와 실제 센서는 이 최소 펌웨어의 범위 밖이다.

## 시험 범위

- 등록한 `light-*` 기기만 시험 장소에 배치한다. 다른 기기는 자동 신뢰하지 않는다.
- 시험 계정·비콘은 내부 데이터다. 앱 인증과 외부 토큰 전달을 생략한다.
- MQTT 접속 인증은 유지한다. 비밀번호 원문은 `.env.*`에만 쓴다.
- 시뮬레이터는 서버와 같은 PC의 시계를 쓴다. 시각 요청을 보내지 않는다.
- `demo`는 설정의 브로커가 이미 사용 중이면 실패한다. 기존 브로커를 종료하지 않는다.
- 시험 도구는 프로덕션 서버와 같은 DB로 실행하지 않는다.
