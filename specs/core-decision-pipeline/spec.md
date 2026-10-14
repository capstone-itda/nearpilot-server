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

- 상태: 작성 중
- 담당: eunji719 (판정)
- 요구사항: FR-18
- 따르는 결정: #15, #23, #24, #25, #26, #27, #33, #34, #35, #39

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
  - 최초 대여가 ⑤⑥을 통과하면 `RESERVED` 점유 세션이 생긴다. 본인 재조작은 기존 `ACTIVE` 세션에 연결한다.
  - 공용형 제한 기기의 요청은 승인 사용분의 `reserved` 행만 생긴다. 기기 상태와 점유 세션은 바뀌지 않는다.
  - 단계마다 입력과 결과가 감사 기록에 남는다.
  - 판정 한 건마다 판정 기록이 한 번 남는다.
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
| ⓪ 중복 | 새 요청인데 정리한 뒤에도 중복 방지 항목이 한도 이상이다 | `DENY` / `CAPACITY_EXCEEDED` |
| ① 비콘 | 최근 T초 안에 수신한 등록 비콘이 없다 | `DENY` / `BEACON_NOT_SEEN` |
| ② 후보 | 신뢰, 온라인, 요청 종류를 만족하고 `FAULT`가 아닌 기기가 없다 | `DENY` / `NO_CANDIDATE` |
| ② 후보 | 명시 지정 대상이 위 조건 밖이다 | `DENY` / `TARGET_UNAVAILABLE` |
| ③ 근접 | 사후확률 최대 기기의 확률이 설정 임계값 미만이다 | `ASK` / `LOW_CONFIDENCE` |
| ③ 근접 | 명시 지정 대상의 확률이 명시 지정 요청의 설정 임계값 미만이다 | `ASK` / `MOVE_CLOSER` |
| ④ 정책 | 승인이 필요하다. 승인 대기, 거부, 대기 만료, 승인 만료를 사유로 구분한다 | `NEED_APPROVAL` |
| ④ 정책 | 정책이 허용하지 않는다 | `DENY` |
| ⑤ 기기 점유 | 타인 점유이거나 본인 세션이 `ACTIVE`가 아니다 | `BUSY` / `DEVICE_BUSY` |
| ⑥ 사용자 점유 | 계정이 같은 `exclusive_group`의 다른 기기를 점유한다 | `BUSY` / `USER_BUSY` |

- ③에서 대상이 확정된다. ④는 확정된 대상만 검사한다.
- ⑤⑥은 `Repository.reserve_if_free()` 한 번으로 검사하고 `RESERVED`를 만든다.
- 공용형(`shared`) 기기는 ⑤⑥과 점유 세션 생성을 건너뛴다. 건너뛰는 주체는 DB다.
- 판정은 공용형 기기에도 `reserve_if_free()`를 호출한다 (6절).

## 3. 제약

- 기본값은 거부다. 예외, 누락, 알 수 없는 상태는 `OK`가 되지 않는다 (FR-18).
- 공용형의 ⑤⑥ 생략 외에는 호스트나 발화로 단계를 건너뛰지 않는다 (FR-18).
- 정책과 점유는 대상을 확정한 뒤에만 검사한다. 검사 결과로 다른 물리 대상을 고르지 않는다 (FR-18).
- decision은 트랜잭션을 직접 열지 않는다. ⑤⑥은 `reserve_if_free()`만 호출한다 (FR-19, NFR-03). decision은 기기의 점유 방식에 따라 호출을 나누지 않는다.
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
| `Repository.reserve_if_free` (⑤⑥) | 사용. 대여형과 공용형 모두 호출한다 (6절) | DB |
| `Repository.save_decision` | 사용. `DecisionRecord`를 넘긴다 (6절) | DB |
| `AuditLog.append` | 사용. `event_id`는 decision이 만든다 (6절) | DB |
| 실행 직전 재검사 | 제공. `DecisionRechecker.recheck`를 상태 관리가 호출한다 | 상태 관리 |

### 만들지 않는 것
- ①②③ 단계 내부 로직의 세부 (FR-13, FR-15, FR-16, FR-17)
- 전역 할당 (FR-23, `any:locker`)
- MCP 도구와 토큰 식별 (FR-08, FR-09, FR-10, FR-11)
- 기기 실행과 명령 전송 (FR-06)

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 같은 `(계정, request_id)`와 같은 정규화 입력이 다시 온다 | 기존 판정과 진행 상태를 반환한다 | |
| 같은 `(계정, request_id)`에 다른 입력이 온다 | `DENY` / `REQUEST_ID_CONFLICT`를 반환한다 | |
| 다른 계정이 같은 `request_id`를 보낸다 | 다른 계정의 결과를 반환하지 않는다 | |
| 보장기간이 지나 DB가 비운 `request_id`가 다시 온다 | 새 요청으로 판정한다 | |
| 새 요청인데 중복 방지 항목이 한도 이상이다 | `DENY` / `CAPACITY_EXCEEDED`를 반환한다. 재전송에는 기존 결과를 반환한다 | |
| 단계 실행 중 예상하지 못한 예외가 난다 | `DENY` / `INTERNAL_ERROR`를 반환한다. 호스트 응답에 예외 내용을 넣지 않는다 | |
| 두 요청이 같은 기기를 동시에 요청한다 | `reserve_if_free()` 결과를 따른다. 실패한 요청은 `BUSY`다 | |
| 공용형 제한 기기를 요청한다 | `reserve_if_free()`를 호출한다. `use_id`는 `None`이고 `usage_id`는 사용분 ID다 | |
| 공용형 `open` 기기를 승인 없이 요청한다 | `reserve_if_free()`를 호출한다. DB는 아무것도 쓰지 않고 `RESERVED`를 반환한다 | |
| 같은 `internal_request_id`에 다른 판정을 다시 저장한다 | DB가 충돌로 거부한다. decision은 기존 판정 기록을 덮어쓰지 않는다 | |
| 실행 직전 재검사에서 자기 요청의 예약을 만난다 | 타인 점유로 보지 않는다 | |
| 실행 직전 재검사가 실패한다 | 상태 관리가 전송을 막고 `cancel_not_sent`로 이번 요청의 예약과 승인 사용분을 정리한다 | |

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
- ③은 명시 지정 요청에도 `posterior()`에 ②의 전체 후보를 넘긴다 (#26).
  - 판정이 `posteriors[지정 대상]`을 읽어 임계값과 비교한다. 미만이면 `ASK` / `MOVE_CLOSER`다.
  - 지정 대상이 후보에 없으면 `posterior()`를 부르지 않고 `DENY` / `TARGET_UNAVAILABLE`로 거부한다.
- 판정은 실행 인계에 실행 종류 `execution_kind`를 넣는다 (#35). 실행 종류는 최초 대여, 기존 대여의 재조작, 공용형 조작이다.
  - 같은 요청의 재전송은 ⓪에서 기존 실행의 진행 상태나 결과를 반환한다. 새 명령을 만들지 않는다.
  - 대여 중인 본인의 새 조작은 새 요청이다. 판정은 새 요청을 기존 `use_id`에 연결해서 인계한다.
  - 판정은 재조작에도 본인 점유, 현재 접근 정책, 근접 조건을 검사한다. 대여시간 초과만으로 재조작을 막지 않는다 (FR-20, FR-21).
  - DB는 저장한 요청과 점유 세션의 연결을 검사한다. 판정은 인계 값만으로 기존 점유를 바꾸지 않는다.

- #35의 재조작은 `reserve_if_free`의 `reoperating` 결과로 식별한다. `ExecutionKind.RENTAL_REOPERATION`과 기존 `use_id`를 인계한다.
- 공용형 기기의 요청에도 `reserve_if_free()` 하나만 호출한다 (#27). 판정은 기기의 점유 방식에 따라 호출을 나누지 않는다.
  - DB가 기기의 `occupancy`를 보고 ⑤⑥과 점유 세션 생성을 건너뛴다. DB는 같은 `BEGIN IMMEDIATE`에서 승인 사용분만 예약한다.
  - 공용형의 `ReserveResult`는 `use_id`가 `None`이고 `usage_id`가 사용분 ID다.
  - 공용형 `open` 정책이고 `approval_id`가 `None`이면 DB는 아무것도 쓰지 않고 `RESERVED`를 반환한다.
- `save_decision()`에는 자유 형식 `context` 대신 `DecisionRecord`를 넘긴다 (#33).
  - 필드: `model_version`, `calib_version`, `policy_version`, `settings`, `posteriors`, 관측 구간
  - `ProximityResult`의 `model_version`, `calib_version`, `window_start`, `window_end`와 `posteriors`를 그대로 옮긴다. 근접 추정 전에 끝난 판정은 `None`이다.
  - 판정 한 건마다 `save_decision()`을 한 번 호출한다. `ASK`, `BUSY`, `DENY`도 저장한다.
  - 같은 `internal_request_id`에 다른 판정을 다시 저장하면 DB가 충돌로 거부한다.
- `AuditLog.append()`에 넘기는 `event_id`는 decision이 UUID로 만든다 (#34). DB는 같은 `event_id`를 한 번만 저장한다.
  - 감사 테이블이 원본이다. JSONL 기록이 실패해도 판정을 멈추지 않는다.
- `posteriors`는 근접 추정이 장소의 전체 상태로 계산한 값이다 (#39). 근접 추정은 후보끼리 다시 정규화하지 않는다. 합은 1 이하다.
  - 판정은 `posteriors`를 다시 정규화하지 않고 임계값과 비교한다. 비교 방식은 바뀌지 않는다.
  - 판정은 명시 지정 요청에 별도 임계값을 적용한다. 그 외 요청에는 기기 종류별 임계값을 적용한다. 두 값 모두 설정에서 받는다.
  - 감사 기록에는 다시 정규화하지 않은 후보 확률과 적용한 임계값을 남긴다 (FR-22).

- #39에서 판정이 장소 전체 기기 목록을 `place_nodes`로 넘긴다. 지정 여부와 대상은 근접 추정에 넘기지 않는다.

## 7. 가정

- ① T의 초기 기준은 3초(`beacon_recent_sec`), ② 온라인 유지 시간의 초기 기준은 15초(`online_ttl_sec`)다 (requirements.md §1.2). 판정은 두 값을 설정에서 받는다.
- 외부 요청의 중복 방지 보장기간이 지나면 DB가 외부 `request_id`를 비운다 (FR-22). 그 뒤 같은 `request_id`가 오면 ⓪은 새 요청으로 본다. 판정은 ⓪~⑥을 처음부터 검사한다.
- 신규 조작 접수 제한은 ⓪에서 새 요청으로 확인한 요청에만 적용한다 (FR-22). 한도 이상이면 `DENY` / `CAPACITY_EXCEEDED`다. 같은 요청의 재전송에는 기존 결과를 반환한다.
- 미결: DB가 한도 초과를 판정에 알리는 접점과 거부한 요청을 기록하는 방식. `link_request()`의 반환에 담을지, 별도 조회를 둘지 DB 담당과 정한다. 접점이 정해질 때까지 판정은 용량 부족 거부를 구현하지 않는다.
- 미결: 후보가 있는데 관측이 없을 때 `posterior()`가 빈 `posteriors`를 반환하는가, 균등 분포를 반환하는가. 판정은 빈 `posteriors`를 `LOW_CONFIDENCE`로 처리하려 한다. 근접 추정 담당에게 확인한다 (#26).
- 실행 직전 재검사는 판정이 제공하고 상태 관리가 호출한다. 재검사 실패의 미전송 취소도 상태 관리가 시작한다.
- 미결: 명시 지정 임계값의 수치. 근접 추정 담당이 검증셋으로 실험한 뒤 정한다. 설정 이름은 `threshold_explicit_locker`, `threshold_explicit_light`다. 기본값 `None`이면 명시 지정 실행을 허가하지 않는다.
- 미결: 오류로 거부한 요청을 같은 `request_id`로 재전송할 때 다시 판정할지. #25는 이 부분을 정하지 않았다.
- 미결: `input_digest`의 직렬화와 해시 방식. #24는 필드와 계산 주체만 정했다. 구현할 때 담당자가 정해서 6절에 쓴다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-18 | `test_FR18_*` | 단계를 ⓪부터 ⑥까지 순서대로 검사한다 |
| FR-18 | `test_FR18_*` | 첫 실패 단계에서 멈추고 뒤 단계를 실행하지 않는다 |
| FR-18 | `test_FR18_*` | 예외, 누락, 알 수 없는 상태는 `OK`가 되지 않는다 |

- 다른 계층의 구현 없이 fake만으로 확인한다.
