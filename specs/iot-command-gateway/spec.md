# 명령 전송·결과 대기와 반납 검사 요청

- 담당: 이 작업을 지시한 담당자 (상태 관리의 IOT 구현 범위)
- 구현 브랜치: `feat/iot-command-gateway`
- 요구사항: FR-06, FR-07, FR-20, NFR-08
- 따르는 결정: architecture.md에 반영한 #17, #19, #37, #38, #140~#143이다.
- 상태: 구현 전 초안이다. 메시지·연결·보고 인계 모듈을 사용한다.

## 1. 왜

CORE는 발행 확인과 물리 실행 결과를 구분해야 한다.
확인하지 못한 결과를 미전송으로 바꾸면 승인 사용분과 점유를 잘못 정리할 수 있다.

## 2. 무엇

- 입력: `send(NodeCommand, timeout_sec)`와 `request_check(SafetyCheckRequest)`다.
- 입력: 보고 검증 모듈이 확인한 `NodeResult`와 연결 끊김이다.
- 출력: `cmd`·`safety_check` 발행과 `CommandResult`다. 검사 요청은 결과를 기다리지 않는다.
- 바뀌는 상태: 명령별 전송 시도·대기 상태와 검사 요청 연결의 메모리다.
- 약속:
  - CORE가 넘긴 ID·대상 연결·동작·기준·유효 시간을 보존한다.
  - 명령 대기와 CORE 인계는 같은 사건 ID의 보고를 쓴다.
  - 새 명령 ID로 자동 재시도하지 않는다.
  - 결과 대기가 끝나도 구독과 늦은 결과 인계를 유지한다.

## 3. 제약

- 명령 발행은 CORE의 인계에서만 시작한다. 게이트웨이는 실행 허가를 새로 판단하지 않는다 (FR-06).
- `open`은 사물함, `on`·`off`는 조명·스피커의 계약을 따른다 (architecture.md §5 MQTT 3절).
- 재전송은 같은 명령 ID와 내용을 쓴다. 만료 시각을 현재 시각으로 연장하지 않는다 (FR-06).
- 발행 전 결과 대기를 등록한다. 빠른 실행 결과를 대기 등록 사이에서 잃지 않는다 (FR-07).
- 발행하지 않았음을 증명할 때만 `NOT_SENT`다. 발행 시도 뒤 불확실한 오류·단절·기한 초과는 `UNKNOWN`이다 (FR-07).
- PUBACK·`accepted`·`running`·일반 상태 보고를 성공으로 보지 않는다. 진행 보고로 응답 기한을 연장하지 않는다 (FR-07).
- `succeeded`·`failed`·`unknown`은 대응하는 `CommandOutcome`으로 변환한다 (FR-07).
- 검증한 `DEVICE_BUSY` 거부만 `REJECTED`다. 다른 거부는 `FAILED`로 반환한다 (FR-07, shared `NodeCommandGateway`).
- `NodeResult.state`와 `error_code`는 바꾸지 않는다. 서버 결과에는 `report`가 없다 (FR-07).
- 기기 결과는 `CommandResult.report`에 원래 모델을 담는다. CORE 이벤트에도 같은 `event_id`를 쓴다 (FR-07).
- 명령 대기는 기기 ID와 명령 ID가 맞는 보고만 따른다. 다른 결과로 다른 명령을 끝내지 않는다 (FR-07).
- 상충 결과를 임의의 성공으로 합치지 않는다. 각 원래 보고를 CORE 정산 경로에 남긴다 (FR-07).
- `request_check()`는 결과를 기다리지 않는다. 응답 기한·재검사·확인 불능 타이머는 CORE가 맡는다 (FR-20).
- 검사 요청의 재전송은 내용을 보존한다. 새 측정에는 CORE가 발급한 새 검사 ID를 쓴다 (FR-20).
- 송신은 허용한 필드만 쓴다. 사용자 ID·요청자·승인·점유 정보를 넣지 않는다 (NFR-08).
- 시간 값은 CORE가 전달한 적용 설정과 제한 시간을 따른다. 별도 명령 만료 기본값을 만들지 않는다 (requirements.md §1.2).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/mqtt/`의 명령·대기·검사 요청 파일이다.
- `server/tests/iot/`의 명령 전송·대기 테스트다.

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`의 Gotchas 밖, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 이 작업: 형식 검증·보고 재인계·브로커 접근 제어와 CORE·DB·API·펌웨어다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeCommandGateway.send` | 제공한다 | CORE |
| `SafetyCheckGateway.request_check` | 제공한다 | CORE |
| `NodeCommand`, `CommandResult`, `NodeResult` | 사용한다 | shared |
| `SafetyCheckRequest` | 사용한다 | shared |

### 만들지 않는 것

- 실행 재검사·명령 ID 발급·DB의 전송 시작과 결과 저장이다.
- 반납 기준 충족·점유 해제·복구와 새 명령 ID 재시도다.
- 보고 본문·최신성 검증과 CORE 재인계다. 앞 spec의 결과를 사용한다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| CORE 명령 호출이 없다 | `cmd`를 발행하지 않는다 | `test_FR06_no_publish_without_core_call` |
| 같은 ID·내용을 동시에 호출한다 | 대기를 공유한다. 새 물리 명령으로 만들지 않는다 | `test_FR06_concurrent_same_command` |
| 같은 ID의 내용이 다르다 | 기존 내용과 대기를 보존하고 발행하지 않는다 | `test_FR06_changed_command_not_republished` |
| 발행 전에 전송 불가를 확인했다 | 미전송이 확실하면 `NOT_SENT`다 | `test_FR06_definite_not_sent_before_publish` |
| 발행 시도 뒤 끊기거나 기한이 지났다 | `UNKNOWN`이다 | `test_FR07_disconnect_timeout_unknown` |
| PUBACK·접수·구동 시작만 왔다 | 완료로 반환하지 않는다 | `test_FR07_progress_not_success` |
| 발행 직후 결과가 왔다 | 등록한 대기를 깨운다 | `test_FR07_fast_result_not_lost` |
| 기기 ID나 명령 ID가 다르다 | 대기 중인 다른 명령을 끝내지 않는다 | `test_FR07_result_correlation` |
| `DEVICE_BUSY` 또는 다른 거부가 왔다 | 결과 변환과 원래 보고를 보존한다 | `test_FR07_rejection_mapping_preserves_report` |
| CORE 인계가 기기 잠금을 기다린다 | `send()` 대기는 독립적으로 끝날 수 있다 | `test_FR07_wait_and_sink_no_deadlock` |
| 대기 뒤 늦은 결과가 왔다 | 보고 인계 경로가 계속 받는다 | `test_FR07_late_result_subscription_remains` |
| 검사 요청 발행이 실패했다 | 실패를 호출자에게 알린다. 자체 재검사를 만들지 않는다 | `test_FR20_check_send_failure_not_passed` |
| 검사 요청을 정상 발행했다 | 결과를 기다리지 않고 반환한다 | `test_FR20_request_check_does_not_wait` |

## 6. 결정

- 명령 대기와 CORE 인계를 분리한다 — 담당자가 2026-10-15에 선택했다.
- 보고 수신이 먼저 명령 대기를 깨울 수 있게 한다 — 실행 서비스의 잠금과 교착하지 않게 한다.
- 서버가 만든 결과와 기기 결과를 다른 경로로 만든다 — 서버 결과에 기기 사건 ID를 붙이지 않는다.

## 7. 가정

- 실제 기기의 검증 전 실행은 #140에 따라 차단한다. 시각 검증 근거의 주입은 서버 조립 계약에서 연결한다.
- 연결 준비·종료 접점은 S3, 메모리 재인계 포화 대응은 S4가 미결이다.
- CORE·DB는 같은 사건의 중복 정산을 막는다. 게이트웨이의 메모리 중복 목록은 저장 완료의 증거가 아니다.
- 검사 충돌과 불완전한 근거는 #143 A안을 따른다. 전용 오류 결과를 추가하지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-06 | 5절의 `test_FR06_*` | CORE 인계·동일 ID·미전송 근거를 확인한다 |
| FR-07 | 5절의 `test_FR07_*` | 결과 변환·상관관계·늦은 결과·교착을 확인한다 |
| FR-20 | 5절의 `test_FR20_*` | 검사 결과를 기다리지 않고 실패를 알린다 |
| NFR-08 | `test_NFR08_command_and_check_payload_minimal` | 송신 본문에 내부 계정 정보를 넣지 않는다 |

전송·시계·CORE 인계는 fake를 쓴다. 명령의 실제 물리 중복 방지는 시뮬레이터 spec이 검증한다.
