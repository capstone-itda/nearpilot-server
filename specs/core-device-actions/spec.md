# 선언 기반 판정·인자·명령

- 상태: 진행 중
- 담당: 작업 지시자 (shared, 판정, 상태 관리, DB, IOT, API 연결 구현)
- 요구사항: FR-09, FR-11, FR-17, FR-18, FR-20, FR-22, FR-23, NFR-07
- 따르는 결정: #245 (decision PR #246). `architecture.md` §4 "기기 자기 선언의 후속 반영"의 두 번째 단계다. 첫 단계는 `specs/iot-device-descriptor/spec.md`다.

## 1. 왜

- 판정은 `Capability`(사물함, 조명, 스피커)와 `Action`(`open`, `close`, `on`, `off`) 열거형으로 분기한다. 새 종류의 기기는 선언을 발표해도 코드를 고치기 전에는 쓸 수 없다.
- 동작 인자는 조명의 밝기·색과 스피커의 음량만 따로 받는다. 기기가 선언한 다른 인자는 받을 수 없다.
- 재조작과 이탈 꺼짐을 동작 이름(`close`, `on`, `off`)으로 판단해서, 선언의 역할(`reoperate`, `deactivate`)과 다른 이름의 동작을 처리하지 못한다.
- 근접 임계값, 인자 상한과 기본값, 전역 할당 허용 종류가 종류별 고정 설정이라 새 종류의 설정을 추가할 수 없다.

## 2. 무엇

- 입력: `request_use`의 `arguments`(객체). 동작은 대상 기기가 선언한 동작 이름이다. `target_spec`의 `{kind}`는 선언의 종류 이름이다. 종류별 설정 `threshold_{kind}`, `param_max`, `param_default`, `any_allowed_kinds`.
- 출력: 선언에 없는 동작은 `DENY`·`UNSUPPORTED_ACTION`. 인자 스키마의 `minimum`·`maximum` 위반은 `PARAMETER_OUT_OF_RANGE`, 그 밖의 위반은 `INVALID_INPUT`. 조회 응답은 기기마다 `kind`와 `actions`(`name`, `description`, `input_schema`)를 담는다. `cmd`의 `arguments`(v2 기기에는 `parameters`)는 판정이 정한 인자 그대로다.
- 바뀌는 상태: `node_command.arguments`(마이그레이션 `0011`, 기존 `light`·`speaker`를 옮긴다). `node.capability` 열 이름을 `kind`로 바꾼다.
- 약속: 판정은 선언의 `occupancy`와 동작 `role`로만 뜻을 구분한다. 종류는 대상 선택과 종류별 설정 조회에만 쓴다. 설정만 추가한 새 종류의 기기를 명시 지정으로 실행할 수 있다. 인자 없는 요청의 중복 판단 요약은 이전과 같다.

## 3. 제약

- 판정은 종류와 동작 이름으로 분기하지 않는다. 판정의 근거는 `occupancy`와 `role`이다 (NFR-07, `architecture.md` §4).
- 판정은 대상을 정한 뒤 저장한 선언으로 동작과 인자를 검사한다. 인자를 지원하는 다른 기기를 고르지 않는다 (FR-09).
- 서버는 선언의 범위와 `param_max.{kind}.{action}.{arg}` 중 좁은 범위로 검사한다. 범위 밖 값을 보정하지 않는다 (FR-09).
- 생략한 인자는 같은 기기·같은 동작의 마지막 성공 명령의 값, `param_default.{kind}.{action}.{arg}` 순서로 채운다. 둘 다 없으면 생략한 채 보낸다. 채운 값도 다시 검사한다 (FR-09, #245 담당자 결정).
- `color`처럼 `#RRGGBB` 형식의 인자는 대문자로 정규화한다 (FR-09).
- `reoperate` 동작은 본인 `ACTIVE` 대여의 재조작으로만 실행하고, 승인을 요구하지 않는다. 대여가 없으면 `USE_NOT_FOUND`다 (FR-09).
- 켜기는 역할이 없는 동작의 성공, 꺼짐은 그 기기의 `deactivate` 동작이다. 이탈 꺼짐은 `deactivate` 동작을 인자 없이 요청한다 (FR-20).
- `threshold_{kind}`가 없으면 근접 지정과 전역 할당을 허가하지 않는다. `any_allowed_kinds`에 없는 종류의 `any:` 요청은 거부한다. 명시 지정은 종류별 설정 없이 쓴다 (FR-17, FR-23, §1.2).
- 전역 할당은 같은 종류의 요청끼리만 묶는다 (FR-23).
- API는 `arguments`가 객체인지만 검사한다 (FR-09).
- 조회 응답의 동작 목록에 `vendor`, 선언 해시, 사용자 ID, 점유 주체를 넣지 않는다 (FR-11).
- 판정 한 건에 적용한 설정 전체를 감사 기록에 보존한다. 기존 스냅샷도 복원한다 (§1.2, FR-22).
- 옛 설정 키(`light_brightness_max`, `light_default_brightness`, `light_default_color`, `speaker_volume_max`, `speaker_default_volume`)는 새 형식으로 옮긴다 (§1.2).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/shared/` — `Action`·`Capability` 열거형과 조명·스피커 인자 모델 제거, `arguments`, 종류별 설정, `TargetSpec.device_kind`, `UNSUPPORTED_ACTION`
- `server/src/nearpilot/core/decision/`, `core/policy/`, `core/allocation/`, `core/lifecycle/`
- `server/src/nearpilot/db/` — `node_command.arguments`, `node.kind`, 마지막 성공 인자 조회
- `server/src/nearpilot/iot/` — `cmd`의 `arguments`
- `server/src/nearpilot/api/mcp/`, `api/ws/schema.py` — `arguments` 입력, 조회 응답, 종류·동작 이름 검사
- `server/src/nearpilot/runtime/` — 설정 로더, 앱 조립
- `server/src/nearpilot/core/dashboard/`, `core/proximity/fit.py`, `runtime/provisioning.py` — 열거형을 문자열로 바꾸는 것만
- `server/config/` — 설정 예시
- `server/tools/` — 열거형을 문자열로 바꾸는 것만
- `specs/core-light-control/spec.md`, `specs/core-speaker-volume/spec.md` — 이 spec이 대체한다고 표시한다
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: 근접 추정 계산(`core/proximity/` 중 `fit.py` 밖), 대시보드 키 이름(`capability` → `deviceKind`), 펌웨어, 시뮬레이터, 앱, 기기 접속 설정 묶음의 키

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `UseRequest.arguments` | 제공 | API → 판정 |
| `ExecutionContext.arguments` | 제공 | 판정 → 상태 관리 |
| `NodeCommand.arguments`, `CommandRecord.arguments` | 제공 | 상태 관리 → DB·IOT |
| `Repository.last_success_arguments(node_id, action)` | 제공 | DB → 판정 |
| `ReserveRequest.reoperate_only`, `PolicyChecker.check(..., reoperate=)` | 제공 | 판정 → DB·정책 |
| `NodeInfo.kind`, `NodeInfo.descriptor` | 사용 | DB → 판정·상태 관리 |
| `Settings.threshold_for`, `param_max`, `param_default`, `any_allowed_kinds` | 제공 | 설정 → 판정 |
| `find_available_devices`, `get_device_state`의 `kind`, `actions` | 제공 | API → 호스트 |

### 만들지 않는 것

- 근접 모델 배치가 없는 기기를 근접 지정·전역 할당 후보에서 빼는 것 (FR-05). 근접 추정이 모델에 없는 신뢰 기기를 오류로 보는 검사를 바꿔야 한다. 후속 작업이다.
- 대시보드의 `deviceKind`, `vendor`, `actions`, `descriptorHash` 표시 — PR ③
- 펌웨어 선언 내장, 시뮬레이터 v3, 기기 접속 설정 묶음의 `capability`·`occupancy` 제거 — PR ④
- v2 발표 수신의 종료 — 팀 기기를 모두 올린 뒤 fix PR

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 선언에 없는 동작을 요청한다 | `DENY`·`UNSUPPORTED_ACTION`. 다른 기기를 고르지 않는다 | `test_FR09_undeclared_action_unsupported` |
| 선언이 없는 기기(이전하지 못한 행)를 요청한다 | `DENY`·`UNSUPPORTED_ACTION` | `test_FR09_node_without_descriptor_unsupported` |
| 인자가 `maximum`이나 `param_max`를 넘는다 | `DENY`·`PARAMETER_OUT_OF_RANGE`. 보정하지 않는다 | `test_FR09_argument_over_param_max_out_of_range` |
| 선언하지 않은 인자, 틀린 자료형, 형식 위반 | `DENY`·`INVALID_INPUT` | `test_FR09_argument_schema_violation_invalid` |
| `arguments`가 객체가 아니다 | API가 `INVALID_INPUT`으로 거부한다 | `test_FR09_arguments_not_object_rejected` |
| 생략한 인자를 마지막 성공값으로 채운다 | 같은 기기·같은 동작의 마지막 성공 명령 값을 쓴다 | `test_FR09_omitted_argument_uses_last_success` |
| 마지막 성공이 없고 기본값 설정이 있다 | `param_default` 값을 쓴다 | `test_FR09_omitted_argument_uses_param_default` |
| 기본값 설정도 없다 | 그 인자를 생략한 채 보낸다 | `test_FR09_omitted_argument_without_default_left_out` |
| 채운 값이 줄어든 `param_max`를 넘는다 | `PARAMETER_OUT_OF_RANGE` | `test_FR09_filled_argument_out_of_range` |
| `reoperate` 동작을 대여 없이 요청한다 | 새 점유를 만들지 않고 `USE_NOT_FOUND`. 승인을 요구하지 않는다 | `test_FR09_reoperate_role_without_rental_use_not_found` |
| `close`가 아닌 이름의 `reoperate` 동작 | 이름과 관계없이 재조작으로 처리한다 | `test_FR09_reoperate_role_by_declaration_not_name` |
| 유지 대상자가 모두 떠났고 `deactivate` 동작 이름이 `off`가 아니다 | 선언한 `deactivate` 동작을 인자 없이 요청한다 | `test_FR20_leave_off_uses_declared_deactivate` |
| 마지막 성공 명령이 `deactivate`다 | 이탈 꺼짐을 요청하지 않는다 | `test_FR20_no_leave_off_after_deactivate` |
| `threshold_{kind}`가 없는 종류의 근접 지정 | `DENY`·`INVALID_INPUT` | `test_FR17_kind_without_threshold_denied` |
| `any_allowed_kinds`에 없는 종류의 `any:` | `DENY`·`INVALID_INPUT` | `test_FR23_any_kind_not_allowed_denied` |
| 다른 종류의 `any:` 요청이 같은 구간에 온다 | 종류별로 따로 배정한다 | `test_FR23_window_groups_by_kind` |
| 옛 설정 키를 쓴 설정 파일 | 새 키로 옮겨 읽고 경고를 남긴다. 같은 값을 두 형식으로 쓰면 거부한다 | `test_FR09_legacy_setting_keys_migrated` |
| 옛 형식의 설정 스냅샷을 복원한다 | 새 형식으로 옮겨 복원한다 | `test_FR22_legacy_settings_snapshot_restored` |
| 기존 명령 기록의 `light`, `speaker` | 마이그레이션이 `arguments`로 옮긴다 | `test_FR09_migration_moves_command_arguments` |
| v2로 발표한 기기에 인자 있는 명령 | `parameters`로 같은 값을 보낸다 | `test_FR06_command_follows_announced_version` (PR ①) |

## 6. 결정

1. `Action`과 `Capability` 열거형을 지우고 동작 이름과 종류 이름을 문자열로 쓴다. 이름 규칙은 선언과 같은 `^[a-z][a-z0-9_]*$`, 32바이트 이하다 — 새 종류를 코드 수정 없이 붙인다 (FR-09, NFR-07).
2. `TargetSpec`은 `device_kind`(문자열 또는 `None`)를 담는다. 콜론이 없는 `target_spec`은 기기 ID 그대로인 명시 지정이고 `device_kind`는 `None`이다. 종류는 저장한 기기에서 읽는다 — 기기 ID가 `{종류}-{번호}` 형식이 아니어도 된다. `TargetSpec.kind`는 이미 지정 방식(`TargetKind`)이라 이름을 `device_kind`로 한다.
3. 중복 판단 요약은 기존 키 이름(`target.capability`)을 유지한다. 명시 지정은 기기 ID의 마지막 `-` 앞을 넣는다 — 인자 없는 요청의 요약이 이전과 같다. 인자가 있으면 `arguments`를 넣는다. 업그레이드 직전에 받은 인자 있는 요청을 같은 `request_id`로 다시 보내면 `REQUEST_ID_CONFLICT`다.
4. 근접 임계값은 기존 필드 `threshold_locker`, `threshold_light`, `threshold_speaker`를 그대로 두고, 그 밖의 종류는 `thresholds` 맵에 둔다. 설정 파일은 둘 다 `threshold_{kind}` 키로 쓴다. 판정은 `Settings.threshold_for(kind)`로만 읽는다 — 기존 설정 파일과 스냅샷이 그대로 읽힌다. `getattr`로 읽지 않아서 `threshold_explicit_*` 같은 다른 필드를 종류로 잘못 읽지 않는다.
5. `param_max`, `param_default`는 `{kind: {action: {arg: 값}}}` 맵이다. 설정 파일에서는 `[settings.param_max.light.on]` 표로 쓴다. 기본값은 기존 조명·스피커 설정과 같은 값이다.
6. 판정은 `reoperate_only`를 `ReserveRequest`에, `reoperate`를 `PolicyChecker.check`에 넘긴다. DB와 정책 검사는 동작 이름을 비교하지 않는다.
7. `#RRGGBB` 정규화는 인자 스키마의 `pattern`이 `^#[0-9A-Fa-f]{6}$`인 문자열 인자에 적용한다 — 종류로 분기하지 않는다.
8. 조회 응답의 `capability` 키를 `kind`로, `controls`를 `actions`로 바꾼다. 인자 스키마의 `maximum`은 선언 값과 `param_max` 중 작은 값이다.
9. 조회의 정책 검사 동작은 그 기기가 선언한 첫 번째 역할 없는 동작이다. 없으면 기기를 목록에서 뺀다.
10. 유지 대상자는 켠 동작의 이름을 함께 기억하고, 그 동작으로 유지 권한을 다시 검사한다.
11. 전역 할당 대기 구간은 종류별로 따로 연다. 배정 함수는 바꾸지 않는다.
12. `node.capability` 열 이름을 `kind`로 바꾸고, `node_command.light`·`speaker`를 `arguments`로 옮긴 뒤 지운다(`0011`). 두 열의 키는 `arguments`와 같다.
13. 같은 내부 요청의 재저장 비교는 설정 JSON 문자열이 아니라 복원한 설정으로 한다 — 옛 형식 스냅샷과 새 형식이 같은 값이면 같은 요청이다.
14. 조명 `on`을 선언했지만 `color`를 받지 않는 기기에 흰색(`#FFFFFF`)을 넣어 보내던 동작을 없앤다. 선언에 없는 인자는 보내지 않는다. 팀 조명은 모두 색을 받는다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `tests/core/test_device_actions.py` | 선언 기반 동작·인자 검사, 생략 인자 채우기, 정규화, 역할 기반 재조작 |
| FR-09, FR-11 | `tests/core/test_device_query.py` | 조회 응답의 `kind`, `actions`, `param_max` 적용 |
| FR-09 | `tests/api/test_mcp_arguments.py` | `arguments` 입력 검사와 판정 인계 |
| FR-17, FR-23 | `tests/core/test_device_actions.py` | 종류별 임계값과 전역 할당 허용 종류 |
| FR-23 | `tests/core/allocation/` | 종류별 대기 구간 |
| FR-20 | `tests/core/lifecycle/test_shared_light.py` | 역할 기반 켜기·꺼짐과 이탈 꺼짐 |
| FR-09, FR-22 | `tests/db/test_command_arguments_store.py` | 명령 인자 저장·복원, 마지막 성공 인자, 이전 |
| §1.2 | `tests/runtime/`, `tests/shared/test_config.py` | 종류별 설정 읽기, 옛 키 이전, 스냅샷 복원 |
| FR-09, FR-23, NFR-07 | `tests/integration/test_new_kind_without_code.py` | 설정만 추가한 새 종류의 명시 지정 실행 |
