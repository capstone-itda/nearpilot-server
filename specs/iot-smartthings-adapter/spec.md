# SmartThings 명령 전송·확인과 온라인 하트비트

- 담당: 작업 지시자 (iot 영역)
- 요구사항: FR-06, FR-07, FR-13, NFR-07
- 따르는 결정: #259 (decision PR #261, #262), `specs/iot-smartthings-descriptor/spec.md`

## 1. 왜

- `iot-smartthings-descriptor`는 선언만 만든다. 서버는 아직 SmartThings 기기에 명령을 보내거나 온라인 여부를 알 방법이 없다.
- `NodeCommandGateway`와 온라인 하트비트가 있어야 판정(core)이 코드 변경 없이 이 기기를 다룰 수 있다 (NFR-07, 의존 규칙 4).

## 2. 무엇

- 입력: 판정이 인계하는 `NodeCommand`(명령)와 상태 관리가 주기마다 부르는 하트비트 호출.
- 출력: `NodeCommandGateway.send()`의 `CommandResult`(`SUCCEEDED`·`UNKNOWN`·`NOT_SENT`). 하트비트가 성공하면 `NodeEventSink.on_status()` 호출.
- 바뀌는 상태: 없음(이 작업의 코드 자체는 상태가 없다). 호출자가 `NodeEventSink`를 통해 CORE·DB 상태를 바꾼다.
- 약속: `send()`는 전송과 확인을 한 호출 안에서 끝낸다. 결과는 서버가 만들므로 `CommandResult.report`는 항상 `None`이다. `on_result`는 부르지 않는다. 시간 초과는 `UNKNOWN`이고 성공으로 보지 않는다(FR-07). 같은 `command_id`는 같은 내용으로만 재전송을 받는다(FR-06).

## 3. 제약

- `NodeCommandGateway.send(command, timeout_sec) -> CommandResult`를 구현한다. 접점은 `shared/interfaces.py`가 이미 정의하고 있고 이 작업은 그 Protocol을 고치지 않는다 (`architecture.md` §4 "기기 명령 인터페이스").
- `command_id` 중복 처리는 `MqttCommandGateway`와 같은 규칙이다. 같은 ID에 다른 내용이 오면 `ValueError`, 같은 내용이면 기존 호출에 합류한다 (FR-06, `iot/mqtt/commands.py`).
- 접수 응답만으로 성공을 확정하지 않는다. 명령을 보낸 뒤 확인 속성이 기대값과 같아질 때까지 `timeout_sec` 안에서 상태를 다시 조회한다 (FR-07).
- 명령 전송 자체가 실패하면(연결·인증 오류) `NOT_SENT`다. 전송에 성공했지만 `timeout_sec` 안에 확인하지 못하면 `UNKNOWN`이다. 이 작업은 `FAILED`를 만들지 않는다 — SmartThings 상태 조회만으로는 "확실한 실패"를 구분할 근거가 없다 (FR-07).
- `custom.launchapp`·`samsungvd.remoteControl.send`처럼 확인 속성이 없는 동작은 선언하지 않으므로(`iot-smartthings-descriptor`) 이 게이트웨이도 다루지 않는다.
- capability.command·확인 속성 매핑은 `iot-smartthings-descriptor`의 선언 생성과 같은 표를 쓴다. 두 곳에 중복해서 적지 않는다 (`iot-smartthings-descriptor` spec 결정 1).
- 온라인 하트비트는 `smartthings_poll_sec`마다 상태를 조회해서 성공하면 `NodeEventSink.on_status()`를 부른다. 실패하면 부르지 않는다 — `last_seen_at`은 유효한 응답에만 갱신된다 (FR-13).
- `CLOCK_UNVERIFIED`·`clock_skew_sec`은 적용하지 않는다. 이 어댑터는 `iot/mqtt/delivery.py`를 거치지 않는다 (`architecture.md` §4 "시각").
- `Settings`에 `smartthings_poll_sec`를 더한다. 코드 상수로 쓰지 않는다 (`AGENTS.md` "임계값과 시간 값은 코드 상수로 쓰지 않는다").
- 판정은 바꾸지 않는다 (NFR-07).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/smartthings/`
- `server/src/nearpilot/shared/config.py` — `smartthings_poll_sec` 필드만 더한다. 그 밖의 `shared` 파일은 고치지 않는다
- `server/tests/iot/`, `server/tests/shared/test_config.py`

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `db/`, `api/`, 대시보드, `runtime/assembly.py`. OAuth, REST 엔드포인트, CLI는 아래 "만들지 않는 것"을 본다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeCommandGateway.send` | 제공 | CORE(상태 관리) |
| `NodeEventSink.on_status` | 사용 | CORE |
| `iot.smartthings.descriptor`, `iot.smartthings.actions` | 사용 | 같은 iot-smartthings 모듈(`iot-smartthings-descriptor`) |
| `shared.config.Settings.smartthings_poll_sec` | 더함 | shared |

### 만들지 않는 것

- OAuth 인가 코드 교환·토큰 갱신, `.env.smartthings-token` 저장 — 후속 작업
- `POST /admin/nodes/{node_id}/smartthings/link` REST, `nearpilot smartthings link` CLI — 후속 작업. 이번 대화에서 "새 admin REST 엔드포인트" 방식으로 하기로 정했다(담당자 결정)
- `runtime/assembly.py`에 이 게이트웨이와 하트비트를 실제로 연결하는 조립 코드 — 후속 작업. 위 OAuth/REST가 없으면 조립할 토큰이 없다
- 실제 `urllib`/HTTP 기반 `SmartThingsTransport` 구현 — 이 작업은 `Protocol`만 정의하고 테스트는 fake로 한다. 실제 구현은 OAuth 작업이 토큰 주입과 함께 만든다

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 선언에 없는 동작 이름으로 `send()` 호출 | `NOT_SENT` | `test_FR06_unknown_action_not_sent` |
| 명령 전송(POST) 자체가 실패 | `NOT_SENT` | `test_FR06_post_failure_not_sent` |
| 전송 성공, 확인 속성이 기대값과 바로 같음 | `SUCCEEDED` | `test_FR07_confirmed_immediately_succeeds` |
| 전송 성공, 확인 속성이 몇 번 조회 뒤에 같아짐 | `SUCCEEDED` | `test_FR07_confirmed_after_polling_succeeds` |
| 전송 성공, `timeout_sec` 안에 끝내 확인 못함 | `UNKNOWN` | `test_FR07_timeout_without_confirmation_is_unknown` |
| 확인 조회 중 전송 오류 | 오류를 무시하고 계속 폴링, 시간 끝나면 `UNKNOWN` | `test_FR07_poll_errors_treated_as_unconfirmed` |
| 같은 `command_id`, 같은 내용 재전송 | 충돌로 보지 않고 다시 전송·확인 | `test_FR06_same_command_id_same_content_resends` |
| 같은 `command_id`, 다른 내용 | `ValueError` | `test_FR06_same_command_id_different_content_raises` |
| `timeout_sec`가 0 이하이거나 유한하지 않음 | `ValueError` | `test_FR06_invalid_timeout_raises` |
| 하트비트 조회 성공 | `on_status(IDLE)` 호출, 반환값 있음 | `test_FR13_heartbeat_success_calls_on_status` |
| 하트비트 조회 실패 | `on_status` 호출 안 함, 반환값 `None` | `test_FR13_heartbeat_failure_skips_on_status` |
| `smartthings_poll_sec`가 0 이하이거나 `online_ttl_sec`보다 큼 | `Settings` 생성 시 `ValueError` | `test_FR13_smartthings_poll_sec_validated` |

## 6. 결정

1. capability.command·확인 속성·기대값 계산 표는 `iot/smartthings/actions.py`에 `ACTIONS: dict[str, SmartThingsAction]`로 둔다. `iot-smartthings-descriptor`의 `descriptor.py`는 이 표에서 선언(`declaration`)만 꺼내 쓰도록 고친다 — 매핑을 한 곳에만 둔다(이 spec 제약 6).
2. `send()`의 명령 전송 실패와 확인 실패는 둘 다 `SmartThingsTransport`가 던지는 공통 예외(`SmartThingsTransportError`)로 구분하지 않는다. 전송 단계의 예외는 `NOT_SENT`, 확인 단계의 예외는 폴링 계속(시간이 끝나면 `UNKNOWN`)으로 다르게 처리한다 — 전송 실패는 "보냈는지 모른다"가 아니라 "확실히 못 보냈다"이고, 확인 실패는 일시적 네트워크 문제일 수 있어 재시도할 가치가 있다.
3. `command_id` 재사용의 동시 호출 합류(여러 스레드가 같은 새 `command_id`로 동시에 `send()`를 부르는 경우)는 `MqttCommandGateway`처럼 조건변수로 합류시킨다. 완료된 `command_id`의 같은 내용 재전송은 결과를 캐시하지 않고 다시 전송·확인한다 — 이 작업이 다루는 동작(`on`·`off`·`set_volume`·`mute`·`unmute`·`set_input_source`)은 모두 절대값을 지정하는 멱등 명령이라 다시 보내도 안전하다. 내용이 다른 재사용만 `ValueError`로 막는다(`_known` 사전은 완료 뒤에도 비우지 않는다).
4. 하트비트는 `switch` capability의 상태 조회 성공 여부로만 온라인을 판단한다. 조회에 성공하면 `command_state=IDLE`로 `on_status`를 부른다 — 이 어댑터는 명령 처리 중 여부를 별도로 추적하지 않는다. 더 정밀한 `BUSY` 구분은 후속 작업으로 미룬다.
5. `smartthings_poll_sec`의 검증은 `0 < smartthings_poll_sec <= online_ttl_sec`만 본다. `status_report_sec`과의 관계는 두지 않는다 — `architecture.md`·`requirements.md`에 적은 "status_report_sec 이상"은 ESP32 보고 주기를 그대로 베낀 과한 제약이었다. `docs/`는 이 작업에서 고치지 않으므로(경계) 문서 쪽 문구는 별도 docs PR로 고친다 — #259.

## 7. 가정

- `audioMute`의 확인 속성 값은 `muted`/`unmuted` 문자열이고, `mediaInputSource`의 확인 속성 이름은 `inputSource`라고 가정한다. `architecture.md` §4 "구현 전 확인"의 M5 실기 조회 전에 세운 가정이라 실제와 다를 수 있다. 다르면 fix PR로 고친다 — #259.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-06 | `tests/iot/test_smartthings_gateway.py` | 명령 ID 중복 처리, 모르는 동작·전송 실패의 `NOT_SENT`, 유효하지 않은 `timeout_sec` |
| FR-07 | `tests/iot/test_smartthings_gateway.py` | 확인 성공·시간 초과의 `SUCCEEDED`/`UNKNOWN`, 확인 조회 오류의 재시도 |
| FR-13 | `tests/iot/test_smartthings_heartbeat.py` | 성공·실패의 `on_status` 호출 여부 |
| FR-13 | `tests/shared/test_config.py` | `smartthings_poll_sec` 검증 범위 |
