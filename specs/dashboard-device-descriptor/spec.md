# 대시보드의 기기 선언 표시

- 상태: 진행 중
- 담당: 작업 지시자 (대시보드 서버 조회·스트림, 대시보드 화면 연결 구현)
- 요구사항: FR-12A, FR-12B, FR-13
- 따르는 결정: #245 (decision PR #246). `architecture.md` §4 "기기 자기 선언의 후속 반영"의 세 번째 단계다. 앞 단계는 `specs/iot-device-descriptor/spec.md`, `specs/core-device-actions/spec.md`다.

## 1. 왜

- 소유자는 기기 선언(제작사, 모델, 동작 목록)을 대시보드에서 확인한 뒤 기기를 신뢰한다 (FR-12A, #245 담당자 결정). 대시보드 장소 목록은 기기의 종류와 점유 방식만 보낸다. 제작사, 동작, 선언 해시를 볼 곳이 없다.
- 선언 해시가 바뀌어 `discovered`로 돌아간 기기를 소유자가 다시 신뢰하려면, 어떤 선언을 신뢰하는지 해시로 확인해야 한다 (FR-13).
- 장소 목록과 이력·스트림의 대상은 아직 `capability` 키를 쓴다. `architecture.md` §5는 이 키를 `deviceKind`로 바꿨다.
- 대시보드 화면은 종류(`locker`, `light`, `speaker`)와 동작(`open`, `on`, `off`)을 닫힌 목록으로 검사한다. 선언한 다른 종류의 기기와 `close` 같은 다른 동작의 이벤트는 화면이 버린다.

## 2. 무엇

- 입력: DB `node`에 저장한 기기 선언(`descriptor`)과 선언 해시(`descriptor_hash`). 저장한 요청의 `target_spec`.
- 출력:
  - `GET /dashboard/places`의 기기마다 `deviceKind`, `occupancy`, `vendor`(`name`, `model`, `firmware`), `actions`(`name`, `description`, `role`), `descriptorHash`. `capability` 키는 없다.
  - 이력 행, 요청 한 건, 스트림 `mcp_call`의 `target`은 `{kind, deviceKind?, nodeId?}`다. `deviceKind`는 근접 지정·전역 할당의 종류 이름이다.
  - 대시보드 기기 상세에 선언(제작사, 모델, 펌웨어, 동작과 역할, 선언 해시)이 보인다. 화면은 선언한 어떤 종류·동작 이름도 받아서 그린다.
- 바뀌는 상태: 없음. 대시보드는 읽기만 한다.
- 약속: 장소 목록의 선언 값은 DB에 저장한 선언 그대로다. 대시보드 응답과 스트림은 사용자 ID와 계정 정보를 담지 않는다.

## 3. 제약

- 대시보드 조회 REST의 기기 정보는 종류, 점유 방식, 제작사 정보, 동작 목록(이름, 설명, 역할), 선언 해시를 담는다 (FR-12A).
- 키와 필수 여부는 `architecture.md` §5 "대시보드 조회" 형식 표를 따른다. `actions[].role`은 선택 키다. 표에 없는 키(`input_schema`, `result`, 선언 형식 버전)는 보내지 않는다 (FR-12A).
- `target.deviceKind`는 `nearest:{kind}`와 `any:{kind}`의 종류 이름이다. 명시 지정에는 없다 (FR-12B, `architecture.md` §5 WebSocket).
- 대시보드는 서버에 쓰지 않는다. 신뢰는 시드 스크립트가 바꾼다 (FR-12A).
- 화면은 선언 해시를 줄이지 않고 보인다. 소유자가 해시 전체를 비교할 수 있어야 한다 (FR-13).
- 화면 문구는 제어 신호 출력과 논리적 점유를 따른다 (FR-12A, FR-12B).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/shared/models.py` — `DashboardNode`의 `capability`를 `kind`로 바꾸고 선언과 해시를 더한다
- `server/src/nearpilot/core/dashboard/` — 장소 목록의 기기 정보, `target_of`의 `deviceKind`
- `server/src/nearpilot/db/repositories/dashboard_reads.py` — 기기 선언과 해시 읽기
- `server/src/nearpilot/api/ws/schema.py` — 장소 목록과 대상의 형식 검사
- `dashboard/` — 화면 모델, 서버 어댑터, 기기 상세, 무대 그림, 문구, 목 데이터
- 각 계층의 테스트

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/pyproject.toml`, `.github/`
- 이 작업: 판정, MCP 조회 응답, IOT, DB 스키마, 펌웨어, 시뮬레이터, 앱, 기기 접속 설정 묶음의 키

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| 대시보드 조회 REST 장소 목록·이력·요청 한 건 (`architecture.md` §5) | 제공 | 대시보드 화면 |
| 모니터링 WebSocket `mcp_call` (`architecture.md` §5) | 제공 | 대시보드 화면 |
| `DashboardReadStore.nodes()` → `DashboardNode` | 사용 | DB |
| `DeviceDescriptor` (`shared/descriptor.py`) | 사용 | shared |

### 만들지 않는 것

- 장소에 배치하지 않은 발견 기기의 목록. 형식 표의 장소 목록은 장소에 둔 기기만 담는다. 소유자는 시드로 장소에 먼저 둔 뒤 선언을 확인하고 신뢰한다 (배치와 신뢰는 따로 저장한다).
- 동작 인자 스키마(`input_schema`)와 실행 인자의 표시
- 대시보드에서 신뢰를 바꾸는 버튼
- 펌웨어 선언 내장, 시뮬레이터 v3, 기기 접속 설정 묶음의 `capability`·`occupancy` 제거 (PR ④)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 선언이 없는 기기 (v2 이전에 실패해 다음 발표를 기다리는 행) | 장소 목록에서 뺀다. 점검(`health`)에는 남는다 | `test_FR12A_places_skip_node_without_descriptor` |
| 역할이 없는 동작 | `actions[]`에 `role` 키가 없다 | `test_FR12A_places_show_declaration` |
| 선언의 종류가 사물함·조명·스피커가 아님 | 서버는 그대로 보낸다. 화면은 종류 이름을 그대로 쓰고 기본 상자로 그린다 | `test_FR12A_places_unknown_kind`, `map.test.ts`, `text.test.ts`, `stage.test.ts` |
| 서버가 형식 표 밖의 종류·동작 이름, 해시를 보냄 | 서버 스키마 검사가 `SchemaError`로 막는다. 화면 어댑터는 조회 오류로 알리고 이벤트는 그 이벤트만 버린다 | `test_dashboard_schema.py`, `map.test.ts` |
| 선언 해시가 바뀌어 `discovered`가 됨 | 장소 목록은 새 해시를, 점검과 기기 상세는 `discovered`를 보인다 | `test_FR13_places_show_new_hash_after_redeclare` |
| 명시 지정 요청의 이력 | `target`에 `deviceKind`가 없고 `nodeId`가 있다 | `test_FR12B_target_device_kind` |
| 저장된 옛 대상 표기(`nearest:locker`) | 같은 파서로 읽어 `deviceKind`로 보낸다 | `test_FR12B_target_device_kind` |
| 화면이 모르는 동작(`close`, 선언한 다른 이름)의 스트림 이벤트 | 버리지 않는다. 동작 이름 그대로 보인다 | `map.test.ts`, `text.test.ts` |

## 6. 결정

1. 선언이 없는 기기는 장소 목록에서 뺀다 — 형식 표의 `vendor`, `actions`, `descriptorHash`는 필수다. 값을 지어내지 않는다. 선언이 없는 기기는 판정 후보도 아니다(`specs/core-device-actions/spec.md`). 연결과 신뢰는 점검에서 보인다.
2. `actions[]`는 선언 순서 그대로 보낸다. `role`은 선언에 있을 때만 보낸다 — 형식 표에서 `role`만 선택 키다.
3. 서버 스키마는 `deviceKind`와 동작 이름을 이름 규칙(`^[a-z][a-z0-9_]*$`, 32바이트 이하)으로, `descriptorHash`를 64자리 소문자 16진수로, `role`을 `reoperate`·`deactivate`로 검사한다. 제작사 값과 설명은 문자열만 검사한다 — 선언 검증(`parse_descriptor`)이 길이를 이미 막았다.
4. 화면 모델의 종류와 동작은 닫힌 목록 대신 이름 문자열이다. 아는 이름(사물함·조명·스피커, 열기·닫기·켜기·끄기)만 쉬운 말로 바꾸고 모르는 이름은 그대로 보인다 — 새 종류를 화면 코드 수정 없이 보인다 (NFR-07의 취지).
5. 무대는 사물함·조명·스피커만 고유 그림으로 그리고, 그 밖의 종류는 앞면 부품이 없는 상자로 그린다. 상자 높이는 스피커와 사물함 사이 값(70)이다 — 표시용 배치이고 실제 크기가 아니다.
6. 기기 상세의 "동작" 행은 선언의 동작 이름을 쉬운 말로 잇는다. 그 아래 "선언" 절에 제작사·모델·펌웨어, 동작(이름, 설명, 역할), 선언 해시 전체를 보인다. 상태와 관계없이(고장 격리, 대여 중 포함) 보인다.
7. 목 데이터의 선언은 v2 선언 템플릿(`legacy_descriptor`)과 같은 값과 해시를 쓴다 — 목 화면이 실제 서버의 기존 기기와 같은 해시를 보인다.
8. 화면의 사유 코드 문구에 `UNSUPPORTED_ACTION`, `UNSUPPORTED_PARAMETER`, `PARAMETER_OUT_OF_RANGE`를 서버 문구(`core/dashboard/text.py`)와 같게 더한다 — 선언 기반 판정의 거부 사유가 코드 그대로 보이지 않게 한다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-12A | `tests/core/dashboard/test_dashboard_queries.py::test_FR12A_places_show_declaration` | 장소 목록의 `deviceKind`, `vendor`, `actions`(역할 선택), `descriptorHash` |
| FR-12A | `tests/core/dashboard/test_dashboard_queries.py::test_FR12A_places_unknown_kind` | 선언한 새 종류를 그대로 보낸다 |
| FR-12A | `tests/core/dashboard/test_dashboard_queries.py::test_FR12A_places_skip_node_without_descriptor` | 선언 없는 기기를 빼고 점검에는 남긴다 |
| FR-12A | `tests/db/test_dashboard_reads.py` | 저장한 선언과 해시를 읽는다 |
| FR-12A | `tests/api/test_dashboard_schema.py` | 장소 목록의 새 키 검사와 거부 경로 |
| FR-12A | `tests/api/test_dashboard_rest.py` | 실제 DB로 조립한 서버의 장소 목록 응답 |
| FR-12B | `tests/core/dashboard/test_events.py::test_FR12B_target_device_kind` | 근접·전역은 `deviceKind`, 명시는 `nodeId` |
| FR-13 | `tests/integration/test_dashboard_descriptor_flow.py::test_FR13_places_show_new_hash_after_redeclare` | 선언이 바뀐 발표 뒤 장소 목록의 새 해시와 `discovered` |
| FR-12A | `dashboard/src/data/server/map.test.ts` | 새 키 변환, 모르는 종류·동작 수용, 형식 밖 값 거부 |
| FR-12A | `dashboard/src/panels/DevicePanel.test.tsx` | 기기 상세의 선언 절 |
| FR-12A | `dashboard/src/view/text.test.ts` | 아는 이름의 쉬운 말과 모르는 이름의 그대로 표시 |
