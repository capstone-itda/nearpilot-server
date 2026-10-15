# MQTT 게이트웨이 연결과 브로커 접근 제어

- 담당: 이 작업을 지시한 담당자 (상태 관리의 IOT 구현 범위)
- 구현 브랜치: `feat/iot-mqtt-gateway`
- 요구사항: FR-06, FR-07, FR-13, NFR-08, NFR-11, NFR-12
- 따르는 결정: architecture.md에 반영한 #19, #38, #88, #140이다.
- 상태: 구현 전 초안이다. 전체 분할과 순서는 [plan.md](plan.md)에 있다.

## 1. 왜

CORE가 기기와 통신하려면 MQTT 연결과 구독이 먼저 준비되어야 한다.
기기별 접근 제어가 없으면 다른 기기가 보고를 위조하거나 명령을 읽을 수 있다.
기존 통합 spec은 연결·검증·실행·시뮬레이터를 함께 다뤄 한 feat PR의 경계가 너무 넓었다.

## 2. 무엇

- 입력: 브로커 접속 설정, 서버 자격정보, `NodeEventSink`와 운영 설정이다.
- 출력: 보고 구독과 명령 발행에 쓰는 연결, 구독 준비 확인과 종료 결과다.
- 바뀌는 상태: 브로커 연결·구독·새 발행 수용 여부다.
- 약속:
  - MQTT 연결·구독을 관리하고 다른 IOT 모듈을 연결한다.
  - 재접속할 때 다시 구독한다. 연결 성공만으로 구독 완료를 알리지 않는다.
  - 실제 비밀번호를 저장소와 로그에 넣지 않는다.
  - 복원과 요청 접수의 순서는 서버 조립과 확정한 계약을 따른다.

| 종류 | 방향 | 발행·구독 QoS | retain |
|---|---|---|---|
| `announce` | 기기 → 서버 | 1 | true |
| `rssi` | 기기 → 서버 | 0 | false |
| `status` | 기기 → 서버 | 1 | false |
| `result` | 기기 → 서버 | 1 | false |
| `safety_check_result` | 기기 → 서버 | 1 | false |
| `cmd` | 서버 → 기기 | 1 | false |
| `safety_check` | 서버 → 기기 | 1 | false |

## 3. 제약

- MQTT는 3.1.1이며 `clean_session=true`다. 재접속하면 다시 구독한다 (NFR-11, architecture.md §5 MQTT 1절).
- 연결은 `paho-mqtt` 2.x의 스레드 방식을 쓴다 (architecture.md §8·9).
- Mosquitto는 익명 접속을 거부한다. 기기는 자기 보고만 발행하고 자기 명령·검사 요청만 구독한다 (NFR-08).
- 서버만 `cmd`와 `safety_check`를 발행한다. CORE가 인계한 명령만 발행 경로에 연결한다 (FR-06).
- Mosquitto의 계정별 ACL로 인증 주체와 허용 토픽을 묶고, 토픽·본문의 기기 ID 대조는 보고 검증 모듈이 한다 (NFR-08, S2 A안).
- 접속 설정은 실행 설정에서 받는다. 실제 비밀번호 파일은 커밋하지 않는다 (NFR-08).
- 독립 AP 안에서 실행한다. 외부 인터넷을 연결 조건으로 두지 않는다 (NFR-12).
- 서버 조립은 `start()`가 필요한 다섯 구독의 SUBACK을 확인한 뒤 복원하고 신규 요청을 받는다 (architecture.md §2, S3 A안).
- 연결 복구만으로 기기 온라인·현재 연결·안전을 확정하지 않는다 (FR-13).
- 시작·종료·재접속의 제한 시간과 주기는 설정에서 받는다. 미결 수치를 상수로 넣지 않는다 (requirements.md §1.2).
- IOT는 `shared` 계약만으로 CORE와 연결한다. CORE·DB 구현을 불러오지 않는다 (AGENTS.md 의존 규칙 4).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/mqtt/`의 연결·조립·접속 설정 파일이다.
- `server/tests/iot/`의 연결·구독·접근 제어 테스트다.
- `server/config/`의 `mosquitto.conf`와 비밀값 없는 접근 제어 예시다. AGENTS.md가 IOT에 배정한 경로다.

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`의 Gotchas 밖, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 이 작업: `core/`, `db/`, `api/`, `firmware/`, 메시지 본문 검증과 시뮬레이터다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeEventSink` | 조립에서 주입받는다 | CORE |
| `NodeCommandGateway`, `SafetyCheckGateway` | 구현 모듈을 조립한다 | CORE |
| MQTT v1 | 연결·구독을 제공한다 | 펌웨어 |
| 구독 뒤 복원 | 연결한다. 접점은 S3에서 확정한다 | 서버 조립 |

### 만들지 않는 것

- 본문 변환은 `iot-messages`, 현재 연결과 재인계는 `iot-event-delivery`가 맡는다.
- 명령별 결과 대기와 검사 요청은 `iot-command-gateway`가 맡는다.
- 서버 실행 진입점과 CORE 서비스 조립은 #88의 별도 작업이다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 인증이나 구독이 실패한다 | 구독 준비 완료를 알리지 않는다 | `test_NFR08_failed_auth_or_subscribe_not_ready` |
| 브로커를 재접속한다 | 다시 구독한다. 현재 기기는 다시 확인한다 | `test_FR13_reconnect_resubscribes` |
| retained 발표가 온다 | 보고 경로에 전달한다. 온라인으로 바꾸지 않는다 | `test_FR13_retained_announce_not_online` |
| 익명 계정이 접속한다 | 브로커가 거부한다 | `test_NFR08_anonymous_connection_denied` |
| 다른 기기의 보고를 발행하거나 명령을 읽는다 | 브로커가 차단한다 | `test_NFR08_cross_node_access_denied` |
| 기기 계정이 명령을 발행한다 | 브로커가 차단한다 | `test_FR06_only_server_publishes_commands` |
| 발행 뒤 연결이 끊긴다 | 명령 대기 모듈에 알린다. 미전송으로 추정하지 않는다 | `test_FR07_disconnect_keeps_uncertainty` |
| 서버가 종료한다 | S3의 접점을 따른다. 미해결 명령을 성공으로 만들지 않는다 | `test_FR07_shutdown_does_not_claim_success` |

## 6. 결정

- S2 A안: 브로커가 계정별 토픽 접근을 강제하고 IOT가 토픽·본문 ID를 대조한다 — 담당자가 2026-10-16에 선택했다. 서버 계정과 기기 계정을 분리한 명시적 ACL 예시를 제공한다.
- S3 A안: IOT가 접속 설정과 `start()`·`stop()`을 제공한다 — 담당자가 2026-10-16에 선택했다. 시작은 다섯 보고 구독의 SUBACK까지 기다리고, 재접속 때 같은 구독을 다시 확인한다.
- 접속 설정은 IOT 내부의 `MqttConfig`다. 주소·포트·클라이언트 ID·계정·비밀번호 파일 경로·keepalive·접속 제한·시작 제한·종료 제한·재접속 최소·최대 대기를 필수로 받는다. 시간의 임의 기본값을 만들지 않는다.
- 연결 구현은 `mqtt/connection.py`다. `MqttConnection`은 보고와 연결 상실을 후속 IOT 모듈에 짧은 콜백으로 인계한다. 콜백에서 CORE를 직접 호출하지 않는다. CORE 인계 작업·용량·재시도는 후속 보고 spec이 맡는다.
- 수신은 `IncomingReport`에 토픽·bytes 본문·QoS·retain·dup·최초 UTC 수신 시각을 보존한다. 연결만으로 기기 온라인 상태를 만들지 않는다.
- 발행 접점 `publish_request()`는 후속 CORE 명령·검사 게이트웨이가 사용한다. 준비되지 않았으면 발행 전에 거부하고, Paho 발행 호출 이후 오류는 전송 불명으로 알린다. PUBACK을 물리 성공으로 바꾸지 않는다.
- `stop()`은 새 발행을 먼저 차단하고 연결 상실을 알린 뒤 네트워크 종료를 제한 시간까지 기다린다. 종료 제한 초과는 실패로 알린다. 종료한 인스턴스는 다시 시작하지 않고 새로 만든다.
- 연결·명령 대기·CORE 인계를 분리한다 — 담당자가 2026-10-15에 선택했다.
- Paho 수신과 CORE 인계 작업을 분리한다 — CORE 잠금 대기가 MQTT 수신을 막지 않게 한다.
- Mosquitto 실행은 `server/`의 `mosquitto -c config/mosquitto.conf`다 — architecture.md의 기존 경로를 따른다.
- 이 spec은 연결 책임을 맡는다 — 담당자가 IoT 전체 spec의 분할을 요청했다.

## 7. 가정

- S2·S3는 위 결정으로 구현한다. 서버 실행 진입점과 실제 CORE·명령 대기·보고 인계의 조립은 각 후속 spec과 #88에서 연결한다.
- 실제 시각 검증은 #140 A안을 따른다. 동기화 방식과 실측 수치는 후속 검증으로 남는다.
- IOT 내부 조립은 다른 네 spec과 함께 검증한다. 새 계층 간 접점은 spec만으로 확정하지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-06 | `test_FR06_only_server_publishes_commands` | 명령 토픽의 발행 권한을 제한한다 |
| FR-07 | `test_FR07_disconnect_keeps_uncertainty` | 연결 끊김을 미전송·성공으로 바꾸지 않는다 |
| FR-13 | `test_FR13_reconnect_resubscribes` | 재구독과 현재 연결 확인을 구분한다 |
| NFR-08 | 5절의 `test_NFR08_*` | 익명·교차 기기 접근을 막는다 |
| NFR-11 | `test_NFR11_mqtt_version_qos_retain` | 일곱 토픽의 전달 설정을 확인한다 |
| NFR-12 | `test_NFR12_local_broker_without_internet` | 외부 서비스 없이 연결한다 |

단위 테스트는 MQTT 전송과 CORE의 fake를 쓴다. 접근 제어는 임시 자격정보와 실제 Mosquitto로 검증한다.
다른 계층을 합친 검증은 서버 조립 작업의 `server/tests/integration/`에 둔다.
