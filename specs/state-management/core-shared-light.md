# 공용 조명 유지 대상자와 이탈 꺼짐

- 담당: JM-220 (상태 관리)
- 요구사항: FR-20, NFR-12
- 따르는 결정: #15, #18, #86, #87, #88, 2026-10-26 센서 기반 물리 검증 제외 (#196)

## 1. 왜

- 공용 조명은 점유 세션 없이 여러 방문자가 함께 쓴다 (FR-20).
- 서버는 켜진 시간만으로 조명을 끄지 않는다. 유지 대상자가 모두 떠나면 꺼짐을 요청한다 (product.md 원칙).
- 저장소에는 유지 대상자와 이탈 판단의 구현이 없다.
- 구현이 없으면 아래 문제가 생긴다.
  - 아무도 없는 장소의 조명이 계속 켜져 있다.
  - 신호 유실을 이탈로 잘못 판단해서 사용 중인 조명을 끈다 (NFR-12).
  - 재시작 직후 유지 대상자가 없다는 이유로 조명을 끈다.

## 2. 무엇

- 입력:
  - 공용 조명 `on` 실행의 성공 (`core-execution`의 `ExecutionResult`와 `ExecutionContext`)
  - 공용 조명 `off` 실행의 성공. 방문자의 `off`와 서버 내부 요청의 `off`다.
  - RSSI 관측 (`core-node-events`에서 받는다)
  - 검증을 마친 현재 연결의 `NodeStatus`와 복원 완료 인계다.
  - 시계
- 출력: 유지 대상자가 모두 이탈한 조명의 꺼짐 요청. 서버 내부 요청(`InternalRequest`, 원인 `leave_off`)을 만들어 `core-execution`의 `execute()`로 넘긴다 (#86).
- `tick()`은 꺼짐을 요청한 후보의 기기 ID를 반환한다.
- 바뀌는 상태: 상태 관리 메모리의 조명별 유지 대상자 목록, 계정별 미수신과 유예 타이머, 재시작 뒤 초기 참여 대기. DB에 저장하지 않는다.
- 약속:
  - 유지 대상자가 한 명이라도 남으면 조명을 켜 둔다.
  - 유지 대상자마다 이탈을 판단한다. 같은 장소의 다른 앵커가 받은 본인 비콘도 인정한다.
  - 꺼짐 전송 직전에 유지 대상자를 다시 확인한다.
  - 시간에 따른 자동 꺼짐 타이머를 두지 않는다.
  - 재시작 뒤 유지 대상자를 복원하지 않는다.

## 3. 제약

- 유지 대상자는 켜기 요청에 성공했고 사용 권한이 유효한 계정이다 (FR-20).
- 사용 권한은 `PolicyChecker.check()`로 확인한다. 결과가 `OK`이거나 사유가 `APPROVAL_EXHAUSTED`이면 유지 권한이 있다. 그 밖의 거부는 유지 대상자에서 뺀다. 유지 권한 확인은 새 조작을 허가하거나 승인 사용분을 예약하지 않는다 (architecture.md §4 "이탈 판단의 관측 계약", #87).
- 유지 대상자를 메모리에 둔다. DB와 `shared`에 유지 대상자를 두지 않는다 (architecture.md §4 "운영 기준의 영역별 분담").
- 이탈 판단 (FR-20, architecture.md §4 "이탈 판단의 관측 계약"):
  1. 계정의 유효 관측이 `leave_absent_sec` 동안 없으면 유예를 시작한다.
  2. 유예 중 유효 관측을 다시 받으면 타이머를 초기화한다.
  3. 유예가 `leave_grace_sec`를 넘으면 그 계정을 유지 대상자에서 뺀다.
- 유효 관측은 아래 조건을 모두 만족한다.
  - 비콘이 계정의 등록 비콘이다. 비콘은 `Repository.beacons_of()`로 조회한다.
  - 관측한 앵커가 조명과 같은 장소에 있다. 장소는 `NodeInfo.place_id`로 확인한다.
  - 관측 시각이 마지막으로 인정한 관측보다 새롭다.
- 다른 계정과 다른 장소의 관측은 타이머를 초기화하지 않는다. 과거 관측의 재전송은 새 수신이 아니다 (§4 "이탈 판단의 관측 계약").
- 같은 장소 관측의 인정은 새 조작의 허가와 근접 검사를 면제하지 않는다 (FR-20).
- 현재 접근 정책이 허용한 명시적 `off`는 다른 유지 대상자가 남아 있어도 판정이 허용한다. 이 spec은 `off` 성공 뒤 그 조명의 유지 대상자를 비운다 (FR-20).
- 서버 중단 중 비콘을 받지 못한 시간으로 이탈을 확정하지 않는다 (FR-20).
- 재시작 뒤 초기 참여 대기 (FR-20):
  1. `core-restore`가 복원과 현재 기기 상태 확인을 마치고 관측을 재개한다.
  2. 그 시점부터 `leave_absent_sec`와 `leave_grace_sec`의 합을 기다린다.
  3. 대기가 끝나면 아래 조건을 모두 만족할 때만 꺼짐을 요청한다.
     - 새 유지 대상자가 없다.
     - 저장한 마지막 성공 명령이 `on`이다. 실제 켜짐의 관측이 아니다 (architecture.md §5 MQTT 7절).
     - 현재 연결의 신선한 `status`가 명령 처리 대기(`command_state=idle`)이고 조명이 `FAULT`가 아니다.
  - 마지막 성공 명령이 없거나 `off`이면 실제 켜짐을 추정해서 꺼짐을 요청하지 않는다.
- 비콘이 보인다는 이유만으로 유지 대상자를 복원하거나 새로 등록하지 않는다 (FR-20).
- 오프라인, `FAULT`, 현재 연결이나 명령 처리 상태를 확인하지 못한 조명에는 꺼짐을 요청하지 않는다 (FR-20).
- 시간 값은 `Settings`에서 받는다 (requirements.md §1.2).
- 이탈에 따른 꺼짐은 구현과 검증을 마친 뒤 활성화한다. 시간에 따른 자동 꺼짐과 구분한다 (FR-20).
- 꺼짐 요청 (architecture.md §4 "서버 내부 요청", #86):
  - `InternalRequest`(새 내부 요청 ID, 기기, `off`, `leave_off`, 현재 `Settings`, 생성 시각)와 `execution_kind=server_internal`인 `ExecutionContext`를 만든다. 사용자 ID와 가짜 방문자 계정을 만들지 않는다.
  - 판정의 근접·접근 승인을 다시 적용하지 않는다. 전송 직전의 유지 대상자 확인은 `core-execution`이 `can_request_off()`를 기기 잠금 안에서 불러서 한다.
  - 같은 조명의 꺼짐 요청이 끝나지 않았으면 새 요청을 만들지 않는다. 같은 문맥을 다시 인계해서 진행 상태를 읽는다.
  - 꺼짐이 성공하면 명시적 `off`의 성공과 같게 유지 대상자를 비운다.
- 활성화는 서버 조립이 실행 함수를 주입하는 것으로 한다. 주입하지 않으면 후보만 반환한다. 새 공용 설정을 만들지 않는다 (#88 A안).

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
| `Repository.beacons_of`, `nodes` | 사용 | DB |
| `ExecutionStore.last_succeeded` | 사용 (마지막 제어 상태) | DB |
| `PolicyChecker.check` | 사용 (유지 대상자의 사용 권한 확인) | DB (`core/policy`) |
| `ExecutionService.execute` (서버 내부 요청) | 사용 | 상태 관리 (`core-execution`) |
| `can_request_off` (전송 직전 내부 확인) | 제공 | 상태 관리 (`core-execution`) |

### 만들지 않는 것
- 방문자의 `on`과 `off` 요청 판정 (판정)
- 근접 확률 계산 (근접 추정)
- 대여형 점유의 이탈 해제. 대여형은 이탈로 점유를 해제하지 않는다 (FR-20).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 방문자 A가 켜고 B도 켠다. A만 떠난다 | 조명을 켜 둔다 | `test_FR20_light_stays_on_while_holder_remains` |
| 유일한 유지 대상자가 떠난다 | 미수신 시간과 유예 시간 뒤 서버 내부 요청으로 꺼짐을 요청한다 | `test_FR20_last_holder_leave_requests_off` |
| 실행 함수를 주입하지 않았다 | 꺼짐 후보만 반환하고 요청하지 않는다 | `test_FR20_last_holder_leave_off_candidate` |
| 꺼짐 요청이 아직 끝나지 않았다 | 새 내부 요청을 만들지 않고 같은 문맥을 다시 인계한다 | `test_FR20_pending_off_not_duplicated` |
| 서버 내부 꺼짐이 성공한다 | 유지 대상자를 비우고 같은 보고로 다시 요청하지 않는다 | `test_FR20_internal_off_success_clears_light` |
| 유지 대상자의 횟수형 승인을 다 썼다 | 유지 권한이 있다. 유지 대상자로 남긴다 | `test_FR20_exhausted_approval_keeps_holder` |
| 유예 중 같은 장소의 다른 앵커가 본인 비콘을 받는다 | 타이머를 초기화한다 | `test_FR20_same_place_anchor_resets_timer` |
| 다른 장소의 앵커가 본인 비콘을 받는다 | 타이머를 초기화하지 않는다 | `test_FR20_other_place_account_connection_does_not_reset` |
| 다른 계정의 비콘을 받는다 | 타이머를 초기화하지 않는다 | `test_FR20_other_place_account_connection_does_not_reset` |
| 꺼짐 전송 직전에 새 유지 대상자가 생긴다 | 꺼짐을 보내지 않는다 | `test_FR20_recheck_holders_before_off` |
| 유지 대상자의 승인이 만료된다 | 그 계정을 유지 대상자에서 뺀다 | `test_FR20_expired_permission_removes_holder` |
| 조명이 켜진 지 오래됐지만 유지 대상자가 있다 | 끄지 않는다 | `test_FR20_no_time_based_off` |
| 재시작 직후 유지 대상자가 없다 | 초기 참여 대기 전에는 끄지 않는다 | `test_FR20_restart_waits_initial_period` |
| 초기 참여 대기 뒤 현재 연결의 보고가 없다 | 꺼짐을 요청하지 않는다 | `test_FR20_restart_unconfirmed_light_not_off` |
| 명령 처리 상태가 `busy`나 `unknown`이다 | 꺼짐을 요청하지 않는다 | `test_FR20_restart_requires_idle_command_state` |
| 마지막 성공 명령이 없거나 `off`다 | 꺼짐을 요청하지 않는다 | `test_FR20_off_requires_last_control_on` |
| 조명이 `FAULT`다 | 꺼짐을 요청하지 않는다 | `test_FR20_unavailable_light_not_candidate` |
| 명시적 `off`가 성공한다 | 그 조명의 유지 대상자를 비운다. 마지막 제어 상태가 `off`라 꺼짐을 다시 요청하지 않는다 | `test_FR20_explicit_off_clears_holders` |

## 6. 결정

- `shared_light.py`에 `SharedLightService`를 둔다 — 담당자가 2026-10-15에 선택했다.
- 시계, 설정 조회, 기기별 공통 재진입 잠금을 주입한다 — 관측과 실행 성공 인계를 직렬화한다.
- 외부 호출자가 `tick()`으로 이탈을 검사한다 — 자체 스레드와 새 시간 설정을 만들지 않는다.
- `on_execution_result(context, result)`는 저장을 확인한 실행 결과만 받는다 — 성공한 공용 조명만 유지 대상자를 바꾼다.
- `observe(frame)`는 검증을 마친 RSSI를 받는다 — 활성 비콘과 같은 장소를 기존 조회 계약으로 확인한다.
- 관측 시각으로 과거 관측을 거른다. 미수신 시간은 서버 수신 시각부터 잰다 — 두 시각의 뜻을 구분한다.
- 유예가 끝난 뒤의 관측은 유지 대상자를 다시 만들지 않는다 — 새 켜기 성공으로만 다시 참여한다.
- 유지 대상자는 켜기 성공에 적용한 시간 설정을 보존한다 — 기존 타이머를 새 설정으로 연장하지 않는다.
- 성공한 `off`는 유지 대상자를 비운다. 꺼짐 후보는 `ExecutionStore.last_succeeded(node_id)`의 동작이 `on`일 때만 만든다 — 2026-10-26 결정으로 status에 켜짐 관측이 없다. 담당자가 마지막 성공 명령을 DB에서 읽기로 정했다(재시작 뒤에도 같은 판단).
- `resume(node_id)`는 복원과 현재 기기 확인을 마친 뒤 호출한다 — 초기 참여 대기를 호출 시각부터 잰다.
- `on_status(status)`는 검증을 마친 현재 연결의 보고를 받는다 — 이전 연결의 보고를 꺼짐 후보 근거로 쓰지 않는다.
- `tick()`은 꺼짐 후보마다 서버 내부 요청을 만들어 주입한 `execute`로 넘긴다 — 실행 함수가 없으면 후보만 반환한다.
- `can_request_off(node_id)`는 `core-execution`의 `confirm_internal`로 연결한다 — 실행 서비스가 같은 기기 잠금 안에서 확인하고 전송을 시작한다.
- 끝나지 않은 꺼짐 요청은 조명별로 하나만 둔다 — 같은 문맥의 재인계로 진행 상태를 읽고, 끝나면 지운다.
- 내부 요청 ID는 주입한 `new_id`로 만든다. 기본값은 UUID다 — 테스트에서 ID를 고정한다.
- 조회 실패는 예외로 알리고 유지 대상자를 보존한다 — 실패를 이탈이나 권한 만료로 보지 않는다.
- DB와 정책 검사는 `shared` 계약의 fake로 검증한다 — 실제 서버 조립은 이 spec에 넣지 않는다.

## 7. 가정

- #86 A안: 꺼짐은 `InternalRequest`로 저장하고 기존 실행 경로를 쓴다. 저장과 전송은 `core-execution`이 한다.
- FR-20의 이탈 처리는 CORE가 내부에서 시작한다. 방문자 접근 승인을 새로 받지 않는다.
- #88 A안: 상태 관리가 서버 조립에서 검증한 꺼짐 경로(`execute`)를 연결해서 활성화한다. 서버 조립은 이 spec에 넣지 않는다.
- #87 B안: 결과가 `OK`이거나 사유가 `APPROVAL_EXHAUSTED`이면 유지 권한이 있다. 검사 주기는 이탈 판단 주기와 같다.
- `resume()` 이전에는 꺼짐 후보를 만들지 않는다. 재시작한 서비스는 유지 대상자와 상태 보고를 복원하지 않는다. 마지막 제어 상태는 DB에서 읽는다.
- 보고의 최신성 판단은 IOT가 맡는다. 이 서비스는 현재 연결과 `online_ttl_sec` 안의 수신을 확인한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-20 | `test_FR20_on_success_adds_holder` | 공용 조명 `on` 성공 계정을 유지 대상자로 넣는다 |
| FR-20 | `test_FR20_last_holder_leave_requests_off` | 마지막 유지 대상자의 이탈 뒤에만 꺼짐을 요청한다 |
| FR-20 | `test_FR20_holders_not_restored_after_restart` | 재시작 뒤 유지 대상자 목록이 비어 있다 |
| NFR-12 | `test_NFR12_signal_loss_within_grace_keeps_holder` | 유예 안의 재수신이면 유지 대상자로 남는다 |

- 시간은 주입한 시계로 제어한다.

아래 테스트는 단위 검증이다.

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-20 | `test_FR20_last_holder_leave_off_candidate` | 실행 함수가 없으면 후보만 만든다 |
| FR-20 | `test_FR20_recheck_holders_before_off` | 새 켜기 성공 뒤 기존 후보를 거부한다 |
| FR-20 | `test_FR20_restart_waits_initial_period` | 복원 완료 인계부터 초기 참여 대기를 잰다 |
| FR-20 | `test_FR20_duplicate_success_does_not_reset_timer` | 같은 성공 인계가 타이머를 연장하지 않는다 |
| FR-20 | `test_FR20_query_failure_keeps_holders` | 조회 실패가 유지 대상자를 지우지 않는다 |
| FR-20 | `test_FR20_pending_off_not_duplicated` | 끝나지 않은 꺼짐 요청을 중복해서 만들지 않는다 |
| FR-20 | `test_FR20_internal_off_through_real_execution` | 실제 실행 서비스로 내부 요청을 저장·전송하고 유지 대상자 확인을 거친다 |
| FR-20 | `test_FR20_exhausted_approval_keeps_holder` | 횟수형 승인 소진 계정을 유지 대상자로 남긴다 |
| NFR-12 | `test_NFR12_signal_loss_within_grace_keeps_holder` | 같은 장소 재수신이 유예를 초기화한다 |
