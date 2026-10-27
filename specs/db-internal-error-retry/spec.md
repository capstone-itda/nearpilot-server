# FR-18 오류 거부 재판정의 요청 연결 (`Repository.link_request`)

- 상태: 작성 중
- 담당: paiwhy (DB)
- 요구사항: FR-18, FR-22, NFR-03, NFR-07
- 따르는 결정: #76 (PR #110)

## 1. 왜

- `INTERNAL_ERROR`로 거부한 요청을 같은 `request_id`로 다시 보내도 `link_request`가 저장된 거부를 `previous`로 그대로 돌려줘서 판정이 다시 일어나지 않는다. 일시 장애로 거부한 요청이 영구 거부로 남는다 (#76).
- 판정(`specs/core-internal-error-retry/spec.md`, eunji719)이 `link_request`의 `is_new=True`·`previous=None` 반환만으로 재판정을 일으키기로 했다. `core/decision/` 코드는 바꾸지 않는다. DB가 거부 기록을 "시도 중" 상태로 돌려놓아야 한다.

## 2. 무엇

- 입력: `link_request(UseRequest, input_digest)`의 재전송. 저장된 판정이 `DENY`/`INTERNAL_ERROR`다.
- 출력: `RequestLink(internal_request_id, is_new=True, conflict=False, previous=None)` — 같은 내부 요청 ID로 새 요청처럼 반환한다.
- 바뀌는 상태: 같은 `internal_request_id`의 `request` 행에서 판정 칸(`verdict`, `reason_code`, `node_id`, `p_max`, `model`, `calib`, `policy_ver`, `config_ver`, `decision_context`, `settings_json`)을 `NULL`로 비운다. 비우기 전 값을 `audit_log`에 남긴다.
- 약속:
  - 같은 `(user_id, request_id)`·같은 `input_digest`이고 저장된 판정이 `DENY`/`INTERNAL_ERROR`일 때만 비운다.
  - 조회·감사·비우기는 `BEGIN IMMEDIATE` 트랜잭션 하나로 한다.
  - 갱신된 행이 있을 때만 `is_new=True`를 반환한다. 동시에 두 재전송이 와도 하나만 비운다.
  - 스키마와 `save_decision`은 바꾸지 않는다.

## 3. 제약

- DB는 거부 기록의 상태만 바꾸고 비교한다. 판정 코드는 모른다 (NFR-07, #24, `specs/core-internal-error-retry/spec.md` 3절).
- 비우는 조건은 `WHERE internal_request_id = ? AND input_digest = ? AND verdict = ? AND reason_code = ?`다. 네 값 모두 파라미터로 바인딩한다. `verdict`·`reason_code`는 리터럴 문자열이 아니라 `Verdict.DENY.value`·`ReasonCode.INTERNAL_ERROR.value`로 바인딩한다.
- 비우는 칸은 `save_decision`이 쓰는 판정 칸 전부다: `verdict`, `reason_code`, `node_id`, `p_max`, `model`, `calib`, `policy_ver`, `config_ver`, `decision_context`, `settings_json`. 그 밖의 컬럼(`input_digest`, `target_spec`, `action`, `received_at` 등)은 바꾸지 않는다.
- 비우기 전 값(`verdict`, `reason_code`, `node_id`, `p_max`, `policy_ver`)을 `audit_log`에 `Step.DEDUP`(⓪ 중복)으로 남긴다. 이전 시도의 감사 기록은 지우지 않는다(새 행을 추가한다).
- 입력(`input_digest`)이 다르면 지금처럼 `conflict=True`다. 비우지 않는다.
- 저장된 판정이 `DENY`/`INTERNAL_ERROR`가 아니면(`OK`, `BUSY`, `ASK`, 다른 사유의 `DENY`) 지금처럼 `previous`에 담아 반환한다. 비우지 않는다.
- `verdict`가 이미 `NULL`인 행(저장 실패 또는 동시 재전송 중 다른 쪽이 먼저 비움)은 지금처럼 `is_new=False`, `previous=None`("처리 중")이다.
- `reserve_if_free`는 바꾸지 않는다. 같은 `internal_request_id`의 재판정은 `reserve_if_free`의 기존 멱등 반환(기존 예약을 그대로 돌려줌)으로 이중 예약을 막는다 (FR-19).
- 임계값과 시간 값은 쓰지 않는다. 해당 없음.

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/db/repositories/`
- `server/tests/db/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/` (`core/decision/` 포함), `api/`, `iot/`, `tools/`, `server/src/nearpilot/db/schema/` (새 마이그레이션 없음), `server/tests/integration/` (재판정 전체 시나리오는 `specs/core-internal-error-retry/spec.md`가 맡는다)

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `Repository.link_request`의 재판정(`is_new=True`, `previous=None`) | 제공 | 판정 (`specs/core-internal-error-retry/spec.md`, eunji719, #76) |

### 만들지 않는 것
| 후속 | 내용 | 근거 |
|---|---|---|
| — | 저장 실패로 `verdict`가 `NULL`인 채 남은 요청의 재판정 | `specs/core-internal-error-retry/spec.md` "만들지 않는 것" |
| — | 재전송 횟수 제한, 지연 규칙 | 판정(CORE) 담당 |
| — | 이전 시도가 예약을 남긴 뒤 재판정이 ⑤⑥ 전에 거부로 끝나면 그 예약이 남는 문제. 판정이 오류 뒤 예약을 바로 취소하고, 취소도 실패하면 재시작 복원이 정리한다 (#80, `core-decision-pipeline`, `core-restore`) | 결정됨 (2026-10-27) |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `INTERNAL_ERROR`로 거부한 요청을 같은 키·같은 입력으로 다시 보낸다 | 판정 칸을 비우고 같은 `internal_request_id`로 `is_new=True`, `conflict=False`, `previous=None`을 반환한다 | `test_FR18_link_request_retry_after_internal_error` |
| 두 재전송이 동시에 온다 | 한쪽만 비우고 `is_new=True`를 받는다. 다른 쪽은 그 사이 `verdict`가 `NULL`이 된 행을 보고 `is_new=False`·`previous=None`("처리 중")이다 | `test_NFR03_link_request_retry_concurrent_once` |
| 같은 키에 다른 입력으로 다시 보낸다 | `conflict=True`다. 판정 칸을 비우지 않는다 | `test_FR18_link_request_retry_different_input_conflicts` |
| `INTERNAL_ERROR`가 아닌 확정 판정(`OK`, `DENY`/다른 사유 등)을 다시 보낸다 | 비우지 않는다. 지금처럼 `previous`에 기존 판정을 담아 반환한다 | `test_FR18_link_request_retry_confirmed_decision_unaffected` |
| 재판정도 `INTERNAL_ERROR`로 끝난다 | 다음 재전송도 같은 방식으로 다시 비우고 재판정한다 | `test_FR18_link_request_retry_again_after_second_internal_error` |
| 재판정으로 비우기 전 값이 감사에 남는가 | `audit_log`에 이전 판정(`verdict`, `reason_code`, `node_id`, `p_max`, `policy_ver`)이 `Step.DEDUP`으로 남는다 | `test_FR22_link_request_retry_audits_previous_decision` |
| 예약을 남기고 `INTERNAL_ERROR`로 끝난 요청을 재판정한다 | `reserve_if_free`가 같은 `internal_request_id`의 기존 예약을 그대로 반환한다. 예약이 둘이 되지 않는다 | `test_FR19_link_request_retry_keeps_single_reservation` |
| 오류 뒤 예약을 취소한 요청을 재판정한다 | `reserve_if_free`가 새 예약을 만든다. 취소 전 행은 그대로 둔다. 같은 요청의 살아 있는 예약은 하나다. `link_command`는 새 예약에 명령을 연결한다 | `test_FR18_retry_after_cancelled_reservation_links_new_reservation` |
| 재판정 뒤 같은 키를 다시 보낸다 (한 요청에 취소된 세션과 새 세션) | 판정이 `OK`이면 `previous.use_id`는 살아 있는 세션의 것이다. 살아 있는 세션이 없으면 가장 나중 세션의 것이다. `OK`가 아니면 `use_id`를 담지 않는다 | `test_FR22_link_request_previous_prefers_live_session_after_retry`, `test_FR22_link_request_previous_keeps_cancelled_only_session`, `test_FR22_link_request_previous_of_rejected_retry_has_no_use_id` |

## 6. 결정

- 비우기 전 값은 `verdict`, `reason_code`, `node_id`, `p_max`, `policy_ver` 다섯 개를 감사에 남긴다 — 앞 네 개는 `Decision`이 호스트에 돌려주는 판정 결과와 같은 범위다. `policy_ver`를 더해 재판정 결과가 그사이의 정책 변경 때문인지 확인할 수 있게 한다. `model`·`calib`·`config_ver`·`decision_context`·`settings_json`은 판정을 내리는 데 쓴 내부 값이라 "이전 판정"의 핵심이 아니다.
- 감사 단계는 `Step.DEDUP`("0")이다 — 이 비우기가 `link_request`, 즉 ⓪ 중복 단계에서 일어나기 때문이다.
- 조건부 `UPDATE`의 `WHERE`에 `input_digest`도 바인딩한다 — 이미 호출 쪽에서 `input_digest`를 확인했지만, 트랜잭션 안에서 같은 조건을 다시 확인해 조회와 갱신 사이의 전제를 명시적으로 고정한다.
- `verdict`·`reason_code`의 비교값은 `Verdict`·`ReasonCode` enum의 `.value`로 바인딩한다 — 리터럴 문자열을 SQL에 직접 쓰지 않는다. enum 값이 바뀌면 이 조건도 같이 바뀐다.
- `previous.use_id`는 저장된 판정이 `OK`일 때만 담는다 — `OK`가 아닌 판정에는 `use_id`를 담지 않는다 (`core-request-use` 3절). 재판정이 `BUSY` 등으로 끝나면 취소된 옛 세션만 남기 때문이다. 한 요청에 세션이 여럿이면(취소된 세션과 재판정의 새 세션) 살아 있는 세션을, 없으면 가장 나중 세션을 고른다 — 세션이 하나뿐인 지금의 모든 상태에서 결과를 바꾸지 않는다.

## 7. 가정

- 없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-18 | `test_FR18_link_request_retry_*` | 재판정 조건과 범위(같은 입력·`INTERNAL_ERROR`일 때만) |
| NFR-03 | `test_NFR03_link_request_retry_concurrent_once` | 동시 재전송 중 하나만 비운다 |
| FR-22 | `test_FR22_link_request_retry_audits_previous_decision` | 이전 판정이 감사에 남는다 |
| FR-19 | `test_FR19_link_request_retry_keeps_single_reservation` | 재판정이 기존 예약을 그대로 쓴다 |
| FR-18 | `test_FR18_retry_after_cancelled_reservation_links_new_reservation` | 취소한 예약의 재판정이 새로 예약하고 명령을 연결한다 |
| FR-22 | `test_FR22_link_request_previous_*` | 세션이 여럿인 요청의 `previous.use_id` 선택 |
