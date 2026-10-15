# MQTT v1 메시지 형식과 shared 모델 변환

- 담당: 이 작업을 지시한 담당자 (상태 관리의 IOT 구현 범위)
- 구현 브랜치: `feat/iot-messages`
- 요구사항: FR-04, FR-05, FR-06, FR-07, FR-20, NFR-08, NFR-11
- 따르는 결정: architecture.md에 반영한 #19, #31, #38, #140~#143이다.
- 상태: 구현 전 초안이다. 전체 계획은 [plan.md](../iot-mqtt-gateway/plan.md)에 있다.

## 1. 왜

서버는 잘못된 MQTT 본문을 관측과 실행 결과로 받아들이면 안 된다.
펌웨어 본문과 `shared` 모델의 필드 이름이 달라 명시적인 변환이 필요하다.

## 2. 무엇

- 입력: 토픽, UTF-8 본문, 최초 서버 수신 시각과 MQTT 전달 정보다.
- 출력: `NodeAnnounce`, 비콘별 `RssiFrame`, `NodeStatus`, `NodeResult`, `SafetyCheckResult`다.
- 출력: `NodeCommand`와 `SafetyCheckRequest`에서 만든 MQTT 본문이다.
- 바뀌는 상태: 없음. 변환은 현재 연결·순번·명령 대기를 저장하지 않는다.
- 약속:
  - 일곱 토픽의 v1 필드 의미를 보존한다.
  - 서버 수신 시각은 본문에서 받지 않는다. 호출자가 주입한 최초 수신 시각을 쓴다.
  - 추가 필드는 실행과 안전의 근거로 쓰지 않는다.
  - 형식 검증과 현재 연결·최신성 검증을 구분한다.

## 3. 제약

- 토픽은 `nearpilot/node/{node_id}/{종류}`다. 본문의 기기 ID와 같아야 한다 (NFR-08).
- 본문은 UTF-8 JSON 객체이며 `schema_version=1`이다 (NFR-11, architecture.md §5 MQTT 2절).
- 필수 필드·타입·열거형을 검사한다. 정수 자리의 bool을 허용하지 않는다 (architecture.md §5 MQTT 2절).
- 시각은 RFC 3339의 UTC 문자열이다. 연결 ID와 결과 사건 ID는 UUID다 (architecture.md §5 MQTT 2·3절).
- `seq`는 1 이상의 정수다. 순번의 선후 판정은 보고 검증 spec이 맡는다 (architecture.md §5 MQTT 2절).
- 비콘 UUID는 하이픈을 포함한 소문자 문자열이다. `major`·`minor`는 0~65535의 정수다 (FR-05).
- RSSI는 유한한 수다. 한 본문에 같은 비콘을 두 번 담지 않는다 (FR-05).
- RSSI 본문 전체를 검증한 뒤 같은 연결·순번·시각으로 비콘별 모델을 만든다 (FR-05).
- `physical_state`의 네 필드는 architecture.md §5 MQTT 3절을 따른다. `safe`를 종료 근거로 만들지 않는다 (FR-07).
- `criteria_id`·`criteria_version`은 `CheckCriteria`로 변환한다. v1 필수 근거는 §4의 표를 따른다 (FR-20, #141).
- IOT는 bool 근거를 꾸미지 않는다. 실제 기준 충족과 종료의 결정은 펌웨어·CORE가 맡는다 (FR-07, #143).
- 실행 결과의 상태·오류 코드·사건 ID를 그대로 담는다. 서버 타임아웃을 `NodeResult`로 만들지 않는다 (FR-07).
- `rejected`의 오류 코드는 `RejectCode`다. 검사 결과에 실행용 거부 코드를 추가하지 않는다 (FR-07, #143).
- 송신 본문에 사용자 ID·외부 요청 ID·승인 정보·점유 주체를 넣지 않는다 (NFR-08).
- 명령·검사 ID, 대상 연결·기준·발행 시각·만료 시각을 변환 중 바꾸지 않는다 (FR-06, FR-20).

## 4. 경계

### 고치는 곳

- `server/src/nearpilot/iot/messages/`
- `server/tests/iot/`의 메시지 형식 테스트다.

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md`의 Gotchas 밖, `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`다.
- 이 작업: 연결·명령 대기·CORE 인계, `core/`, `db/`, `api/`, `firmware/`, 시뮬레이터다.

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| 기기 보고의 다섯 shared 모델 | 변환에 사용한다 | shared |
| `NodeCommand`, `SafetyCheckRequest` | 송신에 사용한다 | CORE |
| MQTT v1 본문 | 검증·변환한다 | 펌웨어 |

### 만들지 않는 것

- 현재 연결·시각 나이·순번 검사는 `iot-event-delivery`가 맡는다.
- 계정 인증과 브로커 권한은 `iot-mqtt-gateway`가 맡는다.
- 실행 허가·반납 종료와 DB 저장은 CORE가 맡는다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| JSON 객체가 아니거나 필수 타입이 틀리다 | 변환을 거부한다 | `test_FR07_invalid_payload_rejected` |
| 토픽과 본문의 기기 ID가 다르다 | 변환을 거부한다 | `test_NFR08_topic_payload_identity_mismatch` |
| 추가 필드에 `safe=true`가 있다 | 실행·종료 근거로 쓰지 않는다 | `test_FR07_extra_fields_not_safety_evidence` |
| RSSI가 NaN·무한대이거나 비콘이 중복이다 | 본문 전체를 거부한다 | `test_FR05_invalid_rssi_batch_rejected` |
| 관측 배열이 비었다 | 빈 결과다. 가짜 프레임을 만들지 않는다 | `test_FR05_empty_observations_no_fake_frame` |
| 본문에 여러 비콘이 있다 | 공통 메타데이터로 비콘별 모델을 만든다 | `test_FR05_batch_preserves_metadata` |
| IR 무드등이 `light_on=null`을 보고한다 | `None`을 보존한다 | `test_FR07_unobserved_light_is_none` |
| 검사 결과에 필수 근거가 없다 | 유효한 통과로 만들지 않는다 | `test_FR20_missing_evidence_never_passes` |
| 명령을 직렬화한다 | 내부 계정 정보 없이 허용한 필드만 담는다 | `test_FR06_encode_preserves_command` |
| 검사를 직렬화한다 | ID·기준·연결·시각을 보존한다 | `test_FR20_encode_preserves_check` |

## 6. 결정

- 본문 검증을 `iot/messages/`로 분리한다 — 담당자가 2026-10-15에 선택한 책임 분리를 따른다.
- 형식 변환은 부작용 없이 작성한다 — 최초 수신 시각을 보존하고 개별 변환을 검증한다.
- RSSI 본문 전체를 검증한 뒤 모델을 만든다 — 잘못된 본문의 앞 항목만 인계하지 않게 한다.

## 7. 가정

- #141의 v1 기준은 §4 표다. 미래 센서 조건을 같은 버전에 추가하지 않는다.
- #142는 확인 불가를 `unknown`·`null`로 보존한다. 스피커 새 필드는 후속 계약 전 추가하지 않는다.
- #143은 기존 확인 불능 경로를 쓴다. 오류용 새 상태·필드는 후속 계약 전 추가하지 않는다.
- 실제 시각 오차와 상태 허용 나이는 보고 검증 spec이 미결로 관리한다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-04 | `test_FR04_announce_to_shared_model` | 기능·점유 방식·공통 메타데이터를 변환한다 |
| FR-05 | 5절의 `test_FR05_*` | 빈 배열·일괄 관측·비콘과 RSSI를 검증한다 |
| FR-06 | `test_FR06_encode_preserves_command` | 명령과 유효 시간을 보존한다 |
| FR-07 | 5절의 `test_FR07_*` | 원시 관측과 결과 사건을 보존한다 |
| FR-20 | 5절의 `test_FR20_*` | 검사 요청과 근거를 변환한다 |
| NFR-08 | `test_NFR08_outbound_allowlist` | 내부 계정 정보를 보내지 않는다 |
| NFR-11 | `test_NFR11_all_v1_message_shapes` | 일곱 토픽의 v1 형식을 확인한다 |

실제 브로커와 다른 계층 구현은 필요 없다. 입력과 변환 결과를 직접 비교한다.
