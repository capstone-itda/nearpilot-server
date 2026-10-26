# MQTT 실행 설정

2026-10-26에 센서 기반 물리 검증과 반납 검사를 범위에서 제외했다 (`docs/requirements.md` FR-07·FR-20·NFR-04).
서버의 반납·복구 검사 경로는 지웠다. 아래 표는 설정 예시의 정리 상태다.
새 계약의 전환 절차와 기존 데이터 이전은 `docs/architecture.md` §4의 미결을 따른다.

| 예시 | 정리 상태 |
|---|---|
| `seed/example.toml`의 `release_policy.check_criteria` | 지웠다. 서버는 검사 기준을 읽지 않는다. |
| `mosquitto.acl.example`의 검사 토픽 | 지웠다. 서버는 검사 요청을 보내지 않고 검사 결과를 받지 않는다. |
| `Settings`의 검사 시간 값 | 지웠다. `release_tick_sec`는 저장 결과가 불명인 반납 종료를 다시 확인하는 주기다. |
| `tools/node_sim/fleet.example.json`의 `light_observable` | 후속 시뮬레이터 작업에서 관측 부재로 `unknown`을 만드는 이전 시나리오를 제거한다. |

서버와 ESP32는 같은 독립 AP의 Wi-Fi를 사용하고 MQTT 3.1.1로 통신한다.
휴대폰의 BLE 비콘을 ESP32가 수신하고 RSSI를 MQTT로 보고한다. 외부 인터넷은 필요 없다.

`server/`에서 `mosquitto -c config/mosquitto.conf`로 시작한다.
예시는 localhost에만 바인딩한다. 실제 기기를 연결할 때 listener의 주소를 서버의 AP 주소로 바꾼다.

`mosquitto.acl.example`의 기기별 계정과 토픽 ID를 실제 기기 목록에 맞춘다.
기기마다 다른 계정을 만들고 서버 전용 계정을 따로 쓴다. 각 계정은 명시한 토픽에만 접근한다.
기기 계정에 명령 쓰기 권한이나 다른 기기의 읽기·쓰기 권한을 추가하지 않는다.

브로커의 암호화된 비밀번호 파일은 `mosquitto_passwd -c config/.env.mosquitto-passwords nearpilot-server`로 만들고,
기기 계정은 `-c` 없이 같은 파일에 추가한다. 비밀번호는 대화형 입력으로 받는다.
이 파일과 서버 접속용 원문 비밀번호 파일(`.env.mqtt-password` 등)은 기존 `.env.*` ignore 규칙을 따른다.
둘은 서로 다른 파일이다. 실제 비밀번호 파일은 커밋하지 않는다.

IOT `MqttConfig`에는 주소·포트·클라이언트 ID·서버 계정·원문 비밀번호 파일 경로와
keepalive, 접속·시작·종료 제한 시간, 재접속 최소·최대 대기 시간을 모두 전달한다.
시간 값은 초 단위이고 임의 기본값이 없다. 종료한 연결 객체는 새로 만든다.
`start()`가 성공해야 서버 조립이 복원과 요청 접수를 진행할 수 있다.
브로커 접속만으로 기기 온라인·현재 연결·실행 가능 여부를 확정하지 않는다.

`on_report`와 `on_connection_lost`는 후속 IOT 작업에 짧게 인계하는 콜백이다.
이 콜백에서 CORE를 직접 호출하거나 명령 결과를 기다리지 않는다.
보고의 검증·재인계와 명령의 결과 대기는 서버 조립(`nearpilot.runtime`)이 연결한다.

실제 브로커 테스트는 `MOSQUITTO_EXECUTABLE`과 `MOSQUITTO_PASSWD_EXECUTABLE`에 실행 파일 경로를 주거나
두 명령을 PATH에 둔 뒤 `pytest tests/iot/test_broker_acl.py`로 실행한다.
테스트는 localhost의 임시 포트·임시 자격정보만 사용한다.

# 서버 실행 설정

`server.example.toml`을 `server.toml`로 복사해서 배포 값을 채운다. 운영용 `server.toml`은 커밋하지 않는다.
`server/`에서 `python -m nearpilot --config config/server.toml`로 실행한다. 단일 프로세스·단일 worker다.

- 상대 경로는 설정 파일이 있는 디렉터리 기준이다. 알 수 없는 키나 빠진 필수 키는 시작을 막는다.
- `[settings]`는 운영 수치(`Settings`)다. `version`만 필수이고 빠진 키는 시연용 초기값을 쓴다.
  미설정 임계값(스피커, 명시 지정)은 쓰지 않는다. 비워 두면 그 요청은 실행을 허가하지 않는다.
- `[http]`의 `public_hosts`는 선택이다. 터널 같은 공개 주소로 `/mcp`를 열 때 그 Host를 쓴다 (예: `["mcp.example.com"]`).
  `/mcp`는 localhost와 이 Host로 온 요청만 받는다. 다른 Host는 토큰이 맞아도 `421`이다.
- `[runtime]`은 주기 작업 간격, SQLite 대기시간과 종료 대기 제한이다. 모두 선택이다.
- `[proximity.models]`에 장소별 모델 버전을 명시한다. 파일은 `<model_root>/<place_id>/<model_version>.json`이다.
- `[mqtt]`와 `[delivery]`는 모든 키가 필수다. `password_file`은 서버 계정의 원문 비밀번호 파일이다.
- `[delivery.clock_skew_sec]`에 없는 기기의 관측과 검사는 쓰지 않는다. 0은 같은 PC의 시뮬레이터에만 쓴다.
- `[dashboard]`는 선택이다. 있으면 서버가 소유자 토큰으로 대시보드의 모니터링 WebSocket(`/dashboard/stream`)과
  조회 REST(`/dashboard/...`)를 연다. 없으면 대시보드를 열지 않는다. 라우터, 조회 서비스, 따라 읽기를 만들지 않고 서버의 다른 동작은 같다.
  `serve --collect-only`에서도 있으면 연다. 이때 장소의 모델 버전은 `없음`이다.
  - 키 6개가 모두 필수이고 기본값이 없다. 빠지거나 값이 틀리거나 모르는 키가 있으면 시작을 막는다.
  - `queue_max_events`는 연결마다 쌓아 둘 이벤트 수다. 넘으면 그 연결을 닫고 대시보드가 다시 연결한다.
    `batch_max_events`는 메시지 하나에 담는 이벤트 수, `auth_timeout_sec`은 연결한 뒤 인증 메시지를 기다리는 시간이다.
    `audit_limit_max`는 감사 조회 한 번의 최대 행 수다.
  - `weak_rssi_dbm`은 장소 목록의 `weakRssiDbm`이다. `tail_interval_sec`은 따라 읽기 간격이고, 사건이 화면에 보이기까지 최대 이만큼 늦다.
  - 예시 값은 시연용 초기값이다. `-80`은 시뮬레이터 예시 기기(`tools/node_sim/fleet.example.json`)의 수신(-58~-71)보다 약한 값이고 `0.5`는 다른 주기 작업(0.25~1초)과 같은 규모다. 시연 장소에서 보고 고친다.
  - 평가 리포트는 `var/reports/`(설정 파일 폴더의 부모)의 CSV 가운데 `generated_at`이 가장 늦은 하나를 읽는다. 없으면 신뢰도 조회에 `evaluation`이 없다.
- 설정 변경은 재시작으로 적용한다. 진행 중인 요청과 점유는 저장한 설정 스냅샷을 그대로 쓴다.

시작은 설정·모델 검증, DB 준비, 근접 관측 복원, MQTT 구독 확인, 재시작 복원, 보존기간 정리 순서다.
하나라도 실패하면 요청을 받지 않고 끝낸다. 종료할 때 제한 시간을 넘긴 작업은 오류로 알리고,
미완료 기록은 다음 시작의 복원에서 다시 확인한다.

# 운영 명령

`pip install -e .` 뒤 `server/`에서 `nearpilot <명령>`으로 쓴다. `nearpilot serve`는 위의 `python -m nearpilot`과 같은 서버를 띄운다. 다만 `--config`의 기본값이 있고, DB 폴더(`var/`)를 만들고, 시작에 실패하면 종료 코드 1이다.
설정 파일은 `--config`로 고르고 기본은 `config/server.toml`이다 (실행 폴더 기준).
종료 코드는 0 성공, 1 실행 중 거부·실패(서버 시작 실패 포함), 2 입력 오류, 130 Ctrl+C다. `serve`는 Ctrl+C로 멈춰도 정상 종료라서 0이다.

파일 인자에 이름(`/`나 `\`가 없는 값)을 주면 아래 기본 폴더에서 찾고, 경로를 주면 실행 폴더 기준으로 읽는다.

| 폴더 | 파일 | 만드는 쪽 | 읽는 명령 |
|---|---|---|---|
| `config/seed/` | 시드: 계정, 비콘, 장소, 기기 신뢰와 종료 정책, 접근 정책 | 사람 | `seed` |
| `config/plans/` | 수집 계획: 회차 순서와 길이, 수집하는 사람의 비콘 | 사람 | `collect run` |
| `config/layouts/` | 배치: 기기와 기기 앞 위치의 좌표, none 지점 | 사람 | `fit` |
| `config/fit/` | 적합 설정: 분할 비율, 후보 값, 통과 기준 | 사람 | `collect list --summary`, `split`, `fit`, `eval` |
| `var/records/` | 수집 회차 기록 | `collect run` | |
| `var/splits/` | 분할 결과 | `split` | `fit`, `eval` |
| `var/models/` | 모델 파일 (`[proximity].model_root`) | `fit` | `eval`, `serve` |
| `var/reports/` | 평가 보고서 (CSV, Markdown) | `eval` | |

`config/`의 `example.*`는 예시다. 좌표, 회차 길이, 통과 기준을 실제 환경에 맞춰 바꾼다.
`var/`는 설정 파일 폴더의 부모(`server/var/`)이고 명령이 만든다.

모델을 처음 만들 때의 흐름이다.

```
nearpilot serve --collect-only
nearpilot seed example.toml
nearpilot collect run example.json
nearpilot collect list --summary --place place-a --fit example.toml
nearpilot split place-a --fit example.toml
nearpilot fit example.json --split place-a-seed42.json --version v1 --fit example.toml
nearpilot eval place-a/v1 --split place-a-seed42.json --fit example.toml
nearpilot serve
```

- `serve --collect-only`는 근접 모델 없이 시작해서 RSSI와 기기 보고를 저장하고 MCP를 열지 않는다. 수집하는 동안 켜 둔다.
- 기기는 MQTT로 발표해야 DB에 생긴다. 발표 전에 `seed`를 실행하면 없는 기기에 관한 항목만 거부되고(종료 코드 1)
  나머지는 반영된다. 기기가 발표한 뒤 같은 명령을 다시 실행한다. 이미 있는 계정은 건너뛴다.
  계정 토큰은 등록할 때 한 번만 화면에 나온다. DB에는 해시만 저장한다.
- `collect run`은 회차마다 Enter로 시작하고 q로 끝낸다. 기록은 `var/records/<계획 파일 이름>-<UTC 날짜>.jsonl`에 덧붙인다.
  `collect list --summary --place`의 `분할 가능`이 라벨마다 `예`이면 `split`을 실행한다.
- `split`, `fit`, `eval`은 `--fit`을 생략하면 `<장소>.toml`을 읽는다. 예시 파일은 `example.toml`이라서 `--fit example.toml`을 준다.
  `split`과 `fit`은 이미 있는 결과 파일과 같은 모델 버전을 덮어쓰지 않는다.
- `eval`은 기준에 못 미쳐도 종료 코드 0이고, 보고서의 통과 칸에 쓴다. 전환 시간과 깜빡임은 `미측정`이다.
- `server.example.toml`의 `[proximity.models]` 값(`replace-with-model-version`)은 자리값이다. 첫 모델을 만들기 전에는 `serve --collect-only`만 쓴다.
- `fit` 출력의 `place-a = "v1"` 줄을 `config/server.toml`의 `[proximity.models]`에 쓴다.
  수집 전용 서버를 끈 뒤 `nearpilot serve`를 실행한다. 모델 표가 없으면 일반 `serve`는 시작하지 않는다 (종료 코드 2).

# FAULT 복구

기기가 `FAULT`로 격리되면 그 기기가 있는 장소의 소유자가 `nearpilot recover`로 푼다. `nearpilot serve`가 떠 있어야 한다.
명령은 DB를 열지 않고 서버의 복구 REST 두 경로(`GET /admin/nodes/{node_id}/recovery`, `POST /admin/nodes/{node_id}/recovery/{recovery_id}/approve`)만 부른다.
준비 판단과 정산은 서버가 하고 명령은 응답을 옮겨 쓴다. 수집 전용 서버(`serve --collect-only`)도 두 경로를 연다.

소유자 토큰은 `--token-file`로 받는다. 명령줄 인자로 받지 않는다 (셸 기록에 남는다).
`nearpilot seed`가 소유자 계정을 등록할 때 화면에 한 번 나온 토큰을 파일에 저장한다.
파일에는 토큰 하나만 쓴다. 앞뒤 공백과 줄바꿈은 지우고, UTF-8 텍스트여야 한다.
파일 이름을 `.env.owner-token`처럼 `.env.`로 시작하게 지으면 기존 `.env.*` ignore 규칙을 따른다. 권한은 소유자만 읽고 쓰는 `600`을 권장한다.

`status`로 준비 상태를 본 뒤 `approve`로 승인한다.

```
nearpilot recover status locker-1 --token-file config/.env.owner-token
nearpilot recover approve locker-1 <복구 ID> --token-file config/.env.owner-token
```

- `status`는 상태를 바꾸지 않는다. `상태`, `복구 ID`(없으면 `없음`), `부족한 조건`(없으면 `없음`, 있으면 쉼표로 이은 사유 코드)을 줄마다 쓴다.
  상태가 `ready`이면 마지막 줄에 실행할 `approve` 명령을 쓴다. 복구 ID와 토큰 파일 경로가 채워져 있다.
- `approve`가 복구를 완료하면 `복구를 완료했다`와 완료 시각을 쓰고 기기는 `AVAILABLE`로 돌아온다.
  서버가 저장 결과를 확인하는 중이면 완료가 아니다. 표준 오류에 안내와 `reason_code`를 쓰고 종료 코드 1로 끝난다.
  잠시 뒤 같은 명령을 다시 실행한다. 같은 복구 건의 반복 승인은 기존 완료 결과를 돌려준다.
- 서버 주소는 `--server URL`(예: `http://192.168.0.10:8000`)로 준다. 주지 않으면 설정 파일의 `[http]`로 `http://{host}:{port}`를 만든다.
  `host`가 `0.0.0.0`이면 `127.0.0.1`로 부른다. 설정 파일은 `--config`(기본 `config/server.toml`)로 고른다.
  서버와 다른 컴퓨터에서 쓰면 `--server`를 준다. `status`가 쓴 다음 명령에는 `--server`와 `--config`가 없으니 `status`에 준 값을 `approve`에도 준다.
- 요청 하나를 10초까지 기다린다.

종료 코드는 아래와 같다. 사유는 표준 오류에 한 줄로 쓴다.

| 종료 코드 | 뜻 |
|---|---|
| 0 | `status`는 조회에 성공했다 (`not_ready`여도 0이다). `approve`는 복구를 완료했다. |
| 1 | 서버가 거부했거나 오류를 돌려줬다. `approve`가 아직 완료가 아니다. 서버에 연결하지 못했다. |
| 2 | 입력 오류다. 요청을 보내지 않는다. 토큰 파일이 없거나 비었거나 토큰 하나가 아니다. 설정 파일이나 `--server`가 잘못됐다. 인자가 빠졌다. |

서버가 거부하면 표준 오류에 상태 코드와 사유 코드를 쓴다.

| 사유 | 뜻 |
|---|---|
| `INVALID_TOKEN` (401) | 토큰이 없거나 틀리다. 토큰 파일을 확인한다. |
| `NOT_OWNER` (403) | 토큰의 계정이 그 기기 장소의 소유자가 아니다. |
| `RECOVERY_MISMATCH`, `NOT_READY`, `STATE_CONFLICT` (409) | `status`를 다시 실행해 복구 ID와 상태를 확인한다. |
| `STORE_NOT_APPLIED` (503) | 저장이 반영되지 않았다. 변경 없이 같은 명령을 다시 실행할 수 있다. |
| `INTERNAL_ERROR` (500) | 서버 내부 오류다. 서버 로그를 본다. |

상태가 `ready`가 아니면 `approve`를 하지 않고 `부족한 조건`을 확인한다. 조건이 풀리면 `status`를 다시 실행한다.

- `NO_RECOVERY_REQUIRED`: 복구할 건이 없다.
- `COMMAND_STATE_UNCONFIRMED`: 기기가 현재 연결에서 명령 처리 대기 보고를 하지 않았다. 격리 직후에는 이 조건이고, 기기가 대기 보고를 하면 `ready`가 된다.
  기기가 서버에 연결돼 있는지 확인하고 기다린다.
- `OFFLINE`: 기기가 서버에 연결돼 있지 않다. 기기의 전원, Wi-Fi, MQTT 연결을 확인한다.
- 그 밖의 사유 코드는 서버가 정한 값 그대로다.
