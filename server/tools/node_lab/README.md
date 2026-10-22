# ESP32 관측과 명령 연결 시험

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
실제 HMM으로 확률을 계산하고 가까운 기기에 `on`, `off`를 요청한다.
CORE가 허가한 명령만 기존 MQTT 경로로 보낸다.

## 화면과 로그 읽기

같은 결과를 화면과 `var/node-lab/evidence.jsonl`에 쓴다.

| event | 확인한 내용 |
|---|---|
| `node_configured` | 등록한 시험 기기의 발표를 받아 장소·신뢰·정책을 연결했다 |
| `inventory_reloaded` | 새 기기를 기존 복원 경로로 읽도록 시험 Runtime을 재시작했다 |
| `node_observation` | RSSI, 관측·수신 시각, 물리 상태, 실행 가능 여부와 차단 사유다 |
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
`frame_ts`와 `received_at`은 RSSI 시각이고 `status_observed_at`과 `status_received_at`은 물리 상태 보고의 시각이다.
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
quit
```

`on`과 `off`는 `nearest:light`를 요청한다. 비콘이 없거나 확률이 낮으면 실행하지 않는다.
`on light-1`처럼 명시 지정도 입력할 수 있다.
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

3. 실제 비콘의 UUID·major·minor를 `lab.json`에 넣는다. 처음 생성할 때 `--uuid`, `--major`, `--minor`로 지정해도 된다.
4. 폐쇄망 시각원과 기기별 최대 시각 오차를 실측한다. `server.toml`의 `[delivery.clock_skew_sec]`에 기기별 초 값을 넣는다.
5. 펌웨어의 Wi-Fi·MQTT·기기 ID·시각원·검증한 시각 오차를 맞춘다. MQTT 비밀번호는 시험 폴더의 `.env.light-1`처럼 기기별 파일을 쓴다.
6. 첫 터미널에서 `broker --dir var/node-lab-hardware`, 두 번째에서 `run --dir var/node-lab-hardware`를 실행한다.
7. iBeacon을 송출하고 ESP32 시리얼의 `BLE_BEACON`과 서버의 `node_observation`을 비교한다.
   송출 앱이 없으면 보유 ESP32 한 대를 `firmware/node_lab/`의 선택 `ibeacon` 환경으로 쓴다.
8. 위치를 바꾸면서 `proximity`의 확률 변화를 본다. 기기 간 선택에는 앵커 2대 이상을 쓴다.
9. 콘솔에서 `on`을 입력한다. `decision=OK`, ESP32의 `COMMAND_RECEIVED`·`COMMAND_HANDLED`, 서버의 `command_result`를 같은 명령 ID로 연결한다.

실제 기기의 시각 오차 설정은 기본으로 비어 있다. 미측정 값을 0으로 채우지 않는다.
시각이 검증되지 않으면 서버는 보고를 거부하고 펌웨어는 새 출력을 차단한다.
실제 기기가 없으므로 이 저장소는 신뢰할 시각 오차 값을 제공하지 않는다.
AP 안의 NTP 서버 운영과 Windows 방화벽의 MQTT 포트 허용은 환경에서 준비한다.

합성 모델은 `models/node-lab/synthetic-1.json`이다. 좌표와 파라미터는 초기 예시다.
실측 모델을 쓸 때는 `server.toml`의 모델 루트와 장소별 버전을 바꾼다.
장소 ID는 이 도구의 시험 장소인 `node-lab`과 일치해야 한다.

## 펌웨어의 unknown 결과

ESP32만으로는 LED의 실제 켜짐이나 구동부 완료를 증명할 수 없다.
최소 펌웨어는 시리얼 수신 확인과 선택 GPIO 출력 뒤 `unknown`을 보고한다.
서버가 기기를 `FAULT`로 격리하는 결과는 예상 동작이다. 로그 확인을 실패한 것으로 보지 않는다.
다음 명령을 위해 운영 DB의 FAULT를 수동 SQL로 풀지 않는다.
첫 연결 시험을 새로 시작하려면 새 `--dir`로 별도 시험 DB를 만든다.
실제 완료 성공을 검증하려면 완료 관측 수단과 복구 절차를 따로 준비한다.
반납 검사와 실제 센서는 이 최소 펌웨어의 범위 밖이다.

## 시험 범위

- 등록한 `light-*` 기기만 시험 장소에 배치한다. 다른 기기는 자동 신뢰하지 않는다.
- 시험 계정·비콘은 내부 데이터다. 앱 인증과 외부 토큰 전달을 생략한다.
- MQTT 접속 인증은 유지한다. 비밀번호 원문은 `.env.*`에만 쓴다.
- 같은 PC 시뮬레이터에만 검증된 시각 오차 0을 지정한다.
- `demo`는 설정의 브로커가 이미 사용 중이면 실패한다. 기존 브로커를 종료하지 않는다.
- 시험 도구는 프로덕션 서버와 같은 DB로 실행하지 않는다.
