# 가상 ESP32 기기 시뮬레이터

- 상태: 작성 중
- 담당: JM-220 (상태 관리)
- 요구사항: FR-04, FR-05, FR-06, FR-07
- 따르는 결정: #19, #37, #38

## 1. 왜

- 펌웨어 담당과 실제 기기 검증은 미결이다 (architecture.md §2, §5 MQTT 7절).
- 실제 기기가 없으면 서버의 실행, 반납, 장애 경로를 끝까지 시험할 수 없다.
- MQTT 계약 v1은 서버와 시뮬레이터의 개발 기준이다 (FR-07). 시뮬레이터는 계약의 기기 쪽을 구현한다.
- 지표 3, 4, 6의 부하 시험과 장애 시험에도 가상 기기가 필요하다 (requirements.md §4).

## 2. 무엇

- 입력:
  - 시험 코드가 정한 기기 구성: `node_id`, 기능, 점유 방식, 브로커 접속 설정
  - 시험 코드가 넣는 물리 관측과 장애: 문 닫힘, 조명 켜짐, 구동부 상태, 센서 이상, 응답 지연, 응답 유실, 재시작
  - 시험 코드가 넣는 비콘과 RSSI 값
  - 브로커에서 받은 `cmd`, `safety_check`
- 출력: 기기 → 서버 보고 (`announce`, `rssi`, `status`, `result`, `safety_check_result`)
- 바뀌는 상태: 시뮬레이터 메모리의 기기 연결 ID, 토픽별 순번, 명령 기록, 검사 기록, 가상 물리 상태
- 약속:
  - 시뮬레이터는 architecture.md §5 MQTT 4절의 명령 접수 순서를 따른다.
  - 같은 `command_id`의 재전송에 물리 동작을 다시 시작하지 않는다. 저장한 결과를 다시 보고한다.
  - 다른 구동 명령이 진행 중이면 새 명령을 대기열에 넣지 않고 `DEVICE_BUSY`로 거부한다.
  - 반납 검사 요청을 받으면 그때의 가상 물리 상태로 새로 검사한다.
  - 시험 코드는 장애를 넣어서 불명, 실패, 무응답 경로를 만들 수 있다.

## 3. 제약

- 페이로드는 `iot/messages`의 스키마로 만들고 읽는다. 시뮬레이터 안에 스키마를 다시 쓰지 않는다.
- 시뮬레이터는 서버와 같은 PC의 UTC 시계를 쓴다 (architecture.md §5 MQTT 6절).
- 접속할 때마다 새 기기 연결 ID를 만든다. 재접속과 재시작에도 새 값을 만든다 (§5 MQTT 2절).
- 순번은 연결과 토픽마다 1부터 증가한다 (§5 MQTT 2절).
- `announce`는 QoS 1, retained로 연결마다 발행한다 (§5 MQTT 1절, FR-04).
- RSSI는 약 1초 프레임으로 보고한다. 주기는 서버 `Settings.rssi_frame_sec`를 받는다 (FR-05).
- `status`는 `Settings.status_report_sec` 간격으로 보고한다 (§5 MQTT 6절).
- 명령 접수 순서 (§5 MQTT 4절):
  1. 같은 `command_id`의 기록을 먼저 조회한다.
  2. 기록이 있고 내용이 같으면 저장한 결과를 보고한다.
  3. 기록이 있고 내용이 다르면 `COMMAND_ID_CONFLICT`로 거부한다.
  4. 다른 구동 명령이 진행 중이면 `DEVICE_BUSY`로 거부한다.
  5. 버전, 대상 연결, 기능별 동작, 유효 시간을 검사한다.
  6. 접수 기록을 저장한 뒤 물리 동작을 시작한다.
- 물리 동작 시작 직전에 대상 연결과 유효 시간을 다시 검사한다 (§5 MQTT 4절).
- 거부한 명령의 ID, 내용, 결과도 명령 기록에 보존한다 (§5 MQTT 4절).
- 시뮬레이터 재시작은 명령 기록을 보존한다. 접수 기록만 있고 결과가 없으면 `unknown`을 보고한다. 동작을 재개하지 않는다 (§5 MQTT 4절).
- 결과를 다시 보고할 때 `event_id`, 상태 값, 발생 시각을 유지한다. 순번과 발행 시각은 새로 부여한다 (§5 MQTT 3절).
- 반납 검사의 통과 조건은 §5 MQTT 7절의 표를 따른다. 필수 관측이 `null` 또는 `unknown`이면 `passed`를 보고하지 않는다.
- 같은 `check_id`의 재전송에는 기존 검사 결과를 보고한다 (architecture.md §4 "반납 검사와 종료").
- `status`에 `safe` 필드를 넣지 않는다 (§5 MQTT 3절).

## 4. 경계

### 고치는 곳
- `server/tools/node_sim/`
- `server/tests/tools/node_sim/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/src/nearpilot/` 전체, `tools/data_collection/`, `tools/evaluation/`, `firmware/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| MQTT 계약 v1 기기 쪽 (architecture.md §5) | 제공 | 서버 IOT |
| `iot/messages` 스키마 | 사용 | 상태 관리 (`iot-mqtt-messages`) |
| `Settings` | 사용 (주기 값) | 공동 |

### 만들지 않는 것
- 실제 펌웨어 (`firmware/`)
- 근접 모델용 RSSI 생성 모델. 시뮬레이터는 시험 코드가 넣은 RSSI 값을 그대로 보고한다.
- 같은 `check_id`에 다른 내용이 온 충돌 보고. 보고 형식이 미결이다 (7절).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 같은 `command_id`, 같은 내용이 다시 온다 | 저장한 결과를 다시 보고한다. 구동 횟수는 그대로다 | `test_FR06_sim_duplicate_command_not_rerun` |
| 같은 `command_id`, 다른 내용이 온다 | `COMMAND_ID_CONFLICT`로 거부한다. 기존 기록을 보존한다 | `test_FR06_sim_command_id_conflict` |
| 구동 중에 다른 새 명령이 온다 | `rejected`와 `DEVICE_BUSY`를 보고한다. 대기열에 넣지 않는다 | `test_FR06_sim_device_busy_no_queue` |
| `target_session_id`가 현재 연결과 다르다 | `SESSION_MISMATCH`로 거부한다 | `test_FR06_sim_session_mismatch` |
| `expires_at`이 지났다 | `COMMAND_EXPIRED`로 거부한다 | `test_FR06_sim_command_expired` |
| 오래된 중복 명령이 온다 | 연결과 만료 검사보다 먼저 기존 결과를 보고한다 | `test_FR06_sim_old_duplicate_returns_record` |
| 조명에 `open`이 온다 | `UNSUPPORTED_ACTION`으로 거부한다 | `test_FR06_sim_unsupported_action` |
| 시험 코드가 결과 유실을 넣었다 | 결과를 발행하지 않는다 | `test_FR07_sim_drop_result` |
| 접수 뒤 시뮬레이터를 재시작한다 | 새 연결 ID로 `unknown`을 보고한다. 동작을 재개하지 않는다 | `test_FR07_sim_restart_reports_unknown` |
| 문이 열린 상태에서 반납 검사가 온다 | `not_ready`를 보고한다 | `test_FR07_sim_safety_not_ready` |
| `door_closed`가 `null`이다 | `passed`를 보고하지 않는다 | `test_FR07_sim_safety_null_not_passed` |
| 같은 `check_id`가 다시 온다 | 기존 검사 결과를 보고한다 | `test_FR07_sim_safety_same_check_id` |

## 6. 결정

- 시뮬레이터 테스트는 `server/tests/tools/node_sim/`에 둔다 — architecture.md §2는 테스트를 `tests/` 아래 같은 이름의 폴더에 두라고 한다. 도구 경로를 그대로 옮긴다.

## 7. 가정

- 미결: 펌웨어 담당과 실제 기기 검증 (architecture.md §5 MQTT 7절). 시뮬레이터의 통과 조건은 실제 센서 검증 결과가 아니다.
- 미결: 같은 `check_id`에 다른 내용이 온 충돌을 보고하는 형식. 시뮬레이터는 이 경우 결과를 보고하지 않는다. 서버는 응답 기한 초과로 다룬다 (architecture.md §4 "반납 검사와 종료").
- 미결: `evidence`의 필수 항목. 시뮬레이터는 검사에 쓴 `physical_state` 네 필드를 `evidence`에 담는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_sim_announce_on_connect` | 연결마다 retained 발표와 새 연결 ID를 발행한다 |
| FR-05 | `test_FR05_sim_rssi_frame` | 넣은 비콘과 RSSI를 프레임으로 보고하고 순번을 올린다 |
| FR-06 | `test_FR06_sim_accept_then_succeed` | `accepted`, `running`, `succeeded`를 같은 `command_id`로 보고한다 |
| FR-07 | `test_FR07_sim_safety_passed` | §5 MQTT 7절 조건을 만족하면 `passed`를 보고한다 |
