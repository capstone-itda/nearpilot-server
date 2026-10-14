# MQTT 게이트웨이

- 상태: 작성 중
- 담당: JM-220 (상태 관리)
- 요구사항: FR-04, FR-05, FR-06, FR-07, NFR-08, NFR-12
- 따르는 결정: #17, #19, #37, #38

## 1. 왜

- CORE는 기기와 직접 통신하지 않는다. CORE는 `NodeCommandGateway`, `SafetyCheckGateway`, `NodeEventSink`로만 기기와 닿는다 (architecture.md §4).
- 저장소에는 세 접점을 MQTT에 연결하는 코드가 없다. `iot/mqtt/`는 비어 있다.
- 게이트웨이가 없으면 아래 문제가 생긴다.
  - 서버가 허가한 명령을 기기에 보낼 수 없다 (FR-06).
  - 다른 기기를 사칭한 보고, 이전 연결의 보고, 오래된 보고를 CORE에 넘길 수 있다.
  - 시간 초과를 미실행으로 잘못 보고해서 승인 사용분과 점유를 잘못 반환할 수 있다 (FR-19).

## 2. 무엇

- 입력:
  - 브로커에서 받은 기기 보고 (`announce`, `rssi`, `status`, `result`, `safety_check_result`)
  - CORE의 `NodeCommandGateway.send(command, timeout_sec)`
  - CORE의 `SafetyCheckGateway.request_check(request)`
- 출력:
  - 검증한 기기 보고를 `NodeEventSink`로 CORE에 넘긴다.
  - `send()`는 `CommandResult`를 반환한다. 결과는 `NOT_SENT`, `SUCCEEDED`, `FAILED`, `UNKNOWN`, `REJECTED` 중 하나다.
  - 브로커에 `cmd`와 `safety_check`를 발행한다.
- 바뀌는 상태: 게이트웨이 메모리의 기기별 현재 연결, 마지막 순번, 결과 대기 목록, 인계 재시도 목록. DB는 바꾸지 않는다.
- 약속:
  - 검증을 통과한 보고만 CORE에 넘긴다.
  - 발행을 시도하지 않은 것이 확실할 때만 `NOT_SENT`를 반환한다.
  - 발행을 시도한 뒤 결과가 없으면 `UNKNOWN`이다. 시간 초과와 연결 끊김을 미실행으로 보지 않는다.
  - 새 명령 ID로 자동 재시도하지 않는다.
  - 결과 대기가 끝난 뒤에도 늦은 결과를 받아서 CORE에 넘긴다.

## 3. 제약

- MQTT 3.1.1, `clean_session=true`로 접속한다. 접속할 때마다 구독한다 (architecture.md §5 MQTT 1절).
- 서버는 전용 계정으로 접속한다. 자격정보와 접속 설정은 실행 설정에서 받는다. 코드와 저장소에 비밀번호를 쓰지 않는다 (NFR-08, §5 MQTT 1절).
- 토픽은 `nearpilot/node/{node_id}/{종류}`다. QoS와 retain은 §5 MQTT 1절의 표를 따른다.
- 토픽의 `node_id`와 본문의 `node_id`가 다르면 보고를 거부한다 (§5 MQTT 1절).
- 서버는 신선한 실시간 `status`로 현재 연결을 확인한다. retained `announce`만으로 현재 연결을 바꾸지 않는다 (§5 MQTT 6절).
- 이전 연결의 `status`와 RSSI, 역순 보고, 중복 보고는 CORE에 넘기지 않는다 (§5 MQTT 6절).
- 이전 연결의 `result`는 원래 명령의 정산 근거로 넘긴다 (§5 MQTT 5절).
- 관측 시각이 허용 나이를 넘었거나 허용 오차 밖의 미래이면 보고를 거부한다. 허용 나이는 설정에서 받는다 (§5 MQTT 6절).
  - RSSI는 `beacon_recent_sec`, 반납 검사 결과는 `safety_report_max_age_sec`를 쓴다.
- 관측 시각을 수신 시각으로 바꾸지 않는다. 수신 시각은 게이트웨이가 찍어서 따로 넘긴다 (architecture.md §4 "기기 보고의 인계").
- 결과 대기 시간은 `send()`의 `timeout_sec` 인자로 받는다. 코드 상수로 쓰지 않는다 (AGENTS.md 코딩 규칙).
- `accepted`, `running`, PUBACK은 성공이 아니다. `succeeded` 결과만 성공이다 (§5 MQTT 5절).
- `rejected`와 `DEVICE_BUSY`만 `CommandOutcome.REJECTED`다. 다른 거부 코드는 `FAILED`다 (§5 MQTT 5절).
- 서버가 만든 결과(`NOT_SENT`, 시간 초과의 `UNKNOWN`)에는 `report`가 없다. 서버는 이 결과를 `NodeResult`로 만들지 않는다 (architecture.md §4 "기기 보고의 인계").
- 기기가 보고한 `unknown`과 서버의 시간 초과를 구분한다. 근거에 출처를 남긴다 (FR-07).
- `request_check()`는 결과를 기다리지 않는다 (architecture.md §4 "반납 검사와 종료").
- CORE 인계가 예외로 실패하면 실패를 기록하고 다시 인계한다. 인계에 실패한 보고를 버리지 않는다 (§4 "기기 보고의 인계").
- 게이트웨이는 안전, 점유 해제, 장애 상태를 판정하지 않는다 (architecture.md §4 기기 이벤트 인터페이스).
- 브로커 설정 파일은 `server/config/mosquitto.conf`다. 익명 접속을 막는다. 기기 계정은 자기 토픽만 쓰고 읽는다 (NFR-08, §5 MQTT 1절).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/iot/mqtt/`
- `server/config/mosquitto.conf`, `server/config/` 아래의 ACL 예시 파일. 비밀번호 파일은 넣지 않는다.
- `server/tests/iot/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `iot/messages/` (스키마 결함은 `iot-mqtt-messages`의 fix로 고친다), `core/`, `db/`, `api/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeCommandGateway.send` | 제공 | CORE (상태 관리) |
| `SafetyCheckGateway.request_check` | 제공 | CORE (상태 관리) |
| `NodeEventSink` 다섯 함수 | 사용 | CORE |
| MQTT 계약 v1 (architecture.md §5) | 제공 | 펌웨어, 시뮬레이터 |
| `iot/messages` 스키마 | 사용 | 상태 관리 (`iot-mqtt-messages`) |

### 만들지 않는 것
- 페이로드 스키마 (`iot-mqtt-messages`)
- 기기 이벤트의 CORE 쪽 처리 (`core-node-events`)
- 명령 결과의 저장과 정산 (`core-execution`)
- 서버 실행 진입점과 게이트웨이 조립. 진입점은 미결이다 (AGENTS.md 검증 명령).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 토픽과 본문의 `node_id`가 다르다 | 보고를 거부하고 기록한다 | `test_NFR08_reject_node_id_mismatch` |
| 같은 연결, 같은 토픽의 `seq`가 마지막 값 이하다 | 역순 또는 중복으로 거부한다 | `test_FR05_reject_stale_seq` |
| 이전 연결의 `status`가 온다 | CORE에 넘기지 않는다 | `test_FR07_drop_old_session_status` |
| 이전 연결의 `result`가 온다 | 원래 명령의 결과로 CORE에 넘긴다 | `test_FR07_forward_old_session_result` |
| retained `announce`만 오고 `status`가 없다 | 현재 연결을 확정하지 않는다. 발표는 넘긴다 | `test_FR04_retained_announce_not_session_proof` |
| 브로커 연결이 없어 발행을 시도하지 못했다 | `NOT_SENT`를 반환한다 | `test_FR06_not_sent_when_publish_not_attempted` |
| 발행했지만 `timeout_sec` 안에 결과가 없다 | `UNKNOWN`을 반환한다. 구독은 유지한다 | `test_FR07_timeout_is_unknown` |
| 발행 도중 연결이 끊겼다 | `UNKNOWN`을 반환한다 | `test_FR07_disconnect_after_publish_is_unknown` |
| `accepted` 뒤 `running`만 오고 시간이 끝난다 | `UNKNOWN`을 반환한다 | `test_FR07_running_is_not_success` |
| `rejected`와 `DEVICE_BUSY`가 온다 | `REJECTED`와 `report`를 반환한다 | `test_FR06_device_busy_is_rejected` |
| `rejected`와 `SESSION_MISMATCH`가 온다 | `FAILED`를 반환한다 | `test_FR07_other_reject_is_failed` |
| 결과 대기가 끝난 뒤 결과가 온다 | `on_result()`로 CORE에 넘긴다 | `test_FR07_late_result_forwarded` |
| 같은 `event_id`의 결과가 두 번 온다 | 두 번 모두 넘길 수 있다. 중복 정산은 CORE와 DB가 막는다 | `test_FR07_duplicate_event_forwarded_with_same_id` |
| `on_result()`가 예외를 던진다 | 실패를 기록하고 다시 인계한다 | `test_FR07_retry_sink_failure` |
| 같은 `command_id`로 `send()`를 다시 부른다 | 같은 본문을 다시 발행한다. 새 명령 ID를 만들지 않는다 | `test_FR06_resend_same_command_id` |
| 반납 검사 요청의 발행이 실패한다 | 예외를 던지지 않는다. CORE가 응답 기한 초과로 다룬다 | `test_FR07_safety_check_publish_failure` |

## 6. 결정

## 7. 가정

- 미결: IOT 구현의 담당 배정 (architecture.md §2). 이 spec은 §2 담당 표대로 상태 관리가 `iot/`를 맡는다고 가정한다.
- 미결: 시각 오차 허용값 (architecture.md §5 MQTT 6절). 개발 기준에서는 시뮬레이터와 서버가 같은 PC의 시계를 쓴다. 허용 오차가 정해질 때까지 미래 시각 검사의 허용 오차는 생성자 인자로 받는다. 기본값은 0초다.
- 미결: 서버 실행 진입점. 게이트웨이는 생성자 인자로 `NodeEventSink`와 접속 설정을 받는다. 조립은 진입점이 정해진 뒤 한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_forward_announce` | 검증한 발표를 `on_announce()`로 넘긴다 |
| FR-05 | `test_FR05_forward_rssi_with_received_at` | RSSI 관측마다 `on_rssi()`를 부른다. `frame_ts`와 `received_at`이 다르다 |
| FR-06 | `test_FR06_send_publishes_cmd_and_waits_result` | `cmd`를 QoS 1로 발행하고 `succeeded` 결과로 `SUCCEEDED`를 반환한다 |
| FR-07 | `test_FR07_forward_safety_check_result` | 검증한 검사 결과를 `on_safety_check_result()`로 넘긴다 |
| NFR-08 | `test_NFR08_broker_config_denies_anonymous` | 설정 파일이 익명 접속을 막고 기기별 토픽 접근을 제한한다 |
| NFR-12 | `test_NFR12_reconnect_resubscribes` | 재접속하면 모든 기기 보고 토픽을 다시 구독한다 |

- paho 클라이언트는 fake로 대신한다. 실제 브로커 연결 시험은 `server/tests/integration/`에 둔다.
