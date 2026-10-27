# 문체와 용어

<!--
이 문서는 "글을 어떻게 쓰고, 이 말은 무슨 뜻인가"에 답한다.
docs/, specs/, 이슈 본문, PR 본문은 이 문서를 따른다.
-->

## 1. 문체 규칙 (ASD-STE100 한국어)

| # | 규칙 |
|---|---|
| 1 | 한 문장에는 생각 하나만 쓴다. 절차 문장은 15어절, 서술 문장은 20어절 이하로 쓴다. |
| 2 | 한 문단에는 주제 하나만 쓴다. 문단은 6문장 이하로 쓰고, 핵심을 첫 문장에 쓴다. |
| 3 | 행위자를 주어로 쓴다. `~된다` 대신 `X가 ~한다`로 쓴다. |
| 4 | 이 문서의 용어만 쓴다. 같은 대상을 다른 말로 부르지 않는다. |
| 5 | 명사를 나열하지 않고 동사를 쓴다. `~ 수행`, `~ 처리`, `~ 진행`을 쓰지 않는다. |
| 6 | `해당`, `이것`, `그것`, `상기`를 쓰지 않는다. 그 자리에 명사를 다시 쓴다. |
| 7 | `등`, `적절히`, `필요 시`, `가능하면`, `일부`를 쓰지 않는다. 이중 부정을 쓰지 않는다. |
| 8 | 조건을 문장 앞에 쓴다. 조건이 둘 이상이면 목록으로 쓴다. |
| 9 | 문장은 `-다`로 끝낸다. 절차는 번호 목록으로 쓰고, 한 단계에는 동작 하나만 쓴다. |
| 10 | 가운뎃점은 같은 종류의 낱말을 나열할 때만 쓴다. 가운뎃점, 빗금, 괄호로 문장을 압축하지 않는다. |
| 11 | 코드 식별자는 원문 그대로 백틱으로 감싼다. |

## 2. 용어
<!-- 용어 하나에는 뜻이 하나다. "쓰지 않는 말" 칸의 말은 쓰지 않는다. -->

### 도메인

2026-10-26 결정은 실행 성공과 반납 완료의 뜻을 아래처럼 정한다.
기존 코드와 spec에 남은 물리 검사 용어는 이 문서 끝의 "이전 계약 용어"를 따른다.

| 용어 | 뜻 | 코드 이름 | 쓰지 않는 말 |
|---|---|---|---|
| 기기 | ESP32 보드 하나와 그 보드가 제어하는 사물함, 조명 또는 스피커. 앵커 역할도 한다 | `node`, `NodeInfo` | 장치, 노드 |
| 앵커 | 비콘의 RSSI를 측정해서 보고하는 기기의 역할 | | |
| 비콘 | 방문자 폰이 송출하는 iBeacon 식별자 `(uuid, major, minor)` | `beacon`, `BeaconKey` | |
| 계정 | 토큰으로 식별하는 사용자. 역할은 방문자 또는 소유자다 | `user`, `Account` | |
| 토큰 | 서버가 계정에 발급하는 32바이트 무작위 값. AI 호스트와 앱이 인증에 쓴다 | | |
| 토큰 해시 | 토큰의 SHA-256 해시를 소문자 hex 문자열로 쓴 값. 서버는 토큰 원문 대신 토큰 해시를 저장한다 | `token_hash` | |
| 방문자 | 기기를 사용하는 계정 | `Role.VISITOR` | |
| 소유자 | 장소와 기기를 관리하고 접근을 승인하는 계정 | `Role.OWNER` | |
| 운영자 | 대시보드로 시스템을 지켜보는 사람. 계정 역할이 아니고, 서버에 쓰지 않는다 | | |
| 장소 | 소유자가 가진 공간. 기기를 배치한다 | `place` | |
| AI 호스트 | 사용자의 요청을 MCP로 서버에 전달하는 LLM 앱. Claude, ChatGPT, Gemini | `host_kind` | |
| 판정 | core가 요청에 내리는 결과. `OK`, `BUSY`, `ASK`, `DENY`, `NEED_APPROVAL` | `Verdict` | |
| 사유 코드 | 판정의 이유 | `ReasonCode` | |
| 승인 결정 결과 | 소유자의 접근 승인 결정의 반영 여부, 재호출 성공 여부와 사유 | `ApprovalDecisionResult`, `ApprovalDecisionReason` | |
| 관리 쓰기 결과 | 관리 변경을 반영했는지와 그 사유 | `AdminWriteResult`, `AdminWriteOutcome`, `AdminWriteReason` | |
| 판정 기록 | 판정 한 건에 적용한 버전, 설정, 후보별 사후확률, 관측 구간을 담은 값 객체 | `DecisionRecord` | |
| 허가 | 판정 `OK`로 기기 실행을 허락하는 것 | `Verdict.OK` | 승인 |
| 승인 | 소유자가 제한 기기의 접근 요청을 받아들이는 것 | `approval` | 허가 |
| 대기 승인 | 소유자가 아직 결정하지 않은 승인 요청 | `approval.status`의 `pending` | |
| 승인 이력 | 요청자와 기기 하나에 대한 승인 기록 전체. 정책 검사가 승인 필요 사유를 고르는 데 쓴다 | `Repository.approvals_in_scope`, `ApprovalRecord` | |
| 승인 사용분 | 승인 하나에서 요청 하나가 쓰는 사용 횟수 | `approval_usage` | |
| 복구 승인 | 소유자가 `FAULT` 기기의 복구 건 하나의 복구를 허용하는 것. 승인과 별개다. 승인의 유효시간을 적용하지 않는다 | | |
| 정책 검사 | ④에서 접근 정책과 승인을 검사하는 것. 결과는 허용, 승인 필요, 거부다 | `PolicyChecker.check`, `PolicyCheckResult` | |
| 접근 정책 | 기기의 승인 필요 여부. `open` 또는 `restricted` | `PolicyRevision.mode` | |
| 공개 범위 | 기기를 주변 목록에 보일지 여부. `public` 또는 `private` | `Visibility` | |
| 점유 방식 | 기기를 한 계정이 점유하는지 여부. 대여형 `rental` 또는 공용형 `shared` | `Occupancy` | |
| 대여시간 기준 시각 | 대여시간 초과를 계산하는 기준이다. 접근 승인 만료와 구분한다 | `rental_due_at` | 대여 권한 만료 시각 |
| 초과시간 | 대여시간 기준 시각을 넘긴 시간이다. 종료 전에는 현재 시각, 종료 뒤에는 종료 시각으로 계산한다 | | |
| 대여 기본 시간 | 대여시간 기준 시각을 정하는 시간이다. 자동 반납의 기준이 아니다 | `rental_duration_sec` | |
| 유지 대상자 | 공용형 기기의 켜기 제어를 유지하는 근거가 되는 계정. 켜기 신호 출력에 성공했고 사용 권한이 유효한 계정이다. 서버는 유지 대상자를 저장하지 않는다 | | 유지 참여자 |
| 초기 참여 대기 | 서버가 재시작 뒤 관측을 재개한 시점부터 새 유지 대상자를 기다리는 시간. 이탈 판단의 미수신 시간과 유예 시간의 합이다 | `leave_absent_sec`, `leave_grace_sec` | |
| 기기 연결 ID | 기기가 MQTT에 접속할 때 새로 만드는 UUID다. 재접속과 재시작마다 바뀐다. 명령의 대상으로 쓸 때는 `target_session_id`다 | `session_id` | 점유 세션 ID, 노드 세션 ID, `node_session_id` |
| 결과 사건 ID | 기기가 결과 보고 하나에 붙이는 UUID다. 같은 보고를 다시 보내도 유지한다. 서버가 만든 결과에는 없다 | `NodeResult.event_id` | |
| 제어 신호 출력 | 펌웨어가 서보, 조명이나 스피커에 명령의 제어 신호를 내보내는 것. 실제 상태 확인을 포함하지 않는다 | | |
| 실행 성공 | 펌웨어가 명령의 제어 신호 출력을 마치고 결과를 저장한 것. 실제 켜짐, 열림과 재생의 증명이 아니다 | `succeeded` | |
| 명령 처리 상태 | 펌웨어가 명령 처리를 기다리는지, 처리 중인지, 확인할 수 없는지 나타내는 상태 | 새 필드 형식은 미결이다 | 물리 상태 |
| 마지막 제어 상태 | 저장한 성공 명령의 `on` 또는 `off`다. 실제 켜짐과 꺼짐의 관측이 아니다 | | |
| 반납 완료 | 본인 반납의 논리적 점유 해제를 서버가 저장한 것. 문 닫힘과 잠금, 물리 안전의 증명이 아니다 | | |
| 점유 세션 | 대여형 기기를 한 계정이 점유하는 기간의 기록 | `use_session`, `use_id` | 사용 세션 |
| 신뢰 상태 | 소유자가 정한 기기의 신뢰 여부. `discovered`, `trusted`, `revoked` | `TrustState` | |
| 온라인 | 기기의 마지막 수신 시각이 TTL 안에 있는 상태 | `last_seen_at` | |
| 온라인 유지 시간 | 유효한 기기 메시지의 마지막 수신부터 온라인으로 보는 시간. 온라인의 TTL이다 | `online_ttl_sec` | |
| 기기 상태 보고 주기 | 기기가 상태를 보고하는 간격 | `status_report_sec` | |
| 최근 비콘 기준 | ①에서 등록 비콘의 유효 관측을 인정하는 최근 구간 | `beacon_recent_sec` | |
| 예약 대기 제한 | 예약부터 전송 시작까지 기다리는 한도. 미전송이 확실할 때만 예약을 만료 취소한다 | `reservation_wait_sec` | |
| 실행 응답 제한 | 전송 시도 뒤 실행 결과를 기다리는 한도 | `exec_timeout_sec` | |
| 승인 기본 유효시간 | 승인 시점부터 승인이 유효한 시간. 접근 승인에만 적용한다 | `approval_ttl_sec` | |
| 승인 대기 만료 | 대기 신청이 신청 시점부터 정해진 시간을 넘겨 끝나는 것. 소유자의 거부와 구분한다 | `approval_pending_ttl_sec`, `ReasonCode.APPROVAL_REQUEST_EXPIRED` | |
| 외부 요청 ID | 호스트가 보내는 계정 범위의 멱등 키. 중복 방지 보장기간이 지나면 서버가 비운다 | `request_id` | |
| 중복 방지 보장기간 | 요청의 최종 처리 완료 뒤 서버가 외부 요청 ID로 중복을 검사하는 기간 | `request_dedup_ttl_sec` | |
| 신규 조작 접수 제한 | 서버 전체의 중복 방지 항목 수가 한도 이상일 때 새 물리 조작 요청을 거부하는 기준 | `dedup_entry_limit`, `ReasonCode.CAPACITY_EXCEEDED` | |
| 사후확률 | 근접 추정이 장소의 전체 상태로 계산한, 방문자가 한 기기 앞에 있을 확률. 후보끼리 다시 정규화하지 않는다 | `ProximityResult.posteriors` | |
| 전체 상태 | 근접 추정이 사후확률을 계산하는 상태의 집합. 장소의 `trusted` 기기 앞 상태들과 "어느 기기 앞도 아님" 상태다 | | |
| 모델 파일 | 장소별 근접 파라미터, 기기 배치, HMM 전이와 보정 값을 버전으로 묶은 파일 | `model_version`, `calib_version` | |
| 수집 회차 | 같은 비콘의 관측을 한 라벨과 수집 구간으로 묶은 기록. 학습·검증·시험 분할의 단위다 | `collection_run`, `collection_id` | |
| 수집 라벨 | 수집 회차에서 관측한 위치. 기기 1~7 앞 또는 `none`이다 | `collection_run.label` | |
| 후보 밖 상태 | 전체 상태 중 ②의 후보 기기 앞이 아닌 상태 | | |
| 근접 임계값 | 판정이 근접 지정과 허용 후보 중 배정의 ③에서 사후확률과 비교하는 설정 값. 명시적 지정 요청에는 쓰지 않는다 | `threshold_locker`, `threshold_light`, `threshold_speaker` | |
| 내부 요청 ID | 서버가 외부 요청 ID에 연결하는 전역 고유 ID | `internal_request_id` | |
| 명령 ID | 서버가 물리 명령마다 발급하는 고유 ID | `command_id` | |
| 정규화 입력 | ⓪이 같은 요청인지 대조하는 입력. 대상 지정, 동작, 원 요청 연결 ID로 구성한다 | `input_digest` | |
| 실행 직전 재검사 | 기기에 명령을 보내기 직전에 허가, 근접, 대상을 다시 검사하는 것 | `DecisionRechecker.recheck` | |
| 실행 인계 | 판정이 허가와 예약을 마친 요청의 실행을 상태 관리에 넘기는 것 | `ExecutionContext`, `ExecutionService.execute` | |
| 사건별 저장 | 예약 이후의 사건 하나에 딸린 기록을 한 트랜잭션으로 반영하는 것 | `ExecutionStore` | |
| 근거 | 사건별 저장과 복구 건에 담는, 사건을 증명하는 값. JSON 기본 타입(`str`, `int`, `float`, `bool`, `None`, list, 문자열 키 mapping)만 쓴다. 시각은 UTC ISO 8601 문자열로 넣는다 | `evidence` | |
| 실행 종류 | 실행 인계가 구분하는 조작의 종류. 최초 대여, 기존 대여의 재조작, 공용형 조작, 서버 내부 요청이다 | `ExecutionKind` | |
| 서버 내부 요청 | 방문자 요청 없이 서버가 시작한 명령 요청. 사용자 ID와 판정 기록이 없다 | `InternalRequest`, `ExecutionKind.SERVER_INTERNAL` | |
| 최초 대여 | 점유 세션을 새로 만드는 대여형 조작 | | |
| 재조작 | 대여 중인 본인이 기존 점유 세션의 기기에 하는 새 조작. 새 요청과 새 명령을 기존 `use_id`에 연결한다 | | |
| 구동 중 | 펌웨어가 명령의 제어 신호를 출력하는 상태. 기기 상태 `ACTIVE`인 대여 중과 구분한다 | 새 상태 필드 형식은 미결이다 | |
| 접수·실행 전 거부 | 기기가 새 명령을 접수하기 전에 검증된 `DEVICE_BUSY`로 거부한 결과. 서버의 확실한 미전송(`NOT_SENT`)과 다르다 | `CommandOutcome.REJECTED` | |
| 처리 중 사유 | 다른 구동 명령이 진행 중이어서 새 조작을 시작하지 못하는 사유. 기기 점유 충돌과 구분한다 | `ReasonCode.COMMAND_IN_PROGRESS` | |
| 요청 처리 중 사유 | 같은 요청을 아직 판정하거나 인계하는 중이라 결과가 없다는 사유. 재전송에만 쓴다. 오류가 아니다 | `ReasonCode.REQUEST_IN_PROGRESS` | |
| 실행 중단 사유 | 재검사 밖의 이유로 상태 관리가 전송을 막은 사유. 재검사 판정과 구분한다 | `ExecutionResult.reason` | |
| 예약 시각 | DB가 예약을 저장한 시각. 예약 대기 제한을 이 시각부터 잰다 | `ReserveResult.reserved_at`, `ExecutionContext.reserved_at` | |
| 복원 조회 | 재시작한 서버가 끝나지 않은 요청, 명령, 점유 세션, 승인 사용분, 복구 건을 기기별로 읽는 조회 | `restore_pending` | |
| 복원 묶음 | 복원 조회가 기기 하나에 반환하는 현재 기기 상태와 연결 기록의 목록 | `RestoreBundle` | |
| 실행 가능 여부 | 재시작 복원과 현재 연결 확인을 마쳐 기기에 새 실행을 허가할 수 있는지 여부 | `ExecutionReadiness.can_execute` | |
| 원래 실행 문맥 | 판정이 인계한 실행 문맥을 저장 기록으로 다시 만든 것. 판정 당시 적용한 설정을 쓴다 | `ExecutionStore.execution_context` | |
| 복구 건 | 기기의 장애 격리 하나에 딸린 복구 업무의 기록. 근거와 복구 승인을 연결한다 | `RecoveryRecord`, `recovery_id` | `store_event_id` |
| 복구 준비 조회 | 복구 건의 준비 상태와 부족한 조건을 읽는 조회. 복구를 승인하지 않는다 | | |
| 복구 정산 | 복구 승인 기록, 승인 사용분 정산, 점유 종료, 기기 복구, 감사 기록, 완료 결과를 한 트랜잭션으로 반영하는 것 | `settle_recovery` | |
| 관리 쓰기 | 계정과 비콘 등록, 기기 신뢰 변경, 장소 배치, 정책 변경을 DB에 저장하는 것 | `AdminRepository` | |
| 전역 할당 | 대기 구간 안의 `any:locker` 요청을 묶어 기기를 1:1로 배정하는 것 | | |

### 상태 값

| 대상 | 값 | 코드 이름 |
|---|---|---|
| 기기 상태 | `AVAILABLE`, `RESERVED`, `ACTIVE`, `CLOSING`, `FAULT` | `DeviceState` |
| 점유 세션 상태 | `RESERVED`, `ACTIVE`, `CLOSING`, `CLOSED` | `SessionState` |
| 종료 사유 | `release`, `leave`, `timeout`, `fault`, `cancelled` | `EndReason` |
| 실행 진행 상태 | `pending`, `in_progress`, `succeeded`, `failed`, `unknown`, `not_sent`, `rejected` | `ExecutionStatus` |
| 사건 반영 결과 | `applied`, `already_applied`, `conflict` | `StoreOutcome` |
| 기기 실행 결과 상태 | `accepted`, `running`, `succeeded`, `failed`, `unknown`, `rejected` | `NodeResultState` |
| 거부 코드 | `SESSION_MISMATCH`, `COMMAND_EXPIRED`, `CLOCK_UNVERIFIED`, `UNSUPPORTED_ACTION`, `COMMAND_ID_CONFLICT`, `INVALID_MESSAGE`, `STORAGE_ERROR`, `DEVICE_BUSY` | `RejectCode` |
| 복구 준비 상태 | `not_ready`, `ready`, `recovering`, `recovered` | `RecoveryStatus` |
| 복구 승인 결과 | `completed`, `rejected`, `pending` | `RecoveryOutcome` |
| 승인 상태 | `pending`, `approved`, `denied` | `approval.status` |
| 승인 사용분 상태 | `reserved`, `held`, `consumed`, `released` | `approval_usage.state` |

### 작업

| 용어 | 뜻 |
|---|---|
| 영역 | 담당 역할 하나가 맡는 폴더 묶음. `architecture.md` §2에 있다 |
| 담당자 | 영역을 맡은 사람 |
| spec | 작업 하나의 명세. `specs/{계층}-{요약}/spec.md` |
| decision PR | 팀 결정 하나를 반영하는 PR. 브랜치는 `decision/{요약}`이다 |
| 추상 요약 | 경계에서 보이는 입력, 출력, 바뀌는 상태, 약속만 쓴 설명. 내부 동작은 쓰지 않는다 |
| 미결 | 아직 정하지 않은 내용. 이슈가 생기면 `미결 — #이슈번호`로 쓴다 |

### 이전 계약 용어

아래 용어는 센서 기반 물리 검증을 쓰던 이전 계약의 뜻이다. 새 실행·반납·복구 조건에는 쓰지 않는다.
기존 코드와 spec의 설명을 읽거나 저장 기록을 이전할 때만 참조한다.

| 용어 | 이전 계약의 뜻 | 코드 이름 | 쓰지 않는 말 |
|---|---|---|---|
| 반납 검사 | 서버의 요청을 받은 기기가 반납의 물리 조건을 새로 검사하는 것. 서버는 검사 결과와 점유 상태, 장애 상태로 종료를 결정한다 | `SafetyCheckRequest`, `SafetyCheckResult` | 안전 검사 |
| 검사 ID | 서버가 반납 검사 시도마다 발급하는 고유 ID. 반납은 `use_id`로 식별한다 | `check_id` | |
| 검사 기준 | 기기가 반납 검사에서 확인하는 물리 조건의 묶음. ID와 버전이 있다. 점유 세션은 생성 당시 종료 정책과 함께 검사 기준을 보존한다 | `CheckCriteria`, `UseSession.check_criteria` | |
| 확인 불능 | 서버가 반납 검사의 결과를 확인하지 못한 상태. 응답 기한 초과, 유효하지 않은 결과, `unknown`이 원인이다. `not_ready`와 구분한다 | | |
| 반납 검사 결과 | 이전 계약의 `passed`, `not_ready`, `fault`, `unknown`이다. 새 계약의 반납 조건에서 제외한다 | `SafetyCheckOutcome` | |
