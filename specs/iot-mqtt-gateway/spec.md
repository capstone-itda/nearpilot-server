# MQTT 게이트웨이와 기기 시뮬레이터

- 담당: 이 작업을 지시한 담당자 (상태 관리의 IOT 구현 범위)
- 요구사항: FR-04, FR-05, FR-06, FR-07, FR-13, FR-20, NFR-08, NFR-10, NFR-11, NFR-12
- 따르는 결정: architecture.md에 반영한 #19, #31, #32, #37, #38, #61, #88, #90이다.
- 상태: 구현 전 초안이다. 계약의 부족한 부분과 결정 제안은 [contract-review.md](contract-review.md)에 기록한다.
- 펌웨어 계약 이슈: [#140](https://github.com/capstone-itda/nearpilot-server/issues/140), [#141](https://github.com/capstone-itda/nearpilot-server/issues/141), [#142](https://github.com/capstone-itda/nearpilot-server/issues/142), [#143](https://github.com/capstone-itda/nearpilot-server/issues/143)이다.

## 1. 왜

현재 IOT 계층에는 빈 패키지만 있다. CORE는 기기 보고를 받을 수 없고 물리 명령을 보낼 수 없다.
하드웨어 없이 MQTT 계약을 확인할 기기 시뮬레이터도 필요하다.
서버는 전송 확인과 실제 실행 결과를 구분해야 한다. 결과 불명을 미실행으로 바꾸면 중복 실행과 잘못된 점유 해제가 생긴다.

## 2. 무엇

- 입력:
  - MQTT의 `announce`, `rssi`, `status`, `result`, `safety_check_result`다.
  - CORE의 `NodeCommandGateway.send(NodeCommand, timeout_sec)`다.
  - CORE의 `SafetyCheckGateway.request_check(SafetyCheckRequest)`다.
  - 서버 조립이 주입한 `NodeEventSink`, 운영 설정과 브로커 접속 설정이다.
- 출력:
  - 검증한 기기 보고를 담은 `NodeEventSink`의 다섯 호출이다.
  - `cmd`와 `safety_check`의 MQTT 발행이다.
  - `send()`의 `CommandResult`다. 결과는 미전송·성공·실패·불명·검증한 구동 전 거부를 구분한다.
  - 거부한 메시지와 인계 실패의 진단 기록이다. 기록에는 자격정보 원문을 넣지 않는다.
- 바뀌는 상태:
  - 브로커 연결과 구독 상태다.
  - 기기별 현재 연결 확인과 토픽별 순번 검사에 쓰는 메모리다.
  - 명령별 결과 대기와 검사 요청의 연결을 기억하는 메모리다.
  - CORE에 아직 인계하지 못한 보고의 메모리다.
  - 시뮬레이터의 물리 관측과 재시작 뒤에도 보존하는 명령 기록이다.
- 약속:
  - IOT는 CORE가 넘긴 명령만 발행한다. 허가와 점유 상태는 판단하지 않는다.
  - IOT는 관측 시각과 최초 서버 수신 시각을 그대로 전달한다.
  - IOT는 결과 대기가 끝난 뒤에도 실행 결과를 CORE에 인계한다.
  - IOT는 인계 실패를 기록하고 같은 보고를 다시 인계한다.
  - 첫 구현은 실행 중의 메모리 재인계만 보장한다. 서버 재시작 뒤의 미인계 복원은 보장하지 않는다.
  - 실제 기기 검증과 서버·시뮬레이터 검증을 구분한다.

### MQTT와 shared의 연결

토픽과 본문은 architecture.md §5 MQTT 1~3절을 따른다. 새 토픽이나 필드를 추가하지 않는다.

| 종류 | 방향 | QoS | retain | shared 접점 |
|---|---|---|---|---|
| `announce` | 기기 → 서버 | 1 | true | `on_announce(NodeAnnounce)` |
| `rssi` | 기기 → 서버 | 0 | false | 비콘마다 `on_rssi(RssiFrame)` |
| `status` | 기기 → 서버 | 1 | false | `on_status(NodeStatus)` |
| `result` | 기기 → 서버 | 1 | false | `on_result(NodeResult)`와 명령 결과 대기 |
| `safety_check_result` | 기기 → 서버 | 1 | false | `on_safety_check_result(SafetyCheckResult)` |
| `cmd` | 서버 → 기기 | 1 | false | `send(NodeCommand, timeout_sec)` |
| `safety_check` | 서버 → 기기 | 1 | false | `request_check(SafetyCheckRequest)` |

- `rssi.observations`의 각 항목은 공통 시각·연결·순번을 공유하는 `RssiFrame` 하나가 된다.
- IOT는 RSSI 본문 전체를 검증한 뒤 비콘별로 인계한다. 같은 본문에 중복 비콘이 있으면 거부한다.
- 빈 관측 배열은 유효하다. IOT는 가짜 비콘이나 가짜 RSSI를 만들지 않는다.
- 비콘별 인계가 실패하면 IOT는 실패한 항목을 보존한다. 성공한 항목까지 새 사건으로 만들지 않는다.
- 등록 비콘의 선택과 저장은 CORE·DB의 책임이다. IOT는 수집 회차와 라벨을 받지 않는다.

## 3. 제약

- IOT는 MQTT 3.1.1과 UTF-8 JSON 객체를 쓴다. 재접속하면 `clean_session=true`로 접속하고 다시 구독한다 (architecture.md §5 MQTT 1절, NFR-11).
- IOT는 `paho-mqtt` 2.x를 쓴다. Windows에서 스레드 방식으로 연결한다 (architecture.md §8·9).
- IOT는 `schema_version=1`, 필수 필드와 타입을 검사한다. 알 수 없는 추가 필드를 판정에 쓰지 않는다 (architecture.md §5 MQTT 2절).
- IOT는 토픽과 본문의 `node_id`를 대조한다. 브로커 인증과 접근 제어의 검증 책임은 계약 검토 S2를 먼저 확정한다 (NFR-08).
- IOT는 UTC 시각, UUID, 양의 정수 순번과 열거형을 검사한다. 정수 자리의 bool을 허용하지 않는다 (architecture.md §5 MQTT 2·3절).
- IOT는 RSSI의 유한성, 비콘 UUID 표기와 `major`·`minor` 범위를 검사한다 (FR-05, architecture.md §5 MQTT 3절).
- IOT는 retained 발표를 발견에만 쓴다. 발표만으로 현재 연결·온라인·안전을 확인하지 않는다 (FR-04, FR-13).
- IOT는 신선한 실시간 `status`로 현재 연결을 확인한다. 이전 연결의 관측과 역순·중복 관측은 현재 판정에 쓰지 않는다 (architecture.md §5 MQTT 6절).
- IOT는 새 연결로 바꿀 때 마지막 유효 관측보다 오래된 보고로 되돌아가지 않는다 (architecture.md §5 MQTT 6절).
- IOT는 RSSI 나이에 `beacon_recent_sec`를 쓴다. 반납 검사 나이에는 `safety_report_max_age_sec`를 쓴다 (FR-05, FR-07).
- IOT는 실제 기기의 시각 오차를 임의로 정하지 않는다. 검증 전 실행과 안전 확인을 허용하지 않는다 (FR-07, #140).
- IOT는 `NodeCommand`의 ID·동작·대상 연결·발행 시각·만료 시각을 바꾸지 않는다 (FR-06).
- IOT는 사용자 ID, 외부 요청 ID, 승인 정보와 점유 주체를 기기에 보내지 않는다 (NFR-08).
- IOT는 새 명령 ID로 자동 재시도하지 않는다. 같은 명령의 재전송도 내용을 바꾸지 않는다 (FR-06).
- 명령의 유효 시간은 `exec_timeout_sec`다. CORE가 만든 `expires_at`을 재전송 시점으로 연장하지 않는다 (architecture.md §5 MQTT 6절).
- IOT는 발행하지 않았음을 증명할 때만 `NOT_SENT`를 반환한다. 발행 시도 뒤 응답이 없으면 `UNKNOWN`이다 (FR-07).
- IOT는 PUBACK, `accepted`, `running`과 일반 상태 보고를 실행 성공으로 보지 않는다 (FR-07).
- IOT는 `DEVICE_BUSY` 거부만 `CommandOutcome.REJECTED`로 반환한다. 다른 거부는 `FAILED`로 반환하고 원문 보고를 보존한다 (FR-07, shared `NodeCommandGateway`).
- IOT는 모든 거부의 `NodeResult.state`와 `error_code`를 보존한다. 오류 코드를 호스트의 사유 코드로 바꾸지 않는다 (architecture.md §4 "기기 보고의 인계").
- 기기 결과를 반환할 때 IOT는 같은 `NodeResult`를 `CommandResult.report`에 담는다. CORE 인계에도 같은 `event_id`를 쓴다 (FR-07).
- 서버 타임아웃과 통신 오류에는 `report`가 없다. IOT는 서버가 만든 결과를 `NodeResult`로 만들지 않는다 (FR-07).
- IOT는 과거 연결의 실행 결과도 정산 경로로 인계한다. 과거 결과로 현재 연결·온라인·안전을 갱신하지 않는다 (FR-07, FR-13).
- IOT는 검사 요청의 기기·검사 ID·대상 연결·기준을 대조한다. 반납 종료와 안전 충족의 결정은 CORE가 한다 (FR-20).
- `request_check()`는 검사 결과를 기다리지 않는다. 응답 기한과 재검사는 CORE가 잰다 (FR-20).
- IOT는 계층 간 접점에 `shared`만 쓴다. CORE와 DB 구현을 직접 불러오지 않는다 (AGENTS.md 의존 규칙 4).
- Mosquitto는 익명 접속을 거부한다. 기기 계정은 자기 보고만 발행하고 자기 명령·검사 요청만 구독한다 (NFR-08).
- IOT는 접속 정보와 자격정보를 실행 설정에서 받는다. 실제 비밀번호 파일을 저장소에 넣지 않는다 (NFR-08).
- 서버와 시뮬레이터는 독립 AP 안의 Mosquitto로 통신한다. 외부 인터넷을 실행 조건으로 두지 않는다 (NFR-12).
- 임계값·주기·한도는 설정으로 받는다. 미결인 공용 설정은 구현 상수로 대체하지 않는다 (requirements.md §1.2).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/`
- `server/tests/iot/`
- `server/tools/node_sim/`
- `server/config/`의 Mosquitto 설정과 비밀값 없는 접근 제어 예시다. AGENTS.md가 IOT 구현자에게 배정한 경로다.
- `specs/iot-mqtt-gateway/`

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`의 Gotchas 밖, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`이다.
- 이 작업: `core/`, `db/`, `api/`, `firmware/`, `app/`, `dashboard/`다.
- 공용 설정과 다른 계층 계약은 별도 decision PR에서 반영한다. 이 spec으로 경계를 넓히지 않는다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeCommandGateway.send` | 제공 | 상태 관리 |
| `SafetyCheckGateway.request_check` | 제공 | 상태 관리 |
| `NodeEventSink`의 다섯 호출 | 사용 | CORE의 기기 이벤트 서비스 |
| `Settings`와 기기 보고 모델 | 사용 | shared |
| architecture.md §5 MQTT v1 | 제공·사용 | 펌웨어와 시뮬레이터 |
| Mosquitto 인증과 토픽 접근 제어 | 제공 | 기기·서버 접속 설정 |
| 시작·종료와 구독 준비 확인 | 연결 방식 미결 S3 | #88의 서버 조립 |

### 만들지 않는 것

- 펌웨어와 실제 센서 드라이버다.
- 판정, 기기 신뢰 부여, DB 저장과 점유 정산이다.
- CORE의 반납 응답 기한·재검사·장애 복구다.
- 서버 실행 진입점과 CORE 서비스 조립이다. 게이트웨이 연결은 S3 확정 뒤 조립 작업이 맡는다.
- 수집 회차 기록, RSSI 모델 학습과 성능 평가다.
- 서버의 미인계 보고를 재시작 뒤 복원하는 영속 저장이다.
- 실제 기기 검증을 완료했다는 선언이다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| JSON·필수 타입·버전·토픽이 틀리다 | 거부 사유를 기록한다. CORE에 인계하지 않는다 | `test_FR07_invalid_message_not_forwarded` |
| 토픽과 본문의 기기 ID가 다르다 | 거부한다 | `test_NFR08_topic_payload_identity_mismatch` |
| 추가 필드에 사용자 ID나 `safe=true`가 있다 | 실행과 안전의 근거로 쓰지 않는다 | `test_FR07_extra_fields_do_not_authorize` |
| retained 발표가 늦게 도착한다 | 발견만 인계한다. 현재 연결을 바꾸지 않는다 | `test_FR04_retained_announce_not_live_session` |
| `status` 확인 전 RSSI가 온다 | 현재 관측으로 인계하지 않는다. 수신 시각으로 최신성을 꾸미지 않는다 | `test_FR05_rssi_requires_confirmed_session` |
| RSSI 배열이 비었다 | 유효하게 읽는다. `RssiFrame`은 만들지 않는다 | `test_FR05_empty_observations_no_synthetic_frame` |
| 같은 RSSI 본문에 비콘이 둘이다 | 같은 순번과 시각으로 두 프레임을 인계한다 | `test_FR05_batch_observations_preserve_metadata` |
| RSSI의 비콘별 CORE 인계가 실패한다 | 실패한 항목을 보존한다. 재인계는 원래 모델을 쓴다 | `test_FR05_partial_handover_retries_missing_item` |
| 이전 연결·역순·중복 관측이 온다 | 현재 판정에 인계하지 않는다 | `test_FR13_old_or_duplicate_status_not_live` |
| 새 연결의 보고가 마지막 유효 관측보다 오래됐다 | 연결을 되돌리지 않는다 | `test_FR13_session_cannot_move_backwards` |
| 시각 오차와 상태 허용 나이가 미설정이다 | 실제 기기의 현재 연결·실행·안전 확인을 허용하지 않는다 | `test_FR07_unverified_clock_blocks_execution` |
| CORE가 명령을 인계하지 않았다 | `cmd`를 발행하지 않는다 | `test_FR06_no_command_without_core_call` |
| 발행 전에 전송 불가를 확인했다 | 미전송이 확실하면 `NOT_SENT`다 | `test_FR06_definite_not_sent_before_publish` |
| 발행 뒤 연결이 끊기거나 기한이 지났다 | `UNKNOWN`이다. 보고를 구독하고 새 ID로 재시도하지 않는다 | `test_FR07_disconnect_or_timeout_is_unknown` |
| PUBACK·접수·구동 시작만 확인했다 | 완료로 반환하지 않는다 | `test_FR07_progress_is_not_success` |
| `DEVICE_BUSY` 거부가 왔다 | `REJECTED`와 원래 보고를 반환한다 | `test_FR07_device_busy_preserves_rejection` |
| 다른 거부 코드가 왔다 | `FAILED`와 원래 `rejected` 보고를 반환한다 | `test_FR07_other_rejections_are_failed_with_report` |
| 기기·명령 ID가 다른 결과가 왔다 | 대기 중인 다른 명령을 완료하지 않는다 | `test_FR07_result_matches_node_and_command` |
| `send()`가 결과를 기다릴 때 결과가 온다 | 대기를 깨운다. CORE 인계의 잠금 대기와 교착하지 않는다 | `test_FR07_result_wait_and_sink_do_not_deadlock` |
| `send()` 반환과 이벤트가 같은 사건이다 | 같은 `event_id`를 보존한다. 정산은 CORE·DB가 중복을 막는다 | `test_FR07_send_and_event_share_report` |
| 대기가 끝난 뒤 과거 연결의 결과가 온다 | 정산 경로로 인계한다. 현재 연결을 바꾸지 않는다 | `test_FR07_late_old_session_result_forwarded` |
| 결과 순번이 관측 순번보다 낮다 | 관측의 중복 기준으로 실행 결과를 버리지 않는다 | `test_FR07_result_not_filtered_as_observation` |
| 상충하는 결과가 온다 | 성공이나 미전송으로 합치지 않는다. 충돌 근거를 CORE에 남긴다 | `test_FR07_conflicting_reports_not_hidden` |
| CORE 인계가 예외로 실패했다 | 최초 수신 시각과 사건 ID를 유지하고 재인계한다 | `test_FR07_sink_failure_retries_same_event` |
| 미인계 보고가 있는 서버를 재시작한다 | 메모리 재인계 보장이 끝난다. 미전송·성공으로 추정하지 않는다 | `test_FR07_restart_does_not_claim_delivery` |
| 검사 요청을 발행한다 | 결과를 기다리지 않고 반환한다 | `test_FR20_check_request_does_not_wait_for_result` |
| 검사 결과의 ID·기준·연결·나이가 틀리다 | 종료 근거로 인계하지 않는다. CORE의 확인 불능 경로가 적용된다 | `test_FR20_invalid_check_not_close_evidence` |
| 검사 결과에 필수 근거가 없다 | 값을 만들지 않는다. 유효한 통과로 인계하지 않는다 | `test_FR20_missing_evidence_never_passes` |
| 브로커를 재접속한다 | 보고 토픽을 다시 구독한다. retained 발표로 현재 연결을 복원하지 않는다 | `test_FR13_reconnect_resubscribes_and_reconfirms` |
| 다른 기기 계정으로 보고를 발행하거나 명령을 읽는다 | Mosquitto가 차단한다 | `test_NFR08_broker_blocks_cross_node_access` |
| 시뮬레이터에 같은 명령이 재전송된다 | 기록한 결과만 반환한다. 물리 동작 횟수는 늘리지 않는다 | `test_FR06_simulator_duplicate_command_runs_once` |
| 시뮬레이터가 구동 중 새 명령을 받는다 | 대기열에 넣지 않고 `DEVICE_BUSY`로 거부한다 | `test_FR06_simulator_busy_rejects_without_queue` |
| 시뮬레이터가 명령 도중 재시작한다 | 기록을 읽는다. 완료를 증명하지 못하면 `unknown`이다 | `test_FR07_simulator_restart_preserves_command_record` |

## 6. 결정

- 메시지 검증·연결 관리·명령 대기·CORE 재인계를 분리한다 — 담당자가 2026-10-15에 추천안을 선택했다.
- Paho 수신 스레드와 CORE 인계 작업을 분리한다 — 담당자가 같은 선택에서 승인했다.
- 수신은 명령 결과 대기를 먼저 풀 수 있어야 한다 — CORE는 전송을 기다리는 동안 기기 잠금을 보유할 수 있다.
- CORE 인계 완료를 기다리는 동작으로 MQTT 수신을 막지 않는다 — 동기 `send()`와 결과 인계의 교착을 막는다.
- 첫 구현은 실행 중 메모리 재인계만 보장한다 — 담당자가 2026-10-15에 선택했다.
- 재인계할 때 최초 `received_at`을 바꾸지 않는다 — 실패 후 지연을 최신 관측으로 만들지 않는다.
- 메시지 검증은 `iot/messages/`, 연결·대기·인계는 `iot/mqtt/`에 둔다 — architecture.md §2의 경로를 따른다.
- 시뮬레이터도 실제 MQTT 본문과 접근 제어를 쓴다 — 하드웨어 없는 계약 검증이 목적이다.
- 시뮬레이터는 모든 메시지의 정상 경로와 실패 주입을 제공한다 — 실제 기기의 센서 검증을 대신하지 않는다.

## 7. 가정

- 펌웨어 담당이 없는 동안 기존 v1은 개발 기준이다. 실제 하드웨어의 완료·안전 검증은 별개다 — #140~#143이다.
- 시뮬레이터는 서버와 같은 PC의 UTC 시계를 쓴다 — architecture.md §5 MQTT 6절이다.
- 첫 구현의 반납 기준은 기존 상태 관리 spec이 쓰는 §4 표를 참조한다. §5와의 차이는 확정 전 별도 변경하지 않는다 — #141이다.
- 충돌한 검사 요청은 기존 계약대로 응답 기한 초과로 다룬다. 기기는 기존 검사 결과를 덮어쓰지 않는다 — #143이다.
- 서버 재시작 때 미인계 보고를 잃을 수 있다. QoS 1을 CORE 저장 완료의 보장으로 설명하지 않는다 — 담당자 결정이다.
- 서버 조립은 검증 전 실제 기기의 실행과 안전 확인을 차단한다. 게이트웨이도 임의의 시각 오차를 쓰지 않는다 — #140과 S3다.
- 상태 보고 허용 나이와 연결 전환의 동률 규칙은 미결이다. 이 검증 부분은 S1 확정 전 구현하지 않는다.
- 인증 주체의 증명은 미결이다. 기기 계정·토픽·본문의 검증 책임은 S2 확정 전 구현 완료로 표시하지 않는다.
- 접속 설정의 주입과 시작·종료 접점은 미결이다. 서버 조립 연결은 S3 확정 전 구현하지 않는다.
- 재인계 주기·용량·포화 동작은 미결이다. 메모리 보장 범위를 넘는 수치를 임의로 넣지 않는다 — S4다.
- `shared`와 `docs`의 변경이 필요하면 별도 결정으로 먼저 반영한다. 이 spec은 새 계약의 확정을 대신하지 않는다.

## 8. 테스트

아래 IOT 테스트는 구현할 계획이다. 기존 회귀 테스트와 새 IOT 검증을 구분한다.
2026-10-15 문서 작성 때 기존 전체 `pytest`는 1,488개 통과, 1개 예상 실패였다.
이 결과는 새 게이트웨이와 시뮬레이터의 구현 완료를 뜻하지 않는다.

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_announce_decoded_and_forwarded` | 기능·점유 방식·연결·순번·시각을 보존한다 |
| FR-05 | `test_FR05_rssi_metadata_and_validation` | 비콘별 모델, 유한한 RSSI, 최신성과 중복을 검사한다 |
| FR-06 | `test_FR06_core_command_is_only_publish_source` | CORE 명령만 발행하고 ID·대상 연결·만료를 보존한다 |
| FR-07 | `test_FR07_terminal_outcome_mapping` | 성공·실패·불명·거부와 원래 보고를 구분한다 |
| FR-07 | `test_FR07_timeout_has_no_device_report` | 서버 결과에 기기 사건을 만들지 않는다 |
| FR-13 | `test_FR13_only_live_status_confirms_session` | 발표·과거 결과로 현재 연결을 확인하지 않는다 |
| FR-20 | `test_FR20_safety_request_and_result_contract` | 검사 요청은 기다리지 않고 유효한 결과만 인계한다 |
| NFR-08 | `test_NFR08_credentials_acl_and_minimal_payload` | 익명 접속·교차 기기 접근을 막고 내부 계정 정보를 보내지 않는다 |
| NFR-10 | `test_NFR10_simulator_capability_matrix` | 사물함 3개·조명 3개·스피커 1개·무드등 1개의 보고 형식을 확인한다 |
| NFR-11 | `test_NFR11_mqtt_version_qos_and_retain` | MQTT 3.1.1과 토픽별 QoS·retain을 확인한다 |
| NFR-12 | `test_NFR12_local_broker_without_external_services` | 외부 서비스 없이 브로커와 시뮬레이터를 연결한다 |

- IOT 단위 테스트는 `NodeEventSink`와 MQTT 전송의 fake를 쓴다. 다른 계층 구현을 불러오지 않는다.
- Mosquitto 계약 검증은 별도 브로커에서 시뮬레이터와 실제 Paho 연결을 쓴다.
- 접근 제어 검증은 임시 자격정보로 정상 발행·구독, 익명 접속, 다른 기기 발행과 읽기 시도를 확인한다.
- 시뮬레이터 검증은 같은 ID 재전송, 만료, 연결 변경, 구동 중 거부, 기록 저장 실패와 재시작을 포함한다.
- 반납 검사 검증은 네 결과, 중복 검사, 새 검사 ID, 구기준과 근거 누락을 포함한다.
- 서버와 CORE·DB를 합치는 시나리오는 후속 조립 작업에서 `server/tests/integration/`에 둔다.
- 구현 전에 `server/`에서 전체 `pytest`를 확인하고 spec을 먼저 커밋한다.
- 구현 뒤 `server/`에서 `pytest tests/iot`와 전체 `pytest`를 실행한다.
- 실제 기기는 #140~#143의 검증을 따로 마친다. 시뮬레이터 통과만으로 실제 기기 사용을 허가하지 않는다.
