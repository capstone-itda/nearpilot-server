# 최종 시연 서버

기존 NearPilot 서버에 앱 REST, AI 호스트 MCP와 대시보드를 함께 연결한다.
`node-lab`의 브로커 실행과 USB 설정 주입을 재사용한다. 콘솔 시험 서버는 실행하지 않는다.

| 기기 | 기능 | 점유 방식 |
|---|---|---|
| `locker-1`, `locker-2`, `locker-3` | 사물함 | 대여형 |
| `light-1`, `light-2`, `light-3` | 조명 | 공용형 |
| `speaker-1` | 스피커 | 공용형 |

장소 ID는 `final-demo`다. 기본 접근 정책은 모두 공개·개방이다.
기기마다 별도 MQTT 계정과 자기 토픽만 쓰는 ACL을 둔다.
새 폴더를 만드는 명령은 새 비밀번호를 발급한다. 기존 시연 폴더를 재생성하지 않는다.
기본 서버 주소는 `10.106.106.199`, HTTP 포트는 `8000`, MQTT 포트는 `18883`다.
기존 기기의 포트 `18883`을 유지한다. 기존 시험 DB와 원본 인증정보 파일은 보존한다.
현재 시연 폴더는 기기별 마지막 주입 기록의 인증정보를 가져와 통합했다. USB 재주입은 하지 않았다.

## 1. 구성 만들기

Python 3.12 이상, 서버 의존성, Mosquitto와 PowerShell 7이 필요하다.
대시보드는 Node.js와 `dashboard/`의 npm 의존성이 필요하다.
저장소의 `server/`에서 실행한다.

```powershell
python -m tools.final_demo
```

`server/var/final-demo/`에 아래 파일을 만든다. 기존 폴더는 덮어쓰지 않는다.

| 파일 | 용도 |
|---|---|
| `server.toml` | 앱, MCP, 대시보드와 MQTT를 연결하는 기존 서버 설정 |
| `seed.toml` | 소유자, 방문자, 비콘, 장소, 이름, 신뢰와 접근 정책 |
| `fleet.json`, `lab.json` | USB 주입에 쓰는 기기 정보. 별도 sim 구성에서는 가상 기기도 실행한다 |
| `broker.conf`, `broker.acl` | 인증을 요구하는 MQTT 브로커와 기기별 권한 |
| `.env.*` | 새 MQTT 비밀번호와 암호화한 브로커 비밀번호 파일 |
| `models/final-demo/synthetic-1.json` | 기기 7대의 합성 초기 모델 |
| `run.ps1` | 기존 브로커, 서버, 시드, 가상 기기와 대시보드를 실행하는 스크립트 |

다른 주소나 포트를 쓰려면 새 구성 폴더를 만든다.

```powershell
python -m tools.final_demo --dir var/final-demo-2 --host 192.168.0.10 --mqtt-port 18883 --http-port 8000
```

생성한 `run.ps1`에는 이 checkout의 서버 경로가 들어 있다. 저장소를 옮기면 새 폴더에 구성을 다시 만든다.

## 2. ESP32 연결 준비

1. 서버 PC와 기기를 같은 독립 AP에 연결한다.
2. `server.toml`의 `[delivery.clock_skew_sec]`에 기기별 실측 최대 시각 오차를 초로 넣는다.
3. 시연 폴더에 `.env.wifi.json`을 만든다. 실제 `ssid`와 `password` 문자열을 넣는다.
4. 각 기기에 제품 펌웨어를 올린다. ID와 접속 정보는 다음 단계에서 주입한다.
5. 새 기기만 기존 주입 도구로 설정한다. 기존 인증정보를 통합한 기기는 USB 재주입 없이 연결한다.

펌웨어 환경은 `firmware/platformio.ini`를 따른다.
사물함은 `esp32dev-locker-1`, 조명은 `esp32dev-light-1`, 스피커는 `esp32dev-speaker-1`이다.
같은 종류의 기기는 같은 펌웨어를 쓰고 주입하는 `--node`를 바꾼다. 플래시를 지우지 않는다.

아래 주입 예시는 저장소 루트에서 실행한다. COM 포트와 시각 신뢰 기간은 검증한 값으로 바꾼다.

```powershell
.\firmware\node_lab\.venv\Scripts\python.exe server/tools/node_lab/provision.py --dir server/var/final-demo --node locker-1 --port COM6 --time-server 10.106.106.199 --time-trust-sec 120 --time-resync-sec 60
```

시각 오차는 다른 기기의 값을 빌려 쓰지 않는다.
미설정 기기는 미검증 값 `-1`을 주입받고 시각을 신뢰하지 않는다.
시각 실측은 `server/tools/node_lab/README.md`의 USB 시각 실측 절차를 따른다.
PC의 NTP 응답과 ESP32의 `CLOCK_SYNCED`를 확인한다.
AP에서 서버 PC의 TCP `8000`, `18883`에 접근할 수 있어야 한다.
다른 PC에서 대시보드를 열면 TCP `5173`도 사용한다.

## 3. 최초 등록과 실행

아래 명령은 모두 저장소의 `server/`에서 실행한다. 각 실행 명령은 별도 터미널을 쓴다.
`run.ps1`은 어느 작업 폴더에서 호출해도 checkout의 `server/`로 이동한다.
다른 Python을 쓰려면 `-Python <python.exe 경로>`를 붙인다.

첫 터미널에서 브로커를 켠다.

```powershell
.\var\final-demo\run.ps1 broker
```

두 번째 터미널에서 최초 기기 발표를 받는 수집 전용 서버를 켠다.
이 단계는 앱 REST와 대시보드를 제공하고 MCP 조작은 받지 않는다.

```powershell
.\var\final-demo\run.ps1 collect
```

기기 7대가 발표한 뒤 세 번째 터미널에서 초기 구성을 반영한다.
시드는 DB에 없는 기기를 알려 준다. 누락한 기기가 발표하면 같은 명령을 다시 실행한다.

```powershell
.\var\final-demo\run.ps1 seed
```

최초 실행이 보여 주는 `demo-owner`와 `demo-visitor` 토큰을 보관한다.
소유자 토큰은 앱의 소유자 로그인과 대시보드에 쓴다. 방문자 토큰은 앱과 AI 호스트에 쓴다.
같은 시드를 다시 실행해도 토큰은 다시 발급하지 않는다.
출력 전체를 공유 로그나 저장소에 넣지 않는다. 토큰 파일은 `.env.`로 시작하는 이름을 쓴다.

시드의 기기 항목이 모두 반영되면 두 번째 터미널의 수집 전용 서버를 Ctrl+C로 종료한다.
같은 터미널에서 전체 서버를 켠다. 재시작은 새 기기를 기존 복원 목록으로 읽는 절차다.

```powershell
.\var\final-demo\run.ps1 server
```

다음 시연부터는 브로커와 전체 서버를 바로 켠다. DB와 기기 명령 기록을 보존한다.
`FAULT` 복구는 기존 `nearpilot recover`를 쓴다. SQL로 상태를 바꾸지 않는다.

## 4. 앱, AI 호스트와 대시보드

| 연결 | 기본 주소와 인증 |
|---|---|
| Android 앱 | `http://10.106.106.199:8000`. 기존 토큰을 넣거나 앱에서 새 방문자를 만든다 |
| AI 호스트 MCP | `http://10.106.106.199:8000/mcp`. 앱과 같은 방문자 Bearer 토큰을 쓴다 |
| 대시보드 | 아래 명령 뒤 `http://localhost:5173/?source=server`. 소유자 토큰을 넣는다 |

```powershell
.\var\final-demo\run.ps1 dashboard
```

다른 PC에서 대시보드를 열면 `http://10.106.106.199:5173/?source=server`를 쓴다.
개발 서버는 조회 REST와 WebSocket을 시연 서버로 전달한다. 기본 화면의 목 데이터를 쓰지 않는다.
앱에서는 비콘 송출을 켠다. 시드의 UUID와 minor는 초기값이다.
앱이 비콘을 바꾼 계정은 예전 비콘을 다시 덮어쓰지 않도록 시드의 비콘 항목을 맞춘다.
장소·정책만 재반영할 때는 시드의 `[[accounts]]`, `[[beacons]]`를 빼고 실행할 수 있다.

대시보드에서 전체 7대의 등록을 확인한다. MCP의 주변 목록은 최근 비콘 수신으로 확인한 같은 장소의 사용 가능한 공개 기기를 보여 준다.
사물함·조명·스피커 모두 시연 근접 임계값 `0.8`을 쓴다. `get_device_state`로 ID를 지정해 조회할 수 있다.
AI 호스트에서 다음 순서로 조작한다.

1. `locker-1`을 열고 같은 대여에서 닫는다. `release_use`로 대여를 반납한다.
2. `light-1`을 켠다. 같은 조명에 밝기와 색을 지정해 다시 켠 뒤 끈다.
3. `speaker-1`을 명시해 켜고 끈다.
4. 나머지 사물함과 조명도 ID를 바꿔 확인한다.

`nearest:light`, `nearest:speaker`도 근접 추정과 임계값 `0.8` 검사를 한다.
`light-1`, `speaker-1`처럼 명시 지정하면 최초 판정과 실행 직전 재검사에서 확률 계산을 생략한다.
이 생략은 기기 기능이나 공용형 속성이 아니라 요청의 명시 지정 여부로 자동 적용한다.
명시 지정도 장소에서 등록 비콘을 받아야 하고 기존 정책·점유·실행 검사를 거친다.
제한 기기 시연은 생성한 `seed.toml`에서 정책을 바꿔 다시 반영한다.
예를 들어 `locker-3`의 `mode`를 `restricted`로 바꾸고 `approver = "demo-owner"`를 넣는다.
앱으로 승인한 뒤 새 사용 요청을 보낸다. 승인은 실행 허가와 별개다.

## 5. 근접 모델

기본 모델은 `node-lab`과 같은 좌표 기반 합성 초기값이다.
`validation.source = SYNTHETIC_NOT_MEASURED`이며 실제 정확도를 검증하지 않았다.
좌표, 기기 간격과 시뮬레이터 RSSI는 배치 예시다. 실제 시연 배치로 표시하지 않는다.
실측 데이터를 수집·적합·평가한 뒤 모델을 시연 폴더에 넣는다.
`server.toml`의 `[proximity.models]`에서 `final-demo`의 버전을 바꾸고 서버를 재시작한다.
담당자의 2026-10-28 지시로 시연 임계값을 모든 기능에서 `0.8`로 설정했다.
설정 버전은 `final-demo-2`다. 이 수치는 실측 품질 검증 결과가 아니다.

## 6. 실기와 분리한 연결 확인

`server/`에서 별도 localhost 구성을 만든다. 실기용 시연 폴더를 수정하지 않는다.

```powershell
python -m tools.final_demo --mode sim --dir var/final-demo-sim --mqtt-port 18885 --http-port 8001
```

각 터미널에서 아래 순서로 실행한다.

```powershell
.\var\final-demo-sim\run.ps1 broker
.\var\final-demo-sim\run.ps1 collect
.\var\final-demo-sim\run.ps1 sim
.\var\final-demo-sim\run.ps1 seed
```

수집 전용 서버를 종료한 뒤 `run.ps1 server`로 바꾼다. MCP 주소의 포트는 `8001`이다.
`run.ps1 sim` 터미널은 계속 켜 둔다. `quit`를 입력하면 가상 기기를 종료한다.
실기용 구성에서 `run.ps1 sim`을 실행하면 스크립트가 거부한다.
가상 기기의 성공은 서버 연결 검증이다. 실제 LED, 서보와 스피커의 출력 검증을 대신하지 않는다.
기존 사물함 시뮬레이터는 `open`만 지원한다. 가상 사물함은 열기와 논리 반납으로 확인한다.
사물함의 `close`와 조명의 밝기·색 출력은 제품 펌웨어를 올린 실기로 확인한다.
