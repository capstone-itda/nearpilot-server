# FR-18 오류로 거부한 요청의 재판정 (#76)

- 담당: eunji719 (판정)
- 요구사항: FR-18, FR-22
- 따르는 결정: #76

## 1. 왜

- `INTERNAL_ERROR`로 거부한 요청을 같은 `request_id`로 다시 보내도 같은 거부가 돌아온다. ⓪이 저장된 거부를 기존 판정(`previous`)으로 반환하기 때문이다. 일시 장애로 거부한 요청이 영구 거부로 남는다 (#76).
- 판정이 바뀌면 이미 만든 예약이 이중 예약이 될 수 있다. 재판정의 조건을 정해야 한다.

## 2. 무엇

- 입력: 같은 `(계정, request_id)`와 같은 정규화 입력의 재전송. 이전 판정은 `DENY` / `INTERNAL_ERROR`다.
- 출력: 새로 한 판정 (`Decision`). 이전 판정이 아니다.
- 바뀌는 상태: 같은 내부 요청 ID의 판정 기록 (거부를 새 판정으로 바꾼다). 예약은 같은 내부 요청 ID의 기존 예약을 쓴다.
- 약속:
  - `link_request`는 이전 판정이 `INTERNAL_ERROR`이고 입력이 같으면 같은 `internal_request_id`로 `is_new=True`, `previous=None`을 반환한다.
  - 이 반환은 거부 기록을 "시도 중"으로 되돌리는 것과 한 번에 일어난다 (조건부 갱신). 동시에 온 두 재전송 중 하나만 새 시도가 된다. 나머지는 `IN_PROGRESS`로 거부한다.
  - 판정은 `is_new=True`이면 지금처럼 ①~⑥을 진행한다. 판정 코드는 재시도인지 알 필요가 없다.
  - ⑤⑥의 `reserve_if_free`는 같은 `internal_request_id`의 기존 예약을 그대로 반환하므로 이중 예약이 생기지 않는다.
  - 다른 판정(`OK`, `BUSY`, `ASK`, `DENY` 중 `INTERNAL_ERROR`가 아닌 것)은 확정 판정이다. 재판정하지 않는다.
  - 입력이 다르면 지금처럼 `REQUEST_ID_CONFLICT`다.

## 3. 제약

- 판정만 판정한다. DB는 거부 기록의 상태를 바꾸고 비교만 한다 (NFR-07, #24).
- 호스트 응답에 예외 내용을 넣지 않는다. 감사 기록에만 남긴다 (#25).
- 재판정 횟수의 제한은 두지 않는다. 호스트의 재전송 정책을 따른다.
- 이전 시도의 감사 기록은 지우지 않는다.

## 4. 경계

### 고치는 곳
- `server/tests/integration/` (실제 DB로 재판정을 확인하는 시나리오)
- `server/tests/core/` (fake 저장소로 `is_new=True`의 재진행을 확인하는 테스트)

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/decision/` (판정 코드는 바뀌지 않아야 한다. 바꿔야 하면 이 spec을 고친다)

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `Repository.link_request` | 사용 | DB |
| `Repository.save_decision` | 사용 | DB |
| `Repository.reserve_if_free` | 사용 (같은 내부 요청 ID의 기존 예약 반환) | DB |

### DB 담당에게 요청하는 변경 (DB spec에서 구현)
- `link_request`의 `_link_existing`: 같은 입력이고 저장된 판정이 `DENY` / `INTERNAL_ERROR`이면 `verdict`, `reason_code`를 NULL로 되돌리는 조건부 `UPDATE`(`WHERE verdict = 'DENY' AND reason_code = 'INTERNAL_ERROR'`)를 하고, 갱신된 행이 있을 때만 `is_new=True`로 반환한다.
- 이 변경은 스키마를 바꾸지 않는다. `save_decision`은 변경하지 않는다 (판정 기록이 NULL로 돌아간 행에 처음 저장하는 경우가 된다).

### 만들지 않는 것
- 저장 실패로 `verdict`가 NULL인 채 남은 요청의 재판정. 동시에 처리 중인 요청과 구분할 수 없어서 `IN_PROGRESS`로 거부하는 지금 동작을 유지한다.
- 재전송 횟수 제한, 지연 규칙.
- `shared` 모델 변경 (`RequestLink`에 재시도 표시 추가 등).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `INTERNAL_ERROR`로 거부한 요청을 같은 입력으로 재전송한다 | 다시 판정한다. 새 판정을 저장한다 | `test_FR18_오류로_거부한_요청을_같은_입력으로_다시_보내면_다시_판정한다` |
| 재전송이 `OK`를 받는다 | 같은 내부 요청 ID로 `OK`가 저장된다 | `test_FR18_retry_after_internal_error_saves_ok` |
| 첫 시도가 예약 뒤에 실패했다 | 재판정이 기존 예약을 쓴다. 예약이 둘이 되지 않는다 | `test_FR19_retry_after_internal_error_reuses_reservation` |
| 같은 키, 다른 입력으로 재전송한다 | `REQUEST_ID_CONFLICT`. 거부 기록을 바꾸지 않는다 | `test_FR18_retry_with_different_input_conflicts` |
| `OK`나 `BUSY` 같은 확정 판정을 재전송한다 | 저장된 판정을 그대로 반환한다 | `test_FR18_확정한_판정은_재전송해도_다시_판정하지_않는다` |
| 같은 거부에 대한 재전송이 동시에 둘 온다 | 하나만 판정한다. 나머지는 `INTERNAL_ERROR` (`IN_PROGRESS`) | `test_FR18_재판정이_진행_중이면_같은_키는_다시_판정하지_않고_거부한다` |
| 재판정도 예외로 끝난다 | 다시 `INTERNAL_ERROR`. 다음 재전송이 또 재판정한다 | `test_FR18_재판정도_실패하면_다음_재전송이_다시_판정한다` |
| 저장 실패로 `verdict`가 NULL이다 | `IN_PROGRESS`로 거부한다 (바뀌지 않음) | `test_FR18_null_verdict_stays_in_progress` |

## 6. 결정

- 같은 내부 요청 ID로 재시도한다 (선택지 A) — 예약의 멱등성(`reserve_if_free`)을 그대로 쓰고 스키마를 바꾸지 않는다.
- 판정 코드는 바꾸지 않는다 — 재시도 여부를 ⓪의 `is_new`가 이미 알려준다.

## 7. 가정

- DB 담당이 위 "요청하는 변경"을 DB spec에서 구현한다고 가정한다 — #76, `specs/db-admin-repository/spec.md`의 "#76 오류 거부 재판정"
- 이 가정이 틀리면 이 spec의 테스트 중 실제 DB를 쓰는 것은 DB 변경이 들어온 뒤에 통과한다. fake 저장소 테스트는 먼저 쓴다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-18 | `test_FR18_오류로_거부한_요청을_같은_입력으로_다시_보내면_다시_판정한다` | `is_new=True`인 재전송이 ①~⑥을 다시 진행한다 |
| FR-18 | `test_FR18_retry_after_internal_error_saves_ok` | 재판정 결과가 같은 내부 요청 ID로 저장된다 |
| FR-19 | `test_FR19_retry_after_internal_error_reuses_reservation` | 예약이 하나다 |
| FR-18 | `test_FR18_retry_with_different_input_conflicts` | 입력이 다르면 충돌이다 |
| FR-18 | `test_FR18_확정한_판정은_재전송해도_다시_판정하지_않는다` | 확정 판정은 재판정하지 않는다 |
| FR-18 | `test_FR18_재판정이_진행_중이면_같은_키는_다시_판정하지_않고_거부한다` | 동시 재전송 중 하나만 판정한다 |
| FR-18 | `test_FR18_재판정도_실패하면_다음_재전송이_다시_판정한다` | 다시 실패해도 재시도할 수 있다 |
| FR-18 | `test_FR18_null_verdict_stays_in_progress` | 저장 실패 행은 지금 동작을 유지한다 |
