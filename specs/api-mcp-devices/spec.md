# FR-09 find_available_devices·get_device_state 도구

- 담당: eunji719 (판정)
- 요구사항: FR-09, FR-11, FR-14
- 따르는 결정: #29

## 1. 왜

- 호스트가 `request_use`를 부르기 전에 주변에서 쓸 수 있는 기기와 기기 상태를 알아야 한다. 지금은 `request_use`, `release_use`, `get_use_status` 도구만 있다.
- `CoreService`의 조회 결과를 그대로 보내면 필드가 늘 때 내부 값이 새고, 비공개 기기의 존재 여부가 응답 모양으로 드러날 수 있다.

## 2. 무엇

- 입력: 도구 `find_available_devices()`, `get_device_state(node_id)`. 계정은 `request_use`와 같이 토큰으로 정한다.
- 출력:
  - `find_available_devices`: `devices`(목록)와 `reason`. 항목은 `node_id`, `capability`, `occupancy`, `access`다. 성공이면 `reason`이 `null`이다. 후보가 없으면 `devices`가 빈 목록이다.
  - `get_device_state`: `node_id`, `capability`, `occupancy`, `state`, `online`, `trusted`, `access_mode`, `reason`. 성공이면 `reason`이 `null`이다. 값이 없으면 `null`이다.
- 바뀌는 상태: 없음. 조회만 한다.
- 약속:
  - 토큰 계정으로 `CoreService`를 부른다. 서비스의 조회 결과를 바꾸지 않는다 (NFR-07).
  - 없는 기기, 비공개 기기, 권한 없는 조회는 같은 응답이다. `reason`이 `TARGET_UNAVAILABLE`이고 나머지는 `null`이다 (FR-14).
  - `get_device_state` 응답의 모양은 성공과 실패가 같다.
  - 인자가 틀리거나 서비스가 예외를 내도 같은 모양의 응답을 반환한다.

| 상황 | `find_available_devices` | `get_device_state` |
|---|---|---|
| 정상 | `devices` 목록, `reason`은 `null` | 상태 값, `reason`은 `null` |
| 조회할 수 없는 기기 | — | `reason`은 `TARGET_UNAVAILABLE`, 나머지는 `null` |
| `node_id`가 비었다 | — | `reason`은 `INVALID_INPUT`. 서비스를 부르지 않는다 |
| 서비스가 예외를 낸다 | `devices`는 빈 목록, `reason`은 `INTERNAL_ERROR` | `reason`은 `INTERNAL_ERROR` |

## 3. 제약

- 응답에는 허용 목록의 필드만 담는다. 사용자 ID, 점유 주체, 근접 확률, 장소 ID를 담지 않는다 (FR-11, NFR-08). 서비스가 허용 목록 밖의 키를 돌려줘도 버린다.
- 계정은 토큰으로만 정한다. 인자로 받은 값을 계정으로 쓰지 않는다 (FR-08).
- 동기 서비스는 이벤트 루프를 막지 않도록 스레드에서 실행한다.
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/api/mcp/`
- `server/tests/api/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `db/`, `iot/`, `api/auth/`, `api/rest/`, `api/ws/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `CoreService.find_available_devices` | 사용 | 판정 (`core-device-query`) |
| `CoreService.get_device_state` | 사용 | 판정 (`core-device-query`) |
| 도구 `find_available_devices`, `get_device_state` | 제공 | AI 호스트 |

### 만들지 않는 것
- 후보 조건, 분류, 비공개 판단. 서비스가 한다.
- `request_access` 도구
- 서버 진입점과 조립 (#88)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 도구 목록 | 두 도구가 있다 | `test_FR09_도구_목록에_조회_도구가_있다` |
| 목록 조회 | 토큰 계정으로 서비스를 부르고 `devices`를 반환한다 | `test_FR09_find_available_devices는_토큰_계정으로_서비스를_부른다` |
| 후보가 없다 | 빈 `devices`, `reason`은 `null` | `test_FR09_후보가_없으면_빈_목록` |
| 목록 항목에 허용 목록 밖의 키가 있다 | 버린다 | `test_FR11_목록_항목에서_허용_목록_밖의_키를_버린다` |
| 목록 서비스가 예외를 낸다 | 빈 `devices`, `INTERNAL_ERROR` | `test_FR09_목록_서비스_예외는_INTERNAL_ERROR` |
| 상태 조회 | 토큰 계정과 `node_id`로 서비스를 부른다 | `test_FR09_get_device_state는_토큰_계정과_node_id로_서비스를_부른다` |
| 조회할 수 없는 기기 | `TARGET_UNAVAILABLE`, 나머지 `null` | `test_FR14_조회할_수_없는_기기는_모두_같은_응답` |
| 상태에 허용 목록 밖의 키가 있다 | 버린다 | `test_FR11_상태에서_허용_목록_밖의_키를_버린다` |
| `node_id`가 비었다 | `INVALID_INPUT`, 서비스를 부르지 않는다 | `test_FR09_node_id가_비면_INVALID_INPUT` |
| 상태 서비스가 예외를 낸다 | `INTERNAL_ERROR` | `test_FR09_상태_서비스_예외는_INTERNAL_ERROR` |
| 토큰이 없다 | 401. 서비스를 부르지 않는다 | `test_FR08_토큰이_없으면_조회_도구를_처리하지_않는다` |

## 6. 결정

- 목록 도구는 목록을 `devices` 키 안에 담은 객체를 반환한다 — 호스트마다 최상위 배열의 처리가 달라서 같은 결과를 보장하기 어렵기 때문이다 (FR-10).
- 목록 도구에도 `reason`을 둔다. 서비스 오류가 '후보 없음'으로 보이지 않게 한다.
- 사유 코드는 서비스의 `reason_code`를 그대로 쓴다. `get_use_status`와 같은 방식이다.

## 7. 가정

- 서비스가 `reason_code`가 있는 매핑을 돌려주면 조회할 수 없는 기기다. 다른 키는 쓰지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | 5절의 `test_FR09_*` | 두 도구의 호출과 응답 모양 |
| FR-11 | 5절의 `test_FR11_*` | 허용 목록 밖의 값이 나가지 않는다 |
| FR-14 | `test_FR14_조회할_수_없는_기기는_모두_같은_응답` | 비공개·없는 기기가 구분되지 않는다 |

- 구현은 `server/tests/api/test_mcp_devices.py`다.
- 공통 도구는 `server/tests/api/_mcp_client.py`를 쓴다.
