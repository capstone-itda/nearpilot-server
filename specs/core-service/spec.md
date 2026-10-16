# MCP 기능 여섯 개의 CORE 서비스

- 상태: 구현 전 초안. 구현과 같은 feat PR에서만 main에 반영한다.
- 담당: JM-220 (판정, 서버 조립)
- 요구사항: FR-09, FR-11, FR-18, FR-21, NFR-03, NFR-07, NFR-08
- 따르는 결정: #88. 내부 구성은 담당자가 2026-10-16 대화에서 선택했다.

## 1. 왜

- MCP는 `CoreService`를 받지만 여섯 함수를 제공하는 실제 객체가 없다.
- 개별 기능은 구현돼 있다. 실제 객체가 없으면 서버는 MCP에 fake만 연결할 수 있다.

## 2. 무엇

- 입력: 인증한 계정의 식별자, `UseRequest`, 기존 기능 서비스, 주입한 시계와 설정이다.
- 출력: `shared.interfaces.CoreService`를 만족하는 객체다.
- 바뀌는 상태: 기존 기능 서비스가 계약에 따라 바꾸는 상태다. 위임 객체는 도메인 상태를 따로 저장하지 않는다.
- 약속:
  - `request_use`는 `RequestUseService`에 위임한다.
  - `request_access`는 `AccessRequestService`에 위임한다.
  - 기기 목록과 상태 조회는 `DeviceQueryService`에 위임한다.
  - 반납과 본인 사용 조회는 `ReleaseService`에 위임한다.
  - 위임 객체는 입력, 판정, 사유와 소유권 검사 결과를 바꾸지 않는다.
  - 서버 조립은 생성한 객체를 기존 MCP `build_app`에 연결한다.

## 3. 제약

- CORE만 판정한다. API는 기존 응답 변환을 쓴다 (NFR-07).
- 사용 요청은 실행 성공을 확인한 뒤에만 `OK`와 대여형 `use_id`를 반환한다 (FR-09).
- 반납과 사용 조회는 기존 본인 소유권 검사를 거친다 (FR-21).
- 호스트 응답에는 사용자 ID와 점유 주체를 넣지 않는다 (FR-11, NFR-08).
- 다른 계층에는 `shared` 계약으로만 의존한다 (`AGENTS.md` 의존 규칙 4).
- 위임 객체는 전역 잠금으로 동시 요청을 직렬화하지 않는다 (NFR-03).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/core/decision/core_service.py`
- `server/src/nearpilot/core/decision/__init__.py`
- `server/tests/core/`
- `server/tests/api/`의 기존 MCP 연결 검증이다.

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 이 작업: DB, IOT, 근접 추정, 정책 검사, 상태 관리의 구현이다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `CoreService` | 제공 | MCP |
| `ExecutionService`, `ExecutionStore` | 기존 사용 요청 서비스에 주입 | 상태 관리, DB |
| `Repository`, `AuditLog`, `ProximityEstimator`, `PolicyChecker`, `ExecutionReadiness` | 기존 판정 서비스에 주입 | DB, 근접 추정, 정책 검사, 상태 관리 |
| `build_app(core, authenticator, clock)` | 기존 연결 함수 사용 | API |

### 만들지 않는 것

- 서버 실행, SQLite 연결 수명, MQTT 연결과 설정 로더는 `core-server-assembly`가 맡는다.
- 용량 제한은 기존 `core-decision-pipeline` spec의 fix가 맡는다.
- REST, WebSocket, 인증 실패 감사 접점과 원래 요청 연결 계약은 별도 작업이다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 여섯 기능을 호출한다 | 계정과 인자를 맞는 서비스에 한 번 넘긴다 | `test_FR09_core_service_delegates_six_methods` |
| 사용 요청이 거부되거나 실행이 불명이다 | 기존 판정과 사유를 그대로 반환한다 | `test_FR18_core_service_keeps_decision` |
| 다른 계정의 사용을 조회하거나 반납한다 | 기존 `USE_NOT_FOUND` 결과를 유지한다 | `test_FR21_core_service_keeps_ownership_check` |
| 위임한 서비스가 예외를 낸다 | 기존 API 오류 변환을 거친다. 새 허가를 만들지 않는다 | `test_FR09_core_service_exception_reaches_mcp_guard` |
| 서로 다른 요청이 동시에 온다 | 같은 서비스 객체를 쓰고 요청 인자를 섞지 않는다 | `test_NFR03_core_service_concurrent_calls` |
| MCP 응답을 만든다 | 내부 식별자와 점유 주체를 내보내지 않는다 | `test_FR11_core_service_mcp_response_allowlist` |

## 6. 결정

- `core/decision/core_service.py`에 얇은 위임 객체를 둔다 — 담당자가 2026-10-16에 선택했다.
- 생성자는 기존 기능 서비스를 받는다 — 위임 검증과 자원 조립을 나눈다.
- 서비스 생성 접점도 같은 모듈에서 제공한다 — 서버 조립은 생성한 CORE 서비스를 MCP에 연결한다.
- 상태를 가진 서비스와 할당 대기창은 프로세스에서 공유한다 — 호출마다 실행 상태를 잃지 않는다.
- 실제 구현체의 최종 생성과 순환 콜백 연결은 `runtime/`이 맡는다 — 판정 모듈은 외부 계층 구현을 불러오지 않는다.

## 7. 가정

- 없음. 위임 객체는 기존 계약만 조합한다.
- 용량 제한 fix의 완료 여부는 위임 객체의 단위 검증을 막지 않는다. 실제 서버의 완료 검증에는 필요하다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-09 | `test_FR09_core_service_*` | 여섯 기능 위임과 예외 전달이다 |
| FR-11 | `test_FR11_core_service_mcp_response_allowlist` | 호스트 응답의 허용 필드다 |
| FR-18 | `test_FR18_core_service_keeps_decision` | 판정과 사유를 바꾸지 않는다 |
| FR-21 | `test_FR21_core_service_keeps_ownership_check` | 반납과 조회의 본인 제한이다 |
| NFR-03 | `test_NFR03_core_service_concurrent_calls` | 서비스 공유와 요청 분리다 |
| NFR-07, NFR-08 | 기존 MCP 회귀 검사와 위 테스트 | API의 인가 분리와 정보 최소화다 |

- 위 테스트 이름은 구현할 검증 계획이다. 이 초안 PR은 테스트 구현을 포함하지 않는다.
- 커밋 전 대상 작업 공간의 `server/`에서 전체 `pytest`를 실행한다.