# FR-18 실행 직전 재검사

- 상태: 완료
- 담당: eunji719 (판정)
- 요구사항: FR-18
- 따르는 결정: #16, #23, #26, #35, #39, #42

## 1. 왜

- 최초 판정(`OK`)과 기기에 명령을 보내는 시점 사이에 상태가 바뀔 수 있다. 비콘이 사라지거나, 기기가 `FAULT`가 되거나, 정책이 바뀌거나, 사용자가 멀어질 수 있다.
- 재검사가 없으면 상태 관리가 오래된 허가로 명령을 보낸다 (FR-06).
- 재검사가 대상을 다시 고르거나 자기 예약을 타인 점유로 보면 다른 기기가 실행되거나 정상 요청이 막힌다.

## 2. 무엇

- 입력: 실행 인계 객체 (`ExecutionContext`)
- 출력: 판정 (`Decision`). 통과는 `Verdict.OK` 뿐이다.
- 바뀌는 상태: 없음. 재검사 결과는 감사 기록에만 남긴다.
- 약속:
  - 상태 관리가 전송 직전에 `DecisionRechecker.recheck(ctx)`를 부른다 (#16).
  - 확정한 `ctx.node_id`만 검사한다. 검사 결과로 다른 기기를 고르지 않는다 (FR-18).
  - 정상 거부는 최초 판정과 같은 판정과 사유로 반환한다.
  - 검사 오류와 알 수 없는 상태는 `DENY` / `RECHECK_FAILED`다. `RECHECK_FAILED`는 정상 거부에 쓰지 않는다.
  - 자기 요청의 예약(`ctx.use_id`)을 타인 점유로 보지 않는다.
  - `recheck()`는 예외를 던지지 않는다.

| 단계 | 실패 조건 (`ctx.node_id` 기준) | 판정 / 사유 코드 |
|---|---|---|
| ① 비콘 | 현재 시각 기준 최근 T초 안에 활성 비콘을 대상이 있는 장소의 신뢰 기기가 수신하지 않았다 | `DENY` / `BEACON_NOT_SEEN` |
| ② 대상 | 대상이 같은 장소의 후보 조건(신뢰, 온라인, 종류, `FAULT` 아님)을 더 이상 만족하지 않는다 | `DENY` / `TARGET_UNAVAILABLE` |
| ③ 근접 | 대상의 사후확률이 설정 임계값 미만이다. 명시 지정 요청은 명시 지정 임계값을 쓴다 | `ASK` / `MOVE_CLOSER` |
| ④ 정책 | 승인이 필요하다 | `NEED_APPROVAL` / 정책 사유 |
| ④ 정책 | 정책이 허용하지 않는다 | `DENY` / 정책 사유 |
| 예약 | `ctx.use_id`가 있는데 세션이 없거나, 상태가 실행 종류에 맞지 않거나(최초 대여 `RESERVED`, 재조작 `ACTIVE`), 대상 기기나 계정과 맞지 않는다 | `DENY` / `RECHECK_FAILED` |
| 오류 | 위 검사 중 예외가 났거나 알 수 없는 판정을 받았다. 활성 비콘이 둘 이상이거나 대상의 장소가 없거나 임계값 설정이 없는 경우도 포함한다 | `DENY` / `RECHECK_FAILED` |

## 3. 제약

- 기본값은 거부다. 예외, 누락, 알 수 없는 상태는 `OK`가 되지 않는다 (FR-18).
- ⑤⑥은 다시 실행하지 않는다. `reserve_if_free()`를 부르지 않는다. 자기 예약은 `session()`과 `session_owner_matches()`로만 확인한다 (#16, FR-19).
- 재검사는 저장소를 바꾸지 않는다. `save_decision()`을 부르지 않는다. 최초 판정을 덮어쓰지 않기 위해서다.
- 재검사 실패 때 예약과 승인 사용분을 반환하는 일은 상태 관리가 DB에 요청한다. 이 작업은 하지 않는다 (#16).
- 임계값과 T는 `ctx.settings`에서 받는다. 판정 한 건에 설정 버전 하나를 적용한다 (FR-22).
- 호스트에 보내는 `Decision`에 사용자 ID와 점유 주체를 넣지 않는다 (FR-11, NFR-08).
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

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
| `DecisionRechecker.recheck` | 제공 | 상태 관리 |
| `Repository.beacons_of`, `rssi_since`, `nodes` | 사용 | DB |
| `Repository.session`, `session_owner_matches` | 사용 | DB |
| `ProximityEstimator.posterior` | 사용 | 근접 추정 |
| `PolicyChecker.check` | 사용 | DB |
| `AuditLog.append` | 사용 | DB |

### 만들지 않는 것
- 실행 인계(`ExecutionService.execute` 호출)와 `ExecutionContext` 조립. 최초 판정이 `OK`를 낸 뒤 context를 조립하는 일은 후속 작업이다.
- 재검사 실패 뒤 예약과 승인 사용분 반환 (상태 관리, #17)
- 전역 할당의 재검사 (FR-23)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 모든 검사를 통과한다 | `OK`를 반환한다. 저장소를 바꾸지 않는다 | `test_FR18_재검사는_모두_통과하면_OK이고_저장하지_않는다` |
| 비콘이 사라졌다 | `DENY` / `BEACON_NOT_SEEN`. 뒤 단계를 실행하지 않는다 | `test_FR18_재검사_비콘이_없으면_거부한다` |
| 대상이 `FAULT`나 오프라인이다 | `DENY` / `TARGET_UNAVAILABLE`. 다른 기기를 고르지 않는다 | `test_FR18_재검사_대상이_후보_밖이면_거부한다` |
| 대상의 확률이 임계값 미만이다 | `ASK` / `MOVE_CLOSER`. 다른 기기가 더 높아도 대상을 바꾸지 않는다 | `test_FR18_재검사_확률이_낮으면_MOVE_CLOSER이고_대상을_바꾸지_않는다` |
| 정책이 바뀌어 승인이 필요하거나 거부한다 | 정책 판정과 사유를 그대로 반환한다 | `test_FR18_재검사_정책_결과를_그대로_반영한다` |
| 대상이 자기 예약으로 `RESERVED` 상태다 | 타인 점유로 보지 않고 통과한다 | `test_FR18_재검사는_자기_예약을_타인_점유로_보지_않는다` |
| 재조작이다 | 기존 `ACTIVE` 세션이면 통과한다. 최초 대여의 `RESERVED`는 재조작을 통과시키지 않고 반대도 같다 | `test_FR18_재검사_재조작은_ACTIVE_세션이어야_통과한다` |
| 비콘을 대상이 없는 장소만 봤다 | `DENY` / `BEACON_NOT_SEEN` | `test_FR18_재검사_비콘을_다른_장소만_봤으면_거부한다` |
| 세션이 없거나 `RESERVED`가 아니거나 계정이 맞지 않는다 | `DENY` / `RECHECK_FAILED` | `test_FR18_재검사_예약이_유효하지_않으면_RECHECK_FAILED` |
| 검사 중 예외가 난다 | `DENY` / `RECHECK_FAILED`. 예외 내용을 `Decision`에 넣지 않는다 | `test_FR18_재검사_예외는_RECHECK_FAILED로_거부한다` |
| 정책이 알 수 없는 판정을 반환한다 | `DENY` / `RECHECK_FAILED` | `test_FR18_재검사_정책의_알_수_없는_판정은_OK가_되지_않는다` |
| 감사 기록이 실패한다 | `OK`가 되지 않는다 | `test_FR18_재검사_감사_기록이_실패하면_OK가_되지_않는다` |
| 같은 `ctx`와 같은 상태로 다시 부른다 | 같은 판정을 반환한다 | `test_FR18_재검사는_같은_입력에_같은_판정을_반환한다` |

## 6. 결정

- 현재 시각은 생성자로 받는 `clock`에서 읽는다 — 재검사는 전송 직전 시각이 필요한데 `ctx`에는 최초 판정 시각만 있다. 시계를 주입하면 테스트가 고정 시각으로 결정적이다. 선택은 담당자가 했다.
- ①②③④를 모두 다시 검사한다 — 비콘 이탈, 기기 상태 변화, 정책 변경을 모두 전송 전에 잡기 위해서다. ⑤⑥은 다시 하지 않고 자기 예약의 유효성만 확인한다. 선택은 담당자가 했다.
- ③ 실패는 최초 판정과 같은 `ASK` / `MOVE_CLOSER`를 반환한다 — #16이 정상 거부를 판정과 사유 그대로 전달하라고 정했다. 선택은 담당자가 했다.
- 장소는 대상 기기의 장소다 — 최초 판정은 ①에서 비콘을 본 기기로 장소를 정했고(`core-decision-pipeline` 6절) 대상은 그 장소에서 확정됐다. 재검사는 대상을 바꾸지 않으므로 대상의 장소를 기준으로 비콘 수신, 후보, `place_nodes`를 다시 본다.
- 임계값은 요청의 대상 지정이 명시 지정이면 `threshold_explicit_*`, 아니면 `threshold_*`다. 설정이 `None`이면 최초 판정이 거부했어야 할 요청이므로 검사 오류(`RECHECK_FAILED`)다.
- 세션 상태는 실행 종류로 정한다 — 최초 대여는 `RESERVED`, 재조작은 기존 `ACTIVE`다 (#35). 공용형(`SHARED`)은 세션이 없다.
- 구조: `core/decision/recheck.py`의 `DecisionRechecker`를 구현하는 `Rechecker` 클래스 하나다. 의존하는 계층과 `clock`은 생성자로 받는다. 후보 조건과 임계값 선택은 `pipeline.py`와 같은 코드를 쓰도록 모듈 함수로 뺀다 (최초 판정과 재검사가 다르게 판단하지 않게 하려는 것이다).
- 재검사 감사는 `Step.EXEC` 단계로 남기고, 페이로드에 `recheck`와 검사 결과, 설정 버전을 넣는다.

## 7. 가정

- ③에서 대상이 `posteriors`에 없으면 확률 0으로 보고 `MOVE_CLOSER`로 처리한다. 최초 판정과 같다.
- ④의 정책 `approval_id`가 재검사 시점에 달라져도 이 작업은 `ctx.usage_id`를 바꾸지 않는다. 승인 사용분은 최초 판정의 ⑤⑥ 결과 그대로다.
- 세션이 없는 경우(`ctx.use_id is None`)는 공용형이다. 예약 검사를 건너뛴다.
- 세션 상태가 `RESERVED`가 아닌 경우를 `RECHECK_FAILED`로 본다. 알 수 없는 상태는 `OK`가 되지 않기 때문이다 (FR-18). 상태 관리가 이미 상태를 바꾼 경우의 구분은 상태 관리와 맞춰야 한다.
- 미결: ① T와 ② 온라인 TTL의 값. #15. 값은 `ctx.settings`에서 받는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-18 | 5절의 테스트 이름 전부 | 재검사의 통과, 정상 거부, 검사 오류 |
| FR-18 | `test_FR18_재검사는_다른_기기를_고르지_않는다` | 대상이 `ctx.node_id`로 고정된다 |
| FR-11 | `test_FR11_재검사_Decision에는_사용자_ID가_없다` | 판정에 사용자 ID가 없다 |

- 구현은 `server/tests/core/test_recheck.py`다.
- 다른 계층의 구현 없이 fake만으로 확인한다.
