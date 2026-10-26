# FR-18·FR-19 머지된 결정의 판정 반영 (#78, #79, #80, #82)

- 담당: eunji719 (판정)
- 요구사항: FR-09, FR-18, FR-19
- 따르는 결정: #78, #79, #80, #82

## 1. 왜

- 판정이 실행 중단 사유를 구분하지 못한다. `NOT_SENT` 결과에 재검사 판정이 없으면 모두 `INTERNAL_ERROR`로 거부해서, 정상 거부인 예약 만료와 연결 없음이 내부 오류처럼 보인다.
- 판정이 예약 시각을 상태 관리에 넘기지 않는다. 상태 관리는 인계 시각부터 예약 대기를 다시 세거나, 명령을 연결하지 않은 예약의 만료를 잴 수 없다.
- 복원 중인 기기가 ② 후보에 남는다. 복원 중인 기기를 골라서, 쓸 수 있는 다른 기기를 놓친다. 후보 목록(`find_available_devices`)에 나온 기기가 `request_use`에서 거부된다.

## 2. 무엇

- 입력: `ExecutionResult.reason`, `ReserveResult.reserved_at`, `ExecutionReadiness.can_execute`
- 출력: 호스트 `Decision`과 상태 관리로 넘기는 `ExecutionContext`
- 바뀌는 상태: 없다. 판정 기록은 지금처럼 저장한다.
- 약속:
  - 재검사 밖의 이유로 막힌 실행은 `reason`으로 호스트 판정을 정한다. 재검사 판정이 있으면 그것이 먼저다.
  - `ExecutionContext.reserved_at`에 `ReserveResult.reserved_at`을 그대로 담는다.
  - ②는 `can_execute`가 `False`인 기기를 후보에서 뺀다. 후보 목록 조회도 같은 조건을 쓴다.

| `ExecutionResult.reason` | 호스트 판정 |
|---|---|
| `COMMAND_IN_PROGRESS` | `BUSY` / `COMMAND_IN_PROGRESS` |
| `DEVICE_BUSY` | `BUSY` / `DEVICE_BUSY` |
| `TARGET_UNAVAILABLE` | `DENY` / `TARGET_UNAVAILABLE` |
| `NODE_NOT_CONNECTED` | `DENY` / `TARGET_UNAVAILABLE` |
| `RESERVATION_EXPIRED` | `DENY` / `TARGET_UNAVAILABLE` |
| `COMMAND_NOT_SENT` | `DENY` / `TARGET_UNAVAILABLE` |
| 그 밖의 값 | `DENY` / `INTERNAL_ERROR` |

## 3. 제약

- 연결 없음, 예약 만료, 미전송 증명 같은 내부 판단 근거를 호스트 응답에 공개하지 않는다 (#79, FR-11).
- 재검사 판정이 있으면 `reason`보다 먼저 쓴다. 두 값은 함께 오지 않는다 (`ExecutionResult`가 막는다).
- `INTERNAL_ERROR`는 예상하지 못한 예외와 알 수 없는 값에만 쓴다 (#25). 정상 거부에 쓰지 않는다.
- `reserved_at`을 판정이 만들거나 바꾸지 않는다. DB가 저장한 값을 그대로 넘긴다 (#80).
- ②에서 뺀 기기는 ③의 후보에도 넣지 않는다. 장소 전체 기기(`place_nodes`)는 지금처럼 넘긴다 (#39).
- 복원 중이라는 사실을 호스트에 공개하지 않는다. 명시 지정 대상이 빠지면 `TARGET_UNAVAILABLE`이다 (#82).
- 실행 직전 재검사의 ② 조건은 바꾸지 않는다. 전송 직전의 복원 검사는 상태 관리가 한다 (#82).
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/decision/`
- `server/tests/core/`
- `specs/core-decision-apply/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/proximity/`, `core/policy/`, `core/lifecycle/`, `db/`, `api/`, `iot/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `ExecutionResult.reason` | 사용 | 상태 관리 |
| `ReserveResult.reserved_at`, `ExecutionContext.reserved_at` | 사용, 제공 | DB, 상태 관리 |
| `ExecutionReadiness.can_execute` | 사용 | 상태 관리 |

### 만들지 않는 것
- `INTERNAL_ERROR`로 거부한 요청의 재판정 (#76). `core-internal-error-retry`와 `db-internal-error-retry`가 만든다.
- `ExecutionContext`의 서버 내부 요청(`SERVER_INTERNAL`)과 실행 문맥 조회 (#83, #86). 상태 관리와 DB의 일이다.
- 서버 진입점과 서비스 조립 (#88). 상태 관리가 만든다. 진입점이 `ExecutionReadiness`를 주입한다.
- 실행 직전 재검사의 ② 변경

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `NOT_SENT`이고 재검사 판정이 있다 | 재검사 판정을 그대로 반환한다 | `test_FR18_재검사_판정이_있으면_먼저_쓴다` |
| `NOT_SENT`이고 `reason`이 구동 경합이다 | `BUSY` / `COMMAND_IN_PROGRESS` | `test_FR19_중단_사유별_호스트_판정` |
| `NOT_SENT`이고 `reason`이 상태 충돌이다 | `BUSY` / `DEVICE_BUSY` | `test_FR19_중단_사유별_호스트_판정` |
| `NOT_SENT`이고 `reason`이 복원 미완료, 연결 없음, 예약 만료, 미전송 증명이다 | `DENY` / `TARGET_UNAVAILABLE` | `test_FR19_중단_사유별_호스트_판정` |
| `reason`이 알 수 없는 값이다 | `DENY` / `INTERNAL_ERROR` | `test_FR18_알_수_없는_중단_사유는_거부한다` |
| `NOT_SENT`인데 `reason`도 재검사 판정도 없다 | `DENY` / `INTERNAL_ERROR` | `test_FR18_사유가_없는_미전송은_거부한다` |
| 응답 | 내부 원인 이름이 없다 | `test_FR11_중단_사유의_내부_원인을_공개하지_않는다` |
| 예약 결과에 `reserved_at`이 있다 | `ExecutionContext.reserved_at`에 그대로 담는다 | `test_FR19_예약_시각을_그대로_넘긴다` |
| 예약 결과에 `reserved_at`이 없다 (공용형 `open`) | `ExecutionContext.reserved_at`이 `None`이다 | `test_FR19_예약_시각이_없으면_None이다` |
| 후보 기기가 `can_execute`가 `False`다 | ②에서 뺀다. ③에 넘기지 않는다 | `test_FR18_복원_중인_기기는_후보에서_뺀다` |
| 명시 지정 대상만 복원 중이다 | `DENY` / `TARGET_UNAVAILABLE` | `test_FR18_복원_중인_지정_대상은_거부한다` |
| 후보가 모두 복원 중이다 | `DENY` / `NO_CANDIDATE` | `test_FR18_후보가_모두_복원_중이면_거부한다` |
| 복원 중이 아닌 다른 후보가 있다 | 그 후보로 계속한다 | `test_FR18_복원_중인_기기_대신_다른_기기를_고른다` |
| `can_execute`가 예외를 낸다 | `DENY` / `INTERNAL_ERROR` | `test_FR18_복원_여부_조회_예외는_거부한다` |
| 후보 목록 조회 | 복원 중인 기기를 목록에서 뺀다 | `test_FR09_복원_중인_기기는_목록에서_뺀다` |
| 응답 | 복원 중이라는 말이 없다 | `test_FR11_복원_사실을_공개하지_않는다` |

## 6. 결정

- 호스트에 보이는 사유는 위 표로 정한다. 담당자가 정했다 — 연결 없음, 예약 만료, 미전송 증명은 모두 `DENY` / `TARGET_UNAVAILABLE`로 보여서 내부 판단 근거를 공개하지 않고 `ReasonCode`를 늘리지 않는다.
- `DecisionPipeline`과 `DeviceQueryService`의 생성자에 `readiness`를 필수 인자로 추가한다. 주입이 없으면 모든 기기를 실행할 수 없다고 봐야 해서(기본값은 거부) 기본값을 두지 않는다.
- `select_candidates`에 선택 인자 `executable`(기기 ID → 불리언 함수)을 추가한다. 인자가 없으면 지금과 같다. 재검사는 인자를 넘기지 않는다.
- `reserved_at`은 판정 도중 `_Applied`에 담았다가 `ExecutionContext`를 만들 때 쓴다.

## 7. 가정

- `CommandRecord`에는 중단 사유가 없다 — 재전송으로 저장 기록(`execution()`)에서 만든 `NOT_SENT`의 사유는 알 수 없으므로 지금처럼 `DENY` / `INTERNAL_ERROR`다. 사유를 저장하는 계약이 생기면 바꾼다.
- `ExecutionResult.reason`이 `NOT_SENT` 밖의 상태(`PENDING` 등)와 함께 와도 `reason`을 먼저 쓴다 — `reason`은 재검사 밖의 이유로 막힌 모든 결과에 붙는다고 본다 (#79).

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-18 | `test_FR18_재검사_판정이_있으면_먼저_쓴다` | 재검사 판정 우선 |
| FR-19 | `test_FR19_중단_사유별_호스트_판정` | 사유 6개의 판정 표 |
| FR-18 | `test_FR18_알_수_없는_중단_사유는_거부한다` | 기본값 거부 |
| FR-18 | `test_FR18_사유가_없는_미전송은_거부한다` | 기존 동작 유지 |
| FR-11 | `test_FR11_중단_사유의_내부_원인을_공개하지_않는다` | 응답 메시지 |
| FR-19 | `test_FR19_예약_시각을_그대로_넘긴다` | `reserved_at` 전달 |
| FR-19 | `test_FR19_예약_시각이_없으면_None이다` | 공용형 `open` |
| FR-18 | `test_FR18_복원_중인_기기는_후보에서_뺀다` | ② 후보, ③ 입력 |
| FR-18 | `test_FR18_복원_중인_지정_대상은_거부한다` | `TARGET_UNAVAILABLE` |
| FR-18 | `test_FR18_후보가_모두_복원_중이면_거부한다` | `NO_CANDIDATE` |
| FR-18 | `test_FR18_복원_중인_기기_대신_다른_기기를_고른다` | 다른 후보 선택 |
| FR-18 | `test_FR18_복원_여부_조회_예외는_거부한다` | `INTERNAL_ERROR` |
| FR-09 | `test_FR09_복원_중인_기기는_목록에서_뺀다` | 후보 목록 |
| FR-11 | `test_FR11_복원_사실을_공개하지_않는다` | 응답 |
