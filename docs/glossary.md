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

| 용어 | 뜻 | 코드 이름 | 쓰지 않는 말 |
|---|---|---|---|
| 기기 | ESP32 보드 하나와 그 보드가 제어하는 사물함 또는 조명. 앵커 역할도 한다 | `node`, `NodeInfo` | 장치, 노드 |
| 앵커 | 비콘의 RSSI를 측정해서 보고하는 기기의 역할 | `anchor_params` | |
| 비콘 | 방문자 폰이 송출하는 iBeacon 식별자 `(uuid, major, minor)` | `beacon`, `BeaconKey` | |
| 계정 | 토큰으로 식별하는 사용자. 역할은 방문자 또는 소유자다 | `user`, `Account` | |
| 방문자 | 기기를 사용하는 계정 | `Role.VISITOR` | |
| 소유자 | 장소와 기기를 관리하고 접근을 승인하는 계정 | `Role.OWNER` | |
| 장소 | 소유자가 가진 공간. 기기를 배치한다 | `place` | |
| AI 호스트 | 사용자의 요청을 MCP로 서버에 전달하는 LLM 앱. Claude, ChatGPT, Gemini | `host_kind` | |
| 판정 | core가 요청에 내리는 결과. `OK`, `BUSY`, `ASK`, `DENY`, `NEED_APPROVAL` | `Verdict` | |
| 사유 코드 | 판정의 이유 | `ReasonCode` | |
| 허가 | 판정 `OK`로 기기 실행을 허락하는 것 | `Verdict.OK` | 승인 |
| 승인 | 소유자가 제한 기기의 접근 요청을 받아들이는 것 | `approval` | 허가 |
| 승인 사용분 | 승인 하나에서 요청 하나가 쓰는 사용 횟수 | `approval_usage` | |
| 정책 검사 | ④에서 접근 정책과 승인을 검사하는 것. 결과는 허용, 승인 필요, 거부다 | `PolicyChecker.check`, `PolicyCheckResult` | |
| 접근 정책 | 기기의 승인 필요 여부. `open` 또는 `restricted` | `PolicyRevision.mode` | |
| 공개 범위 | 기기를 주변 목록에 보일지 여부. `public` 또는 `private` | `Visibility` | |
| 점유 방식 | 기기를 한 계정이 점유하는지 여부. 대여형 `rental` 또는 공용형 `shared` | `Occupancy` | |
| 대여시간 기준 시각 | 대여시간 초과를 계산하는 기준이다. 접근 승인 만료와 구분한다 | `rental_due_at` | 대여 권한 만료 시각 |
| 초과시간 | 대여시간 기준 시각을 넘긴 시간이다. 종료 전에는 현재 시각, 종료 뒤에는 종료 시각으로 계산한다 | | |
| 대여 기본 시간 | 대여시간 기준 시각을 정하는 시간이다. 자동 반납의 기준이 아니다 | `rental_duration_sec` | |
| 유지 대상자 | 공용 조명을 켜 두는 근거가 되는 계정. 켜기 요청에 성공했고 사용 권한이 유효한 계정이다. 서버는 유지 대상자를 저장하지 않는다 | | 유지 참여자 |
| 초기 참여 대기 | 서버가 재시작 뒤 관측을 재개한 시점부터 새 유지 대상자를 기다리는 시간. 이탈 판단의 미수신 시간과 유예 시간의 합이다 | `leave_absent_sec`, `leave_grace_sec` | |
| 기기 연결 ID | 기기가 MQTT에 접속할 때 새로 만드는 UUID다 | `session_id` | 점유 세션 ID |
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
| 내부 요청 ID | 서버가 외부 요청 ID에 연결하는 전역 고유 ID | `internal_request_id` | |
| 명령 ID | 서버가 물리 명령마다 발급하는 고유 ID | `command_id` | |
| 정규화 입력 | ⓪이 같은 요청인지 대조하는 입력. 대상 지정, 동작, 원 요청 연결 ID로 구성한다 | `input_digest` | |
| 실행 직전 재검사 | 기기에 명령을 보내기 직전에 허가, 근접, 대상을 다시 검사하는 것 | `DecisionRechecker.recheck` | |
| 실행 인계 | 판정이 허가와 예약을 마친 요청의 실행을 상태 관리에 넘기는 것 | `ExecutionContext`, `ExecutionService.execute` | |
| 사건별 저장 | 예약 이후의 사건 하나에 딸린 기록을 한 트랜잭션으로 반영하는 것 | `ExecutionStore` | |
| 전역 할당 | 대기 구간 안의 `any:locker` 요청을 묶어 기기를 1:1로 배정하는 것 | | |

### 상태 값

| 대상 | 값 | 코드 이름 |
|---|---|---|
| 기기 상태 | `AVAILABLE`, `RESERVED`, `ACTIVE`, `CLOSING`, `FAULT` | `DeviceState` |
| 점유 세션 상태 | `RESERVED`, `ACTIVE`, `CLOSING`, `CLOSED` | `SessionState` |
| 종료 사유 | `release`, `leave`, `timeout`, `fault`, `cancelled` | `EndReason` |
| 실행 진행 상태 | `pending`, `in_progress`, `succeeded`, `failed`, `unknown`, `not_sent` | `ExecutionStatus` |
| 사건 반영 결과 | `applied`, `already_applied`, `conflict` | `StoreOutcome` |
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
