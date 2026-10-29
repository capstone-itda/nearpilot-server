# 스피커 음량 제어

- 상태: 완료
- 담당: 작업 지시자 (CORE, API, DB, IOT, 펌웨어 연결 구현)
- 요구사항: FR-04, FR-06, FR-07, FR-09, FR-18, FR-19, FR-20, FR-22, NFR-07, NFR-10
- 따르는 결정: 2026-10-29 담당자 결정. 이슈 없이 담당자가 정했다. 같은 PR에서 docs에 반영한다.

## 1. 왜

- 방문자가 "스피커 소리 좀 줄여 줘"라고 요청해도 서버는 재생과 정지만 허가한다. 음량을 전달할 계약이 없다.
- speaker-1 펌웨어는 부팅할 때 DFPlayer 최대 음량 30을 한 번 보낸다. 모듈 전원에 커패시터가 없어서 큰 음량의 순간 전류를 막을 수 없다. 음량 상한이 필요하다.

## 2. 무엇

- 입력: `request_use`의 선택 인자 `volume`(정수). 스피커 기기 발표의 `controls`.
- 출력: 서버가 정한 음량을 담은 `cmd.parameters`. 또는 `DENY`와 `INVALID_INPUT`, `UNSUPPORTED_PARAMETER`, `PARAMETER_OUT_OF_RANGE`.
- 바뀌는 상태: `node.speaker_controls`, `node_command.speaker` (마이그레이션 `0009`). 펌웨어 명령 기록의 내용에 스피커 `parameters`를 넣는다.
- 약속: 서버와 펌웨어가 각자 음량 상한을 검사한다. 범위 밖 값은 보정하지 않고 거부한다. 재생 중인 스피커의 새 `on`은 곡을 이어서 재생하고 음량만 바꾼다.

## 3. 제약

- k는 발표한 `volume_max`와 `speaker_volume_max`(20) 중 작은 값이다. `volume`은 0 이상 k 이하의 정수다 (FR-09).
- 생략한 음량은 그 기기의 마지막 성공 `on` 명령의 음량을 따른다. 없으면 `speaker_default_volume`(20)을 쓴다 (FR-09).
- 지원하지 않는 인자로 다른 기기를 고르지 않는다. ③에서 대상을 정한 뒤 검사한다 (FR-18).
- 음량 0의 `on`은 `on`이다. 동작을 바꾸지 않는다 (FR-09, FR-20).
- 같은 `request_id`에 다른 음량이 오면 `REQUEST_ID_CONFLICT`다. 같은 `command_id`에 다른 `parameters`가 오면 펌웨어는 `COMMAND_ID_CONFLICT`다 (FR-22, architecture.md §5 MQTT 4절).
- 인자 없는 요청의 정규화 입력과 인자 없는 명령의 기기 기록 내용은 기존 값과 같다. 조명 설정이 있는 요청과 명령의 값도 기존과 같다.
- 명령은 저장한 명령 기록의 음량으로 만든다. 재전송과 재시작이 값을 바꾸지 않는다 (FR-19).
- 펌웨어 상한 `NP_SPEAKER_MAX_VOLUME`은 20이다. 부팅 음량 `NP_SPEAKER_VOLUME`은 20 이하다 (NFR-10).

## 4. 경계

### 고치는 곳

- `docs/requirements.md`, `docs/architecture.md`, `docs/glossary.md` — 2026-10-29 담당자 결정 반영
- `server/src/nearpilot/shared/` — 결정이 정한 `SpeakerSetting`, `SpeakerControls`, `UseRequest.speaker`, `NodeCommand.speaker`, `NodeAnnounce.speaker_controls`, `NodeInfo.speaker_controls`, `ExecutionContext.speaker`, `CommandRecord.speaker`, 설정 2개, `Repository.last_speaker_setting`, `ExecutionStore.link_command`의 `speaker` 인자
- `server/src/nearpilot/api/mcp/`, `server/src/nearpilot/runtime/app.py`
- `server/src/nearpilot/core/decision/`, `server/src/nearpilot/core/lifecycle/execution.py`, `server/src/nearpilot/core/dashboard/text.py`
- `server/src/nearpilot/db/`
- `server/src/nearpilot/iot/messages/`
- `server/tools/evaluation/db.py` — 마이그레이션 수와 같은 기대 스키마 버전
- `server/config/`
- `firmware/`
- `specs/firmware-contracts/spec.md` — 바뀐 speaker-1 부팅 음량과 재생 중 `on`의 설명
- `server/tests/integration/test_server_assembly.py` — `link_command`를 감싸는 테스트 래퍼의 인자
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: REST, WebSocket, 대시보드 응답, 승인 범위(동작 단위 그대로), 조명 설정의 동작, `tools/node_lab`

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `UseRequest.speaker` | 제공 | API → CORE |
| `ExecutionContext.speaker` | 제공 | 판정 → 상태 관리 |
| `ExecutionStore.link_command(..., speaker)`, `CommandRecord.speaker` | 사용 | 상태 관리 → DB |
| `Repository.last_speaker_setting` | 사용 | 판정 → DB |
| `NodeCommand.speaker` → `cmd.parameters` | 제공 | 상태 관리 → IOT → 펌웨어 |
| `announce.controls` → `NodeAnnounce.speaker_controls` | 사용 | 펌웨어 → IOT → 상태 관리 → DB |

### 만들지 않는 것

- 음악 검색, 선곡, 폴더 선택
- 사물함 인자
- 대시보드의 음량 표시
- 실행 직전 재검사의 인자 재검사. 판정과 전송 사이에 상한이 바뀌면 펌웨어가 거부한다.
- 시험 콘솔(`tools/node_lab`)의 음량 입력

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `volume`이 문자열, 실수, 불리언 | `INVALID_INPUT`, 판정 기록 없음 | `test_FR09_speaker_input_rejects_wrong_types` |
| `volume` 음수, 30 초과 | `INVALID_INPUT` | `test_FR09_speaker_input_rejects_out_of_wire_range` |
| `off`나 스피커가 아닌 대상에 `volume`, 스피커 대상에 `brightness`·`color` | `INVALID_INPUT` | `test_FR09_speaker_input_only_for_speaker_on` |
| 대상 기기가 스피커 제어 정보를 발표하지 않음 | `DENY`, `UNSUPPORTED_PARAMETER`, 예약 없음 | `test_FR09_speaker_without_controls_denies` |
| 음량이 k 초과 (k와 k+1 경계) | k는 허가, k+1은 `PARAMETER_OUT_OF_RANGE` | `test_FR09_volume_boundary` |
| 발표 상한이 설정 상한보다 작음 | 작은 값이 k | `test_FR09_volume_cap_is_smaller_of_two` |
| 인자 생략, 앞선 설정 없음 | 설정 기본값으로 완전한 명령 | `test_FR09_volume_omitted_uses_default` |
| 인자 생략, 앞선 성공 `on` 있음 | 앞선 값 유지 | `test_FR09_volume_omitted_keeps_last_setting` |
| 생략으로 정한 기본 음량이 k 초과 | `PARAMETER_OUT_OF_RANGE` | `test_FR09_default_volume_above_device_cap_denies` |
| 앞선 성공 `on`의 음량이 낮춘 k를 넘고 인자 생략 | `PARAMETER_OUT_OF_RANGE`. 방문자가 k 이하의 음량을 준다 (조명 밝기와 같다) | `test_FR09_default_volume_above_device_cap_denies` |
| 근접 지정의 가장 가까운 스피커가 음량을 지원하지 않음 | 다른 스피커를 고르지 않고 `UNSUPPORTED_PARAMETER`, 정책 검사 전 | `test_FR18_unsupported_speaker_does_not_pick_another_device` |
| `off`나 스피커가 아닌 기기 | `speaker` 없는 명령 | `test_FR09_speaker_off_and_other_devices_keep_plain_commands` |
| 런타임 MCP 스키마 | `volume` 최대값이 `speaker_volume_max` | `test_FR09_runtime_schema_uses_speaker_volume_max` |
| 재생 중인 스피커에 새 음량 `on` | 새 명령으로 허가 | `test_FR09_playing_speaker_accepts_new_volume` |
| 같은 `request_id`에 다른 음량 | `REQUEST_ID_CONFLICT` | `test_FR22_same_request_id_other_volume_conflicts` |
| 인자 없는 요청과 조명 요청의 정규화 입력 | 기존 digest와 같음 | `test_FR22_digest_without_speaker_unchanged` |
| 저장한 명령 재구성 | DB의 `speaker`로 같은 `NodeCommand` | `test_FR19_command_speaker_round_trip` |
| 명령 ID로 다시 만든 실행 문맥 | `speaker`가 판정이 넘긴 값과 같음 | `test_FR09_restored_context_keeps_speaker` |
| 발표의 스피커 `controls` 형식 오류, 조명 키를 담은 스피커 발표 | 발표 거부 | `test_FR04_announce_rejects_bad_speaker_controls` |
| 스피커 `controls`만 바뀜 | 신뢰 유지, 감사 기록 | `test_FR04_speaker_controls_change_keeps_trust` |
| 설정 상한·기본값이 범위 밖 | 설정 로드 실패 | `test_FR09_speaker_settings_validate` |
| 조회 응답의 스피커 `controls` | `{"volume_max": k}` | `test_FR09_device_query_speaker_controls` |
| 음량 0 `on`의 성공 | 마지막 제어 상태 `on` | `test_FR20_zero_volume_on_is_on` |
| 펌웨어가 상한 초과 음량 수신 | `UNSUPPORTED_PARAMETER`, 출력 없음 | `test_NFR10_speaker_parameters_above_cap_rejected` |
| 펌웨어가 형식이 틀린 스피커 `parameters` 수신 | `UNSUPPORTED_PARAMETER` | `test_NFR10_speaker_parameters_bad_shape_rejected` |
| 펌웨어가 같은 ID에 다른 음량 수신 | `COMMAND_ID_CONFLICT` | `test_FR22_speaker_parameters_change_conflicts` |
| 펌웨어 재시작 뒤 같은 명령 재전송 | 저장 결과만 보고, 재구동 없음 | `test_FR19_speaker_command_not_rerun_after_restart` |
| 펌웨어가 음량 명령을 실행 요청으로 변환 | 음량을 담은 `Actuation` | `test_NFR10_speaker_actuation_carries_volume` |

## 6. 결정

- 조명 타입 옆에 스피커 전용 `SpeakerSetting`, `SpeakerControls`와 `speaker`·`speaker_controls` 필드를 따로 둔다 — 머지된 조명 코드를 공용 인자로 다시 고치지 않는다.
- MQTT는 기존 필드를 기능별 내용으로 쓴다. 스피커 발표의 `controls`는 `{"volume_max": n}`, 스피커 명령의 `parameters`는 `{"volume": n}`이다. `schema_version`은 2를 유지한다.
- 판정은 ③의 대상 확정 직후 조명 설정과 같은 자리에서 음량을 검사하고 정한다. 감사 단계는 `Step.PROXIMITY`, 감사 키는 `speaker`다.
- 마지막 음량은 그 기기에서 전송 시작이 가장 늦은 `succeeded` `on` 명령의 `speaker`다. 그 명령의 `speaker`가 없으면 기본값을 쓴다 — 조명과 같은 순서 기준이다.
- 스피커 제어 정보를 발표한 기기의 `on`은 인자가 없어도 정한 음량을 명령에 담는다. 그 밖의 기기에는 스피커 `parameters`를 보내지 않는다.
- MCP 입력은 평탄한 선택 인자 `volume`이다. JSON 스키마의 최대값은 `speaker_volume_max`다. 엄격한 타입 검사는 도구 함수 안에서 한다.
- 판정이 넘기는 실행 문맥의 `request.speaker`는 비운다. 정한 값은 `ExecutionContext.speaker`에 담는다 — 조명과 같은 이유다.
- 펌웨어는 재생 중인 스피커의 `on`에 음량 프레임만 보낸다. 재생 중이 아니면 음량 프레임, 간격(`NP_SPEAKER_FRAME_GAP_MS`), 폴더 반복 프레임을 보낸다. `parameters` 없는 `on`은 음량 프레임을 보내지 않는다.
- 설정 초기값은 `speaker_volume_max` 20, `speaker_default_volume` 20이다. 펌웨어 `NP_SPEAKER_MAX_VOLUME`과 부팅 음량 `NP_SPEAKER_VOLUME`도 20이다 — 담당자가 커패시터 없는 speaker-1의 상한으로 정했다.
- 대시보드의 `UNSUPPORTED_PARAMETER`·`PARAMETER_OUT_OF_RANGE` 문구는 기능을 가리지 않는 문구로 바꾼다 ("기기가 받은 인자를 지원하지 않음", "인자가 기기의 상한을 넘음").
- IOT는 `light`와 `speaker`를 함께 담은 명령, 값이 없는 `SpeakerSetting`, `on`이 아닌 명령의 `speaker`를 인코딩하지 않는다 — 한 명령에는 기능 하나의 인자만 담는다.
- 펌웨어는 `parameters` 검사를 기능으로 나눈다. 스피커 문맥은 `{"volume": n}`만 받고, 내용 비교에는 `[volume]`을 붙인다. 출력 직전에도 상한을 다시 보고 넘으면 `SPEAKER_LIMIT_REFUSED`로 실패한다. `COMMAND_HANDLED` 로그에 `volume`(인자가 없으면 -1)을 더한다.
- 기존 펌웨어 `config.local.h`에 `NP_SPEAKER_MAX_VOLUME`이 없으면 빌드가 `#error`로 멈춘다. 부팅 음량이 상한을 넘으면 `static_assert`로 멈춘다.
- 펌웨어 새 주석은 주변 펌웨어 파일처럼 영어로 쓴다.

## 7. 가정

- 상한 20에서 커패시터 없는 speaker-1이 재시작이나 잡음 없이 재생한다는 측정은 하지 않았다 — 담당자 확인 대기.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `tests/shared/test_speaker_models.py` | 모델 검증, 설정 검증 |
| FR-04, FR-09, FR-19 | `tests/db/test_speaker_store.py` | `link_command` 저장, 마지막 음량 조회, 발표 저장과 감사, 마이그레이션, 실행 문맥 재구성 |
| FR-04, FR-06 | `tests/iot/test_speaker_messages.py` | `controls` 해석, `parameters` 인코딩 |
| FR-09, FR-18, FR-22 | `tests/core/test_speaker_setting.py` | 지원·상한 검사, 생략 값, 경계, 인계, digest |
| FR-09 | `tests/core/test_device_query.py` | 조회 응답 `controls` |
| FR-20 | `tests/core/lifecycle/test_shared_light.py` | 음량 0 `on`의 성공이 유지 대상자를 더한다 |
| FR-09 | `tests/api/test_mcp_speaker.py` | 입력 스키마, 타입·형식·대상 검사 |
| FR-09, FR-19, FR-22 | `tests/integration/test_speaker_volume_flow.py` | MCP → CORE → DB → MQTT 경로, 결과 인계, 생략 값 유지, 거부 시 명령 없음 |
| NFR-10, FR-19, FR-22 | `firmware/test/test_common/test_command.cpp`, `test_executor.cpp` | 펌웨어 검증, 내용 비교, 실행 요청 변환, 재시작 뒤 재구동 없음 |

### 확인 (2026-10-29, macOS)

- 서버 전체 `pytest`: 4,413개 통과, 4개 생략, 1개 예상 실패.
- 펌웨어 `pio test -e native` 63개 통과. `esp32dev`, `esp32dev-light-1`, `esp32dev-locker-1`, `esp32dev-speaker-1` 빌드 성공. speaker-1은 RAM 57,652바이트, Flash 1,104,525바이트다.
- speaker-1 실기기에는 올리지 않았다. 음량 프레임 순서와 `SPEAKER_LIMIT_REFUSED`는 빌드와 코드 검토로만 확인했다.
