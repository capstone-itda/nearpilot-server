# FR-09 접근 승인 요청 (request_access)

- 상태: 완료
- 담당: eunji719 (판정)
- 요구사항: FR-03, FR-09, FR-11, FR-14
- 따르는 결정: #23, #24, #25

## 1. 왜

- `CoreService.request_access`를 구현한 곳이 없다. 제한 기기의 방문자가 승인을 요청할 수 없어서, 승인이 필요한 기기는 소유자가 승인할 기회조차 없다.
- 승인 요청이 실행 허가로 보이면 방문자가 요청만으로 기기를 쓴다고 오해한다. 같은 요청을 다시 보낼 때 신청이 쌓이면 소유자의 대기 목록이 넘친다.

## 2. 무엇

- 입력: 인증된 `user_id`, `node_id`, 동작(`Action`), 외부 `request_id`
- 출력: `Decision`. 사용자 ID와 점유 주체가 없다. `use_id`는 없다.
- 바뀌는 상태: 승인 신청 하나(`pending`)와 요청 한 건의 판정 기록. 기기, 점유, 승인 사용분은 바꾸지 않는다.
- 약속: 승인 요청은 실행 허가가 아니다. 소유자의 결정은 이 서비스가 아니라 다음 `request_use`의 ④가 확인한다.

| 정책 검사(④) 결과 | 이 서비스의 동작 | 응답 |
|---|---|---|
| `OK` (승인 불필요 또는 유효한 승인 있음) | 신청을 만들지 않는다 | `OK` / `OK`, `message`에 "승인이 필요 없습니다. request_use를 호출하세요" |
| `NEED_APPROVAL` (`APPROVAL_PENDING`) | 기존 신청을 그대로 쓴다. 만료를 연장하지 않는다 | `NEED_APPROVAL` / `APPROVAL_PENDING` |
| `NEED_APPROVAL` (그 밖의 사유) | 승인 신청을 만든다 | `NEED_APPROVAL` / `APPROVAL_PENDING` |
| `DENY` | 신청을 만들지 않는다 | `DENY` / ④의 사유 |

## 3. 제약

- `OK` 응답에도 `use_id`를 담지 않는다. 승인 요청은 실행 허가가 아니다 (FR-09).
- 사용자 ID, 점유 주체, 승인 ID, 정책 ID와 버전, 소유자 ID를 응답에 넣지 않는다 (FR-11, NFR-08).
- 없는 기기와 비공개 기기(소유자가 아닌 계정)는 구분하지 않고 `DENY` / `TARGET_UNAVAILABLE`이다 (FR-14).
- 같은 `(user_id, request_id)`와 같은 입력은 기존 판정을 반환하고 신청을 다시 만들지 않는다. 같은 키에 다른 입력이면 `DENY` / `REQUEST_ID_CONFLICT`다 (#24).
- 같은 요청자, 기기, 동작, 정책 버전에 만료되지 않은 `pending`이 있으면 새 신청을 만들지 않고 기존 신청의 상태를 따른다 (FR-03).
- 판정하지 않는다. 승인이 필요한지는 `PolicyChecker.check`의 결과를 그대로 쓴다 (NFR-07).
- 알 수 없는 예외는 `DENY` / `INTERNAL_ERROR`로 거부한다. 호스트에는 일반 안내만 준다. 예외 내용은 감사 기록에만 남긴다 (#25).
- 시간 값은 설정과 `clock`에서 받는다. 코드 상수로 쓰지 않는다.
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/core/decision/`
- `server/tests/core/`
- `specs/core-access-request/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/proximity/`, `core/policy/`, `core/lifecycle/`, `db/`, `api/`, `iot/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `Repository.link_request`, `save_decision`, `nodes`, `current_policy` | 사용 | DB |
| `Repository.create_approval_request` | 사용 | DB |
| `PolicyChecker.check` | 사용 | 정책 (DB) |
| `AuditLog.append` | 사용 | DB |
| `request_access(user_id, node_id, action, request_id)` (`CoreService`) | 제공 | API (`api/mcp`) |

### 만들지 않는 것
- MCP 도구 `request_access`와 응답 직렬화 (`api-mcp-access` spec)
- 소유자 앱 알림 요청. 승인 알림 인터페이스가 `shared`에 없고 전달 수단이 미결이다 (FR-03).
- 승인 요청 목록 조회, 소유자의 승인과 거부 (`Repository.decide_approval`, 관리 기능)
- 서버 진입점과 서비스 조립 (#88)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 승인이 필요한 기기에 처음 요청한다 | 신청을 만들고 `NEED_APPROVAL` / `APPROVAL_PENDING` | `test_FR03_승인이_필요하면_신청을_만든다` |
| 대기 중인 신청이 있다 | 새 신청을 만들지 않고 `APPROVAL_PENDING` | `test_FR03_대기_중이면_기존_신청을_쓴다` |
| 거부 또는 만료한 뒤 다시 요청한다 | 새 신청을 만든다 | `test_FR03_거부나_만료_뒤에는_새_신청을_만든다` |
| 승인이 필요 없거나 유효한 승인이 있다 | 신청을 만들지 않고 `OK`, `use_id` 없음 | `test_FR09_승인이_필요_없으면_신청을_만들지_않는다` |
| ④가 `DENY`다 | 신청을 만들지 않고 ④의 사유로 `DENY` | `test_FR14_정책이_거부하면_신청하지_않는다` |
| 기기에 정책이 없다 (`current_policy`가 `LookupError`) | 신청을 만들지 않고 `DENY` / `POLICY_DENIED` | `test_FR14_정책이_없으면_거부한다` |
| 비공개 기기를 소유자가 요청한다 | 다른 기기와 같게 처리한다 | `test_FR14_비공개_기기도_소유자는_요청한다` |
| 판정 저장이 다른 내용과 충돌한다 | `DENY` / `INTERNAL_ERROR` | `test_FR18_판정_저장이_충돌하면_거부한다` |
| 없는 기기다 | `DENY` / `TARGET_UNAVAILABLE` | `test_FR14_없는_기기와_비공개_기기는_같다` |
| 비공개 기기를 소유자가 아닌 계정이 요청한다 | `DENY` / `TARGET_UNAVAILABLE` | `test_FR14_없는_기기와_비공개_기기는_같다` |
| `node_id` 형식이 틀렸거나 `nearest:`·`any:` 처럼 기기를 지정하지 않았다 | `DENY` / `INVALID_INPUT`. 요청 행을 만들지 않는다 | `test_FR09_node_id_형식_오류는_거부한다` |
| 같은 `request_id`와 같은 입력으로 재전송한다 | 기존 판정을 반환한다. 신청을 다시 만들지 않는다 | `test_FR10_재전송은_기존_판정을_반환한다` |
| 같은 `request_id`에 다른 입력이 온다 | `DENY` / `REQUEST_ID_CONFLICT` | `test_FR10_같은_키_다른_입력은_충돌이다` |
| 같은 키의 첫 요청이 아직 판정 중이다 | `DENY` / `INTERNAL_ERROR` | `test_FR10_판정_중인_같은_키는_거부한다` |
| `create_approval_request`가 예외를 낸다 | `DENY` / `INTERNAL_ERROR`. 예외 내용은 감사 기록에만 남긴다 | `test_FR18_저장_예외는_내부_오류로_거부한다` |
| 신청 결과의 상태가 `pending`이 아니다 | `DENY` / `INTERNAL_ERROR` (기본값은 거부) | `test_FR18_알_수_없는_신청_상태는_거부한다` |
| 정책 검사가 예외를 낸다 | `DENY` / `INTERNAL_ERROR` | `test_FR18_정책_검사_예외는_내부_오류로_거부한다` |
| 응답 | 사용자 ID, 승인 ID, 정책 정보가 없다 | `test_FR11_응답에_내부_값이_없다` |

## 6. 결정

- 서비스는 `core/decision/access.py`의 `AccessRequestService` 하나다. 생성자로 `Repository`, `PolicyChecker`, `AuditLog`, `Settings`, `clock`, `new_id`를 받는다 — `request_use` 파이프라인과 분리한다. 근접·후보·점유를 쓰지 않고 신청만 만들기 때문이다.
- ⓪은 `link_request`와 `input_digest`를 그대로 쓴다. `node_id`를 명시 지정 대상(`TargetSpec`)으로 해서 `UseRequest`를 만든다. 담당자가 정했다 — `approval.internal_request_id`가 `request`의 FK여서 요청 행이 필요하고, 재전송과 충돌 규칙을 `request_use`와 같게 한다.
- 승인이 필요 없거나 이미 유효한 승인이 있으면 `OK`와 안내 문구로 응답한다. `use_id`는 없다. 담당자가 정했다 — `shared`의 사유 코드를 늘리지 않는다.
- 승인 신청의 정책 ID와 버전은 `Repository.current_policy(node_id)`의 값을 쓴다.
- 감사 기록은 `Step.POLICY`로 한 번 남긴다. 신청을 만들었는지(`created`)와 기존 신청을 썼는지를 `payload`에 담는다.

## 7. 가정

- 근접 검사를 하지 않는다 — FR-09의 `request_access` 설명에 근접 조건이 없고, 신청은 실행 허가가 아니다. 담당자 확인이 필요하다.
- 거부 또는 만료한 신청 뒤의 새 신청을 허용한다 — FR-03이 만료한 신청이 새 신청을 막지 않는다고 하고, 거부 뒤의 재신청을 막는 규칙이 없다. 새 신청을 막는 규칙이 생기면 고친다.
- 소유자 앱 알림은 이 서비스가 요청하지 않는다 — 알림 접점이 `shared`에 없다. 접점이 생기면 후속 작업에서 신청을 새로 만들었을 때 알린다 (FR-03).
- 신청과 `save_decision`은 한 트랜잭션이 아니다 — 판정 저장이 실패하면 신청은 남고 응답은 `INTERNAL_ERROR`다. 같은 키로 재전송하면 기존 신청을 쓰므로 신청이 쌓이지 않는다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-03 | `test_FR03_승인이_필요하면_신청을_만든다` | 신청 입력(요청자, 기기, 동작, 정책 ID·버전, 내부 요청 ID), 응답 |
| FR-03 | `test_FR03_대기_중이면_기존_신청을_쓴다` | 신청 호출 수, 만료 연장 없음 |
| FR-03 | `test_FR03_거부나_만료_뒤에는_새_신청을_만든다` | 사유별 신청 생성 |
| FR-09 | `test_FR09_승인이_필요_없으면_신청을_만들지_않는다` | `OK`, `use_id` 없음 |
| FR-09 | `test_FR09_node_id_형식_오류는_거부한다` | `INVALID_INPUT` |
| FR-10 | `test_FR10_재전송은_기존_판정을_반환한다` | 신청 한 번 |
| FR-10 | `test_FR10_같은_키_다른_입력은_충돌이다` | `REQUEST_ID_CONFLICT` |
| FR-10 | `test_FR10_판정_중인_같은_키는_거부한다` | `INTERNAL_ERROR` |
| FR-11 | `test_FR11_응답에_내부_값이_없다` | 응답 필드 |
| FR-14 | `test_FR14_정책이_거부하면_신청하지_않는다` | 신청 호출 없음 |
| FR-14 | `test_FR14_없는_기기와_비공개_기기는_같다` | 같은 응답 |
| FR-14 | `test_FR14_정책이_없으면_거부한다` | `POLICY_DENIED`, 신청 없음 |
| FR-14 | `test_FR14_비공개_기기도_소유자는_요청한다` | 소유자 허용 |
| FR-18 | `test_FR18_판정_저장이_충돌하면_거부한다` | `INTERNAL_ERROR` |
| FR-18 | `test_FR18_저장_예외는_내부_오류로_거부한다` | `INTERNAL_ERROR`, 감사 기록 |
| FR-18 | `test_FR18_알_수_없는_신청_상태는_거부한다` | 기본값 거부 |
| FR-18 | `test_FR18_정책_검사_예외는_내부_오류로_거부한다` | `INTERNAL_ERROR` |
