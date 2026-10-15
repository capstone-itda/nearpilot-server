# FR-09 기기 조회 (find_available_devices, get_device_state)

- 상태: 완료
- 담당: eunji719 (판정)
- 요구사항: FR-09, FR-11, FR-14
- 따르는 결정: #23, #42

## 1. 왜

- `CoreService`의 `find_available_devices`와 `get_device_state`를 구현한 곳이 없다. MCP 도구 `find_available_devices`, `get_device_state`가 부를 서비스가 없다.
- 후보 목록이 `request_use`와 다른 조건을 쓰면 목록에 나온 기기가 요청에서 거부된다. 목록이 비공개 기기나 점유 주체를 보여 주면 FR-11을 어긴다.

## 2. 무엇

- 입력: 인증된 `user_id` (`get_device_state`는 `node_id`도 받는다)
- 출력: 호스트에 보낼 값. 사용자 ID와 점유 주체가 없다.
- 바뀌는 상태: 없다. 조회만 한다.

### find_available_devices

- `request_use`의 ①②③과 같은 조건으로 후보를 고른다. 비콘 → 장소 → 후보(`select_candidates`) → 근접 확률이 기능별 임계값(`threshold_*`) 이상이다.
- 임계값이 설정에 없는 기능(`threshold_speaker`가 `None`)은 목록에서 뺀다.
- 위 조건에 더해 아래 기기만 남긴다.
  - 미점유다 (`device_state`가 `AVAILABLE`).
  - 공개 기기다 (`visibility`가 `public`).
  - ④ 정책 검사가 `OK` 또는 `NEED_APPROVAL`이다. 검사 동작은 기능별 기본 동작이다.
- 항목마다 `access`를 담는다. ④가 `OK`면 `operable`(조작 가능), `NEED_APPROVAL`이면 `approval_required`(승인 필요)다.
- 비콘을 수신하지 못했으면 빈 목록이다.

| 기능 | 정책 검사 동작 |
|---|---|
| `locker` | `open` |
| `light` | `on` |
| `speaker` | `on` |

| 응답 항목 | 값 |
|---|---|
| `node_id` | 기기 ID |
| `capability` | `locker`, `light`, `speaker` |
| `occupancy` | `rental`, `shared` |
| `access` | `operable`, `approval_required` |

### get_device_state

- 근접 검사 없이 기기 상태를 조회한다.
- 공개 기기는 유효한 토큰의 계정이 모두 조회한다. 비공개 기기는 소유자만 조회한다.
- 없는 기기, 비공개 기기, 권한이 없는 조회는 구분하지 않고 같은 응답(`reason_code`가 `TARGET_UNAVAILABLE`)이다.

| 응답 항목 | 값 |
|---|---|
| `node_id` | 기기 ID |
| `capability` | `locker`, `light`, `speaker` |
| `occupancy` | `rental`, `shared` |
| `state` | `available`, `occupied`, `fault` (`RESERVED`·`ACTIVE`·`CLOSING`은 `occupied`) |
| `online` | 마지막 수신이 `online_ttl_sec` 안이면 `true` |
| `trusted` | 신뢰 상태가 `trusted`면 `true` |
| `access_mode` | `open`, `restricted` |

## 3. 제약

- 사용자 ID, 점유 주체, 근접 확률, 장소 ID, 내부 설정 값을 응답에 넣지 않는다 (FR-11, NFR-08). 응답 키는 위 표의 것뿐이다.
- 비공개 기기는 목록에서 숨긴다 (FR-14).
- 임계값과 시간 값은 설정에서 받는다. 코드 상수로 쓰지 않는다.
- 이 서비스는 저장소를 바꾸지 않는다. 승인 신청을 만들거나 승인 사용분을 예약하지 않는다.
- 판정을 바꾸지 않는다. 근접 확률과 정책 검사 결과를 그대로 쓴다 (NFR-07).
- 같은 입력에는 같은 출력이다. 항목은 근접 확률 내림차순, 같으면 `node_id` 순이다 (NFR-09).
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
| `Repository.beacons_of`, `rssi_since`, `nodes`, `current_policy` | 사용 | DB |
| `ProximityEstimator.posterior` | 사용 | 근접 |
| `PolicyChecker.check` | 사용 | 정책 |
| `find_available_devices(user_id)`, `get_device_state(user_id, node_id)` (`CoreService`) | 제공 | API (`api/mcp`) |

### 만들지 않는 것
- MCP 도구와 응답 직렬화 (`api-mcp-devices` spec)
- `request_access` (`CoreService.request_access`)
- 서버 진입점과 서비스 조립 (#88)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 활성 비콘이 없다 | 빈 목록 | `test_FR09_비콘이_없으면_빈_목록` |
| 최근 비콘 관측이 없다 | 빈 목록 | `test_FR09_비콘_관측이_없으면_빈_목록` |
| 후보가 임계값 미만이다 | 목록에서 뺀다 | `test_FR09_근접_확률이_임계값_미만이면_뺀다` |
| 임계값이 없는 기능(스피커) | 목록에서 뺀다 | `test_FR09_임계값이_없는_기능은_뺀다` |
| 미신뢰·오프라인·FAULT·다른 장소 | 목록에서 뺀다 | `test_FR09_후보_조건을_만족하지_않으면_뺀다` |
| 점유 중이다 | 목록에서 뺀다 | `test_FR09_점유_중인_기기는_뺀다` |
| 비공개 기기다 | 목록에서 뺀다 | `test_FR14_비공개_기기는_목록에서_숨긴다` |
| ④가 `OK` | `access`가 `operable` | `test_FR09_정책이_허용하면_조작_가능` |
| ④가 `NEED_APPROVAL` | `access`가 `approval_required` | `test_FR09_승인이_필요하면_승인_필요` |
| ④가 `DENY` | 목록에서 뺀다 | `test_FR09_정책이_거부하면_뺀다` |
| 한 기기의 정책 조회가 예외를 낸다 | 그 기기만 뺀다 | `test_FR09_정책_조회가_실패한_기기만_뺀다` |
| 근접 계산이 예외를 낸다 | 예외를 올린다 (API가 `INTERNAL_ERROR`로 거부한다) | `test_FR09_근접_계산_예외는_올린다` |
| 활성 비콘이 둘 이상이거나 장소가 하나로 정해지지 않는다 | 예외를 올린다 | `test_FR09_비콘_장소_불변_조건_위반은_예외` |
| 응답 항목 | 허용 목록의 키만 있다 | `test_FR11_목록_항목에_내부_값이_없다` |
| 공개 기기를 조회한다 | 상태 값을 반환한다 | `test_FR09_공개_기기_상태를_반환한다` |
| 비공개 기기를 소유자가 조회한다 | 상태 값을 반환한다 | `test_FR14_비공개_기기는_소유자만_조회한다` |
| 비공개 기기를 다른 계정이 조회한다 | `TARGET_UNAVAILABLE` | `test_FR14_비공개_기기는_타인에게_없는_기기와_같다` |
| 없는 기기를 조회한다 | `TARGET_UNAVAILABLE` | `test_FR14_비공개_기기는_타인에게_없는_기기와_같다` |
| 점유·FAULT·오프라인 기기를 조회한다 | `state`·`online`에 반영한다. 점유 주체는 없다 | `test_FR09_점유_상태는_주체_없이_반환한다` |
| 정책 조회가 예외를 낸다 | 예외를 올린다 | `test_FR09_상태_조회_예외는_올린다` |

## 6. 결정

- 서비스는 `core/decision/query.py`의 `DeviceQueryService` 하나다. 생성자로 `Repository`, `ProximityEstimator`, `PolicyChecker`, `Settings`, `clock`을 받는다. `request_use` 판정 파이프라인과 분리한다 — 조회는 `link_request`와 `save_decision`을 쓰지 않고 감사 기록도 남기지 않기 때문이다.
- 후보 조건은 `pipeline.select_candidates`와 `threshold_for`를 그대로 쓴다. 담당자가 정했다 — 목록에 나온 기기가 `request_use` ②③을 통과하도록 같은 조건을 쓴다.
- '조작 가능'과 '승인 필요'는 기능별 기본 동작으로 `PolicyChecker.check`를 불러 나눈다. 담당자가 정했다. 정책 모드만 보면 이미 승인받은 기기도 '승인 필요'로 보인다.
- `get_device_state`는 최소 필드만 반환하고 비공개·권한 없음·없는 기기를 같은 응답으로 돌려준다. 담당자가 정했다.

## 7. 가정

- 소유자는 현재 정책 리비전의 `approver_user_id`로 판단한다. 소유자 필드가 따로 없다.
- 비공개 기기를 현재 점유 중인 본인도 조회할 수 있다는 요구(requirements FR-09)는 구현하지 않는다. 기기 기준으로 본인의 현재 점유 세션을 조회하는 접점이 `Repository`에 없다. 이 경우도 `TARGET_UNAVAILABLE`로 거부한다.
- 호스트용 사유 코드에 '없는 기기'가 없어서 `TARGET_UNAVAILABLE`을 쓴다.
- `get_device_state`의 `state`에 본인 점유 여부를 넣지 않는다. 같은 접점이 없다.
- 미결: 위 두 가정을 풀려면 `Repository`에 본인 점유 세션 조회가 필요하다. 이슈 초안은 PR 본문에 쓴다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | 5절의 `test_FR09_*` | 후보 조건, 분류, 상태 조회 |
| FR-11 | `test_FR11_목록_항목에_내부_값이_없다` | 사용자 ID·점유 주체·확률이 없다 |
| FR-14 | 5절의 `test_FR14_*` | 비공개 기기를 숨긴다 |

- 구현은 `server/tests/core/test_device_query.py`다.
- 다른 계층의 구현 없이 fake만으로 확인한다.
