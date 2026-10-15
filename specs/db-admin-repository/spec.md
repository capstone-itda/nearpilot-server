# 관리 쓰기 저장 (`AdminRepository`)

- 상태: 작성 중
- 담당: paiwhy (DB)
- 요구사항: FR-01, FR-12A, FR-14, FR-16, NFR-08
- 따르는 결정: #29, #30, #41, #42, #131 (PR #132)

## 1. 왜

- `shared/interfaces.py`에는 관리 기능, 시드 스크립트, 수집 도구가 쓸 `AdminRepository`가 있다. `db/`에는 구현이 없다.
- 구현이 없으면 계정, 비콘, 장소, 정책을 테스트 데이터로만 넣을 수 있다. 시연 환경을 만들 시드 스크립트를 쓸 수 없다.
- 구현이 없으면 `tools/data_collection`이 수집 회차를 기록하지 못한다. 하드웨어가 오면 바로 RSSI를 모아야 한다 (#131).

## 2. 무엇

- 입력: `AccountRegistration`, `BeaconRegistration`, `(node_id, TrustState)`, `PlaceAssignment`, `(PolicyRevision, expected_version)`, `start_collection(collection_id, beacon, label, started_at)`, `end_collection(collection_id, ended_at)`
- 출력: `AdminWriteResult(outcome, reason)`
- 바뀌는 상태: `user`, `beacon`, `node.trust_state`, `place`, `node.place_id`, `access_policy`, `policy_revision`, `collection_run`, `audit_log`
- 약속:
  - `AdminRepository`의 함수 7개(`register_account`, `register_beacon`, `set_node_trust`, `assign_place`, `set_policy`, `start_collection`, `end_collection`)를 모두 구현한다.
  - 쓰기 하나는 감사 기록과 함께 `BEGIN IMMEDIATE` 트랜잭션 하나다.
  - 거부하거나 저장에 실패하면 아무것도 바뀌지 않는다.

## 3. 제약

- 각 쓰기는 감사와 함께 한 트랜잭션으로 반영한다. 감사 단계는 `admin`이다 (`interfaces.py`, `architecture.md` §6 `audit_log`).
- 인증·권한 검사는 하지 않는다. CORE 관리 기능이 맡는다 (`interfaces.py`).
- 감사 기록에 토큰 원문을 남기지 않는다. `token_hash`도 감사 payload에 넣지 않는다 (NFR-08, #29).
- `register_beacon`
  - 같은 `beacon_id`·같은 계정·같은 `BeaconKey`로 다시 등록하면 `APPLIED`다. `is_active`대로 켜거나 끈다 — 켜면 같은 계정의 다른 비콘을 끈다 (재활성화).
  - 같은 `beacon_id`인데 계정이나 `BeaconKey`가 다르면 `CONFLICT`·`STATE_CONFLICT`다 — 다른 비콘으로 바뀌는 등록은 거부한다.
  - 다른 `beacon_id`로 같은 `BeaconKey`를 등록하면: 다른 계정이면 `BEACON_ALREADY_REGISTERED`(FR-01), 같은 계정이면 `CONFLICT`·`STATE_CONFLICT`다 — 같은 계정이 같은 키로 `beacon_id`만 바꿔 중복 등록하는 것을 막는다.
  - 계정당 활성 비콘은 하나 이하다. `is_active=True`면 같은 계정의 다른 비콘을 끈다. 끄기와 등록·활성화는 한 트랜잭션이다 (#42).
  - 거부하거나 저장에 실패하면 기존 활성 비콘을 유지한다.
  - `is_active=False` 등록은 같은 계정의 다른 비콘을 끄지 않는다.
- `set_node_trust`는 신뢰 상태만 바꾼다. 기기 상태와 점유 세션은 바꾸지 않는다.
- `assign_place`는 장소를 등록하고 기기를 연결한다. 점유 상태는 바꾸지 않는다.
  - 같은 `place_id`·같은 소유자·같은 이름으로 다시 호출하면 `APPLIED`다 — 넘겨준 기기를 연결하고, 이미 연결된 기기는 그대로 둔다(멱등, 기기 목록을 늘릴 수 있다).
  - 같은 `place_id`인데 소유자나 이름이 다르면 `CONFLICT`·`STATE_CONFLICT`다.
- `set_policy`
  - 새 리비전 추가와 `current_version` 갱신은 원자적이다. 과거 리비전은 바꾸지 않는다.
  - `expected_version`이 현재 버전과 다르면 `POLICY_VERSION_MISMATCH`다. 최초 등록이면 `expected_version`은 `None`이다.
  - 정책 변경은 진행 중인 점유와 이미 받은 승인에 소급하지 않는다 (FR-14). DB는 기존 행을 바꾸지 않는다.
- `start_collection` (#131)
  - `ended_at`은 `None`으로 만든다. `label`은 기기 앞(`node_id`) 또는 `none`이다 (FR-16).
  - 같은 비콘의 진행 중 회차나 끝난 회차와 구간이 겹치면 `COLLECTION_OVERLAP`이다. 끝 시각과 다음 시작 시각이 같으면 겹치지 않는다.
  - 등록되지 않은 비콘은 `BEACON_NOT_FOUND`, 없는 `node_id` 라벨은 `NODE_NOT_FOUND`, 이미 있는 `collection_id`는 `STATE_CONFLICT`다.
- `end_collection` (#131)
  - `ended_at`이 `started_at`보다 뒤가 아니면 `INVALID_INTERVAL`이다.
  - 없는 회차는 `COLLECTION_NOT_FOUND`, 이미 끝난 회차는 `STATE_CONFLICT`다.
  - 구간은 `started_at <= frame_ts < ended_at`이다. 관측 연결은 기존 `save_rssi`가 한다.
- 시각은 UTC ISO 8601 문자열로 저장한다 (`db-repository` spec 결정).

## 4. 경계

### 고치는 곳
- `server/src/nearpilot/db/repositories/`
- `server/src/nearpilot/db/schema/`
- `server/tests/db/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `core/`, `api/`, `iot/`, `tools/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `AdminRepository` 전체 | 제공 | 관리 기능, 시드 스크립트 |
| `start_collection`, `end_collection` | 제공 | 근접 추정 (`tools/data_collection`) |

### 만들지 않는 것
| 후속 | 내용 | 근거 |
|---|---|---|
| `db-recovery-retention` | 보존기간 정리, 완료 20분 뒤 `request_id` 비우기, 중복 방지 10만 건 한도, 감사 JSONL 내보내기 | #15, #34 |
| — | `#76` 오류 거부 재판정 | 은지 확인 대기 |
| — | 시드 스크립트 자체 | 관리 기능·도구 담당 |

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 새 계정을 등록한다 | `APPLIED`, 감사 1건 | `test_FR01_register_account_applied` |
| 같은 `user_id`로 다시 등록한다 | `REJECTED`, `ACCOUNT_ALREADY_EXISTS`. 기존 행을 바꾸지 않는다 | `test_FR01_register_account_duplicate` |
| 다른 계정과 같은 `token_hash`로 등록한다 | `REJECTED`, `ACCOUNT_ALREADY_EXISTS` | `test_FR01_register_account_token_hash_taken` |
| 감사 기록에 `token_hash`가 남는가 | 남지 않는다 | `test_NFR08_admin_audit_has_no_token` |
| 새 비콘을 활성으로 등록한다 | `APPLIED`. 같은 계정의 기존 활성 비콘은 꺼진다 | `test_FR01_register_beacon_swaps_active` |
| 비활성으로 등록한다 | 기존 활성 비콘을 유지한다 | `test_FR01_register_beacon_inactive_keeps_active` |
| 다른 계정에 같은 `BeaconKey`가 있다 | `REJECTED`, `BEACON_ALREADY_REGISTERED`. 기존 활성 비콘 유지 | `test_FR01_register_beacon_taken_by_other` |
| 없는 계정의 비콘이다 | `REJECTED`, `ACCOUNT_NOT_FOUND` | `test_FR01_register_beacon_account_missing` |
| 같은 `beacon_id`·같은 계정·같은 키로 다시 활성으로 등록한다 | `APPLIED`. 그 비콘이 활성, 다른 비콘은 꺼진다 | `test_FR01_register_beacon_reactivate` |
| 같은 `beacon_id`인데 계정이 다르다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR01_register_beacon_same_id_other_account` |
| 같은 `beacon_id`인데 `BeaconKey`가 다르다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR01_register_beacon_same_id_other_key` |
| 다른 `beacon_id`로 같은 키를, 같은 계정이 등록한다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR01_register_beacon_same_account_new_id` |
| 두 활성 등록이 동시에 온다 | 계정당 활성 비콘은 끝까지 하나다 | `test_NFR03_register_beacon_concurrent_one_active` |
| 기기 신뢰 상태를 바꾼다 | `APPLIED`. `device_state`와 세션은 그대로다 | `test_FR16_set_node_trust_only_trust` |
| 없는 기기다 | `REJECTED`, `NODE_NOT_FOUND` | `test_FR16_set_node_trust_missing` |
| 장소를 만들고 기기를 연결한다 | `APPLIED`. 기기 점유 상태는 그대로다 | `test_FR12A_assign_place_applied` |
| 장소 소유자가 없는 계정이다 | `REJECTED`, `ACCOUNT_NOT_FOUND` | `test_FR12A_assign_place_owner_missing` |
| 연결할 기기 중 하나가 없다 | `REJECTED`, `NODE_NOT_FOUND`. 아무것도 바뀌지 않는다 | `test_FR12A_assign_place_node_missing_atomic` |
| 같은 `place_id`를 다른 소유자로 다시 등록한다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR12A_assign_place_owner_conflict` |
| 같은 `place_id`를 같은 소유자·같은 이름으로 다시 호출한다 (기기가 늘었다) | `APPLIED`. 새 기기를 연결하고 기존 연결은 그대로 둔다 | `test_FR12A_assign_place_idempotent_adds_nodes` |
| 같은 `place_id`·같은 소유자인데 이름이 다르다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR12A_assign_place_name_conflict` |
| 정책을 처음 등록한다 (`expected_version=None`) | `APPLIED`. `current_version`이 1이다 | `test_FR14_set_policy_first` |
| 다음 버전을 등록한다 | `APPLIED`. 과거 리비전은 그대로다 | `test_FR14_set_policy_next_version` |
| `expected_version`이 현재와 다르다 | `CONFLICT`, `POLICY_VERSION_MISMATCH` | `test_FR14_set_policy_version_mismatch` |
| 새 리비전 번호가 현재+1이 아니다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR14_set_policy_version_gap` |
| 정책이 이미 있는데 `expected_version=None`이다 | `CONFLICT`, `POLICY_VERSION_MISMATCH` | `test_FR14_set_policy_first_but_exists` |
| 없는 기기나 없는 승인자다 | `REJECTED`, `NODE_NOT_FOUND` 또는 `ACCOUNT_NOT_FOUND` | `test_FR14_set_policy_missing_refs` |
| 두 정책 변경이 같은 기대 버전으로 동시에 온다 | 하나만 `APPLIED`, 나머지는 `POLICY_VERSION_MISMATCH` | `test_NFR03_set_policy_concurrent` |
| 정책을 바꿔도 기존 승인과 세션은 그대로다 | 기존 행이 바뀌지 않는다 | `test_FR14_set_policy_no_retroactive` |
| 수집 회차를 시작한다 | `APPLIED`, `ended_at`은 비어 있다 | `test_FR16_start_collection_applied` |
| 같은 비콘에 진행 중인 회차가 있다 | `REJECTED`, `COLLECTION_OVERLAP` | `test_FR16_start_collection_overlap_open` |
| 끝난 회차와 구간이 겹친다 | `REJECTED`, `COLLECTION_OVERLAP` | `test_FR16_start_collection_overlap_closed` |
| 이전 회차 끝 시각과 새 시작 시각이 같다 | `APPLIED` | `test_FR16_start_collection_touching_ok` |
| 등록되지 않은 비콘이다 | `REJECTED`, `BEACON_NOT_FOUND` | `test_FR16_start_collection_beacon_missing` |
| 라벨이 없는 `node_id`다 | `REJECTED`, `NODE_NOT_FOUND` | `test_FR16_start_collection_label_missing` |
| 라벨이 `none`이다 | `APPLIED` | `test_FR16_start_collection_label_none` |
| 이미 있는 `collection_id`다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR16_start_collection_duplicate_id` |
| 두 시작이 같은 비콘에 동시에 온다 | 하나만 `APPLIED` | `test_NFR03_start_collection_concurrent` |
| 회차를 끝낸다 | `APPLIED` | `test_FR16_end_collection_applied` |
| 끝 시각이 시작 시각과 같거나 앞이다 | `REJECTED`, `INVALID_INTERVAL` | `test_FR16_end_collection_invalid_interval` |
| 없는 회차다 | `REJECTED`, `COLLECTION_NOT_FOUND` | `test_FR16_end_collection_missing` |
| 이미 끝난 회차다 | `CONFLICT`, `STATE_CONFLICT` | `test_FR16_end_collection_already_ended` |
| 끝낸 회차 구간의 관측이 `save_rssi`로 연결된다 | `started_at <= frame_ts < ended_at`인 관측만 연결된다 | `test_FR16_collection_links_observations` |

## 6. 결정

- 결과 종류는 입력 검증 실패(없는 대상, 중복 등록, 구간 겹침, 잘못된 구간)를 `REJECTED`로, 현재 상태와 맞지 않는 경우(버전 불일치, 이미 끝난 회차, 이미 있는 ID, 소유자 충돌)를 `CONFLICT`로 둔다 — `AdminWriteOutcome`의 두 값을 "다시 보내도 안 되는 입력"과 "지금 상태와 어긋난 입력"으로 나눈다.
- 같은 내용의 재등록도 거부로 돌려준다는 결정은 `register_account`에만 적용한다 — 같은 `user_id`로 다시 등록하면 `ACCOUNT_ALREADY_EXISTS`다. `AdminWriteOutcome`에 `ALREADY_APPLIED`가 없다. 시드 스크립트는 이 사유를 "이미 있음"으로 보고 넘어가면 된다. `register_beacon`과 `assign_place`는 각자의 멱등 규칙(3절)을 따른다 — 호출자가 같은 입력을 다시 보내는 시드·관리 흐름에서 매번 거부당하지 않게 한다.
- 감사 payload에는 바꾼 대상의 ID와 바뀐 값만 넣는다. `token_hash`는 넣지 않는다 — NFR-08.
- 활성 비콘 교체는 기존 활성 비콘을 먼저 끄고 새 비콘을 켠다 — `beacon_one_active_per_user` 부분 UNIQUE 인덱스가 트랜잭션 중간에도 위반되지 않게 한다.
- 수집 회차 겹침 검사는 `BEGIN IMMEDIATE` 안에서 조회와 삽입을 함께 한다 — 동시에 두 회차가 열려도 하나만 들어간다.
- `AdminRepository` 구현은 `db/repositories/admin_repository.py`의 `SqliteAdminRepository`에 둔다. `SqliteRepository`와 같은 연결을 받는다 — DB 쓰기 경로는 하나다 (`AGENTS.md` 의존 규칙 5).
- `AdminWriteReason.PLACE_NOT_FOUND`는 이번에 쓰지 않는다 — `assign_place`의 `place_id`는 새로 만드는 대상이라 "없음"을 거부할 상황이 없다.

## 7. 가정

- #131 이슈 댓글에서 "같은 끝 시각으로 다시 끝내면 성공"이라고 했지만, PR #132가 반영한 계약은 "이미 끝난 회차는 `STATE_CONFLICT`"다. 계약을 따른다.
- 점유 중인 기기의 장소·정책 변경 제한은 `architecture.md` §4의 미결이다. 이 작업은 점유 여부와 상관없이 `assign_place`·`set_policy`를 허용한다. 제한이 정해지면 관리 기능(CORE)이 검사한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-01 | `test_FR01_*` | 계정·비콘 등록, 활성 비콘 교체 |
| FR-12A | `test_FR12A_*` | 장소 등록과 기기 연결 |
| FR-14 | `test_FR14_set_policy_*` | 정책 리비전과 버전 검사, 소급 없음 |
| FR-16 | `test_FR16_*` | 신뢰 상태, 수집 회차 시작·끝, 관측 연결 |
| NFR-03 | `test_NFR03_*` | 동시 쓰기에서 한쪽만 반영 |
| NFR-08 | `test_NFR08_admin_audit_has_no_token` | 감사에 토큰이 남지 않는다 |
