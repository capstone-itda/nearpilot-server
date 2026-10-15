## 질문

서로 다른 반납 검사 조건을 어떤 v1 기준으로 통일하는가?

## 맥락

- 관련 영역: 펌웨어, IOT, 상태 관리, 기기 시뮬레이터다.
- 관련 요구사항: FR-07, FR-20, NFR-04다.
- 입력: 검사 기준 ID와 버전을 담은 safety_check다.
- 출력: 검사 결과와 항목별 evidence다.
- 바뀌는 상태: CORE가 근거로 채택하는 반납 검사 결과다. 점유 종료는 CORE가 결정한다.
- 약속: architecture.md §4 "반납 검사와 종료"는 locker-return에 door_closed·locked, light-return과 speaker-return에 actuator_idle을 요구한다.
- 약속: 같은 문서 §5 MQTT 7절은 사물함에 door_closed·actuator_state·sensor_fault 조건을 쓴다.
- 약속: 같은 절은 꺼짐을 요구하는 조명 정책에 light_on=false도 요구한다.
- 약속: §5 MQTT 3절의 evidence 설명은 필수 항목을 미결로 가리킨다. §4에는 이미 개발 기준 표가 있다.
- 약속: specs/state-management/core-release.md는 §4의 개발 기준을 따른다.
- 지금 정해야 하는 이유: 같은 criteria_id와 version에 서로 다른 passed 조건을 구현하면 서버와 펌웨어가 같은 결과를 다르게 해석한다.
- 근거: docs/requirements.md FR-07·FR-20, docs/architecture.md §4와 §5, specs/state-management/core-release.md다.

## 선택지

### A. 기존 v1은 §4 표로 고정하고 추가 조건은 새 버전으로 구분한다
- 결과: 현재 서버와 시뮬레이터는 §4의 항목 이름과 bool 근거를 같은 뜻으로 쓴다.
- 결과: §5의 다른 표와 미결 표기를 decision PR에서 정리한다.
- 결과: 구동부 정지, 센서 정상, 정책상 꺼짐을 새 필수 조건으로 채택하면 새 검사 기준 버전을 만든다.
- 결과: 기존 점유 세션은 생성 당시 검사 기준을 유지한다. 새 버전을 과거 v1에 소급하지 않는다.

### B. 추가 조건을 합친 새 버전을 먼저 정의한다
- 결과: 새 항목과 정책별 조건을 합의한 뒤 CORE·시뮬레이터·펌웨어가 함께 새 버전을 구현한다.
- 결과: 새 버전이 필요한 실제 기기의 검증은 계약 반영까지 대기한다.

## 제안

A를 서버·시뮬레이터의 개발 기준으로 제안한다.
현재 상태 관리 spec이 가리키는 v1을 유지할 수 있다.
문서의 차이는 조용히 덮어쓰지 않고 별도 decision PR로 정리한다.
실제 기기가 locked나 actuator_idle을 증명하지 못하면 passed로 추정하지 않는다.
실제 센서 조합과 추가 조건은 검증한 뒤 새 버전으로 합의한다.

## 결정

2026-10-15 담당자가 대화에서 A안의 합의 완료와 문서 반영을 지시했다.
A안을 채택한다. 반납 검사 v1은 architecture.md §4의 기준을 쓴다. 사물함은 `door_closed`·`locked`, 조명·스피커는 `actuator_idle`을 확인한다.
문서 반영: [PR #147](https://github.com/capstone-itda/nearpilot-server/pull/147)다.
실제 기기의 미측정 수치와 근거, 후속 보고 형식은 PR의 문서에 남긴 후속 검증 범위를 따른다.
