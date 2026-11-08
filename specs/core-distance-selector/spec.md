# 거리 선택자

- 상태: 작성 중
- 담당: 작업 지시자 (shared, IOT, 근접 추정, 판정, 상태 관리, DB, API 연결 구현)
- 요구사항: FR-09, FR-16, FR-17, FR-18, FR-22
- 따르는 결정: #253 (decision PR #254). 근접 참여 기기의 정의는 #252의 담당자 결정을 따른다.

## 1. 왜

- `target_spec`은 `nearest:{kind}`, 기기 ID, `any:{kind}`만 받는다. "가까운 조명 2개 꺼 줘", "제일 먼 조명 켜 줘", "나 있는 쪽 빼고 다 켜 줘"를 요청할 수 없다.
- 판정은 요청 하나에 기기 하나만 고르고 실행한다.
- IOT는 비콘을 하나도 보지 못한 빈 `rssi` 보고를 버린다. 서버는 "켜져 있지만 비콘이 안 보이는 먼 기기"와 "RSSI를 보고하지 않는 기기"를 구분하지 못한다.
- 서버는 기기별 마지막 `rssi` 보고 시각을 따로 기록하지 않는다. `last_seen_at`은 상태 보고로도 바뀐다.

## 2. 무엇

- 입력:
  - `request_use`의 `target_spec` `near:{kind}:{start}:{count}`, `far:{kind}:{start}:{count}`. `{count}`는 정수 또는 `all`이다.
  - 빈 보고를 포함한 모든 유효 `rssi` 보고
- 출력:
  - 거리 선택자 요청의 응답은 기존 키에 `devices`, `selected_count`, `similar`를 더한다. `devices`는 기기마다 `node_id`, `verdict`, `reason`, `use_id`를 담는다.
  - 다른 대상 지정의 응답 키와 값은 바뀌지 않는다.
- 바뀌는 상태:
  - `request` 테이블의 하위 요청 상위 연결 열 `selector_parent_id` (마이그레이션 `0012`)
  - 근접 추정의 기기별 마지막 `rssi` 보고 시각 (메모리)
- 약속:
  - 서버가 요청 순간의 거리 순서로 기기를 고른다. AI 호스트는 RSSI와 사후확률을 보지 않는다.
  - `shared` 기기는 순서만 쓴다. `rental` 기기는 고른 기기마다 `threshold_{kind}`와 비교한다.
  - 고른 기기 중 하나라도 검사에 걸리면 어느 기기도 실행하지 않는다.
  - `nearest:`, `any:`, 명시 지정의 판정, 중복 판단 요약, 응답은 이전과 같다.

## 3. 제약

- 도구의 입력 스키마는 바꾸지 않는다. `request_use`의 도구 설명에 거리 선택자의 문법과 예를 쓴다 (FR-09).
- 거리 선택자의 형식이 틀리면 `DENY`와 `INVALID_INPUT`이다. `{start}`와 정수 `{count}`는 1 이상이다 (FR-09).
- 거리 순서에는 요청 종류의 ② 후보 중 근접 참여 기기만 선다. 근접 참여 기기는 최근 구간 안에 `rssi` 보고(빈 보고 포함)를 보낸 `trusted` 기기다 (FR-09, #252).
- 순서는 ①의 활성 비콘에 대한 기기별 정렬 값으로 정한다. 정렬 값은 근접 추정이 계산한다 (FR-09, FR-16).
- 비콘을 보지 못한 기기는 가장 먼 쪽에 둔다. 정렬 값이 같은 기기끼리는 기기 ID 순서다 (FR-09).
- 서버는 시작부터 개수만큼 고른다. 모자라면 있는 만큼 고른다. 하나도 없으면 `DENY`와 `NO_CANDIDATE`다 (FR-09).
- 고른 범위의 경계에서 안쪽 기기와 바깥 기기의 정렬 값 차이가 기준 값보다 작으면 그대로 실행하고 `similar`를 표시한다 (FR-09).
- `shared` 기기는 사후확률 검사를 하지 않는다. ① 비콘 검사는 유지한다 (FR-17).
- `rental` 기기는 고른 기기마다 사후확률을 `threshold_{kind}`와 비교한다. 미만이면 `ASK`와 `LOW_CONFIDENCE`다. `threshold_{kind}`가 없으면 `DENY`와 `INVALID_INPUT`이다 (FR-17, `nearest:`와 같은 사유).
- 서버는 고른 기기마다 ④~⑥과 동작·인자 검사를 한다. 하나라도 걸리면 어느 기기도 실행하지 않는다. 서버는 거리 순서에서 처음 걸린 기기의 판정과 사유를 반환한다 (FR-09).
- 판정은 이미 만든 하위 요청의 예약과 승인 사용분을 미전송으로 취소한다 (`architecture.md` §4 "거리 선택자의 판정").
- 하위 요청은 단일 기기 요청과 같은 경로를 쓴다. 하위 요청에는 외부 `request_id`가 없다 (`architecture.md` §4, §6).
- 상위 요청의 판정 기록은 거리 선택자, 거리 순서, 기기별 정렬 값, 고른 기기, `similar`를 담는다. 상위 요청의 `node_id`는 비어 있다 (FR-22).
- ⓪은 상위 요청의 외부 `(계정, request_id)`로 중복을 검사한다. 재전송에는 하위 요청들의 진행 상태와 결과를 묶어 반환한다 (`architecture.md` §4).
- 하위 요청의 실행 직전 재검사는 대상을 다시 고르지 않는다. `shared` 기기는 비콘 수신만 확인한다. `rental` 기기는 근접 임계값을 다시 확인한다 (FR-18).
- 한 하위 요청의 재검사 거부나 실행 실패는 다른 하위 요청의 실행을 되돌리지 않는다 (FR-09).
- 응답에 사용자 ID, 점유 주체, 하위 요청의 `internal_request_id`를 넣지 않는다 (FR-11).
- 시간 값과 기준 값은 설정에서 받는다 (AGENTS.md).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/shared/`
  - `TargetKind.NEAR`, `TargetKind.FAR`, `TargetSpec.start`, `TargetSpec.count`
  - `Decision.devices`, `Decision.selected_count`, `Decision.similar`, 기기별 결과 값 객체
  - `DecisionRecord`의 거리 순서 필드
  - `RssiReport`, `NodeEventSink.on_rssi_report`
  - `ProximityEstimator.distance_order`와 결과 값 객체
  - `Repository`의 하위 요청 접점
  - 설정 `anchor_recent_sec`, `order_similar_db`
- `server/src/nearpilot/iot/` — 빈 보고를 포함한 유효 `rssi` 보고의 전달
- `server/src/nearpilot/core/proximity/` — 마지막 보고 시각, 거리 순서 (사후확률 계산은 고치지 않는다)
- `server/src/nearpilot/core/decision/` — 거리 선택자 판정, 하위 요청, 재검사, 응답 묶음, 중복 판단 요약, `request_access`의 거부
- `server/src/nearpilot/core/lifecycle/node_events.py` — 보고 전달
- `server/src/nearpilot/db/` — 마이그레이션 `0012`, 하위 요청 저장과 조회, 판정 기록, 복원
- `server/src/nearpilot/api/mcp/` — 도구 설명, 응답
- `server/src/nearpilot/runtime/` — 설정 로더, 앱 조립
- `server/config/` — 설정 예시
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: 사후확률 계산(`core/proximity/filter.py`의 HMM, 모델 파일, `fit.py`), `nearest:`·`any:`·명시 지정의 판정 규칙, 펌웨어, 시뮬레이터, 대시보드 화면

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `NodeEventSink.on_rssi_report(RssiReport)` | 제공 | IOT → 상태 관리 |
| `ProximityEstimator.distance_order(beacon, candidates, at)` | 제공 | 근접 추정 → 판정 |
| `Repository.link_selector_child`, `Repository.selector_children` | 제공 | DB → 판정 |
| `Repository.save_decision`의 `DecisionRecord` 거리 순서 필드 | 제공 | DB → 판정 |
| `CoreService.request_use`의 `Decision.devices`, `selected_count`, `similar` | 제공 | 판정 → API |
| `ExecutionService.execute(ExecutionContext)` | 사용 | 상태 관리 |
| `DecisionRechecker.recheck(ExecutionContext)` | 제공 | 판정 → 상태 관리 |
| `ExecutionStore.cancel_not_sent` | 사용 | DB |

### 만들지 않는 것

- 근접 모델 교체, 자동 영점, 근접 참여 기기를 FR-16 전체 상태에 적용하는 것 (#252)
- 모델 파일에 없는 `trusted` 기기의 `ModelMismatchError` 처리 (`specs/core-device-actions` 후속). `rental` 기기를 고른 거리 선택자 요청은 `nearest:`와 같은 조건에서 이 오류를 만난다.
- 끄기와 켜기를 한 요청으로 묶기
- `find_available_devices`에 거리 순서를 싣는 것
- 대시보드 화면의 하위 요청 표시

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 거리 선택자의 형식이 틀리다 (`near:light:0:1`, `near:light:1:0`, `near:light:x:2`, `near:light:1`) | API가 `DENY`·`INVALID_INPUT`. 판정을 부르지 않는다 | `test_FR09_selector_malformed_invalid_input` |
| 근접 참여 기기가 하나도 없다 | `DENY`·`NO_CANDIDATE` | `test_FR09_selector_no_participant_no_candidate` |
| 최근 구간 안에 보고가 없는 후보 | 거리 순서에 서지 않는다. 같은 기기를 기기 ID로는 쓸 수 있다 | `test_FR09_selector_non_participant_excluded` |
| 빈 보고만 보낸 기기 | 가장 먼 쪽에 선다 | `test_FR09_selector_empty_report_keeps_participant_last` |
| 개수보다 기기가 적다 | 있는 만큼 실행한다. `selected_count`가 실제 개수다 | `test_FR09_selector_count_shortage_runs_available` |
| 경계의 두 기기가 비슷하게 들린다 | 실행하고 `similar`를 `true`로 담는다 | `test_FR09_selector_similar_boundary_flag` |
| 두 번째 기기가 승인 필요다 | 아무것도 실행하지 않는다. 첫 기기의 예약과 사용분을 취소한다. 판정은 `NEED_APPROVAL` | `test_FR18_selector_one_fails_none_executes` |
| `rental` 기기를 고른 요청의 사후확률이 임계값 미만이다 | `ASK`·`LOW_CONFIDENCE`. 아무것도 실행하지 않는다 | `test_FR17_selector_rental_threshold_ask` |
| `rental` 종류에 `threshold_{kind}`가 없다 | `DENY`·`INVALID_INPUT` | `test_FR17_selector_rental_without_threshold_denied` |
| 같은 `request_id`로 재전송한다 | 하위 요청들의 진행 상태와 결과를 묶어 반환한다. 새 명령을 만들지 않는다 | `test_FR22_selector_resend_returns_children_results` |
| 같은 `request_id`에 시작이나 개수가 다른 요청이 온다 | `DENY`·`REQUEST_ID_CONFLICT` | `test_FR22_selector_digest_includes_start_count` |
| 실행 직전에 `shared` 기기의 비콘이 사라졌다 | 그 하위 요청만 막는다. 다른 하위 요청은 실행한다 | `test_FR18_selector_recheck_shared_beacon_only` |
| 실행 직전에 `rental` 기기의 사후확률이 떨어졌다 | 그 하위 요청은 `ASK`·`MOVE_CLOSER` | `test_FR18_selector_recheck_rental_move_closer` |
| 한 기기의 실행이 실패한다 | 다른 기기의 결과는 그대로다. 응답의 `devices`가 기기별 결과를 담는다 | `test_FR18_selector_partial_exec_failure_reported` |
| 하위 요청을 실행하던 중 서버가 재시작한다 | 복원이 하위 요청의 `UseRequest`를 상위 요청의 외부 ID로 다시 만든다 | `test_FR22_selector_child_restore_uses_parent_request_id` |
| `request_access`에 거리 선택자를 넣는다 | `DENY`·`INVALID_INPUT` | `test_FR09_request_access_rejects_selector` |
| 신뢰하지 않은 기기가 보고한다 | 마지막 보고 시각을 기록하지 않는다 | `test_FR16_report_time_only_trusted` |
| 하위 요청을 판정하던 중 오류가 난다 | 만든 하위 요청의 예약을 취소하고 `INTERNAL_ERROR` | `test_FR18_selector_internal_error_cancels_children` |
| 검사에서 걸린 요청을 같은 `request_id`로 재전송한다 | 같은 판정과 걸린 기기를 반환한다 | `test_FR22_selector_resend_after_check_failure` |

## 6. 결정

1. 정렬 값은 최근 구간 안에서 그 비콘 RSSI의 평균이다 — 기기 보고 값이 1초 평균이라 같은 방식을 쓴다. 평균과 강한 쪽 값의 비교는 실험(#252)에서 한다.
2. 하위 요청들은 동시에 실행한다 — 한 기기의 실행은 결과를 기다리며 막힌다. 차례로 실행하면 기기 수만큼 응답이 늦어진다 (NFR-01).
3. 상위 요청의 판정은 모든 하위 요청이 실행에 성공하면 `OK`다. 하나라도 실행 단계에서 막히거나 실패하면, 거리 순서에서 처음 실패한 기기의 판정과 사유다. `devices`는 항상 기기별 결과를 담는다 — 호스트가 "일부만 됨"을 판정만 보고도 알 수 있다.
4. 응답 키는 `devices`(`node_id`, `verdict`, `reason`, `use_id`), `selected_count`, `similar`다. 거리 선택자 요청의 상위 `node_id`와 `use_id`는 `null`이다 — 기존 키의 뜻을 바꾸지 않는다.
5. `similar`는 경계 두 기기의 정렬 값 차이로 정한다. 둘 다 비콘을 못 봤으면 비슷하다. 하나만 못 봤으면 비슷하지 않다.
6. `far` 순서는 `near` 순서를 뒤집은 것이다 — 같은 상황에서 두 방향의 결과가 서로 맞는다.
7. `request_access`는 거리 선택자를 받지 않는다 — 승인은 기기 하나에 대한 것이다.
8. IOT는 유효한 `rssi` 메시지마다 `on_rssi_report`를 한 번 부른다. 근접 추정은 `trusted` 기기의 마지막 보고 시각만 메모리에 둔다. 서버를 다시 시작하면 다음 보고가 올 때까지 그 기기는 거리 순서에 없다.
9. 하위 요청 행은 상위 요청의 `target_spec`, 고른 기기의 `node_id`, 비어 있는 외부 `request_id`, `selector_parent_id`를 가진다. 메모리의 `UseRequest.request_id`는 상위 요청의 외부 ID다 — 재검사가 요청 ID를 대조한다.
10. 하위 요청도 판정 한 건으로 보고 판정 기록을 저장한다 (FR-18, FR-22).
11. 판정은 거리 순서대로 하위 요청마다 ③(`rental`만), 동작·인자, ④, 예약을 하고 처음 걸린 곳에서 멈춘다.
12. 검사에서 걸리면 응답의 `devices`는 처음 걸린 기기 하나다 — 승인이 필요하면 호스트가 그 기기 ID로 승인을 요청해야 한다. 고르기 전에 끝나면 `devices`는 비고 `selected_count`는 0, `similar`는 `false`다.
13. 옆 기기 때문에 취소한 하위 요청의 판정 기록은 `OK`로 남고 실행 기록은 미전송 취소다. 취소 근거는 `source: selector_sibling`과 걸린 기기다 — 단일 요청이 실행 직전 재검사에서 막힌 경우와 같은 모양이다.
14. 상위 요청의 판정 기록은 만든 하위 요청의 `internal_request_id` 목록도 담는다. 재전송은 이 목록으로 하위 요청을 찾는다 — 내부 오류 뒤 재판정(#76)에서 앞 시도의 하위 요청이 섞이지 않는다. 호스트 응답에는 넣지 않는다.
15. 한 하위 요청의 실행이 예외로 끝나면 그 기기만 `INTERNAL_ERROR`다. 다른 기기의 결과는 그대로다.
16. 근접 추정은 `ProximityEstimator.observe_report`로 보고 시각을 받는다. 최근 구간과 정렬 값의 구간은 서버 수신 시각으로 자른다 — ① 비콘 검사와 같은 기준이다. 정렬 값은 요청 시각까지 받은 프레임만 쓴다.
17. IOT는 빈 보고도 기존 `rssi` 보고와 같은 시각·연결·순번 검사를 거친다. `decode_rssi_report`를 더하고 `decode_message`의 반환은 그대로 둔다. 빈 보고는 기기의 `last_seen_at`을 바꾸지 않는다.
18. 기존 판정 입구(`decide_for_execution`)는 거리 선택자를 `INVALID_INPUT`으로 거부한다. 거리 선택자는 `decide_selector`만 처리한다.
19. 통합 시험은 runtime 시험 세계에 시험 안에서만 `light-3`을 더한다 — 다른 runtime 시험의 기기 목록을 바꾸지 않는다.

## 7. 가정

- 근접 참여 기기의 최근 구간 설정은 `anchor_recent_sec`, 초기값 3초, 허용 범위는 `rssi_frame_sec` 이상이다 — #252
- "비슷함" 기준 설정은 `order_similar_db`, 초기값 3 dB, 허용 범위는 0 초과다 — #253 (실험으로 정한다)
- 공개 규칙에 빈 보고 의무가 들어간다 — #252. 그 전까지 빈 보고를 보내지 않는 기기는 비콘이 안 보이면 거리 순서에서 빠진다. 팀 펌웨어는 빈 보고를 보낸다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `test_FR09_selector_parse_near_far_all` | 문법 해석과 직렬화가 왕복한다. 기존 세 형식의 해석은 그대로다 |
| FR-09 | `test_FR09_selector_malformed_invalid_input` | 틀린 형식은 API에서 `INVALID_INPUT` |
| FR-09 | `test_FR09_tool_description_mentions_selector` | 도구 설명에 문법과 예가 있다. 입력 스키마는 그대로다 |
| FR-09 | `test_FR09_selector_near_picks_closest_two` | `near:light:1:2`가 가까운 두 기기를 실행한다 |
| FR-09 | `test_FR09_selector_far_picks_farthest` | `far:light:1:1`이 가장 먼 기기를 실행한다 |
| FR-09 | `test_FR09_selector_start_two_until_end` | `near:light:2:all`이 첫 기기를 뺀 나머지를 실행한다 |
| FR-09 | `test_FR09_selector_count_shortage_runs_available` | 모자라면 있는 만큼, `selected_count` |
| FR-09 | `test_FR09_selector_no_participant_no_candidate` | 고를 기기가 없으면 `NO_CANDIDATE` |
| FR-09 | `test_FR09_selector_non_participant_excluded` | 보고가 끊긴 후보는 순서에 없다 |
| FR-09 | `test_FR09_selector_empty_report_keeps_participant_last` | 빈 보고 기기는 가장 먼 쪽 |
| FR-09 | `test_FR09_selector_tie_by_node_id` | 정렬 값이 같으면 기기 ID 순서 |
| FR-09 | `test_FR09_selector_similar_boundary_flag` | 경계 차이가 기준보다 작으면 `similar` |
| FR-09 | `test_FR09_selector_response_devices_keys` | 응답의 `devices`, `selected_count`, `similar`. 하위 요청 ID와 사용자 ID가 없다 |
| FR-09 | `test_FR09_existing_response_keys_unchanged` | 다른 대상 지정의 응답 키가 그대로다 |
| FR-09 | `test_FR09_iot_empty_rssi_report_handed_over` | IOT가 빈 보고도 `on_rssi_report`로 넘긴다 |
| FR-09 | `test_FR09_request_access_rejects_selector` | `request_access`의 거리 선택자는 `INVALID_INPUT` |
| FR-09 | `test_FR09_selector_lights_real_db` | 실제 DB에서 조명 세 대 중 두 대를 실행하고 재전송한다 (통합) |
| FR-09 | `test_FR09_settings_selector_values_validated` | 새 설정의 기본값, 허용 범위, 옛 설정 스냅샷 |
| FR-09 | `test_FR09_node_events_forward_rssi_report` | 상태 관리가 보고를 근접 추정에 넘긴다 |
| FR-09 | `test_FR09_selector_old_entry_rejects_selector` | 기존 판정 입구는 거리 선택자를 거부한다 |
| FR-16 | `test_FR16_distance_order_mean_of_recent_window` | 정렬 값은 최근 구간의 평균이다. 구간 밖 프레임은 쓰지 않는다 |
| FR-16 | `test_FR16_report_time_only_trusted` | 신뢰한 기기의 보고만 기록한다 |
| FR-17 | `test_FR17_selector_shared_skips_posterior` | `shared` 기기만 고른 요청은 `posterior`를 부르지 않는다. `threshold_{kind}`가 없어도 된다 |
| FR-17 | `test_FR17_selector_rental_threshold_ask` | `rental` 기기는 임계값 미만이면 `ASK` |
| FR-17 | `test_FR17_selector_rental_without_threshold_denied` | `rental` 종류에 임계값이 없으면 `INVALID_INPUT` |
| FR-18 | `test_FR18_selector_children_follow_single_path` | 하위 요청마다 예약, 판정 기록, 실행 인계가 있다 |
| FR-18 | `test_FR18_selector_one_fails_none_executes` | 하나가 걸리면 앞 하위 요청의 예약을 취소하고 아무것도 인계하지 않는다 |
| FR-18 | `test_FR18_selector_recheck_shared_beacon_only` | `shared` 하위 요청의 재검사는 비콘만 본다 |
| FR-18 | `test_FR18_selector_recheck_rental_move_closer` | `rental` 하위 요청의 재검사는 임계값을 본다 |
| FR-18 | `test_FR18_selector_partial_exec_failure_reported` | 한 기기의 실패가 다른 기기를 되돌리지 않고 응답에 기기별로 나온다 |
| FR-18 | `test_FR18_selector_children_run_concurrently` | 하위 요청들을 동시에 실행한다 |
| FR-18 | `test_FR18_selector_internal_error_cancels_children` | 판정 오류 뒤 만든 하위 요청의 예약을 취소한다 |
| FR-22 | `test_FR22_selector_parent_record_has_order` | 상위 판정 기록에 거리 순서, 정렬 값, 고른 기기, `similar` |
| FR-22 | `test_FR22_selector_resend_returns_children_results` | 재전송이 하위 결과를 묶어 반환한다 |
| FR-22 | `test_FR22_selector_resend_after_check_failure` | 검사에서 걸린 요청의 재전송이 같은 판정과 걸린 기기를 반환한다 |
| FR-22 | `test_FR22_selector_record_round_trip` | 거리 선택자 기록과 하위 요청을 저장하고 다시 읽는다 |
| FR-22 | `test_FR22_selector_digest_includes_start_count` | 시작이나 개수가 다르면 충돌 |
| FR-22 | `test_FR22_existing_digest_unchanged` | 기존 대상 지정의 중복 판단 요약이 그대로다 |
| FR-22 | `test_FR22_selector_child_rows_link_parent` | 하위 요청 행이 상위 연결을 갖고 외부 `request_id`가 비어 있다 |
| FR-22 | `test_FR22_selector_child_restore_uses_parent_request_id` | 복원이 하위 요청을 상위 외부 ID로 다시 만든다 |
