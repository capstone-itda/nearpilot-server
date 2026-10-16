# 서버 진입점과 서비스 조립

- 상태: 구현 전 초안. 선행 구현과 실제 연결 검증 뒤에만 같은 feat PR을 머지한다.
- 담당: JM-220 (상태 관리, 판정)
- 요구사항: FR-05, FR-06, FR-07, FR-08, FR-09, FR-11, FR-19, FR-20, FR-21, FR-22, FR-23, NFR-03, NFR-04, NFR-08, NFR-12, NFR-13
- 따르는 결정: #34, #40, #82, #83, #88, #140, #164, #165
- 내부 선택: 담당자가 2026-10-16 대화에서 경로, 실행 방식, 설정, 주기와 실패 정책을 선택했다.

## 1. 왜

- 서버에는 기능별 구현이 있지만 실행 진입점과 전체 조립이 없다.
- DB 연결을 스레드 사이에 공유하면 기존 SQLite 계약을 어긴다.
- 복원, 주기 호출과 종료를 연결하지 않으면 상태 관리와 기록 정리가 동작하지 않는다.

## 2. 무엇

- 입력: TOML 설정 경로, DB 파일, 장소별 모델 파일, 비밀 파일과 MQTT 보고다.
- 출력: 같은 프로세스의 FastAPI와 MCP 엔드포인트, 주기 작업과 종료 결과다.
- 바뀌는 상태: 기존 서비스가 각 계약에 따라 바꾸는 DB·기기·감사 상태다.
- 약속:
  - `server/`에서 `python -m nearpilot --config config/server.toml`로 실행한다.
  - 단일 프로세스와 단일 worker를 쓴다.
  - MQTT 구독 준비 뒤 최초 복원과 시작 정리를 마쳐야 요청을 받는다.
  - 개별 기기의 현재 상태를 확인하지 못하면 기존 실행 가능 여부 검사로 차단한다.
  - 초기화가 실패하면 획득한 자원을 정리한다.

## 3. 제약

- 조립만 실제 계층 구현을 만들어 연결한다. 서비스 내부는 기존 `shared` 계약만 쓴다 (`AGENTS.md` 의존 규칙 4, architecture.md §2).
- CORE의 허가 없이 MQTT 실행 명령을 보내지 않는다 (FR-06).
- DB 연결은 생성한 스레드에서만 쓰고 닫는다. 기존 WAL과 트랜잭션 경로를 유지한다 (FR-19, NFR-03).
- MCP는 기존 인증과 허용 필드 응답 변환을 쓴다 (FR-08, FR-11, NFR-08).
- 기기별 공통 재진입 잠금과 복구 서비스를 모든 상태 관리 서비스에 주입한다 (NFR-04).
- MQTT SUBACK은 기기 온라인이나 안전 확인이 아니다 (FR-07, NFR-04).
- 실제 기기의 시각 오차가 미설정이면 기존 IOT 차단을 유지한다 (NFR-08, #140).
- 모델과 설정의 실제 값·버전을 보존한다. 과거 요청을 현재 설정으로 복원하지 않는다 (FR-22).
- 종료 제한을 넘겨도 불명 작업을 성공이나 미전송으로 바꾸지 않는다 (FR-07, FR-19).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/__main__.py`
- `server/src/nearpilot/runtime/`
- `server/config/server.example.toml`과 `server/config/README.md`다.
- `server/tests/integration/`의 조립 검증이다.

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 이 작업: 기존 판정·정책·근접 추정 알고리즘, DB 정리 알고리즘, MQTT 본문과 서비스의 도메인 동작이다.
- 실제 비밀 파일과 운영용 `server.toml`은 저장소에 넣지 않는다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `CoreService`, 기존 MCP `build_app` | 생성·연결 | 판정, API |
| `Repository`, `AccountLookup`, `ExecutionStore`, `AuditLog` | 생성·주입 | DB |
| `RetentionStore.cleanup` | 시작과 주기 호출 | DB |
| `ProximityEstimator`, 구현의 기존 `restore` 접점 | 생성·관측 복원 | 근접 추정 |
| `ExecutionReadiness`, `DecisionRechecker` | 생성·연결 | 상태 관리, 판정 |
| `NodeEventSink`, `NodeCommandGateway`, `SafetyCheckGateway` | 생성·연결 | 상태 관리, IOT |

### 만들지 않는 것

- `CoreService` 위임 객체는 `core-service`가 맡는다.
- 기록 삭제는 `db-retention`이 맡는다.
- 요청 한도 집계와 거부는 기존 DB·판정 spec의 fix가 맡는다.
- REST, WebSocket, 관리 서비스와 시드 스크립트는 별도 작업이다.
- 인증 실패 감사 접점과 원래 요청 연결의 새 계약은 만들지 않는다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 필수 설정이나 모델 파일이 잘못됐다 | 요청을 받지 않는다. 획득한 자원을 정리한다 | `test_FR22_runtime_invalid_config_stops_startup` |
| 문서가 허용한 임계값이 미설정이다 | 서버는 기존 실행 차단 동작을 유지한다 | `test_FR09_runtime_unset_threshold_stays_blocked` |
| MQTT 구독이 실패했다 | 복원과 요청 접수로 진행하지 않는다 | `test_FR06_runtime_requires_suback` |
| 구독 중 보고가 도착했다 | 최초 수신 시각으로 큐에 보관한다. 최초 복원 뒤 인계한다 | `test_FR05_runtime_buffers_startup_reports` |
| 최초 복원 조회나 시작 정리가 실패했다 | 요청을 받지 않고 종료한다 | `test_FR19_runtime_startup_restore_cleanup_failure` |
| 한 기기의 현재 상태가 아직 없다 | 다른 기기의 접수와 구분하고 그 기기의 실행을 차단한다 | `test_NFR04_runtime_pending_node_stays_blocked` |
| 운영 중 기록 정리가 실패했다 | 경고를 남기고 다음 정리 주기에 재시도한다 | `test_FR22_runtime_cleanup_retries` |
| JSONL을 쓸 수 없다 | DB 저장과 실행을 막지 않는다 | `test_FR22_runtime_jsonl_failure_is_nonblocking` |
| 요청과 MQTT 결과가 다른 스레드에서 온다 | 연결을 공유하지 않고 기존 사건별 저장으로 정산한다 | `test_NFR03_runtime_thread_owned_connections` |
| `any:locker` 요청이 동시에 온다 | 공유한 할당 대기창으로 묶는다 | `test_FR23_runtime_shares_allocation_window` |
| 초기화 중간에 실패했다 | 이미 시작한 작업과 연결만 종료한다 | `test_NFR04_runtime_partial_startup_cleanup` |
| 종료 대기 제한을 넘겼다 | 오류를 알린다. 미완료 기록을 다음 복원 대상으로 유지한다 | `test_FR07_runtime_shutdown_timeout_keeps_unknown` |
| 다른 계정의 사용을 MCP로 조회한다 | 기존 본인 조회 제한을 유지한다 | `test_FR21_runtime_mcp_ownership` |

## 6. 결정

### 경로와 실행

- 진입점은 `nearpilot/__main__.py`다. 조립은 `nearpilot/runtime/`에 둔다 — 담당자가 선택했다.
- FastAPI 수명주기가 자원을 관리한다 — 시작과 종료의 책임을 한 곳에 둔다.
- 기존 MCP 앱의 수명주기도 연결한다 — 마운트만 하고 초기화를 생략하지 않는다.
- 외부 MCP 경로는 기존 `/mcp`를 유지한다 — 마운트로 경로를 중복하지 않는다.

### 설정과 모델

- TOML은 운영 `Settings`, 실행 설정과 장소별 모델 버전을 담는다 — 담당자가 선택했다.
- 상대 경로는 설정 파일의 부모 디렉터리 기준으로 해석한다.
- 비밀은 파일 경로로 받는다. 원문을 설정 예시와 로그에 넣지 않는다.
- 필수 키, 알 수 없는 키, 값 범위와 시간 값의 일관성을 검증한다.
- 장소와 모델 버전은 명시적으로 고른다. 파일 이름으로 최신 버전을 추측하지 않는다.
- HMM은 DB에서 읽은 최근 관측으로 기존 복원 접점을 호출한다.
- 설정 변경은 재시작으로 적용한다. 기존 요청과 점유의 설정 스냅샷은 유지한다.
- MQTT와 보고 설정의 필수 값에는 임의 기본값을 넣지 않는다.
- 같은 PC의 시뮬레이터에만 검증된 시각 오차 0을 명시한다.

### DB와 서비스 연결

- 최상위 동기 호출마다 그 스레드에서 DB 연결을 연다 — 담당자가 선택했다.
- 중첩 호출은 같은 연결을 쓴다. 최상위 호출의 정리에서 등록을 해제하고 연결을 닫는다.
- 인증, CORE 진입, 기기 이벤트 인계, 주기 호출을 각 연결 범위로 감싼다.
- 네트워크 응답을 기다리는 동안 DB 트랜잭션을 열어 두지 않는다.
- 상태 관리 서비스와 할당 대기창은 프로세스에서 공유한다.
- 순환 참조는 작업 시작 전 콜백 연결로 해소한다. 미연결 콜백으로 작업을 시작하지 않는다.
- `RestoreService`를 판정·조회·실행의 실행 가능 여부 검사에 연결한다.
- `Rechecker`를 실행 직전 재검사에 연결한다.
- `ExecutionStore.execution_context`를 기기 이벤트의 원래 문맥 조회에 연결한다.
- 저장을 확인한 공용 조명 성공을 기존 성공 인계 접점에 연결한다.
- 내부 꺼짐은 기존 실행 경로와 전송 직전 `can_request_off`에 연결한다.
- MQTT 연결 상실은 명령 대기자에게 통지한다.
- 모든 연결의 JSONL 경로를 같은 절대 경로로 정규화한다.

### 시작 순서

1. 설정과 모델을 검증한다.
2. DB를 준비한다.
3. 근접 추정에 최근 관측을 복원한다.
4. 서비스와 콜백을 연결한다.
5. MQTT를 시작하고 다섯 구독의 SUBACK을 확인한다.
6. `RestoreService.restore()`를 한 번 호출한다.
7. `RetentionStore.cleanup(now)`를 한 번 호출한다.
8. 보고 검증·CORE 인계 작업과 주기 작업을 시작한다.
9. 요청 접수를 시작한다.

- 5~7단계의 보고는 큐에 보관한다. 오래된 보고의 거부 규칙은 유지한다.
- 6단계는 최초 복원 호출이다. 모든 기기의 복원 완료를 기다리는 절차가 아니다.
- 시작 정리가 실패하면 요청을 받지 않고 종료한다 — 담당자가 선택했다.

### 주기 작업

| 작업 | 초기 주기 | 설정 소유 |
|---|---|---|
| 실행 저장 재시도와 예약 만료 | 0.25초 | runtime 실행 설정 |
| 반납 검사와 재시도 | 0.25초 | runtime 실행 설정 |
| 미완료 복원 재시도 | 0.25초 | runtime 실행 설정 |
| 복구 저장 재시도 | 0.25초 | runtime 실행 설정 |
| 공용 조명 이탈·유지 권한 검사 | 1초 | runtime 실행 설정 |
| 보존기간 정리 | 60초 | `Settings.retention_cleanup_interval_sec` |

- 작업마다 별도 스레드를 쓴다. 같은 작업의 호출은 겹치지 않는다.
- 호출 주기와 도메인 응답 기한은 서로 다른 값이다.
- 각 작업은 실패를 기록하고 다음 호출을 예약한다. 실패를 성공이나 안전 확인으로 바꾸지 않는다.
- SQLite 대기시간의 초기값은 5,000ms다. runtime 실행 설정으로 받는다.
- 할당 대기창과 IOT 재인계에는 중복 주기 작업을 추가하지 않는다.

### 종료와 부분 실패

1. 새 요청 접수를 막는다.
2. 새 주기 호출을 멈춘다.
3. 진행 중인 호출을 제한 시간까지 기다린다.
4. MQTT와 보고 작업을 멈춘다.
5. 각 소유 스레드에서 DB 내보내기 등록과 연결을 정리한다.

- 종료 대기 초기값은 30초다. runtime 실행 설정으로 받는다 — 담당자가 선택했다.
- 제한을 넘겨도 다른 스레드의 사용 중 DB 연결을 강제로 닫지 않는다.
- 제한 초과는 오류로 알린다. 미완료 상태를 임의로 정산하지 않는다.
- 초기화 실패도 획득한 자원을 같은 원칙으로 정리한다.
- JSONL의 시작·종료 내보내기는 실패해도 DB 결과를 바꾸지 않는다.

## 7. 가정

- `core-service`, `db-retention-capacity`와 요청 용량 제한 fix는 실제 완료 검증의 선행 작업이다.
- 선행 구현 전에는 fake로 조립 계약을 검증할 수 있다. 실제 연결 완료로 표시하지 않는다.
- DB 정리의 세부 보호 기준과 JSONL 커서 방식은 DB 담당자가 먼저 확정한다.
- 승인 신청은 요청 목적을 구분해 외부 키를 유지한다. 실제 연결 검증은 #173의 확정 계약을 따른다.
- 실제 MQTT 주소·자격정보·제한 시간·보고 용량은 배포 설정으로 공급한다.
- 운영 임계값과 실제 기기 시각 오차는 담당 검증 없이 채우지 않는다.
- 실행 설정은 runtime 내부 값이다. 새 공동 설정이나 외부 계약이 필요하면 별도 결정으로 넘긴다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-05, FR-06 | `test_FR05_runtime_buffers_startup_reports`, `test_FR06_runtime_requires_suback` | 보고 수신과 실행 접수의 순서다 |
| FR-07 | `test_FR07_runtime_shutdown_timeout_keeps_unknown` | 종료 불명과 재시작 복원이다 |
| FR-08, FR-09, FR-11 | MCP 실제 앱 연결 검사 | 인증, 여섯 도구와 응답 정보 제한이다 |
| FR-19 | `test_FR19_runtime_startup_restore_cleanup_failure` | 복원·정리 전에는 접수하지 않는다 |
| FR-20 | 실제 반납·이탈 주기 호출 검사 | 반납 재검사와 내부 꺼짐의 연결이다 |
| FR-21 | `test_FR21_runtime_mcp_ownership` | 본인 사용 조회 제한이다 |
| FR-22 | `test_FR22_runtime_*` | 설정 검증, 정리 재시도와 JSONL 실패 격리다 |
| FR-23 | `test_FR23_runtime_shares_allocation_window` | 동시 할당 요청을 함께 배정한다 |
| NFR-03, NFR-04 | `test_NFR03_runtime_*`, `test_NFR04_runtime_*` | 연결 소유권, 기기 차단과 자원 정리다 |
| NFR-08, NFR-12, NFR-13 | 실제 MQTT·SQLite·시뮬레이터 연결 검사 | 비밀 비노출, 폐쇄망 연결과 재시작 재현이다 |

- 테스트 이름은 구현 계획이다. 초안 PR은 실행 코드와 새 테스트를 포함하지 않는다.
- 단위 검증은 fake로 실패를 주입한다. 실제 연결 검증은 `server/tests/integration/`에 둔다.
- 전체 `pytest`와 실제 MQTT·SQLite 연결 검증을 통과한 뒤에만 구현 완료로 바꾼다.