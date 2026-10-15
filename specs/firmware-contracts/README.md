# 펌웨어 계약 검수와 후속 이슈

이 폴더는 실제 기기의 계약 검수와 후속 이슈 본문을 담는다.
MQTT 연결 구현은 [게이트웨이 spec](../iot-mqtt-gateway/spec.md)이 맡는다.
IOT의 전체 분할은 [구현 계획](../iot-mqtt-gateway/plan.md)에 있다.

## 검수 기준

2026-10-16에 `docs/product.md`, `docs/requirements.md`와 `docs/architecture.md`를 대조했다.
계약의 근거는 이 문서들이다. 닫힌 이슈와 PR 토론을 계약의 근거로 쓰지 않는다.
이번 검수는 문서 검수다. 실제 기기의 시각 오차와 센서 능력은 측정하지 않았다.
펌웨어 담당 배정은 architecture.md §2에서 미결이다.

## 검수 결과

기존 #140~#143은 이미 발행하고 종결한 이슈다. 같은 질문을 다시 발행하지 않는다.
네 결정은 현재 기준 문서에 반영되어 있다. 기존 이슈 번호는 추적에만 쓴다.

| 사안 | 문서에서 확인한 현재 계약 | 남은 일 |
|---|---|---|
| 시각 신뢰 | 폐쇄망 시각원과 기기별 오차 실측을 쓴다. 검증 전 실행과 안전 확인을 차단한다 | 실제 동기화 방식, 재동기화 간격, 오차 실측과 설정 계약이다 |
| 반납 검사 | 사물함 v1은 `door_closed`·`locked`, 조명·스피커 v1은 `actuator_idle`을 요구한다 | 실제 센서의 항목별 증명 능력과 이상 검출 범위다 |
| 실행 완료 | 물리 완료를 증명한 기능만 `succeeded`다. 관측 없는 IR 무드등은 `light_on=null`이다 | 동작별 완료 관측, 결과 보존과 스피커 보고 필드다 |
| 검사 오류 | 첫 구현은 확인 불능 경로를 유지한다. 측정하지 못한 bool을 만들거나 기존 결과를 덮어쓰지 않는다 | 오류 종류, 근거 예외와 사건 ID를 담을 후속 보고 형식이다 |

기존 반납 검사 이슈 사본은 이미 해결한 §4·§5의 충돌을 현재 질문처럼 담고 있었다.
이번 본문은 해결한 충돌을 제거하고 실제 기기의 근거 제공 능력을 질문한다.
검사 오류 보고 확장은 첫 구현의 선행 조건이 아니다. 확정 전에는 기존 v1을 따른다.
나머지 실제 기기 검증이 끝나기 전에는 시뮬레이터 통과를 실제 기기의 검증 완료로 표시하지 않는다.

## 후속 이슈 본문

| 사안 | 검수한 본문 | 발행한 후속 이슈 | 기존 원칙을 추적하는 이슈 |
|---|---|---|---|
| 시각 동기화와 설정 | [clock.md](clock.md) | [#155](https://github.com/capstone-itda/nearpilot-server/issues/155) | [#140](https://github.com/capstone-itda/nearpilot-server/issues/140) |
| 반납 근거의 실제 센서 검증 | [criteria.md](criteria.md) | [#156](https://github.com/capstone-itda/nearpilot-server/issues/156) | [#141](https://github.com/capstone-itda/nearpilot-server/issues/141) |
| 동작별 완료 관측과 보고 필드 | [completion.md](completion.md) | [#157](https://github.com/capstone-itda/nearpilot-server/issues/157) | [#142](https://github.com/capstone-itda/nearpilot-server/issues/142) |
| 검사 오류의 후속 보고 형식 | [check_errors.md](check_errors.md) | [#158](https://github.com/capstone-itda/nearpilot-server/issues/158) | [#143](https://github.com/capstone-itda/nearpilot-server/issues/143) |

각 본문의 선택지는 미결이다. 이번 검수는 실제 센서, 새 보고 필드나 공용 설정을 확정하지 않는다.
팀이 계약을 결정하면 별도 decision PR로 docs와 공용 계약에 반영한다.

## 분리 범위

기존 `specs/iot-mqtt-gateway/firmware-issues/`의 네 사본을 이 폴더의 후속 본문으로 정리했다.
원래 발행 본문과 결정 기록은 기존 GitHub 이슈에 남아 있다.
게이트웨이의 `contract-review.md`에는 서버 계약의 선택지를 두고 펌웨어 검수는 이 문서를 참조한다.
이번 변경은 펌웨어 코드와 서버의 판정·전송·종료 동작을 바꾸지 않는다.
