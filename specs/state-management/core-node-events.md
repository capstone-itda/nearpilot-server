# 기기 이벤트 수신

- 담당: JM-220 (상태 관리)
- 요구사항: FR-04, FR-05, FR-07, FR-13, NFR-04
- 따르는 결정: #18, #31, #32, #38, #83, #86, #88, 2026-10-26 센서 기반 물리 검증 제외 (#196), 2026-10-29 DB에 없는 명령의 결과

## 1. 왜

- IOT는 검증한 기기 보고를 `NodeEventSink`로 CORE에 넘긴다 (architecture.md §4 "기기 보고의 인계").
- 저장소에는 `NodeEventSink`의 구현이 없다.
- 구현이 없으면 아래 문제가 생긴다.
  - 발견한 기기와 기기 연결 ID를 저장하지 못한다 (FR-04).
  - RSSI 관측을 저장하지 못하고 근접 추정에 넘기지 못한다 (FR-05, FR-16).
  - `last_seen_at`을 갱신하지 못해서 모든 기기가 오프라인이 된다 (FR-13).
  - 센서 이상을 받아도 기기를 격리하지 못한다 (NFR-04).

## 2. 무엇

- 입력: `NodeEventSink`의 네 호출 (`on_announce`, `on_rssi`, `on_status`, `on_result`). 반납 검사 결과의 인계는 2026-10-26 결정으로 없앴다.
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
  - 과거 명령의 결과로 현재 온라인 시각과 명령 처리 상태를 갱신하지 않는다.
  - 실행 결과는 `core-execution`, 상태 보고는 `core-restore`, `core-shared-light`, `core-fault-recovery`로 넘긴다.
  - 서버 내부 요청의 실행 결과도 같은 경로로 넘긴다 (#86).

## 3. 제약

- 발표는 `upsert_announced_node()`로 저장한다. 발표는 신뢰 부여가 아니다 (FR-04).
- retained 발표만으로 현재 기기 연결 ID와 `last_seen_at`을 바꾸지 않는다 (FR-13, architecture.md §5 MQTT 6절).
- 현재 연결의 `status`를 받으면 `set_node_session()`과 `touch_node()`를 부른다. 시각은 서버 수신 시각이다 (FR-04, FR-13).
- 현재 연결의 RSSI 프레임도 `touch_node()`를 부른다. 유효한 기기 메시지의 마지막 수신이 온라인의 기준이다 (requirements.md §1.2 `online_ttl_sec`).
- `on_result()`는 `touch_node()`를 부르지 않는다. 과거 결과는 현재 온라인 시각의 근거가 아니다 (FR-13).
- RSSI는 `save_rssi()`로 저장한다. 등록하지 않은 비콘의 거부는 DB가 한다 (FR-05).
- RSSI는 `ProximityEstimator.observe(frame, node=NodeInfo)`로 넘긴다. `NodeInfo`는 `Repository.nodes()`로 조회한 값이다 (architecture.md §4 "근접 사후확률의 범위").
- RSSI 저장과 근접 추정 전달은 별개다. 근접 추정이 예외를 던져도 저장한 관측은 그대로 둔다.
- RSSI는 `core-shared-light`의 이탈 판단에도 넘긴다 (architecture.md §4 "이탈 판단의 관측 계약").
- `status`는 명령 처리 상태(`command_state`)만 담는다. 센서 이상으로 격리하지 않는다 (2026-10-26 결정). 기기 격리는 점유 중 발표 변경과 명령 결과가 만든다. 점유 세션과 승인 사용분은 바꾸지 않는다 (architecture.md §4 "예약 이후 저장").
- 격리와 복구 건 저장은 별개다. `ALREADY_APPLIED`만으로 복구 건의 저장 완료를 추정하지 않는다 (architecture.md §4 "복구 건과 복구 정산").
- 복구 건 저장에 실패하면 재인계에서도 같은 장애의 저장을 계속한다. `core-fault-recovery`가 같은 복구 ID로 저장 여부를 확인한다 (NFR-04).
- `command_state=busy`는 이상이 아니다. 기기 상태 `ACTIVE`와 구분한다 (FR-19).
- 일반 `status`는 반납 종료의 근거가 아니다. 반납은 `core-release`가 논리 조건으로 결정한다 (FR-07, FR-20).
- 기기별 관측, 장애 반영, 복구는 같은 기기 잠금으로 직렬화한다 (architecture.md §4 "복구 준비 조회와 복구 승인").
- 저장 함수가 `StoreError`를 던지면 예외를 다시 던진다. 인계 실패를 성공으로 처리하지 않는다 (architecture.md §4 "기기 보고의 인계").
- 실행 결과의 원래 문맥은 `context_for(command_id)`로 읽는다. 서버 조립은 `ExecutionStore.execution_context`를 연결한다 (#83). 조회가 실패하면 인계 실패로 알린다. 문맥이 없으면 DB에 없는 명령이다. `command_id`와 함께 경고를 기록하고 인계를 끝낸다 (architecture.md §4 "기기 보고의 인계").
- 서버 내부 요청의 문맥은 `request`가 없다. 저장한 명령의 동작은 `internal_request.action`과 대조한다. 성공한 내부 꺼짐은 `core-shared-light`에 넘겨 유지 대상자를 비운다 (architecture.md §4 "서버 내부 요청").

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
- 반납의 처리 (`core-release`)
- 공용 조명의 이탈 판단 (`core-shared-light`)
- 보고의 형식, 연결, 순번, 최신성 검사 (IOT 담당의 별도 작업)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 처음 보는 기기가 발표한다 | `upsert_announced_node()`를 부른다. 신뢰 상태를 바꾸지 않는다 | `test_FR04_announce_upserts_node` |
| 신뢰한 기기가 다른 기능으로 발표한다 | `upsert_announced_node()`를 부른다. 신뢰 되돌림은 DB가 한다 | `test_FR04_changed_announce_forwarded` |
| 진행 중인 점유가 있는 기기가 다른 기능으로 발표한다 | 점유를 보존하고 먼저 격리한다. 복구 건 저장 뒤 DB의 신뢰 되돌림을 적용한다 | `test_FR04_changed_announce_during_occupancy` |
| 발표만 오고 `status`가 없다 | `touch_node()`와 `set_node_session()`을 부르지 않는다 | `test_FR13_announce_does_not_touch` |
| `status`가 어떤 명령 처리 상태여도 | 격리하지 않는다. 점유를 바꾸지 않는다 | `test_NFR04_status_never_isolates` |
| 이미 `FAULT`인 기기가 `sensor_fault`를 다시 보고한다 | 같은 장애의 복구 건 저장을 확인한다. 저장이 끝났으면 같은 복구 건을 유지한다 | `test_NFR04_repeat_fault_same_recovery` |
| 격리는 성공했지만 복구 건 저장에 실패했다 | 재인계에서 같은 복구 ID로 저장을 재시도한다. `ALREADY_APPLIED`를 이유로 생략하지 않는다 | `test_NFR04_retry_missing_recovery_after_isolation` |
| `command_state`가 `busy`다 | 격리하지 않는다 | `test_FR19_busy_actuator_not_fault` |
| 결과 대기가 끝난 뒤 `on_result()`가 온다 | `touch_node()`를 부르지 않는다. `core-execution`으로 넘긴다 | `test_FR13_result_does_not_touch` |
| `save_rssi()`가 `StoreNotApplied`를 던진다 | 예외를 다시 던진다 | `test_FR05_rssi_store_error_propagates` |
| `observe()`가 예외를 던진다 | 저장한 관측을 유지한다. 실패를 기록한다 | `test_FR05_observe_error_keeps_saved_rssi` |
| 같은 기기의 `status`와 복구 승인이 동시에 온다 | 같은 기기 잠금으로 하나씩 처리한다 | `test_NFR04_node_events_serialized` |
| 서버 내부 꺼짐의 결과가 늦게 온다 | 원래 문맥으로 실행 서비스에 넘긴다. 성공이면 공용 조명에 넘긴다 | `test_FR20_internal_off_result_routed` |
| 기기가 DB에 없는 명령의 결과를 다시 보고한다 | 경고를 기록하고 인계를 끝낸다. 명령과 기기 상태를 바꾸지 않는다 | `test_FR07_unknown_command_result_is_dropped` |
| 원래 문맥 조회가 `StoreError`를 던진다 | 예외를 다시 던진다. IOT가 다시 인계한다 | `test_FR07_context_lookup_error_propagates` |

## 6. 결정

- `node_events.py`에 `NodeEventService`를 둔다 — 담당자가 2026-10-15에 추천 구성을 선택했다.
- 기존 실행·반납·복원·복구·공용 조명 서비스와 공통 기기 재진입 잠금을 주입한다 — 기존 상태 관리 접점을 실제로 연결한다.
- 점유 중 기능이나 점유 방식을 바꾼 발표는 추가 격리한다 — 담당자가 2026-10-15에 선택했다. DB의 신뢰 되돌림과 점유 보존은 유지한다.
- 점유 중 발표 변경은 격리와 복구 건 저장 뒤 발표를 저장한다 — 발표 저장 직후 재시작해도 변경 전 점유의 연결을 보존한다.
- 장애 연결 조회와 온라인 저장보다 격리를 먼저 시도한다 — 부가 저장의 실패가 이미 확인한 이상의 격리를 생략하지 않는다.
- 센서 이상은 격리 저장을 확인한 뒤 복구 건을 저장한다 — 격리의 `ALREADY_APPLIED`도 복구 건 확인을 생략하지 않는다.
- `status`의 현재 연결과 온라인 시각을 저장한 뒤 복원과 공용 조명에 넘긴다 — 일반 관측으로 반납을 종료하지 않는다.
- RSSI는 앵커 잠금 안에서 저장하고 근접 추정에 넘긴다 — 다른 조명의 잠금을 잡는 이탈 관측은 앵커 잠금 밖에서 넘긴다.
- 근접 추정 실패도 이탈 관측의 인계를 시도한다 — 실패를 로그에 기록하고 원래 예외를 다시 던진다. IOT의 재인계로 재시도한다.
- RSSI 인계는 저장·근접 관측·이탈 관측의 완료를 따로 기억한다 — 재인계는 실패한 소비자만 다시 호출한다. 완료한 프레임은 비콘별 마지막 순번만 남긴다.
- 실행 결과의 원래 문맥 조회를 필수 함수로 주입한다 — 명령·요청·기기 연결을 저장과 대조한다. 현재 설정으로 원래 문맥을 만들지 않는다.
- 결과 인계 뒤 저장한 성공만 공용 조명에 넘긴다 — `execute()`를 다시 호출하지 않는다.
- DB에 없는 명령의 결과는 경고를 기록하고 정상 반환한다 — 담당자가 2026-10-29에 선택했다. 기기는 DB 교체 전의 명령 기록을 재부팅·재접속 때 다시 보고한다. 재인계해도 결과가 같아서 재시도가 끝나지 않았다. `shared` 접점과 IOT는 바꾸지 않는다.

## 7. 가정

- #88 A안: 상태 관리가 CORE 쪽 `NodeEventSink`를 구현하고 서버 조립에서 연결한다. 서버 조립은 이 spec에 넣지 않는다.
- #83 A안: `ExecutionStore.execution_context(command_id)`가 원래 문맥을 반환한다. 서버 조립은 이 함수를 `context_for`에 연결한다.
- 관측의 형식과 최신성 검사는 IOT의 계약을 따른다. 이 spec은 검증을 마친 보고를 받는다. 시각 오차는 근접 추정 담당에게 맡기며 이 작업에서 결정하지 않는다.
- 실행 결과는 `core-execution`, 복구 건은 `core-fault-recovery`, 이탈 관측은 `core-shared-light`를 쓴다. 반납 서비스는 이 서비스에 주입하지 않는다. CORE 내부 접점은 실제 서비스로 연결했다.
- IOT가 현재 연결과 최신성을 검증한다. 이 서비스는 같은 보고의 재인계도 받는다. 재시작 뒤 RSSI의 중복 저장은 DB가 막는다. 근접 추정은 메모리 관측을 새로 시작한다.
- CORE 내부 서비스 연결은 실제 구현으로 검증한다. DB·근접 추정·IOT는 `shared` 계약의 fake로 대신한다. 실제 MQTT 연결은 이 spec에 넣지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_status_sets_node_session` | 현재 연결의 `status`로 기기 연결 ID를 저장한다 |
| FR-05 | `test_FR05_rssi_saved_and_observed` | RSSI를 저장하고 조회한 `NodeInfo`와 함께 근접 추정에 넘긴다 |
| FR-07 | `test_FR07_routes_result_to_real_execution` | 실행 결과를 실제 실행 서비스에 넘긴다. 같은 사건은 한 번만 정산한다 |
| FR-07 | `test_FR07_status_does_not_change_logical_release` | 상태 보고는 논리적으로 끝난 반납을 바꾸지 않는다 |
| FR-20 | `test_FR20_sink_has_no_release_check_handover` | 반납 검사 결과의 인계 접점이 없다 |
| FR-13 | `test_FR13_status_touches_with_received_at` | `last_seen_at`을 서버 수신 시각으로 갱신한다 |
| NFR-04 | `test_NFR04_status_never_isolates` | 상태 보고로 격리하지 않는다 |
| NFR-04 | `test_NFR04_retry_missing_recovery_after_isolation` | 격리 뒤 실패한 복구 건 저장을 같은 ID로 재시도한다 |
| NFR-04 | `test_NFR04_link_query_failure_still_isolates_known_fault` | 장애 연결 조회가 실패해도 기기를 먼저 격리한다 |
| NFR-04 | `test_NFR04_online_write_failure_does_not_skip_sensor_isolation` | 온라인 저장이 실패해도 격리와 복구 건을 먼저 저장한다. 재인계는 복구 건을 중복하지 않는다 |
| FR-04 | `test_FR04_announce_write_failure_keeps_changed_occupied_node_isolated` | 발표 저장 실패와 새 수신 서비스의 재인계에서도 격리와 점유를 보존한다 |
| FR-05 | `test_FR05_failed_light_handover_retries_only_missing_consumer` | 성공한 근접 관측을 중복 적용하지 않고 이탈 관측만 재시도한다 |
| FR-05 | `test_FR05_anchor_lock_released_before_light_observation` | 이탈 관측이 앵커 잠금을 다시 기다리는 교착을 막는다 |
| FR-07 | `test_FR07_unknown_command_result_is_dropped` | DB에 없는 명령의 결과는 `command_id`를 경고로 남기고 인계를 끝낸다 |
| FR-07 | `test_FR07_context_lookup_error_propagates` | 원래 문맥의 조회 실패는 전달 완료로 보지 않는다 |
| FR-07 | `test_FR07_reports_during_gateway_wait_do_not_deadlock` | 실행 서비스의 전송 대기 중 다른 스레드의 결과 인계를 받는다 |
| FR-13 | `test_FR13_result_does_not_touch` | 과거 결과로 현재 연결·온라인 시각·물리 관측을 갱신하지 않는다 |
| FR-20 | `test_FR20_internal_off_result_routed` | 서버 내부 꺼짐의 결과를 실행 서비스와 공용 조명에 넘긴다 |

- 실제 실행·반납·복원·복구·공용 조명 서비스를 조립해서 CORE 내부 연결을 검증한다.
- 기기 형식 검증, 실제 MQTT와 서버 실행 진입점은 이 검증에 넣지 않는다.
