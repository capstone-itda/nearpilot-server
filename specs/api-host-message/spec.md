# MCP 응답에 사용자 안내 문장 추가

- 담당: CcleverR (api)
- 요구사항: FR-09, FR-11, FR-14, FR-21, NFR-08
- 따르는 결정: 없음

## 1. 왜

- 지금 MCP 응답의 `message`는 `INTERNAL_ERROR`, 승인 요청, 반납 때만 채워지고 나머지는 `null`이다.
- 호스트(Claude Desktop·Codex 등)가 `message`가 없으면 `verdict`/`reason` 코드값을 그대로 보여주거나, 같은 결과를 호스트마다 다르게 설명한다.
- `MCPServer("nearpilot")`에 서버 차원의 안내(`instructions`)가 없어 호스트마다 `message` 사용 방식을 다르게 해석할 수 있다.

## 2. 무엇

- 입력: `request_use`, `request_access`, `release_use` 세 도구의 기존 호출. 새 인자는 없다.
- 출력: 세 도구 응답의 `message`가 항상 비어 있지 않은 한국어 문장이다. 응답 키 구성은 바꾸지 않는다.
- 바뀌는 상태: 없음.
- 약속:
  - core가 이미 채운 `message`(승인 요청, 반납, `INTERNAL_ERROR`)는 그대로 쓴다. api는 `decision.message`가 `None`일 때만 문장을 만든다.
  - 문장은 합쇼체("-습니다", "-십시오", "-겠습니까?")로 쓰고, 결과 + 원인 + 다음 행동 순서다. `OK`는 결과만 쓴다.
  - 기기는 `node_id`에서 바로 "{번호}번 {종류 한국어 이름}" 으로 바꾼다. 번호가 없거나 모르는 종류면 `node_id`를 그대로 쓴다.
  - 거리 선택자·묶음 요청(`devices`가 있는 응답)은 기기별 결과를 묶어 한 문장으로 만든다. `similar`가 참이면 안내를 덧붙인다.
  - `MCPServer("nearpilot")`에 `instructions`를 더해 호스트가 `message`를 바탕으로 답하고 코드값을 그대로 읽지 않게 안내한다.

## 3. 제약

- 사용자 ID, 점유 주체, 내부 요청 ID, 근접 확률을 문장에 넣지 않는다 (FR-11, NFR-08).
- `INTERNAL_ERROR`는 예외 내용 대신 일반 안내만 보낸다 (기존 동작 유지).
- 응답 키 구성은 바꾸지 않는다. 평가 스크립트와 기존 테스트가 `verdict`/`reason`을 계속 읽는다.
- 다른 계층(`shared/`, `core/`, `db/`, `iot/`)은 고치지 않는다.

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
| `Decision.message` | 사용(비어 있으면 api가 채운다) | 판정(core) |
| MCP 도구 응답 `message`, `MCPServer.instructions` | 제공 | AI 호스트 |

### 만들지 않는 것
- `find_available_devices`, `get_device_state`, `get_use_status` 세 조회 도구의 `message`. 지금 응답에 `message` 키가 없어 키를 더해야 한다. 이 spec은 다루지 않고 별도로 다룬다 (7절 가정 참고).
- core가 이미 채운 메시지의 말투 통일 (access.py `request_use를 호출하세요`, release.py `반납을 완료했다` 등). 합쇼체가 아니지만 core 영역이라 이 spec은 고치지 않는다. 담당자에게 보고한다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `decision.message`가 `None`이다 | `reason`으로 문장을 만든다 | `test_FR09_모든_ReasonCode에_문장이_있다` |
| `decision.message`가 이미 있다 (core가 채웠다) | 그대로 쓴다 | `test_core가_채운_message는_덮지_않는다` |
| `node_id`가 "{종류}-{번호}" 형식이고 종류를 안다 | "{번호}번 {한국어 이름}" | `test_FR14_아는_kind는_번호와_한국어_이름으로_바꾼다` |
| `node_id`에 번호가 없다 | `node_id` 그대로 | `test_FR14_번호가_없으면_node_id_그대로` |
| `node_id`의 종류를 모른다 | `node_id` 그대로 | `test_FR14_모르는_kind는_node_id_그대로` |
| `node_id`가 없다(판정 전 거부 등) | "기기" | `test_FR14_node_id가_없으면_기기` |
| 동작 이름을 모른다 | "요청한 동작을 실행했습니다" 형식의 일반 표현 | `test_FR09_모르는_동작은_일반_표현` |
| 거리 선택자 응답이 전부 성공이다 | 기기 번호를 묶은 한 문장 | `test_FR09_거리_선택자_전부_성공` |
| 거리 선택자 응답이 일부 실패다 | 성공 묶음 문장 뒤에 실패 기기별 문장 | `test_FR09_거리_선택자_일부_실패` |
| `similar`가 참이다 | 안내를 덧붙인다 | `test_FR09_거리_선택자_similar_덧붙인다` |
| 묶음 요청(`requests`)의 기기마다 동작이 다르다 | `item` 순서로 원래 동작을 찾아 쓴다 | `test_FR09_묶음_요청은_item으로_동작을_찾는다` |
| 문장에 NFR-08 금지 항목이 들어갈 수 있는 값이 있다 | 들어가지 않는다 | `test_NFR08_사용자_ID나_점유_주체_근접_확률이_없다` |
| 문장에 `request_id`·`use_id`·도구 이름 같은 내부 용어가 들어갈 수 있다 | 들어가지 않는다 | `test_문장에_내부_용어나_도구_이름이_없다` |
| 초기화 응답을 받는다 | `instructions`에 안내가 있다 | `test_FR09_초기화_응답에_호스트_안내_instructions가_있다` |

## 6. 결정

- 문장 생성은 `api/mcp/messages.py` 새 모듈에 둔다 — `response.py`는 허용 목록 변환만 맡기고, 문장 규칙은 따로 둬서 `ReasonCode`마다 하나씩 늘어나는 표를 한곳에 모은다.
- `host_response`에 `action`, `item_actions`(묶음 요청의 1부터 센 항목 순서 → 동작 이름) 선택 인자를 더한다. `server.py`가 요청에서 이미 아는 값을 넘긴다 — `DeviceOutcome`에 동작 이름이 없어서 문장이 동작을 말하려면 호출자가 알려줘야 한다.
- 거리 선택자·묶음의 실패 기기 문장은 "{기기}{은/는} {원인} {동작 실패 동사}." 형식으로 통일한다(예시의 "끄지 못했습니다"). 단일 판정의 실패 문장(`DEVICE_BUSY` 등)은 동작 동사 없이 "~습니다. ~해 주십시오." 형식이다 — 주어진 예시가 두 형식을 다르게 보여준다.
- `LOW_CONFIDENCE`의 안내 문장은 기기 종류를 넣지 않고 "가까운 기기가 여러 개라..."로 일반화한다. `Decision`에 종류 정보가 없고(있는 건 `node_id`뿐), 그 시점에는 아직 기기를 하나로 정하지 못한 상태라 종류도 모를 수 있다 — target_spec을 더 파싱해 넣는 비용보다 일반화가 간단하다.
- `node_id`가 "{종류}-{숫자}" 형식이 아니거나 종류를 모르면 `node_id`를 그대로 쓴다. 종류 목록(`light`, `locker`, `speaker`, `display`, `fan`)은 저장소에서 실제 kind로 쓰이는 값(`server/config/server.example.toml`, `server/src/nearpilot/iot/smartthings/descriptor.py`)과 테스트가 "새 kind가 코드 변경 없이 동작한다"를 보일 때 쓰는 값(`fan`, `server/tests/integration/test_new_kind_without_code.py`)을 모았다. 목록에 없는 새 kind는 코드 변경 없이 `node_id`로 그대로 표시된다 — 이 성질을 깨지 않는다.

## 7. 가정

- `node_id`가 알려진 종류가 아니어서 그대로 쓸 때, 조사(을/를 등)는 받침 여부를 알 수 없어 받침 없는 쪽("를", "가", "는", "와")으로 고정한다. 영문·숫자 혼합 ID의 정확한 조사는 범위 밖이다.
- 조회 도구 3개(`find_available_devices`, `get_device_state`, `get_use_status`)의 `message`는 응답에 새 키를 더해야 하는 변경이라 이 spec의 경계 밖으로 둔다. 담당자의 결정이 필요하다 — 이슈를 올린다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `server/tests/api/test_mcp_messages.py`의 `test_FR09_*` | `message`가 항상 채워지고 결과+원인+다음행동 순서다 |
| FR-11, NFR-08 | `test_NFR08_사용자_ID나_점유_주체_근접_확률이_없다` | 금지 항목이 문장에 없다 |
| FR-14 | `test_FR14_*` | `node_id` → 한국어 이름 변환의 정상·예외 경로 |
| FR-21 | 기존 `test_mcp_release.py`, `test_mcp.py` | `message`가 채워져도 응답 모양과 허용 목록이 그대로다 |

- 모든 `ReasonCode`를 순회해 문장이 있고 합쇼체인지 확인한다: `test_FR09_모든_ReasonCode에_문장이_있다`, `test_합쇼체로만_끝난다`.
- 구현은 `server/tests/api/test_mcp_messages.py`이고, 기존 `test_mcp.py`·`test_mcp_selector.py`의 `message` 기대값도 갱신한다.
