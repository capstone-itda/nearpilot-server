# 기기 이벤트 수신

- 상태: 작성 중
- 담당: JM-220 (상태 관리)
- 요구사항: FR-04, FR-05, FR-07, FR-13, NFR-04
- 따르는 결정: #18, #31, #32, #38

## 1. 왜

- IOT는 검증한 기기 보고를 `NodeEventSink`로 CORE에 넘긴다 (architecture.md §4 "기기 보고의 인계").
- 저장소에는 `NodeEventSink`의 구현이 없다.
- 구현이 없으면 아래 문제가 생긴다.
  - 발견한 기기와 기기 연결 ID를 저장하지 못한다 (FR-04).
  - RSSI 관측을 저장하지 못하고 근접 추정에 넘기지 못한다 (FR-05, FR-16).
  - `last_seen_at`을 갱신하지 못해서 모든 기기가 오프라인이 된다 (FR-13).
  - 센서 이상을 받아도 기기를 격리하지 못한다 (NFR-04).

## 2. 무엇

- 입력: `NodeEventSink`의 다섯 호출 (`on_announce`, `on_rssi`, `on_status`, `on_result`, `on_safety_check_result`)
- 출력: 없음. 반영에 실패하면 예외를 던진다. IOT가 다시 인계한다.
- 바뀌는 상태:
  - 기기 발표, 신뢰 되돌림, 현재 기기 연결 ID, `last_seen_at` (`Repository`)
  - RSSI 관측 (`Repository.save_rssi`)
  - 근접 추정의 HMM 상태 (`ProximityEstimator.observe`)
  - 센서 이상이면 기기 `FAULT`와 새 복구 건 (`ExecutionStore.isolate_device`, `core-fault-recovery`)
  - 상태 관리 메모리의 기기별 최신 물리 관측
- 약속:
  - 기기별 관측과 장애 반영을 한 번에 하나씩 순서대로 한다.
  - 같은 보고를 다시 받아도 결과가 같다.
  - 과거 명령의 결과로 현재 온라인 시각과 안전 상태를 갱신하지 않는다.
  - 실행 결과는 `core-execution`, 반납 검사 결과는 `core-release`로 넘긴다.

## 3. 제약

- 발표는 `upsert_announced_node()`로 저장한다. 발표는 신뢰 부여가 아니다 (FR-04).
- retained 발표만으로 현재 기기 연결 ID와 `last_seen_at`을 바꾸지 않는다 (FR-13, architecture.md §5 MQTT 6절).
- 현재 연결의 `status`를 받으면 `set_node_session()`과 `touch_node()`를 부른다. 시각은 서버 수신 시각이다 (FR-04, FR-13).
- 현재 연결의 RSSI 프레임도 `touch_node()`를 부른다. 유효한 기기 메시지의 마지막 수신이 온라인의 기준이다 (requirements.md §1.2 `online_ttl_sec`).
- `on_result()`와 `on_safety_check_result()`는 `touch_node()`를 부르지 않는다. 과거 결과는 현재 온라인 시각의 근거가 아니다 (FR-13).
- RSSI는 `save_rssi()`로 저장한다. 등록하지 않은 비콘의 거부는 DB가 한다 (FR-05).
- RSSI는 `ProximityEstimator.observe(frame, node=NodeInfo)`로 넘긴다. `NodeInfo`는 `Repository.nodes()`로 조회한 값이다 (architecture.md §4 "근접 사후확률의 범위").
- RSSI 저장과 근접 추정 전달은 별개다. 근접 추정이 예외를 던져도 저장한 관측은 그대로 둔다.
- RSSI는 `core-shared-light`의 이탈 판단에도 넘긴다 (architecture.md §4 "이탈 판단의 관측 계약").
- `status`의 `sensor_fault`가 `true`이면 `isolate_device()`로 격리하고 새 복구 건을 만든다. 점유 세션과 승인 사용분은 바꾸지 않는다 (architecture.md §4 "예약 이후 저장").
- 격리와 복구 건 저장은 별개다. `ALREADY_APPLIED`만으로 복구 건의 저장 완료를 추정하지 않는다 (architecture.md §4 "복구 건과 복구 정산").
- 복구 건 저장에 실패하면 재인계에서도 같은 장애의 저장을 계속한다. `core-fault-recovery`가 같은 복구 ID로 저장 여부를 확인한다 (NFR-04).
- `actuator_state=busy`는 이상이 아니다. 기기 상태 `ACTIVE`와 구분한다 (FR-19).
- 일반 `status`는 종료 안전의 근거가 아니다. 반납 종료는 반납 검사 결과로만 결정한다 (FR-07).
- 기기별 관측, 장애 반영, 복구는 같은 기기 잠금으로 직렬화한다 (architecture.md §4 "복구 준비 조회와 복구 승인").
- 저장 함수가 `StoreError`를 던지면 예외를 다시 던진다. 인계 실패를 성공으로 처리하지 않는다 (architecture.md §4 "기기 보고의 인계").

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/lifecycle/`
- `server/tests/core/lifecycle/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/`, `core/proximity/`, `core/policy/`, `core/allocation/`, `db/`, `api/`, `iot/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeEventSink` | 제공 | IOT |
| `Repository.upsert_announced_node`, `set_node_session`, `touch_node`, `save_rssi`, `nodes` | 사용 | DB |
| `ExecutionStore.isolate_device` | 사용 | DB |
| `ProximityEstimator.observe` | 사용 | 근접 추정 |

### 만들지 않는 것
- 실행 결과의 정산 (`core-execution`)
- 반납 검사 결과의 처리 (`core-release`)
- 공용 조명의 이탈 판단 (`core-shared-light`)
- 보고의 형식, 연결, 순번, 최신성 검사 (`iot-mqtt-gateway`)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 처음 보는 기기가 발표한다 | `upsert_announced_node()`를 부른다. 신뢰 상태를 바꾸지 않는다 | `test_FR04_announce_upserts_node` |
| 신뢰한 기기가 다른 기능으로 발표한다 | `upsert_announced_node()`를 부른다. 신뢰 되돌림은 DB가 한다 | `test_FR04_changed_announce_forwarded` |
| 진행 중인 점유가 있는 기기가 다른 기능으로 발표한다 | DB의 신뢰 되돌림을 적용한다. 점유를 임의로 종료하지 않는다. 추가 격리 여부는 6절에서 정한다 | `test_FR04_changed_announce_during_occupancy` |
| 발표만 오고 `status`가 없다 | `touch_node()`와 `set_node_session()`을 부르지 않는다 | `test_FR13_announce_does_not_touch` |
| `status`의 `sensor_fault`가 `true`다 | `isolate_device()`를 부르고 복구 건을 만든다 | `test_NFR04_sensor_fault_isolates` |
| 이미 `FAULT`인 기기가 `sensor_fault`를 다시 보고한다 | 같은 장애의 복구 건 저장을 확인한다. 저장이 끝났으면 같은 복구 건을 유지한다 | `test_NFR04_repeat_fault_same_recovery` |
| 격리는 성공했지만 복구 건 저장에 실패했다 | 재인계에서 같은 복구 ID로 저장을 재시도한다. `ALREADY_APPLIED`를 이유로 생략하지 않는다 | `test_NFR04_retry_missing_recovery_after_isolation` |
| `actuator_state`가 `busy`다 | 격리하지 않는다 | `test_FR19_busy_actuator_not_fault` |
| 결과 대기가 끝난 뒤 `on_result()`가 온다 | `touch_node()`를 부르지 않는다. `core-execution`으로 넘긴다 | `test_FR13_result_does_not_touch` |
| `save_rssi()`가 `StoreNotApplied`를 던진다 | 예외를 다시 던진다 | `test_FR05_rssi_store_error_propagates` |
| `observe()`가 예외를 던진다 | 저장한 관측을 유지한다. 실패를 기록한다 | `test_FR05_observe_error_keeps_saved_rssi` |
| 같은 기기의 `status`와 복구 승인이 동시에 온다 | 같은 기기 잠금으로 하나씩 처리한다 | `test_NFR04_node_events_serialized` |

## 6. 결정

- 담당자 선택 대기: 점유 중 기능·점유 방식 변경 발표에 추가 격리를 적용할지 정한다. 신뢰 되돌림과 점유 보존은 기존 계약을 따른다. DB의 발표 저장 동작을 바꾸는 선택은 팀 결정이다.

## 7. 가정

- 미결: `NodeEventSink`의 구현 담당. architecture.md §4는 제공 영역을 CORE로만 쓴다. 이 spec은 IOT와 장애 반영을 맡은 상태 관리가 구현한다고 가정한다.
- 미결: 시각 오차 허용값. 관측의 최신성 검사는 `iot-mqtt-gateway`가 한다. 이 spec은 넘겨받은 보고를 신선한 보고로 본다.
- 선행 의존: 실행 결과는 `core-execution`, 반납 검사는 `core-release`, 복구 건은 `core-fault-recovery`, 이탈 관측은 `core-shared-light`를 쓴다. 연결할 내부 접점과 조립 순서는 담당자가 정한다. fake 검증만으로 실제 연결의 완료를 대신하지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_status_sets_node_session` | 현재 연결의 `status`로 기기 연결 ID를 저장한다 |
| FR-05 | `test_FR05_rssi_saved_and_observed` | RSSI를 저장하고 조회한 `NodeInfo`와 함께 근접 추정에 넘긴다 |
| FR-07 | `test_FR07_routes_result_and_safety_check` | 실행 결과와 반납 검사 결과를 담당 모듈로 넘긴다 |
| FR-13 | `test_FR13_status_touches_with_received_at` | `last_seen_at`을 서버 수신 시각으로 갱신한다 |
| NFR-04 | `test_NFR04_sensor_fault_isolates` | 센서 이상이면 점유를 보존하고 기기만 격리한다 |
