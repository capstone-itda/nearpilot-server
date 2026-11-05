# 앱 BLE 기기 설정과 mDNS

- 담당: 작업 지시자 (상태 관리·API·펌웨어·앱)
- 요구사항: FR-12A, NFR-12
- 따르는 결정: #234 (A안. 앱이 서버의 접속 설정 묶음과 사용자가 입력한 Wi-Fi를 BLE로 기기에 넣는다. 서버 주소는 mDNS 이름이다)

## 1. 왜

- 펌웨어를 구운 뒤 Wi-Fi와 서버 주소를 넣으려면 USB 케이블과 PC의 `nearpilot provision`이 필요하다. 시연 장소에서 기기마다 PC에 꽂아야 한다.
- 기기에는 서버 IP가 들어 있어 서버 PC나 AP가 바뀌면 모든 기기를 다시 주입해야 한다.

## 2. 무엇

- 입력:
  - 서버 설정 선택 섹션 `[provisioning]`: `fleet`(기기 목록 파일), `mdns_name`, `mdns_address`(IPv4)
  - 앱: 소유자 토큰, 기기 ID, Wi-Fi 이름·비밀번호
- 출력:
  - REST `GET /admin/nodes/{node_id}/provisioning`: `200` 묶음 `{node_id, mqtt_user, mqtt_password, mqtt_host, mqtt_port, capability, occupancy}`, `Cache-Control: no-store`. 거부는 `403` `NOT_OWNER`, `404` `NOT_FOUND`
  - 서버의 mDNS 알림: `<mdns_name>.local` → `mdns_address`
  - 펌웨어 BLE 설정 서비스: 쓰기 특성 `command`, 알림 특성 `reply`
- 바뀌는 상태:
  - 펌웨어 NVS 설정 (USB 주입과 같은 키 9개, 같은 저장 절차)
  - 서버: 없음. 묶음을 저장하거나 로그에 쓰지 않는다.
- 약속:
  - 묶음의 `mqtt_host`는 `<mdns_name>.local`, `mqtt_port`는 `[mqtt].port`다.
  - BLE 줄 형식과 응답은 USB 시리얼 주입과 같다 (architecture.md §5 "BLE 기기 설정").
  - 설정이 없는 기기는 항상 설정 모드다. 설정이 있는 기기는 실행 중 BOOT 버튼(GPIO0)을 3초 누르면 5분 동안 설정 모드다.
  - `.local` 주소는 기기가 MQTT에 접속할 때마다 mDNS로 찾는다.

## 3. 제약

- 묶음은 기기 비밀번호를 담는다. 응답 외에 로그, 감사 기록, 예외 문구에 쓰지 않는다 (NFR-08).
- 장소 소유권은 `Repository.is_place_owner`로 검사한다. API는 db를 직접 부르지 않는다 (AGENTS.md 의존 규칙 1).
- 펌웨어는 암호화한 링크(LE Secure Connections)의 쓰기만 받고 연결 기기를 저장하지 않는다.
- 시간 값(BOOT 누름 3초, 설정 모드 5분)은 펌웨어 빌드 값이다 (AGENTS.md 코딩 규칙).
- 앱은 묶음을 저장하지 않는다. 화면을 떠나면 지운다.
- mDNS는 Python `zeroconf`를 쓴다. `server/pyproject.toml`에 의존성을 더한다 (architecture.md §8).

## 4. 경계

### 고치는 곳

- `server/pyproject.toml` (`zeroconf`)
- `server/src/nearpilot/shared/` (`models.py` `ProvisioningBundle`, `interfaces.py` `ProvisioningSource`, `NodeAdminService.provisioning`)
- `server/src/nearpilot/core/lifecycle/node_admin.py`
- `server/src/nearpilot/api/rest/nodes.py`
- `server/src/nearpilot/runtime/` (`config.py` `[provisioning]`, `provisioning.py` 추가: 묶음 읽기와 mDNS 알림, `assembly.py`)
- `server/tools/node_lab/provision.py`, `server/tools/final_demo.py`, `server/config/` (`server.example.toml`, `README.md`, `final-demo/`)
- `firmware/` (`src/main.cpp`, `include/config.example.h`, `README.md`)
- `app/app/src/` (`provision/` 추가, `ui/`, `MainActivity.kt`, `AndroidManifest.xml`, 테스트)
- `server/tests/`

### 고치지 않는 곳

- 항상: `docs/`, `AGENTS.md` (Gotchas 절은 제외), `.github/`
- 이 작업: `dashboard/`, `api/mcp/`, MQTT 계약, 시뮬레이터, `firmware/node_lab/`

### 다른 영역과 닿는 곳

| 계약 | 사용 또는 제공 | 상대 영역 |
|---|---|---|
| `ProvisioningSource.bundle(node_id)` (architecture.md §4 "기기 접속 설정 묶음") | 제공 (조립), 사용 (상태 관리) | 조립·상태 관리 |
| `NodeAdminService.provisioning(user_id, node_id)` | 제공 (상태 관리), 사용 (API) | API |
| REST `GET /admin/nodes/{node_id}/provisioning` | 제공 (API), 사용 (앱) | 앱 |
| BLE 설정 서비스 (architecture.md §5 "BLE 기기 설정") | 제공 (펌웨어), 사용 (앱) | 앱·펌웨어 |
| mDNS `<mdns_name>.local` | 제공 (서버), 사용 (펌웨어) | 펌웨어 |

### 만들지 않는 것

- 앱의 기기 목록 조회 (기기 ID는 사용자가 입력한다)
- 기기의 Wi-Fi 목록 검색
- 서버가 기기 비밀번호를 새로 만드는 기능 (기존 비밀번호 파일을 읽는다)
- iOS 앱

## 5. 예외 상황

| 상황 | 기대 동작 | 테스트 |
|---|---|---|
| 장소 소유자가 아니다 | `403` `NOT_OWNER`. 묶음을 읽지 않는다 | `test_FR12A_provisioning_rejects_non_owner`, `test_FR12A_provisioning_route_maps_errors` |
| `[provisioning]`이 없거나, 목록에 없는 기기거나, 비밀번호 파일이 없다 | `404` `NOT_FOUND` | `test_NFR12_fleet_source_missing_device` |
| 토큰이 없거나 틀렸다 | `401` `INVALID_TOKEN` | `test_FR12A_provisioning_route_requires_token` |
| 내부 예외 문구에 비밀번호가 있다 | 응답과 로그에 쓰지 않는다 | `test_NFR08_provisioning_never_logged` |
| `[provisioning]` 값이 틀렸다 (이름 형식, IPv4가 아님, 목록 파일 없음) | 시작하지 않는다 | `test_NFR12_provisioning_config_validated` |
| mDNS 알림을 시작하지 못했다 (포트 사용 중 등) | 오류를 로그에 남기고 서버는 계속한다. 묶음 조회는 그대로다 | `test_NFR12_mdns_failure_keeps_server` |
| 암호화하지 않은 링크로 쓴다 | 펌웨어가 쓰기를 거부한다 (NimBLE `WRITE_ENC`) | 실제 기기 확인 |
| 설정 모드가 아닌 기기 | 설정 서비스를 광고하지 않고 BLE 줄을 무시한다 | 실제 기기 확인 |
| `.local` 이름을 찾지 못했다 | `MQTT_HOST_UNRESOLVED`를 남기고 재시도 간격을 늘린다 | 실제 기기 확인 |

## 6. 결정

- 기기 목록 파일은 `nearpilot provision`과 최종 시연이 쓰는 `fleet.json`이다. 기기의 `username`(없으면 `node_id`)이 `mqtt_user`, `password_file`(없으면 목록 파일 옆의 `.env.<node_id>`)이 비밀번호다. 묶음은 조회할 때마다 파일에서 읽는다.
- `ProvisioningSource` 구현과 mDNS 알림은 `runtime/provisioning.py`에 둔다 — 서버 설정과 파일을 읽는 조립 영역이다.
- mDNS는 `_mqtt._tcp` 서비스 하나를 `<mdns_name>.local`과 `mdns_address`로 등록한다. 호스트 이름의 A 질의에도 답한다.
- mDNS 시작 실패는 서버를 멈추지 않는다 — 기기는 IP 주소 설정으로도 접속할 수 있고, 묶음 조회는 mDNS와 무관하다.
- `nearpilot provision`(USB)도 `[provisioning]`이 있으면 `mqtt_host`를 `<mdns_name>.local`로 넣는다 — 두 경로가 같은 값을 넣는다.
- 최종 시연 구성은 hardware 모드에만 `[provisioning]`을 넣는다(`mdns_name = "nearpilot"`, 서버 주소). sim 모드는 같은 PC라 알리지 않는다.
- 펌웨어 BLE:
  - 설정 서비스와 두 특성의 UUID는 `config.example.h`의 고정 값이다. 광고 이름은 `NP-<node_id>`, 설정이 없으면 `NP-setup`이다.
  - 쓰기 하나를 줄 하나로 본다(끝의 줄바꿈은 지운다). 200바이트를 넘으면 `CONFIG_REJECTED reason=line_too_long`이다.
  - BLE 콜백은 줄을 큐에 넣기만 하고, loop가 USB와 같은 함수로 처리한다.
  - GATT 표는 부팅 때 비콘 검색을 시작하기 전에 등록한다 — NimBLE는 첫 광고 때 등록하는데, 설정이 있는 기기는 그때 검색 중이라 등록이 거부되고 assert로 멈춘다.
  - 응답 줄은 20바이트씩 나눠 알리고 줄바꿈으로 끝낸다 — MTU와 상관없이 앱이 줄을 다시 잇는다. 같은 줄을 시리얼에도 쓴다.
  - 설정 모드가 끝나면 광고를 멈추고 연결을 끊는다.
- 펌웨어 mDNS: Wi-Fi 연결 뒤 `MDNS.begin("np-<node_id>")`, MQTT 접속 직전 `MDNS.queryHost`(2초)로 찾고 `mqtt.setHost(IP)`로 접속한다.
- 앱: `provision/ConfigLines.kt`가 줄 만들기와 응답 해석을 맡고(JVM 테스트), `provision/BleProvisioner.kt`가 GATT를 맡는다. 쓰기는 응답 줄을 받은 뒤 다음 줄을 쓴다.

## 7. 가정

없음

## 8. 테스트

| 요구사항 | 테스트 | 확인하는 것 |
|---|---|---|
| NFR-12 | `tests/integration/test_runtime_provisioning.py` | 설정 검증, 목록 파일 읽기, mDNS 등록·해제, mDNS 실패 |
| FR-12A | `tests/core/lifecycle/test_node_admin.py` | 소유권 검사, 묶음 없음 |
| FR-12A | `tests/api/test_rest_node_provisioning.py` | 인증, 상태 코드, `no-store`, 로그에 비밀번호 없음 |
| NFR-12 | `tests/tools/test_node_lab_provision.py`, `tests/tools/test_final_demo_config.py` | mDNS 이름 주입, 시연 구성의 `[provisioning]` |
| FR-12A | `app/app/src/test/.../ConfigLinesTest.kt` | 줄 형식, 16진수, 응답 해석 |
