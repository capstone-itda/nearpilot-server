# 기기 선언 발표의 검증과 저장

- 상태: 완료
- 담당: 작업 지시자 (shared 선언 모델, IOT, DB, 상태 관리 연결 구현)
- 요구사항: FR-04, FR-06, NFR-07
- 따르는 결정: #245 (decision PR #246). 후속 PR ②~④의 첫 단계다 (`architecture.md` §4 "기기 자기 선언의 후속 반영").

## 1. 왜

- 서버는 기기 선언(`nearpilot.device/1`)을 담은 `announce` v3를 받지 못한다. v2의 `capability`, `occupancy`, `controls`만 읽는다.
- 서버는 선언 해시를 저장하지 않아 신뢰를 선언에 묶을 수 없다. 기능과 점유 방식이 같으면 동작 목록이 바뀌어도 신뢰가 유지된다.
- IOT는 기기가 선언한 동작과 인자를 모른 채 명령을 보낸다.

## 2. 무엇

- 입력: `announce` v3(`descriptor`)와 v2(`capability`, `occupancy`, `controls?`).
- 출력: `NodeAnnounce.descriptor`(`DeviceDescriptor`). 기기가 마지막으로 발표한 버전의 `cmd`(v3 `arguments`, v2 `parameters`). 선언에 맞지 않는 명령은 보내지 않고 `NOT_SENT`.
- 바뀌는 상태: `node.descriptor`, `node.descriptor_hash`. 기존 `node.light_controls`, `node.speaker_controls`는 선언으로 옮기고 지운다. 거부한 발표의 감사 행.
- 약속: 선언 해시가 같은 재발표는 신뢰를 바꾸지 않는다. 해시가 바뀌면 `discovered`로 돌리고, 진행 중인 점유가 있으면 `FAULT`로 격리한다. `vendor`만 바뀌면 신뢰를 유지하고 감사에 남긴다. 기존 기기의 해시는 v2 템플릿과 같아서 다시 신뢰하지 않아도 된다.

## 3. 제약

- 형식 버전, 허용 키워드 9개, 깊이 1, 크기 상한, 이름 규칙, 점유 방식과 역할을 검증한다. 실패하면 발표를 거부하고 감사 로그에 남긴다 (FR-04, `device-protocol.md` 3·4·5절).
- 서버가 모르는 점유 방식, 역할, 스키마 키워드는 거부한다. 무시하지 않는다 (`device-protocol.md` 6절).
- `shared` 기기는 `deactivate` 역할을 정확히 하나 선언하고 그 동작은 인자를 받지 않는다. `reoperate`는 `rental`만, `rental`의 `deactivate`는 거부한다 (FR-09, #245 담당자 결정).
- `announce` 전체는 2048바이트 이하다 (`architecture.md` §5 MQTT 3절).
- 선언 해시는 `kind`, `occupancy`, `actions` 전체로 계산한다. `vendor`는 넣지 않는다 (FR-04).
- 전환 기간에는 v2 발표도 받아 종류별 선언 템플릿으로 선언을 만든다. 템플릿은 참조 펌웨어가 내장할 선언과 같다 (FR-04).
- `cmd`는 기기가 마지막으로 발표한 `schema_version`으로 보낸다. v2 기기에는 `schema_version` 2와 `parameters`로 보낸다. 값은 `arguments`와 같다 (`architecture.md` §5 MQTT 3절).
- IOT는 보내기 전에 선언으로 동작과 인자를 다시 검증한다. 맞지 않으면 보내지 않고 `NOT_SENT`다 (FR-06).
- 진행 중인 점유(`RESERVED`, `ACTIVE`, `CLOSING`)가 있는 기기의 선언 해시가 바뀌면 `isolate_device`로 격리한다 (FR-04, NFR-04).
- 판정의 근거는 바꾸지 않는다. 이 작업은 판정 분기를 추가하지 않는다 (NFR-07).
- 기존 기기의 MQTT 계정, 포트, 비밀번호와 기기 목록은 그대로 둔다 (`architecture.md` §4 "기기 접속 설정 묶음").

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/shared/` — 결정이 정한 `DeviceDescriptor`, `ActionSpec`, 동작 역할, `NodeAnnounce.descriptor`, `NodeInfo`의 선언과 해시, 선언 검증·인자 검증·해시·v2 템플릿
- `server/src/nearpilot/iot/messages/`, `server/src/nearpilot/iot/mqtt/`
- `server/src/nearpilot/db/` — `node`의 선언 열, 이전, 발표 저장
- `server/src/nearpilot/core/lifecycle/node_events.py` — 해시 변경의 격리
- `server/src/nearpilot/runtime/assembly.py` — 거부한 발표의 감사 연결
- `server/tools/evaluation/db.py` — 기대 스키마 버전
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: 판정(`core/decision/`), API, 대시보드, 펌웨어, 시뮬레이터, 앱, 기기 접속 설정 묶음. 각각 PR ②~④가 맡는다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `announce.descriptor` → `NodeAnnounce.descriptor` | 제공 | 펌웨어 → IOT → 상태 관리 → DB |
| `NodeEventSink.on_announce(NodeAnnounce)` | 사용 | IOT → 상태 관리 |
| `Repository.upsert_announced_node` | 사용 | 상태 관리 → DB |
| `NodeInfo.descriptor`, `NodeInfo.descriptor_hash` | 제공 | DB → 판정·상태 관리 |
| `NodeCommandGateway.send` → `cmd` | 제공 | 상태 관리 → IOT → 펌웨어 |
| `AuditLog.append` (거부한 발표) | 사용 | IOT → DB |

### 만들지 않는 것

- 판정의 선언 기반 동작·인자 검증, `arguments` 입력, 종류별 설정 — PR ②
- `Capability`, `Action` 열거형과 조명·스피커 인자 모델의 제거 — PR ②
- `node.capability` 열의 `kind` 이름 변경 — PR ②
- 대시보드의 선언 표시 — PR ③
- 펌웨어 선언 내장, 시뮬레이터 v3, BLE 설정 키 정리 — PR ④
- v2 발표 수신의 종료 — 팀 기기를 모두 올린 뒤 fix PR

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 선언에 모르는 키워드(`format`, 최상위, 동작, 스키마) | 발표 거부 | `test_FR04_descriptor_rejects_unknown_keywords` |
| 모르는 `occupancy`나 `role` | 발표 거부 | `test_FR04_descriptor_rejects_unknown_occupancy_and_role` |
| 형식 버전이 다르거나 형식 문자열이 틀림 | 발표 거부 | `test_FR04_descriptor_rejects_unsupported_format` |
| 크기 상한의 경계 (동작 8·9개, 이름 32·33바이트, 설명 120·121바이트, 인자 8·9개, `enum` 16·17개, `pattern` 64·65바이트) | 상한은 받고 넘으면 거부 | `test_FR04_descriptor_size_limits` |
| 이름 규칙 위반 (대문자, 숫자 시작, `:`) | 발표 거부 | `test_FR04_descriptor_rejects_bad_names` |
| 중첩 인자(객체, 배열)나 최상위 `type`이 `object`가 아님 | 발표 거부 | `test_FR04_descriptor_rejects_nested_schema` |
| `shared`에 `deactivate` 없음·두 개, 인자 받는 `deactivate` | 발표 거부 | `test_FR04_shared_requires_one_plain_deactivate` |
| `rental`의 `deactivate`, `shared`의 `reoperate` | 발표 거부 | `test_FR04_role_occupancy_mismatch_rejected` |
| `announce` 원문 2048바이트 초과 | 발표 거부 | `test_FR04_announce_over_2048_bytes_rejected` |
| v2 발표 | 템플릿 선언으로 변환, 해시가 템플릿과 같음 | `test_FR04_v2_announce_becomes_template` |
| 거부한 발표 | 서버 로그와 `announce` 감사 한 행, 같은 연결의 같은 사유는 한 번 | `test_FR04_rejected_announce_audited_once` |
| 처음 보는 기기 | `discovered`로 선언과 해시 저장 | `test_FR04_new_node_stored_with_descriptor` |
| 같은 해시 재발표 | 신뢰와 감사 변화 없음 | `test_FR04_same_hash_keeps_trust` |
| `vendor`만 바뀜 | 신뢰 유지, 새 값 저장, 감사 | `test_FR04_vendor_change_keeps_trust` |
| 설명만 바뀜 | 해시가 바뀌어 `discovered`, 감사 | `test_FR04_description_change_resets_trust` |
| 점유 중 해시 변경 | `FAULT` 격리와 복구 건 | `test_FR04_hash_change_while_occupied_isolates` |
| 기존 DB 이전 | 템플릿 선언·해시, 신뢰 유지, 제어 정보 열 제거 | `test_FR04_migration_backfills_descriptor` |
| 이전한 기기가 v2로 다시 발표 | 해시가 같아 신뢰 유지 | `test_FR04_migrated_node_v2_reannounce_keeps_trust` |
| v3로 발표한 기기에 명령 | `schema_version` 3, `arguments` | `test_FR06_v3_node_gets_v3_command` |
| v2로 발표한 기기에 명령 | `schema_version` 2, `parameters` | `test_FR06_v2_node_gets_v2_command` |
| 선언에 없는 동작이나 스키마 밖 인자 | 보내지 않고 `NOT_SENT` | `test_FR06_command_outside_descriptor_not_sent` |
| 대상 연결의 발표를 받지 못함 | 보내지 않고 `NOT_SENT` | `test_FR06_command_without_announce_not_sent` |
| 지금 서버가 모르는 종류의 v3 발표 | 거부, 감사 (결정 6) | `test_FR04_unknown_kind_rejected_until_generalized` |

## 6. 결정

1. 선언 검증, 인자 검증, 해시, v2 템플릿은 `shared/descriptor.py`의 순수 함수로 둔다 — IOT, DB, 판정(PR ②)이 같은 규칙을 쓰고 `shared`에만 의존한다.
2. 선언 해시는 `{"kind", "occupancy", "actions"}`를 `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`로 직렬화한 UTF-8의 SHA-256 소문자 hex다. 동작 순서는 선언 순서를 지키고, 없는 `role`은 키를 넣지 않는다 — 정렬된 키와 구분자로 같은 선언은 같은 해시가 된다.
3. v2 템플릿은 아래와 같다. `result`는 모두 `{"kind": "signal_output"}`, `required`는 `[]`, `additionalProperties`는 `false`다. `vendor`는 `{"name": "nearpilot", "model": <capability>, "firmware": "v2"}`다.
   - `locker`: `open` "사물함을 연다", `close` "사물함을 잠근다. 반납이 아니다" (`reoperate`)
   - `light`: `on` — 제어 정보가 있으면 `brightness`(`integer`, 0~`brightness_max`)와 색 지원 시 `color`(`string`, `^#[0-9A-Fa-f]{6}$`). 설명은 "조명을 켜고 밝기와 색을 정한다" / "조명을 켜고 밝기를 정한다" / "조명을 켠다". `off` "조명을 끈다" (`deactivate`)
   - `speaker`: `on` — 제어 정보가 있으면 `volume`(`integer`, 0~`volume_max`). 설명은 "스피커를 재생하고 음량을 정한다" / "스피커를 재생한다". `off` "스피커를 정지한다" (`deactivate`)
   - 역할 규칙에 맞지 않는 v2 조합(예: `shared` 사물함)은 템플릿 검증에서 거부한다 — 팀 기기는 사물함 `rental`, 조명·스피커 `shared`다.
4. 인자 스키마의 `type`은 `integer`, `number`, `string`, `boolean`이다. `minimum`·`maximum`은 수 타입에만, `pattern`·`maxLength`는 `string`에만 쓴다. `pattern`은 Python `re`로 컴파일되어야 하고 검사는 `re.search`다 — JSON Schema의 `pattern`은 앵커 없는 검색이다. `additionalProperties`가 없거나 `true`면 선언 밖 인자도 평탄한 값이면 받는다 — JSON Schema의 의미를 그대로 따른다. 그래서 `deactivate` 동작은 `properties`가 비어 있고 `additionalProperties`가 `false`여야 "인자를 받지 않는다"를 만족한다.
5. 인자 검증 결과는 `OUT_OF_RANGE`(`minimum`·`maximum`만 위반)와 `INVALID`(그 밖)로 나눈다. 둘 다 있으면 `INVALID`다 — 판정(PR ②)의 `PARAMETER_OUT_OF_RANGE`와 `INVALID_INPUT`에 그대로 대응한다.
6. 이 PR은 `Capability` 열거형에 있는 종류(`locker`, `light`, `speaker`)의 v3 발표만 받는다. 다른 종류는 거부하고 감사에 남긴다 — 판정·조회·대시보드가 아직 열거형을 쓴다. PR ②가 열거형을 걷어 내며 이 검사를 지운다.
7. `NodeInfo`는 `descriptor`와 `descriptor_hash`를 더 담는다. `light_controls`, `speaker_controls`는 DB가 선언에서 계산해 채우는 호환 값으로 PR ②에서 지운다. 조명 `on`의 `brightness`가 1~255 정수 상한이면 조명 제어 정보, 스피커 `on`의 `volume`이 1~30 정수 상한이면 스피커 제어 정보다. 그 밖은 `None`이다.
8. `node.capability` 열은 이 PR에서 종류 이름을 담는다. `kind`로의 이름 변경은 PR ②가 `nodes(kind)`와 함께 한다 — 열 이름을 쓰는 테스트 고정값이 많다.
9. 이전은 `0010_device_descriptor.sql`이 열을 더하고, `migrate()`의 버전별 Python 후처리가 템플릿으로 선언과 해시를 채운 뒤 `light_controls`, `speaker_controls`를 지운다 — SQLite에 SHA-256이 없다. `db-repository` spec의 "마이그레이션 도구를 추가하지 않는다"의 예외는 이 후처리 하나다.
10. 선언이 없는 `node` 행(이전 전 행, 테스트 고정값)은 `descriptor=None`이다. 이 행에 발표가 오면 종류와 점유 방식만 비교해서 같으면 신뢰를 유지하고 선언을 저장한다 — 이전과 같은 규칙이다.
11. IOT는 기기 연결마다 마지막으로 받은 발표(가장 큰 `seq`)를 메모리에 둔다. `cmd`의 버전과 송신 전 검증은 명령의 `target_session_id` 연결의 발표를 쓴다. 그 발표가 없으면 `NOT_SENT`다 — 재시작 뒤에는 retained 발표로 다시 채워진다.
12. 거부한 발표의 감사는 `Step.ANNOUNCE`이고 내용은 `node_id`, `session_id`, `reason`이다. 원문은 넣지 않는다. 같은 `(node_id, session_id, reason)`은 프로세스 안에서 한 번만 쓴다 — retained 발표가 다시 올 때마다 감사가 늘지 않게 한다. 토픽을 해석하지 못한 메시지는 감사하지 않고 로그만 남긴다.
13. `encode_command`는 `schema_version`을 키워드 인자로 받는다. 기본값을 두지 않는다 — 버전 선택을 호출자가 빠뜨리지 않게 한다.

## 7. 가정

- 없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `tests/shared/test_descriptor.py` | 형식·키워드·상한·이름·역할 검증, 해시, 템플릿, 인자 검증 |
| FR-04, FR-06 | `tests/iot/test_descriptor_messages.py` | v3·v2 해석, 2048바이트, 버전별 `cmd` 인코딩 |
| FR-04, FR-06 | `tests/iot/test_descriptor_delivery.py` | 거부 감사, 연결별 발표, 송신 전 검증 `NOT_SENT` |
| FR-04 | `tests/db/test_descriptor_store.py` | 저장, 해시·`vendor` 규칙, 감사, 이전 |
| FR-04, NFR-04 | `tests/core/lifecycle/test_node_events.py` | 점유 중 해시 변경의 격리 |
| FR-04, FR-06 | `tests/integration/test_descriptor_announce_flow.py` | v2 기기와 v3 기기가 같은 서버에서 발표·명령. 시뮬레이터는 아직 v2라 v3 명령의 형식만 본다 |

- 바뀐 기존 테스트: 제어 정보만 바뀌면 신뢰를 유지하던 `test_FR04_controls_change_keeps_trust`, `test_FR04_speaker_controls_change_keeps_trust`는 해시 규칙의 `..._resets_trust`로 바꿨다. 조명·스피커 제어 정보를 함께 담는 발표는 선언으로 만들 수 없어 `test_FR04_light_and_speaker_controls_change_each_audited`를 지웠다. 역할 규칙에 맞지 않는 v2 조합은 `test_FR04_v2_role_rule_violations_rejected`가 거부를 본다.
