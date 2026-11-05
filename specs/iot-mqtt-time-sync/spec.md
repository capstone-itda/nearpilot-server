# MQTT 시각 동기화

- 담당: 작업 지시자 (상태 관리·펌웨어)
- 요구사항: FR-07, NFR-12
- 따르는 결정: #236 (A안. MQTT 요청·응답으로 시각을 맞춘다)

## 1. 왜

- 실제 기기는 서버 PC의 NTP 서버에 시각을 맞춘다. 서버 PC마다 Windows 시간 서비스를 NTP 서버로 켜고 재동기화해야 하며, 방화벽에 UDP 123을 열어야 한다.
- 서버는 기기별로 실측한 최대 시각 오차(`[delivery.clock_skew_sec]`)가 없으면 그 기기의 관측을 쓰지 않는다. 새 기기마다 오차를 재고 설정에 넣어야 한다.
- 기기 주입 값에 시각원 주소, 오차, 신뢰 기간, 재동기화 간격이 들어 있어 서버 PC나 설정이 바뀌면 다시 주입해야 한다.

## 2. 무엇

- 입력: 기기의 `time_request`(`request_id`)
- 출력: 서버의 `time`(`request_id`, `server_time`, `max_error_sec`, `trust_sec`, `resync_sec`)
- 바뀌는 상태:
  - 펌웨어의 시각과 시각 신뢰. SNTP를 쓰지 않는다.
  - 서버 설정: `[delivery.clock_skew_sec]` 표 대신 `[delivery]`의 `clock_max_error_sec`, `clock_trust_sec`, `clock_resync_sec`
  - 펌웨어 설정 키: 13개에서 시각 키 4개(`time_server`, `clock_max_error_sec`, `time_trust_ms`, `time_resync_ms`)를 뺀 9개
- 약속:
  - 기기는 T + r/2로 시각을 맞춘다. r/2와 신뢰 기간의 표류 상한의 합이 `max_error_sec`를 넘는 응답은 버린다.
  - 서버는 모든 기기의 관측 나이·미래 관측 검사에 `clock_max_error_sec` 하나를 쓴다.
  - 시각을 신뢰하기 전에는 기기가 발표, 상태, RSSI를 보내지 않고 새 명령을 `CLOCK_UNVERIFIED`로 거부한다. 기존 규칙과 같다.

## 3. 제약

- 서버는 시각 요청에 CORE를 거치지 않고 MQTT 수신 스레드에서 바로 답한다. 응답이 늦으면 왕복 시간이 늘어 오차 상한이 커진다 (architecture.md §5 MQTT 6절).
- `server_time`은 발행 직전의 서버 UTC 시각이다 (#236).
- 세 설정은 코드 기본값 없이 서버 설정에서 받는다 (AGENTS.md 코딩 규칙).
- 시각 요청과 응답에 비밀번호, 사용자 정보를 넣지 않는다 (NFR-08).
- 기기는 자기 `time_request`만 발행하고 자기 `time`만 구독한다. 서버만 `time`을 발행한다 (architecture.md §5 MQTT 1절).
- 기존 NVS 설정(13개 키)은 그대로 읽는다. 시각 키는 무시한다. 재주입을 요구하지 않는다.

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/` (`messages/`, `mqtt/connection.py`, `mqtt/delivery.py`, `mqtt/clock.py` 추가)
- `server/src/nearpilot/runtime/` (`config.py`, `assembly.py`)
- `server/src/nearpilot/cli/provision.py`
- `server/tools/final_demo.py`, `server/tools/node_lab/`, `server/tools/evaluation/harness.py`, `server/tools/node_sim/config.py` (브로커 ACL만)
- `server/config/` (`server.example.toml`, `README.md`, `final-demo/`)
- `firmware/` (`lib/np_config/`, `lib/np_clock/`, `lib/np_ready/`, `src/main.cpp`, `include/config.example.h`, `test/test_common/`, `README.md`)
- `specs/core-ops-setup/spec.md` (NTP 규칙과 시각 인자를 지운 현재 동작으로 맞춘다)
- `server/tests/`

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `firmware/node_lab/` (연결 시험용 별도 펌웨어. SNTP를 그대로 쓴다), `app/`, `dashboard/`, 시뮬레이터 동작 (서버와 같은 PC 시계를 쓴다)

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| MQTT `time_request`·`time` (architecture.md §5 MQTT 3절·6절) | 제공 (서버), 사용 (펌웨어) | 펌웨어 |
| `NodeEventSink` 인계 전 관측 시각 검사 | 변경 없음 (오차 값의 출처만 바뀐다) | CORE |
| 펌웨어 USB 설정 명령 | 사용 (키 9개) | 펌웨어 |

### 만들지 않는 것

- 시뮬레이터의 시각 요청 (같은 PC 시계를 쓴다)
- `firmware/node_lab`의 시각 동기화 변경
- 원격 재시작 (#235), BLE 기기 설정과 mDNS (#234)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 요청 본문이 틀렸다 (버전, `node_id` 불일치, `request_id` 없음·64자 초과) | 서버가 답하지 않고 경고만 남긴다 | `test_FR07_time_request_rejects_invalid` |
| 구독 준비 전 요청 | 발행하지 않는다. 기기가 다시 요청한다 | `test_FR07_time_responder_skips_when_not_ready` |
| 응답 발행 실패 | 예외를 밖으로 내지 않고 경고만 남긴다 | `test_FR07_time_responder_survives_publish_failure` |
| 기기가 받은 응답의 `request_id`가 마지막 요청과 다르다 | 시각을 바꾸지 않는다 | 펌웨어 `test_FR07_reply_wrong_request` |
| 왕복 시간이 길어 오차 상한을 넘는다 | 응답을 버리고 다시 요청한다 | 펌웨어 `test_FR07_offer_rejects_slow_round_trip` |
| 신뢰 기간이 지났다 | `clock_trust_expired`, 새 명령 `CLOCK_UNVERIFIED`, 보고 중단 | 펌웨어 `test_FR07_clock_trust_expires` |
| 옛 설정 파일에 `[delivery.clock_skew_sec]`가 있다 | 시작하지 않고 바뀐 설정 이름을 알린다 | `test_FR07_config_rejects_old_clock_skew_table` |
| 세 설정이 없거나 범위 밖이다 | 시작하지 않는다 | `test_FR07_delivery_clock_settings_validated` |

## 6. 결정

- 시각 응답은 새 모듈 `iot/mqtt/clock.py`의 `TimeResponder`가 Paho 수신 콜백에서 바로 발행한다 — 처리 큐를 거치면 왕복 시간이 늘어난다.
- `time_request`·`time`은 QoS 0이다. 잃으면 기기가 다시 요청한다 — 재전송된 옛 응답은 요청 ID가 달라 버려진다.
- 설정 범위: `clock_max_error_sec`는 0 이상 60 이하의 수(0은 같은 PC의 시뮬레이터만), `clock_trust_sec`는 15~86400의 정수, `clock_resync_sec`는 5 이상 `clock_trust_sec` 이하의 정수 — 펌웨어의 신뢰 기간 범위(15초~1일)를 따른다.
- 최종 시연 구성의 값: 오차 0.5초, 신뢰 120초, 재동기화 60초 — 이전 주입 값(신뢰 120초, 재동기화 60초)을 잇고, 0.5초는 Wi-Fi·BLE 공존에서도 왕복 1초 안의 응답을 받는다.
- 펌웨어의 표류 상한은 빌드 값 `NP_CLOCK_DRIFT_PPM`(100ppm), 미동기 재요청 간격은 `NP_CLOCK_RETRY_MS`(2초)다 — ESP32 수정 발진기의 허용 오차보다 넉넉하다.
- 펌웨어는 응답을 받은 시각 t1을 MQTT 수신 콜백에서 `micros()`로 잡는다 — loop의 다른 처리 시간이 왕복 시간에 들어가지 않는다.
- 준비 사유 순서는 설정, 저장소, Wi-Fi, MQTT, 시각, 발표다 — 시각은 MQTT 연결 뒤에만 맞출 수 있다. `clock_not_configured` 사유는 없어진다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-07 | `tests/iot/test_iot_time_sync.py` | 요청 해석, 응답 형식, 발행 시각, 잘못된 요청 무시, 준비 전·발행 실패 |
| FR-07 | `tests/iot/` 기존 관측 나이 테스트 | 기기별 표 대신 `clock_max_error_sec` 하나로 같은 검사를 한다 |
| FR-07 | `tests/integration/test_runtime_config.py` | 세 설정의 검증, 옛 표 거부 |
| FR-07 | `tests/integration/test_server_real_broker.py` | 실제 Mosquitto에서 시각 요청에 응답한다 |
| NFR-12 | `tests/tools/test_final_demo_config.py` | NTP 방화벽 규칙과 기기별 오차 표가 없다, ACL에 시각 토픽이 있다 |
| NFR-12 | `tests/tools/test_node_lab_provision.py`, `tests/cli/test_cli_provision.py` | 주입 값이 키 9개다 |
| FR-07 | `firmware/test/test_common/` (`test_clock.cpp`, `test_config.cpp`, `test_ready.cpp`) | 응답 해석, 오차 상한, 신뢰 만료, 옛 설정 읽기, 준비 사유 순서 |
