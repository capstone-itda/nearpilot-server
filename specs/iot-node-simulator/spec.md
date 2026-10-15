# MQTT 기기 시뮬레이터와 실패 시나리오

- 담당: 이 작업을 지시한 담당자 (상태 관리의 IOT 구현 범위)
- 구현 브랜치: `feat/iot-node-simulator`
- 요구사항: FR-04, FR-05, FR-06, FR-07, FR-20, NFR-08, NFR-10, NFR-11, NFR-12
- 따르는 결정: architecture.md에 반영한 #19, #37, #61, #140~#143이다.
- 상태: 시뮬레이터와 IOT 내부 전체 연결 시나리오를 구현했다. 실행 방법은 `server/tools/node_sim/README.md`에 있다.

## 1. 왜

실제 ESP32 없이도 MQTT 계약과 중복 실행 방지를 확인해야 한다.
전원 차단·결과 지연·연결 단절을 반복해도 같은 실패를 재현할 수 있어야 한다.

## 2. 무엇

- 입력: 기기별 실행 설정, 가상 물리 관측·비콘·실패 시나리오, `cmd`·`safety_check`다.
- 출력: MQTT v1의 기능 발표·RSSI·상태·명령 결과·검사 결과다.
- 바뀌는 상태: 연결 ID·토픽별 순번·물리 관측·구동 여부와 재시작 뒤 보존하는 명령 기록이다.
- 약속:
  - 가상 기기도 실제 MQTT 연결과 기기별 자격정보를 쓴다.
  - 같은 명령은 재접속·재시작 뒤에도 다시 구동하지 않는다.
  - 결과를 증명하지 못하면 `unknown`이다. 실제 센서의 검증 완료로 설명하지 않는다.
  - 기능별 정상 경로와 실패 경로를 실행 설정으로 재현한다.

## 3. 제약

- 연결마다 새 UUID를 만들고 발표한다. 순번은 같은 연결·토픽에서 1부터 증가한다 (FR-04, architecture.md §5 MQTT 2절).
- 발표·RSSI·상태·결과의 QoS와 retain은 MQTT v1 표를 따른다 (NFR-11).
- RSSI는 `rssi_frame_sec`, 상태 보고는 `status_report_sec`를 따른다. 주기를 코드 상수로 넣지 않는다 (FR-05, requirements.md §1.2).
- 서버와 같은 PC의 UTC 시계를 쓴다. 실제 기기의 오차 검증을 완료한 것으로 취급하지 않는다 (FR-07, #140).
- 기존 명령 ID 조회를 연결·만료 검사보다 먼저 한다. 같은 내용이면 기존 결과만 보고한다 (FR-06).
- 같은 ID의 다른 내용은 `COMMAND_ID_CONFLICT`로 거부하고 기존 기록을 보존한다 (FR-06).
- 새 구동 명령의 진행 확인·접수·시작을 직렬화한다. 구동 중이면 대기열 없이 `DEVICE_BUSY`로 거부한다 (FR-06).
- 명령 접수·거부 기록을 재시작 뒤에도 보존한다. 접수 기록을 저장하기 전에 구동하지 않는다 (FR-06).
- 새 명령의 버전·기능·대상 연결·유효 시간을 검사한다. 시작 직전에도 연결과 만료를 확인한다 (FR-06).
- 재시작 뒤 결과를 증명하지 못하면 `unknown`이다. 구동을 자동 재개하지 않는다 (FR-07).
- 명령 기록은 초기 구현에서 자동 삭제하지 않는다. 외부 요청 ID의 보존기간을 적용하지 않는다 (FR-06).
- 결과 재보고는 사건 ID·상태·발생 시각을 보존하고 발행 순번·시각을 새로 만든다 (FR-07).
- 검사 기준은 §4의 v1 표다. 사물함은 `door_closed`·`locked`, 조명·스피커는 `actuator_idle`이다 (FR-20, #141).
- 각 검사 항목의 bool 값과 관측 시각을 담는다. 모든 항목이 true일 때만 `passed`다 (FR-20).
- 새 검사 ID에는 새 측정을 한다. 같은 ID·내용에는 기존 결과만 보고한다 (FR-20).
- 같은 검사 ID의 다른 내용은 검사하지 않고 기존 결과를 보존한다. 서버는 응답 기한 초과로 다룬다 (FR-20, #143).
- 측정하지 못한 bool을 꾸미지 않는다. 확인 불가와 정상 `not_ready`를 구분한다 (FR-07, #143).
- IR 무드등의 관측 수단이 없는 시나리오는 `light_on=null`이다. IR 송출만으로 성공을 만들지 않는다 (FR-07, #142).
- 스피커의 가상 완료는 실제 재생 확인 능력과 구분한다. MQTT에 새 재생 필드를 임의로 추가하지 않는다 (FR-07, #142).
- 가상 장비 구성은 사물함 3개·조명 3개·스피커 1개·다른 장소의 무드등 1개다 (NFR-10).
- 기기별 계정과 자기 토픽의 접근 제어를 따른다. 실제 비밀번호를 저장소에 넣지 않는다 (NFR-08).
- 독립 AP의 Mosquitto와 통신한다. 외부 서비스 없이 실행할 수 있어야 한다 (NFR-12).

## 4. 경계

### 고치는 곳

- `server/tools/node_sim/`의 시뮬레이터·실행 안내·비밀값 없는 예시 설정이다.
- `server/tests/iot/`의 시뮬레이터 테스트와 IOT 내부 연결 시나리오다.

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`의 Gotchas 밖, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 이 작업: 실제 펌웨어·센서 드라이버, CORE·DB·API와 수집·평가 도구다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| MQTT v1 | 기기 역할로 제공·사용한다 | IOT 게이트웨이 |
| `Settings`의 보고 주기·제한 시간 | 실행 설정에 사용한다 | shared |

### 만들지 않는 것

- 실제 BLE 스캔·센서 드라이버·SNTP 구현이다.
- 근접 모델과 평가 지표의 통과 선언이다.
- 실제 기기의 센서 조합·스피커 필드와 완료 근거의 확정이다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 같은 명령이 재전송된다 | 기존 결과만 보고한다. 구동 횟수는 늘지 않는다 | `test_FR06_duplicate_runs_once` |
| 같은 ID의 내용이 다르다 | 충돌로 거부하고 기존 기록을 보존한다 | `test_FR06_command_conflict_keeps_record` |
| 구동 중 새 명령이 온다 | `DEVICE_BUSY`로 거부하고 거부 기록을 보존한다 | `test_FR06_busy_rejected_without_queue` |
| 명령이 만료했거나 대상 연결이 다르다 | 구동하지 않고 대응하는 코드로 거부한다 | `test_FR06_expired_or_wrong_session_rejected` |
| 접수 기록 저장이 실패한다 | 구동하지 않는다 | `test_FR06_storage_error_prevents_actuation` |
| 명령 도중 재시작한다 | 기록을 읽고 `unknown`을 보고한다 | `test_FR07_restart_preserves_record` |
| 결과가 지연·유실되거나 연결이 끊긴다 | 서버의 불명·늦은 결과 경로를 재현한다 | `test_FR07_delayed_or_lost_result` |
| 비콘이 없거나 복수다 | 빈 배열 또는 비콘별 관측을 보고한다 | `test_FR05_empty_and_multiple_beacons` |
| 재접속한다 | 새 연결 ID로 발표하고 순번을 새로 시작한다 | `test_FR04_reconnect_announces_new_session` |
| 검사 항목이 false이거나 측정 불능이다 | 정상 `not_ready`와 확인 불능을 구분한다 | `test_FR20_check_outcome_matrix` |
| 같은 검사 ID의 내용이 다르다 | 새 측정·덮어쓰기를 하지 않는다 | `test_FR20_check_conflict_keeps_record` |
| 일반 상태·명령 성공만 있다 | 반납 검사 통과를 대신하지 않는다 | `test_FR20_status_not_check_evidence` |
| 실제 관측이 없는 무드등 시나리오다 | `null`과 확인 불가를 보존한다 | `test_FR07_unobserved_light_not_success` |

## 6. 결정

- 시뮬레이터는 별도 spec으로 구현한다 — 전체 IOT 범위를 한 feat PR에 묶지 않는다.
- 실제 MQTT 본문과 자격정보를 쓴다 — 서버의 계약 경로를 직접 검증한다.
- 결과 사건과 명령 기록은 구분한다 — 재보고와 중복 구동 방지를 각각 확인한다.
- 명령·검사 기록은 기기별 SQLite 파일에 둔다 — 담당자가 2026-10-16에 A안을 선택했다. 서버 DB와 분리하고 표준 `sqlite3`만 쓴다.
- 명령 기록은 명령 ID·내용·마지막 결과 사건(ID·상태·발생 시각·거부 코드)이다. 접수와 결과 갱신은 각각 한 트랜잭션이다. 접수 기록의 커밋이 끝난 뒤에만 구동한다.
- 거부한 새 명령도 기록한다. 같은 ID의 다른 내용은 기록하지 않고 새 사건 ID로 `COMMAND_ID_CONFLICT`를 보고한다.
- 재시작하면 `accepted`·`running`으로 남은 기록을 새 사건 ID의 `unknown`으로 바꾸고 접속한 뒤 보고한다. 구동을 재개하지 않는다.
- 구조는 MQTT와 분리한 기기 논리 `node.py`의 `SimNode`, 기록 `store.py`, Paho 연결 `device.py`의 `SimDevice`, 실행 설정 `config.py`와 `python -m tools.node_sim` 실행이다.
- 실패 시나리오는 기기별 실행 설정의 `scenario`로 준다. 구동 시간, 최종 결과, 결과 지연·유실, 조명 관측 가능 여부, 측정 불능 항목, 센서 고장, 기록 저장 실패, 비콘 목록이다.
- 측정하지 못한 검사 항목은 근거에서 빼고 `unknown`을 보고한다. 서버는 불완전한 근거를 확인 불능으로 다룬다 (#143 A안).
- 같은 검사 ID의 다른 내용, 대상 연결이 다르거나 만료한 검사 요청에는 검사·보고하지 않는다. 서버는 응답 기한 초과로 다룬다.
- 사물함의 `open`은 잠금을 풀고 문 닫힘은 시나리오 조작으로 바꾼다. 문을 닫으면 다시 잠근다. 이는 가상 관측이며 실제 센서 능력이 아니다.
- 보고 주기는 `Settings.status_report_sec`·`rssi_frame_sec`이다. 실행 설정의 `settings`로 바꿀 수 있다.
- 실제 브로커 시나리오의 Mosquitto 실행 fixture는 `tests/iot/conftest.py`에 두고 접근 제어 테스트와 함께 쓴다.

## 7. 가정
- 시뮬레이터의 가상 `locked`는 검사 근거다. 새로운 `status` 필드로 추가하지 않는다.
- 실제 센서 능력과 시각 오차는 #140·#142의 후속 실측을 기다린다.
- S1~S4를 확정했으므로 IOT 내부 전체 연결을 실제 브로커와 CORE fake로 검증한다. CORE·DB를 합친 검증은 서버 조립(#88)이 맡는다.
- 불완전한 `unknown` 근거를 보내는 실패 시나리오는 서버의 확인 불능 취급을 검증한다. 유효한 통과로 만들지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_reconnect_announces_new_session` | 연결마다 발표와 ID를 갱신한다 |
| FR-05 | `test_FR05_empty_and_multiple_beacons` | RSSI 본문과 보고 주기를 지킨다 |
| FR-06 | 5절의 `test_FR06_*` | 영속 기록·구동 직렬화·중복 방지를 확인한다 |
| FR-07 | 5절의 `test_FR07_*` | 불명·지연·재시작·관측 불가를 확인한다 |
| FR-20 | 5절의 `test_FR20_*` | §4의 v1 근거와 검사 중복을 확인한다 |
| NFR-08 | `test_NFR08_simulator_uses_node_credentials` | 기기별 계정을 쓴다 |
| NFR-10 | `test_NFR10_eight_device_scenarios` | 두 장소와 여덟 기기 구성을 확인한다 |
| NFR-11 | `test_NFR11_simulator_v1_qos_retain` | MQTT v1과 전달 설정을 지킨다 |
| NFR-12 | `test_NFR12_simulator_local_ap` | 외부 서비스 없이 실행한다 |

기기 재시작은 같은 기록 경로로 새 시뮬레이터를 만들어 검증한다.
실제 브로커의 연결 시나리오는 CORE fake와 IOT 모듈을 쓴다. CORE·DB 구현은 서버 조립 검증이 맡는다.

| 파일 | 확인하는 것 |
|---|---|
| `server/tests/iot/test_node_sim.py` | 기기 논리·기록·설정·Paho 연결 설정. 보내는 본문은 서버의 MQTT v1 변환으로 검증한다 |
| `server/tests/iot/test_node_sim_broker.py` | 실제 Mosquitto에서 시뮬레이터·`MqttConnection`·`ReportDelivery`·`MqttCommandGateway`의 연결 |

실제 브로커 시나리오는 `test_NFR12_simulator_local_ap`, `test_FR07_delayed_or_lost_result`, `test_FR07_restart_reports_unknown_over_broker`, `test_FR04_reconnect_over_broker`, `test_NFR08_misconfigured_node_blocked`다.

2026-10-16에 새 테스트 37개(브로커 없이 32개, 실제 브로커 5개)를 확인했다. 실제 브로커 시나리오는 3번 반복해도 통과했다.
구동 중 거부·재시작의 `unknown`·측정 불능 근거·IR 무드등의 `unknown`을 하나씩 바꾸면 각각 테스트가 실패하는 것도 확인했다.
`server/`에서 실제 Mosquitto 2.1.2를 지정한 전체 `pytest`: 1,995개 통과, 기존 예상 실패 1개다. 브로커가 없으면 실제 브로커 테스트 17개만 건너뛴다.
실제 브로커 fixture는 `tests/iot/conftest.py`로 옮겼고 접근 제어 테스트도 같은 fixture를 쓴다.
