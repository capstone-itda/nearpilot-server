# 비콘 백그라운드 송출 앱

- 담당: eunji719 (앱)
- 요구사항: FR-02, NFR-12
- 따르는 결정: 없음

## 1. 왜

방문자 폰이 비콘을 송출하지 않으면 기기가 RSSI를 보고하지 못한다. 서버는 계정–비콘 대조(①)를 통과하지 못하고, 모든 요청을 거부한다 (FR-15).
앱(`app/`)은 비어 있어서 지금은 시험용 타사 앱으로만 비콘을 송출할 수 있다.

## 2. 무엇

- 입력: 방문자가 입력한 비콘 식별자 `(uuid, major, minor)`
- 출력: 입력한 식별자의 iBeacon 광고. 화면과 알림에 표시하는 송출 상태
- 바뀌는 상태: 폰에 저장한 비콘 식별자. 송출 상태(`송출 중`, `중지`, `중단됨`)
- 약속:
  - 앱은 송출을 시작하면 화면을 끄거나 다른 앱으로 가도 송출을 계속한다 (FR-02).
  - 앱은 송출 상태를 표시한다. 앱은 송출이 끊기면 `중단됨`과 사유를 표시한다 (FR-02).
  - 앱은 인터넷이나 서버 연결 없이 송출한다 (NFR-12).

## 3. 제약

- 앱은 Android 앱이다. iOS는 범위 밖이다 (product.md).
- 송출 형식은 iBeacon이다. 식별자는 `(uuid, major, minor)`다 (glossary.md "비콘").
- `uuid`는 하이픈을 포함한 UUID 문자열이다. `major`와 `minor`는 0~65535의 정수다 (architecture.md §5 RSSI).
- 앱은 등록한 식별자 하나만 송출한다. 계정당 활성 비콘은 하나 이하다 (FR-01).
- 앱은 송출 성공을 추정하지 않는다. Android가 광고 시작을 확인한 뒤에만 `송출 중`을 표시한다 (FR-02).

## 4. 경계

### 고치는 곳
- `app/`

### 고치지 않는 곳
- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/`, `firmware/`, `dashboard/`

### 다른 영역과 닿는 곳
| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| iBeacon 식별자 `(uuid, major, minor)` | 제공 (BLE 광고) | 기기 (펌웨어가 수신해 `rssi` 토픽으로 보고) |

### 만들지 않는 것
- 계정과 비콘 등록, 토큰 발급 (FR-01). REST 경로가 미결이다.
- 소유자 승인 화면과 승인 알림 (FR-03, FR-14). REST 경로와 알림 전달 수단이 미결이다.
- 송출 상태를 서버나 대시보드에 보내는 기능

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `uuid` 형식이 틀렸다 | 저장과 송출을 거부하고 입력 오류를 표시한다 | `FR02_rejects_invalid_uuid` |
| `major`나 `minor`가 0~65535 밖이거나 정수가 아니다 | 저장과 송출을 거부하고 입력 오류를 표시한다 | `FR02_rejects_out_of_range_major_minor` |
| 블루투스가 꺼져 있다 | 송출을 시작하지 않고 블루투스를 켜라고 표시한다 | 수동 확인 |
| 송출 중에 블루투스가 꺼졌다 | `중단됨`과 사유를 표시한다. 블루투스가 다시 켜져도 자동으로 재시작하지 않는다 | `FR02_bluetooth_off_marks_interrupted`, 수동 확인 |
| 블루투스 광고 권한이 없거나 거부됐다 | 송출을 시작하지 않고 권한이 필요하다고 표시한다 | 수동 확인 |
| 기기가 BLE 광고를 지원하지 않는다 | 송출을 시작하지 않고 지원하지 않는 기기라고 표시한다 | 수동 확인 |
| Android가 광고 시작을 실패로 알렸다 | `중단됨`과 실패 코드를 표시한다 | `FR02_start_failure_marks_interrupted` |
| 송출 중에 앱 화면을 닫았다 | 포그라운드 서비스가 송출을 계속한다. 알림에 `송출 중`을 표시한다 | 수동 확인 |
| 송출 중에 사용자가 시작을 다시 눌렀다 | 광고를 하나만 유지한다 | `FR02_start_twice_keeps_single_advertisement` |

## 6. 결정

- Kotlin과 Android Studio(Gradle)로 만든다 — 백그라운드 송출과 BLE 권한을 Android 공식 API로 직접 다룬다.
- iBeacon 광고는 Android 기본 `BluetoothLeAdvertiser`로 직접 만든다 — 외부 라이브러리 없이 패킷을 투명하게 다룬다. 제조사 데이터는 Apple ID `0x004C`, 접두 `02 15`, UUID 16바이트, major 2바이트, minor 2바이트, 측정 전력 1바이트다. major와 minor는 빅엔디언이다.
- 백그라운드 송출은 포그라운드 서비스와 상시 알림으로 유지한다 — Android는 화면 밖 앱의 BLE 동작을 제한한다.
- 송출 로직(식별자 검증, 패킷 생성, 상태 전이)은 Android API와 분리해 JVM 단위 테스트로 확인한다 — 서버의 pytest 규칙은 앱에 적용되지 않는다. 테스트 이름에 FR ID를 쓴다.
- 식별자는 폰 안에만 저장한다 — 등록 REST가 정해지기 전까지 서버와 주고받지 않는다.

## 7. 가정

- 담당자가 앱 담당을 맡는다 — 앱 담당은 미결이다 (architecture.md §2).
- 서버에 등록할 식별자는 방문자가 직접 입력한다 — 계정 등록 REST 경로는 미결이다. 경로가 정해지면 입력 대신 등록 결과를 저장하도록 후속 작업에서 바꾼다.
- 최소 지원 버전, 측정 전력 값, 광고 간격과 송신 세기는 구현할 때 정해 "결정" 절에 쓴다.

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| FR-02 | `FR02_builds_ibeacon_manufacturer_data` | 식별자를 iBeacon 제조사 데이터로 바르게 바꾼다 |
| FR-02 | `FR02_rejects_invalid_uuid` | 형식이 틀린 `uuid`를 거부한다 |
| FR-02 | `FR02_rejects_out_of_range_major_minor` | 범위 밖 `major`·`minor`를 거부한다 |
| FR-02 | `FR02_start_success_marks_broadcasting` | 광고 시작을 확인한 뒤에만 `송출 중`이 된다 |
| FR-02 | `FR02_start_failure_marks_interrupted` | 광고 시작 실패를 `중단됨`과 실패 코드로 바꾼다 |
| FR-02 | `FR02_bluetooth_off_marks_interrupted` | 송출 중 블루투스 꺼짐을 `중단됨`으로 바꾼다 |
| FR-02 | `FR02_start_twice_keeps_single_advertisement` | 시작을 다시 눌러도 광고를 하나만 유지한다 |
| FR-02, NFR-12 | 수동: ESP32 BLE 스캔 스케치 | 화면을 끄고 인터넷을 끊어도 ESP32 시리얼에 입력한 `uuid`·`major`·`minor`가 계속 찍힌다 |
