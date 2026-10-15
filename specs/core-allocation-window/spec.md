# FR-23 대기 구간과 any:locker 요청 연결

- 상태: 작성 중
- 담당: eunji719 (판정)
- 요구사항: FR-23, FR-16, FR-18, FR-19
- 따르는 결정: 없음
- 선행: `core-allocation` (#117)

## 1. 왜

- 배정 함수(`allocate`)가 있어도 `any:locker` 요청을 모아서 넘기는 곳이 없다. 지금 `any:`는 파이프라인에서 `INVALID_INPUT`으로 거부된다.
- 요청을 하나씩 배정하면 겹친 요청이 같은 사물함을 고른다. 한 요청이 먼저 가져가면 다른 요청은 쓸 수 있는 사물함이 남아도 `BUSY`가 된다 (FR-23).

## 2. 무엇

- 입력: `any:locker` 요청 (`UseRequest`)과 설정의 `allocation_window_ms`
- 출력: 요청의 `Decision`. 배정받으면 지금의 `nearest:` 요청처럼 `OK`와 `use_id`, 실행 인계 `ExecutionContext`를 낸다.
- 바뀌는 상태: 배정받은 요청의 ⑤⑥ 예약 (`reserve_if_free`). 지금의 `nearest:` 요청과 같다.
- 약속:
  - 첫 `any:` 요청이 대기 구간을 연다. 구간은 `allocation_window_ms` 뒤에 닫힌다 (고정 구간). 그 안에 들어온 `any:` 요청을 한 묶음으로 배정한다.
  - 허용쌍은 같은 ② 후보, ③ 근접 임계값(`threshold_locker`), ④ 정책 `OK`, 점유(`AVAILABLE`) 조건을 모두 만족한 쌍이다.
  - 판정 결과는 아래와 같다.

| 상황 | 판정 |
|---|---|
| 후보가 없다 | `DENY` / `NO_CANDIDATE` (지금과 같다) |
| 임계값을 넘은 후보가 없다 | `ASK` / `LOW_CONFIDENCE` |
| 임계값을 넘은 후보가 있지만 ④가 모두 `OK`가 아니다 | 확률이 가장 큰 후보의 ④ 판정 (`NEED_APPROVAL` 또는 `DENY`)을 그대로 쓴다 |
| ④를 통과했지만 모두 점유 중이다 | `BUSY` / `DEVICE_BUSY` |
| 허용쌍이 있지만 배정받지 못했다 (다른 요청이 가져갔다) | `BUSY` / `DEVICE_BUSY` |
| 배정받았지만 ⑤⑥이 거부한다 | 지금과 같다 (`BUSY` 등) |
| 배정받았다 | `OK`. 실행 인계와 실행 직전 재검사는 지금과 같다 |

## 3. 제약

- 근접 확률을 다시 정규화하거나 임계값을 완화하지 않는다. `any:` 요청은 `threshold_locker`를 쓴다. 명시 지정 임계값은 쓰지 않는다 (FR-23).
- ④가 `NEED_APPROVAL`인 기기는 허용쌍에 넣지 않는다. 담당자가 정했다.
- 배정은 판정을 바꾸지 않는 계산이다. 배정 함수에 넘기는 값은 이미 거른 허용쌍의 확률이다 (NFR-07).
- 대기 시간은 `allocation_window_ms`다. 코드 상수로 쓰지 않는다. 대기는 구간을 연 요청 스레드가 한다. 따로 타이머 스레드를 만들지 않는다.
- 요청이 오지 않거나 배정이 실패해도 대기 중인 요청이 영원히 멈추지 않는다. 구간을 연 스레드는 예외가 나도 모든 요청을 깨운다.
- 같은 입력은 입력 도착 순서와 관계없이 같은 배정이다 (NFR-09). 구간 안의 도착 순서는 배정에 영향을 주지 않는다.
- 호스트 응답에 배정 방식, 다른 요청, 다른 후보를 공개하지 않는다 (FR-11).
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/allocation/`
- `server/src/nearpilot/core/decision/`
- `server/tests/core/`
- `specs/core-allocation-window/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/proximity/`, `core/policy/`, `core/lifecycle/`, `db/`, `api/`, `iot/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `allocate`, `AllocationRequest` | 사용 | 판정 (`core-allocation`) |
| `Repository.reserve_if_free`, `PolicyChecker.check`, `ProximityEstimator.posterior` | 사용 | DB, 정책, 근접 (지금과 같다) |
| `AllocationWindow.submit` | 제공 | 판정 파이프라인 (같은 영역) |

### 만들지 않는 것
- 서버 조립에서 `AllocationWindow`를 만들어 주입하는 일 (#88). 상태 관리가 진입점에서 한다.
- `any:` 이외 기능(`any:light` 등)의 배정. `any:`는 `locker`만 지원한다.
- 본인이 점유 중인 사물함의 재조작을 `any:`로 하는 일. 새 대여만 다룬다.
- 대기 구간 안의 같은 `request_id` 재전송 처리. 지금의 ⓪과 같다 (판정 중인 같은 키는 `INTERNAL_ERROR`).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 구간 안에 요청 둘이 온다 | 한 묶음으로 배정한다 | `test_FR23_구간_안의_요청은_한_묶음으로_배정한다` |
| 구간이 닫힌 뒤 요청이 온다 | 새 구간을 연다 | `test_FR23_구간이_닫힌_뒤의_요청은_새_구간을_연다` |
| 대기 시간 | `allocation_window_ms`를 초로 바꿔서 기다린다 | `test_FR23_설정한_대기_시간만큼_기다린다` |
| 배정 함수가 예외를 낸다 | 묶음의 모든 요청이 예외를 받는다 | `test_FR23_배정_예외는_모든_요청에_전달한다` |
| 구간을 연 스레드의 대기가 예외를 낸다 | 묶음의 모든 요청을 깨운다 | `test_FR23_대기_예외여도_모든_요청을_깨운다` |
| `any:locker` 요청 하나 | 허용쌍 중 확률이 가장 큰 사물함을 받는다 | `test_FR23_any_요청_하나는_가장_가까운_허용_기기를_받는다` |
| 임계값을 넘은 후보가 없다 | `ASK` / `LOW_CONFIDENCE` | `test_FR23_임계값을_넘은_후보가_없으면_ASK` |
| 임계값 미만 후보는 허용쌍이 아니다 | 배정 입력에 없다 | `test_FR23_임계값_미만_후보는_배정_입력에_없다` |
| ④가 `NEED_APPROVAL`이다 | 허용쌍에서 뺀다 | `test_FR23_승인이_필요한_기기는_허용쌍이_아니다` |
| ④가 모두 `OK`가 아니다 | 가장 큰 확률 후보의 ④ 판정 | `test_FR23_정책이_모두_거부하면_그_판정을_유지한다` |
| 점유 중인 기기 | 허용쌍에서 뺀다. 남은 후보가 없으면 `BUSY` | `test_FR23_점유_중인_기기는_허용쌍이_아니다` |
| 다른 요청에 밀려 배정받지 못한다 | `BUSY` / `DEVICE_BUSY` | `test_FR23_배정받지_못하면_BUSY` |
| 배정받은 기기를 예약한다 | 배정한 기기로 `reserve_if_free`를 부른다. `ExecutionContext.node_id`가 배정한 기기다 | `test_FR23_배정받은_기기를_예약하고_인계한다` |
| 승인 ID가 있는 정책이다 | 예약에 승인 ID를 넘긴다 | `test_FR23_예약에_정책의_승인_ID를_넘긴다` |
| 두 요청이 사물함 하나를 두고 겹친다 | 서로 다른 기기를 받거나 한쪽은 `BUSY`다. 중복 예약은 없다 | `test_FR23_겹친_요청은_중복_예약되지_않는다` |
| 배정 중 예외가 난다 | `DENY` / `INTERNAL_ERROR` | `test_FR18_any_배정_예외는_내부_오류로_거부한다` |
| 대기 구간이 주입되지 않았다 | `DENY` / `INVALID_INPUT` (지금과 같다) | `test_FR18_any_지정은_구간이_없으면_거부한다` |
| `any:`가 `locker`가 아니다 | `DENY` / `INVALID_INPUT` | `test_FR23_locker_외_any_지정은_거부한다` |
| 응답 | 배정 방식과 다른 후보가 없다 | `test_FR11_배정_정보를_공개하지_않는다` |

## 6. 결정

- 구간은 `core/allocation/window.py`의 `AllocationWindow(window_ms, wait=time.sleep, allocate_fn=allocate)` 하나다. `submit(request) -> str | None`이 구간이 닫힐 때까지 기다린 뒤 배정한 기기 ID 또는 `None`을 반환한다. 담당자가 정했다 — 호출하는 쪽(요청 스레드)이 기다리는 동기 방식이라 파이프라인 구조를 바꾸지 않는다.
- 구간은 고정이다. 담당자가 정했다 — 첫 요청이 구간을 열고 `allocation_window_ms` 뒤에 닫는다. 최대 대기가 정해져서 NFR-01(p95 2초)을 지킨다.
- 구간을 연 요청의 스레드가 `wait(초)`로 기다리고 배정한다. 나머지 요청은 이벤트를 기다린다. 담당자가 정했다 — 타이머 스레드를 만들지 않는다. `wait`과 `allocate_fn`은 테스트가 바꿀 수 있다.
- `pending()`은 지금 열린 구간에서 기다리는 요청 수를 반환한다. 진단과 테스트용이다.
- ④가 `NEED_APPROVAL`인 기기는 허용쌍에서 뺀다. 담당자가 정했다 — 승인이 필요한 기기를 배정해서 방문자가 막히는 일을 피한다. 쓸 수 있는 기기가 없고 승인 필요 기기만 있으면 그 ④ 판정을 그대로 낸다.
- 파이프라인은 `DecisionPipeline(…, window=None)`으로 받는다. 기본값이 `None`이면 지금처럼 `any:`를 `INVALID_INPUT`으로 거부한다.
- 배정 단계의 감사 기록은 `Step.DEVICE_OCCUPANCY`로 남기고 `payload`에 `allocation`(허용쌍 기기, 배정 결과)을 담는다. 담당자가 정했다 — `Step`에 값을 늘리면 `shared`를 바꿔야 한다.
- `any:` 처리는 `pipeline.py`의 `_run_any` 메서드로 나눈다. ①②까지의 계산은 `_run`과 공유한다.

## 7. 가정

- `any:`는 새 대여만 다룬다 — 점유 중인 사물함은 `AVAILABLE`이 아니어서 허용쌍에서 빠진다. 본인 점유의 재조작은 명시 지정이나 `nearest:`로 한다.
- 사물함 외 `any:`는 FR-23이 `any:locker`만 묶는다고 해서 지원하지 않는다.
- 구간을 연 스레드가 배정까지 한다 — 대기하는 동안 스레드 하나가 묶인다. MCP 도구는 요청마다 스레드(`asyncio.to_thread`)에서 판정을 실행하므로 구간 하나가 요청 수만큼 스레드를 쓴다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-23 | 5절의 `test_FR23_*` | 구간, 허용쌍, 배정, 예약, 동시성 |
| FR-18 | `test_FR18_any_*` | 예외와 설정 누락 시 거부 |
| FR-11 | `test_FR11_배정_정보를_공개하지_않는다` | 응답에 배정 정보가 없다 |

- 구현은 `server/tests/core/test_allocation_window.py`(구간)와 `server/tests/core/test_decision_pipeline.py`(연결)다.
- 구간 테스트는 `wait`을 가짜로 바꿔서 실제 시간을 기다리지 않는다. 동시 요청은 스레드로 보내고 `pending()`으로 모두 모인 것을 확인한 뒤 구간을 닫는다.
