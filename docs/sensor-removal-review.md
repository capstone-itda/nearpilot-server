# 센서 검증 제외의 문서 영향 점검

## 점검 범위

2026-10-26에 기존 Markdown 문서 77개의 본문과 참조를 검색하고 관련 절을 대조했다.
대상은 `docs/`, `specs/`, README, 실측·문제 기록, 루트 지침과 `.github/` 양식이다.
이 점검표 자체는 77개에 포함하지 않는다. 점검 당시 대상 main은 `e079881`이다.
작업 브랜치와 main 사이의 앱 spec 변경도 확인했다. BLE 송출과 안내는 유지 범위다.

검색은 센서·물리 관측·검사뿐 아니라 성공·불명·반납·복구·제어 상태와 연결 계약을 포함한다.
키워드가 없는 인증, 근접, 정책과 할당 문서도 목록에 포함했다.

## 반영 원칙

- 새 요구는 `product.md`, `requirements.md`, `architecture.md`, `glossary.md`에 반영한다.
- 실행법은 현재 코드를 설명한다. README에는 현재 동작과 센서 제외 목표의 차이를 적는다.
- `AGENTS.md`는 머지된 spec의 동작을 fix PR에서 코드와 함께 바꾸도록 정한다.
- 이 decision PR은 기존 spec의 동작과 테스트 결과를 새 구현처럼 고쳐 쓰지 않는다.
- 이전 계획, 초안과 실측은 기록으로 보존한다. 이전 검사를 새 구현의 요구로 적용하지 않는다.
- 후속 확인은 변경하거나 검증할 위치다. 표에 있는 모든 파일의 코드 변경이 필수라는 뜻은 아니다.

## 문서별 결과

| 문서 | 처리 | 확인한 내용 |
|---|---|---|
| [.github/ISSUE_TEMPLATE/decision.md](../.github/ISSUE_TEMPLATE/decision.md) | 계약 유지 | 팀 결정 이슈 양식이다. 센서 기능을 요구하지 않는다. |
| [.github/PULL_REQUEST_TEMPLATE/decision.md](../.github/PULL_REQUEST_TEMPLATE/decision.md) | 계약 유지 | decision PR 양식을 유지한다. |
| [.github/PULL_REQUEST_TEMPLATE/docs.md](../.github/PULL_REQUEST_TEMPLATE/docs.md) | 계약 유지 | 문서 PR 양식을 유지한다. |
| [.github/pull_request_template.md](../.github/pull_request_template.md) | 계약 유지 | 일반 PR 양식을 유지한다. |
| [AGENTS.md](../AGENTS.md) | 계약 유지 | 판정 책임, 의존 규칙, 구현과 spec의 동시 변경 규칙을 유지한다. 기존 Gotchas는 기록이다. |
| [CLAUDE.md](../CLAUDE.md) | 계약 유지 | AGENTS.md를 불러오는 지침이다. |
| [dashboard/README.md](../dashboard/README.md) | 범위 안내 | 화면·목 장면·서버 어댑터의 출력 결과 표시와 실제 물리 상태를 구분한다. |
| [docs/architecture.md](../docs/architecture.md) | 이번 반영 | 공동 계약, MQTT, 저장, 복원·복구, 연결 시험과 표시 책임을 맞춘다. |
| [docs/glossary.md](../docs/glossary.md) | 이번 반영 | 성공·반납의 뜻을 바꾸고 이전 검사 용어를 구분한다. |
| [docs/product.md](../docs/product.md) | 이번 반영 | 센서 검증을 제외하고 출력 완료와 논리적 점유 해제로 보장 범위를 정한다. |
| [docs/requirements.md](../docs/requirements.md) | 이번 반영 | 실행·반납·복구·장비·설정·평가와 대시보드 표시 기준을 맞춘다. |
| [firmware/README.md](../firmware/README.md) | 범위 안내 | 현재 센서·검사 모듈 설명과 센서 제외 목표를 구분한다. |
| [firmware/node_lab/README.md](../firmware/node_lab/README.md) | 범위 안내 | 시험 펌웨어의 현재 unknown 보고와 새 출력 완료 목표를 구분한다. |
| [server/config/README.md](../server/config/README.md) | 범위 안내 | 시드의 검사 기준, 검사 ACL, 시간 설정과 시뮬레이터 예시의 후속 변경을 명시한다. |
| [server/tools/node_lab/HARDWARE_VALIDATION.md](../server/tools/node_lab/HARDWARE_VALIDATION.md) | 범위 안내 | 과거 실측과 로그를 보존한다. 물리 완료 관측을 새 범위의 남은 작업에서 제외한다. |
| [server/tools/node_lab/KNOWN_ISSUES.md](../server/tools/node_lab/KNOWN_ISSUES.md) | 기록 보존 | 기기 추가·reserved_at·연결 시험의 기존 관찰이다. 센서 제외로 해결했다고 표시하지 않는다. |
| [server/tools/node_lab/README.md](../server/tools/node_lab/README.md) | 범위 안내 | 시험 설정 생성, 물리 상태 표시와 성공 기준의 후속 변경을 명시한다. |
| [server/tools/node_sim/README.md](../server/tools/node_sim/README.md) | 범위 안내 | 관측 부재·센서 고장·검사 시나리오가 이전 계약임을 명시한다. |
| [specs/_template/spec.md](../specs/_template/spec.md) | 계약 유지 | spec의 공통 양식을 유지한다. |
| [specs/api-auth/spec.md](../specs/api-auth/spec.md) | 계약 유지 | 토큰 계정 식별과 본인 요청의 인증을 유지한다. |
| [specs/api-dashboard/spec.md](../specs/api-dashboard/spec.md) | 후속 확인 | 조회·스트림에서 새 표시 의미를 보존하는지 확인한다. 인증과 보기 전용 범위는 유지한다. |
| [specs/api-mcp-access/spec.md](../specs/api-mcp-access/spec.md) | 계약 유지 | 접근 승인 요청과 응답 필터를 유지한다. |
| [specs/api-mcp-devices/spec.md](../specs/api-mcp-devices/spec.md) | 계약 유지 | 기기 목록과 서버의 논리 상태 조회를 유지한다. |
| [specs/api-mcp-release/spec.md](../specs/api-mcp-release/spec.md) | 후속 확인 | 반납 message와 상태 표시가 논리적 종료를 뜻하는지 확인한다. 응답 필터는 유지한다. |
| [specs/api-mcp/spec.md](../specs/api-mcp/spec.md) | 계약 유지 | 인증, 도구 등록과 응답 허용 목록을 유지한다. 새 판정이나 물리 확인을 추가하지 않는다. |
| [specs/app-beacon-broadcast/spec.md](../specs/app-beacon-broadcast/spec.md) | 계약 유지 | BLE 송출과 앱의 송출 상태 확인은 유지한다. 최신 main의 안내 변경도 같은 범위다. |
| [specs/core-access-request/spec.md](../specs/core-access-request/spec.md) | 계약 유지 | 소유자의 접근 승인 요청을 유지한다. 복구 승인과 구분한다. |
| [specs/core-allocation-window/spec.md](../specs/core-allocation-window/spec.md) | 계약 유지 | 할당 대기와 결과 전달을 유지한다. |
| [specs/core-allocation/spec.md](../specs/core-allocation/spec.md) | 계약 유지 | 전역 할당 알고리즘과 동점 처리를 유지한다. |
| [specs/core-dashboard-queries/spec.md](../specs/core-dashboard-queries/spec.md) | 후속 확인 | 열림·반납 확인 중 문구와 센서 고장 원인, 이력·종료 표시를 새 의미로 맞춘다. |
| [specs/core-decision-apply/spec.md](../specs/core-decision-apply/spec.md) | 계약 유지 | 판정 입력과 단계 실행을 유지한다. |
| [specs/core-decision-pipeline/spec.md](../specs/core-decision-pipeline/spec.md) | 계약 유지 | 계정, 근접, 정책, 점유의 판정 순서를 유지한다. |
| [specs/core-device-query/spec.md](../specs/core-device-query/spec.md) | 계약 유지 | available·occupied·fault는 서버의 논리 상태다. 센서 관측을 반환하지 않는다. |
| [specs/core-integration-any-locker/spec.md](../specs/core-integration-any-locker/spec.md) | 계약 유지 | 할당과 실제 DB의 점유 충돌 검증을 유지한다. |
| [specs/core-integration-real-db/spec.md](../specs/core-integration-real-db/spec.md) | 계약 유지 | 판정·정책·DB의 예약과 승인 연결 검증을 유지한다. |
| [specs/core-internal-error-retry/spec.md](../specs/core-internal-error-retry/spec.md) | 계약 유지 | 내부 오류의 재판정과 중복 예약 방지를 유지한다. |
| [specs/core-node-lab/spec.md](../specs/core-node-lab/spec.md) | 후속 확인 | 시험 펌웨어, 설정 생성, 시리얼·GPIO 결과와 상태 출력을 새 계약으로 맞춘다. |
| [specs/core-ops-cli/spec.md](../specs/core-ops-cli/spec.md) | 후속 확인 | 수집 전용 모드의 반납 검사 재개 설명과 시드 종료 정책을 맞춘다. |
| [specs/core-policy-check/spec.md](../specs/core-policy-check/spec.md) | 계약 유지 | 접근 정책과 승인 검사를 유지한다. |
| [specs/core-policy-reasons/spec.md](../specs/core-policy-reasons/spec.md) | 계약 유지 | 승인 필요 사유와 정책 분기를 유지한다. |
| [specs/core-proximity-data-collection/spec.md](../specs/core-proximity-data-collection/spec.md) | 계약 유지 | BLE RSSI와 수집 라벨의 수집 계약을 유지한다. |
| [specs/core-proximity-estimator/spec.md](../specs/core-proximity-estimator/spec.md) | 계약 유지 | RSSI 기반 근접 추정과 현재 기기 후보의 계약을 유지한다. |
| [specs/core-proximity-model-fit/spec.md](../specs/core-proximity-model-fit/spec.md) | 계약 유지 | 근접 모델 적합과 평가를 유지한다. 물리 완료 검증과 별개다. |
| [specs/core-recheck/spec.md](../specs/core-recheck/spec.md) | 계약 유지 | 실행 직전 계정·근접·정책과 자기 예약 검사를 유지한다. |
| [specs/core-request-use/spec.md](../specs/core-request-use/spec.md) | 후속 확인 | 출력 완료의 성공을 호스트 응답으로 연결한다. 결과별 판정과 정보 최소화는 유지한다. |
| [specs/core-server-assembly/spec.md](../specs/core-server-assembly/spec.md) | 후속 확인 | 검사 게이트웨이·타이머·설정을 제거하고 복원·복구와 새 상태를 연결한다. |
| [specs/core-service/spec.md](../specs/core-service/spec.md) | 후속 확인 | 출력 완료·논리적 반납의 위임과 응답 의미를 확인한다. 소유권 검사는 유지한다. |
| [specs/dashboard-monitor/spec.md](../specs/dashboard-monitor/spec.md) | 후속 확인 | 목 데이터·레일·무대·상세와 서버 어댑터의 표시를 출력 결과와 논리적 점유로 맞춘다. |
| [specs/db-admin-repository/spec.md](../specs/db-admin-repository/spec.md) | 계약 유지 | 계정·비콘·장소·정책의 쓰기 접점을 유지한다. 종료 정책 값은 전환 때 확인한다. |
| [specs/db-audit-jsonl/spec.md](../specs/db-audit-jsonl/spec.md) | 계약 유지 | 감사 원본과 JSONL 사본의 저장·내보내기를 유지한다. |
| [specs/db-collection-active-beacon/spec.md](../specs/db-collection-active-beacon/spec.md) | 계약 유지 | 활성 비콘과 수집 회차의 제약을 유지한다. |
| [specs/db-contract-followups/spec.md](../specs/db-contract-followups/spec.md) | 후속 확인 | 복원 문맥·설정 스냅샷의 이전을 확인한다. 내부 요청과 소유권 접점은 유지한다. |
| [specs/db-evaluation-foundation/spec.md](../specs/db-evaluation-foundation/spec.md) | 후속 확인 | 지표 4·6의 새 성공·종료 기준과 후속 평가 범위를 맞춘다. 읽기 전용 연결은 유지한다. |
| [specs/db-execution-store/spec.md](../specs/db-execution-store/spec.md) | 후속 확인 | check_id·check_evidence 없는 종료, CloseResult와 복구 근거 저장을 맞춘다. |
| [specs/db-internal-error-retry/spec.md](../specs/db-internal-error-retry/spec.md) | 계약 유지 | 오류 요청 재접수와 기존 기록 보호를 유지한다. |
| [specs/db-repository/spec.md](../specs/db-repository/spec.md) | 후속 확인 | 기존 종료 정책과 복구 저장 데이터의 이전을 확인한다. 예약과 점유 검사 규칙은 유지한다. |
| [specs/db-retention-capacity/spec.md](../specs/db-retention-capacity/spec.md) | 후속 확인 | 새 종료·복구 완료 기록의 정리 조건을 확인한다. 미해결 기록 보존은 유지한다. |
| [specs/firmware-contracts/spec.md](../specs/firmware-contracts/spec.md) | 후속 확인 | 센서·완료 관측·검사와 검사 기록을 제거하고 출력 완료·상태·버전을 맞춘다. |
| [specs/iot-command-gateway/spec.md](../specs/iot-command-gateway/spec.md) | 후속 확인 | 검사 요청을 제거한다. 출력 결과 대기, 미전송·불명 구분과 늦은 결과는 유지한다. |
| [specs/iot-event-delivery/spec.md](../specs/iot-event-delivery/spec.md) | 후속 확인 | 검사 검증·인계를 제거한다. 상태·결과의 버전, 최신성과 재인계는 유지한다. |
| [specs/iot-messages/spec.md](../specs/iot-messages/spec.md) | 후속 확인 | 물리 상태·검사 형식을 제거하고 새 상태·결과 버전과 필드를 맞춘다. |
| [specs/iot-mqtt-gateway/contract-review.md](../specs/iot-mqtt-gateway/contract-review.md) | 기록 보존 | 이전 계약 검토와 질문 기록이다. 검사 관련 검토를 새 계약의 근거로 쓰지 않는다. |
| [specs/iot-mqtt-gateway/plan.md](../specs/iot-mqtt-gateway/plan.md) | 기록 보존 | 이전 단계 계획이다. 물리 완료·반납 검사 항목은 새 구현 목표로 쓰지 않는다. |
| [specs/iot-mqtt-gateway/spec.md](../specs/iot-mqtt-gateway/spec.md) | 후속 확인 | 검사 구독·발행·접점을 제거한다. 자격정보와 허가한 명령 전송은 유지한다. |
| [specs/iot-node-simulator/spec.md](../specs/iot-node-simulator/spec.md) | 후속 확인 | 가상 물리 검사와 관측 부재의 unknown 시나리오를 새 출력 처리 계약으로 맞춘다. |
| [specs/state-management/core-execution.md](../specs/state-management/core-execution.md) | 후속 확인 | 새 성공 의미를 연동한다. 실패·불명 격리, 결과 저장과 멱등성은 유지한다. |
| [specs/state-management/core-fault-recovery.md](../specs/state-management/core-fault-recovery.md) | 후속 확인 | 안전·물리 동기화 조건을 제외하고 현재 연결, 명령 상태와 소유자 승인을 적용한다. |
| [specs/state-management/core-node-events.md](../specs/state-management/core-node-events.md) | 후속 확인 | 검사 결과 전달과 센서 이상 격리를 제거하고 새 상태 보고를 연동한다. |
| [specs/state-management/core-release.md](../specs/state-management/core-release.md) | 후속 확인 | 검사·재검사를 제외하고 논리적 종료와 완료 기록 조회를 맞춘다. |
| [specs/state-management/core-restore.md](../specs/state-management/core-restore.md) | 후속 확인 | 센서 정상 확인과 반납 검사 재개를 현재 연결 확인과 논리적 종료 재개로 맞춘다. |
| [specs/state-management/core-shared-light.md](../specs/state-management/core-shared-light.md) | 후속 확인 | 현재 light_on 관측 대신 저장한 마지막 제어 상태를 쓴다. 이탈·유예는 유지한다. |
| [specs/state-management/execution-decisions.md](../specs/state-management/execution-decisions.md) | 기록 보존 | 실행 인계·미전송·중단 사유의 이전 논의를 보존한다. 검사 제거와 무관한 계약은 유지한다. |
| [specs/state-management/fault-recovery-decisions.md](../specs/state-management/fault-recovery-decisions.md) | 기록 보존 | 초안 2의 물리 안전·동기화 조건은 새 목표에서 제외한다. 소유권 조회는 유지한다. |
| [specs/state-management/lifecycle-review.md](../specs/state-management/lifecycle-review.md) | 기록 보존 | 이전 구현 순서와 검사 선행 조건을 보존한다. 새 범위는 현재 docs를 따른다. |
| [specs/state-management/node-events-decisions.md](../specs/state-management/node-events-decisions.md) | 기록 보존 | 원래 실행 문맥 조회의 초안이다. 조회와 중복 실행 방지의 약속은 유지한다. |
| [specs/state-management/restore-decisions.md](../specs/state-management/restore-decisions.md) | 기록 보존 | 원래 예약 시각과 복원 조회의 초안이다. 기록 보존과 현재 연결 확인은 유지한다. |
| [specs/state-management/shared-light-decisions.md](../specs/state-management/shared-light-decisions.md) | 기록 보존 | 내부 요청·실행 연결·유지 권한의 초안이다. 센서 관측을 새 종료 근거로 추가하지 않는다. |

## 함께 확인한 실행 예시

아래 예시는 현재 구현이 읽는 값이다. 후속 구현과 전환 절차가 정해질 때 함께 맞춘다.

| 경로 | 확인한 의존 |
|---|---|
| `server/config/seed/example.toml` | `release_policy.check_criteria`를 기기의 검사 기준으로 넣는다. |
| `server/config/mosquitto.acl.example` | 검사 요청·결과 토픽의 발행·구독 권한을 둔다. |
| `server/tools/node_sim/fleet.example.json` | 무드등의 `light_observable=false`를 둔다. |
| `firmware/include/config.example.h` | 기능별 물리 완료 관측 대기 설정을 둔다. |

문서 수정만으로 예시의 키를 지우지 않는다. 현재 설정 파서와 실행 코드의 계약을 함께 바꾼다.

## 남은 결정

- 메시지별 새 버전과 상태 필드 형식, 구버전 전환 절차는 미결이다.
- 기존 점유·미완료 반납·복구 건과 펌웨어 기록의 이전 절차는 미결이다.
- 결과 불명 명령의 소유자 복구 시 승인 사용분 정산 기준은 미결이다.
- 위 미결은 `requirements.md`와 `architecture.md`에 기록한 범위를 따른다.
