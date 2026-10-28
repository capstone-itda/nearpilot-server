# 최종 시연 서버 구성

- 담당: 작업 지시자 (시연 환경)
- 요구사항: FR-01, FR-04, FR-12A, FR-13, FR-14, FR-16, FR-17, FR-22, NFR-08, NFR-10, NFR-12
- 근거: `docs/product.md`, `docs/requirements.md`, `docs/architecture.md`, `specs/core-server-assembly/spec.md`, `specs/core-ops-cli/spec.md`, `specs/core-node-lab/spec.md`

## 1. 왜

`node-lab`은 콘솔 시험용이다. 앱, AI 호스트와 대시보드를 함께 연결하는 시연 구성이 없다.
시험 생성 도구는 스피커를 받지 않는다. 최종 시연에는 기기 7대가 필요하다.

## 2. 무엇

- 입력: 서버 주소, 별도 출력 폴더와 시연 설정 양식이다.
- 출력: 서버 설정, 기기 목록, MQTT 자격정보와 ACL, 시드, 합성 모델과 실행 스크립트다.
- 바뀌는 상태: 새 시연 폴더다. DB 상태는 기존 서버와 시드 명령만 바꾼다.
- 약속: 기존 `nearpilot serve`로 앱 REST, MCP와 대시보드를 함께 제공한다.
- 약속: `locker-1`부터 `locker-3`, `light-1`부터 `light-3`, `speaker-1`을 한 장소에 구성한다.
- 약속: 기존 시험 폴더와 이미 생성한 시연 폴더를 덮어쓰지 않는다.

## 3. 제약

- 사물함은 대여형이고 조명과 스피커는 공용형이다 (NFR-10).
- 기기는 MQTT 발표로 등록한다. 관리 쓰기는 기존 시드 명령을 쓴다 (FR-04, FR-13, FR-14).
- 소유자와 방문자는 기존 시드가 토큰을 발급한다. 앱은 기존 계정 REST를 쓴다 (FR-01).
- 비밀번호 원문은 `.env.*` 파일에만 쓴다. 기기마다 다른 MQTT 계정과 ACL을 쓴다 (NFR-08).
- 실제 기기의 시각 오차는 실측 전까지 비워 둔다. 같은 PC의 시뮬레이터에만 0초를 쓴다 (NFR-12).
- 근접 모델은 합성임을 표시한다. 스피커의 근접 임계값을 만들지 않는다 (FR-16, FR-17).
- 대시보드 설정을 포함하고 서버의 기존 조회 기능을 쓴다 (FR-12A).
- 서버 설정과 모델의 버전을 보존한다. 생성 과정에서 기존 기록을 바꾸지 않는다 (FR-22).

## 4. 경계

### 고치는 곳

- `server/config/final-demo/`: 시연 양식, 실행 스크립트와 사용 절차다.
- `server/tools/final_demo.py`: 양식으로 별도 시연 폴더를 만드는 준비 스크립트다.
- `server/tests/tools/`, `server/tests/integration/`: 구성 검증과 실제 서버 연결 검증이다.
- 이 spec이다.
- `.gitignore`의 시연 생성물 제외 규칙이다.
- `AGENTS.md`의 Gotchas 절이다.

### 고치지 않는 곳

- `docs/`, `AGENTS.md`의 Gotchas 이외 절, `server/src/`, DB 스키마, 외부 계약, 기존 `node_lab` 구현이다.
- 기존 시험 DB, 자격정보, 펌웨어와 앱 코드다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| 서버 설정과 `nearpilot serve` | 기존 서버 실행 | 서버 조립 |
| `nearpilot seed` | 기존 관리 쓰기 | CORE, DB |
| MQTT v2, 기기별 ACL | 기존 발표와 실행 경로 | IOT |
| `node_lab`의 브로커 실행과 USB 주입 | 기존 시험 도구 사용 | 기기 시험 |
| 앱 REST, MCP, 대시보드 REST와 WebSocket | 기존 서버 경로 사용 | API |

### 만들지 않는 것

- 새 판정 서버, 새 인증 방식, 자동 기기 신뢰, 물리 상태 검증과 임의의 시각 실측값이다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 출력 폴더가 있다 | 생성 전에 거부하고 기존 파일을 보존한다 | `test_FR22_final_demo_preserves_existing_directory` |
| 주소나 포트가 잘못됐다 | 파일을 만들기 전에 거부한다 | `test_NFR12_final_demo_rejects_invalid_network` |
| 실제 기기의 시각 오차가 없다 | 미설정을 유지한다. 기존 실행 차단을 유지한다 | `test_FR16_FR17_NFR12_final_demo_clock_and_model` |
| 시뮬레이터를 고른다 | localhost 주소와 별도 폴더를 쓴다 | `test_FR16_FR17_NFR12_final_demo_clock_and_model` |
| 기기가 발표하지 않았다 | 시드는 기기 항목을 거부한다. 발표 뒤 다시 실행한다 | 실제 연결 검증 |
| 새 기기를 등록한 뒤 서버가 복원 목록을 읽지 않았다 | 정상 종료하고 다시 시작한다 | 실제 연결 검증 |
| 스피커를 근접 지정한다 | 미설정 임계값의 기존 차단을 유지한다. 명시 지정은 장소 비콘을 검사한다 | 구성 검증과 실제 연결 검증 |

## 6. 결정

- 기존 서버와 시연 전용 구성 폴더를 쓴다 — 담당자가 2026-10-28에 선택했다.
- 서버 PC 주소는 `10.106.106.199`를 유지한다 — 담당자가 같은 날 선택했다.
- 별도 구성은 양식, 준비 스크립트와 실행 스크립트로 만든다 — 담당자가 승인한 구성 방식이다.
- 서버 시작은 기존 `nearpilot serve`에 맡긴다. 브로커는 기존 `node_lab broker`로 실행한다.
- USB 주입을 재사용하려고 `lab.json`과 `fleet.json`을 같은 폴더에 둔다.
- 실기용 구성을 기본으로 만든다. 연결 검증은 별도 시뮬레이터 구성에서 진행한다.

## 7. 가정

- 요청한 기기 7대는 같은 장소에 둔다 — NFR-10의 배치를 따른다.
- 접근 정책은 `node-lab`처럼 공개·개방으로 시작한다. 제한 기기 시연은 시드를 고친 뒤 반영한다.
- 스피커 정책의 승인 방식은 개방 정책에서 사용하지 않는 값으로만 넣는다. 기본 승인 방식의 팀 결정을 대신하지 않는다.
- 비콘은 기존 시험의 UUID와 소유자 minor 1, 방문자 minor 2를 초기값으로 쓴다. 앱의 비콘 등록으로 교체할 수 있다.
- 합성 좌표는 실행 경로 확인용이다. 최종 근접 품질은 실제 배치의 수집·적합·평가 뒤 확인한다.
- 기존 시뮬레이터는 사물함 `open`만 지원한다. `close`와 조명 설정 출력은 실기로 확인한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-01, FR-13, FR-14 | `test_FR01_FR13_FR14_final_demo_seed` | 시드의 계정, 장소와 정책을 기존 파서가 받는다 |
| FR-04, NFR-10 | `test_FR04_NFR10_final_demo_inventory` | 기기 7대의 기능, 점유 방식과 ACL이 일치한다 |
| FR-12A | `test_FR12A_final_demo_http` | 대시보드 설정과 LAN MCP Host가 있다 |
| FR-16, FR-17, NFR-12 | `test_FR16_FR17_NFR12_final_demo_clock_and_model` | 실기 시각을 만들지 않고 합성 모델과 임계값을 보존한다 |
| FR-22 | `test_FR22_final_demo_preserves_existing_directory` | 기존 폴더를 바꾸지 않는다 |
| NFR-08 | `test_NFR08_final_demo_credentials` | 비밀번호를 분리하고 공개 파일에 노출하지 않는다 |

기존 전체 `pytest`와 별도 구성의 실제 Mosquitto·서버·시뮬레이터 연결도 확인한다.
