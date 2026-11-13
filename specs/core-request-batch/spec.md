# 묶음 요청

- 상태: 완료
- 담당: 작업 지시자 (shared, 판정, DB, API 연결 구현)
- 요구사항: FR-09, FR-18, FR-22
- 따르는 결정: #284 (decision PR #285)

## 1. 왜

- `request_use` 한 번에는 대상 지정 하나와 동작 하나만 담는다. 거리 선택자도 종류 하나와 동작 하나다.
- "조명 켜고 스피커 꺼 줘"는 호출 두 번이다. Claude Code는 `readOnlyHint`가 없는 MCP 도구 호출을 한 메시지에서도 하나씩 실행한다. 그래서 기기가 차례로 움직인다 (#284).
- 도구 설명에 "전부"를 거리 선택자로 바꾸는 예가 없다. "조명 다 켜 줘"를 기기 ID마다 따로 부를 수 있다.

## 2. 무엇

- 입력:
  - `request_use`의 선택 인자 `requests`(배열). 항목마다 `target_spec`, `action`, 선택 인자 `arguments`다. `request_id`는 하나다.
- 출력:
  - 묶음 요청의 응답은 기존 여섯 키에 `devices`, `selected_count`, `similar`를 더한다. `devices`의 기기마다 `item`(1부터 센 항목 순서), `node_id`, `verdict`, `reason`, `use_id`를 담는다.
  - 단일 요청과 거리 선택자 요청의 응답 키와 값은 바뀌지 않는다.
- 바뀌는 상태:
  - 묶음 요청은 상위 요청 행 하나와 고른 기기마다 하위 요청 행을 둔다. 기존 열 `selector_parent_id`를 쓴다. 마이그레이션은 없다.
  - 상위 요청 행의 `decision_context`에 묶음 기록 `"batch"`를 더한다.
- 약속:
  - 항목의 `target_spec`은 네 방식을 모두 쓴다. 항목마다 판정 경로는 단일 요청이나 거리 선택자 요청과 같다.
  - 고른 기기 중 하나라도 판정에서 걸리면 어느 기기도 실행하지 않는다. 모두 통과하면 기기들을 동시에 실행한다.
  - 단일 요청과 거리 선택자 요청의 판정, 중복 판단 요약, 응답은 이전과 같다.

## 3. 제약

- `request_use`는 선택 인자 `requests`(배열)를 받는다. 항목은 `target_spec`, `action`, 선택 인자 `arguments`다. `request_id`는 묶음 전체에 하나다 (FR-09).
- `requests`와 단일 요청 인자(`target_spec`, `action`, `arguments`)를 함께 보내거나 둘 다 보내지 않으면 `DENY`와 `INVALID_INPUT`이다 (FR-09).
- 항목이 없거나 `batch_max_items`보다 많으면 `DENY`와 `INVALID_INPUT`이다. `batch_max_items`의 초기값은 8, 허용 범위는 1 이상의 정수다 (FR-09, §1.2).
- API는 `requests`와 단일 요청 인자 중 하나만 왔는지, 항목이 객체 배열인지, 항목의 형식(키, `target_spec`, `action`, `arguments`)을 검사한다. 항목 수 상한과 같은 기기 여부는 판정이 검사한다 (`architecture.md` §4 "묶음 요청의 판정").
- 서버는 항목 순서대로 대상을 고른다. 앞 항목이 고른 기기를 뒤 항목이 다시 고르면 `DENY`와 `INVALID_INPUT`이다 (FR-09).
- 근접 지정, 전역 할당, 명시 지정 항목은 단일 요청과 같은 ①~⑥으로 판정한다. 거리 선택자 항목은 거리 선택자 판정을 따른다. 경계 확률 검사도 항목마다 한다 (FR-09, FR-18).
- 고른 기기 중 하나라도 걸리면 어느 기기도 실행하지 않는다. 처음 걸린 기기의 판정과 사유를 반환한다. 순서는 항목 순서이고, 항목 안에서는 거리 순서다 (FR-09).
- 판정은 이미 만든 하위 요청의 예약과 승인 사용분을 미전송으로 취소한다 (`architecture.md` §4).
- 모두 통과하면 판정은 `OK`다. 실행, 실행 직전 재검사, 실행 결과는 기기마다 따로다. 기기들을 동시에 실행한다 (FR-09).
- 한 하위 요청의 재검사 거부나 실행 실패는 다른 하위 요청의 실행을 되돌리지 않는다 (FR-09).
- 응답은 기기마다 항목 순서, 기기 정보, 실행 결과, `use_id`를 담는다. 고른 기기 수의 합과 "비슷함"을 담는다. "비슷함"은 거리 선택자 항목 하나라도 비슷하면 표시한다 (FR-09).
- ⓪은 상위 요청의 외부 `(계정, request_id)`로 중복을 검사한다. 입력 요약은 요청 목록 전체와 항목 순서로 만든다. 항목 순서가 다르면 다른 입력이다 (FR-22).
- 묶음 요청의 판정 기록은 요청 목록과 항목마다 고른 기기를 담는다. 거리 선택자 항목은 거리 선택자 기록도 항목마다 담는다. 상위 요청의 `node_id`는 비어 있다 (FR-22).
- 하위 요청에는 외부 `request_id`가 없다. 하위 요청의 명령, 점유 세션, 승인 사용분은 하위 요청에 연결한다 (`architecture.md` §6).
- 응답에 사용자 ID, 점유 주체, 하위 요청의 `internal_request_id`를 넣지 않는다 (FR-11).
- 기준 값은 설정에서 받는다 (AGENTS.md).

## 4. 경계

### 고치는 곳

- `docs/architecture.md` §4 "묶음 요청의 판정"의 API 검사 문장 — API가 항목의 형식을 검사하는 구현과 맞춘다 (결정 31)
- `server/src/nearpilot/shared/`
  - `TargetKind.BATCH`, `TargetSpec.is_batch`, `TargetSpec`의 묶음 표기 `batch:`, 묶음 동작 이름 `BATCH_ACTION`
  - `UseItem`, `UseRequest.items`
  - `DeviceOutcome.item`
  - `BatchItemRecord`(항목의 `nodes` 포함), `BatchRecord`, `DecisionRecord.batch`, `BatchItemChildren`, `BatchChildren`
  - `Repository.batch_children`
  - 설정 `batch_max_items`
- `server/src/nearpilot/core/decision/` — 묶음 판정(`DecisionPipeline.decide_batch`), 응답 묶음(`aggregate_batch`), 중복 판단 요약
- `server/src/nearpilot/core/dashboard/events.py` — 묶음 상위 요청의 대상 표기
- `server/src/nearpilot/db/repositories/` — 묶음 하위 요청 행, 묶음 기록 저장과 조회
- `server/src/nearpilot/api/mcp/` — 입력 스키마, 검사, 응답, 도구 설명
- `server/src/nearpilot/runtime/` — 설정 로더
- `server/config/` — 설정 예시
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `docs/`(위 architecture 한 문장은 제외), `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: DB 스키마(마이그레이션), 근접 추정, 상태 관리(`core/lifecycle/`), IOT, 펌웨어, 대시보드 화면(`dashboard/`), 단일 요청과 거리 선택자의 판정 규칙

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `CoreService.request_use(UseRequest)`의 묶음 요청과 `Decision.devices[].item` | 제공 | 판정 → API |
| `Repository.link_request`, `Repository.link_selector_child`, `Repository.save_decision`의 `DecisionRecord.batch` | 사용·제공 | DB → 판정 |
| `Repository.batch_children` | 제공 | DB → 판정 |
| `ExecutionService.execute(ExecutionContext)` | 사용 | 상태 관리 |
| `DecisionRechecker.recheck(ExecutionContext)` | 사용 (바꾸지 않는다) | 판정 → 상태 관리 |
| `ExecutionStore.cancel_not_sent` | 사용 | DB |

### 만들지 않는 것

- 대시보드 화면의 묶음 요청 표시. 서버는 상위 요청의 대상을 `{"kind": "batch"}`로 보낸다.
- `request_access`의 묶음 요청
- 묶음 안의 항목끼리 실행 순서를 정하는 것

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `requests`와 `target_spec`을 함께 보낸다 / 둘 다 없다 | API가 `DENY`·`INVALID_INPUT`. 판정을 부르지 않는다 | `test_FR09_batch_mixed_or_missing_form_invalid_input` |
| `requests`가 빈 배열이다 / 객체가 아닌 항목이 있다 / 항목에 모르는 키가 있다 / 항목의 `target_spec`이나 `action` 형식이 틀렸다 / 항목의 `arguments`가 객체가 아니다 | API가 `DENY`·`INVALID_INPUT`. 판정을 부르지 않는다 | `test_FR09_batch_malformed_items_invalid_input` |
| 항목 안에 묶음 표기 `batch:`가 있다 | `DENY`·`INVALID_INPUT` | `test_FR09_batch_malformed_items_invalid_input` |
| 항목 수가 `batch_max_items`보다 많다 | `DENY`·`INVALID_INPUT`. 하위 요청을 만들지 않는다 | `test_FR09_batch_over_max_items_denied` |
| API가 판정 전에 묶음 요청을 거부한다 | 응답에 `devices` 빈 목록, `selected_count` 0, `similar` `false`를 담는다 | `test_FR09_batch_api_rejection_keeps_batch_keys` |
| 같은 기기를 두 항목이 고른다 (명시 지정 둘, 거리 선택자와 명시 지정) | `DENY`·`INVALID_INPUT`. 앞 항목의 예약을 취소한다. `devices`는 뒤 항목의 그 기기다 | `test_FR09_batch_duplicate_device_denied` |
| 두 번째 항목의 기기가 승인 필요다 | 아무것도 실행하지 않는다. 첫 항목의 예약과 사용분을 취소한다. 판정은 `NEED_APPROVAL`, `devices`는 걸린 기기 | `test_FR18_batch_one_fails_none_executes` |
| 거리 선택자 항목에 고를 기기가 없다 / 경계 확률이 낮다 | 상위 판정이 `DENY`·`NO_CANDIDATE` / `ASK`·`ORDER_UNCERTAIN`. 앞 항목의 예약을 취소한다. `devices`는 빈 목록 | `test_FR09_batch_selector_item_stops_batch` |
| 전역 할당 항목의 기기들이 모두 승인 필요다 | `NEED_APPROVAL`. 확률이 가장 큰 걸린 기기의 하위 요청을 만들어 `devices`에 낸다. 앞 예약을 취소한다 | `test_FR09_batch_any_item_policy_stop_reports_device` |
| 근접 지정 항목의 사후확률이 임계값 미만이다 | `ASK`·`LOW_CONFIDENCE`. 아무것도 실행하지 않는다. 그 항목의 하위 요청은 만들지 않고 `devices`는 빈 목록이다 | `test_FR18_batch_one_fails_none_executes`, `test_FR18_batch_item_child_created_after_choosing` |
| 한 기기의 실행이 실패한다 | 다른 기기의 결과는 그대로다. `devices`가 기기별 결과를 담는다 | `test_FR18_batch_partial_exec_failure_reported` |
| 같은 `request_id`로 재전송한다 | 하위 요청들의 진행 상태와 결과를 항목 순서로 묶어 반환한다. 새 명령을 만들지 않는다 | `test_FR22_batch_resend_returns_children_results` |
| 검사에서 걸린 묶음 요청을 재전송한다 | 같은 판정과 걸린 기기를 반환한다 | `test_FR22_batch_resend_after_check_failure` |
| 같은 `request_id`에 항목 순서나 내용이 다른 요청이 온다 | `DENY`·`REQUEST_ID_CONFLICT` | `test_FR22_batch_same_request_id_other_input_conflicts` |
| 같은 `request_id`로 단일 요청을 보냈던 키에 묶음 요청이 온다 | `DENY`·`REQUEST_ID_CONFLICT` (요약이 다르다) | `test_FR22_batch_digest_differs_from_single_request_with_same_content` |
| `OK`로 저장된 상위 요청의 묶음 기록이나 하위 판정이 맞지 않는데 재전송한다 (기록 없음, 항목 수나 항목 내용이 요청과 다름, 항목의 하위 요청 수가 맞지 않음, 하위 판정 없음, `OK`가 아닌 하위 판정) | 저장하지 않고 `DENY`·`INTERNAL_ERROR` | `test_FR22_batch_resend_inconsistent_ok_is_internal_error` |
| 하위 요청을 판정하던 중 오류가 난다 | 만든 하위 요청의 예약을 취소하고 `INTERNAL_ERROR` | `test_FR18_batch_internal_error_cancels_children` |
| 하위 요청을 실행하던 중 서버가 재시작한다 | 복원이 하위 요청의 `UseRequest`를 항목의 대상·동작과 상위 요청의 외부 ID로 다시 만든다 | `test_FR22_batch_child_restore_uses_item_target` |
| 하위 요청이 진행 중인데 중복 보장기간이 지난다 | 상위 요청의 외부 `request_id`를 비우지 않는다 | `test_FR22_batch_parent_request_id_kept_while_child_in_progress` |
| 단일 요청의 `target_spec`이나 `action`이 JSON으로 읽히는 문자열(`"null"`, `"[1]"`)이다 | 문자열 그대로 판정에 넘긴다 (이전과 같다) | `test_FR09_request_use_keeps_json_like_strings` |
| 기존 판정 입구(`decide_for_execution`, `decide_selector`)에 묶음 요청이 온다 | `decide_for_execution`은 `INVALID_INPUT`, `decide_selector`는 `ValueError` | `test_FR09_batch_old_entries_reject_batch` |

## 6. 결정

1. 묶음 요청은 `UseRequest` 하나로 넘긴다. `target`은 `TargetSpec(TargetKind.BATCH, None)`, `action`은 `"batch"`, `arguments`는 `None`, `items`는 항목들이다 — `CoreService.request_use`의 시그니처와 `link_request`를 그대로 쓴다.
2. 묶음 표기의 직렬화는 `batch:`다. `TargetSpec.parse("batch:")`가 묶음 표기를 돌려준다. 콜론 뒤에 글자가 있으면 형식 오류다 — 상위 요청 행의 `target_spec`이 `NOT NULL`이고, 기기 ID와 겹치지 않는다. 대시보드는 `{"kind": "batch"}`를 받는다.
3. `UseRequest`는 `target.kind`가 `BATCH`일 때만 `items`를 담는다. 그때 `action`은 `"batch"`이고 `arguments`는 `None`이다. 항목의 `target`은 `BATCH`가 아니다. 항목 수 상한은 판정이 설정으로 검사한다 — 모델은 설정을 모른다.
4. 입력 요약은 `{"batch": [항목마다 단일 요청과 같은 정규화], "parent_internal_request_id": ...}`다. 항목의 정규화는 단일 요청의 `target`, `action`, `arguments` 규칙과 같다 — 단일 요청과 거리 선택자 요청의 요약은 바뀌지 않는다.
5. 판정은 `DecisionPipeline.decide_batch(req) -> SelectorDecision`이다. `RequestUseService`는 묶음 요청을 `decide_batch`로 보내고, 하위 요청들을 거리 선택자와 같은 `_execute_children`으로 동시에 실행한다.
6. 하위 요청 행은 기기를 고른 뒤 `link_selector_child`로 만든다. 행의 `target_spec`, `action`은 그 항목의 값이고 `node_id`는 고른 기기다 — glossary "하위 요청"과 architecture §4의 "고른 기기마다". 그래서 하위 요청은 늘 기기를 가진다.
7. 근접 지정, 전역 할당, 명시 지정 항목은 단일 요청의 ①②③(전역 할당은 ④ 정책 검사와 배정 포함)으로 기기 하나를 고르고, 하위 요청을 만든 뒤 나머지(같은 기기 검사, 동작·인자 검사, ④, ⑤⑥)를 한다. 단일 요청의 `_run`은 이 두 부분을 이어 부른다. 고르기 감사는 상위 요청 ID로, 나머지 감사는 하위 요청 ID로 남는다. 거리 선택자 항목의 ①②와 고르기, 경계 확률 감사도 상위 요청 ID로 남고, 고른 기기마다 하위 요청을 만든다 — 거리 선택자 요청과 같은 모양이다. 고르기에서 멈추면 하위 요청 없이 묶음을 멈춘다 (결정 9와 같다).
8. 같은 기기 검사는 대상을 정한 직후, 동작·인자 검사 전에 한다. 걸리면 그 하위 요청의 판정이 `DENY`·`INVALID_INPUT`이고 감사 상세는 `{"duplicate": node_id}`다. 전역 할당 항목은 배정 뒤에 검사한다.
9. 항목의 고르기가 하위 요청 전에 멈추면(거리 선택자의 `NO_CANDIDATE`, `ORDER_UNCERTAIN`, 근접 지정의 `LOW_CONFIDENCE`, ①②③ 실패. 결정 32는 제외) 상위 판정이 그 판정이고 `devices`는 빈 목록이다. 앞 항목의 예약은 취소한다 (`source: batch_sibling`).
10. 걸린 하위 요청 때문에 취소한 앞 하위 요청의 판정 기록은 `OK`로 남고 실행 기록은 미전송 취소다. 취소 근거는 `source: batch_sibling`과 걸린 기기다 — 거리 선택자의 `selector_sibling`과 같은 모양이다.
11. 응답의 `selected_count`는 판정한 항목이 실제로 고른 기기 수의 합이다 (requirements FR-09). 거리 선택자 항목은 하위 요청을 만들었으면 고른 수 전체(중간 기기에서 걸려도), 하위 요청 전에 멈췄으면 0이다 — 거리 선택자 요청과 같다. 다른 항목은 만든 하위 요청 수(0 또는 1)다. 판정하지 않은 뒤 항목은 세지 않는다.
12. `similar`는 판정한 거리 선택자 항목 중 하나라도 `similar`이면 참이다. `ORDER_UNCERTAIN`으로 멈춘 항목도 계산한 값을 더한다.
13. 걸리면 `devices`는 걸린 하위 요청 하나다. `OK`면 모든 하위 요청이다. 기기마다 `item`을 담는다. 단일 요청과 거리 선택자 요청의 `DeviceOutcome.item`은 `None`이고 응답에 `item` 키를 넣지 않는다 — 기존 응답이 그대로다.
14. 상위 판정은 모든 하위 요청이 실행에 성공하면 `OK`다. 실행 단계에서 막히거나 실패하면 항목 순서에서 처음 실패한 기기의 판정과 사유다 — 거리 선택자 결정 3과 같다.
15. 묶음 기록은 `BatchRecord(items)`다. 항목마다 `BatchItemRecord(target_spec, action, arguments, children, selector, nodes)`다. `children`은 그 항목이 만든 하위 요청 ID, `nodes`는 같은 순서의 고른 기기(거리 선택자 항목은 `selected`의 앞쪽), `selector`는 거리 선택자 항목의 `SelectorRecord`(그 안의 `children`도 같은 ID)다. 요청의 항목을 모두 항목 순서로 담는다. 판정하지 않은 항목은 하위 요청·기기·`selector`가 없다. 호스트에 내보내지 않는다.
16. `batch_children(parent_iid)`는 묶음 기록과 항목마다 하위 요청(`SelectorChild`)을 기록 순서대로 돌려준다. 하위 요청의 기기는 저장된 판정의 `node_id`다 — 거리 선택자 항목은 기록의 `selected`와 같다. 판정 기록에 묶음 기록이 없으면 `None`이다.
17. `OK`로 저장된 상위 요청의 묶음 기록이 요청과 맞지 않으면 재전송은 저장하지 않고 `INTERNAL_ERROR`다 — 거리 선택자 결정 21과 같다. 맞는 기록은 항목 수와 항목마다 대상·동작·인자가 요청과 같고, 거리 선택자 항목은 하위 요청 수가 고른 수와 같고, 다른 항목은 하위 요청이 하나이며, 하위 판정이 모두 있고 `OK`다.
18. 거리 선택자 항목의 사후확률(`rental` 기기)은 항목마다 한 번 부른다. 근접 지정 항목은 단일 요청처럼 하위 요청마다 부른다.
19. 상위 요청의 판정 기록은 근접 추정 값을 담지 않는다. 하위 요청의 판정 기록이 담는다.
20. 묶음 요청이 아닌 요청이 `decide_batch`에 오면 `ValueError`다. `decide_for_execution`은 묶음 요청을 `INVALID_INPUT`으로 거부한다. `decide_selector`는 묶음 요청에 `ValueError`다 (지금처럼 거리 선택자가 아니므로).
21. 항목 수 검사는 ⓪ 뒤, 첫 항목 전에 한다. 넘으면 하위 요청 없이 `DENY`·`INVALID_INPUT`이고 감사 단계는 ② 후보, 상세는 `{"items": n, "batch_max_items": m}`이다.
22. MCP 입력 스키마에서 `target_spec`과 `action`은 선택 인자가 된다. 기존 호출은 그대로 받는다. `requests`의 항목 스키마는 `target_spec`(문자열, 필수), `action`(문자열, 필수), `arguments`(객체, 선택)다.
23. 도구 설명에 묶음 요청의 문법과 예("조명 다 켜고 스피커 꺼 줘"), "조명 다 켜 줘"는 `near:light:1:all`이라는 예, 하나라도 걸리면 아무것도 실행하지 않는다는 것, 응답 `devices`의 `item`(1부터 센 항목 순서), 같은 기기를 두 항목에 넣지 않는다는 것, 묶음 요청이 `NEED_APPROVAL`이면 `devices`의 `node_id`로 `request_access`를 부른다는 안내를 더한다.
24. 재검사(`recheck.py`)는 바꾸지 않는다. 하위 요청의 `ExecutionContext.request`는 항목의 대상·동작을 가진 `UseRequest`이고 `request_id`는 상위 요청의 외부 ID다. 그래서 단일 요청과 거리 선택자 하위 요청의 재검사를 그대로 받는다.
25. MCP 도구 함수는 `requests`를 `Any`로 받고 직접 검사한다 — `arguments`와 같은 방식이다. `target_spec`과 `action`은 기본값 `None`인 `str`로 받는다 — SDK는 주석이 `str`인 인자를 JSON으로 선파싱하지 않으므로 `"null"`, `"[1]"` 같은 값도 이전처럼 문자열로 넘어간다. JSON `null` 값은 입력 스키마(`string`)를 어기므로 SDK가 도구 오류로 거부한다 — main의 단일 요청과 같다. 생략할 인자는 보내지 않는다.
26. 판정 전 거부의 응답 모양: 묶음 형식(단일 요청 인자가 하나도 없음)이면 묶음 키를 담는다. 두 형식을 섞었거나 둘 다 없으면 기존 여섯 키만 담는다.
27. 묶음 기록은 요청의 항목을 모두 담는다 (결정 15). ①②에서 멈춘 거리 선택자 항목은 `selector`가 없고 `children`이 비어 있다. 항목 수 초과·용량 초과도 항목을 모두 담고 하위 요청은 없다.
28. 같은 기기 검사는 거리 선택자 항목의 하위 요청에도 대상을 정한 직후에 한다. 앞 항목의 기기 집합은 항목이 모두 통과한 뒤에 갱신한다 — 한 항목 안의 고른 기기는 서로 다르다.
29. 걸린 하위 요청 때문에 하는 취소(`_cancel_siblings`)는 근거의 `source`를 인자로 받는다. 거리 선택자는 `selector_sibling`, 묶음 요청은 `batch_sibling`이다.
30. 판정 중 예외 정리에서 하위 요청에 `INTERNAL_ERROR`를 저장할 때 `node_id`는 그 하위 요청의 기기다 — 하위 요청은 늘 기기를 가진다. `link_selector_child`의 `node_id`는 main과 같은 `str`이다.
31. API는 항목의 형식(키, `target_spec`, `action`, `arguments`)을 단일 요청 인자와 같은 규칙으로 검사한다 — `UseItem`이 해석한 `TargetSpec`을 담기 때문이다. 항목 수 상한과 같은 기기 여부만 판정이 검사한다. 빈 배열은 API가 거부한다. architecture §4의 "항목 수와 항목 안쪽은 판정이 검사한다"를 이 구현에 맞춘다.
32. 전역 할당 항목이 ③ 안의 ④ 정책 검사에서 걸리면(`NEED_APPROVAL`, `DENY`) 그 판정은 기기를 가진다. 판정은 그 기기로 하위 요청을 만들어 판정을 저장하고 `devices`에 낸다 — 호스트가 결정 23대로 `devices`의 `node_id`로 `request_access`를 부를 수 있다. 결정 9의 다른 고르기 멈춤은 기기가 없으므로 하위 요청을 만들지 않는다.

## 7. 가정

- 없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `test_FR09_batch_target_spec_parse_serialize` | `batch:` 해석과 직렬화가 왕복하고 `batch:x`는 형식 오류다. 기존 형식의 해석은 그대로다 |
| FR-09 | `test_FR09_batch_use_request_validation` | 묶음 `UseRequest`의 `items`, `action`, `arguments` 규칙과 항목의 `BATCH` 금지 |
| FR-09 | `test_FR09_settings_batch_max_items_validated` | 기본값 8, 허용 범위, 옛 설정 스냅샷 |
| FR-09 | `test_FR09_batch_mixed_or_missing_form_invalid_input` | 두 형식을 함께 보내거나 둘 다 없으면 API가 거부한다 |
| FR-09 | `test_FR09_batch_malformed_items_invalid_input` | 빈 배열, 객체가 아닌 항목, 틀린 항목 형식은 API가 거부한다 |
| FR-09 | `test_FR09_batch_api_rejection_keeps_batch_keys` | 판정 전 거부도 묶음 키를 담는다 |
| FR-09 | `test_FR09_batch_tool_schema_and_description` | 입력 스키마에 `requests`가 있고 `target_spec`·`action`은 선택이다. 도구 설명에 묶음 요청과 `near:light:1:all` 예가 있다 |
| FR-09 | `test_FR09_batch_response_devices_have_item` | 응답의 `devices`에 `item`이 있고 하위 요청 ID와 사용자 ID가 없다. 거리 선택자 응답에는 `item`이 없다 |
| FR-09 | `test_FR09_batch_mixed_kinds_runs_all` | 조명 `near:light:1:all` `on`과 스피커 명시 지정 `off`를 함께 실행한다 |
| FR-09 | `test_FR09_batch_over_max_items_denied` | 항목 수가 상한을 넘으면 하위 요청 없이 거부한다 |
| FR-09 | `test_FR09_batch_duplicate_device_denied` | 같은 기기를 두 번 고르면 거부하고 앞 예약을 취소한다 |
| FR-09 | `test_FR09_batch_any_item_policy_stop_reports_device` | 전역 할당 항목이 ④에서 걸리면 걸린 기기가 `devices`에 나온다 |
| FR-09 | `test_FR09_batch_selected_count_counts_selector_item_fully` | 거리 선택자 항목이 중간 기기에서 걸려도 고른 수 전체를 센다 |
| FR-09 | `test_FR09_batch_selector_item_stops_batch` | 거리 선택자 항목이 하위 요청 전에 멈추면 상위 판정이 그 판정이고 앞 예약을 취소한다 |
| FR-09 | `test_FR09_batch_similar_from_any_selector_item` | 거리 선택자 항목 하나라도 비슷하면 `similar` |
| FR-09 | `test_FR09_batch_old_entries_reject_batch` | 기존 판정 입구는 묶음 요청을 받지 않는다 |
| FR-09 | `test_FR09_batch_dashboard_target_kind` | 묶음 상위 요청의 대시보드 대상 표기는 `{"kind": "batch"}`다 |
| FR-09 | `test_FR09_batch_lights_and_speaker_real_db` | 실제 DB에서 조명과 스피커 묶음을 동시에 실행하고 재전송한다 (통합) |
| FR-18 | `test_FR18_batch_children_follow_single_path` | 항목의 하위 요청마다 예약, 판정 기록, 실행 인계가 있고 근접 지정 항목은 단일 요청과 같은 ①~⑥을 거친다 |
| FR-18 | `test_FR18_batch_one_fails_none_executes` | 하나가 걸리면 앞 하위 요청의 예약을 취소하고 아무것도 인계하지 않는다 |
| FR-18 | `test_FR18_batch_children_run_concurrently` | 다른 항목의 하위 요청들을 동시에 실행한다 |
| FR-18 | `test_FR18_batch_partial_exec_failure_reported` | 한 기기의 실패가 다른 기기를 되돌리지 않고 응답에 기기별로 나온다 |
| FR-18 | `test_FR18_batch_internal_error_cancels_children` | 판정 오류 뒤 만든 하위 요청의 예약을 취소한다 |
| FR-18 | `test_FR18_batch_item_child_created_after_choosing` | 근접 지정 항목은 기기를 고른 뒤 하위 요청을 만들고, 고르기에서 걸리면 하위 요청이 없다 |
| FR-09 | `test_FR09_request_use_keeps_json_like_strings` | 단일 요청의 JSON 같은 문자열 인자가 문자열 그대로 판정에 간다 |
| FR-22 | `test_FR22_batch_digest_depends_on_items_and_order` | 항목 내용이나 순서가 다르면 요약이 다르다. 단일 요청과 거리 선택자의 요약은 그대로다 |
| FR-22 | `test_FR22_batch_record_round_trip` | 묶음 기록과 하위 요청을 저장하고 다시 읽는다 |
| FR-22 | `test_FR22_batch_record_nodes_round_trip` | 항목의 `nodes`가 왕복하고 옛 기록은 빈 값으로 읽는다 |
| FR-22 | `test_FR22_batch_item_record_nodes_validated` | 항목의 `nodes`가 하위 요청 수와 거리 선택자 `selected`와 맞아야 한다 |
| FR-22 | `test_FR22_batch_record_has_all_requested_items` | 걸려도, 항목 수 초과·용량 초과여도 기록이 요청의 항목을 모두 담는다 |
| FR-22 | `test_FR22_batch_record_has_item_nodes` | 상위 판정 기록의 항목마다 고른 기기가 있다 |
| FR-22 | `test_FR22_batch_same_request_id_other_input_conflicts` | 같은 `request_id`에 다른 입력이 오면 `REQUEST_ID_CONFLICT` |
| FR-22 | `test_FR22_batch_digest_differs_from_single_request_with_same_content` | 같은 내용의 단일 요청과 묶음 요청의 요약이 다르다 |
| FR-22 | `test_FR22_batch_parent_record_has_items` | 상위 판정 기록에 요청 목록, 항목별 하위 요청, 거리 선택자 기록이 있다 |
| FR-22 | `test_FR22_batch_resend_returns_children_results` | 재전송이 하위 결과를 항목 순서로 묶어 반환한다 |
| FR-22 | `test_FR22_batch_resend_after_check_failure` | 검사에서 걸린 요청의 재전송이 같은 판정과 걸린 기기를 반환한다 |
| FR-22 | `test_FR22_batch_resend_inconsistent_ok_is_internal_error` | 맞지 않는 `OK` 기록의 재전송은 `OK`를 내지 않는다 |
| FR-22 | `test_FR22_batch_child_restore_uses_item_target` | 복원이 하위 요청을 항목의 대상·동작과 상위 외부 ID로 다시 만든다 |
| FR-22 | `test_FR22_batch_parent_request_id_kept_while_child_in_progress` | 하위 요청이 진행 중이면 보존 정리가 상위 요청의 외부 ID를 비우지 않는다 |
