# 실제 기기의 관측과 명령 연결 확인

- 확인 날짜: 2026-10-23이다.
- 기기는 ESP32-WROOM-32E 한 대다. 출력은 시리얼이다. 외부 센서는 없다.
- Android가 시험 비콘을 송출한다. 노트북과 기기는 휴대폰 핫스팟에 연결한다.
- 앱 인증, REST와 WebSocket은 사용하지 않았다. 기존 Runtime과 CORE를 시험 콘솔에서 호출했다.
- 설정과 원본 로그는 로컬 `server/var/node-lab-hotspot/`에 있다. 비밀번호는 Git에서 제외한다.

## 시각 실측

Windows NTP 응답의 leap 0과 stratum 5를 확인했다. ESP32에서 `CLOCK_SYNCED`를 확인했다.
서비스 재시작 뒤 미동기화 응답은 관리자 재동기화로 복구했다.

| 조건 | 기간 | 유효 표본 | USB 지연을 포함한 오차 구간의 최대 절댓값 |
|---|---|---|---|
| 보고 차단 상태에서 재부팅 | 150초 | 40개 | 0.0226713초 |
| 상태와 RSSI 보고, 실행 요청을 포함한 재부팅 | 95초 | 18개 | 0.6391453초 |

두 번째 구간의 큰 값은 측정 응답의 대기 시간까지 포함한다. 실제 시계 차이와 같다고 단정하지 않는다.
명시한 여유 0.25초를 더하고 올림해서 최종 시험 설정은 양쪽 모두 1초다.
초기 연결 요청 때는 첫 실측에 따른 0.3초를 사용했다. 부하 실측 뒤 최종 값을 넓혔다.
이 수치는 같은 환경의 짧은 시험에만 적용한다. 운영용 장시간 오차 보증이 아니다.
첫 실측에서 동기화 경과 시간이 다시 감소했다. 재동기화를 확인했다.
원본은 `clock-measurement-1.jsonl`, `clock-measurement-enabled.jsonl`, `clock-applied.json`이다.

## 실제 실행 경로

1. 기기는 Android 비콘의 RSSI를 수신했다.
2. 서버는 실제 RSSI와 물리 상태를 저장했다. 실행 가능 여부가 `ready=true`로 바뀌었다.
3. 서버는 합성 모델 `synthetic-1`로 확률을 계산했다. 실측 정확도 검증은 아니다.
4. 휴대폰을 가까이 옮긴 뒤 확률이 실행 기준 0.75를 넘었다.
5. 시험 콘솔은 `on`을 기존 CORE에 요청했다. 요청의 `p_max`는 약 0.90이었다.
6. 서버는 명령을 전송했다. 기기는 아래 로그로 명령을 접수하고 시리얼로 처리했다.
7. 기기는 물리 완료 근거가 없어 `unknown`을 보고했다. 서버는 기기를 `FAULT`로 격리했다.
8. 이후 `off` 요청은 `DENY / NO_CANDIDATE`였다. 장애 기기를 다시 실행하지 않았다.

```text
COMMAND_RECEIVED command_id=d6959eba-d2a8-4175-a831-835ac240d69c
COMMAND_RESULT command_id=d6959eba-d2a8-4175-a831-835ac240d69c state=accepted
COMMAND_RESULT command_id=d6959eba-d2a8-4175-a831-835ac240d69c state=running
COMMAND_HANDLED command_id=d6959eba-d2a8-4175-a831-835ac240d69c action=on output=serial_only physical_completion=unverified
COMMAND_RESULT command_id=d6959eba-d2a8-4175-a831-835ac240d69c state=unknown
```

서버 `command_result`도 같은 명령 ID와 `status=unknown`을 기록했다.
최종 요청 응답은 `DENY / EXEC_UNKNOWN`이다. 명령 전송 전 거부와 구분한다.
실제 조명 제어와 물리 성공은 확인하지 않았다. 센서 없이 성공 결과를 만들지 않았다.

## 남은 검증과 기존 문제

- 운영용 시각 오차에는 장시간, 시각원 단절과 변경 시험이 더 필요하다.
- 실제 근접 품질에는 실측 모델과 기기 두 대 이상의 비교가 더 필요하다.
- 시리얼 처리 뒤 물리 성공에는 출력 장치와 완료 관측 수단이 필요하다.
- 공용형 결과 재인계의 기존 `ValueError`도 재현됐다. 원인은 `KNOWN_ISSUES.md`를 따른다.
- 결과 재인계 경고를 완료 성공으로 숨기지 않았다. 시험 DB의 `FAULT`를 강제로 풀지 않았다.

## 소프트웨어 검증

- 전체 서버 검사: `2737 passed, 2 skipped, 1 xfailed`다. 실제 Mosquitto를 포함한다.
- 두 건은 Windows 파일명 제약으로 건너뛴다. 예상 실패는 기존 합성 모델 검사다.
- 펌웨어 `esp32dev` 빌드와 실제 COM6 업로드를 확인했다.
- 시각 구간 검사는 USB 지연 포함, 서버 시계 점프와 응답 지연 초과를 검증한다.

## 공통 펌웨어의 자동 연결 (2026-10-23)

같은 ESP32-WROOM-32E(ESP32-D0WD-V3) 한 대와 Android 핫스팟으로 확인했다.
펌웨어 업로드는 앱 영역만 쓴다. NVS의 설정과 명령 기록은 업로드 뒤에도 남았다.
원본은 로컬 `server/var/node-lab-hotspot/`의 `boot-*.jsonl`, `provision.jsonl`, `run-auto-*.log`다.
서버는 시험 콘솔의 `node_readiness`로 확인했다. 시험 DB의 `light-1`은 이전 시험의 `FAULT`다.
모든 시나리오에서 서버는 `fault=true`, `ready=false`(`fault_recovery`)를 유지했다. 재부팅·재주입으로 풀리지 않았다.

| 시나리오 | 방법 | 기기 | 서버 |
|---|---|---|---|
| 설정 없는 부팅 | USB RTS 재시작 | `CONFIG_MISSING`, 네트워크 미시작 | — |
| USB 설정 주입 | `provision.py` | 11개 값 저장, 재시작 뒤 `CONFIG_STATE` 일치. 로그에는 비밀값의 바이트 수만 | — |
| 서버·시각원에 닿지 않음 | 노트북이 핫스팟 밖 | Wi-Fi 1.5초. MQTT `error=-3` 4·8·16·30·30초 재시도. 발표·status·RSSI 없음 | — |
| 시각원 미동기화 응답 | Windows NTP가 leap 3, stratum 0 응답 | MQTT 접속 뒤 `ANNOUNCE_DEFERRED`. 발표·status·RSSI 없음 | 온라인 아님 |
| 정상 부팅 (EN 대체) | USB RTS 재시작 | Wi-Fi 1.5초, MQTT 2.2초, 신뢰 2.7초, 발표 2.8초, `NODE_READY` 3.0초 | 새 연결 `online=true` |
| EN 버튼 | 사람이 누름 | 마지막 부팅 기준 `NODE_READY` 2.5초, 순서 위반 없음. 3.3초 간격 재시작 두 번이 기록됐다 | 새 연결 `online=true` |
| 전원 재투입 | USB 케이블 5초 분리 | 다시 꽂은 뒤 설정 유지(`generation=1`) | 15초 뒤 `online=false`, 재투입 뒤 새 연결 `online=true` |
| 서버 강제 종료·재시작 | 시험 콘솔 프로세스 강제 종료 | 연결 유지 | 재시작 5초 안에 같은 연결 `online=true` |
| 브로커 재시작 | 시험 Mosquitto 종료 뒤 약 50초 뒤 시작 | `MQTT_DISCONNECTED`, 4·8·16·30초 재시도, 다음 시도에 새 연결·발표 | 15초 뒤 `online=false`, 재접속 3초 뒤 `online=true` |
| Wi-Fi 끊김 | 핫스팟 약 30초 끔 | `WIFI_DISCONNECTED reason=1`, `MQTT_DISCONNECTED error=-5 wifi=0`, 20·30초 재시도, 63초 뒤 `NODE_READY`. 시각 신뢰는 유지 | 14초 뒤 `online=false`, 새 연결 `online=true` |
| 시각원 단절 | `Stop-Service w32time` | 마지막 동기화 120초 뒤 `CLOCK_TRUST_EXPIRED`, `NODE_NOT_READY reason=clock_trust_expired`, 보고 중단 | 15초 뒤 `online=false` |
| 시각원 복구 | `Start-Service w32time`, 재동기화 | 약 2분 뒤 재동기화, 같은 연결로 `NODE_READY` | 1초 뒤 `online=true` |
| 펌웨어 재업로드 | PlatformIO upload | 설정 유지, `NODE_READY` 4.3초 | 새 연결 `online=true` |

### 시험 중 고친 펌웨어 결함

- 발행 중 끊긴 연결을 다음 반복의 재접속이 먼저 처리해서 `MQTT_DISCONNECTED`와 오류 코드가 빠졌다. 재접속 전에 끊김을 기록한다.
- 같은 반복에서 시각 신뢰를 두 번 읽어 SNTP 콜백 사이에 `NODE_READY`가 `CLOCK_TRUSTED`보다 먼저 찍혔다. 한 번 읽은 값으로 판정한다.
- lwIP SNTP는 실패 뒤 재시도 간격을 늘려 시각원 복구 뒤 약 2분 걸렸다. 신뢰가 없는 동안 30초마다 SNTP를 다시 요청한다(`CLOCK_RETRY`). 이 개선은 시각원 단절로 다시 시험하지 않았다.

### 미해결 관찰

- 연결이 정상인데도 가끔 MQTT 응답 대기가 2초(`NP_MQTT_IO_TIMEOUT_MS`)를 넘어 `error=-9`로 끊기고 0.2초 안에 새 연결로 재접속한다. 약 30분 동안 원인 코드가 없던 수정 전 재접속 2회, 발행 중 `-9` 끊김 1회, 구독 `-9` 실패 1회를 관찰했다.
- 같은 시간 ping 60회는 손실 0, 최대 93ms였다. 후보는 BLE 스캔 창 90%와 Wi-Fi의 무선 공유, 또는 짧은 대기 시간이다. 원인을 확정하지 않았고 값을 바꾸지 않았다.
- 재접속은 연결 ID를 바꾼다. 그 순간 진행 중인 명령은 `SESSION_MISMATCH`로 거부될 수 있다. 명령 전송 시점과 겹치는 경우는 시험하지 않았다.
- 이 보드는 EN 버튼, USB RTS 재시작, 전원 재투입을 모두 `reset_reason=power_on`으로 기록한다. 펌웨어 재시작만 `software`다.
- Wi-Fi 재시도 상한 30초 때문에 AP가 돌아온 뒤 재접속까지 최대 30초가 더 걸린다.
- USB 포트를 여는 순간 보드가 재시작될 때가 있었다. 전원 재투입 직후 부팅 로그는 포트가 다시 잡히는 동안 놓쳤다. 서버 로그와 `CONFIG?`로 확인했다.

### 한 대로 확인하지 못한 것

- 여러 기기의 동시 부팅, 기기 간 구분, 다른 기기 토픽에 대한 ACL 거부
- 서버·시각원 주소 변경 뒤 재주입. 노트북 주소가 바뀌지 않아 실행하지 않았다
- 명령 기록이 있는 기기의 ID 변경 거부. 펌웨어 검토만 했다
- 장시간 동작의 시각 오차와 시각원 변경 뒤 오차
