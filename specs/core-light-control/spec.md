# 조명의 밝기·색 제어

- 상태: 구현 중
- 담당: 작업 지시자 (CORE, API, DB, IOT, 펌웨어 연결 구현)
- 요구사항: FR-04, FR-06, FR-07, FR-09, FR-18, FR-19, FR-20, FR-22, NFR-07, NFR-10
- 따르는 결정: 2026-10-27 담당자 결정 (`decision/light-control`). 이슈 없이 담당자가 정했다.

## 1. 왜

- 방문자가 "가장 가까운 무드등을 파란색, 밝기 30으로 켜 줘"라고 요청하면 서버는 켜기만 허가한다. 밝기와 색을 전달할 계약이 없다.
- light-1 펌웨어는 흰색과 고정 밝기 64만 출력한다.

## 2. 무엇

- 입력: `request_use`의 선택 인자 `brightness`(정수)와 `color`(`#RRGGBB`). 조명 기기 발표의 `controls`.
- 출력: 서버가 정한 밝기와 색을 담은 `cmd.parameters`. 또는 `DENY`와 `INVALID_INPUT`, `UNSUPPORTED_PARAMETER`, `PARAMETER_OUT_OF_RANGE`.
- 바뀌는 상태: `node.light_controls`, `node_command.light`. 펌웨어 명령 기록의 내용에 `parameters`를 넣는다.
- 약속: 서버와 펌웨어가 각자 밝기 상한을 검사한다. 범위 밖 값은 보정하지 않고 거부한다. 인자 일부만 실행하지 않는다.

## 3. 제약

- k는 발표한 `brightness_max`와 `light_brightness_max` 중 작은 값이다 (FR-09).
- 생략한 인자는 마지막 성공 `on` 명령의 조명 설정을 따른다. 없으면 설정 기본값을 쓴다 (FR-09).
- 지원하지 않는 인자로 다른 기기를 고르지 않는다. ③에서 대상을 정한 뒤 검사한다 (FR-18).
- 밝기 0의 `on`은 `on`이다. 동작을 바꾸지 않는다 (FR-09, FR-20).
- 같은 `request_id`에 다른 인자가 오면 `REQUEST_ID_CONFLICT`다. 같은 `command_id`에 다른 `parameters`가 오면 펌웨어는 `COMMAND_ID_CONFLICT`다 (FR-22, architecture.md §5 MQTT 4절).
- 인자 없는 요청의 정규화 입력과 인자 없는 명령의 기기 기록 내용은 기존 값과 같다.
- 명령은 저장한 명령 기록의 조명 설정으로 만든다. 재전송과 재시작이 값을 바꾸지 않는다 (FR-19).
- 펌웨어 채널 출력은 `색 채널 × brightness ÷ 255`(내림)다. 채널 출력은 펌웨어 상한 `NP_LIGHT_RING_MAX_LEVEL`을 넘지 않는다 (architecture.md §5 MQTT 3절).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/shared/` — 결정이 정한 `LightSetting`, `LightControls`, `UseRequest.light`, `NodeCommand.light`, `NodeAnnounce.light_controls`, `NodeInfo.light_controls`, `ExecutionContext.light`, `CommandRecord.light`, 사유 코드, 설정 3개, `Repository.last_light_setting`, `ExecutionStore.link_command`의 `light` 인자
- `server/src/nearpilot/api/mcp/`
- `server/src/nearpilot/core/decision/`, `server/src/nearpilot/core/lifecycle/`
- `server/src/nearpilot/db/`
- `server/src/nearpilot/iot/messages/`
- `server/tools/evaluation/db.py` — 마이그레이션 수와 같은 기대 스키마 버전
- `server/config/`
- `firmware/`
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: REST, WebSocket, 대시보드 응답, 승인 범위(동작 단위 그대로)

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `UseRequest.light` | 제공 | API → CORE |
| `ExecutionContext.light` | 제공 | 판정 → 상태 관리 |
| `ExecutionStore.link_command(..., light)`, `CommandRecord.light` | 사용 | 상태 관리 → DB |
| `Repository.last_light_setting` | 사용 | 판정 → DB |
| `NodeCommand.light` → `cmd.parameters` | 제공 | 상태 관리 → IOT → 펌웨어 |
| `announce.controls` → `NodeAnnounce.light_controls` | 사용 | 펌웨어 → IOT → 상태 관리 → DB |

### 만들지 않는 것

- 음악 검색과 선곡
- 스피커 음량과 사물함 인자
- 대시보드의 조명 설정 표시
- 실행 직전 재검사의 인자 재검사. 판정과 전송 사이에 상한이 바뀌면 펌웨어가 거부한다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `brightness`가 문자열, 실수, 불리언 | `INVALID_INPUT`, 판정 기록 없음 | `test_FR09_light_input_rejects_wrong_types` |
| `brightness` 음수, 255 초과 | `INVALID_INPUT` | `test_FR09_light_input_rejects_out_of_wire_range` |
| `color` 형식 오류 | `INVALID_INPUT` | `test_FR09_light_input_rejects_bad_color` |
| `off`, `open`, `close`나 사물함 대상에 인자 | `INVALID_INPUT` | `test_FR09_light_input_only_for_light_on` |
| 대상 기기가 조명 제어 정보를 발표하지 않음 | `DENY`, `UNSUPPORTED_PARAMETER`, 예약 없음 | `test_FR09_nearest_light_without_controls_denies` |
| 색 미지원 기기에 `color` | `DENY`, `UNSUPPORTED_PARAMETER` | `test_FR09_color_unsupported_denies` |
| 밝기가 k 초과 (k와 k+1 경계) | k는 허가, k+1은 `PARAMETER_OUT_OF_RANGE` | `test_FR09_brightness_boundary` |
| 인자 생략, 앞선 설정 없음 | 설정 기본값으로 완전한 명령 | `test_FR09_omitted_uses_defaults` |
| 인자 생략, 앞선 성공 `on` 있음 | 앞선 값 유지 | `test_FR09_omitted_keeps_last_setting` |
| 생략으로 정한 기본 밝기가 k 초과 | `PARAMETER_OUT_OF_RANGE` | `test_FR09_default_above_device_cap_denies` |
| 켜진 조명에 새 설정 `on` | 새 명령으로 허가 | `test_FR09_lit_light_accepts_new_setting` |
| 같은 `request_id`에 다른 인자 | `REQUEST_ID_CONFLICT` | `test_FR22_same_request_id_other_light_conflicts` |
| 인자 없는 요청의 정규화 입력 | 기존 digest와 같음 | `test_FR22_digest_without_light_unchanged` |
| 저장한 명령 재구성 | DB의 `light`로 같은 `NodeCommand` | `test_FR19_command_light_round_trip` |
| 발표의 `controls` 형식 오류 | 발표 거부 | `test_FR04_announce_rejects_bad_controls` |
| `controls`만 바뀜 | 신뢰 유지, 감사 기록 | `test_FR04_controls_change_keeps_trust` |
| 펌웨어가 상한 초과 밝기 수신 | `UNSUPPORTED_PARAMETER`, 출력 없음 | `test_NFR10_light_parameters_above_cap_rejected` |
| 펌웨어가 같은 ID에 다른 `parameters` 수신 | `COMMAND_ID_CONFLICT` | `test_FR22_parameters_change_conflicts` |
| 펌웨어 재시작 뒤 같은 명령 재전송 | 저장 결과만 보고, 재구동 없음 | `test_FR19_parameters_command_not_rerun_after_restart` |
| 조명 제어 정보가 없는 조명에 인자 | 인자 없는 요청은 그대로 실행, 인자 요청은 `UNSUPPORTED_PARAMETER` | `test_FR09_runtime_light_without_controls_stays_plain` |

## 6. 결정

- 판정은 ③의 대상 확정 직후 조명 설정을 검사하고 정한다. 감사 단계는 `Step.PROXIMITY`다 — 판정 단계를 일곱 칸으로 유지한다.
- 마지막 조명 설정은 그 기기에서 전송 시작이 가장 늦은 `succeeded` `on` 명령의 `light`다. 그 명령의 `light`가 없으면 기본값을 쓴다 — FR-20의 마지막 제어 상태와 같은 순서 기준이다.
- 조명 제어 정보를 발표한 기기의 `on`은 인자가 없어도 정한 값을 명령에 담는다. 그 밖의 기기에는 `parameters`를 보내지 않는다.
- MCP 입력은 평탄한 선택 인자 `brightness`, `color`다. JSON 스키마에 최소·최대값과 패턴을 쓴다. 엄격한 타입 검사는 도구 함수 안에서 한다 — 거부 응답 형식을 기존 `INVALID_INPUT`과 맞춘다.
- 펌웨어는 `parameters`가 있을 때만 기록 내용 끝에 붙인다 — 이전 기록과 내용 비교가 그대로다.
- light-1의 `NP_LIGHT_RING_MAX_LEVEL`은 64다. 인자 없는 `on`은 기존처럼 흰색 64다.

## 7. 가정

- k의 초기 기준 64는 light-1의 보드 5V 출력과 보조배터리 전원에서 흰색 16개 점등을 확인한 값이다. 전류 측정은 하지 않았다 — 담당자 확인 대기.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `tests/api/test_mcp_light.py` | 입력 스키마, 타입·형식·대상 검사, 조회 응답 `controls` |
| FR-09, FR-18 | `tests/core/test_light_setting.py` | 지원·상한 검사, 생략 값, 경계, 인계 |
| FR-22 | `tests/core/test_light_setting.py` | digest 보존과 충돌 |
| FR-04, FR-09, FR-19 | `tests/db/test_light_store.py` | `link_command` 저장, 마지막 설정 조회, 발표 저장과 감사, 마이그레이션 |
| FR-04, FR-06 | `tests/iot/test_light_messages.py` | `controls` 해석, `parameters` 인코딩 |
| FR-20 | `tests/core/lifecycle/test_shared_light.py` | 밝기 0 `on`의 성공이 유지 대상자를 더한다 |
| FR-09, FR-19, FR-22 | `tests/integration/test_light_control_flow.py` | MCP → CORE → DB → MQTT 경로, 생략 값 유지, 거부 시 명령 없음, 같은 `request_id` 충돌 |
| NFR-10, FR-22 | `firmware/test/test_common/test_command.cpp`, `test_executor.cpp` | 펌웨어 검증, 내용 비교, 출력 상한 |

### 실기기 확인 (2026-10-27, light-1, COM9)

- 펌웨어 `esp32dev-light-1`을 올리고 시험 서버(`tools.node_lab`의 Runtime)와 시험 브로커로 확인했다. 요청은 CORE의 `request_use`에 명시 대상 `light-1`로 넣었다. MCP 입력 검사는 통합 테스트로 확인했다.
- 발표에 `controls {"brightness_max": 64, "color": true}`가 실리고 `node.light_controls`에 저장된다.
- 서버를 거친 명령의 출력(`COMMAND_HANDLED`의 `rgb`)과 저장한 `node_command.light`:

| 요청 | 판정 | 출력 `rgb` |
|---|---|---|
| 밝기 30, `#0000FF` | `OK` | `00001E` |
| 밝기 65 | `DENY`, `PARAMETER_OUT_OF_RANGE`, 명령 없음 | — |
| `#FF0000`만 (밝기 생략) | `OK`, 밝기 30 유지 | `1E0000` |
| 인자 없음 | `OK`, 30·`#FF0000` 유지 | `1E0000` |
| 밝기 64, `#FFFFFF` | `OK` | `404040` |
| 밝기 0 | `OK`, 동작 `on` | `000000` |
| `off` | `OK` | `000000` |

- 서버를 거치지 않은 밝기 65 명령은 펌웨어가 `rejected`, `UNSUPPORTED_PARAMETER`로 거부하고 출력하지 않았다.
- 재시작하면 링이 꺼진 채 부팅했다(`initial=off`). 실행한 명령 ID에 다른 내용을 보내자 `COMMAND_ID_CONFLICT`로 거부하고 저장 결과 `succeeded`만 다시 보고했다. 재구동은 없었다.
- 링의 실제 색과 밝기는 눈으로 확인하지 않았다. 전류는 측정하지 않았다.
