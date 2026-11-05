# 서버 설치 준비 줄이기

- 담당: 작업 지시자 (시연 환경)
- 요구사항: NFR-08, NFR-12
- 따르는 결정: #140 (A안. 시각원은 독립 AP 안에 둔다)

## 1. 왜

- 시연·시험 폴더를 만들려면 `mosquitto_passwd`가 있어야 한다. 브로커 비밀번호 파일을 그 프로그램으로만 암호화한다.
- 기기와 앱이 서버 PC에 붙으려면 Windows 방화벽에서 HTTP, MQTT, NTP 포트를 열어야 한다. 문서에는 "환경에서 준비한다"고만 있어서 사람이 규칙을 직접 만든다. 빠뜨려도 기기에는 연결 실패만 보인다.
- 기기 주입은 PlatformIO 가상환경의 Python으로 저장소 경로의 스크립트를 실행한다. 서버 명령 `nearpilot`으로는 주입할 수 없다.
- 그래서 다른 PC에서 서버를 띄우기 어렵다.

## 2. 무엇

- 입력: `python -m tools.final_demo`, `python -m tools.node_lab init`, `nearpilot --dir DIR provision`
- 출력:
  - `mosquitto_passwd` 없이 만든 브로커 비밀번호 파일 `.env.broker-passwords`
  - 시연 폴더의 방화벽 규칙 스크립트 `firewall.ps1`
  - 운영 명령 `nearpilot provision`. 기존 주입 스크립트와 같은 USB 주입을 한다.
- 바뀌는 상태: 없음. 서버 DB, MQTT 계약, 펌웨어는 그대로다.
- 약속:
  - 비밀번호 파일은 Mosquitto가 읽는 `$7$` 형식이다. 기존 브로커 설정을 바꾸지 않는다.
  - `firewall.ps1`은 사람이 관리자 PowerShell에서 실행한다. 도구는 스크립트를 만들기만 한다.
  - `nearpilot provision`은 기존 `provision.py`와 같은 값을 같은 순서로 주입하고 다시 읽어 확인한다.

## 3. 제약

- 비밀번호와 SSID는 화면과 로그에 쓰지 않는다 (NFR-08).
- 방화벽 규칙은 로컬 서브넷의 원격 주소만 허용한다. 독립 AP 밖으로 포트를 열지 않는다 (NFR-12).
- 시각 신뢰 기간과 재동기화 간격은 명령 인자로 받는다. 코드 상수로 두지 않는다 (AGENTS.md 코딩 규칙).
- 시각원은 #140 A안대로 독립 AP 안의 NTP다. NTP 규칙(UDP 123)은 실제 기기를 쓰는 hardware 구성에만 넣는다 (NFR-12).

## 4. 경계

### 고치는 곳

- `server/tools/final_demo.py`
- `server/tools/node_lab/` (`setup.py`, `provision.py`, `README.md`)
- `server/src/nearpilot/cli/` (`provision.py` 추가, `__init__.py`에 등록)
- `server/config/final-demo/` (`README.md`, `firewall.template.ps1` 추가)
- `server/config/README.md`
- `server/tests/tools/`, `server/tests/cli/`, `server/tests/integration/`

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `server/src/nearpilot/shared/`, `server/pyproject.toml`, `.github/`
- 이 작업: `server/tools/evaluation/`(평가 하네스는 `mosquitto_passwd`를 그대로 쓴다), `firmware/`, `app/`, MQTT 계약

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| Mosquitto 비밀번호 파일 형식 (`password_file`) | 사용 | 외부 (Mosquitto) |
| 펌웨어 USB 설정 명령 (`CONFIG_BEGIN`·`CONFIG_SET`·`CONFIG_COMMIT`·`CONFIG?`) | 사용 (변경 없음) | 펌웨어 |

### 만들지 않는 것

- Mosquitto 설치 자동화
- 방화벽 규칙의 자동 실행
- NTP를 대신하는 시각 전달. 시각 계약을 바꾸므로 팀 결정이 필요하다.
- 앱의 BLE 주입과 mDNS 서버 찾기 (#234)
- 기기 원격 재시작 (#235)
- `pyserial` 의존성 추가. `server/pyproject.toml`은 경계 밖이다.

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| `mosquitto_passwd`가 설치되지 않았다 | 시연·시험 폴더를 그대로 만든다 | `test_NFR12_final_demo_without_mosquitto_passwd` |
| 같은 비밀번호를 두 번 해시한다 | 솔트가 달라 결과가 다르다. 둘 다 같은 비밀번호로 검증된다 | `test_NFR08_broker_hash_uses_random_salt` |
| sim 구성에 방화벽 스크립트를 만든다 | 기기가 같은 PC라 NTP 규칙을 넣지 않는다 | `test_NFR12_final_demo_firewall_rules` |
| `pyserial`이 없다 | 설치 명령을 안내하고 종료 코드 2로 끝난다. 포트를 열지 않는다 | `test_NFR12_provision_without_pyserial` |
| `--dir` 없이 `nearpilot provision`을 부른다 | 종료 코드 2로 끝난다 | `test_NFR12_provision_requires_dir` |
| 기기가 설정을 거부하거나 다시 읽은 값이 다르다 | 기존 주입과 같이 실패를 기록하고 종료 코드 1로 끝난다 | `test_NFR12_provision_device_failure_exit_code` |

## 6. 결정

- 비밀번호 해시는 PBKDF2-HMAC-SHA512, 반복 101회, 무작위 솔트 12바이트의 `$7$` 형식이다 — `mosquitto_passwd` 기본값과 같은 형식이라 기존 브로커 설정이 그대로 읽는다. 담당자가 2026-11-05에 골랐다.
- 방화벽은 실행하지 않고 스크립트를 만든다 — 보안 설정 변경은 사람이 확인하고 실행한다. 규칙은 그룹 `NearPilot`으로 묶고, 스크립트는 그룹의 기존 규칙을 지운 뒤 다시 만든다. `-Remove`는 그룹 규칙을 지우기만 한다.
- `nearpilot provision`의 `--time-server`를 생략하면 시험 폴더 서버 설정의 `[mqtt].host`를 쓴다 — 시연 구성에서는 브로커와 시각원이 같은 서버 PC다.
- `nearpilot provision`은 `tools/node_lab/provision.py`의 함수를 부른다. 주입 절차를 두 곳에 두지 않는다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-08 | `test_NFR08_broker_hash_matches_mosquitto_format` | 알려진 솔트로 만든 해시가 PBKDF2-SHA512 `$7$101$` 형식과 값이 같다 |
| NFR-08 | `test_NFR08_broker_hash_uses_random_salt` | 솔트가 매번 다르다 |
| NFR-08 | `test_NFR08_final_demo_credentials` | 비밀번호 파일에 평문이 없다 |
| NFR-12 | `test_NFR12_final_demo_without_mosquitto_passwd` | `mosquitto_passwd` 없이 폴더를 만든다 |
| NFR-12 | `test_NFR12_final_demo_firewall_rules` | hardware 구성은 HTTP·MQTT·NTP 규칙, sim 구성은 HTTP·MQTT 규칙을 로컬 서브넷에만 연다 |
| NFR-12 | `test_NFR12_provision_*` | `nearpilot provision`의 인자, 기본 시각원, 오류 종료 코드 |
| NFR-12 | `test_FR01_FR04_FR12A_FR13_FR14_FR16_final_demo_real_broker` | 실제 Mosquitto가 Python으로 만든 비밀번호 파일로 서버와 기기를 인증한다 |
