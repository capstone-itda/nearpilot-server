<!--
intent 문서 규칙  ·  이 주석이 규칙 원문이다 (다른 문서는 요약·포인터일 뿐)

무엇인가
- docs/intent/ 아래 문서는 4인 공유 문서다. 역할별 담당 범위의 spec 문서는 각자 관리하지만 이 문서들은 아니다.
- 이 파일은 템플릿이다. intent 하나당 복사해서 docs/intent/{계층}-{요약}.md 로 쓴다.
  예) docs/intent/shared-approval-flow.md  (계층: api core db iot shared app dashboard firmware, 요약: 영문 소문자·숫자·하이픈)
- 브랜치는 intent/{계층}-{요약} 으로, 파일 이름과 같게 딴다. intent PR 에는 그 파일 하나만 담는다.

수정 절차
- 수정은 반드시 PR 로 올린다. main 에 직접 push 하지 않는다.
- PR 은 작성자를 포함해 3인이 확인해야 머지한다. 즉 작성자 외 2인의 리뷰 승인이 필요하다.
- Reviewers 에 작성자 외 2인 이상을 직접 지정한다.
- docs/prd.md, docs/architecture.md 수정도 같은 규칙을 따른다.

강제되지 않는다는 점
- 이 레포는 무료 플랜의 비공개 레포라 브랜치 룰셋·브랜치 보호·CODEOWNERS 가 모두 잠겨 있다.
- 즉 GitHub 는 승인 없는 머지를 막아주지 않는다. 협업자 4인은 전원 admin 이다.
- CI "PR 규칙" 이 브랜치·파일 형식과 승인 수를 검사하지만, 실패해도 머지 버튼은 막히지 않는다.
- 이 규칙은 장치가 아니라 4인의 합의로 지킨다. 머지 전에 작성자 외 승인 2개와 CI 초록불을 눈으로 확인한다.

형식
- 아래 5개 절은 고정이다. 순서를 바꾸거나 절을 추가·삭제하지 않는다.
- 작성 후 이 주석 블록은 지우지 않는다.
-->

# Intent: FR-18 결정론적 판정 파이프라인 (⓪~⑥)

Author: eunji719 (판정). Status: draft.

<!-- Status: draft → review → accepted. 대체되면 superseded 로 바꾸고 대체 문서를 링크한다. -->

## Problem

- PRD §3은 `request_use`의 실행 허가를 ⓪중복 → ①비콘 → ②후보 → ③근접 → ④정책 → ⑤장치 점유 → ⑥사용자 점유 순서로 판정하고, 기본값은 거부라고 정한다 (FR-18).
- 현재 저장소에는 이 순서를 실행하는 코드가 없다. `shared/`(PR #8, 머지 전)에 `Repository`·`ProximityEstimator`·`AuditLog` 같은 계약과 `Verdict`·`Step`·`ReasonCode`만 정의되어 있다.
- 각 단계의 구현은 여러 담당에 나뉘어 있다. 근접은 근접 추정(`core/proximity`), 정책·승인은 DB(`core/policy`), ⑤⑥ 원자적 점유는 DB(`db/`), 실행·상태 전이는 상태 관리(`core/lifecycle`)가 맡는다. 단계를 어떤 순서로 부르고 언제 멈추는지를 정하는 곳이 없으면 다음 문제가 생긴다.
  - 정책·점유 때문에 다른 물리 장치가 선택될 수 있다. PRD §3 ②는 대상을 확정한 뒤에만 ④~⑥을 검사하라고 요구한다.
  - 호스트나 발화에 따라 단계가 생략될 수 있다. 공용형의 ⑤⑥ 생략 외에는 금지되어 있다.
  - 같은 요청을 재전송했을 때 판정이 달라지거나 중복 실행될 수 있다.

## Proposed outcome

- `request_use`가 들어오면 core가 항상 같은 순서(⓪→⑥)로 검사한다. 첫 실패 단계의 판정과 사유 코드를 그대로 반환하고 뒤 단계는 실행하지 않는다.
  - ⓪ 같은 `(계정, request_id)`와 같은 정규화 입력이면 기존 판정·진행 상태를 반환한다. 입력이 다르면 `DENY / REQUEST_ID_CONFLICT`. 다른 계정의 결과는 반환하지 않는다.
  - ① 최근 T초 안에 수신된 등록 비콘이 없으면 `DENY / BEACON_NOT_SEEN`.
  - ② 신뢰·온라인·요청 종류가 맞고 FAULT가 아닌 장치가 없으면 `DENY / NO_CANDIDATE`. 명시 지정 대상이 조건 밖이면 `DENY / TARGET_UNAVAILABLE`.
  - ③ 사후확률 최대 장치가 설정 임계값 미만이면 `ASK / LOW_CONFIDENCE`. 명시 지정이면 `ASK / MOVE_CLOSER`. 이 단계에서 대상이 확정된다.
  - ④ 확정된 대상에 대해서만 정책·승인을 검사해 `NEED_APPROVAL`(대기·거부·만료 사유 구분) 또는 `DENY`를 반환한다.
  - ⑤⑥ `Repository.reserve_if_free()` 한 번으로 검사와 `RESERVED` 생성을 함께 처리한다. 실패하면 `BUSY / DEVICE_BUSY` 또는 `BUSY / USER_BUSY`. 공용형(`shared`)은 ⑤⑥과 점유 세션 생성을 건너뛴다.
- 모든 단계를 통과해야만 `OK`가 되고, 그 뒤 실행 직전 재검사(허가·근접·대상)를 거친다. 재검사에서 자기 요청의 예약은 타인 점유로 보지 않는다.
- 단계마다 입력과 결과를 감사 기록에 남긴다. 판정 한 건에는 같은 설정 버전을 적용하고 내부 요청 ID로 재현할 수 있게 한다 (FR-22).
- 다른 계층 구현 없이 fake만으로 단계 순서·조기 종료·기본 거부를 pytest로 확인할 수 있다 (`test_FR18_*`).

이번 intent의 범위 밖(별도 intent로 다룸)
- ①②③ 각 단계 내부 로직의 세부(FR-15·13·16·17), 전역 할당(FR-23, `any:locker`), MCP 도구·토큰 식별(FR-08~11), 실행·명령 전송(FR-06).

## Affected users and systems

- 방문자: AI 호스트로 요청할 때 받는 판정·사유가 이 순서로 결정된다.
- AI 호스트(Claude·ChatGPT·Gemini): 같은 입력에 같은 판정을 받는다 (FR-10, NFR-09).
- core/decision (판정): 새로 구현한다.
- core/proximity (근접 추정): ③에서 `ProximityEstimator.posterior`를 호출받는다.
- core/policy, db (DB): ④에서 정책·승인 조회, ⓪의 `link_request`, ⑤⑥의 `reserve_if_free`, `save_decision`, `AuditLog.append`를 호출받는다.
- core/lifecycle (상태 관리): `OK` 이후 재검사·실행·상태 전이 흐름과 맞닿는다.
- shared: 기존 계약(PR #8)을 그대로 쓴다. 이 intent는 shared 변경을 포함하지 않는다.

## Constraints

- 기본값은 거부다. 예외·누락·알 수 없는 상태는 `OK`가 되지 않는다 (FR-18).
- 공용형의 ⑤⑥ 생략 외에는 호스트·발화로 단계를 생략하지 않는다 (PRD §3).
- 정책·점유 검사는 대상 확정 후에만 수행하고, 그 결과로 다른 물리 대상을 고르지 않는다 (PRD §3 ②).
- ⑤⑥ 검사와 `RESERVED` 생성은 `BEGIN IMMEDIATE` 한 트랜잭션에서 처리한다. decision은 트랜잭션을 직접 열지 않고 `reserve_if_free()`만 호출한다 (FR-19, NFR-03, architecture §3).
- 판정은 core만 한다. api·iot는 결과를 바꾸지 않는다 (NFR-07).
- 다른 계층에는 `shared/interfaces.py`의 Protocol로만 의존한다 (architecture §3).
- 임계값·T 같은 수치는 코드 상수가 아닌 설정으로 받고, 적용한 값과 버전을 감사 기록에 남긴다 (PRD §2 수치 설정, FR-22).
- 응답에는 사용자 ID와 점유 주체를 넣지 않는다 (FR-11, NFR-08). 이 변환은 api 쪽에서 하지만 decision이 만드는 `Decision`에도 넣지 않는다.
- 서버 판정 median ≤ 1초, p95 ≤ 2초 (NFR-01).

## Open questions

1. ① 비콘 최근 수신 T 값과 ② 온라인 TTL 값은 PRD §7에서 미결이다. 누가 언제 정하는가? 정해지기 전에는 설정 항목으로만 두고 값은 비워 둔다.
2. ④ 정책 검사 경계: `core/policy`(DB 담당)가 "정책·승인 검사 함수"를 제공하고 decision은 그 결과만 반영하는가, 아니면 decision이 `repo.current_policy`·`repo.valid_approval`을 직접 조합하는가?
3. 실행 직전 재검사는 decision이 제공하고 lifecycle이 호출하는가, 아니면 lifecycle 안에서 하는가? 재검사 실패 시 예약·승인 사용분 반환은 누가 트리거하는가?
4. 예상치 못한 예외로 기본 거부할 때 쓸 사유 코드가 `ReasonCode`에 없다. 기존 코드를 재사용할지, shared에 새 코드를 추가할지(4인 리뷰) 정해야 한다.
5. ⓪ "정규화 입력"에 포함할 필드(예: `target_spec`, `action`, 원 요청 연결 ID)와 `input_digest` 계산 방식. DB의 `request` 테이블 "정규화 입력 요약" 컬럼과 맞춰야 한다.
6. 공용 조명은 ⑤⑥을 건너뛰지만 승인 사용분은 원자적으로 확보해야 한다(PRD §3). 이 확보를 `reserve_if_free()`로 처리할지, 별도 DB 호출이 필요한지?
7. 이 intent는 PR #8(shared 계약)을 전제로 한다. PR #8이 바뀌면 이 문서의 인터페이스 이름도 함께 고친다.
