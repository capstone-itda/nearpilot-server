<!--
spec 규칙
- spec 하나는 담당자 한 명이 feat PR로 끝내는 작업 하나를 다룬다.
- 경로는 specs/{계층}-{요약}/spec.md 다. 이름은 브랜치 이름과 같다.
- 1~5절(왜, 무엇, 제약, 경계, 예외 상황)은 에이전트에게 주는 작업 계약이다.
  다섯 절을 모두 채운다. 쓸 내용이 없는 절에는 "없음"이라고 쓴다.
- 요구사항 전문은 복사하지 않는다. 이 작업에 적용되는 내용만 한 줄로 쓰고, 끝에 출처 ID를 붙인다.
- 아래를 바꿔야 하면 팀 결정이다. spec에 쓰지 않고 이슈를 올린다.
  shared 계약, 다른 영역과 주고받는 입력·출력·부작용·타이밍, MCP·MQTT·REST 계약,
  DB 엔터티 관계, docs 내용, 미결 수치, 공용 설정
-->

# <작업 이름>

# FR-18 결정론적 판정 파이프라인 (⓪~⑥)

- 상태: 구현 중
- 담당: eunji719 (판정)
- 요구사항: FR-18
- 따르는 결정: #23, #24, #25, #26

## 1. 왜

- FR-18은 `request_use`의 실행 허가를 ⓪부터 ⑥까지 정해진 순서로 판정하라고 요구한다. 기본값은 거부다.
- 저장소에는 이 순서를 실행하는 코드가 없다. `shared/`에는 계약과 `Verdict`, `Step`, `ReasonCode`만 있다.
- 단계마다 구현하는 역할이 다르다. 단계를 부르는 순서와 멈추는 때를 정하는 곳이 없으면 아래 문제가 생긴다.
  - 정책이나 점유 때문에 다른 물리 기기를 고를 수 있다.
  - 호스트나 발화에 따라 단계를 건너뛸 수 있다.
  - 같은 요청을 재전송하면 판정이 달라지거나 기기가 두 번 실행될 수 있다.

## 2. 무엇

- 입력: `request_use` 요청 (`UseRequest`)
- 출력: 판정 (`Decision`). 판정 값은 `Verdict`, 사유는 `ReasonCode`다.
- 바뀌는 상태:
  - 대여형 요청이 ⑤⑥을 통과하면 `RESERVED` 점유 세션이 생긴다.
  - 단계마다 입력과 결과가 감사 기록에 남는다.
- 약속:
  - core는 항상 ⓪부터 ⑥까지 같은 순서로 검사한다.
  - 한 단계가 실패하면 core는 그 단계의 판정과 사유 코드를 반환한다. 뒤 단계는 실행하지 않는다.
  - 모든 단계를 통과한 요청만 `OK`다.
  - `OK` 뒤에는 실행 직전 재검사로 허가, 근접, 대상을 다시 검사한다.
  - 같은 입력에는 같은 판정을 반환한다 (FR-10, NFR-09).
  - 판정 한 건에는 설정 버전 하나를 적용한다. 내부 요청 ID로 판정을 재현할 수 있다 (FR-22).

| 단계 | 실패 조건 | 판정 / 사유 코드 |
|---|---|---|
| ⓪ 중복 | 같은 `(계정, request_id)`에 다른 정규화 입력이 왔다. 정규화 입력은 6절을 따른다 | `DENY` / `REQUEST_ID_CONFLICT` |
| ① 비콘 | 최근 T초 안에 수신한 등록 비콘이 없다 | `DENY` / `BEACON_NOT_SEEN` |
| ② 후보 | 신뢰, 온라인, 요청 종류를 만족하고 `FAULT`가 아닌 기기가 없다 | `DENY` / `NO_CANDIDATE` |
| ② 후보 | 명시 지정 대상이 위 조건 밖이다 | `DENY` / `TARGET_UNAVAILABLE` |
| ③ 근접 | 사후확률 최대 기기의 확률이 설정 임계값 미만이다 | `ASK` / `LOW_CONFIDENCE` |
| ③ 근접 | 명시 지정 대상의 확률이 설정 임계값 미만이다 | `ASK` / `MOVE_CLOSER` |
| ④ 정책 | 승인이 필요하다. 승인 대기, 거부, 만료를 사유로 구분한다 | `NEED_APPROVAL` |
| ④ 정책 | 정책이 허용하지 않는다 | `DENY` |
| ⑤ 기기 점유 | 대상 기기가 이미 점유 중이다 | `BUSY` / `DEVICE_BUSY` |
| ⑥ 사용자 점유 | 계정이 같은 `exclusive_group`의 다른 기기를 점유한다 | `BUSY` / `USER_BUSY` |

- ③에서 대상이 확정된다. ④는 확정된 대상만 검사한다.
- ⑤⑥은 `Repository.reserve_if_free()` 한 번으로 검사하고 `RESERVED`를 만든다.
- 공용형(`shared`) 기기는 ⑤⑥과 점유 세션 생성을 건너뛴다.

## 3. 제약

- 기본값은 거부다. 예외, 누락, 알 수 없는 상태는 `OK`가 되지 않는다 (FR-18).
- 공용형의 ⑤⑥ 생략 외에는 호스트나 발화로 단계를 건너뛰지 않는다 (FR-18).
- 정책과 점유는 대상을 확정한 뒤에만 검사한다. 검사 결과로 다른 물리 대상을 고르지 않는다 (FR-18).
- decision은 트랜잭션을 직접 열지 않는다. ⑤⑥은 `reserve_if_free()`만 호출한다 (FR-19, NFR-03).
- decision은 다른 계층에 `shared/interfaces.py`의 Protocol로만 의존한다 (`AGENTS.md` 의존 규칙 4).
- 임계값과 T는 설정에서 받는다. 적용한 값과 설정 버전을 감사 기록에 남긴다 (requirements.md §1.2, FR-22).
- `Decision`에는 사용자 ID와 점유 주체를 넣지 않는다. 응답 변환은 api가 한다 (FR-11, NFR-08).
- 서버 판정 시간은 median 1초 이하, p95 2초 이하다 (NFR-01).

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
| `Repository.link_request` (⓪) | 사용 | DB |
| `ProximityEstimator.posterior` (③) | 사용 | 근접 추정 |
| `PolicyChecker.check` (④) | 사용. `core/policy`가 제공한다 (6절) | DB |
| `Repository.reserve_if_free` (⑤⑥) | 사용 | DB |
| `Repository.save_decision` | 사용 | DB |
| `AuditLog.append` | 사용 | DB |
| 실행 직전 재검사 | 미결 (7절) | 상태 관리 |

### 만들지 않는 것
- ①②③ 단계 내부 로직의 세부 (FR-13, FR-15, FR-16, FR-17)
- 전역 할당 (FR-23, `any:locker`)
- MCP 도구와 토큰 식별 (FR-08, FR-09, FR-10, FR-11)
- 기기 실행과 명령 전송 (FR-06)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 같은 `(계정, request_id)`와 같은 정규화 입력이 다시 온다 | 기존 판정과 진행 상태를 반환한다 | `test_FR18_같은_키_같은_입력이면_기존_판정을_반환한다` |
| 같은 `(계정, request_id)`에 다른 입력이 온다 | `DENY` / `REQUEST_ID_CONFLICT`를 반환한다 | `test_FR18_같은_키_다른_입력이면_REQUEST_ID_CONFLICT이고_저장하지_않는다` |
| 다른 계정이 같은 `request_id`를 보낸다 | 다른 계정의 결과를 반환하지 않는다 | |
| 단계 실행 중 예상하지 못한 예외가 난다 | `DENY` / `INTERNAL_ERROR`를 반환한다. 호스트 응답에 예외 내용을 넣지 않는다 | `test_FR18_단계에서_예외가_나면_INTERNAL_ERROR로_거부한다` |
| 두 요청이 같은 기기를 동시에 요청한다 | `reserve_if_free()` 결과를 따른다. 실패한 요청은 `BUSY`다 | `test_FR18_FR19_점유_실패는_reserve_if_free_결과를_따른다` |
| 실행 직전 재검사에서 자기 요청의 예약을 만난다 | 타인 점유로 보지 않는다 | |
| 실행 직전 재검사가 실패한다 | 예약과 승인 사용분 반환을 누가 시작하는지 미결 (7절) | |

## 6. 결정

- ④는 `core/policy`가 제공하는 `PolicyChecker.check()` 한 번으로 검사한다 (#23). 판정은 반환된 `PolicyCheckResult`를 `Decision`에 반영하고, 정책 모드와 승인 범위 규칙을 직접 조합하지 않는다.
  - 반환 값: `verdict`(`OK`, `NEED_APPROVAL`, `DENY`), 사유, 적용한 `policy_version`(정책이 없는 기기는 `None`), `approval_id`
  - 정책이 없는 기기는 `DENY`로 거부한다.
  - 승인 사용 횟수 소진(`APPROVAL_EXHAUSTED`)과 승인과 다른 동작(`ACTION_NOT_ALLOWED`)은 `NEED_APPROVAL`이다. 다시 승인받으면 되기 때문이다.
  - `approval_id`는 ⑤⑥의 `ReserveRequest.approval_id`로 넘긴다.
- ⓪의 `input_digest`는 판정이 계산해서 `link_request()`에 넘긴다. DB는 저장하고 비교만 한다 (#24).
  - 정규화 입력에 넣는 필드: `target`(정규화한 대상 지정), `action`, `parent_internal_request_id`
  - 넣지 않는 필드: `request_id`(키 자체다), `received_at`, `host_kind`. 호스트가 바뀌어도 재전송은 같은 요청이다.
- 예상하지 못한 예외는 `DENY`와 `INTERNAL_ERROR`로 거부한다 (#25). 호스트 응답에는 일반 안내만 넣고, 예외 내용은 감사 기록에만 남긴다.
- ③은 명시 지정 요청에도 `posterior()`에 ②의 전체 후보를 넘긴다 (#26). 지정 대상 하나만 넘기면 확률의 합이 1이라서 임계값 검사가 무의미해진다.
  - 판정이 `posteriors[지정 대상]`을 읽어 임계값과 비교한다. 미만이면 `ASK` / `MOVE_CLOSER`다.
  - 지정 대상이 후보에 없으면 `posterior()`를 부르지 않고 `DENY` / `TARGET_UNAVAILABLE`로 거부한다.

- `input_digest`는 정규화 입력을 JSON으로 직렬화한 문자열의 SHA-256 16진 문자열이다.
  - 직렬화: 키를 정렬하고 공백 없이(`separators=(",", ":")`), `ensure_ascii=False`로 쓴다.
  - 내용: `{"action", "parent_internal_request_id", "target": {"capability", "kind", "node_id"}}`. 값은 enum의 문자열 값이다.
- 구조: `core/decision/pipeline.py`의 `DecisionPipeline` 클래스 하나가 ⓪~⑥을 순서대로 실행한다. 의존하는 계층과 `Settings`는 생성자로 받는다. `input_digest` 계산은 `digest.py`에 둔다.
- 판정 시각은 `UseRequest.received_at`이다. 시계를 따로 읽지 않으므로 같은 입력에는 같은 판정이 나온다 (NFR-09).

## 7. 가정

- ① T와 ② 온라인 TTL은 `Settings.beacon_recent_sec`, `Settings.online_ttl_sec`를 쓴다. 값은 미결(#15)이고 판정 코드는 값을 모른다.
- ②는 `trust_state == TRUSTED`, `last_seen_at`이 TTL 안, `device_state != FAULT`, `capability` 일치를 후보 조건으로 본다. `RESERVED`·`ACTIVE` 기기도 후보다. 점유는 ⑤에서 검사한다.
- 대상 지정이 `any:`면 전역 할당(FR-23)이 맡는다. 이 작업은 만들지 않으므로 ②에서 `DENY` / `INVALID_INPUT`으로 거부한다.
- 임계값은 후보가 아니라 요청한 `capability`의 설정(`threshold_locker`, `threshold_light`)을 쓴다.
- ③ `ASK` / `LOW_CONFIDENCE`의 `node_id`는 `None`, `p_max`는 최대 확률이다. `MOVE_CLOSER`의 `node_id`는 지정 대상이다.
- ⓪에서 같은 키에 판정이 이미 끝났으면(`previous`) 그 판정을 반환하고 다시 저장하지 않는다. 충돌(`conflict`)이면 `save_decision()`을 부르지 않는다. 기존 판정을 덮어쓰지 않기 위해서다.
- ⓪에서 같은 키가 이미 있는데 판정이 아직 없으면(진행 중) `DENY` / `INTERNAL_ERROR`로 거부한다. 두 번째 판정 실행이 이중 예약을 만들지 않게 하기 위해서다.
- `reserve_if_free()`가 예외를 내면 `INTERNAL_ERROR`로 거부한다. 반영 여부가 불명인 예약의 정리는 이 작업 밖이다.
- 공용형 기기에서 정책이 `approval_id`를 반환하면(승인 사용분 필요) `DENY` / `POLICY_DENIED`로 거부한다. 승인 사용분 확보 접점이 정해지기 전까지 원자적 확보를 보장할 수 없어서다 (아래 미결).
- 이 작업은 `Decision`만 반환한다. 실행 인계(`ExecutionService.execute`)와 `ExecutionContext` 조립은 만들지 않는다. 실행 직전 재검사 위치가 미결이다 (아래).
- 미결: ① T 값과 ② 온라인 TTL 값. requirements.md FR-15, FR-13의 미결과 같다.
- 미결: 후보가 있는데 관측이 없을 때 `posterior()`가 빈 `posteriors`를 반환하는가, 균등 분포를 반환하는가. 판정은 빈 `posteriors`를 `LOW_CONFIDENCE`로 처리하려 한다. 근접 추정 담당에게 확인한다 (#26).
- 미결: 실행 직전 재검사의 위치. decision이 제공하고 lifecycle이 호출하는가, 아니면 lifecycle 안에서 하는가. 재검사가 실패하면 예약과 승인 사용분 반환을 누가 시작하는가.
- 미결: 오류로 거부한 요청을 같은 `request_id`로 재전송할 때 다시 판정할지. #25는 이 부분을 정하지 않았다.
- 미결: 공용 조명의 승인 사용분 확보. 공용 조명은 ⑤⑥을 건너뛰지만 승인 사용분은 원자적으로 확보해야 한다 (FR-18). `reserve_if_free()`로 처리할지, 다른 DB 호출이 필요한지 정해야 한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-18 | `test_FR18_단계를_0부터_순서대로_검사한다` | 단계를 ⓪부터 ⑥까지 순서대로 검사한다 |
| FR-18 | `test_FR18_비콘이_없으면_거부하고_뒤_단계를_실행하지_않는다` 외 | 첫 실패 단계에서 멈추고 뒤 단계를 실행하지 않는다 |
| FR-18 | `test_FR18_단계에서_예외가_나면_INTERNAL_ERROR로_거부한다` 외 | 예외, 누락, 알 수 없는 상태는 `OK`가 되지 않는다 |
| FR-11 | `test_FR11_Decision에는_사용자_ID가_없다` | 판정에 사용자 ID가 없다 |

- 구현은 `server/tests/core/test_decision_pipeline.py`다.

- 다른 계층의 구현 없이 fake만으로 확인한다.
