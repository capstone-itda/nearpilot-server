# FR-09 request_use 조립 (판정에서 실행 인계까지)

- 담당: eunji719 (판정)
- 요구사항: FR-09, FR-18, FR-19
- 따르는 결정: #16, #35, 2026-10-27 결정 3 (재조작과 반납의 경합)

## 1. 왜

- 판정 파이프라인(`DecisionPipeline`)은 허가 여부만 반환하고, 실행 인계(`ExecutionService.execute`)는 상태 관리가 제공한다. 둘을 이어서 `request_use` 한 번의 결과로 만드는 곳이 없다.
- 이어 주는 곳이 없으면 MCP 도구가 판정 결과를 곧바로 호스트에 보내서 아직 실행하지 않은 요청에 `use_id`를 주거나, 실패한 실행을 성공처럼 보여 줄 수 있다.
- 같은 `request_id`를 재전송하면 판정은 저장된 `OK`를 돌려주는데, 그 요청의 실행은 이미 실패했을 수 있다.

## 2. 무엇

- 입력: `request_use` 요청 (`UseRequest`)
- 출력: 호스트에 보낼 판정 (`Decision`). 사용자 ID와 점유 주체가 없다.
- 바뀌는 상태: 새로 `OK`가 난 요청의 실행 인계. 판정과 실행이 바꾸는 상태는 각 영역이 정한 대로다.
- 약속:
  - 새로 `OK`가 난 요청만 `ExecutionService.execute()`로 인계한다. 그 밖의 판정은 그대로 반환한다.
  - 호스트에는 실행 결과를 확인한 뒤의 판정을 반환한다. `OK`와 `use_id`는 실행 성공(`SUCCEEDED`)일 때만 반환한다 (#16).
  - 재전송에는 저장된 실행 상태로 다시 만든 판정을 반환한다. 새 명령을 만들지 않는다.
  - 같은 실행 결과에는 같은 판정을 반환한다 (FR-10, NFR-09).

| 실행 결과 (`ExecutionStatus`) | 반환하는 판정 / 사유 코드 |
|---|---|
| `SUCCEEDED` | `OK` / `OK`. 대여형이면 `use_id`를 담는다 |
| `NOT_SENT`이고 재검사 판정이 있다 | 재검사 판정과 사유를 그대로 반환한다 |
| `NOT_SENT`이고 재검사 판정이 없다 | 중단 사유를 아래 중단 사유 표로 바꾼다. 사유가 없거나 표에 없으면 `DENY` / `INTERNAL_ERROR` |
| `REJECTED` | `BUSY` / `DEVICE_BUSY` |
| `FAILED` | `DENY` / `EXEC_FAILED` |
| `UNKNOWN` | `DENY` / `EXEC_UNKNOWN` |
| `PENDING`, `IN_PROGRESS` | `BUSY` / `COMMAND_IN_PROGRESS` |

| 중단 사유 | 반환하는 판정 / 사유 코드 |
|---|---|
| `COMMAND_IN_PROGRESS` | `BUSY` / `COMMAND_IN_PROGRESS` |
| `DEVICE_BUSY` | `BUSY` / `DEVICE_BUSY` |
| `TARGET_UNAVAILABLE`, `NODE_NOT_CONNECTED`, `RESERVATION_EXPIRED`, `COMMAND_NOT_SENT` | `DENY` / `TARGET_UNAVAILABLE` |
| `USE_NOT_FOUND` (재조작 인계 전에 대여가 반납됐다) | `DENY` / `USE_NOT_FOUND` |

## 3. 제약

- 기본값은 거부다. 예외, 누락, 알 수 없는 상태는 `OK`가 되지 않는다 (FR-18).
- `OK`가 아닌 판정이나 실행 성공이 아닌 결과에는 `use_id`를 담지 않는다 (#16).
- `Decision`에는 사용자 ID와 점유 주체를 넣지 않는다 (FR-11, NFR-08).
- 호스트 응답에 예외 내용을 넣지 않는다. 일반 안내만 넣는다 (#25).
- 이 서비스는 판정을 바꾸지 않는다. 판정을 다시 계산하지 않고 `DecisionPipeline`의 결과를 쓴다 (NFR-07).
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).
- 서버 판정 시간은 median 1초 이하, p95 2초 이하다 (NFR-01). 실행 대기 시간은 상태 관리의 `exec_timeout_sec`을 따른다.

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/decision/`
- `server/tests/core/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/proximity/`, `core/policy/`, `core/lifecycle/`, `db/`, `api/`, `iot/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `DecisionPipeline.decide_for_execution` | 사용 | 판정 (`core-decision-pipeline`) |
| `ExecutionService.execute` | 사용 | 상태 관리 |
| `ExecutionStore.execution` | 사용 | DB |
| `request_use(req) -> Decision` (`CoreService.request_use`) | 제공 | API (`api/mcp`) |

### 만들지 않는 것
- MCP 도구, 헤더 인증 연결, 응답 직렬화 (`api-mcp` spec)
- `CoreService`의 나머지 함수 5개 (`find_available_devices`, `get_device_state`, `request_access`, `release_use`, `get_use_status`)
- 서버 진입점과 서비스 조립 (#88)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 판정이 `OK`가 아니다 | 그대로 반환한다. `execute()`를 부르지 않는다 | `test_FR09_OK가_아닌_판정은_그대로_반환하고_인계하지_않는다` |
| 새로 `OK`가 났다 | `execute()`에 `decide_for_execution`이 만든 context를 넘긴다 | `test_FR09_새_OK는_context로_인계한다` |
| 실행이 성공했다 | `OK`와 `use_id`를 반환한다. 공용형은 `use_id`가 `None`이다 | `test_FR09_실행_성공이면_OK와_use_id를_반환한다` |
| 재검사가 막았다 | 재검사 판정과 사유를 그대로 반환한다 | `test_FR09_재검사가_막으면_그_판정을_반환한다` |
| 실행이 거부, 실패, 불명, 진행 중이다 | 2절 표의 판정을 반환한다. `use_id`는 없다 | `test_FR09_실행_결과별_판정` |
| 인계가 `NOT_SENT`인데 재검사 판정이 없다 | `DENY` / `INTERNAL_ERROR` | `test_FR09_사유를_모르는_NOT_SENT는_INTERNAL_ERROR` |
| `execute()`가 예외를 낸다 | `DENY` / `INTERNAL_ERROR`. 예외 내용을 넣지 않는다 | `test_FR09_인계가_예외를_내면_INTERNAL_ERROR` |
| 같은 요청이 재전송된다 (기존 `OK`) | 저장된 실행 상태로 2절 표대로 반환한다. `execute()`를 부르지 않는다 | `test_FR09_재전송은_저장된_실행_상태로_반환한다` |
| 재전송인데 실행 기록이 없다 | `BUSY` / `REQUEST_IN_PROGRESS`. 인계가 아직 끝나지 않은 요청이다 | `test_FR18_재전송인데_실행_기록이_없으면_REQUEST_IN_PROGRESS` |
| 재전송인데 실행 기록이 `NOT_SENT`다 | 첫 응답과 같은 판정을 준다. 근거(`evidence`)의 `source`가 `recheck`이면 근거의 판정과 사유, 아니면 근거의 중단 사유를 2절의 중단 사유 표로 바꾼다. 근거로 알 수 없으면 `DENY` / `INTERNAL_ERROR` | `test_FR18_재전송된_NOT_SENT는_첫_응답과_같은_판정을_준다` |
| `NOT_SENT` 재전송의 응답을 첫 호출과 비교한다 | 중단 사유마다 판정·사유·기기·`use_id`가 첫 호출과 같다 | `test_FR18_재전송은_첫_호출과_같은_판정을_준다` |
| 재전송인데 기존 판정이 `OK`가 아니다 | 기존 판정을 그대로 반환한다 | `test_FR09_재전송된_거부_판정은_그대로_반환한다` |
| 실행 기록 조회가 예외를 낸다 | `DENY` / `INTERNAL_ERROR` | `test_FR09_실행_기록_조회가_실패하면_INTERNAL_ERROR` |
| 재조작 인계의 중단 사유가 `USE_NOT_FOUND`다 (대여가 이미 반납됐다) | `DENY` / `USE_NOT_FOUND` | `test_FR19_반납과_겹친_재조작은_USE_NOT_FOUND` |
| 실제 서버에서 재조작 인계 직전에 같은 사용자가 반납한다 | 반납은 `OK`, 재조작은 `DENY` / `USE_NOT_FOUND`. `INTERNAL_ERROR`가 아니다 | `test_FR19_runtime_reoperation_after_release_is_use_not_found` (`tests/integration/`) |

## 6. 결정

- 서비스는 `core/decision/service.py`의 `RequestUseService` 하나다. 생성자로 `DecisionPipeline`, `ExecutionService`, `ExecutionStore`를 받고 `request_use(req) -> Decision`을 제공한다. 판정 파이프라인은 실행을 모르는 채로 둔다 — 판정과 실행 인계를 한 클래스에 묶으면 판정 테스트가 상태 관리 fake에 얽히기 때문이다.
- 재전송은 저장된 실행 상태로 다시 만든다 — 저장된 `OK`를 그대로 돌려주면 이미 실패한 실행이 성공처럼 보이기 때문이다. 담당자가 정했다. `decide_for_execution`이 `(기존 판정, None)`을 반환하고 기존 판정이 `OK`이면 `ExecutionStore.execution(internal_request_id)`을 조회한다.
- 실행 결과의 판정은 `Decision`을 새로 만들지 않고 최초 판정(`OK`)에서 `verdict`, `reason`, `use_id`, `message`만 바꾼다. `internal_request_id`, `request_id`, `node_id`, `p_max`는 유지한다.
- `ExecutionResult`와 `CommandRecord`는 같은 `ExecutionStatus` 표로 판정을 만든다. `use_id`는 `SUCCEEDED`일 때 `ExecutionResult.use_id`(재전송은 `CommandRecord.use_id`)를 쓴다.

## 7. 가정

- `NOT_SENT`에 재검사 판정이 없으면 `ExecutionResult.reason`(재전송은 근거의 `reason`)의 중단 사유를 2절의 중단 사유 표로 바꾼다. 사유를 알 수 없으면 `DENY` / `INTERNAL_ERROR`로 거부한다.
- 재전송인데 `ExecutionStore.execution()`이 `None`이면 실행 기록이 없다. 인계가 끝나지 않은 요청이다. context를 다시 만들지 않고 `BUSY` / `REQUEST_IN_PROGRESS`로 응답한다 (FR-18 ⓪ "진행 상태를 반환", 2026-10-27 팀 결정). 남은 예약은 상태 관리의 예약 대기 제한이 정리한다.
- 재전송의 `NOT_SENT`는 명령 기록의 근거로 첫 응답을 다시 만든다. 상태 관리가 미전송 취소 근거에 재검사의 `verdict`·`reason` 또는 중단 사유 `reason`을 남긴다 (`core/lifecycle/execution.py`). 근거로 알 수 없을 때만 `DENY` / `INTERNAL_ERROR`다 (2026-10-27 팀 결정).
- 인계 대기 중에 같은 요청이 동시에 재전송되면 상태 관리가 한 번만 실행하고 진행 상태를 반환한다 (`execute()`의 약속). 이 서비스는 잠금을 두지 않는다.
- 오류로 거부한 요청의 재전송은 판정이 다시 판정한다 (#76, `core-internal-error-retry`). 이 서비스는 ⓪의 결과를 따른다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | 5절의 `test_FR09_*` | 새 `OK`만 인계하고, 실행 결과로 판정을 만든다 |
| FR-11 | `test_FR11_request_use_Decision에는_사용자_ID가_없다` | 판정에 사용자 ID가 없다 |
| FR-18 | `test_FR18_FR09_실행_성공_전에는_use_id를_반환하지_않는다` | 성공 전 `use_id`가 없다 |

- 구현은 `server/tests/core/test_request_use.py`다.
- 다른 계층의 구현 없이 fake만으로 확인한다.
