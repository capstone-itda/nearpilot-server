# 소유자 승인 알림과 결정

- 담당: eunji719 (앱)
- 요구사항: FR-03, FR-14, NFR-08, NFR-12
- 따르는 결정: #183

## 1. 왜

제한 기기의 승인 요청이 생겨도 소유자는 알 방법이 없고, 승인하거나 거부할 화면도 없다. 방문자의 요청은 대기 만료(10분)까지 `NEED_APPROVAL`로 남는다 (FR-03).

## 2. 무엇

- 입력: 계정 탭에 저장한 서버 주소와 토큰. 소유자의 승인·거부
- 출력: `GET /approvals`, `GET /approvals/{approval_id}`, `POST /approvals/{approval_id}/decision` 호출. 새 승인 요청의 시스템 알림. 승인 탭의 대기 목록과 결정 결과
- 바뀌는 상태: 폰에 저장한 "승인 알림 받기" 설정과 이미 알린 승인 ID
- 약속:
  - "승인 알림 받기"를 켜면 앱이 꺼져 있어도 서버에 새 승인 요청을 주기적으로 묻고, 새 요청마다 시스템 알림을 띄운다 (FR-03).
  - 소유자가 알림을 누르면 앱의 승인 탭이 열리고 그 요청이 보인다 (FR-03).
  - 앱은 소유자의 결정만 서버에 보낸다. 승인 권한, 대기 상태, 정책 버전은 서버가 검증한다 (FR-03).
  - 앱은 결정을 반영하지 못한 이유(이미 결정됨, 정책 변경, 대기 만료, 권한 없음)를 구분해서 보여 준다 (FR-03).
  - 앱은 승인을 실행 허가로 안내하지 않는다. 방문자가 AI 호스트로 다시 요청해야 한다고 알린다 (FR-03).

## 3. 제약

- 경로와 응답 형식은 `architecture.md` §5 REST "승인"과 "공통 규칙"을 따른다 (#183).
- 승인 요청 한 건은 `approval_id`, `node_id`, `action`, `status`, `created_at`, `pending_expires_at`, `valid_until`을 담는다. 요청자 정보는 없다 (#183, NFR-08).
- 같은 결정의 재전송은 `200`과 기존 결과다 (FR-03).
- 알림은 소유자를 웹이나 AI 호스트 화면으로 보내지 않고 앱의 승인 화면을 연다 (FR-03).
- 인터넷 없이 독립 AP 안의 서버로만 통신한다. FCM은 쓰지 않는다 (NFR-12, FR-03 미결 항목의 후보 설명).

## 4. 경계

### 고치는 곳
- `app/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `GET /approvals`, `GET /approvals/{approval_id}`, `POST /approvals/{approval_id}/decision` (§5 REST "승인") | 사용 | API (`api/rest/`) |

### 만들지 않는 것
- 서버의 승인 REST와 CORE의 승인 조회·결정 인터페이스. `shared` 계약이 필요해서 팀 결정 이슈로 넘긴다.
- 승인 이력 화면, 정책 변경 화면. 정책은 시드 스크립트가 쓴다 (FR-12A).

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 서버 주소나 토큰이 없다 | 승인 탭이 계정 탭에서 먼저 연결하라고 안내한다. 알림을 켤 수 없다 | 수동 확인 |
| 서버가 승인 경로를 아직 제공하지 않는다 (`404`, JSON 아님) | "서버에 승인 기능이 아직 없어요"를 표시한다 | `FR03_missing_server_feature_is_explained` |
| 서버에 연결하지 못했다 | 목록을 지우지 않고 연결 오류를 표시한다. 알림 서비스는 다음 주기에 다시 묻는다 | `FR03_network_error_keeps_polling` |
| 토큰이 틀렸다 (`401`) | 토큰을 확인하라고 안내한다. 알림 서비스는 멈추고 알림으로 알린다 | `FR03_invalid_token_stops_polling` |
| 결정이 `409 ALREADY_DECIDED`·`POLICY_VERSION_MISMATCH`·`REQUEST_EXPIRED`다 | 사유를 구분해서 안내하고 목록을 새로 고친다 | `FR03_decision_reasons_are_distinguished` |
| 결정이 `403 NOT_AUTHORIZED`·`404 NOT_FOUND`다 | 권한이 없거나 없는 요청이라고 안내한다 | `FR03_decision_reasons_are_distinguished` |
| 같은 승인을 두 번 눌렀다 | 서버가 `200`과 기존 결과를 보내므로 성공으로 보여 준다. 요청 중에는 버튼을 막는다 | `FR03_decision_posts_choice` |
| 같은 승인 요청을 여러 번 조회했다 | 알림은 승인 ID마다 한 번만 띄운다 | `FR03_notifies_each_approval_once` |
| 승인 목록 응답이 배열이거나 `{"approvals": [...]}`다 | 둘 다 읽는다 | `FR03_reads_list_in_both_shapes` |
| 대기 만료 시각이 지났다 | 목록에서 만료로 표시하고 결정 버튼을 막는다 | `FR03_remaining_time_label` |
| 알림 권한이나 로컬 네트워크 권한이 없다 | 알림 켜기 전에 요청한다. 거부하면 켜지 않고 이유를 안내한다 | 수동 확인 |

## 6. 결정

- 알림은 포그라운드 서비스의 주기 조회로 받는다 (담당자 선택) — 독립 AP에서 인터넷 없이 동작하고, 앱이 꺼져 있어도 알린다. 대기 만료가 10분이라 WorkManager의 최소 주기(15분)로는 늦다.
- 조회 주기는 10초다 — 대기 만료 10분 안에 충분히 알리고, 서버 부하가 작다. 앱 상수다.
- 포그라운드 서비스 유형은 `specialUse`다 — `dataSync`는 Android 15부터 하루 6시간 제한이 있다.
- 새 요청 알림은 중요도 높음 채널, 조회 중 상시 알림은 중요도 낮음 채널을 쓴다.
- 이미 알린 승인 ID는 폰에 저장해서 서비스가 다시 시작해도 같은 요청을 다시 알리지 않는다. 대기 목록에서 사라진 ID는 지운다.
- 화면은 아래 탭 "승인"을 더한다. 계정 화면의 HTTP 처리(`RestClient`)를 계정·승인 API가 함께 쓴다.
- 동작은 `open`→"열기", `on`→"켜기", `off`→"끄기"로 보여 준다.

## 7. 가정

- 서버 승인 REST가 아직 없다 — `shared`의 CORE 승인 조회·결정 인터페이스가 필요하다. 이슈 초안을 담당자에게 넘긴다. 그 전까지 승인 탭은 "서버에 승인 기능이 아직 없어요"를 보여 준다.
- `GET /approvals`의 목록 응답은 배열 또는 `{"approvals": [...]}`다 — §5 REST는 "승인 요청 목록"이라고만 정했다. 서버 구현에서 정하면 한쪽만 남긴다.
- 알림 수단은 포그라운드 서비스의 주기 조회다 — FR-03의 미결이다. 다른 수단으로 정해지면 앱 안만 바꾸면 된다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-03 | `FR03_lists_pending_approvals_with_token` | `GET /approvals`에 토큰을 보내고 목록을 읽는다 |
| FR-03 | `FR03_reads_list_in_both_shapes` | 배열과 객체 응답을 모두 읽는다 |
| FR-03 | `FR03_decision_posts_choice` | 결정을 `approved`·`denied`로 보내고 결과를 읽는다 |
| FR-03 | `FR03_decision_reasons_are_distinguished` | 거부 사유마다 다른 안내가 있다 |
| FR-03 | `FR03_missing_server_feature_is_explained` | 승인 경로가 없는 서버를 구분한다 |
| FR-03 | `FR03_notifies_each_approval_once` | 새 승인 ID만 알리고, 사라진 ID는 잊는다 |
| FR-03 | `FR03_network_error_keeps_polling` | 연결 오류에도 조회를 계속한다 |
| FR-03 | `FR03_invalid_token_stops_polling` | 토큰 오류면 조회를 멈춘다 |
| FR-03 | `FR03_remaining_time_label` | 남은 시간과 만료를 표시한다 |
| FR-14, NFR-08 | `FR14_approval_item_has_no_requester` | 승인 항목에 요청자 정보가 없다 |
| FR-03 | 수동: 가짜 승인 서버와 에뮬레이터 | 알림이 뜨고, 누르면 승인 탭이 열리고, 승인하면 목록에서 사라진다 |
